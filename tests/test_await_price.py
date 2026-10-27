"""Await-price flow: unknown or unpriced items pause the sale, price reply resumes it."""

import pytest

from app.agent import handle_text
from app.tools import (
    add_credit,
    confirm_pending,
    get_await_price,
    get_pending,
    record_sale,
    undo_last,
)

SHOP = "s1"


def _seed(conn):
    conn.execute("INSERT INTO shops (shop_id, name) VALUES ('s1', 'S')")
    conn.execute("INSERT INTO customers (shop_id, name) VALUES ('s1', 'Sipho')")
    conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, stock_qty, reorder_level)"
        " VALUES ('s1', 'White Bread', 1950, 12, 6)"
    )
    conn.execute(
        "INSERT INTO products (shop_id, name, stock_qty, reorder_level)"
        " VALUES ('s1', 'Milk 1L', 5, 6)"
    )
    conn.commit()


def test_sale_unknown_item_asks_for_price_and_stores_nothing(conn):
    _seed(conn)
    msg = record_sale(conn, SHOP, "Stoney 2L", 2, buyer="Sipho")
    assert msg == ("Stoney 2L isn't on your list yet. Reply with the price, e.g."
                   " 'stoney 2l R18', and I'll add it and record Sipho's credit.")
    assert get_pending(conn, SHOP) is None
    assert get_await_price(conn, SHOP)["product"] == "Stoney 2L"


def test_cash_sale_unknown_item_ask_variant(conn):
    _seed(conn)
    msg = record_sale(conn, SHOP, "Stoney 2L", 1)
    assert msg.endswith("and I'll add it and record the sale.")


def test_price_reply_completes_credit_sale(conn):
    _seed(conn)
    record_sale(conn, SHOP, "Stoney 2L", 2, buyer="Sipho")
    msg, buttons = handle_text(conn, SHOP, "stoney 2l R25")
    assert buttons == ["Yes", "No"]
    assert "2 x Stoney 2L = R50.00 to Sipho on the book" in msg
    saved, _ = handle_text(conn, SHOP, "Yes")
    assert "Saved. Sipho owes R50.00 more." in saved
    row = conn.execute("SELECT price_cents, stock_qty FROM products"
                       " WHERE shop_id = ? AND name = ?", (SHOP, "Stoney 2L")).fetchone()
    assert (row["price_cents"], row["stock_qty"]) == (2500, -2)


def test_price_reply_completes_credit_book_entry(conn):
    _seed(conn)
    msg = add_credit(conn, SHOP, "Sipho", [{"product": "Stoney 2L", "qty": 2}])
    assert "and I'll add it and record Sipho's credit." in msg
    preview, buttons = handle_text(conn, SHOP, "R25")
    assert buttons == ["Yes", "No"]
    assert "+R50.00 on the book (2x Stoney 2L)" in preview
    handle_text(conn, SHOP, "Yes")
    assert undo_last(conn, SHOP).startswith("Undone: removed Sipho's R50.00 credit.")
    still_there = conn.execute("SELECT price_cents FROM products"
                               " WHERE shop_id = ? AND name = ?", (SHOP, "Stoney 2L")).fetchone()
    assert still_there["price_cents"] == 2500


def test_unpriced_known_item_ask_resumes_on_price(conn):
    _seed(conn)
    assert record_sale(conn, SHOP, "Milk 1L", 1) == "What is the price of Milk 1L?"
    preview, buttons = handle_text(conn, SHOP, "milk R22")
    assert buttons == ["Yes", "No"]
    assert "1 x Milk 1L = R22.00 cash" in preview


def test_multi_line_credit_pauses_on_unknown_line(conn):
    _seed(conn)
    msg = add_credit(conn, SHOP, "Sipho", [{"product": "White Bread", "qty": 1},
                                           {"product": "Stoney 2L", "qty": 2}])
    assert msg.startswith("Stoney 2L isn't on your list yet.")
    preview, _ = handle_text(conn, SHOP, "stoney R25")
    assert "+R69.50 on the book (1x White Bread, 2x Stoney 2L)" in preview


def test_two_prices_in_one_reply_asks_for_one(conn):
    _seed(conn)
    record_sale(conn, SHOP, "Stoney 2L", 1)
    msg, buttons = handle_text(conn, SHOP, "stoney R25 milk R22")
    assert buttons is None
    assert msg == "One price at a time — reply with just the Stoney 2L price, e.g. 'Stoney 2L R18'."


def test_unrelated_question_falls_through_to_llm(conn, monkeypatch):
    _seed(conn)
    record_sale(conn, SHOP, "Stoney 2L", 1)
    monkeypatch.setattr("app.agent.route_to_llm", lambda c, s, t: "LLM-REPLY")
    msg, _ = handle_text(conn, SHOP, "how much did I make today?")
    assert msg == "LLM-REPLY"
    assert get_await_price(conn, SHOP) is not None  # still waiting


def test_price_for_another_product_falls_through(conn, monkeypatch):
    _seed(conn)
    record_sale(conn, SHOP, "Stoney 2L", 1)
    monkeypatch.setattr("app.agent.route_to_llm", lambda c, s, t: "LLM-REPLY")
    msg, _ = handle_text(conn, SHOP, "milk costs R22")
    assert msg == "LLM-REPLY"


def test_no_cancels_await_price(conn):
    _seed(conn)
    record_sale(conn, SHOP, "Stoney 2L", 1)
    msg, _ = handle_text(conn, SHOP, "no")
    assert msg == "OK, I dropped that sale."
    assert get_await_price(conn, SHOP) is None


def test_yes_on_orphaned_await_asks_for_price(conn):
    _seed(conn)
    record_sale(conn, SHOP, "Stoney 2L", 1)
    msg, _ = handle_text(conn, SHOP, "yes")
    assert msg == "Still need the price — reply with it, e.g. 'Stoney 2L R18'."


def test_stale_await_is_ignored(conn, monkeypatch):
    _seed(conn)
    record_sale(conn, SHOP, "Stoney 2L", 1)
    conn.execute("UPDATE await_price SET created_at = '2020-01-01T00:00:00.000Z'")
    conn.commit()
    monkeypatch.setattr("app.agent.route_to_llm", lambda c, s, t: "LLM-REPLY")
    assert handle_text(conn, SHOP, "R25")[0] == "LLM-REPLY"
    assert get_await_price(conn, SHOP) is None


def test_price_reply_waits_when_normal_preview_pending(conn, monkeypatch):
    _seed(conn)
    record_sale(conn, SHOP, "Stoney 2L", 1)
    conn.execute(
        "INSERT INTO pending_actions (shop_id, action_type, payload_json, preview_text)"
        " VALUES ('s1', 'add_stock', '{}', 'Save: ...')"
    )
    conn.commit()
    monkeypatch.setattr("app.agent.route_to_llm", lambda c, s, t: "LLM-REPLY")
    assert handle_text(conn, SHOP, "stoney R25")[0] == "LLM-REPLY"
    assert get_pending(conn, SHOP) is not None
