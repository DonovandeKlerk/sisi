"""Staff: add, record with attribution, owner-only permissions."""

import pytest

from app.agent import _STAFF_NAME, handle_text
from app.tools import (
    add_stock,
    confirm_pending,
    ensure_shop,
    get_pending,
    profit_month,
    record_sale,
    resolve_sender,
)


OWNER = "27710000014"
STAFF = "27821234567"


@pytest.fixture()
def shop(conn):
    ensure_shop(conn, OWNER, "Staff Spaza")
    conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, cost_cents, stock_qty)"
        " VALUES (?, 'Bread', 2000, 1500, 50)", (OWNER,))
    conn.commit()
    _STAFF_NAME.set(None)
    yield OWNER
    _STAFF_NAME.set(None)


def test_add_staff_and_resolve(conn, shop):
    msg, _ = handle_text(conn, OWNER, "add staff 0821234567 Thandi")
    assert "Staff added" in msg and "Thandi" in msg
    assert resolve_sender(conn, "0821234567") == (OWNER, "Thandi")
    assert resolve_sender(conn, OWNER) == (OWNER, None)


def test_add_staff_rejects_owner_number(conn, shop):
    msg, _ = handle_text(conn, OWNER, f"add staff {OWNER} Boss")
    assert "own number" in msg


def test_staff_sale_attributed(conn, shop):
    handle_text(conn, OWNER, f"add staff {STAFF} Thandi")
    _STAFF_NAME.set("Thandi")
    record_sale(conn, OWNER, "Bread", 2)
    out = confirm_pending(conn, OWNER)
    assert "Sale saved" in out
    row = conn.execute("SELECT recorded_by FROM sales WHERE shop_id = ?",
                       (OWNER,)).fetchone()
    assert row["recorded_by"] == "Thandi"


def test_staff_blocked_from_profit_and_reports(conn, shop):
    handle_text(conn, OWNER, f"add staff {STAFF} Thandi")
    for text in ("month-end report", "report september", "statement for Sipho",
                 "statements for everyone who owes", "set shop name X",
                 "set Sipho limit R300", "nudge off", "undo",
                 "delete my data", "reset demo", "add staff 0820000001 Z",
                 "bread expires Friday"):
        msg, _ = handle_text(conn, STAFF, text)
        assert msg.startswith("⛔"), text
    _STAFF_NAME.set("Thandi")
    assert profit_month(conn, OWNER).startswith("⛔")
    _STAFF_NAME.set(None)


def test_staff_can_ask_who_recorded(conn, shop):
    handle_text(conn, OWNER, f"add staff {STAFF} Thandi")
    msg, _ = handle_text(conn, STAFF, "who recorded today")
    assert msg.startswith("📝 *Who recorded today*")


def test_who_recorded_breakdown(conn, shop):
    handle_text(conn, OWNER, f"add staff {STAFF} Thandi")
    _STAFF_NAME.set("Thandi")
    record_sale(conn, OWNER, "Bread", 1)
    confirm_pending(conn, OWNER)
    _STAFF_NAME.set(None)
    record_sale(conn, OWNER, "Bread", 2)
    confirm_pending(conn, OWNER)
    msg, _ = handle_text(conn, OWNER, "who recorded today")
    assert "Thandi: 1 entries" in msg
    assert "Owner: 1 entries" in msg


def test_staff_sale_via_handle_text(conn, shop, monkeypatch):
    import json
    from types import SimpleNamespace

    import app.interpreter as interp
    handle_text(conn, OWNER, f"add staff {STAFF} Thandi")

    def fake(messages):
        body = {"understood": "staff sale",
                "tasks": [{"kind": "sale",
                           "args": {"items": [{"product": "Bread",
                                               "qty": 1}]},
                           "quote": "sold 1 bread"}],
                "question": None, "options": []}
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2))

    monkeypatch.setattr(interp, "propose_plan", fake)
    msg, buttons = handle_text(conn, STAFF, "sold 1 bread")
    assert buttons == ["Yes", "No"]
    assert get_pending(conn, OWNER) is not None  # owner's shop
