"""Check that popups are tied to the right scheduled task, or to none at all.

The case that motivated this: an hourly task ran python.exe, whose console was
hosted by Windows Terminal. Terminal's process ancestry has nothing to do with
the task, so PID matching failed and the old code fell back to "whichever task
ran near that time", which named a completely unrelated Windows task.

    .venv\\Scripts\\python.exe scripts\\test_task_matching.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from popupstopper.tasks import TaskCorrelator, TaskLaunch  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str) -> None:
    results.append((name, passed, detail))
    print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print(f"        {detail}")


def correlator_with(launches: list[TaskLaunch]) -> TaskCorrelator:
    correlator = TaskCorrelator()
    correlator._launches = launches  # noqa: SLF001 - test seam
    return correlator


NOW = time.time()


def test_unrelated_task_is_not_blamed() -> None:
    """The original bug: a terminal popup blamed on a task that just ran nearby."""
    correlator = correlator_with(
        [
            TaskLaunch(r"\Microsoft\Windows\PI\Secure-Boot-Update",
                       r"C:\Windows\System32\SecureBootUpdates.exe", 4321, NOW - 2, 129),
        ]
    )
    match = correlator.find(
        pids=[9000, 9001],
        when=NOW,
        chain=[{"pid": 9000, "name": "WindowsTerminal.exe", "exe": r"C:\...\WindowsTerminal.exe"}],
        exe_name="WindowsTerminal.exe",
    )
    check(
        "an unrelated task is never named",
        match is None,
        "Secure-Boot-Update ran 2s earlier but launches a non-console program, "
        f"so it is not reported -> {match}",
    )


def test_console_task_is_found() -> None:
    """The right answer for the same popup: the task that ran python.exe."""
    correlator = correlator_with(
        [
            TaskLaunch(r"\Ml Pipeline",
                       r"C:\Users\Jgcol\AppData\Local\Programs\Python\Python311\python.exe",
                       7777, NOW - 1, 129),
            TaskLaunch(r"\Microsoft\Windows\Flighting\OneSettings\RefreshCache",
                       r"C:\Windows\System32\rundll32.exe", 8888, NOW - 1, 129),
        ]
    )
    match = correlator.find(
        pids=[9000],
        when=NOW,
        chain=[{"pid": 9000, "name": "WindowsTerminal.exe", "exe": r"C:\...\WindowsTerminal.exe"}],
        exe_name="WindowsTerminal.exe",
    )
    ok = bool(match) and match["task_name"] == r"\Ml Pipeline"
    check(
        "the console task behind a terminal window is found",
        ok,
        f"picked python.exe's task over the rundll32 one -> {match}",
    )


def test_ambiguity_reports_nothing() -> None:
    correlator = correlator_with(
        [
            TaskLaunch(r"\Ml Pipeline", r"C:\Python311\python.exe", 7777, NOW - 1, 129),
            TaskLaunch(r"\Other Job", r"C:\Windows\System32\cmd.exe", 8888, NOW - 2, 129),
        ]
    )
    match = correlator.find(
        pids=[9000], when=NOW,
        chain=[{"pid": 9000, "name": "WindowsTerminal.exe", "exe": ""}],
        exe_name="WindowsTerminal.exe",
    )
    check(
        "two possible console tasks means no guess",
        match is None,
        f"both could have caused it, so neither is named -> {match}",
    )


def test_pid_match_still_wins() -> None:
    correlator = correlator_with(
        [
            TaskLaunch(r"\Real Culprit", r"C:\thing\updater.exe", 4242, NOW - 5, 129),
            TaskLaunch(r"\Innocent", r"C:\other\thing.exe", 5555, NOW - 1, 129),
        ]
    )
    match = correlator.find(
        pids=[9100, 4242], when=NOW,
        chain=[{"pid": 9100, "name": "updater.exe", "exe": r"C:\thing\updater.exe"}],
        exe_name="updater.exe",
    )
    ok = bool(match) and match["task_name"] == r"\Real Culprit" and match["confidence"] == "exact"
    check(
        "a process-ancestry hit still wins",
        ok,
        f"the popup descends from the task's own process -> {match}",
    )


def test_executable_match() -> None:
    correlator = correlator_with(
        [TaskLaunch(r"\Updater", r"C:\vendor\nag.exe", 0, NOW - 300, 200)]
    )
    match = correlator.find(
        pids=[9200], when=NOW,
        chain=[{"pid": 9200, "name": "nag.exe", "exe": r"C:\vendor\nag.exe"}],
        exe_name="nag.exe",
    )
    ok = bool(match) and match["confidence"] == "executable"
    check(
        "the task that launches this same program is matched",
        ok,
        f"no PID recorded, but the executable is the same -> {match}",
    )


def test_normal_app_gets_nothing() -> None:
    correlator = correlator_with(
        [TaskLaunch(r"\Something", r"C:\Python311\python.exe", 7777, NOW - 1, 129)]
    )
    match = correlator.find(
        pids=[9300], when=NOW,
        chain=[{"pid": 9300, "name": "chrome.exe", "exe": r"C:\chrome\chrome.exe"}],
        exe_name="chrome.exe",
    )
    check(
        "an ordinary app window is not tied to a task",
        match is None,
        f"chrome is not a console host and shares nothing with the task -> {match}",
    )


def main() -> int:
    print("Popup Stopper - scheduled task attribution\n")
    test_unrelated_task_is_not_blamed()
    test_console_task_is_found()
    test_ambiguity_reports_nothing()
    test_pid_match_still_wins()
    test_executable_match()
    test_normal_app_gets_nothing()

    failed = [name for name, ok, _ in results if not ok]
    print("\n" + "=" * 62)
    print(f"  {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
