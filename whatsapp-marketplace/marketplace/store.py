"""SQLite persistence for users, listings, orders and agent conversations."""

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    phone       TEXT PRIMARY KEY,
    name        TEXT,
    location    TEXT,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS listings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_phone TEXT NOT NULL REFERENCES users(phone),
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT 'other',
    price_cents  INTEGER NOT NULL,
    quantity     INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',  -- active | sold_out | removed
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id       INTEGER NOT NULL REFERENCES listings(id),
    buyer_phone      TEXT NOT NULL REFERENCES users(phone),
    seller_phone     TEXT NOT NULL REFERENCES users(phone),
    quantity         INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    note             TEXT NOT NULL DEFAULT '',
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    phone      TEXT PRIMARY KEY,
    messages   TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
"""

# Allowed order status transitions, keyed by who is acting.
SELLER_TRANSITIONS = {
    "pending": {"accepted", "cancelled"},
    "accepted": {"shipped", "cancelled"},
}
BUYER_TRANSITIONS = {
    "pending": {"cancelled"},
    "shipped": {"completed"},
}


class MarketplaceError(Exception):
    """A user-facing rule violation (bad id, not enough stock, not your listing...)."""


class Store:
    def __init__(self, path: str):
        self.path = path
        # A shared in-memory DB needs one long-lived connection; files get one per call.
        self._memory_conn = self._connect() if path == ":memory:" else None
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if self._memory_conn is not None:
            yield self._memory_conn
            return
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # ---- users -------------------------------------------------------------

    def ensure_user(self, phone: str, name: str | None = None) -> dict[str, Any]:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO users (phone, name, created_at) VALUES (?, ?, ?)",
                    (phone, name, time.time()),
                )
            elif name and not row["name"]:
                conn.execute("UPDATE users SET name = ? WHERE phone = ?", (name, phone))
            return dict(conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone())

    def get_user(self, phone: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
            return dict(row) if row else None

    def update_profile(self, phone: str, name: str | None, location: str | None) -> dict[str, Any]:
        with self._tx() as conn:
            if name is not None:
                conn.execute("UPDATE users SET name = ? WHERE phone = ?", (name, phone))
            if location is not None:
                conn.execute("UPDATE users SET location = ? WHERE phone = ?", (location, phone))
            return dict(conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone())

    # ---- listings ----------------------------------------------------------

    def create_listing(
        self, seller: str, title: str, description: str, category: str, price_cents: int, quantity: int
    ) -> dict[str, Any]:
        if price_cents < 0:
            raise MarketplaceError("Price cannot be negative.")
        if quantity < 1:
            raise MarketplaceError("Quantity must be at least 1.")
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO listings (seller_phone, title, description, category, price_cents, quantity, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (seller, title.strip(), description.strip(), category.strip().lower() or "other",
                 price_cents, quantity, time.time()),
            )
            return self._listing(conn, cur.lastrowid)

    def get_listing(self, listing_id: int) -> dict[str, Any]:
        with self._conn() as conn:
            return self._listing(conn, listing_id)

    @staticmethod
    def _listing(conn: sqlite3.Connection, listing_id: int) -> dict[str, Any]:
        row = conn.execute(
            "SELECT l.*, u.name AS seller_name, u.location AS seller_location"
            " FROM listings l JOIN users u ON u.phone = l.seller_phone WHERE l.id = ?",
            (listing_id,),
        ).fetchone()
        if row is None:
            raise MarketplaceError(f"Listing #{listing_id} does not exist.")
        return dict(row)

    def search_listings(
        self, query: str = "", category: str = "", max_price_cents: int | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT l.*, u.name AS seller_name, u.location AS seller_location"
            " FROM listings l JOIN users u ON u.phone = l.seller_phone"
            " WHERE l.status = 'active' AND l.quantity > 0"
        )
        params: list[Any] = []
        for word in query.split():
            sql += " AND (l.title LIKE ? OR l.description LIKE ? OR l.category LIKE ?)"
            params += [f"%{word}%"] * 3
        if category:
            sql += " AND l.category = ?"
            params.append(category.strip().lower())
        if max_price_cents is not None:
            sql += " AND l.price_cents <= ?"
            params.append(max_price_cents)
        sql += " ORDER BY l.created_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def listings_by_seller(self, seller: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM listings WHERE seller_phone = ? AND status != 'removed' ORDER BY created_at DESC",
                (seller,),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_listing(
        self,
        seller: str,
        listing_id: int,
        price_cents: int | None = None,
        quantity: int | None = None,
        description: str | None = None,
        remove: bool = False,
    ) -> dict[str, Any]:
        with self._tx() as conn:
            listing = self._listing(conn, listing_id)
            if listing["seller_phone"] != seller:
                raise MarketplaceError("You can only change your own listings.")
            if listing["status"] == "removed":
                raise MarketplaceError(f"Listing #{listing_id} was removed.")
            if price_cents is not None:
                if price_cents < 0:
                    raise MarketplaceError("Price cannot be negative.")
                conn.execute("UPDATE listings SET price_cents = ? WHERE id = ?", (price_cents, listing_id))
            if description is not None:
                conn.execute("UPDATE listings SET description = ? WHERE id = ?", (description, listing_id))
            if quantity is not None:
                if quantity < 0:
                    raise MarketplaceError("Quantity cannot be negative.")
                status = "active" if quantity > 0 else "sold_out"
                conn.execute(
                    "UPDATE listings SET quantity = ?, status = ? WHERE id = ?", (quantity, status, listing_id)
                )
            if remove:
                conn.execute("UPDATE listings SET status = 'removed' WHERE id = ?", (listing_id,))
            return self._listing(conn, listing_id)

    # ---- orders ------------------------------------------------------------

    def place_order(self, buyer: str, listing_id: int, quantity: int, note: str = "") -> dict[str, Any]:
        if quantity < 1:
            raise MarketplaceError("Quantity must be at least 1.")
        with self._tx() as conn:
            listing = self._listing(conn, listing_id)
            if listing["seller_phone"] == buyer:
                raise MarketplaceError("You cannot buy your own listing.")
            if listing["status"] != "active":
                raise MarketplaceError(f"Listing #{listing_id} is not available ({listing['status']}).")
            if listing["quantity"] < quantity:
                raise MarketplaceError(f"Only {listing['quantity']} left for listing #{listing_id}.")
            remaining = listing["quantity"] - quantity
            conn.execute(
                "UPDATE listings SET quantity = ?, status = ? WHERE id = ?",
                (remaining, "active" if remaining else "sold_out", listing_id),
            )
            now = time.time()
            cur = conn.execute(
                "INSERT INTO orders (listing_id, buyer_phone, seller_phone, quantity, unit_price_cents, note,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (listing_id, buyer, listing["seller_phone"], quantity, listing["price_cents"], note, now, now),
            )
            return self._order(conn, cur.lastrowid)

    def get_order(self, order_id: int) -> dict[str, Any]:
        with self._conn() as conn:
            return self._order(conn, order_id)

    @staticmethod
    def _order(conn: sqlite3.Connection, order_id: int) -> dict[str, Any]:
        row = conn.execute(
            "SELECT o.*, l.title AS listing_title, b.name AS buyer_name, s.name AS seller_name"
            " FROM orders o JOIN listings l ON l.id = o.listing_id"
            " JOIN users b ON b.phone = o.buyer_phone JOIN users s ON s.phone = o.seller_phone"
            " WHERE o.id = ?",
            (order_id,),
        ).fetchone()
        if row is None:
            raise MarketplaceError(f"Order #{order_id} does not exist.")
        order = dict(row)
        order["total_cents"] = order["quantity"] * order["unit_price_cents"]
        return order

    def orders_for(self, phone: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            ids = conn.execute(
                "SELECT id FROM orders WHERE buyer_phone = ? OR seller_phone = ? ORDER BY created_at DESC LIMIT 20",
                (phone, phone),
            ).fetchall()
            return [self._order(conn, r["id"]) for r in ids]

    def update_order_status(self, actor: str, order_id: int, new_status: str) -> dict[str, Any]:
        with self._tx() as conn:
            order = self._order(conn, order_id)
            if actor == order["seller_phone"]:
                allowed = SELLER_TRANSITIONS.get(order["status"], set())
            elif actor == order["buyer_phone"]:
                allowed = BUYER_TRANSITIONS.get(order["status"], set())
            else:
                raise MarketplaceError("You are not part of this order.")
            if new_status not in allowed:
                options = ", ".join(sorted(allowed)) or "none"
                raise MarketplaceError(
                    f"Order #{order_id} is '{order['status']}'; you can change it to: {options}."
                )
            if new_status == "cancelled":
                # Put the reserved stock back on the shelf.
                conn.execute(
                    "UPDATE listings SET quantity = quantity + ?,"
                    " status = CASE WHEN status = 'sold_out' THEN 'active' ELSE status END WHERE id = ?",
                    (order["quantity"], order["listing_id"]),
                )
            conn.execute(
                "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?", (new_status, time.time(), order_id)
            )
            return self._order(conn, order_id)

    # ---- agent conversation state -----------------------------------------

    def load_conversation(self, phone: str) -> tuple[list[dict[str, Any]], float | None]:
        with self._conn() as conn:
            row = conn.execute("SELECT messages, updated_at FROM conversations WHERE phone = ?", (phone,)).fetchone()
            if row is None:
                return [], None
            return json.loads(row["messages"]), row["updated_at"]

    def save_conversation(self, phone: str, messages: list[dict[str, Any]]) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO conversations (phone, messages, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(phone) DO UPDATE SET messages = excluded.messages, updated_at = excluded.updated_at",
                (phone, json.dumps(messages), time.time()),
            )

    def clear_conversation(self, phone: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM conversations WHERE phone = ?", (phone,))

    # ---- webhook idempotency ----------------------------------------------

    def mark_processed(self, message_id: str) -> bool:
        """Record an inbound message id; False if it was already seen (WhatsApp retries deliveries)."""
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO processed_messages (message_id, created_at) VALUES (?, ?)",
                (message_id, time.time()),
            )
            return cur.rowcount == 1
