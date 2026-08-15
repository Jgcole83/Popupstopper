"""History tab: search everything Popup Stopper has ever recorded."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..store import Store
from .widgets import DetailPane, EventTable


class HistoryPanel(QWidget):
    action_taken = Signal(str)

    def __init__(self, store: Store) -> None:
        super().__init__()
        self._store = store

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("Everything recorded")
        title.setObjectName("TitleLabel")
        header.addWidget(title)
        header.addStretch(1)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search titles, message text, file paths, tasks")
        self.search.setMinimumWidth(300)
        header.addWidget(self.search)

        self.kind = QComboBox()
        self.kind.addItem("All types", None)
        self.kind.addItem("Windows and dialogs", "window")
        self.kind.addItem("Toast notifications", "toast")
        header.addWidget(self.kind)

        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self.reload)
        header.addWidget(self.btn_refresh)

        self.btn_clear = QPushButton("Delete history")
        self.btn_clear.setObjectName("DangerButton")
        self.btn_clear.clicked.connect(self._clear_history)
        header.addWidget(self.btn_clear)
        outer.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = EventTable(with_date=True)
        self.details = DetailPane()
        self.table.record_selected.connect(self.details.show_record)
        splitter.addWidget(self.table)
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        outer.addWidget(splitter, 1)

        self.status = QLabel("")
        self.status.setObjectName("SubtleLabel")
        outer.addWidget(self.status)

        # Debounce typing so every keystroke does not hit the database.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self.reload)
        self.search.textChanged.connect(lambda _text: self._debounce.start())
        self.kind.currentIndexChanged.connect(lambda _index: self.reload())

    def reload(self) -> None:
        records = self._store.list_events(
            limit=1000,
            query=self.search.text().strip() or None,
            kind=self.kind.currentData(),
        )
        self.table.set_records(records)
        self.status.setText(f"Showing {len(records)} records (newest first).")

    def _clear_history(self) -> None:
        answer = QMessageBox.question(
            self,
            "Delete history",
            "Delete every recorded popup? Your rules and settings are kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = self._store.clear()
        self.reload()
        self.action_taken.emit(f"Deleted {removed} recorded popups.")
