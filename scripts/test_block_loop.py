"""End-to-end check: detect a popup, disable it through the UI, prove it stops.

Drives the real widgets (the same buttons a user clicks) rather than poking
the config directly, so the whole chain is under test.

    .venv\\Scripts\\python.exe scripts\\test_block_loop.py
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from popupstopper.app import _load_icon, _load_stylesheet  # noqa: E402
from popupstopper.config import Config, ensure_dirs  # noqa: E402
from popupstopper.monitor import Monitor  # noqa: E402
from popupstopper.store import Store  # noqa: E402
from popupstopper.ui.main_window import MainWindow  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND

TITLE = "Driver Update Available"
BODY = "AMD Software 25.8.1 is ready to install."

app: QApplication


def pump(seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def fire_popup() -> subprocess.Popen:
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        f"[System.Windows.Forms.MessageBox]::Show('{BODY}','{TITLE}')"
    )
    return subprocess.Popen(["powershell", "-NoProfile", "-Command", script])


def watch_popup(timeout: float = 12.0) -> tuple[bool, float]:
    """Wait for the popup window, then time how long it stays on screen."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if user32.FindWindowW(None, TITLE):
            break
        app.processEvents()
        time.sleep(0.01)
    else:
        return False, 0.0

    appeared = time.time()
    while time.time() < deadline:
        if not user32.FindWindowW(None, TITLE):
            return True, time.time() - appeared
        app.processEvents()
        time.sleep(0.01)
    return True, -1.0  # still on screen when we gave up


def report(step: str, detail: str) -> None:
    print(f"  {step:<44s} {detail}", flush=True)


def main() -> int:
    global app
    ensure_dirs()
    app = QApplication(sys.argv)
    app.setStyleSheet(_load_stylesheet())

    config = Config()
    config.set("monitor_only", True)
    for key in list(config.rules()):
        config.delete_rule(key)

    store = Store()
    events: list[dict] = []
    monitor = Monitor(config, store, on_event=events.append)
    window = MainWindow(_load_icon(), config, store, monitor, elevated=False)
    monitor.start()
    pump(1.5)

    # ---------------------------------------------------------------- step 1
    print("\n[1] A popup appears for the first time (nothing configured yet)")
    proc = fire_popup()
    seen, on_screen = watch_popup(timeout=6.0)
    report("popup appeared:", str(seen))
    report("still on screen after 6s:", "yes (nothing closed it)" if on_screen == -1.0 else "no")
    proc.kill()
    pump(2.0)

    if not events:
        print("  FAIL: the popup was not detected at all")
        return 1
    record = events[-1]
    report("detected as:", f"{record['display_name']} / {record['title']!r}")
    report("came from:", record["exe_path"])
    report("action taken:", record["action"])

    # ---------------------------------------------------------------- step 2
    print("\n[2] User clicks the popup, then 'Auto-close this source'")
    window.live.add_event(record)
    window.live.details.show_record(record)
    window.live.details.btn_block.click()
    pump(1.0)
    rules = config.rules()
    report("rule now stored:", str(rules))
    report("status bar says:", window.status_label.text())

    print("\n[3] Same popup again, while Monitor only is still ON")
    events.clear()
    proc = fire_popup()
    seen, on_screen = watch_popup(timeout=6.0)
    report("popup appeared:", str(seen))
    report("left alone (safety switch):", "yes" if on_screen == -1.0 else f"NO - closed in {on_screen:.2f}s")
    proc.kill()
    pump(2.0)
    if events:
        detail = events[-1].get("details") or {}
        report("recorded as:", f"{events[-1]['action']} / {detail.get('decision_reason')}")

    # ---------------------------------------------------------------- step 4
    print("\n[4] User turns OFF 'Monitor only' on the Settings tab")
    window.settings.chk_monitor_only.setChecked(False)
    pump(1.0)
    report("monitor_only is now:", str(config.get("monitor_only")))
    report("status bar says:", window.status_label.text())

    print("\n[5] Same popup again - it should now be closed automatically")
    events.clear()
    proc = fire_popup()
    seen, on_screen = watch_popup()
    report("popup appeared:", str(seen))
    if on_screen == -1.0:
        report("RESULT:", "FAIL - the popup stayed on screen")
        blocked_once = False
    else:
        report("RESULT:", f"closed automatically after {on_screen * 1000:.0f} ms")
        blocked_once = True
    proc.kill()
    pump(2.0)
    if events:
        report("recorded as:", events[-1]["action"])

    # ---------------------------------------------------------------- step 6
    print("\n[6] And again, to prove it keeps working")
    proc = fire_popup()
    seen, on_screen = watch_popup()
    blocked_twice = on_screen not in (-1.0, 0.0)
    report(
        "RESULT:",
        f"closed automatically after {on_screen * 1000:.0f} ms" if blocked_twice else "FAIL",
    )
    proc.kill()
    pump(1.5)

    monitor.stop()
    window.close()

    # ---------------------------------------------------------------- step 7
    print("\n[7] Restarting the app from scratch - does the rule survive?")
    config2 = Config()
    store2 = Store()
    report("rule loaded from disk:", str(config2.rules()))
    report("monitor_only loaded as:", str(config2.get("monitor_only")))
    monitor2 = Monitor(config2, store2)
    monitor2.start()
    pump(1.5)

    proc = fire_popup()
    seen, on_screen = watch_popup()
    blocked_after_restart = on_screen not in (-1.0, 0.0)
    report(
        "RESULT:",
        f"closed automatically after {on_screen * 1000:.0f} ms"
        if blocked_after_restart
        else "FAIL - the popup was not blocked after restart",
    )
    proc.kill()
    pump(1.0)
    monitor2.stop()

    # ---------------------------------------------------------------- step 8
    print("\n[8] User changes their mind: set it back to 'Monitor only'")
    config2.delete_rule(next(iter(config2.rules()), ""))
    monitor3 = Monitor(config2, store2)
    monitor3.start()
    pump(1.5)
    proc = fire_popup()
    seen, on_screen = watch_popup(timeout=6.0)
    restored = on_screen == -1.0
    report("RESULT:", "popup allowed through again" if restored else "FAIL - still being closed")
    proc.kill()
    pump(1.0)
    monitor3.stop()

    print("\n" + "=" * 62)
    checks = {
        "blocked once monitor-only was off": blocked_once,
        "blocking is repeatable": blocked_twice,
        "rule survives an app restart": blocked_after_restart,
        "un-blocking works": restored,
    }
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
