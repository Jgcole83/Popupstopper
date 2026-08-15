"""Live tab: popups appear here the moment they happen."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .widgets import DetailPane, EventTable


class LivePanel(QWidget):
    stats_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("Popups as they happen")
        title.setObjectName("TitleLabel")
        header.addWidget(title)
        header.addStretch(1)

        self.chk_pause = QCheckBox("Pause the feed")
        header.addWidget(self.chk_pause)

        self.chk_popups_only = QCheckBox("Hide focus changes")
        self.chk_popups_only.setChecked(True)
        header.addWidget(self.chk_popups_only)

        self.btn_clear = QPushButton("Clear view")
        self.btn_clear.clicked.connect(self._clear)
        header.addWidget(self.btn_clear)
        outer.addLayout(header)

        subtitle = QLabel(
            "Leave Popup Stopper running while you play. Everything that opens a dialog or "
            "raises a notification is listed here with the file it came from."
        )
        subtitle.setObjectName("SubtleLabel")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = EventTable(with_date=False, max_rows=500)
        self.details = DetailPane()
        self.table.record_selected.connect(self.details.show_record)
        splitter.addWidget(self.table)
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        outer.addWidget(splitter, 1)

        self.status = QLabel("Waiting for the first popup.")
        self.status.setObjectName("SubtleLabel")
        outer.addWidget(self.status)

        self._count = 0

    def add_event(self, record: dict[str, Any]) -> None:
        if self.chk_pause.isChecked():
            return
        if self.chk_popups_only.isChecked():
            details = record.get("details") or {}
            if isinstance(details, dict) and details.get("popup_like") is False:
                return
        self.table.prepend(record)
        self._count += 1
        self.status.setText(f"{self._count} popups seen since the app started.")

    def _clear(self) -> None:
        self.table.setRowCount(0)
        self._count = 0
        self.status.setText("View cleared. History is still saved under the History tab.")
