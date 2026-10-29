"""Guided one-product-at-a-time intake (plan 2026-10-29).

Task 1: session store + item grounding (phantom fix).
Task 2: guided new-product flow (questions, answers, per-item preview).
Task 3: existing-product fast path + careful matching.
"""

import pytest

from app.setup import skip_setup
from app.tools import (answer_intake_session, get_intake_session, get_pending,
                       ground_intake_items, guided_pack_totals,
                       save_intake_session, start_guided_intake)
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


def test_existing_confident_goes_straight_to_preview(conn, shop):
    start_guided_intake(conn, SHOP,
                        [_vprod("Sasko White Bread 700g", 2,
                                readable=True)],
                        [{"product": "Sasko White Bread 700g"}],
                        "add to stock", "h")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    assert get_intake_session(conn, SHOP)["field"] is None
    preview = get_pending(conn, SHOP)["preview_text"]
    assert "keeps shop price R18.50" in preview  # shoplist, not re-asked


def test_uncertain_count_confirms_first(conn, shop):
    msg = start_guided_intake(
        conn, SHOP, [_vprod("Sasko White Bread 700g", None)],
        [{"product": "Sasko White Bread 700g"}], "add to stock", "h")
    assert get_intake_session(conn, SHOP)["field"] == "qty"
    assert "Reply Yes" in msg  # confirm-qty phrasing, not plain ask


def test_explicit_sell_update_allowed_on_existing(conn, shop):
    start_guided_intake(conn, SHOP,
                        [_vprod("Sasko White Bread 700g", None)],
                        [{"product": "Sasko White Bread 700g"}],
                        "add to stock", "h")
    answer_intake_session(conn, SHOP, "sell at R25")
    item = get_intake_session(conn, SHOP)["items_json"][0]
    assert item["sell_cents"] == 2500  # explicit sell wins over shoplist
    assert item["sell_source"] == "stated"
    assert get_intake_session(conn, SHOP)["field"] == "qty"  # qty re-asked
    msg = answer_intake_session(conn, SHOP, "2")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    assert "R25" in msg and "you said" in msg  # preview shows the update


def test_existing_qty_answer_keeps_shoplist_price(conn, shop):
    start_guided_intake(conn, SHOP,
                        [_vprod("Sasko White Bread 700g", None)],
                        [{"product": "Sasko White Bread 700g"}],
                        "add to stock", "h")
    answer_intake_session(conn, SHOP, "3")
    item = get_intake_session(conn, SHOP)["items_json"][0]
    assert item["sell_cents"] == 1850  # never overwritten without sell words
    assert item["sell_source"] == "shoplist"
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"


def test_existing_qty_yes_approves_seen_count(conn, shop):
    start_guided_intake(conn, SHOP,
                        [_vprod("Sasko White Bread 700g", None)],
                        [{"product": "Sasko White Bread 700g"}],
                        "add to stock", "h")
    session = get_intake_session(conn, SHOP)
    session["items_json"][0]["seen_label"] = "2"  # staged seen count
    save_intake_session(conn, SHOP, session)
    answer_intake_session(conn, SHOP, "yes")
    item = get_intake_session(conn, SHOP)["items_json"][0]
    assert item["qty"] == 2.0
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"


def test_existing_qty_yes_without_seen_reasks(conn, shop):
    start_guided_intake(conn, SHOP,
                        [_vprod("Sasko White Bread 700g", None)],
                        [{"product": "Sasko White Bread 700g"}],
                        "add to stock", "h")
    msg = answer_intake_session(conn, SHOP, "yes")
    assert "didn't catch that" in msg  # nothing staged; never invent qty
    assert get_intake_session(conn, SHOP)["field"] == "qty"


def test_ambiguous_existing_resolves_then_previews(conn, shop):
    start_guided_intake(conn, SHOP, [_vprod("Sasko White Bread 700g", 2)],
                        [{"product": "Bread"}], "add to stock", "h")
    assert get_intake_session(conn, SHOP)["field"] == "which"
    answer_intake_session(conn, SHOP, "Sasko")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"


