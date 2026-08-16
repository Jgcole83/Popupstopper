"""Reusable pieces shared by the Live, Sources and History tabs."""

from __future__ import annotations

import datetime as dt
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

RECORD_ROLE = Qt.ItemDataRole.UserRole + 1

# Why a popup was tied to a scheduled task, in words rather than jargon, so it
# is obvious when the link is proven and when it is an inference.
CONFIDENCE_WORDS = {
    "exact": "Confirmed - this popup came from a process the task started",
    "executable": "Confirmed - the task launches this same program",
    "console-host": "Likely - it was the only console task running at that moment",
    "known-component": "This program is a known Windows Update component",
}

CATEGORY_COLORS = {
    "Windows Update": "#FFB44D",
    "Update": "#FFB44D",
    "Driver / GPU": "#FF8A8A",
    "Installer": "#FFD166",
    "Game launcher": "#7FC4FF",
    "Security": "#6FDCAE",
    "Windows shell": "#B8A6FF",
    "Windows nag": "#B8A6FF",
    "Windows system": "#B8A6FF",
    "Browser": "#8FB6FF",
    "Chat / social": "#C4A7FF",
    "Cloud storage": "#7FE3E3",
    "Scheduled task / script": "#FFC08A",
    "Notification": "#9FB4D0",
}

ACTION_COLORS = {
    "closed": "#2ECC71",
    "logged": "#9A9A9A",
}


def format_time(ts: float | None, with_date: bool = False) -> str:
    if not ts:
        return ""
    moment = dt.datetime.fromtimestamp(ts)
    return moment.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")


def format_ago(ts: float | None) -> str:
    if not ts:
        return "never"
    seconds = max(0, dt.datetime.now().timestamp() - ts)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def kind_label(record: dict[str, Any]) -> str:
    if record.get("kind") == "toast":
        return "Toast"
    if (record.get("window_class") or "") == "#32770":
        return "Dialog"
    return "Window"


def action_label(record: dict[str, Any]) -> str:
    if record.get("action") == "closed":
        return "Auto-closed"
    details = record.get("details") or {}
    if isinstance(details, dict) and details.get("would_close"):
        return "Would close"
    return "Logged"


def card(title: str = "") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(8)
    if title:
        label = QLabel(title)
        label.setObjectName("TitleLabel")
        layout.addWidget(label)
    return frame, layout


