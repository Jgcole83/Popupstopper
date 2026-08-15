"""Main window: the stats strip, the tabs and the status bar."""

from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWidgets import QMainWindow

from .. import actions
from ..config import ACTION_CLOSE, ACTION_MUTE, APP_TITLE, Config
from ..monitor import Monitor
from ..store import Store
from .history_panel import HistoryPanel
from .live_panel import LivePanel
from .prevent_dialog import PreventDialog
from .prevented_panel import PreventedPanel
from .settings_panel import SettingsPanel
from .sources_panel import SourcesPanel
from .tasks_panel import TasksPanel

log = logging.getLogger(__name__)


class StatCard(QFrame):
    def __init__(self, caption: str) -> None:
        super().__init__()
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(0)
        self.value = QLabel("0")
        self.value.setObjectName("StatValue")
        label = QLabel(caption)
        label.setObjectName("StatLabel")
        layout.addWidget(self.value)
        layout.addWidget(label)

    def set_value(self, value: Any) -> None:
        self.value.setText(str(value))


class MainWindow(QMainWindow):
    def __init__(
        self,
        icon: QIcon,
        config: Config,
        store: Store,
        monitor: Monitor,
        elevated: bool,
        on_restart_elevated: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._store = store
        self._monitor = monitor

        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(icon)
        self.resize(1120, 780)

        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 0)
        root.setSpacing(10)

        # Stats strip
        stats_row = QHBoxLayout()
        stats_row.setSpacing(10)
        self.stat_total = StatCard("popups recorded")
        self.stat_today = StatCard("in the last 24 hours")
        self.stat_sources = StatCard("distinct sources")
        self.stat_blocked = StatCard("auto-closed")
        for card in (self.stat_total, self.stat_today, self.stat_sources, self.stat_blocked):
            stats_row.addWidget(card)
        stats_row.addStretch(1)
        root.addLayout(stats_row)

        # Tabs
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        root.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self.live = LivePanel()
        self.sources = SourcesPanel(config, store)
        self.history = HistoryPanel(store)
        self.prevented = PreventedPanel(config)
        self.tasks = TasksPanel(config)
        self.settings = SettingsPanel(
            config=config,
            monitor=monitor,
            elevated=elevated,
            on_restart_elevated=on_restart_elevated,
        )

        self.tabs.addTab(self.live, "Live")
        self.tabs.addTab(self.sources, "Sources")
        self.tabs.addTab(self.history, "History")
        self.tabs.addTab(self.prevented, "Prevented")
        self.tabs.addTab(self.tasks, "Scheduled tasks")
        self.tabs.addTab(self.settings, "Settings")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # Status bar
        status = QStatusBar()
        self.setStatusBar(status)
        self.status_label = QLabel("Starting up...")
        status.addWidget(self.status_label, 1)
        self.mode_label = QLabel("")
        status.addPermanentWidget(self.mode_label)

        self._wire_details(self.live)
        self._wire_details(self.history)
        self._wire_details(self.sources)
        for panel in (self.sources, self.history, self.tasks, self.settings, self.prevented):
            panel.action_taken.connect(self.show_message)
        self.settings.monitor_only_changed.connect(lambda _v: self.refresh_mode())

        self.refresh_stats()
        self.refresh_mode()

        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(5000)
        self._stats_timer.timeout.connect(self.refresh_stats)
        self._stats_timer.start()

    # -- wiring ------------------------------------------------------------

    def _wire_details(self, panel: Any) -> None:
        details = panel.details
        details.block_requested.connect(self._block_source)
        details.allow_requested.connect(self._allow_source)
        details.mute_requested.connect(self._mute_source)
        details.open_path_requested.connect(self._open_path)
        details.disable_task_requested.connect(self.sources.confirm_disable_task)
        details.prevent_requested.connect(self._prevent_source)

    def _prevent_source(self, record: dict[str, Any]) -> None:
        dialog = PreventDialog(record, self._config, self)
        if dialog.exec() == PreventDialog.DialogCode.Accepted and dialog.applied_messages:
            self.prevented.reload()
            self.tabs.setCurrentWidget(self.prevented)
            self.show_message(" ".join(dialog.applied_messages))

    def _block_source(self, source_key: str, display_name: str, kind: str) -> None:
        message = self.sources.apply_rule(source_key, display_name, kind, ACTION_CLOSE)
        if self._config.get("monitor_only", True):
            message += " It will take effect once Monitor only is turned off in Settings."
        self.show_message(message)
        self.settings.refresh_status()

    def _allow_source(self, source_key: str) -> None:
        self.show_message(self.sources.apply_rule(source_key, "", "window", "log"))
        self.settings.refresh_status()

    def _mute_source(self, aumid: str, display_name: str) -> None:
        self.sources.apply_rule(f"toast:{aumid.lower()}", display_name, "toast", ACTION_MUTE)
        result = actions.set_toast_enabled(aumid, False)
        self.show_message(
            f"{display_name or aumid}: {result.get('message')}"
        )

    def _open_path(self, path: str) -> None:
        result = actions.open_in_explorer(path)
        if not result.get("ok"):
            self.show_message(str(result.get("message")))

    # -- updates -----------------------------------------------------------

    def on_event(self, record: dict[str, Any]) -> None:
        self.live.add_event(record)

    def refresh_stats(self) -> None:
        stats = self._store.stats()
        self.stat_total.set_value(stats["total"])
        self.stat_today.set_value(stats["last_24h"])
        self.stat_sources.set_value(stats["sources"])
        self.stat_blocked.set_value(stats["blocked"])

    def refresh_mode(self) -> None:
        monitor_only = bool(self._config.get("monitor_only", True))
        games = self._monitor.games.running_games
        pieces = ["Monitor only" if monitor_only else "Blocking active"]
        if games:
            pieces.append(f"gaming: {', '.join(games)}")
        if not self._monitor.correlator.log_enabled:
            pieces.append("task tracing off")
        if not actions.notifications_globally_enabled():
            pieces.append("Windows notifications off")
        self.mode_label.setText("   |   ".join(pieces))
        self.mode_label.setObjectName("StatusWarn" if monitor_only else "StatusOk")
        self.mode_label.style().unpolish(self.mode_label)
        self.mode_label.style().polish(self.mode_label)

    def show_message(self, message: str) -> None:
        if message:
            self.status_label.setText(message)

    def _on_tab_changed(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if widget is self.sources:
            self.sources.reload()
        elif widget is self.history:
            self.history.reload()
        elif widget is self.prevented:
            self.prevented.reload()
        elif widget is self.settings:
            self.settings.refresh()
        self.refresh_mode()

    # -- window behaviour --------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._config.get("minimise_to_tray_on_close", True):
            event.ignore()
            self.hide()
        else:
            event.accept()

    def show_and_raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.setWindowState(
            (self.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive
        )
