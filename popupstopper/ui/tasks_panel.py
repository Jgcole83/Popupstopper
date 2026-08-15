"""Scheduled tasks tab: see and control the tasks that fire popups."""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import actions, tasks as tasklib
from ..config import Config

log = logging.getLogger(__name__)


class _TaskLoader(QThread):
    loaded = Signal(list)

    def run(self) -> None:  # noqa: D102
        try:
            self.loaded.emit(tasklib.list_tasks())
        except Exception:  # noqa: BLE001
            log.exception("Loading scheduled tasks failed")
            self.loaded.emit([])


class _DefinitionLoader(QThread):
    loaded = Signal(dict)

    def __init__(self, task_name: str) -> None:
        super().__init__()
        self._task_name = task_name

    def run(self) -> None:  # noqa: D102
        try:
            self.loaded.emit(tasklib.task_definition(self._task_name))
        except Exception:  # noqa: BLE001
            log.exception("Reading task definition failed")
            self.loaded.emit({})


class TasksPanel(QWidget):
    action_taken = Signal(str)

    COLUMNS = ("Task", "State", "Folder")

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config
        self._tasks: list[dict[str, Any]] = []
        self._loader: _TaskLoader | None = None
        self._definition_loader: _DefinitionLoader | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("Scheduled tasks")
        title.setObjectName("TitleLabel")
        header.addWidget(title)
        header.addStretch(1)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter, for example: update")
        self.search.setMinimumWidth(240)
        self.search.textChanged.connect(self._apply_filter)
        header.addWidget(self.search)

        self.chk_changed_only = QCheckBox("Only tasks I changed")
        self.chk_changed_only.toggled.connect(self._apply_filter)
        header.addWidget(self.chk_changed_only)

        self.btn_refresh = QPushButton("Load tasks")
        self.btn_refresh.clicked.connect(self.reload)
        header.addWidget(self.btn_refresh)
        outer.addLayout(header)

        subtitle = QLabel(
            "Scheduled tasks are the usual cause of popups that appear out of nowhere. "
            "Disabling one stops it running at all; Popup Stopper remembers what it changed "
            "so you can put it back."
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
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(2, 280)
        self.table.itemSelectionChanged.connect(self._on_select)
        outer.addWidget(self.table, 1)

        self.detail = QLabel("Select a task to see the file it runs.")
        self.detail.setObjectName("MonoLabel")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.detail)

        buttons = QHBoxLayout()
        self.btn_disable = QPushButton("Disable selected task")
        self.btn_disable.setObjectName("DangerButton")
        self.btn_disable.clicked.connect(lambda: self._set_enabled(False))
        self.btn_enable = QPushButton("Enable selected task")
        self.btn_enable.clicked.connect(lambda: self._set_enabled(True))
        self.btn_open = QPushButton("Open file location")
        self.btn_open.clicked.connect(self._open_location)
        for button in (self.btn_disable, self.btn_enable, self.btn_open):
            button.setEnabled(False)
            buttons.addWidget(button)
        buttons.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("SubtleLabel")
        buttons.addWidget(self.status)
        outer.addLayout(buttons)

        self._current_command = ""

    # -- loading -----------------------------------------------------------

    def reload(self) -> None:
        if self._loader and self._loader.isRunning():
            return
        self.btn_refresh.setEnabled(False)
        self.status.setText("Reading the Task Scheduler...")
        self._loader = _TaskLoader()
        self._loader.loaded.connect(self._on_tasks_loaded)
        self._loader.start()

    def _on_tasks_loaded(self, items: list[dict[str, Any]]) -> None:
        self._tasks = items
        self.btn_refresh.setEnabled(True)
        self.btn_refresh.setText("Refresh")

        backups = self._config.get("task_backups", {}) or {}
        self.table.setRowCount(0)
        self.table.setRowCount(len(items))
        for row, task in enumerate(items):
            name_item = QTableWidgetItem(task["name"])
            name_item.setData(Qt.ItemDataRole.UserRole, task)
            if task["task_name"] in backups:
                name_item.setForeground(QColor("#F39C12"))
                name_item.setToolTip("Popup Stopper disabled this task")
            self.table.setItem(row, 0, name_item)

            state_item = QTableWidgetItem(task["state"])
            if task["state"] == "Disabled":
                state_item.setForeground(QColor("#E74C3C"))
            elif task["state"] == "Running":
                state_item.setForeground(QColor("#2ECC71"))
            self.table.setItem(row, 1, state_item)

            self.table.setItem(row, 2, QTableWidgetItem(task["path"]))

        disabled = sum(1 for task in items if task["state"] == "Disabled")
        self.status.setText(f"{len(items)} tasks, {disabled} disabled.")
        self._apply_filter()

    def _apply_filter(self) -> None:
        needle = self.search.text().strip().lower()
        backups = self._config.get("task_backups", {}) or {}
        changed_only = self.chk_changed_only.isChecked()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            task = item.data(Qt.ItemDataRole.UserRole) if item else {}
            visible = True
            if needle:
                visible = needle in task.get("task_name", "").lower()
            if visible and changed_only:
                visible = task.get("task_name") in backups
            self.table.setRowHidden(row, not visible)

    # -- selection ---------------------------------------------------------

    def _selected_task(self) -> dict[str, Any] | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_select(self) -> None:
        task = self._selected_task()
        enabled = task is not None
        for button in (self.btn_disable, self.btn_enable):
            button.setEnabled(enabled)
        self.btn_open.setEnabled(False)
        self._current_command = ""
        if not task:
            return
        self.detail.setText(f"{task['task_name']}  -  reading definition...")
        if self._definition_loader and self._definition_loader.isRunning():
            self._definition_loader.wait(1500)
        self._definition_loader = _DefinitionLoader(task["task_name"])
        self._definition_loader.loaded.connect(self._on_definition)
        self._definition_loader.start()

    def _on_definition(self, definition: dict[str, Any]) -> None:
        task = self._selected_task()
        if not task:
            return
        actions_list = definition.get("actions") or []
        if not actions_list:
            self.detail.setText(f"{task['task_name']}\nNo executable action found.")
            return
        lines = [task["task_name"]]
        for entry in actions_list:
            command = entry.get("command", "")
            arguments = entry.get("arguments", "")
            lines.append(f"Runs: {command} {arguments}".rstrip())
            if not self._current_command:
                self._current_command = command.strip('"')
        triggers = definition.get("triggers") or []
        if triggers:
            lines.append("Triggers: " + ", ".join(triggers))
        self.detail.setText("\n".join(lines))
        self.btn_open.setEnabled(bool(self._current_command))

    # -- actions -----------------------------------------------------------

    def _set_enabled(self, enabled: bool) -> None:
        task = self._selected_task()
        if not task:
            return
        name = task["task_name"]
        if not enabled:
            answer = QMessageBox.question(
                self,
                "Disable scheduled task",
                f"Disable this scheduled task?\n\n{name}\n\n"
                "It will not run again until you enable it here.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        result = actions.set_task_enabled(name, enabled, self._config)
        if result.get("ok"):
            self.status.setText(f"{name}: {result.get('message')}")
            self.action_taken.emit(f"{task['name']} {'enabled' if enabled else 'disabled'}.")
            self.reload()
        else:
            QMessageBox.warning(self, "Could not change the task", str(result.get("message")))
            self.status.setText(str(result.get("message")))

    def _open_location(self) -> None:
        if self._current_command:
            actions.open_in_explorer(self._current_command)