class EventTable(QTableWidget):
    """A list of recorded popups. Used by both the Live and History tabs."""

    COLUMNS = ("Time", "Type", "Source", "Title", "Category", "Scheduled task", "Result")

    record_selected = Signal(dict)

    def __init__(self, with_date: bool = False, max_rows: int = 0) -> None:
        super().__init__(0, len(self.COLUMNS))
        self._with_date = with_date
        self._max_rows = max_rows

        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self.setColumnWidth(2, 190)
        self.setColumnWidth(5, 200)

        self.itemSelectionChanged.connect(self._emit_selection)

    # -- data --------------------------------------------------------------

    def _make_row(self, row: int, record: dict[str, Any]) -> None:
        task = record.get("task_name") or ""
        values = [
            format_time(record.get("ts"), self._with_date),
            kind_label(record),
            record.get("display_name") or record.get("exe_name") or "Unknown",
            record.get("title") or (record.get("body") or "").split("\n")[0] or "(no title)",
            record.get("category") or "",
            task,
            action_label(record),
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 0:
                item.setData(RECORD_ROLE, record)
            if column == 4:
                colour = CATEGORY_COLORS.get(record.get("category") or "")
                if colour:
                    item.setForeground(QColor(colour))
            if column == 6:
                if record.get("action") == "closed":
                    item.setForeground(QColor("#2ECC71"))
                elif value == "Would close":
                    item.setForeground(QColor("#F39C12"))
                else:
                    item.setForeground(QColor("#9A9A9A"))
            item.setToolTip(value)
            self.setItem(row, column, item)

    def prepend(self, record: dict[str, Any]) -> None:
        self.insertRow(0)
        self._make_row(0, record)
        if self._max_rows and self.rowCount() > self._max_rows:
            self.removeRow(self.rowCount() - 1)

    def set_records(self, records: list[dict[str, Any]]) -> None:
        self.setRowCount(0)
        self.setRowCount(len(records))
        for row, record in enumerate(records):
            self._make_row(row, record)

    def selected_record(self) -> dict[str, Any] | None:
        rows = self.selectionModel().selectedRows() if self.selectionModel() else []
        if not rows:
            return None
        item = self.item(rows[0].row(), 0)
        return item.data(RECORD_ROLE) if item else None

    def _emit_selection(self) -> None:
        record = self.selected_record()
        if record:
            self.record_selected.emit(record)


class DetailPane(QWidget):
    """Full attribution for one popup, plus the actions you can take on it."""

    block_requested = Signal(str, str, str)  # source_key, display_name, kind
    allow_requested = Signal(str)  # source_key
    mute_requested = Signal(str, str)  # aumid, display_name
    open_path_requested = Signal(str)
    disable_task_requested = Signal(str)
    prevent_requested = Signal(dict)  # the whole record

    def __init__(self) -> None:
        super().__init__()
        self._record: dict[str, Any] | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        self.heading = QLabel("Select a popup to see where it came from")
        self.heading.setObjectName("TitleLabel")
        self.heading.setWordWrap(True)
        outer.addWidget(self.heading)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        self.form = QFormLayout(body)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setHorizontalSpacing(16)
        self.form.setVerticalSpacing(6)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.btn_prevent = QPushButton("Prevent completely...")
        self.btn_prevent.setObjectName("PrimaryButton")
        self.btn_prevent.setToolTip(
            "Stop this popup being created at all, instead of closing it after it appears"
        )
        self.btn_prevent.clicked.connect(self._prevent)
        self.btn_block = QPushButton("Auto-close this source")
        self.btn_block.clicked.connect(self._block)
        self.btn_allow = QPushButton("Stop auto-closing")
        self.btn_allow.clicked.connect(self._allow)
        self.btn_mute = QPushButton("Mute notifications")
        self.btn_mute.clicked.connect(self._mute)
        self.btn_task = QPushButton("Disable scheduled task")
        self.btn_task.setObjectName("DangerButton")
        self.btn_task.clicked.connect(self._disable_task)
        self.btn_open = QPushButton("Open file location")
        self.btn_open.clicked.connect(self._open_path)
        for button in (
            self.btn_prevent,
            self.btn_block,
            self.btn_allow,
            self.btn_mute,
            self.btn_task,
            self.btn_open,
        ):
            button.setEnabled(False)
            buttons.addWidget(button)
        buttons.addStretch(1)
        outer.addLayout(buttons)

    # -- population --------------------------------------------------------

    def _clear_form(self) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)

    def _add(self, label: str, value: str, mono: bool = False) -> None:
        if not value:
            return
        widget = QLabel(value)
        widget.setWordWrap(True)
        widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if mono:
            widget.setObjectName("MonoLabel")
        caption = QLabel(label)
        caption.setObjectName("SubtleLabel")
        self.form.addRow(caption, widget)

    def show_record(self, record: dict[str, Any]) -> None:
        self._record = record
        self._clear_form()

        name = record.get("display_name") or record.get("exe_name") or "Unknown source"
        self.heading.setText(f"{name} - {record.get('title') or kind_label(record)}")

        details = record.get("details") or {}
        if not isinstance(details, dict):
            details = {}

        self._add("Seen at", format_time(record.get("ts"), with_date=True))
        self._add("Type", kind_label(record))
        self._add("Category", record.get("category") or "")
        self._add("Window title", record.get("title") or "")
        self._add("Message", record.get("body") or "")
        self._add("Program file", record.get("exe_path") or "", mono=True)
        self._add("Publisher", record.get("publisher") or "")
        self._add("Process id", str(record.get("pid")) if record.get("pid") else "")
        self._add("Command line", record.get("cmdline") or "", mono=True)
        self._add("App id", record.get("aumid") or "", mono=True)
        self._add("Window class", record.get("window_class") or "", mono=True)

        if record.get("task_name"):
            self._add("Scheduled task", str(record["task_name"]))
            self._add("Task runs", record.get("task_exe") or "", mono=True)
            confidence = details.get("task_confidence")
            if confidence:
                self._add("How we know", CONFIDENCE_WORDS.get(confidence, confidence))

        chain = record.get("parents") or []
        if isinstance(chain, list) and len(chain) > 1:
            trail = " <- ".join(
                f"{entry.get('name') or '?'} ({entry.get('pid')})" for entry in chain[:6]
            )
            self._add("Started by", trail, mono=True)

        self._add("Result", action_label(record))
        self._add("Why", details.get("decision_reason") or "")
        if details.get("close_error"):
            self._add("Close problem", str(details["close_error"]))

        is_toast = record.get("kind") == "toast"
        self.btn_prevent.setEnabled(
            bool(record.get("exe_path") or record.get("aumid") or record.get("task_name"))
        )
        self.btn_open.setEnabled(bool(record.get("exe_path")))
        self.btn_block.setEnabled(not is_toast)
        self.btn_allow.setEnabled(True)
        self.btn_mute.setEnabled(bool(record.get("aumid")))
        self.btn_task.setEnabled(bool(record.get("task_name")))

    # -- actions -----------------------------------------------------------

    def _open_path(self) -> None:
        if self._record and self._record.get("exe_path"):
            self.open_path_requested.emit(self._record["exe_path"])

    def _prevent(self) -> None:
        if self._record:
            self.prevent_requested.emit(self._record)

    def _block(self) -> None:
        if self._record:
            self.block_requested.emit(
                self._record.get("source_key", ""),
                self._record.get("display_name") or "",
                self._record.get("kind") or "window",
            )

    def _allow(self) -> None:
        if self._record:
            self.allow_requested.emit(self._record.get("source_key", ""))

    def _mute(self) -> None:
        if self._record and self._record.get("aumid"):
            self.mute_requested.emit(
                self._record["aumid"], self._record.get("display_name") or ""
            )

    def _disable_task(self) -> None:
        if self._record and self._record.get("task_name"):
            self.disable_task_requested.emit(self._record["task_name"])
