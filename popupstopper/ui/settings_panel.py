"""Settings tab: the safety switch, gaming mode and system-level controls."""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import actions, tasks as tasklib
from ..config import Config
from ..monitor import Monitor

log = logging.getLogger(__name__)


def _describe(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SubtleLabel")
    label.setWordWrap(True)
    return label


class SettingsPanel(QWidget):
    action_taken = Signal(str)
    monitor_only_changed = Signal(bool)

    def __init__(
        self,
        config: Config,
        monitor: Monitor,
        elevated: bool,
        on_restart_elevated: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._monitor = monitor
        self._elevated = elevated
        self._on_restart_elevated = on_restart_elevated
        self._loading = True

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        page = QWidget()
        scroll.setWidget(page)
        wrapper = QVBoxLayout(self)
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.addWidget(scroll)

        outer = QVBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(14)

        outer.addWidget(self._build_blocking_group())
        outer.addWidget(self._build_gaming_group())
        outer.addWidget(self._build_windows_group())
        outer.addWidget(self._build_recording_group())
        outer.addWidget(self._build_app_group())
        outer.addStretch(1)

        self._loading = False
        self.refresh()

    # -- groups ------------------------------------------------------------

    def _build_blocking_group(self) -> QGroupBox:
        group = QGroupBox("Blocking")
        layout = QVBoxLayout(group)
        layout.addWidget(
            _describe(
                "Monitor only is the master safety switch. While it is on, Popup Stopper "
                "records everything but never closes a window, no matter what rules you set. "
                "Turn it off once you are happy with your rules on the Sources tab."
            )
        )
        self.chk_monitor_only = QCheckBox("Monitor only - record popups but never close them")
        self.chk_monitor_only.toggled.connect(self._on_monitor_only)
        layout.addWidget(self.chk_monitor_only)

        self.lbl_rules = QLabel("")
        self.lbl_rules.setObjectName("SubtleLabel")
        layout.addWidget(self.lbl_rules)
        return group

    def _build_gaming_group(self) -> QGroupBox:
        group = QGroupBox("Gaming mode")
        layout = QVBoxLayout(group)
        layout.addWidget(
            _describe(
                "Apply your auto-close rules only while a game is running, so the same popup "
                "still reaches you at the desktop. List the game executables, separated by commas."
            )
        )
        self.chk_gaming = QCheckBox("Only auto-close while one of my games is running")
        self.chk_gaming.toggled.connect(self._save_gaming)
        layout.addWidget(self.chk_gaming)

        row = QHBoxLayout()
        self.txt_games = QLineEdit()
        self.txt_games.setPlaceholderText("eldenring.exe, cs2.exe, valorant.exe")
        row.addWidget(self.txt_games, 1)
        btn_save = QPushButton("Save games")
        btn_save.clicked.connect(self._save_gaming)
        row.addWidget(btn_save)
        layout.addLayout(row)

        self.lbl_games_running = QLabel("")
        self.lbl_games_running.setObjectName("SubtleLabel")
        layout.addWidget(self.lbl_games_running)
        return group

    def _build_windows_group(self) -> QGroupBox:
        group = QGroupBox("Windows settings")
        layout = QVBoxLayout(group)

        layout.addWidget(
            _describe(
                "The restart nag is the Windows Update prompt that interrupts games. Turning it "
                "off does not stop updates installing, it only silences the reminder."
            )
        )
        self.chk_update_nag = QCheckBox("Show \"restart to finish updating\" notifications")
        self.chk_update_nag.toggled.connect(self._on_update_nag)
        layout.addWidget(self.chk_update_nag)

        layout.addSpacing(6)
        self.lbl_toasts_global = QLabel("")
        self.lbl_toasts_global.setWordWrap(True)
        layout.addWidget(self.lbl_toasts_global)

        layout.addSpacing(6)
        layout.addWidget(
            _describe(
                "Task Scheduler tracing lets Popup Stopper name the exact task behind a popup "
                "instead of guessing from timing."
            )
        )
        row = QHBoxLayout()
        self.lbl_tasklog = QLabel("Checking...")
        row.addWidget(self.lbl_tasklog)
        row.addStretch(1)
        self.btn_tasklog = QPushButton("Turn on tracing")
        self.btn_tasklog.clicked.connect(self._enable_task_log)
        row.addWidget(self.btn_tasklog)
        layout.addLayout(row)

        layout.addSpacing(6)
        row2 = QHBoxLayout()
        self.lbl_elevation = QLabel("")
        row2.addWidget(self.lbl_elevation)
        row2.addStretch(1)
        self.btn_elevate = QPushButton("Restart as administrator")
        self.btn_elevate.clicked.connect(self._restart_elevated)
        row2.addWidget(self.btn_elevate)
        layout.addLayout(row2)
        return group

    def _build_recording_group(self) -> QGroupBox:
        group = QGroupBox("What gets recorded")
        layout = QVBoxLayout(group)
        layout.addWidget(
            _describe(
                "By default only dialog-style popups are recorded. Recording every window is "
                "thorough but produces a lot of noise."
            )
        )
        self.chk_all_windows = QCheckBox("Record every window that opens")
        self.chk_all_windows.toggled.connect(self._save_detect)
        layout.addWidget(self.chk_all_windows)

        self.chk_focus_steals = QCheckBox("Record anything that steals focus while gaming")
        self.chk_focus_steals.toggled.connect(self._save_detect)
        layout.addWidget(self.chk_focus_steals)
        return group

    def _build_app_group(self) -> QGroupBox:
        group = QGroupBox("Application")
        layout = QVBoxLayout(group)

        self.chk_autostart = QCheckBox("Start Popup Stopper with Windows (already elevated)")
        self.chk_autostart.toggled.connect(self._on_autostart)
        layout.addWidget(self.chk_autostart)
        layout.addWidget(
            _describe(
                "This registers a logon task so the app can start with administrator rights "
                "without a UAC prompt every time you sign in."
            )
        )

        self.chk_tray_on_close = QCheckBox("Keep running in the tray when I close the window")
        self.chk_tray_on_close.toggled.connect(self._save_app)
        layout.addWidget(self.chk_tray_on_close)

        self.chk_start_min = QCheckBox("Start minimised to the tray")
        self.chk_start_min.toggled.connect(self._save_app)
        layout.addWidget(self.chk_start_min)

        self.chk_notify = QCheckBox("Show a tray notification when a popup is auto-closed")
        self.chk_notify.toggled.connect(self._save_app)
        layout.addWidget(self.chk_notify)
        return group

    # -- state -------------------------------------------------------------

    def refresh(self) -> None:
        self._loading = True
        try:
            self.chk_monitor_only.setChecked(bool(self._config.get("monitor_only", True)))

            gaming = self._config.get("gaming_mode", {}) or {}
            self.chk_gaming.setChecked(bool(gaming.get("enabled")))
            self.txt_games.setText(", ".join(gaming.get("games", [])))

            detect = self._config.get("detect", {}) or {}
            self.chk_all_windows.setChecked(bool(detect.get("record_all_windows", False)))
            self.chk_focus_steals.setChecked(bool(detect.get("record_focus_steals", True)))

            self.chk_tray_on_close.setChecked(bool(self._config.get("minimise_to_tray_on_close", True)))
            self.chk_start_min.setChecked(bool(self._config.get("start_minimised", False)))
            self.chk_notify.setChecked(bool(self._config.get("notify_on_block", True)))
            self.chk_autostart.setChecked(actions.get_autostart())

            nag = actions.get_update_restart_notifications()
            self.chk_update_nag.setChecked(True if nag is None else nag)
        finally:
            self._loading = False

        self.refresh_status()

    def refresh_status(self) -> None:
        rules = self._config.rules()
        closing = sum(1 for rule in rules.values() if rule.get("action") == "close")
        muted = sum(1 for rule in rules.values() if rule.get("action") == "mute")
        self.lbl_rules.setText(
            f"{closing} sources set to auto-close, {muted} muted. "
            + (
                "Auto-close is currently held back by Monitor only."
                if self._config.get("monitor_only", True) and closing
                else ""
            )
        )

        games = self._monitor.games.running_games
        self.lbl_games_running.setText(
            f"Running now: {', '.join(games)}" if games else "No configured game is running."
        )

        toasts_on = actions.notifications_globally_enabled()
        self.lbl_toasts_global.setText(
            "Windows notifications are on, so toasts will be recorded."
            if toasts_on
            else "Windows notifications are switched off system-wide, so no app can raise a "
            "toast and none will appear in the Live tab. Dialog and window popups are still "
            "detected normally."
        )
        self.lbl_toasts_global.setObjectName("SubtleLabel" if toasts_on else "StatusWarn")
        self.lbl_toasts_global.style().unpolish(self.lbl_toasts_global)
        self.lbl_toasts_global.style().polish(self.lbl_toasts_global)

        log_on = self._monitor.correlator.log_enabled
        reason = (self._monitor.correlator.log_status or "").strip()
        if reason.lower() in ("", "unknown"):
            reason = "Popups will be matched to tasks by timing only."
        self.lbl_tasklog.setText(
            "Tracing is on - popups can be traced to the exact task."
            if log_on
            else f"Tracing is off. {reason}"
        )
        self.lbl_tasklog.setObjectName("StatusOk" if log_on else "StatusWarn")
        self.btn_tasklog.setEnabled(not log_on)

        self.lbl_elevation.setText(
            "Running as administrator." if self._elevated
            else "Running without administrator rights - scheduled tasks and tracing will fail."
        )
        self.lbl_elevation.setObjectName("StatusOk" if self._elevated else "StatusWarn")
        self.btn_elevate.setVisible(not self._elevated)
        # Re-apply the stylesheet so the object-name colours take effect.
        for label in (self.lbl_tasklog, self.lbl_elevation):
            label.style().unpolish(label)
            label.style().polish(label)

    # -- handlers ----------------------------------------------------------

    def _on_monitor_only(self, checked: bool) -> None:
        if self._loading:
            return
        self._config.set("monitor_only", checked)
        self.monitor_only_changed.emit(checked)
        self.action_taken.emit(
            "Monitor only is on. Nothing will be closed."
            if checked
            else "Monitor only is off. Your auto-close rules are now live."
        )
        self.refresh_status()

    def _save_gaming(self) -> None:
        if self._loading:
            return
        games = [part.strip() for part in self.txt_games.text().split(",") if part.strip()]
        self._config.update(
            {"gaming_mode": {"enabled": self.chk_gaming.isChecked(), "games": games}}
        )
        self._monitor.games.refresh()
        self.action_taken.emit(f"Gaming mode saved with {len(games)} game(s).")
        self.refresh_status()

    def _save_detect(self) -> None:
        if self._loading:
            return
        self._config.update(
            {
                "detect": {
                    "record_all_windows": self.chk_all_windows.isChecked(),
                    "record_focus_steals": self.chk_focus_steals.isChecked(),
                }
            }
        )
        self.action_taken.emit("Recording preferences saved.")

    def _save_app(self) -> None:
        if self._loading:
            return
        self._config.update(
            {
                "minimise_to_tray_on_close": self.chk_tray_on_close.isChecked(),
                "start_minimised": self.chk_start_min.isChecked(),
                "notify_on_block": self.chk_notify.isChecked(),
            }
        )

    def _on_update_nag(self, checked: bool) -> None:
        if self._loading:
            return
        result = actions.set_update_restart_notifications(checked)
        self.action_taken.emit(str(result.get("message")))
        if not result.get("ok"):
            QMessageBox.warning(self, "Could not change the setting", str(result.get("message")))

    def _on_autostart(self, checked: bool) -> None:
        if self._loading:
            return
        result = actions.set_autostart(checked)
        self.action_taken.emit(str(result.get("message")))
        if not result.get("ok"):
            QMessageBox.warning(self, "Could not change startup", str(result.get("message")))
            self._loading = True
            self.chk_autostart.setChecked(not checked)
            self._loading = False

    def _enable_task_log(self) -> None:
        ok, message = tasklib.enable_task_log()
        self._monitor.correlator.log_enabled = ok
        self._monitor.correlator.log_status = message
        if ok:
            self._monitor.correlator.refresh()
            self.action_taken.emit("Task Scheduler tracing is on.")
        else:
            QMessageBox.warning(self, "Could not turn on tracing", message)
        self.refresh_status()

    def _restart_elevated(self) -> None:
        if self._on_restart_elevated:
            self._on_restart_elevated()
