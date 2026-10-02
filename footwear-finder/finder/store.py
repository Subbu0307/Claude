"""Remembers, per WhatsApp number, where that person left their footwear (and an optional photo)."""

import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS saves (
    phone       TEXT PRIMARY KEY,
    spot_code   TEXT,              -- NULL until they tell us the spot (e.g. sent a photo first)
    photo       BLOB,
    photo_mime  TEXT,
    saved_at    REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._memory = self._connect() if path == ":memory:" else None
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if self._memory is not None:
            yield self._memory
            return
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def get(self, phone: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM saves WHERE phone = ?", (phone,)).fetchone()
            return dict(row) if row else None

    def save_spot(self, phone: str, spot_code: str, keep_photo: bool) -> None:
        now = time.time()
        with self._conn() as conn:
            if keep_photo:
                conn.execute(
                    "INSERT INTO saves (phone, spot_code, saved_at, updated_at) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(phone) DO UPDATE SET spot_code = excluded.spot_code, updated_at = excluded.updated_at",
                    (phone, spot_code, now, now),
                )
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO saves (phone, spot_code, photo, photo_mime, saved_at, updated_at)"
                    " VALUES (?, ?, NULL, NULL, ?, ?)",
                    (phone, spot_code, now, now),
                )

    def save_photo(self, phone: str, photo: bytes, mime: str) -> None:
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO saves (phone, photo, photo_mime, saved_at, updated_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(phone) DO UPDATE SET photo = excluded.photo, photo_mime = excluded.photo_mime,"
                " updated_at = excluded.updated_at",
                (phone, photo, mime, now, now),
            )

    def delete(self, phone: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM saves WHERE phone = ?", (phone,))

    def purge(self, retention_seconds: float) -> int:
        """Delete everything older than the retention window: phone numbers and photos included."""
        cutoff = time.time() - retention_seconds
        with self._conn() as conn:
            n = conn.execute("DELETE FROM saves WHERE updated_at < ?", (cutoff,)).rowcount
            conn.execute("DELETE FROM processed_messages WHERE created_at < ?", (time.time() - 86400,))
            return n

    def counts_by_spot(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT spot_code, COUNT(*) AS n FROM saves WHERE spot_code IS NOT NULL GROUP BY spot_code"
            ).fetchall()
            return {r["spot_code"]: r["n"] for r in rows}

    def mark_processed(self, message_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO processed_messages (message_id, created_at) VALUES (?, ?)",
                (message_id, time.time()),
            )
            return cur.rowcount == 1
