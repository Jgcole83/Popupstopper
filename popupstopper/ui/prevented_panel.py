"""Prevented tab: everything Popup Stopper has changed, each with an Undo."""

from __future__ import annotations

import datetime as dt

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import prevent
from ..config import Config
from .prevent_dialog import RISK_COLOURS, RISK_WORDS

KIND_LABELS = {
    "task": "Scheduled task",
    "toast": "Notifications",
    "startup_run": "Startup entry",
    "startup_folder": "Startup shortcut",
    "service": "Service",
    "hard_block": "Program blocked",
}


class PreventedPanel(QWidget):
    action_taken = Signal(str)

    COLUMNS = ("Type", "What was changed", "Details", "Risk", "When", "")

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("Changes Popup Stopper has made")
        title.setObjectName("TitleLabel")
        header.addWidget(title)
        header.addStretch(1)
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self.reload)
        header.addWidget(self.btn_refresh)
        self.btn_undo_all = QPushButton("Undo everything")
        self.btn_undo_all.clicked.connect(self._undo_all)
        header.addWidget(self.btn_undo_all)
        outer.addLayout(header)

        subtitle = QLabel(
            "Every system change made to stop a popup is listed here so nothing is left "
            "behind. Undo puts the original setting back exactly as it was."
        )
        subtitle.setObjectName("SubtleLabel")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        # Fixed, because a cell widget does not report a width to the header.
        head.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 280)
        self.table.setColumnWidth(5, 90)
        outer.addWidget(self.table, 1)

        self.status = QLabel("")
        self.status.setObjectName("SubtleLabel")
        outer.addWidget(self.status)

        self.reload()

    def reload(self) -> None:
        entries = prevent.list_prevented(self._config)
        ordered = sorted(entries.items(), key=lambda item: item[1].get("applied_at", 0), reverse=True)

        self.table.setRowCount(0)
        self.table.setRowCount(len(ordered))
        for row, (key, entry) in enumerate(ordered):
            kind = entry.get("kind", "")
            self.table.setItem(row, 0, QTableWidgetItem(KIND_LABELS.get(kind, kind)))
            self.table.setItem(row, 1, QTableWidgetItem(entry.get("label", "")))

            detail = QTableWidgetItem(entry.get("detail", ""))
            detail.setToolTip(entry.get("detail", ""))
            self.table.setItem(row, 2, detail)

            risk = entry.get("risk", "safe")
            risk_item = QTableWidgetItem(RISK_WORDS.get(risk, risk.upper()))
            risk_item.setForeground(QColor(RISK_COLOURS.get(risk, "#9A9A9A")))
            self.table.setItem(row, 3, risk_item)

            when = entry.get("applied_at")
            stamp = (
                dt.datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M") if when else ""
            )
            self.table.setItem(row, 4, QTableWidgetItem(stamp))

            button = QPushButton("Undo")
            button.clicked.connect(lambda _checked=False, k=key: self._undo(k))
            self.table.setCellWidget(row, 5, button)

        if ordered:
            self.status.setText(f"{len(ordered)} change(s) in place.")
        else:
            self.status.setText(
                "Nothing has been changed. Use \"Prevent completely\" on a popup to stop "
                "it happening again."
            )

    def _undo(self, key: str) -> None:
        result = prevent.undo_prevention(key, self._config)
        self.status.setText(str(result.get("message")))
        self.action_taken.emit(str(result.get("message")))
        if not result.get("ok"):
            QMessageBox.warning(self, "Could not undo", str(result.get("message")))
        self.reload()

    def _undo_all(self) -> None:
        entries = prevent.list_prevented(self._config)
        if not entries:
            return
        answer = QMessageBox.question(
            self,
            "Undo everything",
            f"Put back all {len(entries)} change(s) Popup Stopper has made?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        failures = []
        for key in list(entries):
            result = prevent.undo_prevention(key, self._config)
            if not result.get("ok"):
                failures.append(str(result.get("message")))
        self.reload()
        if failures:
            QMessageBox.warning(self, "Some changes could not be undone", "\n".join(failures))
        else:
            self.action_taken.emit("All changes have been put back.")
