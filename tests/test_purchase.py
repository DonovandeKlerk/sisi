"""Explicit stock payments: record_purchase proposes, Yes posts, undo reverses.

Photo intake records cost on the product row but never pays; this tool
is the only way intake costs reach the books. Cash/petty/bank post a
`purchase` cash move, account=supplier posts on-account debt. Stock
levels never change here.
"""

import json
from types import SimpleNamespace

import pytest

import app.interpreter as interp
from app import agent
from app.i18n import CURRENT_LANG, LANGS
from app.setup import skip_setup
from app.tools import (account_balance, confirm_pending, ensure_shop,
                       get_pending, record_purchase, set_float,
                       supplier_balance, undo_last)
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID


@pytest.fixture()
def shop(conn):
    seed_demo(SHOP, conn)
    skip_setup(conn, SHOP)
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?",
                 (SHOP,))
    conn.commit()
    return SHOP


@pytest.fixture()
def floated(conn, shop):
    set_float(conn, SHOP, {"cash": 100000, "petty": 20000})
    return SHOP


def _stock_snapshot(conn):
    return {r["name"]: r["stock_qty"] for r in conn.execute(
        "SELECT name, stock_qty FROM products WHERE shop_id = ?",
        (SHOP,)).fetchall()}


def _stub_plan(monkeypatch, tasks, question=None):
    def fake(messages):
        body = {"understood": "purchase test", "tasks": tasks,
                "question": question, "options": []}
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2))

    monkeypatch.setattr(interp, "propose_plan", fake)


def test_cash_purchase_posts_without_stock_change(conn, floated):
    before = _stock_snapshot(conn)
    preview = record_purchase(conn, SHOP, 6000, account="cash")
    assert "R60.00" in preview and "Till" in preview
    assert get_pending(conn, SHOP)["action_type"] == "record_purchase"
    saved = confirm_pending(conn, SHOP)
    assert "R60.00" in saved
    moves = conn.execute("SELECT kind, amount_cents, account FROM cash_moves"
                         " WHERE shop_id = ? AND reversed = 0"
                         " AND kind = 'purchase'",
                         (SHOP,)).fetchall()
    assert [(m["kind"], m["amount_cents"], m["account"]) for m in moves] == \
        [("purchase", -6000, "cash")]
    assert account_balance(conn, SHOP, "cash") == 94000
    assert _stock_snapshot(conn) == before  # zero stock change


def test_account_cleaning_petty_bank_and_default(conn, floated):
    record_purchase(conn, SHOP, 1000, account="PETTY")
    assert get_pending(conn, SHOP)["action_type"] == "record_purchase"
    confirm_pending(conn, SHOP)
    record_purchase(conn, SHOP, 2000, account="bank")
    confirm_pending(conn, SHOP)
    record_purchase(conn, SHOP, 3000, account="sock")
    confirm_pending(conn, SHOP)
    assert account_balance(conn, SHOP, "petty") == 19000
    assert account_balance(conn, SHOP, "bank") == -2000
    assert account_balance(conn, SHOP, "cash") == 97000  # unknown -> cash


def test_supplier_purchase_posts_debt_cash_untouched(conn, floated):
    before = _stock_snapshot(conn)
    preview = record_purchase(conn, SHOP, 12000, account="supplier",
                              supplier="Jumbo")
    assert "Jumbo" in preview and "R120.00" in preview
    assert "Owed R0.00 → R120.00" in preview
    confirm_pending(conn, SHOP)
    rows = conn.execute("SELECT amount_cents FROM supplier_moves"
                        " WHERE shop_id = ? AND reversed = 0", (SHOP,)).fetchall()
    assert [r["amount_cents"] for r in rows] == [12000]
    assert supplier_balance(conn, SHOP, "Jumbo") == 12000
    assert account_balance(conn, SHOP, "cash") == 100000
    assert _stock_snapshot(conn) == before


def test_supplier_account_needs_a_name(conn, floated):
    assert record_purchase(conn, SHOP, 12000,
                           account="supplier") == "Which supplier?"
    assert get_pending(conn, SHOP) is None


def test_bad_amounts_ask_how_much(conn, floated):
    for bad in (0, -5, "lots"):
        assert "How much" in record_purchase(conn, SHOP, bad)
    assert get_pending(conn, SHOP) is None


def test_undo_reverses_cash_purchase(conn, floated):
    record_purchase(conn, SHOP, 6000, account="cash")
    confirm_pending(conn, SHOP)
    msg = undo_last(conn, SHOP)
    assert "Undone" in msg and "stock purchase" in msg
    assert account_balance(conn, SHOP, "cash") == 100000
    assert conn.execute("SELECT COUNT(*) n FROM cash_moves"
                        " WHERE shop_id = ? AND reversed = 0"
                        " AND kind = 'purchase'",
                        (SHOP,)).fetchone()["n"] == 0


