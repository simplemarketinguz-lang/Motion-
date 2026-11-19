"""SQLite-хранилище обещаний Simple Agent."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS promises (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id          INTEGER NOT NULL,
    chat_title       TEXT    NOT NULL,
    msg_id           INTEGER NOT NULL,
    link             TEXT,
    quote            TEXT    NOT NULL,
    task             TEXT    NOT NULL,
    counterpart      TEXT,
    due_at           TEXT,              -- ISO UTC или NULL, если срок не назван
    status           TEXT    NOT NULL DEFAULT 'open',   -- open | done | cancelled
    created_at       TEXT    NOT NULL,
    pre_reminded     INTEGER NOT NULL DEFAULT 0,
    last_reminded_at TEXT,
    snoozed_until    TEXT,
    UNIQUE (chat_id, msg_id, task)
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class Promise:
    id: int
    chat_id: int
    chat_title: str
    msg_id: int
    link: str | None
    quote: str
    task: str
    counterpart: str | None
    due_at: datetime | None
    status: str
    created_at: datetime
    pre_reminded: bool
    last_reminded_at: datetime | None
    snoozed_until: datetime | None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Promise":
        return cls(
            id=r["id"], chat_id=r["chat_id"], chat_title=r["chat_title"], msg_id=r["msg_id"],
            link=r["link"], quote=r["quote"], task=r["task"], counterpart=r["counterpart"],
            due_at=_dt(r["due_at"]), status=r["status"], created_at=_dt(r["created_at"]),
            pre_reminded=bool(r["pre_reminded"]), last_reminded_at=_dt(r["last_reminded_at"]),
            snoozed_until=_dt(r["snoozed_until"]),
        )


class Storage:
    def __init__(self, path: str | Path = "promises.db"):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------- key/value ----------
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.db.commit()

    # ---------- promises ----------
    def add(self, *, chat_id: int, chat_title: str, msg_id: int, link: str | None, quote: str,
            task: str, counterpart: str | None, due_at: datetime | None) -> Promise | None:
        cur = self.db.execute(
            """INSERT OR IGNORE INTO promises
               (chat_id, chat_title, msg_id, link, quote, task, counterpart, due_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, chat_title, msg_id, link, quote, task, counterpart, _iso(due_at), _iso(utcnow())),
        )
        self.db.commit()
        return self.by_id(cur.lastrowid) if cur.rowcount else None

    def by_id(self, pid: int) -> Promise | None:
        row = self.db.execute("SELECT * FROM promises WHERE id = ?", (pid,)).fetchone()
        return Promise.from_row(row) if row else None

    def open_promises(self, chat_id: int | None = None) -> list[Promise]:
        sql = "SELECT * FROM promises WHERE status = 'open'"
        args: tuple = ()
        if chat_id is not None:
            sql += " AND chat_id = ?"
            args = (chat_id,)
        sql += " ORDER BY due_at IS NULL, due_at, id"
        return [Promise.from_row(r) for r in self.db.execute(sql, args)]

    def set_status(self, pid: int, status: str) -> bool:
        cur = self.db.execute(
            "UPDATE promises SET status = ? WHERE id = ? AND status = 'open'", (status, pid))
        self.db.commit()
        return cur.rowcount > 0

    def reopen(self, pid: int) -> bool:
        cur = self.db.execute(
            "UPDATE promises SET status = 'open', last_reminded_at = NULL, pre_reminded = 0 "
            "WHERE id = ? AND status != 'open'", (pid,))
        self.db.commit()
        return cur.rowcount > 0

    def snooze(self, pid: int, until: datetime) -> bool:
        cur = self.db.execute(
            "UPDATE promises SET snoozed_until = ?, last_reminded_at = NULL, pre_reminded = 0 "
            "WHERE id = ? AND status = 'open'", (_iso(until), pid))
        self.db.commit()
        return cur.rowcount > 0

    def mark_reminded(self, pid: int, *, pre: bool = False) -> None:
        if pre:
            self.db.execute("UPDATE promises SET pre_reminded = 1 WHERE id = ?", (pid,))
        else:
            self.db.execute("UPDATE promises SET last_reminded_at = ? WHERE id = ?",
                            (_iso(utcnow()), pid))
        self.db.commit()
