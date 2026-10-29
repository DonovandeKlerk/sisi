"""Audit script: suspected auto-posts, double stock, reused slip photos.

Fixture DBs are built programmatically (no committed fixtures).
"""

import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import init_db

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "audit_duplicates.py"


def _load():
    spec = importlib.util.spec_from_file_location("audit_duplicates", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ts(days_ago=0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _shop(conn, shop="s1", product="Tastic Rice 2kg"):
    conn.execute("INSERT INTO shops (shop_id, name) VALUES (?, 'S')", (shop,))
    cur = conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, stock_qty)"
        " VALUES (?, ?, 6500, 8)", (shop, product))
    conn.commit()
    return cur.lastrowid


def test_autoposts_flagged_by_confidence(conn):
    mod = _load()
    _shop(conn)
    conn.execute(
        "INSERT INTO actions (action_id, shop_id, kind, summary, data_json,"
        " created_at) VALUES ('a1', 's1', 'stock_intake', 'intake', '{}', ?)",
        (_ts(),))
    conn.execute(
        "INSERT INTO cash_moves (shop_id, account, amount_cents, kind,"
        " action_id) VALUES ('s1', 'cash', -33000, 'purchase', 'a1')")
    conn.execute(
        "INSERT INTO actions (action_id, shop_id, kind, summary, data_json,"
        " created_at) VALUES ('a2', 's1', 'stock', 'add', '{}', ?)",
        (_ts(),))
    conn.execute(
        "INSERT INTO supplier_moves (shop_id, supplier, amount_cents,"
        " action_id) VALUES ('s1', 'Jumbo', 18000, 'a2')")
    conn.execute(
        "INSERT INTO actions (action_id, shop_id, kind, summary, data_json,"
        " created_at, undone) VALUES ('a3', 's1', 'stock', 'old', '{}', ?, 1)",
        (_ts(),))
    conn.execute(
        "INSERT INTO cash_moves (shop_id, account, amount_cents, kind,"
        " action_id, reversed) VALUES ('s1', 'cash', -100, 'purchase',"
        " 'a3', 1)")
    conn.commit()
    rep = mod.audit(conn)
    assert [(f["action_id"], f["confidence"]) for f in rep["autoposts"]] == [
        ("a1", "certain intake auto-post"),
        ("a2", "possible (intake or explicit add_stock)"),
    ]
    assert rep["autoposts"][0]["cash_cents"] == 33000
    assert rep["autoposts"][1]["supplier_cents"] == 18000
    assert rep["double_stock"] == [] and rep["reused_photos"] == []


def test_double_stock_pairs_with_evidence(conn):
    mod = _load()
    pid = _shop(conn)
    data = {"lines": [{"product_id": pid, "name": "Tastic Rice 2kg",
                       "units": 6, "unit_cost": 5500, "total": 33000}]}
    conn.execute(
        "INSERT INTO receipts (shop_id, supplier, receipt_no, total_cents,"
        " account, data_json, action_id, created_at)"
        " VALUES ('s1', 'Jumbo', 'R-1', 33000, 'cash', ?, 'r1', ?)",
        (json.dumps(data), _ts()))
    conn.execute(
        "INSERT INTO stock_moves (shop_id, product_id, delta_qty, reason,"
        " action_id, created_at) VALUES ('s1', ?, 6, 'restock', 'a9', ?)",
        (pid, _ts(1)))
    conn.execute(
        "INSERT INTO stock_moves (shop_id, product_id, delta_qty, reason,"
        " action_id, created_at) VALUES ('s1', ?, 6, 'restock', 'a8', ?)",
        (pid, _ts(10)))  # outside ±3d: ignored
    conn.commit()
    rep = mod.audit(conn)
    assert len(rep["double_stock"]) == 1
    pair = rep["double_stock"][0]
    assert (pair["supplier"], pair["ref"]) == ("Jumbo", "R-1")
    assert pair["product"] == "Tastic Rice 2kg"
    assert pair["receipt_qty"] == 6
    assert [m["action_id"] for m in pair["moves"]] == ["a9"]
    assert pair["receipt_total_cents"] == 33000


def test_reused_photos_and_shop_filter(conn):
    mod = _load()
    pid = _shop(conn)
    _shop(conn, "s2")
    data = {"lines": [{"product_id": pid, "units": 1, "total": 100}]}
    for shop, no, hsh, rev in (("s1", "R-1", "pic1", 0),
                               ("s1", "R-2", "pic1", 0),
                               ("s1", "R-3", "pic1", 1),  # reversed: ignored
                               ("s2", "R-1", "pic1", 0)):  # other shop: own group
        conn.execute(
            "INSERT INTO receipts (shop_id, supplier, receipt_no, total_cents,"
            " account, image_hash, data_json, action_id, reversed)"
            " VALUES (?, 'Jumbo', ?, 100, 'cash', ?, ?, ?, ?)",
            (shop, no, hsh, json.dumps(data), f"{shop}-{no}", rev))
    conn.commit()
    rep = mod.audit(conn)
    assert len(rep["reused_photos"]) == 1  # s2 has only one
    assert rep["reused_photos"][0]["refs"] == ["R-1", "R-2"]
    assert mod.audit(conn, "s2")["reused_photos"] == []
    out = mod.format_human(rep, "t.db", None)
    assert "(A)" in out and "(B)" in out and "(C)" in out


def test_cli_json_readonly(tmp_path):
    mod = _load()
    db = tmp_path / "shop.db"
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    init_db(c)
    _shop(c)
    c.execute("INSERT INTO actions (action_id, shop_id, kind, summary,"
              " data_json, created_at) VALUES ('a1', 's1', 'stock_intake',"
              " 'intake', '{}', ?)", (_ts(),))
    c.execute("INSERT INTO cash_moves (shop_id, account, amount_cents, kind,"
              " action_id) VALUES ('s1', 'cash', -100, 'purchase', 'a1')")
    c.commit()
    c.close()
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main(["--db", str(db), "--shop", "s1", "--json"])
    assert rc == 0
    rep = json.loads(buf.getvalue())
    assert [f["action_id"] for f in rep["autoposts"]] == ["a1"]
