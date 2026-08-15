"""Prove that disabling a scheduled task stops its popup appearing at all.

Auto-close removes a popup a few tens of milliseconds after it opens.
Disabling the task behind it means the popup is never created in the first
place. This script creates a throwaway task, lets it nag, disables it through
the app's own action code, and confirms the nag cannot come back.

    .venv\\Scripts\\python.exe scripts\\test_task_prevention.py
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from popupstopper import actions  # noqa: E402
from popupstopper.config import Config, DATA_DIR, ensure_dirs  # noqa: E402
from popupstopper.monitor import Monitor  # noqa: E402
from popupstopper.store import Store  # noqa: E402
from popupstopper.tasks import task_definition  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND

TASK = "PopupStopperSelfTest"
TASK_PATH = f"\\{TASK}"
TITLE = "Scheduled Nag Test"
CREATE_NO_WINDOW = 0x08000000


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=30, creationflags=CREATE_NO_WINDOW
    )


def report(step: str, detail: str) -> None:
    print(f"  {step:<40s} {detail}", flush=True)


def wait_for_popup(seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if user32.FindWindowW(None, TITLE):
            return True
        time.sleep(0.05)
    return False


def close_popup() -> None:
    hwnd = user32.FindWindowW(None, TITLE)
    if hwnd:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        time.sleep(0.5)


def main() -> int:
    ensure_dirs()
    nag = DATA_DIR / "nag_test.ps1"
    nag.write_text(
        "Add-Type -AssemblyName System.Windows.Forms\n"
        f"[System.Windows.Forms.MessageBox]::Show("
        f"'A driver update is ready to install.','{TITLE}')\n",
        encoding="utf-8",
    )

    print("[1] Creating a throwaway scheduled task that shows a popup")
    created = run([
        "schtasks", "/create", "/tn", TASK,
        "/tr", f'powershell.exe -NoProfile -WindowStyle Hidden -File "{nag}"',
        "/sc", "once", "/st", "23:59", "/f",
    ])
    if created.returncode != 0:
        print("  could not create the task:", (created.stderr or created.stdout).strip())
        return 1
    report("task created:", TASK_PATH)
    definition = task_definition(TASK_PATH)
    report("app reads it as:", f"enabled={definition.get('enabled')}")
    for action in definition.get("actions", []):
        report("runs:", f"{action.get('command')} {action.get('arguments')}".strip())

    config = Config()
    store = Store()
    events: list[dict] = []
    monitor = Monitor(config, store, on_event=events.append)
    monitor.start()
    time.sleep(1.5)

    print("\n[2] Running the task - the nag should appear and be detected")
    run(["schtasks", "/run", "/tn", TASK])
    appeared = wait_for_popup(15.0)
    report("popup appeared:", str(appeared))
    time.sleep(2.5)
    if events:
        record = events[-1]
        report("detected as:", f"{record['display_name']} / {record['title']!r}")
        report("traced to task:", str(record.get("task_name") or "(needs admin for tracing)"))
    close_popup()
    time.sleep(1.0)

    print("\n[3] Disabling the task, exactly as the app's button does")
    result = actions.set_task_enabled(TASK_PATH, False, config)
    report("result:", str(result.get("message")))
    report("original state remembered:", str(config.get("task_backups", {})))
    definition = task_definition(TASK_PATH)
    report("Windows now reports:", f"enabled={definition.get('enabled')}")

    print("\n[4] Trying to make the nag come back")
    events.clear()
    attempt = run(["schtasks", "/run", "/tn", TASK])
    message = (attempt.stderr or attempt.stdout).strip().splitlines()
    report("schtasks /run says:", message[-1] if message else f"exit {attempt.returncode}")
    came_back = wait_for_popup(12.0)
    report("popup appeared:", str(came_back))
    prevented = not came_back
    report("RESULT:", "prevented entirely - nothing to close" if prevented else "FAIL - it still ran")
    if came_back:
        close_popup()

    print("\n[5] Restoring the task, as 'Enable selected task' does")
    restore = actions.set_task_enabled(TASK_PATH, True, config)
    report("result:", str(restore.get("message")))
    definition = task_definition(TASK_PATH)
    restored = bool(definition.get("enabled"))
    report("Windows now reports:", f"enabled={definition.get('enabled')}")
    report("backup entry cleared:", str(not config.get("task_backups", {})))

    print("\n[6] The nag works again once re-enabled")
    run(["schtasks", "/run", "/tn", TASK])
    back = wait_for_popup(15.0)
    report("popup appeared:", str(back))
    close_popup()

    monitor.stop()
    run(["schtasks", "/delete", "/tn", TASK, "/f"])
    nag.unlink(missing_ok=True)
    print("\n  cleaned up the test task")

    print("\n" + "=" * 62)
    checks = {
        "the task's popup was detected": appeared,
        "disabling it prevented the popup completely": prevented,
        "the task was restored afterwards": restored,
        "the popup works again once re-enabled": back,
    }
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
