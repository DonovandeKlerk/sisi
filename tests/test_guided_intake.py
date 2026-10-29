"""Guided one-product-at-a-time intake (plan 2026-10-29).

Task 1: session store + item grounding (phantom fix).
Task 2: guided new-product flow (questions, answers, per-item preview).
"""

import pytest

from app.setup import skip_setup
from app.tools import (answer_intake_session, get_intake_session, get_pending,
                       ground_intake_items, save_intake_session,
                       start_guided_intake)
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


def _vprod(label, count=None, readable=True, notes=""):
    """Vision product dict from a label ("Whey Protein 2kg" -> brand/variant/size)."""
    words = label.split()
    brand = words[0] if words else ""
    size = ""
    rest = words[1:]
    if rest and any(ch.isdigit() for ch in rest[-1]):
        size = rest[-1]
        rest = rest[:-1]
    return {"brand": brand, "variant": " ".join(rest), "size": size,
            "visible_count": count, "readable": readable, "notes": notes}


def test_grounding_drops_phantom_item():
    items = [{"product": "Albany Bread"}, {"product": "Photo item 4"}]
    products = [{"brand": "Albany", "variant": "Bread", "size": "",
                 "visible_count": 1, "readable": True, "notes": ""}]
    kept = ground_intake_items(items, products, "please add it to our stock")
    assert [e["product"] for e in kept] == ["Albany Bread"]


def test_session_roundtrip_and_isolation(conn):
    save_intake_session(conn, "s1", {"current": 1, "items_json": []})
    assert get_intake_session(conn, "s1")["current"] == 1
    assert get_intake_session(conn, "s2") is None


def test_new_product_full_guided_flow(conn, shop, monkeypatch):
    start_guided_intake(conn, SHOP, [_vprod("Whey Protein 2kg", 1)], [],
                        "add to stock", "h1")
    # asks cost first (identity+qty confident from readable label)
    msg = answer_intake_session(conn, SHOP, "16 rand")
    assert "selling price" in msg  # cost accepted, sell asked next
    msg = answer_intake_session(conn, SHOP, "sell for eighteen")
    assert "Yes" in msg and "No" in msg  # per-item preview proposed
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"


def test_one_question_per_message_skips_supplied(conn, shop):
    start_guided_intake(conn, SHOP, [],
                        [{"product": "Sugar 2kg", "qty": 3,
                          "cost_cents": 4500}],
                        "sugar 3 cost R45", "h2")
    # qty+cost supplied -> first question is selling price
    msg = get_intake_session(conn, SHOP)  # assert field == "sell" via session
    assert msg["field"] == "sell"


def test_guided_amount_forms_and_skip(conn, shop):
    start_guided_intake(conn, SHOP, [_vprod("Whey Protein 2kg", 1)], [],
                        "add to stock", "h3")
    assert get_intake_session(conn, SHOP)["field"] == "cost"
    msg = answer_intake_session(conn, SHOP, "R16")
    assert "selling price" in msg
    item = get_intake_session(conn, SHOP)["items_json"][0]
    assert item["cost_cents"] == 1600
    msg = answer_intake_session(conn, SHOP, "skip")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    item = get_intake_session(conn, SHOP)["items_json"][0]
    assert item["sell_cents"] is None and item["stock_units"] == 1.0


def test_guided_skip_rejected_for_required_qty(conn, shop):
    start_guided_intake(conn, SHOP, [_vprod("Mystery Goods")], [],
                        "add to stock", "h4")
    assert get_intake_session(conn, SHOP)["field"] == "qty"
    msg = answer_intake_session(conn, SHOP, "skip")
    assert "didn't catch that" in msg  # skip works for cost/sell only
    assert get_intake_session(conn, SHOP)["field"] == "qty"
    msg = answer_intake_session(conn, SHOP, "three")
    assert get_intake_session(conn, SHOP)["items_json"][0]["qty"] == 3.0
    assert get_intake_session(conn, SHOP)["field"] == "cost"


