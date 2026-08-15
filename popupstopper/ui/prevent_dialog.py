"""The 'Prevent completely' dialog: pick how to stop a popup at its source."""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import prevent
from ..config import Config
from ..prevent import RISK_SAFE, RISK_STRONG, Lever

log = logging.getLogger(__name__)

RISK_COLOURS = {
    RISK_SAFE: "#2ECC71",
    "caution": "#F39C12",
    RISK_STRONG: "#E74C3C",
}

RISK_WORDS = {
    RISK_SAFE: "SAFE",
    "caution": "CAUTION",
    RISK_STRONG: "STRONG",
}


class _LeverFinder(QThread):
    """Scanning the service list takes a moment, so keep it off the UI thread."""

    found = Signal(list)

    def __init__(self, record: dict[str, Any], config: Config) -> None:
        super().__init__()
        self._record = record
        self._config = config

    def run(self) -> None:  # noqa: D102
        try:
            self.found.emit(prevent.find_levers(self._record, self._config))
        except Exception:  # noqa: BLE001
            log.exception("Looking for prevention options failed")
            self.found.emit([])


class LeverRow(QFrame):
    def __init__(self, lever: Lever) -> None:
        super().__init__()
        self.lever = lever
        self.setObjectName("Card")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(4)

        top = QHBoxLayout()
        self.checkbox = QCheckBox(lever.label)
        font = self.checkbox.font()
        font.setBold(True)
        self.checkbox.setFont(font)
        self.checkbox.setChecked(lever.risk == RISK_SAFE and lever.available)
        self.checkbox.setEnabled(lever.available)
        top.addWidget(self.checkbox, 1)

        badge = QLabel(RISK_WORDS.get(lever.risk, lever.risk.upper()))
        badge.setStyleSheet(
            f"color: {RISK_COLOURS.get(lever.risk, '#9A9A9A')}; font-weight: 600;"
        )
        top.addWidget(badge)
        outer.addLayout(top)

        effect = QLabel(lever.effect)
        effect.setObjectName("SubtleLabel")
        effect.setWordWrap(True)
        outer.addWidget(effect)

        detail = QLabel(lever.detail)
        detail.setObjectName("MonoLabel")
        detail.setWordWrap(True)
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(detail)

        if not lever.available:
            refused = QLabel(f"Not offered: {lever.blocked_reason}.")
            refused.setObjectName("StatusWarn")
            refused.setWordWrap(True)
            outer.addWidget(refused)

    def is_selected(self) -> bool:
        return self.checkbox.isChecked() and self.lever.available


class PreventDialog(QDialog):
    """Shows every lever available for one popup source and applies the chosen ones."""

    def __init__(self, record: dict[str, Any], config: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._record = record
        self._config = config
        self._rows: list[LeverRow] = []
        self.applied_messages: list[str] = []

        name = record.get("display_name") or record.get("exe_name") or "this popup"
        self.setWindowTitle("Prevent this popup completely")
        self.resize(720, 620)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        heading = QLabel(f"Stopping \"{name}\" at the source")
        heading.setObjectName("TitleLabel")
        heading.setWordWrap(True)
        outer.addWidget(heading)

        intro = QLabel(
            "Auto-closing removes a popup about 40 milliseconds after it opens, which can "
            "still pull you out of a fullscreen game. Everything below stops the popup being "
            "created in the first place. Each change is recorded and can be undone from the "
            "Prevented tab."
        )
        intro.setObjectName("SubtleLabel")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        self.status = QLabel("Looking for ways to stop this...")
        self.status.setObjectName("SubtleLabel")
        outer.addWidget(self.status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        holder = QWidget()
        self.list_layout = QVBoxLayout(holder)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        apply_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        apply_button.setText("Apply selected")
        apply_button.setObjectName("PrimaryButton")
        apply_button.setEnabled(False)
        self.buttons.accepted.connect(self._apply)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)

        self._finder = _LeverFinder(record, config)
        self._finder.found.connect(self._show_levers)
        self._finder.start()

    def _show_levers(self, levers: list[Lever]) -> None:
        usable = [lever for lever in levers if lever.available]
        if not levers:
            self.status.setText(
                "No source-level fix is available for this one. It comes from a program "
                "that is already running and is not started by a task, a startup entry or "
                "a service, so auto-close is the only option."
            )
            return

        self.status.setText(
            f"{len(usable)} way(s) to stop this permanently. "
            "Safe options are ticked for you; the strong ones are not."
        )
        for lever in levers:
            row = LeverRow(lever)
            row.checkbox.toggled.connect(self._refresh_button)
            self._rows.append(row)
            self.list_layout.insertWidget(self.list_layout.count() - 1, row)
        self._refresh_button()

    def _refresh_button(self) -> None:
        any_selected = any(row.is_selected() for row in self._rows)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(any_selected)

    def _apply(self) -> None:
        chosen = [row.lever for row in self._rows if row.is_selected()]
        if not chosen:
            return

        strong = [lever for lever in chosen if lever.risk == RISK_STRONG]
        if strong:
            listed = "\n".join(f"  - {lever.label}\n      {lever.detail}" for lever in strong)
            answer = QMessageBox.warning(
                self,
                "Confirm the strong changes",
                "These change how Windows itself behaves:\n\n"
                f"{listed}\n\n"
                "Whatever feature they provide will stop working until you undo them "
                "from the Prevented tab. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        failures: list[str] = []
        for lever in chosen:
            result = prevent.apply_lever(lever, self._config)
            if result.get("ok"):
                self.applied_messages.append(str(result.get("message")))
            else:
                failures.append(f"{lever.label}: {result.get('message')}")

        if failures:
            QMessageBox.warning(
                self,
                "Some changes did not apply",
                "\n\n".join(failures),
            )
        if self.applied_messages:
            self.accept()
        elif not failures:
            self.reject()
