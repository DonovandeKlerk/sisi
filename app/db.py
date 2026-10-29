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
  address    TEXT NOT NULL DEFAULT '',
  phone      TEXT NOT NULL DEFAULT '',
  logo_path  TEXT NOT NULL DEFAULT '',
  logo_orig_path TEXT NOT NULL DEFAULT '',
  channel    TEXT NOT NULL DEFAULT '',
  consent_given_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS customers (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id    TEXT NOT NULL REFERENCES shops(shop_id),
  name       TEXT NOT NULL COLLATE NOCASE,
  credit_limit_cents INTEGER,
  language TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE (shop_id, name)
);

CREATE TABLE IF NOT EXISTS staff (
  shop_id    TEXT NOT NULL REFERENCES shops(shop_id),
  phone      TEXT NOT NULL,
  name       TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY (shop_id, phone)
);

CREATE TABLE IF NOT EXISTS products (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id       TEXT NOT NULL REFERENCES shops(shop_id),
  name          TEXT NOT NULL COLLATE NOCASE,
  price_cents   INTEGER,
  cost_cents    INTEGER,
  stock_qty     REAL NOT NULL DEFAULT 0,
  reorder_level REAL NOT NULL DEFAULT 0,
  wholesaler    TEXT NOT NULL DEFAULT '',
  unit          TEXT NOT NULL DEFAULT '',
  category      TEXT NOT NULL DEFAULT '',
  pack_name     TEXT NOT NULL DEFAULT '',
  pack_size     REAL,
  pack_price_cents INTEGER,
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

CREATE TABLE IF NOT EXISTS task_plans (
  shop_id    TEXT NOT NULL REFERENCES shops(shop_id),
  plan_id    TEXT NOT NULL,
  message_key TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  expires_at TEXT NOT NULL DEFAULT '',
  status     TEXT NOT NULL DEFAULT 'proposed',
  plan_json  TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (shop_id, plan_id)
);

CREATE TABLE IF NOT EXISTS clarify_context (
  shop_id      TEXT PRIMARY KEY REFERENCES shops(shop_id),
  photo_digest TEXT NOT NULL DEFAULT '',
  caption      TEXT NOT NULL DEFAULT '',
  vision_json  TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  expires_at   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS await_price (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id    TEXT NOT NULL REFERENCES shops(shop_id),
  product    TEXT NOT NULL,
  spec_json  TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS await_reminder (
  shop_id    TEXT PRIMARY KEY REFERENCES shops(shop_id),
  due_iso    TEXT NOT NULL DEFAULT '',
  sent_iso   TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
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

CREATE TABLE IF NOT EXISTS cash_moves (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id      TEXT NOT NULL REFERENCES shops(shop_id),
  account      TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  kind         TEXT NOT NULL,
  category     TEXT NOT NULL DEFAULT '',
  note         TEXT NOT NULL DEFAULT '',
  ref_type     TEXT NOT NULL DEFAULT '',
  ref_id       INTEGER,
  action_id    TEXT,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  reversed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS expenses (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id      TEXT NOT NULL REFERENCES shops(shop_id),
  category     TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  account      TEXT NOT NULL,
  note         TEXT NOT NULL DEFAULT '',
  action_id    TEXT NOT NULL UNIQUE,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  reversed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS supplier_moves (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id      TEXT NOT NULL REFERENCES shops(shop_id),
  supplier     TEXT NOT NULL COLLATE NOCASE,
  amount_cents INTEGER NOT NULL,
  note         TEXT NOT NULL DEFAULT '',
  action_id    TEXT,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  reversed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS receipts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id      TEXT NOT NULL REFERENCES shops(shop_id),
  supplier     TEXT NOT NULL,
  date         TEXT NOT NULL DEFAULT '',
  receipt_no   TEXT NOT NULL DEFAULT '',
  total_cents  INTEGER,
  vat_cents    INTEGER,
  account      TEXT NOT NULL DEFAULT 'cash',
  image_hash   TEXT NOT NULL DEFAULT '',
  data_json    TEXT NOT NULL DEFAULT '{}',
  action_id    TEXT NOT NULL UNIQUE,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  reversed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS photo_hashes (
  shop_id    TEXT NOT NULL REFERENCES shops(shop_id),
  hash       TEXT NOT NULL,
  kind       TEXT NOT NULL DEFAULT '',
  note       TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY (shop_id, hash)
);

CREATE TABLE IF NOT EXISTS order_drafts (
  shop_id      TEXT PRIMARY KEY REFERENCES shops(shop_id),
  payload_json TEXT NOT NULL,
  updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
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

CREATE TABLE IF NOT EXISTS reports (
  shop_id    TEXT NOT NULL REFERENCES shops(shop_id),
  month      TEXT NOT NULL,
  pdf_path   TEXT NOT NULL DEFAULT '',
  png_path   TEXT NOT NULL DEFAULT '',
  token      TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY (shop_id, month)
);

CREATE TABLE IF NOT EXISTS flags (
  shop_id TEXT NOT NULL REFERENCES shops(shop_id),
  key     TEXT NOT NULL,
  value   TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (shop_id, key)
);

CREATE TABLE IF NOT EXISTS send_queue (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  to_number    TEXT NOT NULL,
  channel      TEXT NOT NULL DEFAULT '',
  kind         TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  status       TEXT NOT NULL DEFAULT 'queued',
  attempts     INTEGER NOT NULL DEFAULT 0,
  next_at      REAL,
  claimed_at   REAL,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS send_pacing (
  to_number TEXT PRIMARY KEY,
  last_sent REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reminders (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id     TEXT NOT NULL REFERENCES shops(shop_id),
  due_at      TEXT NOT NULL,
  summary     TEXT NOT NULL DEFAULT '',
  context_json TEXT NOT NULL DEFAULT '{}',
  recurrence  TEXT NOT NULL DEFAULT 'none',
  status      TEXT NOT NULL DEFAULT 'pending',
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  sent_at     TEXT NOT NULL DEFAULT '',
  provider_msg_id TEXT NOT NULL DEFAULT '',
  dlr_detail  TEXT NOT NULL DEFAULT '',
  blocked_reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS intake_sessions (
  shop_id    TEXT PRIMARY KEY REFERENCES shops(shop_id),
  photo_digest TEXT NOT NULL DEFAULT '',
  caption    TEXT NOT NULL DEFAULT '',
  vision_json TEXT NOT NULL DEFAULT '[]',
  items_json TEXT NOT NULL DEFAULT '[]',
  current    INTEGER NOT NULL DEFAULT 0,
  field      TEXT,
  status     TEXT NOT NULL DEFAULT 'active',
  stash_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  expires_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_customers_shop ON customers(shop_id);
CREATE INDEX IF NOT EXISTS idx_products_shop ON products(shop_id);
CREATE INDEX IF NOT EXISTS idx_credit_shop ON credit_entries(shop_id);
CREATE INDEX IF NOT EXISTS idx_payments_shop ON payments(shop_id);
CREATE INDEX IF NOT EXISTS idx_sales_shop ON sales(shop_id);
CREATE INDEX IF NOT EXISTS idx_stock_shop ON stock_moves(shop_id);
CREATE INDEX IF NOT EXISTS idx_expiry_shop ON expiry_items(shop_id);
CREATE INDEX IF NOT EXISTS idx_pending_shop ON pending_actions(shop_id);
CREATE INDEX IF NOT EXISTS idx_await_shop ON await_price(shop_id);
CREATE INDEX IF NOT EXISTS idx_plans_shop ON task_plans(shop_id, status);
CREATE INDEX IF NOT EXISTS idx_cash_shop ON cash_moves(shop_id);
CREATE INDEX IF NOT EXISTS idx_expense_shop ON expenses(shop_id);
CREATE INDEX IF NOT EXISTS idx_supplier_shop ON supplier_moves(shop_id);
CREATE INDEX IF NOT EXISTS idx_receipt_shop ON receipts(shop_id);
CREATE INDEX IF NOT EXISTS idx_actions_shop ON actions(shop_id);
CREATE INDEX IF NOT EXISTS idx_turns_shop ON conversation_turns(shop_id);
CREATE INDEX IF NOT EXISTS idx_reports_shop ON reports(shop_id);
CREATE INDEX IF NOT EXISTS idx_flags_shop ON flags(shop_id);
CREATE INDEX IF NOT EXISTS idx_sendq_to ON send_queue(to_number, status, next_at);
CREATE INDEX IF NOT EXISTS idx_rem_due ON reminders(status, due_at);
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
    for sql in (
        "ALTER TABLE products ADD COLUMN cost_cents INTEGER",
        "ALTER TABLE products ADD COLUMN category TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE products ADD COLUMN pack_name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE products ADD COLUMN pack_size REAL",
        "ALTER TABLE products ADD COLUMN pack_price_cents INTEGER",
        "ALTER TABLE shops ADD COLUMN address TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE shops ADD COLUMN phone TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE shops ADD COLUMN logo_path TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE shops ADD COLUMN logo_orig_path TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE shops ADD COLUMN channel TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE customers ADD COLUMN credit_limit_cents INTEGER",
        "ALTER TABLE customers ADD COLUMN language TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sales ADD COLUMN recorded_by TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE credit_entries ADD COLUMN recorded_by TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE payments ADD COLUMN recorded_by TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE expenses ADD COLUMN recorded_by TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE reminders ADD COLUMN provider_msg_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE reminders ADD COLUMN dlr_detail TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE reminders ADD COLUMN blocked_reason TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE send_queue ADD COLUMN claimed_at REAL",
    ):
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists on fresh DBs
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
