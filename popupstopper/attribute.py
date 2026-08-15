"""Work out which program is really responsible for a popup.

The window that appears is often not the interesting answer. A Windows Update
restart nag shows up as a generic host process; what the user actually wants
to know is the executable on disk and the task or service that launched it.
This module resolves the process, walks its ancestry, reads the file's version
metadata and signature, and puts it into a human-meaningful category.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import threading
from ctypes import wintypes
from typing import Any

import psutil

log = logging.getLogger(__name__)

version_dll = ctypes.WinDLL("version", use_last_error=True)
version_dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD
version_dll.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
version_dll.GetFileVersionInfoW.restype = wintypes.BOOL
version_dll.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
version_dll.VerQueryValueW.restype = wintypes.BOOL
version_dll.VerQueryValueW.argtypes = [
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wintypes.UINT),
]

CREATE_NO_WINDOW = 0x08000000

# Category detection, first match wins. Keyed on the executable name.
_CATEGORY_BY_EXE: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "Windows Update",
        frozenset(
            {
                "musnotification.exe",
                "musnotificationux.exe",
                "usoclient.exe",
                "mousocoreworker.exe",
                "wuauclt.exe",
                "tiworker.exe",
                "windowsupdatebox.exe",
                "setuphost.exe",
                "uso_ui.exe",
            }
        ),
    ),
    (
        "Driver / GPU",
        frozenset(
            {
                "radeonsoftware.exe",
                "amdow.exe",
                "amdowd.exe",
                "atieclxx.exe",
                "amdinstallmanagerapp.exe",
                "nvcontainer.exe",
                "nvidia share.exe",
                "nvidia web helper.exe",
                "nvbackend.exe",
                "geforce experience.exe",
                "igfxem.exe",
                "igfxtray.exe",
                "intelcphdcpsvc.exe",
            }
        ),
    ),
    (
        "Installer",
        frozenset(
            {
                "msiexec.exe",
                "setup.exe",
                "install.exe",
                "installer.exe",
                "update.exe",
                "vc_redist.x64.exe",
                "dismhost.exe",
            }
        ),
    ),
    (
        "Game launcher",
        frozenset(
            {
                "battle.net.exe",
                "agent.exe",
                "steam.exe",
                "steamwebhelper.exe",
                "epicgameslauncher.exe",
                "riotclientux.exe",
                "riotclientservices.exe",
                "eadesktop.exe",
                "origin.exe",
                "upc.exe",
                "ubisoftconnect.exe",
                "galaxyclient.exe",
                "rockstarservice.exe",
                "launcher.exe",
            }
        ),
    ),
    (
        "Security",
        frozenset(
            {
                "msmpeng.exe",
                "securityhealthsystray.exe",
                "securityhealthservice.exe",
                "securityhealthhost.exe",
                "smartscreen.exe",
                "mpcmdrun.exe",
            }
        ),
    ),
    (
        "Windows shell",
        frozenset(
            {
                "shellexperiencehost.exe",
                "startmenuexperiencehost.exe",
                "systemsettings.exe",
                "wwahost.exe",
                "explorer.exe",
                "backgroundtaskhost.exe",
                "runtimebroker.exe",
                "applicationframehost.exe",
                "openwith.exe",
                "pickerhost.exe",
            }
        ),
    ),
    (
        "Browser",
        frozenset({"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe"}),
    ),
    (
        "Chat / social",
        frozenset({"discord.exe", "slack.exe", "teams.exe", "ms-teams.exe", "skype.exe"}),
    ),
    (
        "Cloud storage",
        frozenset({"onedrive.exe", "dropbox.exe", "googledrivefs.exe", "filecoauth.exe"}),
    ),
    (
        "Scripting host",
        frozenset(
            {
                "powershell.exe",
                "pwsh.exe",
                "cmd.exe",
                "wscript.exe",
                "cscript.exe",
                "mshta.exe",
                "conhost.exe",
                "python.exe",
            }
        ),
    ),
)

_TITLE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Windows Update", ("restart", "update is ready", "updates are ready", "finish updates", "reboot")),
    ("Windows shell", ("finish setting up", "let's finish", "get even more out of windows", "welcome to")),
    ("Installer", ("install", "setup", "uninstall")),
)


# -- file metadata ---------------------------------------------------------

_version_cache: dict[str, dict[str, str]] = {}
_version_lock = threading.Lock()


def file_version_info(path: str) -> dict[str, str]:
    """Read FileDescription / CompanyName / ProductName from a PE's resources.

    Much faster than shelling out, and it is what Task Manager shows as the
    friendly name of a process.
    """
    if not path:
        return {}
    key = path.lower()
    with _version_lock:
        cached = _version_cache.get(key)
    if cached is not None:
        return cached

    info: dict[str, str] = {}
    try:
        size = version_dll.GetFileVersionInfoSizeW(path, None)
        if size:
            buf = ctypes.create_string_buffer(size)
            if version_dll.GetFileVersionInfoW(path, 0, size, buf):
                lang_ptr = ctypes.c_void_p()
                lang_len = wintypes.UINT()
                if version_dll.VerQueryValueW(
                    buf, "\\VarFileInfo\\Translation", ctypes.byref(lang_ptr), ctypes.byref(lang_len)
                ) and lang_len.value >= 4:
                    codes = ctypes.cast(lang_ptr, ctypes.POINTER(wintypes.WORD))
                    lang, codepage = codes[0], codes[1]
                    for field in ("FileDescription", "CompanyName", "ProductName", "FileVersion"):
                        value_ptr = ctypes.c_void_p()
                        value_len = wintypes.UINT()
                        sub_block = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{field}"
                        if version_dll.VerQueryValueW(
                            buf, sub_block, ctypes.byref(value_ptr), ctypes.byref(value_len)
                        ) and value_len.value:
                            text = ctypes.wstring_at(value_ptr, value_len.value).strip("\x00").strip()
                            if text:
                                info[field] = text
    except OSError:
        pass

    with _version_lock:
        _version_cache[key] = info
    return info


_signature_cache: dict[str, str] = {}
_signature_lock = threading.Lock()


def signature_publisher(path: str) -> str:
    """Authenticode signer for a file, or "" when unsigned/unknown.

    This shells out to PowerShell so it is comparatively slow; results are
    cached per path and it is only ever called from the enrichment worker.
    """
    if not path or not os.path.exists(path):
        return ""
    key = path.lower()
    with _signature_lock:
        if key in _signature_cache:
            return _signature_cache[key]

    publisher = ""
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$s = Get-AuthenticodeSignature -LiteralPath $env:PS_TARGET;"
                " if ($s.Status -eq 'Valid' -and $s.SignerCertificate) {"
                " $s.SignerCertificate.Subject } else { '' }",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=CREATE_NO_WINDOW,
            env={**os.environ, "PS_TARGET": path},
        )
        subject = completed.stdout.strip()
        if subject:
            # Subject looks like "CN=Microsoft Corporation, O=..., L=..."
            for part in subject.split(","):
                part = part.strip()
                if part.upper().startswith("CN="):
                    publisher = part[3:].strip().strip('"')
                    break
            else:
                publisher = subject
    except (subprocess.SubprocessError, OSError):
        publisher = ""

    with _signature_lock:
        _signature_cache[key] = publisher
    return publisher


# -- process ancestry ------------------------------------------------------


def process_chain(pid: int, max_depth: int = 8) -> list[dict[str, Any]]:
    """The process and its ancestors, nearest first."""
    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    try:
        proc: psutil.Process | None = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return chain

    depth = 0
    while proc is not None and depth < max_depth and proc.pid not in seen:
        seen.add(proc.pid)
        entry: dict[str, Any] = {"pid": proc.pid, "name": "", "exe": "", "cmdline": ""}
        try:
            entry["name"] = proc.name()
        except (psutil.Error, OSError):
            pass
        try:
            entry["exe"] = proc.exe() or ""
        except (psutil.Error, OSError):
            pass
        try:
            entry["cmdline"] = " ".join(proc.cmdline())
        except (psutil.Error, OSError):
            pass
        try:
            entry["started"] = proc.create_time()
        except (psutil.Error, OSError):
            entry["started"] = None
        chain.append(entry)

        try:
            proc = proc.parent()
        except (psutil.Error, OSError):
            proc = None
        depth += 1
    return chain


def process_cmdline(pid: int) -> str:
    try:
        return " ".join(psutil.Process(pid).cmdline())
    except (psutil.Error, OSError, ValueError):
        return ""


# -- naming and classification --------------------------------------------


def friendly_name(exe_path: str, fallback: str = "") -> str:
    info = file_version_info(exe_path)
    name = info.get("FileDescription") or info.get("ProductName")
    if name:
        return name
    if exe_path:
        return os.path.basename(exe_path)
    return fallback or "Unknown"


def company_name(exe_path: str) -> str:
    return file_version_info(exe_path).get("CompanyName", "")


def categorize(
    exe_name: str,
    exe_path: str = "",
    title: str = "",
    task_name: str = "",
    chain: list[dict[str, Any]] | None = None,
) -> str:
    exe_name = (exe_name or "").lower()
    lowered_title = (title or "").lower()
    lowered_path = (exe_path or "").lower()

    if task_name:
        task_lower = task_name.lower()
        if "updateorchestrator" in task_lower or "windowsupdate" in task_lower:
            return "Windows Update"

    for category, names in _CATEGORY_BY_EXE:
        if exe_name in names:
            # A script host is only interesting as the thing it is running.
            if category == "Scripting host" and chain:
                return "Scheduled task / script"
            return category

    if "\\windows\\" in lowered_path and "system32" in lowered_path:
        for category, hints in _TITLE_HINTS:
            if any(hint in lowered_title for hint in hints):
                return category
        return "Windows system"

    for category, hints in _TITLE_HINTS:
        if any(hint in lowered_title for hint in hints):
            return category

    if chain:
        for parent in chain[1:]:
            parent_name = (parent.get("name") or "").lower()
            if parent_name in ("taskeng.exe", "svchost.exe") and task_name:
                return "Scheduled task / script"

    return "Application"


def source_key_for_window(exe_path: str, window_class: str, exe_name: str) -> str:
    """A stable identity used to group popups and attach rules."""
    if exe_path:
        return f"exe:{exe_path.lower()}"
    if exe_name:
        return f"exe:{exe_name.lower()}"
    return f"class:{window_class.lower()}"


def source_key_for_toast(aumid: str) -> str:
    return f"toast:{aumid.lower()}"


def enrich_window(event: Any) -> dict[str, Any]:
    """Turn a raw WindowEvent into the full record we store and display."""
    chain = process_chain(event.pid)
    cmdline = chain[0].get("cmdline", "") if chain else process_cmdline(event.pid)
    display = friendly_name(event.exe_path, fallback=event.exe_name or event.window_class)
    publisher = signature_publisher(event.exe_path) or company_name(event.exe_path)
    category = categorize(event.exe_name, event.exe_path, event.title, chain=chain)

    return {
        "ts": event.ts,
        "kind": "window",
        "source_key": source_key_for_window(event.exe_path, event.window_class, event.exe_name),
        "display_name": display,
        "title": event.title,
        "body": event.body,
        "exe_path": event.exe_path,
        "exe_name": event.exe_name,
        "pid": event.pid,
        "cmdline": cmdline,
        "publisher": publisher,
        "parents": chain,
        "aumid": None,
        "window_class": event.window_class,
        "category": category,
        "task_name": None,
        "task_exe": None,
        "action": "logged",
        "details": {
            "event": event.event_name,
            "popup_like": event.is_popup_like,
            "topmost": event.is_topmost,
            "fullscreen": event.is_fullscreen_ish,
            "rect": list(event.rect),
            "hwnd": event.hwnd,
        },
    }