def test_undo_reverses_supplier_purchase(conn, floated):
    record_purchase(conn, SHOP, 12000, account="supplier",
                    supplier="Jumbo")
    confirm_pending(conn, SHOP)
    msg = undo_last(conn, SHOP)
    assert "Undone" in msg and "stock purchase" in msg
    assert supplier_balance(conn, SHOP, "Jumbo") == 0
    assert conn.execute("SELECT COUNT(*) n FROM supplier_moves"
                        " WHERE shop_id = ? AND reversed = 0",
                        (SHOP,)).fetchone()["n"] == 0


def test_kind_registry_covers_purchase(conn, floated):
    from app.interpreter import _EXECUTORS
    from app.llm import PLAN_KINDS
    from app.tools import _APPLIERS, TOOL_DEFS, TOOL_FUNCS
    assert "purchase" in PLAN_KINDS
    assert "purchase" in _EXECUTORS
    assert "record_purchase" in _APPLIERS
    assert "record_purchase" in TOOL_FUNCS
    assert "record_purchase" in {t["function"]["name"] for t in TOOL_DEFS}
    record_purchase(conn, SHOP, 6000, account="cash")
    confirm_pending(conn, SHOP)
    row = conn.execute("SELECT kind FROM actions WHERE shop_id = ?"
                       " ORDER BY rowid DESC LIMIT 1", (SHOP,)).fetchone()
    assert row["kind"] == "purchase"


# --- intent routing: stock-payment words reach purchase, not expense ---------


def _purchase_task(amount, **extra):
    args = {"amount_cents": amount}
    args.update(extra)
    return [{"kind": "purchase", "args": args, "quote": "for stock"}]


def test_paid_cash_for_stock_routes_to_purchase(conn, floated, monkeypatch):
    _stub_plan(monkeypatch, _purchase_task(6000, account="cash"))
    msg, _ = agent.handle_text(conn, SHOP, "paid R60 cash for stock")
    assert "Record stock payment" in msg
    assert get_pending(conn, SHOP)["action_type"] == "record_purchase"


def test_paid_for_stock_routes_to_purchase(conn, floated, monkeypatch):
    _stub_plan(monkeypatch, _purchase_task(6000))
    msg, _ = agent.handle_text(conn, SHOP, "paid R60 for stock")
    assert "Record stock payment" in msg
    assert get_pending(conn, SHOP)["action_type"] == "record_purchase"


def test_supplier_account_for_stock_routes_to_purchase(conn, floated,
                                                       monkeypatch):
    _stub_plan(monkeypatch, _purchase_task(12000, account="supplier",
                                           supplier="Jumbo"))
    msg, _ = agent.handle_text(conn, SHOP,
                               "put R120 on my Jumbo account for stock")
    assert "Stock on account" in msg
    assert get_pending(conn, SHOP)["action_type"] == "record_purchase"


def test_expense_without_stock_words_stays_expense(conn, floated, monkeypatch):
    _stub_plan(monkeypatch, [{"kind": "expense",
                              "args": {"amount_cents": 15000,
                                       "category": "electricity"},
                              "quote": "paid R150 electricity"}])
    msg, _ = agent.handle_text(conn, SHOP, "paid R150 electricity")
    assert "Save expense" in msg
    assert get_pending(conn, SHOP)["action_type"] == "record_expense"


def test_prompts_disambiguate_stock_money():
    from app.prompts import PLANNER_PROMPT, SYSTEM_PROMPT
    assert "purchase(amount_cents?, account?, supplier?, note?)" in \
        PLANNER_PROMPT
    assert "never expense" in PLANNER_PROMPT
    assert "record_purchase" in SYSTEM_PROMPT


# --- four-language renders ----------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_preview_saved_undo_render_in_all_languages(conn, floated, lang):
    CURRENT_LANG.set(lang)
    preview = record_purchase(conn, SHOP, 6000, account="cash")
    assert "{" not in preview and "}" not in preview
    assert "R60.00" in preview
    saved = confirm_pending(conn, SHOP)
    assert "{" not in saved and "}" not in saved
    assert "R60.00" in saved
    sup_preview = record_purchase(conn, SHOP, 12000, account="supplier",
                                  supplier="Jumbo")
    assert "{" not in sup_preview and "}" not in sup_preview
    assert "Jumbo" in sup_preview and "R120.00" in sup_preview
    sup_saved = confirm_pending(conn, SHOP)
    assert "{" not in sup_saved and "}" not in sup_saved
    undone = undo_last(conn, SHOP)
    assert "{" not in undone and "}" not in undone
    undone = undo_last(conn, SHOP)
    assert "{" not in undone and "}" not in undone
