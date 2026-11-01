"""SQLite schema and helpers. One DB, every table keyed by shop_id (owner's WhatsApp number).

Two backends, one dialect. Local files and tests use sqlite3; production can
point at Turso Cloud (SQLite-compatible, transactional, safe for concurrent
containers) by setting TURSO_DATABASE_URL + TURSO_AUTH_TOKEN. An explicit
path to connect() always selects sqlite3, so tests and tools never touch
the network.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Union

log = logging.getLogger("sisi.db")

if TYPE_CHECKING:  # pragma: no cover
    from turso_serverless.connection import Connection as _TursoConnection

DbConnection = Union["sqlite3.Connection", "_TursoConnection", "TursoReconnect"]

try:
    from turso_serverless.dbapi import Error as _TursoError
    from turso_serverless.dbapi import IntegrityError as _TursoIntegrityError
    from turso_serverless.dbapi import OperationalError as _TursoOperationalError
except ImportError:  # pragma: no cover - driver only installed where needed
    _TursoError = _TursoIntegrityError = _TursoOperationalError = None

#: Catch these instead of sqlite3's errors: they cover both backends.
DB_ERROR: tuple[type[BaseException], ...] = (
    (sqlite3.Error,) + ((_TursoError,) if _TursoError else ())
)
DB_INTEGRITY_ERROR: tuple[type[BaseException], ...] = (
    (sqlite3.IntegrityError,) + ((_TursoIntegrityError,) if _TursoIntegrityError else ())
)
DB_OPERATIONAL_ERROR: tuple[type[BaseException], ...] = (
    (sqlite3.OperationalError,) + ((_TursoOperationalError,) if _TursoOperationalError else ())
)

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

CREATE TABLE IF NOT EXISTS error_reports (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id    TEXT NOT NULL,
  context    TEXT NOT NULL DEFAULT '',
  detail     TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_error_reports_shop ON error_reports (shop_id, id);

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

_SCHEMA_TABLES: tuple[str, ...] = tuple(
    dict.fromkeys(
        m.group(1).lower()
        for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA)
    )
)
"""Every table init_db must create; _verify_schema fails loud without them."""


def get_db_path() -> Path:
    return Path(os.environ.get("SISI_DB", "data/sisi.db"))


def turso_creds() -> tuple[str | None, str | None]:
    url = os.environ.get("TURSO_DATABASE_URL", "").strip()
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if url and token:
        return url, token
    return None, None


def using_turso(path: str | Path | None = None) -> bool:
    """True when connect() would use Turso. Explicit paths always use sqlite."""
    if path is not None:
        return False
    url, _token = turso_creds()
    return bool(url)


READ_ONLY: ContextVar[bool] = ContextVar("sisi_read_only", default=False)
"""True while handling a message on a read-only testing channel.

Set by process_message for Whapi until database durability is proven
(unlock is explicit: WHAPI_ALLOW_WRITES=1). Operational memory
(conversation turns, last-reply, numbered options) no-ops while set;
business writes are denied by `enforce_read_only` below.
"""


def read_only() -> bool:
    """True when the current message must not mutate business records."""
    return READ_ONLY.get()


READ_ONLY_WRITE_TABLES = frozenset(
    {"messages_seen", "send_queue", "send_pacing", "reports"})
"""Tables writable in read-only mode: dedupe, the reply pipeline, and
generated-report artifacts. Everything else is denied."""

READ_ONLY_INSERT_TABLES = frozenset({"shops"})
"""Tables allowing INSERT but not UPDATE/DELETE in read-only mode.

