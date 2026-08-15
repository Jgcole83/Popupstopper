"""SQLite-backed history of every popup Popup Stopper has seen."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .config import EVENTS_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    kind          TEXT    NOT NULL,
    source_key    TEXT    NOT NULL,
    display_name  TEXT,
    title         TEXT,
    body          TEXT,
    exe_path      TEXT,
    exe_name      TEXT,
    pid           INTEGER,
    cmdline       TEXT,
    publisher     TEXT,
    parents       TEXT,
    aumid         TEXT,
    window_class  TEXT,
    category      TEXT,
    task_name     TEXT,
    task_exe      TEXT,
    action        TEXT,
    details       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_source ON events (source_key);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events (kind);
"""

_COLUMNS = (
    "ts", "kind", "source_key", "display_name", "title", "body", "exe_path",
    "exe_name", "pid", "cmdline", "publisher", "parents", "aumid",
    "window_class", "category", "task_name", "task_exe", "action", "details",
)

_JSON_COLUMNS = ("parents", "details")


class Store:
    def __init__(self, path: Path = EVENTS_DB_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes ------------------------------------------------------------

    def add_event(self, event: dict[str, Any]) -> dict[str, Any]:
        row = {key: event.get(key) for key in _COLUMNS}
        row["ts"] = row["ts"] or time.time()
        row["kind"] = row["kind"] or "window"
        row["source_key"] = row["source_key"] or "unknown"
        for key in _JSON_COLUMNS:
            value = row.get(key)
            if value is not None and not isinstance(value, str):
                row[key] = json.dumps(value)

        placeholders = ", ".join("?" for _ in _COLUMNS)
        sql = f"INSERT INTO events ({', '.join(_COLUMNS)}) VALUES ({placeholders})"
        with self._lock:
            cursor = self._conn.execute(sql, [row[key] for key in _COLUMNS])
            self._conn.commit()
            event_id = cursor.lastrowid
        return self.get_event(int(event_id))  # type: ignore[arg-type]

    def set_action(self, event_id: int, action: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE events SET action = ? WHERE id = ?", (action, event_id))
            self._conn.commit()

    def clear(self) -> int:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM events")
            self._conn.commit()
            return cursor.rowcount

    def prune(self, keep_days: int = 30) -> int:
        cutoff = time.time() - keep_days * 86400
        with self._lock:
            cursor = self._conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cursor.rowcount

    # -- reads -------------------------------------------------------------

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        for key in _JSON_COLUMNS:
            raw = out.get(key)
            if isinstance(raw, str) and raw:
                try:
                    out[key] = json.loads(raw)
                except json.JSONDecodeError:
                    pass
        return out

    def get_event(self, event_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._to_dict(row) if row else {}

    def list_events(
        self,
        limit: int = 200,
        offset: int = 0,
        query: str | None = None,
        kind: str | None = None,
        source_key: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if query:
            like = f"%{query}%"
            clauses.append(
                "(title LIKE ? OR body LIKE ? OR exe_path LIKE ? OR display_name LIKE ?"
                " OR task_name LIKE ? OR aumid LIKE ?)"
            )
            params.extend([like] * 6)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if source_key:
            clauses.append("source_key = ?")
            params.append(source_key)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM events {where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._to_dict(row) for row in rows]

    def sources(self) -> list[dict[str, Any]]:
        """One row per distinct popup source, with counts and the latest details."""
        sql = """
        SELECT
            source_key,
            COUNT(*)      AS count,
            MAX(ts)       AS last_seen,
            MIN(ts)       AS first_seen,
            SUM(CASE WHEN action = 'closed' THEN 1 ELSE 0 END) AS blocked_count
        FROM events
        GROUP BY source_key
        ORDER BY last_seen DESC
        """
        with self._lock:
            groups = self._conn.execute(sql).fetchall()
            out: list[dict[str, Any]] = []
            for group in groups:
                latest = self._conn.execute(
                    "SELECT * FROM events WHERE source_key = ? ORDER BY ts DESC, id DESC LIMIT 1",
                    (group["source_key"],),
                ).fetchone()
                if latest is None:
                    continue
                info = self._to_dict(latest)
                out.append(
                    {
                        "source_key": group["source_key"],
                        "count": group["count"],
                        "blocked_count": group["blocked_count"] or 0,
                        "first_seen": group["first_seen"],
                        "last_seen": group["last_seen"],
                        "display_name": info.get("display_name"),
                        "kind": info.get("kind"),
                        "exe_path": info.get("exe_path"),
                        "exe_name": info.get("exe_name"),
                        "publisher": info.get("publisher"),
                        "aumid": info.get("aumid"),
                        "category": info.get("category"),
                        "task_name": info.get("task_name"),
                        "task_exe": info.get("task_exe"),
                        "last_title": info.get("title"),
                        "last_body": info.get("body"),
                    }
                )
        return out

    def stats(self) -> dict[str, Any]:
        day_ago = time.time() - 86400
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            today = self._conn.execute(
                "SELECT COUNT(*) FROM events WHERE ts > ?", (day_ago,)
            ).fetchone()[0]
            blocked = self._conn.execute(
                "SELECT COUNT(*) FROM events WHERE action = 'closed'"
            ).fetchone()[0]
            sources = self._conn.execute(
                "SELECT COUNT(DISTINCT source_key) FROM events"
            ).fetchone()[0]
        return {
            "total": total,
            "last_24h": today,
            "blocked": blocked,
            "sources": sources,
        }
