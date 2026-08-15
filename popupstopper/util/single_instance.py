"""Single-instance guard built on a Qt local socket.

A second launch (double-clicking the desktop icon again, say) should raise the
window that is already running rather than start a second monitor, which would
double-record every popup.
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

_PING_TIMEOUT_MS = 500


class SingleInstance(QObject):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self._server: QLocalServer | None = None
        self._on_show: Callable[[], None] | None = None
        self._already_running = self._probe()

    def _probe(self) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        if socket.waitForConnected(_PING_TIMEOUT_MS):
            socket.disconnectFromServer()
            return True
        return False

    def already_running(self) -> bool:
        return self._already_running

    def send_show(self) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        if not socket.waitForConnected(_PING_TIMEOUT_MS):
            return False
        socket.write(b"SHOW\n")
        socket.flush()
        socket.waitForBytesWritten(_PING_TIMEOUT_MS)
        socket.disconnectFromServer()
        return True

    def start_server(self, on_show: Callable[[], None] | None = None) -> bool:
        self._on_show = on_show
        server = QLocalServer(self)
        QLocalServer.removeServer(self.name)
        if not server.listen(self.name):
            log.warning("Single-instance server failed: %s", server.errorString())
            return False
        server.newConnection.connect(self._handle_connection)
        self._server = server
        return True

    def _handle_connection(self) -> None:
        if self._server is None:
            return
        while True:
            socket = self._server.nextPendingConnection()
            if socket is None:
                break
            socket.readyRead.connect(lambda s=socket: self._read(s))
            socket.disconnected.connect(socket.deleteLater)

    def _read(self, socket: QLocalSocket) -> None:
        data = bytes(socket.readAll()).decode(errors="ignore")
        for line in data.splitlines():
            if line.strip().upper() == "SHOW" and self._on_show:
                QTimer.singleShot(0, self._on_show)