def test_multipack_singles_math(conn, shop):
    # 2 outer packs x 5 packets, R20 per outer pack, sell R6 per packet.
    start_guided_intake(conn, SHOP, [_vprod("Simba Chips Multipack", 2)],
                        [], "add to stock", "mp1")
    assert get_intake_session(conn, SHOP)["field"] == "cost"  # packs known
    answer_intake_session(conn, SHOP, "R20")
    assert get_intake_session(conn, SHOP)["field"] == "sell"
    answer_intake_session(conn, SHOP, "sell R6")
    assert get_intake_session(conn, SHOP)["field"] == "sell_unit"
    answer_intake_session(conn, SHOP, "singles")
    assert get_intake_session(conn, SHOP)["field"] == "per_pack"
    msg = answer_intake_session(conn, SHOP, "five in each pack")
    assert "for each pack or for one packet" in msg  # cost basis asked
    assert get_intake_session(conn, SHOP)["field"] == "cost_basis"
    msg = answer_intake_session(conn, SHOP, "each pack")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    line = get_intake_session(conn, SHOP)["items_json"][0]
    assert line["stock_units"] == 10 and line["cost_cents"] == 400
    assert line["sell_cents"] == 600 and line["cost_basis"] == "pack"
    assert "(10 singles)" in msg


def test_multipack_whole_is_two_units(conn, shop):
    start_guided_intake(conn, SHOP, [_vprod("Simba Chips Multipack", 2)],
                        [], "add to stock", "mp2")
    answer_intake_session(conn, SHOP, "R20")
    answer_intake_session(conn, SHOP, "sell R25")
    answer_intake_session(conn, SHOP, "whole")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    line = get_intake_session(conn, SHOP)["items_json"][0]
    assert line["stock_units"] == 2 and line["cost_cents"] == 2000
    totals = guided_pack_totals(line)
    assert totals["pack_price_cents"] == 2500 and totals["price_cents"] is None
    assert "per pack" in get_pending(conn, SHOP)["preview_text"]


def test_both_prices_collected_for_applier(conn, shop):
    # Task 6 owns the applier; this task owns questions, fields, preview.
    start_guided_intake(conn, SHOP, [_vprod("Simba Chips Multipack", 2)],
                        [], "add to stock", "mp3")
    answer_intake_session(conn, SHOP, "R20")
    answer_intake_session(conn, SHOP, "skip")
    answer_intake_session(conn, SHOP, "both")
    assert get_intake_session(conn, SHOP)["field"] == "per_pack"
    answer_intake_session(conn, SHOP, "5")
    assert get_intake_session(conn, SHOP)["field"] == "cost_basis"
    msg = answer_intake_session(conn, SHOP, "per pack")
    assert "per pack" in msg  # pack price asked via g_ask_sell + per
    assert get_intake_session(conn, SHOP)["field"] == "pack_sell"
    msg = answer_intake_session(conn, SHOP, "R25")
    assert "per packet" in msg  # single price re-asked
    assert get_intake_session(conn, SHOP)["field"] == "sell"
    msg = answer_intake_session(conn, SHOP, "R6")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    line = get_intake_session(conn, SHOP)["items_json"][0]
    assert line["sell_cents"] == 600 and line["pack_sell_cents"] == 2500
    totals = guided_pack_totals(line)
    assert totals["price_cents"] == 600
    assert totals["pack_price_cents"] == 2500
    assert totals["stock_units"] == 10 and totals["cost_cents"] == 400
    assert "R6.00 each" in msg and "R25.00 per pack" in msg


