"""Detect toast notifications by watching the Windows notification database.

Toasts are not ordinary windows: they are composed by the shell, so a window
hook cannot see who sent them. Windows does record every one in
%LOCALAPPDATA%\\Microsoft\\Windows\\Notifications\\wpndatabase.db together with
the sending application's AppUserModelID and the full toast payload, which is
exactly the attribution we need.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import winreg
import xml.etree.ElementTree as ET
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000

shlwapi = ctypes.WinDLL("shlwapi", use_last_error=True)
shlwapi.SHLoadIndirectString.restype = ctypes.c_long
shlwapi.SHLoadIndirectString.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    wintypes.UINT,
    ctypes.c_void_p,
]

# AUMIDs whose derived name would be unhelpful, and the category they imply.
KNOWN_AUMIDS: dict[str, tuple[str, str]] = {
    "microsoft.skydrive.desktop": ("OneDrive", "Cloud storage"),
    "windows.defender.securitycenter": ("Windows Security", "Security"),
    "windows.systemtoast.securityandmaintenance": ("Security and Maintenance", "Security"),
    "windows.systemtoast.windowsupdate.notification": ("Windows Update", "Windows Update"),
    "windows.systemtoast.wupdate": ("Windows Update", "Windows Update"),
    "windows.systemtoast.suggested": ("Windows Suggestions", "Windows nag"),
    "windows.systemtoast.startupapp": ("Startup Apps", "Windows shell"),
    "windows.systemtoast.explorer": ("File Explorer", "Windows shell"),
    "windows.systemtoast.backgroundaccess": ("Battery / Background Apps", "Windows shell"),
    "windows.systemtoast.autoplay": ("AutoPlay", "Windows shell"),
    "windows.systemtoast.bthquickpair": ("Bluetooth", "Windows shell"),
    "windows.systemtoast.devicemanagement": ("Device Management", "Windows shell"),
    "windows.systemtoast.devicesetup": ("Device Setup", "Windows shell"),
    "windows.systemtoast.printer": ("Printers", "Windows shell"),
    "windows.systemtoast.o4c": ("Windows Sign-in", "Windows nag"),
    "microsoft.windows.defender": ("Windows Security", "Security"),
    "microsoft.windows.explorer": ("File Explorer", "Windows shell"),
    "microsoft.windows.shell.runtaskdialog": ("Windows Shell Dialog", "Windows shell"),
}

# Substring of the AUMID -> category, checked when there is no exact match.
_TOAST_CATEGORY_HINTS: tuple[tuple[str, str], ...] = (
    ("windowsupdate", "Windows Update"),
    ("update", "Update"),
    ("defender", "Security"),
    ("security", "Security"),
    ("amd", "Driver / GPU"),
    ("radeon", "Driver / GPU"),
    ("nvidia", "Driver / GPU"),
    ("geforce", "Driver / GPU"),
    ("battlenet", "Game launcher"),
    ("blizzard", "Game launcher"),
    ("steam", "Game launcher"),
    ("epicgames", "Game launcher"),
    ("riotgames", "Game launcher"),
    ("discord", "Chat / social"),
    ("slack", "Chat / social"),
    ("teams", "Chat / social"),
    ("skydrive", "Cloud storage"),
    ("onedrive", "Cloud storage"),
    ("dropbox", "Cloud storage"),
    ("chrome", "Browser"),
    ("edge", "Browser"),
    ("firefox", "Browser"),
    ("systemtoast", "Windows shell"),
)

# Trailing AUMID tokens that carry no product meaning.
_GENERIC_TOKENS = frozenset(
    {
        "app", "application", "desktop", "beta", "release", "client", "exe",
        "main", "default", "notifications", "notification", "toast", "shell",
        "ui", "host", "launcher", "service",
    }
)


def resolve_indirect_string(value: str) -> str:
    """Expand a resource reference like "@C:\\path\\x.dll,-12001" to real text."""
    if not value.startswith("@"):
        return value
    buf = ctypes.create_unicode_buffer(1024)
    try:
        if shlwapi.SHLoadIndirectString(value, buf, 1024, None) == 0 and buf.value:
            return buf.value
    except OSError:
        pass
    return value

# ArrivalTime is a Windows FILETIME: 100ns units since 1601-01-01.
_TICKS_AT_EPOCH = 116_444_736_000_000_000
_TICKS_PER_SECOND = 10_000_000


def db_path() -> Path:
    return (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Microsoft"
        / "Windows"
        / "Notifications"
        / "wpndatabase.db"
    )


def ticks_to_epoch(ticks: int) -> float:
    return (ticks - _TICKS_AT_EPOCH) / _TICKS_PER_SECOND


def epoch_to_ticks(epoch: float) -> int:
    return int(epoch * _TICKS_PER_SECOND) + _TICKS_AT_EPOCH


@dataclass
class ToastEvent:
    aumid: str
    title: str
    body: str
    ts: float
    notification_id: int
    app_name: str = ""
    app_path: str = ""
    category: str = "Notification"
    texts: list[str] = field(default_factory=list)


# -- payload parsing -------------------------------------------------------


def _decode_payload(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            for encoding in ("utf-16", "utf-16-le"):
                try:
                    return raw.decode(encoding)
                except UnicodeDecodeError:
                    continue
        for encoding in ("utf-8", "utf-16-le", "latin-1"):
            try:
                text = raw.decode(encoding)
            except UnicodeDecodeError:
                continue
            if "<" in text:
                return text
        return raw.decode("utf-8", errors="replace")
    return str(payload)


def parse_payload(payload: Any) -> list[str]:
    """Pull the visible lines of text out of a toast's XML payload."""
    text = _decode_payload(payload).strip()
    if not text:
        return []
    start = text.find("<")
    if start > 0:
        text = text[start:]
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    lines: list[str] = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1].lower()
        if tag == "text" and node.text:
            value = " ".join(node.text.split())
            if value and value not in lines:
                lines.append(value)
    return lines


