"""SQLite schema and helpers. One DB, every table keyed by shop_id (owner's WhatsApp number)."""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS shops (
  shop_id    TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  language   TEXT NOT NULL DEFAULT 'en',
  consent_given_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS customers (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id    TEXT NOT NULL REFERENCES shops(shop_id),
  name       TEXT NOT NULL COLLATE NOCASE,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE (shop_id, name)
);

CREATE TABLE IF NOT EXISTS products (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id       TEXT NOT NULL REFERENCES shops(shop_id),
  name          TEXT NOT NULL COLLATE NOCASE,
  price_cents   INTEGER,
  stock_qty     REAL NOT NULL DEFAULT 0,
  reorder_level REAL NOT NULL DEFAULT 0,
  wholesaler    TEXT NOT NULL DEFAULT '',
  unit          TEXT NOT NULL DEFAULT '',
  updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE (shop_id, name)
);

CREATE TABLE IF NOT EXISTS credit_entries (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id      TEXT NOT NULL REFERENCES shops(shop_id),
  customer_id  INTEGER NOT NULL REFERENCES customers(id),
  amount_cents INTEGER NOT NULL,
  items_text   TEXT NOT NULL DEFAULT '',
  action_id    TEXT NOT NULL UNIQUE,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  reversed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS payments (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id      TEXT NOT NULL REFERENCES shops(shop_id),
  customer_id  INTEGER NOT NULL REFERENCES customers(id),
  amount_cents INTEGER NOT NULL,
  action_id    TEXT NOT NULL UNIQUE,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  reversed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sales (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id      TEXT NOT NULL REFERENCES shops(shop_id),
  product_id   INTEGER REFERENCES products(id),
  product_name TEXT NOT NULL,
  qty          REAL NOT NULL,
  total_cents  INTEGER NOT NULL,
  customer_id  INTEGER REFERENCES customers(id),
  action_id    TEXT NOT NULL UNIQUE,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  reversed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stock_moves (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id    TEXT NOT NULL REFERENCES shops(shop_id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  delta_qty  REAL NOT NULL,
  reason     TEXT NOT NULL DEFAULT '',
  action_id  TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS expiry_items (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id      TEXT NOT NULL REFERENCES shops(shop_id),
  product_name TEXT NOT NULL,
  qty          REAL NOT NULL DEFAULT 1,
  expiry_date  TEXT NOT NULL,
  note         TEXT NOT NULL DEFAULT '',
  action_id    TEXT,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS pending_actions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id      TEXT NOT NULL REFERENCES shops(shop_id),
  action_type  TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  preview_text TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending',
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  resolved_at  TEXT
);

CREATE TABLE IF NOT EXISTS actions (
  action_id  TEXT PRIMARY KEY,
  shop_id    TEXT NOT NULL REFERENCES shops(shop_id),
  kind       TEXT NOT NULL,
  summary    TEXT NOT NULL,
  data_json  TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  undone     INTEGER NOT NULL DEFAULT 0,
  undone_at  TEXT
);

CREATE TABLE IF NOT EXISTS messages_seen (
  message_id TEXT PRIMARY KEY,
  shop_id    TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS conversation_turns (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id    TEXT NOT NULL REFERENCES shops(shop_id),
  role       TEXT NOT NULL,
  content    TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_customers_shop ON customers(shop_id);
CREATE INDEX IF NOT EXISTS idx_products_shop ON products(shop_id);
CREATE INDEX IF NOT EXISTS idx_credit_shop ON credit_entries(shop_id);
CREATE INDEX IF NOT EXISTS idx_payments_shop ON payments(shop_id);
CREATE INDEX IF NOT EXISTS idx_sales_shop ON sales(shop_id);
CREATE INDEX IF NOT EXISTS idx_stock_shop ON stock_moves(shop_id);
CREATE INDEX IF NOT EXISTS idx_expiry_shop ON expiry_items(shop_id);
CREATE INDEX IF NOT EXISTS idx_pending_shop ON pending_actions(shop_id);
CREATE INDEX IF NOT EXISTS idx_actions_shop ON actions(shop_id);
CREATE INDEX IF NOT EXISTS idx_turns_shop ON conversation_turns(shop_id);
"""


def get_db_path() -> Path:
    return Path(os.environ.get("SISI_DB", "data/sisi.db"))


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path else get_db_path()
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def today_str() -> str:
    """Calendar date for shop logic. SISI_TODAY freezes it for a recorded demo."""
    frozen = os.environ.get("SISI_TODAY", "").strip()
    if frozen:
        return frozen
    return date.today().isoformat()


def cents_to_rand(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}R{cents // 100}.{cents % 100:02d}"
