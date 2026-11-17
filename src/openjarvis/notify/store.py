"""SQLite persistence for alerts, so state survives restarts."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# Alert statuses
DIGEST = "digest"  # waiting for the daily digest
HELD = "held"  # deferred (quiet hours / rate limit), sent later
SENT = "sent"  # delivered by text and/or call, awaiting acknowledgement
ACKED = "acked"  # user acknowledged
DONE = "done"  # delivered, nothing further expected
FAILED = "failed"

URGENCIES = ("low", "normal", "high", "critical")


@dataclass
class Alert:
    id: int
    created_at: float
    urgency: str
    title: str
    message: str
    dedupe_key: str
    status: str
    sms_sent: int
    calls: int
    last_call_at: float
    acked_at: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    urgency TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    dedupe_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    sms_sent INTEGER NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0,
    last_call_at REAL NOT NULL DEFAULT 0,
    acked_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS alerts_status ON alerts(status);
CREATE INDEX IF NOT EXISTS alerts_key ON alerts(dedupe_key);
CREATE TABLE IF NOT EXISTS contacts (
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    critical INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    due_at REAL NOT NULL,
    text TEXT NOT NULL,
    daily INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status, due_at);
"""


@dataclass
class Reminder:
    id: int
    created_at: float
    due_at: float
    text: str
    daily: int
    status: str


_RCOLS = "id, created_at, due_at, text, daily, status"

_COLS = (
    "id, created_at, urgency, title, message, dedupe_key, status, "
    "sms_sent, calls, last_call_at, acked_at"
)


class AlertStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        if path is None:
            from openjarvis.core.paths import get_config_dir

            path = get_config_dir() / "notify.db"
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # -- alerts -----------------------------------------------------------

    def add(
        self,
        *,
        created_at: float,
        urgency: str,
        title: str,
        message: str,
        dedupe_key: str,
        status: str,
    ) -> Alert:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO alerts (created_at, urgency, title, message, "
                "dedupe_key, status) VALUES (?, ?, ?, ?, ?, ?)",
                (created_at, urgency, title, message, dedupe_key, status),
            )
            self._db.commit()
            alert_id = cur.lastrowid
        alert = self.get(int(alert_id))
        assert alert is not None
        return alert

    def get(self, alert_id: int) -> Optional[Alert]:
        with self._lock:
            row = self._db.execute(
                f"SELECT {_COLS} FROM alerts WHERE id = ?", (alert_id,)
            ).fetchone()
        return Alert(*row) if row else None

    def update(self, alert_id: int, **fields: object) -> None:
        if not fields:
            return
        allowed = {"status", "sms_sent", "calls", "last_call_at", "acked_at"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update {bad}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._db.execute(
                f"UPDATE alerts SET {sets} WHERE id = ?", (*fields.values(), alert_id)
            )
            self._db.commit()

    def by_status(self, *statuses: str) -> List[Alert]:
        marks = ",".join("?" * len(statuses))
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLS} FROM alerts WHERE status IN ({marks}) ORDER BY id",
                statuses,
            ).fetchall()
        return [Alert(*r) for r in rows]

    def recent(self, limit: int = 20) -> List[Alert]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLS} FROM alerts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Alert(*r) for r in rows]

    def recent_with_key(self, dedupe_key: str, since: float) -> Optional[Alert]:
        with self._lock:
            row = self._db.execute(
                f"SELECT {_COLS} FROM alerts WHERE dedupe_key = ? AND created_at >= ? "
                "AND status != ? ORDER BY id DESC LIMIT 1",
                (dedupe_key, since, FAILED),
            ).fetchone()
        return Alert(*row) if row else None

    # -- contact log (rate limiting) --------------------------------------

    def log_contact(self, at: float, kind: str, critical: bool) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO contacts (at, kind, critical) VALUES (?, ?, ?)",
                (at, kind, int(critical)),
            )
            self._db.execute("DELETE FROM contacts WHERE at < ?", (at - 86400,))
            self._db.commit()

    def contacts_since(self, since: float, *, include_critical: bool = False) -> int:
        q = "SELECT COUNT(*) FROM contacts WHERE at >= ?"
        if not include_critical:
            q += " AND critical = 0"
        with self._lock:
            return int(self._db.execute(q, (since,)).fetchone()[0])

    # -- reminders --------------------------------------------------------

    def add_reminder(
        self, *, created_at: float, due_at: float, text: str, daily: bool
    ) -> Reminder:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO reminders (created_at, due_at, text, daily) "
                "VALUES (?, ?, ?, ?)",
                (created_at, due_at, text, int(daily)),
            )
            self._db.commit()
            rid = int(cur.lastrowid)
        reminder = self.get_reminder(rid)
        assert reminder is not None
        return reminder

    def get_reminder(self, rid: int) -> Optional[Reminder]:
        with self._lock:
            row = self._db.execute(
                f"SELECT {_RCOLS} FROM reminders WHERE id = ?", (rid,)
            ).fetchone()
        return Reminder(*row) if row else None

    def pending_reminders(self, due_before: Optional[float] = None) -> List[Reminder]:
        q = f"SELECT {_RCOLS} FROM reminders WHERE status = 'pending'"
        args: tuple = ()
        if due_before is not None:
            q += " AND due_at <= ?"
            args = (due_before,)
        with self._lock:
            rows = self._db.execute(q + " ORDER BY due_at, id", args).fetchall()
        return [Reminder(*r) for r in rows]

    def update_reminder(self, rid: int, **fields: object) -> None:
        allowed = {"due_at", "status"}
        if set(fields) - allowed:
            raise ValueError(f"cannot update {set(fields) - allowed}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._db.execute(
                f"UPDATE reminders SET {sets} WHERE id = ?", (*fields.values(), rid)
            )
            self._db.commit()

    # -- meta -------------------------------------------------------------

    def get_meta(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (key, value),
            )
            self._db.commit()
