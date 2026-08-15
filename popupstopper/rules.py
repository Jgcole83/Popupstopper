"""Decide what to do about a popup, and detect when a game is running.

The guiding principle is that nothing is ever closed by accident. A popup is
only auto-closed when the user has explicitly created a close rule for that
exact source, monitor-only mode is off, and the source is not on the
protected list. Everything else is recorded and left alone.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import psutil

from .config import ACTION_CLOSE, ACTION_LOG, ACTION_MUTE, Config, PROTECTED_PROCESSES

log = logging.getLogger(__name__)


@dataclass
class Decision:
    action: str  # what we will actually do: "log" or "close"
    reason: str
    would_close: bool = False  # a close rule matched but something held it back
    rule: dict[str, Any] | None = None


def title_matches(pattern: str | None, title: str) -> bool:
    """Empty pattern matches everything; "re:" prefix switches to regex."""
    if not pattern:
        return True
    title = title or ""
    if pattern.startswith("re:"):
        try:
            return bool(re.search(pattern[3:], title, re.IGNORECASE))
        except re.error:
            return False
    return pattern.lower() in title.lower()


class GameDetector:
    """Tracks whether any configured game executable is currently running."""

    def __init__(self, config: Config, poll_seconds: float = 5.0) -> None:
        self._config = config
        self._poll_seconds = poll_seconds
        self._running: list[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="game-detector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - keep polling
                log.exception("Game detection failed")
            self._stop.wait(self._poll_seconds)

    def refresh(self) -> list[str]:
        games = {
            str(name).lower().strip()
            for name in self._config.get("gaming_mode", {}).get("games", [])
            if str(name).strip()
        }
        found: list[str] = []
        if games:
            for proc in psutil.process_iter(["name"]):
                try:
                    name = (proc.info.get("name") or "").lower()
                except (psutil.Error, OSError):
                    continue
                if name in games and name not in found:
                    found.append(name)
        with self._lock:
            self._running = found
        return found

    @property
    def running_games(self) -> list[str]:
        with self._lock:
            return list(self._running)

    @property
    def game_active(self) -> bool:
        with self._lock:
            return bool(self._running)


class RuleEngine:
    def __init__(self, config: Config, games: GameDetector | None = None) -> None:
        self._config = config
        self._games = games

    # -- queries -----------------------------------------------------------

    def rule_for(self, source_key: str) -> dict[str, Any] | None:
        return self._config.get_rule(source_key)

    def is_protected(self, exe_name: str | None) -> bool:
        return (exe_name or "").lower() in PROTECTED_PROCESSES

    def gaming_gate_open(self) -> tuple[bool, str]:
        """Whether close rules may fire, given the gaming-mode setting."""
        gaming = self._config.get("gaming_mode", {}) or {}
        if not gaming.get("enabled"):
            return True, ""
        if self._games and self._games.game_active:
            return True, ""
        return False, "gaming mode on, no game running"

    # -- the decision ------------------------------------------------------

    def decide(
        self,
        source_key: str,
        exe_name: str | None,
        title: str,
        kind: str = "window",
    ) -> Decision:
        rule = self.rule_for(source_key)
        if rule is None or not rule.get("enabled", True):
            return Decision(ACTION_LOG, "no rule", rule=rule)

        action = rule.get("action", ACTION_LOG)
        if action == ACTION_MUTE:
            # Muting is enforced by Windows itself through the registry, so
            # there is nothing to do at popup time.
            return Decision(ACTION_LOG, "muted at source", rule=rule)
        if action != ACTION_CLOSE:
            return Decision(ACTION_LOG, "rule is monitor-only", rule=rule)

        if not title_matches(rule.get("title_pattern"), title):
            return Decision(ACTION_LOG, "title pattern did not match", rule=rule)

        if kind != "window":
            return Decision(ACTION_LOG, "only windows can be closed", rule=rule)

        if self.is_protected(exe_name):
            return Decision(
                ACTION_LOG, f"{exe_name} is protected and never auto-closed", rule=rule
            )

        if self._config.get("monitor_only", True):
            return Decision(
                ACTION_LOG, "monitor-only mode is on", would_close=True, rule=rule
            )

        gate_open, gate_reason = self.gaming_gate_open()
        if not gate_open:
            return Decision(ACTION_LOG, gate_reason, would_close=True, rule=rule)

        return Decision(ACTION_CLOSE, "close rule matched", would_close=True, rule=rule)


def default_rule(source_key: str, display_name: str, kind: str) -> dict[str, Any]:
    return {
        "source_key": source_key,
        "display_name": display_name,
        "kind": kind,
        "action": ACTION_LOG,
        "title_pattern": None,
        "enabled": True,
        "created": time.time(),
    }
