"""Entry point: ``python -m popupstopper`` or ``pythonw popupstopper\\__main__.py``."""

from __future__ import annotations

import sys
from pathlib import Path

# Allow launching this file directly (the logon task and desktop shortcut do),
# not just as a module, by making sure the project root is importable.
_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from popupstopper.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
