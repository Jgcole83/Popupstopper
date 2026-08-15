"""UAC / elevation helpers.

Popup Stopper always wants to run elevated: enabling the Task Scheduler trace
log and enabling or disabling scheduled tasks both require it.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def is_admin() -> bool:
    if os.name != "nt":
        return os.geteuid() == 0  # type: ignore[attr-defined]
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def relaunch_elevated(extra_args: list[str] | None = None) -> bool:
    """Re-run this app elevated.

    Returns True when the elevation request was accepted, in which case the
    caller should exit and let the new process take over. Returns False if we
    are already elevated or the user dismissed the UAC prompt.
    """
    if is_admin():
        return False
    try:
        exe = sys.executable
        # pythonw.exe keeps a stray console window from flashing up.
        if Path(exe).name.lower() == "python.exe":
            candidate = Path(exe).with_name("pythonw.exe")
            if candidate.exists():
                exe = str(candidate)

        args = ["-m", "popupstopper"]
        args += [a for a in (extra_args or sys.argv[1:]) if not a.endswith("__main__.py")]
        params = " ".join(f'"{arg}"' for arg in args)

        # Run from the project root so "-m popupstopper" resolves.
        working_dir = str(Path(__file__).resolve().parents[2])
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, working_dir, 1)
        if int(result) <= 32:
            log.warning("Elevation declined or failed (ShellExecuteW returned %s)", result)
            return False
        log.info("Elevation granted; the unelevated process will exit")
        return True
    except Exception:  # noqa: BLE001
        log.exception("Could not request elevation")
        return False
