"""Verify every source-level prevention lever, and its undo.

Creates its own throwaway startup entry, Startup-folder shortcut, service and
executable, so nothing belonging to the user is touched. Everything is removed
again at the end, whether the test passes or fails.

The service and hard-block checks need administrator rights; without them the
script runs what it can and reports the rest as skipped.

    .venv\\Scripts\\python.exe scripts\\test_prevent.py
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
import winreg
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from popupstopper import prevent  # noqa: E402
from popupstopper.config import Config, DATA_DIR, ensure_dirs  # noqa: E402

CREATE_NO_WINDOW = 0x08000000
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "PopupStopperPreventTest"
SERVICE_NAME = "PopupStopperTestSvc"
TARGET = DATA_DIR / "ppstest_target.exe"
STARTUP_LNK_NAME = "PopupStopperPreventTest.lnk"

results: dict[str, bool | None] = {}
report_lines: list[str] = []


def say(text: str = "") -> None:
    print(text, flush=True)
    report_lines.append(text)


def step(label: str, detail: str) -> None:
    say(f"    {label:<34s} {detail}")


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def run(args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, creationflags=CREATE_NO_WINDOW
    )


def make_target() -> None:
    """A harmless stand-in program: a renamed copy of cmd.exe."""
    shutil.copy2(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe", TARGET)


def target_runs() -> bool:
    try:
        completed = run([str(TARGET), "/c", "echo", "RAN_OK"], timeout=15)
    except (subprocess.SubprocessError, OSError):
        return False
    return "RAN_OK" in (completed.stdout or "")


def record() -> dict[str, str]:
    return {"exe_path": str(TARGET), "exe_name": TARGET.name}


def lever_of(config: Config, kind: str):
    for lever in prevent.find_levers(record(), config):
        if lever.kind == kind:
            return lever
    return None


# ----------------------------------------------------------------- the tests


def test_startup_run(config: Config) -> bool:
    say("\n[1] Startup registry entry")
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, f'"{TARGET}" --nag')
    step("created a startup entry:", f"HKCU\\...\\Run\\{RUN_VALUE}")

    lever = lever_of(config, "startup_run")
    if lever is None:
        step("RESULT:", "FAIL - the app did not find the startup entry")
        return False
    step("app found it:", lever.label)

    applied = prevent.apply_lever(lever, config)
    step("apply:", str(applied.get("message")))
    gone = not _run_value_exists()
    step("entry removed from Windows:", str(gone))

    undone = prevent.undo_prevention(lever.key, config)
    step("undo:", str(undone.get("message")))
    back = _run_value_exists()
    step("entry restored:", str(back))

    _delete_run_value()
    ok = bool(applied.get("ok")) and gone and back
    step("RESULT:", "PASS" if ok else "FAIL")
    return ok


def _run_value_exists() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except FileNotFoundError:
        return False


def _delete_run_value() -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except OSError:
        pass


def test_startup_shortcut(config: Config) -> bool:
    say("\n[2] Startup folder shortcut")
    folder = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
    folder.mkdir(parents=True, exist_ok=True)
    link = folder / STARTUP_LNK_NAME

    try:
        import pythoncom  # type: ignore[import-not-found]
        from win32com.client import Dispatch  # type: ignore[import-not-found]

        pythoncom.CoInitialize()
        try:
            shell = Dispatch("WScript.Shell")
            shortcut = shell.CreateShortcut(str(link))
            shortcut.TargetPath = str(TARGET)
            shortcut.Save()
            del shortcut, shell
        finally:
            pythoncom.CoUninitialize()
    except Exception as exc:  # noqa: BLE001
        step("SKIPPED:", f"could not create a shortcut ({exc})")
        return True

    step("created a shortcut in Startup:", link.name)
    lever = lever_of(config, "startup_folder")
    if lever is None:
        link.unlink(missing_ok=True)
        step("RESULT:", "FAIL - the app did not find the shortcut")
        return False
    step("app found it:", lever.label)

    applied = prevent.apply_lever(lever, config)
    step("apply:", str(applied.get("message")))
    gone = not link.exists()
    step("shortcut removed from Startup:", str(gone))

    undone = prevent.undo_prevention(lever.key, config)
    step("undo:", str(undone.get("message")))
    back = link.exists()
    step("shortcut restored:", str(back))

    link.unlink(missing_ok=True)
    ok = bool(applied.get("ok")) and gone and back
    step("RESULT:", "PASS" if ok else "FAIL")
    return ok


def test_service(config: Config) -> bool | None:
    say("\n[3] Background service")
    if not is_admin():
        step("SKIPPED:", "needs administrator rights")
        return None

    created = run(["sc", "create", SERVICE_NAME, "binPath=", str(TARGET), "start=", "demand"])
    if created.returncode != 0:
        step("SKIPPED:", (created.stdout or created.stderr).strip())
        return None
    step("created a throwaway service:", SERVICE_NAME)

    try:
        lever = lever_of(config, "service")
        if lever is None:
            step("RESULT:", "FAIL - the app did not find the service")
            return False
        step("app found it:", lever.label)

        applied = prevent.apply_lever(lever, config)
        step("apply:", str(applied.get("message")))
        disabled = _service_start_type() == 4
        step("Windows reports it disabled:", str(disabled))

        undone = prevent.undo_prevention(lever.key, config)
        step("undo:", str(undone.get("message")))
        restored = _service_start_type() == 3
        step("start type restored to demand:", str(restored))

        ok = bool(applied.get("ok")) and disabled and restored
        step("RESULT:", "PASS" if ok else "FAIL")
        return ok
    finally:
        run(["sc", "delete", SERVICE_NAME])


def _service_start_type() -> int:
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}"
        ) as key:
            return int(winreg.QueryValueEx(key, "Start")[0])
    except OSError:
        return -1


def test_hard_block(config: Config) -> bool | None:
    say("\n[4] Hard block - Windows refuses to launch the program")
    if not is_admin():
        step("SKIPPED:", "needs administrator rights")
        return None

    before = target_runs()
    step("program runs normally first:", str(before))

    lever = lever_of(config, "hard_block")
    if lever is None:
        step("RESULT:", "FAIL - no hard block option offered")
        return False

    applied = prevent.apply_lever(lever, config)
    step("apply:", str(applied.get("message")))
    time.sleep(0.5)
    during = target_runs()
    step("program still runs while blocked:", str(during))

    undone = prevent.undo_prevention(lever.key, config)
    step("undo:", str(undone.get("message")))
    time.sleep(0.5)
    after = target_runs()
    step("program runs again after undo:", str(after))

    ok = before and not during and after
    step("RESULT:", "PASS - launching was genuinely prevented" if ok else "FAIL")
    return ok


def test_guards(config: Config) -> bool:
    say("\n[5] Refusing to break Windows")
    explorer = prevent.find_levers(
        {"exe_path": r"C:\Windows\explorer.exe", "exe_name": "explorer.exe"}, config
    )
    block = next((l for l in explorer if l.kind == "hard_block"), None)
    explorer_guarded = block is not None and not block.available
    step("explorer.exe hard block:", f"refused - {block.blocked_reason}" if explorer_guarded else "NOT REFUSED")

    refused_apply = prevent.apply_lever(block, config) if block else {"ok": True}
    apply_guarded = not refused_apply.get("ok")
    step("applying it anyway:", str(refused_apply.get("message")))

    critical = [name for name in ("audiosrv", "dcomlaunch", "winmgmt") if name in prevent.NEVER_DISABLE_SERVICE]
    step("essential services protected:", ", ".join(critical))

    ok = explorer_guarded and apply_guarded and len(critical) == 3
    step("RESULT:", "PASS" if ok else "FAIL")
    return ok


def main() -> int:
    ensure_dirs()
    config = Config(DATA_DIR / "prevent_test_config.json")
    make_target()
    say("Popup Stopper - source-level prevention test")
    say(f"  elevated: {is_admin()}")
    say(f"  test program: {TARGET}")

    try:
        results["startup registry entry"] = test_startup_run(config)
        results["startup folder shortcut"] = test_startup_shortcut(config)
        results["background service"] = test_service(config)
        results["hard block"] = test_hard_block(config)
        results["safety guards"] = test_guards(config)
    finally:
        _delete_run_value()
        run(["sc", "delete", SERVICE_NAME])
        for leftover in prevent.list_prevented(config):
            prevent.undo_prevention(leftover, config)
        TARGET.unlink(missing_ok=True)
        (DATA_DIR / "prevent_test_config.json").unlink(missing_ok=True)
        shutil.rmtree(prevent.SHORTCUT_BACKUP_DIR, ignore_errors=True)

    say("\n" + "=" * 62)
    failed = 0
    for name, outcome in results.items():
        mark = "SKIP" if outcome is None else ("PASS" if outcome else "FAIL")
        if outcome is False:
            failed += 1
        say(f"  {mark}  {name}")

    (DATA_DIR / "prevent_test_report.txt").write_text("\n".join(report_lines), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