`ensure_shop`'s INSERT OR IGNORE is a no-op for existing shops; the
tester's shop already exists, and setup (which would populate a new
row) is blocked separately.
"""


def enforce_read_only(conn: DbConnection) -> None:
    """Deny business writes on this connection via a SQLite authorizer.

    Reads are allowed everywhere. Writes are allowed only on
    READ_ONLY_WRITE_TABLES (+ INSERT on READ_ONLY_INSERT_TABLES); all
    DDL is denied. Call AFTER init_db. Denied statements raise
    sqlite3.DatabaseError ("not authorized"), which the message pipeline
    turns into the friendly fallback — never a silent write, never silence.

    On Turso connections (no authorizer API) this is a no-op: the
    proactive gates (confirm/setup/photo/plan-kind blocks) still apply,
    but the hard backstop is SQLite-only. Logged loudly when hit.
    """

    def _auth(action: int, p1: str | None, p2: str | None,
              _db: str | None, _src: str | None) -> int:
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_INSERT:
            if p1 in READ_ONLY_WRITE_TABLES \
                    or p1 in READ_ONLY_INSERT_TABLES:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            if p1 in READ_ONLY_WRITE_TABLES:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE,
                      sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_DROP_INDEX,
                      sqlite3.SQLITE_CREATE_TRIGGER,
                      sqlite3.SQLITE_DROP_TRIGGER,
                      sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_DROP_VIEW,
                      sqlite3.SQLITE_ALTER_TABLE):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    if not hasattr(conn, "set_authorizer"):
        log.warning("read-only backstop unavailable on %s; proactive gates only",
                    type(conn).__name__)
        return
    conn.set_authorizer(_auth)


_STREAM_LOST = re.compile(r"stream (not found|expired|closed)",
                            re.IGNORECASE)


class TursoReconnect:
    """A Turso connection that survives idle stream expiry.

    Turso serverless streams die after ~a minute without statements; any
    flow holding one connection across slow model calls (a photo spans
    vision + planner) trips over the next query with HTTP 404 'stream
    not found'. On that error — and only that — with no transaction
    open, reconnect once and retry the statement: nothing uncommitted
    can be lost because the driver confirms nothing was pending. With
    an open transaction the writes are already gone server-side, so
    fail loud instead of pretending success. sqlite3 is never wrapped.
    """

    _OWN = frozenset({"_url", "_token", "_conn"})

    def __init__(self, url: str, token: str):
        object.__setattr__(self, "_url", url)
        object.__setattr__(self, "_token", token)
        object.__setattr__(self, "_conn", self._fresh())

    def _fresh(self):
        from turso_serverless import connect as _turso_connect
        conn = _turso_connect(self._url, auth_token=self._token)
        # The driver's Row is already sqlite3.Row-like (index + name
        # access); its factory protocol takes (cursor, row), so pass
        # the prebuilt row straight through.
        conn.row_factory = lambda _cur, row: row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _stream_lost(exc: BaseException) -> bool:
        return _STREAM_LOST.search(str(exc)) is not None

    def _clean(self) -> bool:
        try:
            return not self._conn.in_transaction
        except Exception:
            return False  # unknown state: fail loud, never drop writes

    def _run(self, op, *args, **kwargs):
        try:
            return op(self._conn, *args, **kwargs)
        except Exception as e:
            if not self._stream_lost(e) or not self._clean():
                raise
            log.warning("turso stream expired mid-request; reconnecting")
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = self._fresh()
            return op(self._conn, *args, **kwargs)

    def execute(self, sql, parameters=()):
        return self._run(lambda c: c.execute(sql, parameters))

    def executemany(self, sql, parameters):
        return self._run(lambda c: c.executemany(sql, parameters))

    def executescript(self, sql_script):
        return self._run(lambda c: c.executescript(sql_script))

    def commit(self):
        return self._run(lambda c: c.commit())

    def rollback(self):
        return self._run(lambda c: c.rollback())

    def close(self):
        try:
            self._conn.close()
        except Exception as e:
            if not self._stream_lost(e):
                raise

    def __setattr__(self, name, value):
        if name in TursoReconnect._OWN:
            object.__setattr__(self, name, value)
        else:
            setattr(self._conn, name, value)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_conn"), name)


def connect(path: str | Path | None = None) -> DbConnection:
    if path is None:
        url, token = turso_creds()
        if url and token:
            return TursoReconnect(url, token)
    db_path = Path(path) if path else get_db_path()
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: DbConnection) -> None:
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
        "ALTER TABLE sales ADD COLUMN pay_method TEXT NOT NULL DEFAULT 'cash'",
    ):
        try:
            conn.execute(sql)
        except DB_OPERATIONAL_ERROR as e:
            if _BENIGN_ALTER.search(str(e)) is None:
                raise  # e.g. "no such table": a real schema failure
            # column already exists on fresh DBs
    conn.commit()
    _verify_schema(conn)


_BENIGN_ALTER = re.compile(r"duplicate column|already exists", re.IGNORECASE)
"""ALTER errors safe to swallow; anything else fails loud."""


def _verify_schema(conn: DbConnection) -> None:
    """Fail loud when tables are missing after init (never mask it)."""
    have = {r[0].lower()
            for r in conn.execute("SELECT name FROM sqlite_master"
                                  " WHERE type = 'table'").fetchall()}
    missing = [t for t in _SCHEMA_TABLES if t not in have]
    if missing:
        raise RuntimeError("schema verification failed, missing tables: "
                           + ", ".join(missing))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# Africa/Johannesburg is a fixed +02:00 with no daylight saving.
SAST = timezone(timedelta(hours=2))


def today_str() -> str:
    """Calendar date for shop logic, in Africa/Johannesburg.

    Servers run on UTC, which is still yesterday for the first two hours
    of every South African day. SISI_TODAY freezes it for a recorded demo.
    """
    frozen = os.environ.get("SISI_TODAY", "").strip()
    if frozen:
        return frozen
    return datetime.now(timezone.utc).astimezone(SAST).date().isoformat()


def today_line() -> str:
    """'Sunday 2026-11-01 (Africa/Johannesburg)' — server-computed so the
    model never has to work out a weekday itself (it gets them wrong)."""
    try:
        day = date.fromisoformat(today_str())
    except ValueError:
        day = datetime.now(timezone.utc).astimezone(SAST).date()
    return f"{day.strftime('%A')} {day.isoformat()} (Africa/Johannesburg)"


def cents_to_rand(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}R{cents // 100}.{cents % 100:02d}"
