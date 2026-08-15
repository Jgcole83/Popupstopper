"""Rotating file + console logging."""

from __future__ import annotations

import logging
import logging.handlers
import sys

from .config import LOG_PATH, ensure_dirs

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"


def setup_logging(level: int = logging.INFO) -> None:
    ensure_dirs()
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)

    # pythonw.exe has no stdout, so only add a console handler when there is one.
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(stream)

    logging.getLogger("PySide6").setLevel(logging.WARNING)