def test_guided_name_then_qty_with_shoplist_price(conn, shop):
    start_guided_intake(conn, SHOP,
                        [_vprod("", None, readable=False,
                                notes="label turned away")],
                        [], "add to stock", "h5")
    assert get_intake_session(conn, SHOP)["field"] == "name"
    answer_intake_session(conn, SHOP, "Albany Bread")
    session = get_intake_session(conn, SHOP)
    assert session["field"] == "qty"
    assert session["items_json"][0]["sell_cents"] == 1900  # shoplist kept


def test_answer_unrelated_returns_none(conn, shop):
    assert answer_intake_session(conn, SHOP, "hello there") is None
    start_guided_intake(conn, SHOP, [_vprod("Whey Protein 2kg", 1)], [],
                        "add to stock", "h6")
    assert answer_intake_session(conn, SHOP, "thank you very much") is None
    assert get_intake_session(conn, SHOP)["field"] == "cost"  # progress kept


def test_guided_skip_rejected_at_name(conn, shop):
    start_guided_intake(conn, SHOP,
                        [_vprod("", None, readable=False, notes="x")],
                        [], "add to stock", "hf1")
    assert get_intake_session(conn, SHOP)["field"] == "name"
    for dodge in ("skip", "don't know"):
        msg = answer_intake_session(conn, SHOP, dodge)
        assert "didn't catch that" in msg
        session = get_intake_session(conn, SHOP)
        assert session["field"] == "name"
        assert session["items_json"][0]["name"] == "?"  # never stored


def test_guided_question_at_name_clarifies(conn, shop):
    start_guided_intake(conn, SHOP,
                        [_vprod("", None, readable=False, notes="x")],
                        [], "add to stock", "hf3")
    msg = answer_intake_session(conn, SHOP, "what is this?")
    assert "didn't catch that" in msg
    assert get_intake_session(conn, SHOP)["field"] == "name"


def test_guided_skip_clarifies_at_which(conn, shop):
    start_guided_intake(conn, SHOP,
                        [_vprod("", None, readable=False, notes="x")],
                        [], "add to stock", "hf2a")
    answer_intake_session(conn, SHOP, "Bread")  # matches both demo breads
    assert get_intake_session(conn, SHOP)["field"] == "which"
    msg = answer_intake_session(conn, SHOP, "skip")
    assert "didn't catch that" in msg
    assert get_intake_session(conn, SHOP)["field"] == "which"


def test_guided_skip_clarifies_at_sell_unit(conn, shop):
    start_guided_intake(conn, SHOP, [_vprod("Whey Protein 2kg", None)],
                        [], "add to stock", "hf2b")
    answer_intake_session(conn, SHOP, "2 packs")
    answer_intake_session(conn, SHOP, "R32")
    answer_intake_session(conn, SHOP, "sell R45")
    assert get_intake_session(conn, SHOP)["field"] == "sell_unit"
    msg = answer_intake_session(conn, SHOP, "skip")
    assert "didn't catch that" in msg
    assert get_intake_session(conn, SHOP)["field"] == "sell_unit"


def test_guided_named_price_corrects_filled_field(conn, shop):
    start_guided_intake(conn, SHOP, [],
                        [{"product": "Sugar 2kg", "qty": 3,
                          "sell_cents": 6000}],
                        "sugar 3 sell price", "hf4")
    assert get_intake_session(conn, SHOP)["field"] == "cost"
    msg = answer_intake_session(conn, SHOP, "sell R25")
    item = get_intake_session(conn, SHOP)["items_json"][0]
    assert item["sell_cents"] == 2500  # named field corrected...
    assert item["cost_cents"] is None  # ...open field untouched
    assert get_intake_session(conn, SHOP)["field"] == "cost"  # re-asked
    assert "cost" in msg


def test_guided_skip_phrases_at_cost_sell(conn, shop):
    start_guided_intake(conn, SHOP, [_vprod("Whey Protein 2kg", 1)], [],
                        "add to stock", "hf5")
    msg = answer_intake_session(conn, SHOP, "I don't know")
    assert "selling price" in msg  # cost skipped, sell asked
    msg = answer_intake_session(conn, SHOP, "skip it")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    item = get_intake_session(conn, SHOP)["items_json"][0]
    assert item["cost_cents"] is None and item["sell_cents"] is None
