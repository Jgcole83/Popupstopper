"""Stopping a popup at its source, rather than closing it after it appears.

Auto-closing a window still lets it exist for a few tens of milliseconds, which
is long enough to steal focus from a fullscreen game. The levers here stop the
thing that produces the popup from running at all:

    scheduled task   the task never runs, so nothing is launched
    toast            Windows suppresses the notification itself
    startup entry    the nagging program no longer starts with Windows
    service          the background service is disabled and stopped
    hard block       Windows refuses to launch that executable at all

Every change records exactly what it replaced, so anything applied here can be
undone from the Prevented tab.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
import winreg
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import actions
from .config import DATA_DIR, PROTECTED_PROCESSES, Config

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000

RISK_SAFE = "safe"
RISK_CAUTION = "caution"
RISK_STRONG = "strong"

# Windows launches these constantly and blocking one would break the desktop,
# sign-in, or the app itself. No amount of confirmation makes it a good idea.
NEVER_BLOCK = frozenset(
    {
        "explorer.exe", "svchost.exe", "services.exe", "lsass.exe", "winlogon.exe",
        "wininit.exe", "csrss.exe", "smss.exe", "dwm.exe", "userinit.exe",
        "consent.exe", "conhost.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
        "rundll32.exe", "regsvr32.exe", "msiexec.exe", "taskhostw.exe",
        "sihost.exe", "ctfmon.exe", "runtimebroker.exe", "dllhost.exe",
        "shellexperiencehost.exe", "startmenuexperiencehost.exe",
        "textinputhost.exe", "searchhost.exe", "searchindexer.exe",
        "applicationframehost.exe", "systemsettings.exe", "spoolsv.exe",
        "audiodg.exe", "fontdrvhost.exe", "wudfhost.exe", "trustedinstaller.exe",
        "tiworker.exe", "python.exe", "pythonw.exe", "regedit.exe", "mmc.exe",
        "schtasks.exe", "sc.exe", "net.exe", "wmiprvse.exe",
    }
    | PROTECTED_PROCESSES
)

# Disabling any of these takes out networking, sign-in, audio or Windows
# servicing. The popup is never worth it.
NEVER_DISABLE_SERVICE = frozenset(
    {
        "rpcss", "dcomlaunch", "rpceptmapper", "lsm", "plugplay", "power",
        "profsvc", "themes", "audiosrv", "audioendpointbuilder", "eventlog",
        "schedule", "winmgmt", "cryptsvc", "dnscache", "dhcp", "nsi",
        "netprofm", "nlasvc", "wlansvc", "lanmanworkstation", "lanmanserver",
        "trustedinstaller", "msiserver", "wuauserv", "bfe", "mpssvc",
        "windefend", "securityhealthservice", "wscsvc", "sens", "shellhwdetection",
        "usosvc", "userdatasvc", "usermanager", "systemeventsbroker",
        "statebroker", "timebrokersvc", "camsvc", "coremessagingregistrar",
    }
)

IFEO_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options"
SERVICES_KEY = r"SYSTEM\CurrentControlSet\Services"
BLOCK_STUB = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "systray.exe")
SHORTCUT_BACKUP_DIR = DATA_DIR / "startup_backups"

RUN_KEY_LOCATIONS = (
    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, "this user"),
    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", 0, "this user"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run",
     winreg.KEY_WOW64_64KEY, "all users"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run",
     winreg.KEY_WOW64_32KEY, "all users (32-bit)"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\RunOnce",
     winreg.KEY_WOW64_64KEY, "all users"),
)


def _startup_folders() -> list[tuple[Path, str]]:
    appdata = os.environ.get("APPDATA", "")
    programdata = os.environ.get("ProgramData", "")
    folders = []
    if appdata:
        folders.append(
            (Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup", "this user")
        )
    if programdata:
        folders.append(
            (Path(programdata) / "Microsoft/Windows/Start Menu/Programs/Startup", "all users")
        )
    return folders


@dataclass
class Lever:
    """One way to stop a given popup source from running."""

    kind: str
    key: str
    label: str
    detail: str
    effect: str
    risk: str = RISK_SAFE
    target: dict[str, Any] = field(default_factory=dict)
    blocked_reason: str = ""

    @property
    def available(self) -> bool:
        return not self.blocked_reason


def _result(ok: bool, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": ok, "message": message, **extra}


def _run(args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, creationflags=CREATE_NO_WINDOW
    )


def _extract_exe(command: str) -> str:
    """Pull the executable out of a registry Run command line."""
    command = (command or "").strip()
    if not command:
        return ""
    if command.startswith('"'):
        end = command.find('"', 1)
        return command[1:end] if end > 0 else command.strip('"')
    # Unquoted paths with spaces are ambiguous; take the longest prefix that
    # ends in .exe, falling back to the first whitespace-delimited token.
    lowered = command.lower()
    marker = lowered.find(".exe")
    if marker != -1:
        return command[: marker + 4]
    return command.split()[0]


def _same_program(candidate: str, exe_path: str, exe_name: str) -> bool:
    candidate = (candidate or "").strip().strip('"')
    if not candidate:
        return False
    candidate = os.path.expandvars(candidate)
    if exe_path and os.path.normcase(os.path.normpath(candidate)) == os.path.normcase(
        os.path.normpath(exe_path)
    ):
        return True
    return bool(exe_name) and os.path.basename(candidate).lower() == exe_name.lower()


# ---------------------------------------------------------------- discovery


def _find_run_entries(exe_path: str, exe_name: str) -> list[Lever]:
    levers: list[Lever] = []
    for hive, subkey, view, scope in RUN_KEY_LOCATIONS:
        try:
            key = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with key:
            index = 0
            while True:
                try:
                    name, value, value_type = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if value_type not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                    continue
                if not _same_program(_extract_exe(str(value)), exe_path, exe_name):
                    continue
                hive_name = "HKCU" if hive == winreg.HKEY_CURRENT_USER else "HKLM"
                levers.append(
                    Lever(
                        kind="startup_run",
                        key=f"startup_run:{hive_name}:{subkey}:{view}:{name}",
                        label=f"Stop it starting with Windows ({scope})",
                        detail=f"{hive_name}\\{subkey}\\{name}",
                        effect="The program will not be launched at sign-in any more.",
                        risk=RISK_SAFE,
                        target={
                            "hive": hive_name,
                            "subkey": subkey,
                            "view": view,
                            "name": name,
                            "value": str(value),
                            "value_type": value_type,
                        },
                    )
                )
    return levers


def _shortcut_target(path: Path) -> str:
    try:
        import pythoncom  # type: ignore[import-not-found]
        from win32com.client import Dispatch  # type: ignore[import-not-found]
    except ImportError:
        return ""
    import gc

    try:
        pythoncom.CoInitialize()
        try:
            shell = Dispatch("WScript.Shell")
            shortcut = shell.CreateShortcut(str(path))
            target = str(shortcut.TargetPath or "")
            # Release the COM objects before uninitialising, otherwise pywin32
            # complains about releasing IUnknown on a dead apartment.
            del shortcut
            del shell
            gc.collect()
            return target
        finally:
            pythoncom.CoUninitialize()
    except Exception:  # noqa: BLE001
        return ""


def _find_startup_shortcuts(exe_path: str, exe_name: str) -> list[Lever]:
    levers: list[Lever] = []
    for folder, scope in _startup_folders():
        if not folder.is_dir():
            continue
        for entry in folder.glob("*.lnk"):
            if not _same_program(_shortcut_target(entry), exe_path, exe_name):
                continue
            levers.append(
                Lever(
                    kind="startup_folder",
                    key=f"startup_folder:{entry}",
                    label=f"Remove its Startup folder shortcut ({scope})",
                    detail=str(entry),
                    effect="The shortcut is moved into Popup Stopper's data folder, "
                    "so the program stops launching at sign-in.",
                    risk=RISK_SAFE,
                    target={"path": str(entry)},
                )
            )
    return levers


def _find_services(exe_path: str, exe_name: str) -> list[Lever]:
    levers: list[Lever] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, SERVICES_KEY)
    except OSError:
        return levers

    with root:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(root, name) as service:
                    image_path, _ = winreg.QueryValueEx(service, "ImagePath")
                    try:
                        start, _ = winreg.QueryValueEx(service, "Start")
                    except FileNotFoundError:
                        continue
                    try:
                        display, _ = winreg.QueryValueEx(service, "DisplayName")
                    except FileNotFoundError:
                        display = name
                    try:
                        service_type, _ = winreg.QueryValueEx(service, "Type")
                    except FileNotFoundError:
                        service_type = 0
            except OSError:
                continue

            # Types 1 and 2 are kernel/file-system drivers, not popup sources.
            if int(service_type) in (1, 2):
                continue
            if int(start) == 4:
                continue  # already disabled
            if not _same_program(_extract_exe(str(image_path)), exe_path, exe_name):
                continue

            lever = Lever(
                kind="service",
                key=f"service:{name}",
                label=f"Disable the background service \"{display}\"",
                detail=f"{name} - {image_path}",
                effect="The service is stopped and set to Disabled. Whatever feature "
                "it provides stops working until you undo this.",
                risk=RISK_STRONG,
                target={"name": name, "display": str(display), "start": int(start)},
            )
            if name.lower() in NEVER_DISABLE_SERVICE:
                lever.blocked_reason = "this service is essential to Windows"
            levers.append(lever)
    return levers


def _hard_block_lever(exe_path: str, exe_name: str) -> Lever:
    lever = Lever(
        kind="hard_block",
        key=f"hard_block:{exe_name.lower()}",
        label=f"Stop {exe_name} from launching at all",
        detail=exe_path or exe_name,
        effect="Windows will refuse to start this program, whatever tries to launch "
        "it. Use this for updaters that keep coming back.",
        risk=RISK_STRONG,
        target={"exe_name": exe_name, "exe_path": exe_path},
    )
    if exe_name.lower() in NEVER_BLOCK:
        lever.blocked_reason = "Windows needs this program to work properly"
    return lever


def find_levers(record: dict[str, Any], config: Config | None = None) -> list[Lever]:
    """Every way this particular popup could be stopped at its source."""
    exe_path = (record.get("exe_path") or "").strip()
    exe_name = (record.get("exe_name") or os.path.basename(exe_path)).strip()
    levers: list[Lever] = []

    if record.get("task_name"):
        levers.append(
            Lever(
                kind="task",
                key=f"task:{record['task_name']}",
                label="Disable the scheduled task behind it",
                detail=str(record["task_name"]),
                effect="The task never runs, so the popup is never created. This is the "
                "cleanest fix when a task is the cause.",
                risk=RISK_SAFE,
                target={"task_name": record["task_name"]},
            )
        )

    if record.get("aumid"):
        levers.append(
            Lever(
                kind="toast",
                key=f"toast:{record['aumid']}",
                label="Mute this app's notifications",
                detail=str(record["aumid"]),
                effect="Windows suppresses the toast itself, so it is never shown.",
                risk=RISK_SAFE,
                target={"aumid": record["aumid"]},
            )
        )

    if exe_name:
        levers.extend(_find_run_entries(exe_path, exe_name))
        levers.extend(_find_startup_shortcuts(exe_path, exe_name))
        levers.extend(_find_services(exe_path, exe_name))
        levers.append(_hard_block_lever(exe_path, exe_name))

    applied = set(list_prevented(config).keys()) if config else set()
    return [lever for lever in levers if lever.key not in applied]


# ------------------------------------------------------------------- apply


def apply_lever(lever: Lever, config: Config) -> dict[str, Any]:
    if not lever.available:
        return _result(False, f"refused: {lever.blocked_reason}")

    handler = {
        "task": _apply_task,
        "toast": _apply_toast,
        "startup_run": _apply_run_entry,
        "startup_folder": _apply_shortcut,
        "service": _apply_service,
        "hard_block": _apply_hard_block,
    }.get(lever.kind)
    if handler is None:
        return _result(False, f"unknown prevention type {lever.kind}")

    outcome = handler(lever, config)
    if outcome["ok"]:
        _record_prevention(config, lever, outcome.get("undo", {}))
    return outcome


def _apply_task(lever: Lever, config: Config) -> dict[str, Any]:
    result = actions.set_task_enabled(lever.target["task_name"], False, config)
    return _result(result.get("ok", False), str(result.get("message")), undo={})


def _apply_toast(lever: Lever, _config: Config) -> dict[str, Any]:
    result = actions.set_toast_enabled(lever.target["aumid"], False)
    return _result(result.get("ok", False), str(result.get("message")), undo={})


def _open_run_key(target: dict[str, Any], access: int):
    hive = (
        winreg.HKEY_CURRENT_USER
        if target["hive"] == "HKCU"
        else winreg.HKEY_LOCAL_MACHINE
    )
    return winreg.OpenKey(hive, target["subkey"], 0, access | int(target.get("view", 0)))


def _apply_run_entry(lever: Lever, _config: Config) -> dict[str, Any]:
    try:
        with _open_run_key(lever.target, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, lever.target["name"])
    except PermissionError:
        return _result(False, "access denied - run Popup Stopper as administrator")
    except OSError as exc:
        return _result(False, f"could not remove the startup entry: {exc}")
    return _result(
        True,
        f"removed the startup entry \"{lever.target['name']}\"",
        undo={
            "name": lever.target["name"],
            "value": lever.target["value"],
            "value_type": lever.target["value_type"],
        },
    )


def _apply_shortcut(lever: Lever, _config: Config) -> dict[str, Any]:
    source = Path(lever.target["path"])
    if not source.exists():
        return _result(False, "the shortcut is already gone")
    SHORTCUT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    destination = SHORTCUT_BACKUP_DIR / f"{int(time.time())}__{source.name}"
    try:
        shutil.move(str(source), str(destination))
    except OSError as exc:
        return _result(False, f"could not move the shortcut: {exc}")
    return _result(
        True,
        f"moved \"{source.name}\" out of the Startup folder",
        undo={"original": str(source), "backup": str(destination)},
    )


def _apply_service(lever: Lever, _config: Config) -> dict[str, Any]:
    name = lever.target["name"]
    configured = _run(["sc", "config", name, "start=", "disabled"])
    if configured.returncode != 0:
        message = (configured.stdout or configured.stderr or "").strip()
        if "access is denied" in message.lower():
            message = "access denied - run Popup Stopper as administrator"
        return _result(False, message or "sc config failed")
    _run(["sc", "stop", name])  # best effort; a stopped service is fine either way
    return _result(
        True,
        f"disabled and stopped the service \"{lever.target['display']}\"",
        undo={"name": name, "start": lever.target["start"]},
    )


_START_TYPE_FLAG = {2: "auto", 3: "demand", 4: "disabled"}


def _apply_hard_block(lever: Lever, _config: Config) -> dict[str, Any]:
    exe_name = lever.target["exe_name"]
    if not os.path.exists(BLOCK_STUB):
        return _result(False, "the no-op stub Windows needs for this is missing")
    try:
        key = winreg.CreateKeyEx(
            winreg.HKEY_LOCAL_MACHINE, f"{IFEO_KEY}\\{exe_name}", 0, winreg.KEY_ALL_ACCESS
        )
        with key:
            try:
                previous, _ = winreg.QueryValueEx(key, "Debugger")
            except FileNotFoundError:
                previous = None
            winreg.SetValueEx(key, "Debugger", 0, winreg.REG_SZ, BLOCK_STUB)
    except PermissionError:
        return _result(False, "access denied - run Popup Stopper as administrator")
    except OSError as exc:
        return _result(False, f"could not block the program: {exc}")
    return _result(
        True,
        f"Windows will now refuse to launch {exe_name}",
        undo={"exe_name": exe_name, "previous": previous},
    )


# -------------------------------------------------------------------- undo


def undo_prevention(key: str, config: Config) -> dict[str, Any]:
    entries = list_prevented(config)
    entry = entries.get(key)
    if not entry:
        return _result(False, "that change is not recorded")

    kind = entry.get("kind")
    undo = entry.get("undo", {})
    target = entry.get("target", {})

    if kind == "task":
        result = actions.set_task_enabled(target["task_name"], True, config)
        outcome = _result(result.get("ok", False), str(result.get("message")))
    elif kind == "toast":
        result = actions.set_toast_enabled(target["aumid"], True)
        outcome = _result(result.get("ok", False), str(result.get("message")))
    elif kind == "startup_run":
        outcome = _undo_run_entry(target, undo)
    elif kind == "startup_folder":
        outcome = _undo_shortcut(undo)
    elif kind == "service":
        outcome = _undo_service(undo)
    elif kind == "hard_block":
        outcome = _undo_hard_block(undo)
    else:
        outcome = _result(False, f"unknown prevention type {kind}")

    if outcome["ok"]:
        _forget_prevention(config, key)
    return outcome


def _undo_run_entry(target: dict[str, Any], undo: dict[str, Any]) -> dict[str, Any]:
    try:
        with _open_run_key(target, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(
                key,
                undo["name"],
                0,
                int(undo.get("value_type", winreg.REG_SZ)),
                undo["value"],
            )
    except PermissionError:
        return _result(False, "access denied - run Popup Stopper as administrator")
    except OSError as exc:
        return _result(False, f"could not restore the startup entry: {exc}")
    return _result(True, f"restored the startup entry \"{undo['name']}\"")


def _undo_shortcut(undo: dict[str, Any]) -> dict[str, Any]:
    backup = Path(undo.get("backup", ""))
    original = Path(undo.get("original", ""))
    if not backup.exists():
        return _result(False, "the saved shortcut is missing")
    try:
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(backup), str(original))
    except OSError as exc:
        return _result(False, f"could not restore the shortcut: {exc}")
    return _result(True, f"put \"{original.name}\" back in the Startup folder")


def _undo_service(undo: dict[str, Any]) -> dict[str, Any]:
    name = undo.get("name", "")
    flag = _START_TYPE_FLAG.get(int(undo.get("start", 3)), "demand")
    completed = _run(["sc", "config", name, "start=", flag])
    if completed.returncode != 0:
        message = (completed.stdout or completed.stderr or "").strip()
        if "access is denied" in message.lower():
            message = "access denied - run Popup Stopper as administrator"
        return _result(False, message or "sc config failed")
    return _result(True, f"restored the service \"{name}\" to {flag} start")


def _undo_hard_block(undo: dict[str, Any]) -> dict[str, Any]:
    exe_name = undo.get("exe_name", "")
    previous = undo.get("previous")
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, f"{IFEO_KEY}\\{exe_name}", 0, winreg.KEY_ALL_ACCESS
        ) as key:
            if previous:
                winreg.SetValueEx(key, "Debugger", 0, winreg.REG_SZ, previous)
            else:
                try:
                    winreg.DeleteValue(key, "Debugger")
                except FileNotFoundError:
                    pass
        if not previous:
            # Remove the key too, but only if we left it empty.
            try:
                winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, f"{IFEO_KEY}\\{exe_name}")
            except OSError:
                pass
    except FileNotFoundError:
        return _result(True, f"{exe_name} was already unblocked")
    except PermissionError:
        return _result(False, "access denied - run Popup Stopper as administrator")
    except OSError as exc:
        return _result(False, f"could not unblock the program: {exc}")
    return _result(True, f"{exe_name} can launch again")


# --------------------------------------------------------------- bookkeeping


def list_prevented(config: Config | None) -> dict[str, dict[str, Any]]:
    if config is None:
        return {}
    return dict(config.get("prevention", {}) or {})


def _record_prevention(config: Config, lever: Lever, undo: dict[str, Any]) -> None:
    entries = list_prevented(config)
    entries[lever.key] = {
        "kind": lever.kind,
        "label": lever.label,
        "detail": lever.detail,
        "risk": lever.risk,
        "target": lever.target,
        "undo": undo,
        "applied_at": time.time(),
    }
    config.set("prevention", entries)


def _forget_prevention(config: Config, key: str) -> None:
    entries = list_prevented(config)
    entries.pop(key, None)
    config.set("prevention", entries)
