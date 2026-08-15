"""Everything Popup Stopper can actually change on the system.

Each action is reversible and records enough state to undo it, because the
tool is meant to be used while diagnosing interruptions, not as a one-way
door. Actions that need administrator rights say so in their result.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import winreg
from pathlib import Path
from typing import Any

from . import winapi as w
from .config import APP_NAME, Config, PROJECT_ROOT, PROTECTED_PROCESSES
from .tasks import task_definition

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000

NOTIFICATION_SETTINGS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings"
UPDATE_UX_KEY = r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
RESTART_NOTIFICATION_VALUE = "RestartNotificationsAllowed2"


def _result(ok: bool, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": ok, "message": message, **extra}


# -- windows ---------------------------------------------------------------


def close_window(hwnd: int, exe_name: str = "") -> dict[str, Any]:
    if exe_name and exe_name.lower() in PROTECTED_PROCESSES:
        return _result(False, f"{exe_name} is protected and will not be closed")
    if not hwnd:
        return _result(False, "no window handle")
    if not w.user32.IsWindow(hwnd):
        return _result(False, "window already gone")
    if w.close_window(hwnd):
        return _result(True, "close requested")
    return _result(False, "the window refused to close")


# -- toast notifications ---------------------------------------------------


def get_toast_enabled(aumid: str) -> bool | None:
    """True/False from the registry, or None when the app has no entry yet."""
    if not aumid:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{NOTIFICATION_SETTINGS_KEY}\{aumid}") as key:
            try:
                value, _ = winreg.QueryValueEx(key, "Enabled")
                return bool(value)
            except FileNotFoundError:
                # No explicit value means Windows defaults the app to enabled.
                return True
    except (FileNotFoundError, OSError):
        return None


def set_toast_enabled(aumid: str, enabled: bool) -> dict[str, Any]:
    """Flip the per-app notification switch, the same one the Settings app writes."""
    if not aumid:
        return _result(False, "no application id")
    try:
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            rf"{NOTIFICATION_SETTINGS_KEY}\{aumid}",
            0,
            winreg.KEY_SET_VALUE,
        )
        with key:
            winreg.SetValueEx(key, "Enabled", 0, winreg.REG_DWORD, 1 if enabled else 0)
    except OSError as exc:
        return _result(False, f"could not update the registry: {exc}")
    state = "enabled" if enabled else "muted"
    log.info("Toasts for %s are now %s", aumid, state)
    return _result(True, f"notifications {state}", aumid=aumid, enabled=enabled)


PUSH_NOTIFICATIONS_KEY = r"Software\Microsoft\Windows\CurrentVersion\PushNotifications"


def notifications_globally_enabled() -> bool:
    """Whether Windows shows toasts at all.

    When this is off, no application can raise a toast, so Popup Stopper's
    toast watcher will legitimately never see anything and should say so
    rather than appear broken.
    """
    for root, sub, value_name in (
        (winreg.HKEY_CURRENT_USER, PUSH_NOTIFICATIONS_KEY, "ToastEnabled"),
        (winreg.HKEY_CURRENT_USER, NOTIFICATION_SETTINGS_KEY, "NOC_GLOBAL_SETTING_TOASTS_ENABLED"),
    ):
        try:
            with winreg.OpenKey(root, sub) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
                if not value:
                    return False
        except (FileNotFoundError, OSError):
            continue
    return True


def set_notifications_globally_enabled(enabled: bool) -> dict[str, Any]:
    flag = 1 if enabled else 0
    try:
        for sub, value_name in (
            (PUSH_NOTIFICATIONS_KEY, "ToastEnabled"),
            (NOTIFICATION_SETTINGS_KEY, "NOC_GLOBAL_SETTING_TOASTS_ENABLED"),
        ):
            key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, sub, 0, winreg.KEY_SET_VALUE)
            with key:
                winreg.SetValueEx(key, value_name, 0, winreg.REG_DWORD, flag)
    except OSError as exc:
        return _result(False, f"could not update the registry: {exc}")
    return _result(True, f"Windows notifications turned {'on' if enabled else 'off'}")


def list_toast_apps() -> list[dict[str, Any]]:
    """Every app Windows knows can send notifications, and whether it may."""
    apps: list[dict[str, Any]] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, NOTIFICATION_SETTINGS_KEY) as root:
            index = 0
            while True:
                try:
                    aumid = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                apps.append({"aumid": aumid, "enabled": get_toast_enabled(aumid)})
    except (FileNotFoundError, OSError):
        return apps
    return apps


# -- scheduled tasks -------------------------------------------------------


def set_task_enabled(task_name: str, enabled: bool, config: Config | None = None) -> dict[str, Any]:
    """Enable or disable a scheduled task, remembering its prior state."""
    if not task_name:
        return _result(False, "no task name")

    if config is not None and not enabled:
        definition = task_definition(task_name)
        backups = dict(config.get("task_backups", {}))
        backups[task_name] = {
            "was_enabled": definition.get("enabled") if definition.get("enabled") is not None else True,
            "actions": definition.get("actions", []),
            "changed_at": time.time(),
        }
        config.set("task_backups", backups)

    flag = "/enable" if enabled else "/disable"
    try:
        completed = subprocess.run(
            ["schtasks", "/change", "/tn", task_name, flag],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return _result(False, str(exc))

    if completed.returncode == 0:
        if config is not None and enabled:
            backups = dict(config.get("task_backups", {}))
            backups.pop(task_name, None)
            config.set("task_backups", backups)
        state = "enabled" if enabled else "disabled"
        log.info("Scheduled task %s is now %s", task_name, state)
        return _result(True, f"task {state}", task_name=task_name, enabled=enabled)

    error = (completed.stderr or completed.stdout or "").strip()
    if "denied" in error.lower():
        error = "access denied - Popup Stopper needs to run as administrator"
    return _result(False, error or "schtasks failed", task_name=task_name)


# -- Windows Update restart nags ------------------------------------------


def get_update_restart_notifications() -> bool | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UPDATE_UX_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RESTART_NOTIFICATION_VALUE)
            return bool(value)
    except (FileNotFoundError, OSError):
        return None


def set_update_restart_notifications(enabled: bool) -> dict[str, Any]:
    """The "notify me when a restart is required" switch from Windows Settings."""
    try:
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, UPDATE_UX_KEY, 0, winreg.KEY_SET_VALUE
        )
        with key:
            winreg.SetValueEx(
                key, RESTART_NOTIFICATION_VALUE, 0, winreg.REG_DWORD, 1 if enabled else 0
            )
    except OSError as exc:
        return _result(False, f"could not update the registry: {exc}")
    state = "on" if enabled else "off"
    return _result(True, f"update restart notifications turned {state}", enabled=enabled)


# -- start with Windows ----------------------------------------------------

AUTOSTART_TASK = f"{APP_NAME} Autostart"


def _pythonw_path() -> str:
    """The windowed interpreter, so no console flashes up at logon."""
    executable = Path(sys.executable)
    if executable.name.lower() == "python.exe":
        candidate = executable.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(executable)


def get_autostart() -> bool:
    try:
        completed = subprocess.run(
            ["schtasks", "/query", "/tn", AUTOSTART_TASK],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=CREATE_NO_WINDOW,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return completed.returncode == 0


def set_autostart(enabled: bool) -> dict[str, Any]:
    """Start at logon via a scheduled task.

    A Run-key entry cannot start an elevated process without throwing a UAC
    prompt in the user's face at every logon. A scheduled task registered to
    run with highest privileges starts silently and already elevated.
    """
    if not enabled:
        try:
            completed = subprocess.run(
                ["schtasks", "/delete", "/tn", AUTOSTART_TASK, "/f"],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=CREATE_NO_WINDOW,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            return _result(False, str(exc))
        if completed.returncode == 0:
            return _result(True, "Popup Stopper will no longer start with Windows")
        return _result(False, (completed.stderr or completed.stdout or "").strip())

    entry_point = PROJECT_ROOT / "popupstopper" / "__main__.py"
    command = f'"{_pythonw_path()}" "{entry_point}"'
    try:
        completed = subprocess.run(
            [
                "schtasks", "/create", "/tn", AUTOSTART_TASK, "/tr", command,
                "/sc", "onlogon", "/rl", "highest", "/f",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return _result(False, str(exc))

    if completed.returncode == 0:
        return _result(True, "Popup Stopper will start with Windows, already elevated")
    error = (completed.stderr or completed.stdout or "").strip()
    if "denied" in error.lower():
        error = "access denied - run Popup Stopper as administrator to set this up"
    return _result(False, error or "schtasks failed")


# -- convenience -----------------------------------------------------------


def open_in_explorer(path: str) -> dict[str, Any]:
    """Reveal a file in Explorer, or open the folder if only that exists."""
    if not path:
        return _result(False, "no path given")
    try:
        if os.path.isfile(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif os.path.isdir(path):
            subprocess.Popen(["explorer", os.path.normpath(path)])
        else:
            parent = os.path.dirname(path)
            if not os.path.isdir(parent):
                return _result(False, "that path no longer exists")
            subprocess.Popen(["explorer", os.path.normpath(parent)])
    except OSError as exc:
        return _result(False, str(exc))
    return _result(True, "opened in Explorer")