# -- application identity resolution --------------------------------------


class AppResolver:
    """Maps an AppUserModelID to a display name and an install location.

    Cheap registry lookups happen inline; the expensive PowerShell inventory
    of Start menu entries and packaged apps is built once in the background.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, str]] = {}
        self._start_apps: dict[str, str] = {}
        self._packages: dict[str, dict[str, str]] = {}
        self._inventory_loaded = threading.Event()

    def load_inventory_async(self) -> None:
        threading.Thread(target=self._load_inventory, name="app-inventory", daemon=True).start()

    def _run_powershell(self, command: str, timeout: int = 60) -> Any:
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
            )
            output = completed.stdout.strip()
            if not output:
                return None
            return json.loads(output)
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
            return None

    def _load_inventory(self) -> None:
        start_apps = self._run_powershell(
            "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress -Depth 3"
        )
        if isinstance(start_apps, dict):
            start_apps = [start_apps]
        if isinstance(start_apps, list):
            mapping = {}
            for item in start_apps:
                if isinstance(item, dict) and item.get("AppID"):
                    mapping[str(item["AppID"]).lower()] = str(item.get("Name") or "")
            with self._lock:
                self._start_apps = mapping

        packages = self._run_powershell(
            "Get-AppxPackage | Select-Object Name,PackageFamilyName,InstallLocation"
            " | ConvertTo-Json -Compress -Depth 3"
        )
        if isinstance(packages, dict):
            packages = [packages]
        if isinstance(packages, list):
            mapping = {}
            for item in packages:
                if isinstance(item, dict) and item.get("PackageFamilyName"):
                    mapping[str(item["PackageFamilyName"]).lower()] = {
                        "name": str(item.get("Name") or ""),
                        "path": str(item.get("InstallLocation") or ""),
                    }
            with self._lock:
                self._packages = mapping

        self._inventory_loaded.set()
        log.info(
            "App inventory loaded (%d start apps, %d packages)",
            len(self._start_apps),
            len(self._packages),
        )

    def _registry_lookup(self, aumid: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for root, sub in (
            (winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{aumid}"),
            (winreg.HKEY_CLASSES_ROOT, rf"AppUserModelId\{aumid}"),
        ):
            try:
                with winreg.OpenKey(root, sub) as key:
                    for value_name, target in (
                        ("DisplayName", "name"),
                        ("IconUri", "icon"),
                        ("CustomActivator", "activator"),
                    ):
                        try:
                            value, _ = winreg.QueryValueEx(key, value_name)
                            if value:
                                result.setdefault(target, str(value))
                        except FileNotFoundError:
                            continue
                if result:
                    break
            except (FileNotFoundError, OSError):
                continue
        return result

    @staticmethod
    def _derive_name(aumid: str) -> str:
        """Best-effort readable product name from the AUMID itself."""
        base = aumid.split("!", 1)[0] if "!" in aumid else aumid
        # Packaged AUMIDs end in "_<publisherhash>", which is noise.
        if "_" in base:
            base = base.rsplit("_", 1)[0]
        tokens = [token for token in base.split(".") if token]
        while len(tokens) > 1 and tokens[-1].lower() in _GENERIC_TOKENS:
            tokens.pop()
        return tokens[-1] if tokens else aumid

    def resolve(self, aumid: str) -> dict[str, str]:
        if not aumid:
            return {"name": "Unknown", "path": "", "aumid": "", "category": "Notification"}
        key = aumid.lower()
        with self._lock:
            cached = self._cache.get(key)
        if cached:
            return cached

        name = ""
        path = ""
        category = ""

        known = KNOWN_AUMIDS.get(key)
        if known:
            name, category = known

        with self._lock:
            start_name = self._start_apps.get(key, "")
            packages = dict(self._packages)
        name = name or start_name

        if "!" in aumid:
            family = aumid.split("!", 1)[0].lower()
            package = packages.get(family)
            if package is None:
                # A reinstall changes the publisher hash, so match the family
                # name that precedes it as a fallback.
                prefix = family.rsplit("_", 1)[0]
                for candidate_family, candidate in packages.items():
                    if candidate_family.rsplit("_", 1)[0] == prefix:
                        package = candidate
                        break
            if package:
                path = package.get("path", "")
                name = name or package.get("name", "")

        if not name:
            registry = self._registry_lookup(aumid)
            name = resolve_indirect_string(registry.get("name", ""))

        if not name:
            name = self._derive_name(aumid)

        # Package identifiers such as "MicrosoftWindows.Client.WebExperience"
        # read badly; the app id after "!" is usually the human-facing label.
        if "!" in aumid and " " not in name and ("." in name or "-" in name):
            app_part = aumid.split("!", 1)[1]
            if app_part and app_part.lower() not in _GENERIC_TOKENS:
                name = app_part

        if not category:
            category = "Notification"
            for hint, hint_category in _TOAST_CATEGORY_HINTS:
                if hint in key:
                    category = hint_category
                    break

        resolved = {"name": name, "path": path, "aumid": aumid, "category": category}
        with self._lock:
            self._cache[key] = resolved
        return resolved


# -- database polling ------------------------------------------------------

_QUERY = """
SELECT n.Id, h.PrimaryId, n.Type, n.ArrivalTime, n.Payload
FROM Notification n
JOIN NotificationHandler h ON n.HandlerId = h.RecordId
WHERE n.ArrivalTime > ?
ORDER BY n.ArrivalTime ASC
LIMIT 200
"""

_LATEST_TICK = "SELECT COALESCE(MAX(ArrivalTime), 0) FROM Notification"


class ToastWatcher:
    """Polls the notification database and reports newly arrived toasts."""

    def __init__(
        self,
        on_toast: Callable[[ToastEvent], None],
        resolver: AppResolver | None = None,
        poll_seconds: float = 3.0,
        last_tick: int = 0,
        on_tick_advance: Callable[[int], None] | None = None,
    ) -> None:
        self._on_toast = on_toast
        self.resolver = resolver or AppResolver()
        self._poll_seconds = poll_seconds
        self._last_tick = last_tick
        self._on_tick_advance = on_tick_advance
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._path = db_path()
        self.available = self._path.exists()
        self.last_error = ""

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if not self.available:
            log.warning("Notification database not found at %s", self._path)
            return
        if self._thread and self._thread.is_alive():
            return
        self.resolver.load_inventory_async()
        if self._last_tick <= 0:
            # Start from "now" so the first run does not replay old history.
            self._last_tick = self._current_max_tick() or epoch_to_ticks(time.time())
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="toast-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- polling -----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Read-only connection, falling back to a snapshot copy if locked."""
        uri = "file:" + str(self._path).replace("\\", "/") + "?mode=ro"
        try:
            return sqlite3.connect(uri, uri=True, timeout=2)
        except sqlite3.Error:
            temp_dir = Path(tempfile.gettempdir()) / "popupstopper-wpn"
            temp_dir.mkdir(parents=True, exist_ok=True)
            copy = temp_dir / "wpndatabase.db"
            for suffix in ("", "-wal", "-shm"):
                source = Path(str(self._path) + suffix)
                if source.exists():
                    shutil.copy2(source, Path(str(copy) + suffix))
            return sqlite3.connect(str(copy), timeout=2)

    def _current_max_tick(self) -> int:
        try:
            conn = self._connect()
            try:
                return int(conn.execute(_LATEST_TICK).fetchone()[0] or 0)
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            self.last_error = str(exc)
            return 0

    def poll_once(self) -> list[ToastEvent]:
        events: list[ToastEvent] = []
        try:
            conn = self._connect()
        except (sqlite3.Error, OSError) as exc:
            self.last_error = str(exc)
            return events

        try:
            rows = conn.execute(_QUERY, (self._last_tick,)).fetchall()
        except sqlite3.Error as exc:
            self.last_error = str(exc)
            return events
        finally:
            conn.close()

        for notification_id, aumid, kind, arrival, payload in rows:
            self._last_tick = max(self._last_tick, int(arrival or 0))
            # Tiles and badges update live tiles silently; only toasts pop up.
            if (kind or "").lower() != "toast":
                continue
            texts = parse_payload(payload)
            info = self.resolver.resolve(aumid or "")
            events.append(
                ToastEvent(
                    aumid=aumid or "",
                    title=texts[0] if texts else "",
                    body="\n".join(texts[1:]) if len(texts) > 1 else "",
                    ts=ticks_to_epoch(int(arrival or 0)),
                    notification_id=int(notification_id or 0),
                    app_name=info.get("name", ""),
                    app_path=info.get("path", ""),
                    category=info.get("category", "Notification"),
                    texts=texts,
                )
            )

        if rows and self._on_tick_advance:
            self._on_tick_advance(self._last_tick)
        return events

    def _run(self) -> None:
        log.info("Toast watcher active (db=%s)", self._path)
        while not self._stop.is_set():
            try:
                for event in self.poll_once():
                    self._on_toast(event)
            except Exception:  # noqa: BLE001 - keep the poller alive
                log.exception("Toast poll failed")
            self._stop.wait(self._poll_seconds)
        log.info("Toast watcher stopped")


def enrich_toast(event: ToastEvent) -> dict[str, Any]:
    from .attribute import source_key_for_toast

    return {
        "ts": event.ts,
        "kind": "toast",
        "source_key": source_key_for_toast(event.aumid),
        "display_name": event.app_name or event.aumid,
        "title": event.title,
        "body": event.body,
        "exe_path": event.app_path,
        "exe_name": os.path.basename(event.app_path).lower() if event.app_path else "",
        "pid": None,
        "cmdline": "",
        "publisher": "",
        "parents": [],
        "aumid": event.aumid,
        "window_class": None,
        "category": event.category or "Notification",
        "task_name": None,
        "task_exe": None,
        "action": "logged",
        "details": {"notification_id": event.notification_id, "texts": event.texts},
    }