def test_total_cost_asks_basis(conn, shop):
    start_guided_intake(conn, SHOP, [_vprod("Simba Chips Multipack", 2)],
                        [], "add to stock", "mp4")
    msg = answer_intake_session(conn, SHOP, "R20 for both packs")
    assert "for each pack or for one packet" in msg
    item = get_intake_session(conn, SHOP)["items_json"][0]
    assert item["cost_cents"] is None  # assigned nothing
    assert get_intake_session(conn, SHOP)["field"] == "cost_basis"
    assert item["packs"] == 2  # "both" cross-filled
    answer_intake_session(conn, SHOP, "both together")
    item = get_intake_session(conn, SHOP)["items_json"][0]
    assert item["cost_cents"] == 2000 and item["cost_basis"] == "pack"
    assert get_intake_session(conn, SHOP)["field"] == "sell"
    answer_intake_session(conn, SHOP, "sell R6")
    answer_intake_session(conn, SHOP, "singles")
    answer_intake_session(conn, SHOP, "5")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    line = get_intake_session(conn, SHOP)["items_json"][0]
    assert line["stock_units"] == 10 and line["cost_cents"] == 200


def test_pack_context_carveout_existing(conn, shop):
    start_guided_intake(conn, SHOP, [_vprod("Eggs", 2)],
                        [{"product": "Eggs", "unit": "trays"}],
                        "add to stock", "mp5")
    session = get_intake_session(conn, SHOP)
    assert session["field"] == "sell_unit"  # not straight to preview
    assert get_pending(conn, SHOP) is None
    assert session["items_json"][0]["per_pack"] == 30  # known tray size
    msg = answer_intake_session(conn, SHOP, "singles")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    line = get_intake_session(conn, SHOP)["items_json"][0]
    assert line["stock_units"] == 60 and line["sell_cents"] == 350
    assert "keeps shop price" in msg


def test_packs_question_for_unseen_multipack(conn, shop):
    msg = start_guided_intake(conn, SHOP, [_vprod("Coke 6-pack", None)],
                              [], "add to stock", "mp6")
    assert get_intake_session(conn, SHOP)["field"] == "packs"
    assert "How many packs" in msg
    answer_intake_session(conn, SHOP, "2")
    assert get_intake_session(conn, SHOP)["field"] == "cost"
    answer_intake_session(conn, SHOP, "R60")
    answer_intake_session(conn, SHOP, "sell R12")
    answer_intake_session(conn, SHOP, "singles")
    assert get_intake_session(conn, SHOP)["field"] == "per_pack"
    answer_intake_session(conn, SHOP, "6 per pack")
    assert get_intake_session(conn, SHOP)["field"] == "cost_basis"
    answer_intake_session(conn, SHOP, "pack")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    line = get_intake_session(conn, SHOP)["items_json"][0]
    assert line["stock_units"] == 12 and line["cost_cents"] == 1000


def test_total_cost_pack_words_keep_split(conn, shop):
    start_guided_intake(conn, SHOP, [_vprod("Simba Chips Multipack", 2)],
                        [], "add to stock", "mp8")
    answer_intake_session(conn, SHOP, "R60 for both packs")
    answer_intake_session(conn, SHOP, "per pack")  # pack horn, total stands
    item = get_intake_session(conn, SHOP)["items_json"][0]
    assert item["cost_cents"] == 6000 and item["cost_basis"] == "pack"
    answer_intake_session(conn, SHOP, "sell R6")
    answer_intake_session(conn, SHOP, "singles")
    answer_intake_session(conn, SHOP, "5")
    line = get_intake_session(conn, SHOP)["items_json"][0]
    assert line["cost_cents"] == 600  # 6000/2/5, never tripled


def test_explicit_cost_basis_skips_basis_question(conn, shop):
    start_guided_intake(conn, SHOP, [_vprod("Simba Chips Multipack", 2)],
                        [], "add to stock", "mp7")
    answer_intake_session(conn, SHOP, "R20 per pack")
    item = get_intake_session(conn, SHOP)["items_json"][0]
    assert item["cost_basis"] == "pack"  # explicit, no later question
    assert item["unit"] == "pack"
    answer_intake_session(conn, SHOP, "sell R6")
    answer_intake_session(conn, SHOP, "singles")
    msg = answer_intake_session(conn, SHOP, "5")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    assert "for each pack or for one packet" not in msg
    line = get_intake_session(conn, SHOP)["items_json"][0]
    assert line["cost_cents"] == 400
