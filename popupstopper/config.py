"""Configuration and on-disk paths for Popup Stopper."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

APP_NAME = "PopupStopper"
APP_TITLE = "Popup Stopper"

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
ASSETS_DIR = PACKAGE_DIR / "assets"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = DATA_DIR / "logs"
CONFIG_PATH = DATA_DIR / "config.json"
EVENTS_DB_PATH = DATA_DIR / "events.db"
LOG_PATH = LOG_DIR / "popupstopper.log"

ACTION_LOG = "log"
ACTION_CLOSE = "close"
ACTION_MUTE = "mute"

# Processes whose windows are never auto-closed, no matter what rules say.
# These are security surfaces or things the user actively asked for.
PROTECTED_PROCESSES = frozenset(
    {
        "consent.exe",  # UAC elevation prompt
        "credentialuibroker.exe",
        "lsass.exe",
        "logonui.exe",
        "winlogon.exe",
        "csrss.exe",
        "dwm.exe",
        "userinit.exe",
        "systemsettingsbroker.exe",
        "wininit.exe",
        "smss.exe",
        "services.exe",
        "python.exe",  # ourselves
        "pythonw.exe",
    }
)

# Window classes that belong to the shell itself and are not popups.
IGNORED_WINDOW_CLASSES = frozenset(
    {
        "Progman",
        "WorkerW",
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
        "NotifyIconOverflowWindow",
        "TaskListThumbnailWnd",
        "tooltips_class32",
        "SysShadow",
        "IME",
        "MSCTFIME UI",
        "Default IME",
        "DummyDWMListenerWindow",
        "EdgeUiInputTopWndClass",
        "MultitaskingViewFrame",
        "ForegroundStaging",
        "XamlExplorerHostIslandWindow",
        "Windows.UI.Composition.DesktopWindowContentBridge",
        "TridentDialogClass",
        "SysListView32",
        "tooltips_class",
        "OleMainThreadWndClass",
    }
)

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    # Monitor-only is the safety switch: while true, nothing is ever auto-closed.
    "monitor_only": True,
    "start_minimised": False,
    "minimise_to_tray_on_close": True,
    "notify_on_block": True,
    "detect": {
        "windows": True,
        "toasts": True,
        "toast_poll_seconds": 3.0,
        "task_poll_seconds": 15.0,
        # Ordinary application windows are noise; only dialog-style popups are
        # recorded unless the user asks for everything.
        "record_all_windows": False,
        "record_focus_steals": True,
    },
    "gaming_mode": {
        # When enabled, close rules only fire while one of these executables runs.
        "enabled": False,
        "games": [],
    },
    # source_key -> rule dict
    "rules": {},
    # task path -> {"was_enabled": bool, "changed_at": float}
    "task_backups": {},
    # lever key -> everything needed to undo a source-level prevention
    "prevention": {},
    "seen_toast_tick": 0,
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """Thread-safe JSON config with atomic writes."""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data = dict(DEFAULT_CONFIG)
        self.load()

    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                try:
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                    self._data = _deep_merge(DEFAULT_CONFIG, loaded)
                except (json.JSONDecodeError, OSError):
                    # A corrupt config must not stop the monitor from running.
                    self._data = dict(DEFAULT_CONFIG)
            else:
                self._data = dict(DEFAULT_CONFIG)
                self.save()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self.save()

    def update(self, values: dict[str, Any]) -> None:
        with self._lock:
            self._data = _deep_merge(self._data, values)
            self.save()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    # -- rules -------------------------------------------------------------

    def rules(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._data.get("rules", {}))

    def get_rule(self, source_key: str) -> dict[str, Any] | None:
        with self._lock:
            return self._data.get("rules", {}).get(source_key)

    def set_rule(self, source_key: str, rule: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            rules = self._data.setdefault("rules", {})
            existing = rules.get(source_key, {})
            merged = {**existing, **rule, "source_key": source_key, "updated": time.time()}
            rules[source_key] = merged
            self.save()
            return merged

    def delete_rule(self, source_key: str) -> None:
        with self._lock:
            self._data.get("rules", {}).pop(source_key, None)
            self.save()


def ensure_dirs() -> None:
    for path in (DATA_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)
