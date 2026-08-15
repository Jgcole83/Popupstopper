"""QApplication, system tray and wiring for Popup Stopper."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import logger as applog
from .config import APP_NAME, APP_TITLE, ASSETS_DIR, DATA_DIR, Config, ensure_dirs
from .monitor import Monitor
from .store import Store
from .ui.main_window import MainWindow
from .util.admin import is_admin, relaunch_elevated
from .util.single_instance import SingleInstance

log = logging.getLogger(__name__)


def _load_icon() -> QIcon:
    for name in ("icon.ico", "icon.svg"):
        path = ASSETS_DIR / name
        if path.exists():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                return QIcon(pixmap)
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.blue)
    return QIcon(pixmap)


def _load_stylesheet() -> str:
    path = Path(__file__).parent / "ui" / "style.qss"
    if not path.exists():
        return ""
    # Qt stylesheets need forward slashes in url(), and the assets folder moves
    # with the install, so the path is substituted rather than hard-coded.
    assets_url = ASSETS_DIR.as_posix()
    return path.read_text(encoding="utf-8").replace("@ASSETS@", assets_url)


class EventBridge(QObject):
    """Carries events from the monitor's worker threads onto the GUI thread.

    The monitor calls `emit_event` from a background thread. Because this
    object lives on the main thread, Qt automatically queues the signal and
    delivers it inside the event loop, which is the only safe place to touch
    widgets from.
    """

    event_recorded = Signal(dict)

    def emit_event(self, record: dict[str, Any]) -> None:
        self.event_recorded.emit(record)


class PopupStopperApp(QObject):
    def __init__(self, argv: list[str]) -> None:
        applog.setup_logging()
        ensure_dirs()
        self.elevated = is_admin()
        log.info("=" * 60)
        log.info("%s starting (elevated=%s)", APP_NAME, self.elevated)

        self.qapp = QApplication(argv)
        super().__init__(self.qapp)
        self.qapp.setApplicationName(APP_NAME)
        self.qapp.setApplicationDisplayName(APP_TITLE)
        self.qapp.setQuitOnLastWindowClosed(False)
        self.qapp.setStyleSheet(_load_stylesheet())

        self.primary = True
        self._guard = SingleInstance(f"{APP_NAME}-single-instance")
        if self._guard.already_running():
            log.info("Another instance is already running; asking it to show itself")
            self._guard.send_show()
            self.primary = False
            return

        self.config = Config()
        self.store = Store()

        self.bridge = EventBridge()
        self.monitor = Monitor(self.config, self.store, on_event=self.bridge.emit_event)

        self.icon = _load_icon()
        self.window = MainWindow(
            icon=self.icon,
            config=self.config,
            store=self.store,
            monitor=self.monitor,
            elevated=self.elevated,
            on_restart_elevated=self._restart_elevated,
        )
        self.bridge.event_recorded.connect(self.window.on_event)
        self.bridge.event_recorded.connect(self._maybe_notify)

        self.tray = QSystemTrayIcon(self.icon)
        self.tray.setToolTip(f"{APP_TITLE} - watching for popups")
        self.tray.setContextMenu(self._build_tray_menu())
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        self.monitor.start()
        self.window.settings.refresh_status()
        self._guard.start_server(on_show=self._show_window)

        if not self.elevated:
            self.window.show_message(
                "Running without administrator rights. Scheduled task controls and task "
                "tracing will not work until you restart as administrator."
            )

        start_minimised = bool(self.config.get("start_minimised", False)) or "--minimized" in argv
        if start_minimised:
            log.info("Starting minimised to the tray")
            self.tray.showMessage(
                APP_TITLE,
                "Watching for popups in the background.",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
        else:
            self.window.show()

    # -- tray --------------------------------------------------------------

    def _build_tray_menu(self) -> QMenu:
        menu = QMenu()

        show_action = QAction("Open Popup Stopper", menu)
        show_action.triggered.connect(self._show_window)
        menu.addAction(show_action)
        menu.addSeparator()

        self.action_monitor_only = QAction("Monitor only (never close)", menu)
        self.action_monitor_only.setCheckable(True)
        self.action_monitor_only.setChecked(bool(self.config.get("monitor_only", True)))
        self.action_monitor_only.toggled.connect(self._toggle_monitor_only)
        menu.addAction(self.action_monitor_only)

        self.action_pause = QAction("Pause detection", menu)
        self.action_pause.setCheckable(True)
        self.action_pause.toggled.connect(self._toggle_pause)
        menu.addAction(self.action_pause)
        menu.addSeparator()

        data_action = QAction("Open data folder", menu)
        data_action.triggered.connect(lambda: subprocess.Popen(["explorer", str(DATA_DIR)]))
        menu.addAction(data_action)

        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)
        return menu

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._show_window()

    def _show_window(self) -> None:
        self.window.show_and_raise()

    def _toggle_monitor_only(self, checked: bool) -> None:
        self.config.set("monitor_only", checked)
        self.window.settings.refresh()
        self.window.refresh_mode()
        self.tray.setToolTip(
            f"{APP_TITLE} - {'monitoring only' if checked else 'blocking active'}"
        )

    def _toggle_pause(self, checked: bool) -> None:
        self.monitor.paused = checked
        self.window.show_message(
            "Detection paused." if checked else "Detection resumed."
        )

    def _maybe_notify(self, record: dict[str, Any]) -> None:
        if record.get("action") != "closed":
            return
        if not self.config.get("notify_on_block", True):
            return
        if self.window.isVisible():
            return
        self.tray.showMessage(
            "Popup blocked",
            f"{record.get('display_name') or 'A program'}: {record.get('title') or 'popup closed'}",
            QSystemTrayIcon.MessageIcon.Information,
            4000,
        )

    # -- lifecycle ---------------------------------------------------------

    def _restart_elevated(self) -> None:
        answer = QMessageBox.question(
            self.window,
            "Restart as administrator",
            "Popup Stopper will close and reopen with administrator rights.\n\n"
            "Windows will ask you to confirm.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if relaunch_elevated([]):
            self._quit()
        else:
            QMessageBox.warning(
                self.window,
                "Not elevated",
                "The elevation request was declined, so Popup Stopper is still "
                "running with normal rights.",
            )

    def _quit(self) -> None:
        log.info("Shutting down")
        try:
            self.monitor.stop()
            self.store.close()
        except Exception:  # noqa: BLE001
            log.exception("Error while shutting down")
        self.tray.hide()
        self.qapp.quit()

    def run(self) -> int:
        if not self.primary:
            return 0
        return self.qapp.exec()


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv)

    # Popup Stopper needs administrator rights for the Task Scheduler trace log
    # and for enabling or disabling tasks, so ask for them up front. The flag
    # guards against a loop if the elevated process somehow still is not admin.
    if os.name == "nt" and not is_admin() and "--no-elevate" not in argv:
        if relaunch_elevated(["--no-elevate"] + [a for a in argv[1:] if a != "--no-elevate"]):
            return 0

    app = PopupStopperApp(argv)
    return app.run()
