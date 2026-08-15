"""Sources tab: every program that has popped something up, and what to do about it."""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import actions
from ..config import ACTION_CLOSE, ACTION_LOG, ACTION_MUTE, Config
from ..store import Store
from .widgets import CATEGORY_COLORS, DetailPane, RECORD_ROLE, format_ago

log = logging.getLogger(__name__)

WINDOW_CHOICES = [("Monitor only", ACTION_LOG), ("Auto-close it", ACTION_CLOSE)]
TOAST_CHOICES = [("Monitor only", ACTION_LOG), ("Mute notifications", ACTION_MUTE)]


class SourcesPanel(QWidget):
    action_taken = Signal(str)

    COLUMNS = ("Source", "Category", "Popups", "Last seen", "What to do", "Where it lives")

    def __init__(self, config: Config, store: Store) -> None:
        super().__init__()
        self._config = config
        self._store = store
        self._loading = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("Where your popups come from")
        title.setObjectName("TitleLabel")
        header.addWidget(title)
        header.addStretch(1)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by name, category or path")
        self.search.setMinimumWidth(260)
        self.search.textChanged.connect(self._apply_filter)
        header.addWidget(self.search)

        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self.reload)
        header.addWidget(self.btn_refresh)
        outer.addLayout(header)

        subtitle = QLabel(
            "Each row is one program or app. Change \"What to do\" to control it. "
            "Auto-close only takes effect once you turn off Monitor only on the Settings tab, "
            "so you can never block something by accident."
        )
        subtitle.setObjectName("SubtleLabel")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        head.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 230)
        self.table.setColumnWidth(4, 190)
        self.table.itemSelectionChanged.connect(self._on_select)
        splitter.addWidget(self.table)

        self.details = DetailPane()
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        outer.addWidget(splitter, 1)

        self.status = QLabel("")
        self.status.setObjectName("SubtleLabel")
        outer.addWidget(self.status)

        self._sources: list[dict[str, Any]] = []

    # -- loading -----------------------------------------------------------

    def reload(self) -> None:
        rules = self._config.rules()
        sources = self._store.sources()
        known = {source["source_key"] for source in sources}

        # A rule the user created before the history was cleared should still
        # be visible so it can be undone.
        for key, rule in rules.items():
            if key in known:
                continue
            sources.append(
                {
                    "source_key": key,
                    "display_name": rule.get("display_name") or key,
                    "kind": rule.get("kind", "window"),
                    "category": "Rule only",
                    "count": 0,
                    "last_seen": None,
                    "exe_path": key[4:] if key.startswith("exe:") else "",
                    "aumid": key[6:] if key.startswith("toast:") else None,
                }
            )

        for source in sources:
            rule = rules.get(source["source_key"])
            source["action"] = rule.get("action", ACTION_LOG) if rule else ACTION_LOG

        self._sources = sources
        self._render()

    def _render(self) -> None:
        self._loading = True
        try:
            self.table.setRowCount(0)
            self.table.setRowCount(len(self._sources))
            for row, source in enumerate(self._sources):
                self._fill_row(row, source)
        finally:
            self._loading = False
        self._apply_filter()

        muted = sum(1 for s in self._sources if s.get("action") == ACTION_MUTE)
        closing = sum(1 for s in self._sources if s.get("action") == ACTION_CLOSE)
        total = len(self._sources)
        self.status.setText(
            f"{total} source{'' if total == 1 else 's'} - "
            f"{closing} set to auto-close, {muted} muted."
        )

    def _fill_row(self, row: int, source: dict[str, Any]) -> None:
        is_toast = source.get("kind") == "toast"
        location = source.get("exe_path") or source.get("aumid") or ""

        name_item = QTableWidgetItem(source.get("display_name") or source["source_key"])
        name_item.setData(RECORD_ROLE, source)
        self.table.setItem(row, 0, name_item)

        category = source.get("category") or ""
        category_item = QTableWidgetItem(category)
        colour = CATEGORY_COLORS.get(category)
        if colour:
            category_item.setForeground(QColor(colour))
        self.table.setItem(row, 1, category_item)

        count_item = QTableWidgetItem(str(source.get("count", 0)))
        count_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, 2, count_item)

        self.table.setItem(row, 3, QTableWidgetItem(format_ago(source.get("last_seen"))))

        combo = QComboBox()
        choices = TOAST_CHOICES if is_toast else WINDOW_CHOICES
        for label, value in choices:
            combo.addItem(label, value)
        current = source.get("action", ACTION_LOG)
        index = combo.findData(current)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.currentIndexChanged.connect(
            lambda _index, key=source["source_key"], box=combo: self._on_action_changed(key, box)
        )
        self.table.setCellWidget(row, 4, combo)

        path_item = QTableWidgetItem(location)
        path_item.setToolTip(location)
        self.table.setItem(row, 5, path_item)

    def _apply_filter(self) -> None:
        needle = self.search.text().strip().lower()
        for row in range(self.table.rowCount()):
            if not needle:
                self.table.setRowHidden(row, False)
                continue
            haystack = " ".join(
                self.table.item(row, column).text().lower()
                for column in (0, 1, 5)
                if self.table.item(row, column)
            )
            self.table.setRowHidden(row, needle not in haystack)

    # -- interaction -------------------------------------------------------

    def _selected_source(self) -> dict[str, Any] | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return item.data(RECORD_ROLE) if item else None

    def _on_select(self) -> None:
        source = self._selected_source()
        if not source:
            return
        recent = self._store.list_events(limit=1, source_key=source["source_key"])
        if recent:
            self.details.show_record(recent[0])
        else:
            self.details.show_record(
                {
                    "display_name": source.get("display_name"),
                    "title": "",
                    "kind": source.get("kind", "window"),
                    "category": source.get("category"),
                    "exe_path": source.get("exe_path"),
                    "aumid": source.get("aumid"),
                    "source_key": source["source_key"],
                    "details": {},
                }
            )

    def _on_action_changed(self, source_key: str, combo: QComboBox) -> None:
        if self._loading:
            return
        action = combo.currentData()
        source = next((s for s in self._sources if s["source_key"] == source_key), {})
        display = source.get("display_name") or source_key
        kind = source.get("kind", "window")

        if action == ACTION_LOG:
            self._config.delete_rule(source_key)
            message = f"{display} is back to monitor only."
            if source.get("aumid"):
                result = actions.set_toast_enabled(source["aumid"], True)
                if result.get("ok"):
                    message = f"{display} may show notifications again."
        else:
            self._config.set_rule(
                source_key,
                {
                    "action": action,
                    "display_name": display,
                    "kind": kind,
                    "enabled": True,
                    "title_pattern": None,
                },
            )
            if action == ACTION_MUTE and source.get("aumid"):
                result = actions.set_toast_enabled(source["aumid"], False)
                message = (
                    f"{display} notifications are muted."
                    if result.get("ok")
                    else f"Could not mute {display}: {result.get('message')}"
                )
            elif self._config.get("monitor_only", True):
                message = (
                    f"{display} is marked for auto-close. Turn off Monitor only "
                    "on the Settings tab to make it take effect."
                )
            else:
                message = f"{display} popups will be closed automatically."

        source["action"] = action
        self.status.setText(message)
        self.action_taken.emit(message)

    def apply_rule(self, source_key: str, display_name: str, kind: str, action: str) -> str:
        """Used by the detail pane buttons on other tabs."""
        if action == ACTION_LOG:
            self._config.delete_rule(source_key)
            message = f"{display_name or source_key} is back to monitor only."
        else:
            self._config.set_rule(
                source_key,
                {
                    "action": action,
                    "display_name": display_name,
                    "kind": kind,
                    "enabled": True,
                    "title_pattern": None,
                },
            )
            message = f"{display_name or source_key} set to {action}."
        self.reload()
        return message

    def confirm_disable_task(self, task_name: str) -> None:
        answer = QMessageBox.question(
            self,
            "Disable scheduled task",
            f"Disable this scheduled task?\n\n{task_name}\n\n"
            "It will stop running entirely until you turn it back on from the "
            "Scheduled tasks tab.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        result = actions.set_task_enabled(task_name, False, self._config)
        self.action_taken.emit(result.get("message", ""))
        if not result.get("ok"):
            QMessageBox.warning(self, "Could not disable the task", str(result.get("message")))
