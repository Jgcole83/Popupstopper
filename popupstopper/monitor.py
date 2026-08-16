"""The engine: connects the detectors, attribution, rules and storage.

Detection callbacks must return quickly, otherwise a slow lookup would delay
the decision to close an unwanted window. So the hot path does only what it
needs to act (resolve the executable, evaluate rules, close if asked) and
hands the event to a worker thread for the expensive attribution work.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

from . import actions, attribute
from .config import ACTION_CLOSE, Config
from .rules import GameDetector, RuleEngine
from .store import Store
from .tasks import TaskCorrelator
from .toasts import ToastEvent, ToastWatcher, enrich_toast
from .winhook import WindowEvent, WindowWatcher

log = logging.getLogger(__name__)


class Monitor:
    def __init__(
        self,
        config: Config,
        store: Store,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self._on_event = on_event

        self.games = GameDetector(config)
        self.rules = RuleEngine(config, self.games)
        self.correlator = TaskCorrelator(
            poll_seconds=float(config.get("detect", {}).get("task_poll_seconds", 15.0))
        )
        self.windows = WindowWatcher(self._on_window)
        self.toasts = ToastWatcher(
            self._on_toast,
            poll_seconds=float(config.get("detect", {}).get("toast_poll_seconds", 3.0)),
            last_tick=int(config.get("seen_toast_tick", 0) or 0),
            on_tick_advance=lambda tick: config.set("seen_toast_tick", tick),
        )

        self._queue: queue.Queue[tuple[str, Any, dict[str, Any]]] = queue.Queue(maxsize=500)
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self.started_at = 0.0
        self.paused = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.started_at = time.time()
        self._stop.clear()
        self._worker = threading.Thread(target=self._process_queue, name="enrichment", daemon=True)
        self._worker.start()

        self.games.start()
        self.correlator.start(auto_enable_log=True)
        detect = self.config.get("detect", {}) or {}
        if detect.get("windows", True):
            self.windows.start()
        if detect.get("toasts", True):
            self.toasts.start()
        log.info("Monitor started")

    def stop(self) -> None:
        self._stop.set()
        self.windows.stop()
        self.toasts.stop()
        self.correlator.stop()
        self.games.stop()
        if self._worker:
            self._worker.join(timeout=3)
        log.info("Monitor stopped")

    # -- detection callbacks (hot path) -----------------------------------

    def _on_window(self, event: WindowEvent) -> None:
        if self.paused or not self._should_record(event):
            return

        source_key = attribute.source_key_for_window(
            event.exe_path, event.window_class, event.exe_name
        )
        decision = self.rules.decide(source_key, event.exe_name, event.title, kind="window")

        close_result: dict[str, Any] | None = None
        if decision.action == ACTION_CLOSE:
            close_result = actions.close_window(event.hwnd, event.exe_name)
            log.info(
                "Auto-closed %r from %s: %s",
                event.title,
                event.exe_name,
                close_result.get("message"),
            )

        self._enqueue(
            "window",
            event,
            {
                "decision": decision.action,
                "reason": decision.reason,
                "would_close": decision.would_close,
                "close_result": close_result,
            },
        )

    def _on_toast(self, event: ToastEvent) -> None:
        if self.paused:
            return
        source_key = attribute.source_key_for_toast(event.aumid)
        decision = self.rules.decide(source_key, "", event.title, kind="toast")
        self._enqueue(
            "toast",
            event,
            {"decision": decision.action, "reason": decision.reason, "would_close": False},
        )

    def _should_record(self, event: WindowEvent) -> bool:
        detect = self.config.get("detect", {}) or {}
        if detect.get("record_all_windows", False):
            return True
        if event.is_popup_like:
            return True
        # While gaming, anything that grabs focus is worth knowing about even
        # if it is an ordinary window.
        if (
            detect.get("record_focus_steals", True)
            and event.event_name == "foreground"
            and self.games.game_active
        ):
            return True
        return False

    def _enqueue(self, kind: str, event: Any, context: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait((kind, event, context))
        except queue.Full:
            log.warning("Enrichment queue is full, dropping a %s event", kind)

    # -- enrichment worker -------------------------------------------------

    def _process_queue(self) -> None:
        while not self._stop.is_set():
            try:
                kind, event, context = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                record = self._build_record(kind, event, context)
                stored = self.store.add_event(record)
                if self._on_event:
                    self._on_event(stored)
            except Exception:  # noqa: BLE001 - one bad event must not stop the worker
                log.exception("Failed to record a %s event", kind)
            finally:
                self._queue.task_done()

    def _build_record(self, kind: str, event: Any, context: dict[str, Any]) -> dict[str, Any]:
        if kind == "toast":
            record = enrich_toast(event)
        else:
            record = attribute.enrich_window(event)
            chain = record.get("parents", [])
            pids = [event.pid] + [parent.get("pid", 0) for parent in chain]
            match = self.correlator.attribute(event.exe_name, pids, event.ts, chain=chain)
            if match:
                record["task_name"] = match.get("task_name")
                record["task_exe"] = match.get("task_exe")
                record.setdefault("details", {})["task_confidence"] = match.get("confidence")
                record["category"] = attribute.categorize(
                    event.exe_name,
                    event.exe_path,
                    event.title,
                    task_name=match.get("task_name", ""),
                    chain=record.get("parents"),
                )

        close_result = context.get("close_result") or {}
        record["action"] = "closed" if close_result.get("ok") else "logged"
        details = record.setdefault("details", {})
        details["decision_reason"] = context.get("reason")
        details["would_close"] = context.get("would_close", False)
        if close_result and not close_result.get("ok"):
            details["close_error"] = close_result.get("message")
        details["game_active"] = self.games.game_active
        return record

    # -- status ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        from .winapi import is_admin

        return {
            "running": self.windows.is_running or self.toasts.is_running,
            "paused": self.paused,
            "started_at": self.started_at,
            "uptime_seconds": time.time() - self.started_at if self.started_at else 0,
            "is_admin": is_admin(),
            "window_watcher": self.windows.is_running,
            "toast_watcher": self.toasts.is_running,
            "toast_db_available": self.toasts.available,
            "toast_error": self.toasts.last_error,
            "task_log_enabled": self.correlator.log_enabled,
            "task_log_status": self.correlator.log_status,
            "monitor_only": bool(self.config.get("monitor_only", True)),
            "gaming_mode": self.config.get("gaming_mode", {}),
            "running_games": self.games.running_games,
            "game_active": self.games.game_active,
            "queue_depth": self._queue.qsize(),
        }
