"""Guided one-product-at-a-time intake (plan 2026-10-29).

Task 1: session store + item grounding (phantom fix).
Task 2: guided new-product flow (questions, answers, per-item preview).
Task 3: existing-product fast path + careful matching.
Task 5: conversation commands + photo/dup choices + agent wiring.
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


# Task 5: conversation commands --------------------------------------------


def test_back_skip_pause_resume_cancel(conn, shop):
    start_guided_intake(
        conn, SHOP,
        [_vprod("Whey Protein 2kg", 1), _vprod("Oreo Cookies 200g", 2)],
        [], "add to stock", "t5a")
    assert get_intake_session(conn, SHOP)["field"] == "cost"
    # back with nothing answered -> nothing to redo
    assert "Nothing to go back to" in \
        answer_intake_session(conn, SHOP, "back")
    answer_intake_session(conn, SHOP, "R16")  # cost -> sell asked
    assert get_intake_session(conn, SHOP)["field"] == "sell"
    msg = answer_intake_session(conn, SHOP, "back")  # redoes cost
    assert "cost" in msg.lower()
    assert get_intake_session(conn, SHOP)["field"] == "cost"
    assert get_intake_session(conn, SHOP)["items_json"][0]["cost_cents"] \
        is None
    # "skip item" skips the whole item even at a price field (never a
    # price skip)
    msg = answer_intake_session(conn, SHOP, "skip item")
    assert "Skipped" in msg and "Whey" in msg
    session = get_intake_session(conn, SHOP)
    assert session["current"] == 1
    assert session["items_json"][0]["state"] == "skipped"
    assert session["field"] == "cost"  # second item asked
    # pause parks the session; unrelated text is the planner's turn
    assert "Paused" in answer_intake_session(conn, SHOP, "pause")
    assert answer_intake_session(conn, SHOP,
                                 "how much cash do we have?") is None
    assert get_intake_session(conn, SHOP)["status"] == "paused"
    msg = answer_intake_session(conn, SHOP, "resume")
    assert "Carrying on" in msg and "cost" in msg.lower()
    assert get_intake_session(conn, SHOP)["status"] == "active"
    # cancel asks first; other text disarms; repeat confirms
    msg = answer_intake_session(conn, SHOP, "cancel")
    assert "cancel' again" in msg
    assert get_intake_session(conn, SHOP) is not None
    answer_intake_session(conn, SHOP, "R16")  # disarms + answers cost
    assert get_intake_session(conn, SHOP)["field"] == "sell"
    assert "cancel' again" in answer_intake_session(conn, SHOP, "cancel")
    msg = answer_intake_session(conn, SHOP, "cancel")
    assert "cancelled" in msg
    assert get_intake_session(conn, SHOP) is None
    # cancel all skips the question
    start_guided_intake(conn, SHOP, [_vprod("Whey Protein 2kg", 1)], [],
                        "add to stock", "t5b")
    msg = answer_intake_session(conn, SHOP, "cancel all")
    assert "cancelled" in msg
    assert get_intake_session(conn, SHOP) is None


def test_new_photo_asks_add_or_new(conn, shop, monkeypatch):
    from app import agent as agent_mod
    start_guided_intake(conn, SHOP, [_vprod("Whey Protein 2kg", 1)], [],
                        "add to stock", "t5c")
    monkeypatch.setattr(
        agent_mod, "read_shop_photo",
        lambda image, shop="": {"doc_type": "other",
                                "products": [_vprod("Oreo Cookies 200g", 2)]})
    _, reply, buttons = agent_mod.handle_photo(conn, SHOP, b"second-photo")
    assert "'add'" in reply and "'new'" in reply  # choice, not replace
    assert buttons is None
    session = get_intake_session(conn, SHOP)
    assert session["stash"].get("kind") == "photo"  # stashed, not merged
    assert len(session["items_json"]) == 1
    assert get_intake_session(conn, SHOP)["field"] == "cost"  # progress kept
    agent_mod.handle_text(conn, SHOP, "add")  # merges the stashed photo
    session = get_intake_session(conn, SHOP)
    assert [e["name"] for e in session["items_json"]] == \
        ["Whey Protein 2kg", "Oreo Cookies 200g"]
    assert not session["stash"].get("kind")  # stash consumed
    assert session["current"] == 0  # still on the first item


def test_new_photo_new_starts_over(conn, shop, monkeypatch):
    from app import agent as agent_mod
    start_guided_intake(conn, SHOP, [_vprod("Whey Protein 2kg", 1)], [],
                        "add to stock", "t5d")
    answer_intake_session(conn, SHOP, "R16")
    monkeypatch.setattr(
        agent_mod, "read_shop_photo",
        lambda image, shop="": {"doc_type": "other",
                                "products": [_vprod("Oreo Cookies 200g", 2)]})
    agent_mod.handle_photo(conn, SHOP, b"other-photo")
    agent_mod.handle_text(conn, SHOP, "new")  # restarts from the stash
    session = get_intake_session(conn, SHOP)
    assert [e["name"] for e in session["items_json"]] == ["Oreo Cookies 200g"]
    assert not session["stash"].get("kind")


def test_dup_photo_asks_restock(conn, shop, monkeypatch):
    import hashlib

    from app import agent as agent_mod
    from app.tools import record_photo
    record_photo(conn, SHOP, hashlib.sha256(b"dup-pic").hexdigest(), "stock")
    monkeypatch.setattr(
        agent_mod, "read_shop_photo",
        lambda image, shop="": {
            "doc_type": "other",
            "products": [_vprod("Sasko White Bread 700g", 2)]})
    _, reply, _ = agent_mod.handle_photo(conn, SHOP, b"dup-pic")
    assert "already saved" in reply and "restock" in reply  # ask first
    assert get_intake_session(conn, SHOP) is None  # nothing started yet
    agent_mod.handle_text(conn, SHOP, "restock")  # starts a guided session
    session = get_intake_session(conn, SHOP)
    assert session is not None
    assert session["items_json"][0]["name"] == "Sasko White Bread 700g"
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"


def test_unrelated_question_keeps_progress(conn, shop, monkeypatch):
    import json
    from types import SimpleNamespace

    import app.agent as agent_mod
    import app.interpreter as interp
    start_guided_intake(conn, SHOP, [_vprod("Whey Protein 2kg", 1)], [],
                        "add to stock", "t5e")
    before = json.dumps(get_intake_session(conn, SHOP), sort_keys=True)

    def fake(messages):
        body = {"understood": "cash question",
                "tasks": [{"kind": "cash", "args": {},
                           "quote": "cash"}],
                "question": None, "options": []}
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2))

    monkeypatch.setattr(interp, "propose_plan", fake)
    msg, _ = agent_mod.handle_text(conn, SHOP, "how much cash do we have?")
    assert "Still on item 1 of 1" in msg  # planner reply + resume line
    after = json.dumps(get_intake_session(conn, SHOP), sort_keys=True)
    assert after == before  # session byte-identical


def test_invalid_reply_clarifies_no_products(conn, shop):
    start_guided_intake(conn, SHOP, [_vprod("Whey Protein 2kg", 1)], [],
                        "add to stock", "t5f")
    assert get_intake_session(conn, SHOP)["field"] == "cost"
    msg = answer_intake_session(conn, SHOP, "yesterday maybe")
    assert "didn't catch that" in msg  # g_invalid; cost is skippable
    session = get_intake_session(conn, SHOP)
    assert session["field"] == "cost"  # re-asked, nothing stored
    assert session["items_json"][0]["cost_cents"] is None
    assert len(session["items_json"]) == 1  # no lines added


def test_invalid_reply_at_required_field_needs_it(conn, shop):
    start_guided_intake(conn, SHOP, [_vprod("Mystery Goods")], [],
                        "add to stock", "t5g")
    assert get_intake_session(conn, SHOP)["field"] == "qty"
    msg = answer_intake_session(conn, SHOP, "yesterday maybe")
    assert "really need" in msg  # g_required; qty cannot be skipped
    assert get_intake_session(conn, SHOP)["field"] == "qty"


def test_preview_correction_updates_and_reproposes(conn, shop):
    start_guided_intake(conn, SHOP,
                        [_vprod("Sasko White Bread 700g", 2,
                                readable=True)],
                        [{"product": "Sasko White Bread 700g"}],
                        "add to stock", "t5h")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    msg = answer_intake_session(conn, SHOP, "sell R25")  # at-preview fix
    assert "R25.00" in msg
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    assert get_intake_session(conn, SHOP)["items_json"][0]["sell_cents"] == 2500


def test_yes_delegates_to_session_confirm_qty(conn, shop):
    from app import agent as agent_mod
    start_guided_intake(conn, SHOP,
                        [_vprod("Sasko White Bread 700g", None)],
                        [{"product": "Sasko White Bread 700g"}],
                        "add to stock", "t5i")
    session = get_intake_session(conn, SHOP)
    session["items_json"][0]["seen_label"] = "2"
    save_intake_session(conn, SHOP, session)
    assert get_pending(conn, SHOP) is None  # nothing pending: Yes delegates
    msg, _ = agent_mod.handle_text(conn, SHOP, "yes")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    assert get_intake_session(conn, SHOP)["items_json"][0]["qty"] == 2.0
    assert "nothing waiting" not in msg.lower()


def test_start_replaces_session_and_item_pending(conn, shop):
    start_guided_intake(conn, SHOP,
                        [_vprod("Sasko White Bread 700g", 2)],
                        [{"product": "Sasko White Bread 700g"}],
                        "add to stock", "t5j")
    first = get_pending(conn, SHOP)
    assert first["action_type"] == "stock_intake_item"
    start_guided_intake(conn, SHOP, [_vprod("Whey Protein 2kg", 1)], [],
                        "add to stock", "t5k")
    assert get_intake_session(conn, SHOP)["items_json"][0]["name"] == \
        "Whey Protein 2kg"
    assert get_pending(conn, SHOP) is None  # live item preview superseded


def test_restock_without_stash_falls_through(conn, shop):
    start_guided_intake(conn, SHOP, [_vprod("Whey Protein 2kg", 1)], [],
                        "add to stock", "t5l")
    assert answer_intake_session(conn, SHOP, "restock") is None  # no stash
    assert get_intake_session(conn, SHOP)["field"] == "cost"  # untouched
    assert answer_intake_session(conn, "nope", "restock") is None


def test_same_photo_resend_reasks(conn, shop, monkeypatch):
    import hashlib

    from app import agent as agent_mod
    digest = hashlib.sha256(b"same-pic").hexdigest()
    start_guided_intake(conn, SHOP, [_vprod("Whey Protein 2kg", 1)], [],
                        "add to stock", digest)
    monkeypatch.setattr(
        agent_mod, "read_shop_photo",
        lambda image, shop="": {"doc_type": "other",
                                "products": [_vprod("Whey Protein 2kg", 1)]})
    _, reply, _ = agent_mod.handle_photo(conn, SHOP, b"same-pic")
    assert "cost" in reply.lower()  # current question re-asked
    assert not get_intake_session(conn, SHOP)["stash"].get("kind")  # no stash
    assert len(get_intake_session(conn, SHOP)["items_json"]) == 1


# Task 5 fix: first-photo routing ------------------------------------------


def _stub_plan(monkeypatch, tasks):
    import json
    from types import SimpleNamespace

    import app.interpreter as interp

    def fake(messages):
        body = {"understood": "photo fix test", "tasks": tasks,
                "question": None, "options": []}
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2))

    monkeypatch.setattr(interp, "propose_plan", fake)


def test_first_photo_starts_guided_session(conn, shop, monkeypatch):
    from app import agent as agent_mod
    monkeypatch.setattr(
        agent_mod, "read_shop_photo",
        lambda image, shop="": {"doc_type": "other",
                                "products": [_vprod("Whey Protein 2kg", 1)]})
    _stub_plan(monkeypatch, [{"kind": "stock_add",
                              "args": {"items": [{"product":
                                                  "Whey Protein 2kg"}]},
                              "quote": "add to stock"}])
    _, reply, _ = agent_mod.handle_photo(conn, SHOP, b"first-pic",
                                         "add to stock")
    assert "📦 *Stock intake*" not in reply  # guided, not one-shot
    assert "Found 1 items: Whey Protein 2kg." in reply  # overview
    assert "Item 1 of 1" in reply
    assert "cost" in reply.lower()  # first guided question
    session = get_intake_session(conn, SHOP)
    assert session is not None and session["field"] == "cost"
    assert get_pending(conn, SHOP) is None  # question asked, nothing pending
    msg, _ = agent_mod.handle_text(conn, SHOP, "R16")  # answer advances
    assert "selling price" in msg
    assert get_intake_session(conn, SHOP)["field"] == "sell"


def test_text_multi_item_keeps_oneshot(conn, shop, monkeypatch):
    from app import agent as agent_mod
    _stub_plan(monkeypatch, [{"kind": "stock_add",
                              "args": {"items": [{"product": "Bread"},
                                                 {"product": "Milk"}]},
                              "quote": "add bread and milk"}])
    msg, _ = agent_mod.handle_text(conn, SHOP,
                                   "add 2 bread and 3 milk to stock")
    assert "📦 *Stock intake*" in msg  # one-shot preview kept
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake"
    assert get_intake_session(conn, SHOP) is None  # no guided session


def test_photo_supersedes_live_oneshot(conn, shop, monkeypatch):
    from app import agent as agent_mod
    _stub_plan(monkeypatch, [{"kind": "stock_add",
                              "args": {"items": [{"product": "Bread"},
                                                 {"product": "Milk"}]},
                              "quote": "add bread and milk"}])
    agent_mod.handle_text(conn, SHOP, "add 2 bread and 3 milk to stock")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake"
    monkeypatch.setattr(
        agent_mod, "read_shop_photo",
        lambda image, shop="": {"doc_type": "other",
                                "products": [_vprod("Whey Protein 2kg", 1)]})
    _stub_plan(monkeypatch, [{"kind": "stock_add",
                              "args": {"items": [{"product":
                                                  "Whey Protein 2kg"}]},
                              "quote": "add to stock"}])
    agent_mod.handle_photo(conn, SHOP, b"fresh-pic", "add to stock")
    assert get_intake_session(conn, SHOP)["field"] == "cost"
    assert get_pending(conn, SHOP) is None  # stale one-shot can't catch Yes


# Task 6: per-item atomic save, undo, summary -------------------------------

def _stock(conn, name):
    row = conn.execute("SELECT stock_qty FROM products"
                       " WHERE shop_id = ? AND name = ?",
                       (SHOP, name)).fetchone()
    return row["stock_qty"] if row else None


def _drive_new_to_preview(conn, cost_answer, sell_answer):
    """Answer the open cost/sell questions of the current new item."""
    assert get_intake_session(conn, SHOP)["field"] == "cost"
    answer_intake_session(conn, SHOP, cost_answer)
    assert get_intake_session(conn, SHOP)["field"] == "sell"
    msg = answer_intake_session(conn, SHOP, sell_answer)
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    return msg


def test_item_save_advances_and_summary_ends(conn, shop):
    from app import agent as agent_mod
    start_guided_intake(
        conn, SHOP,
        [_vprod("Whey Protein 2kg", 1), _vprod("Oreo Cookies 200g", 2)],
        [], "add to stock", "t6a")
    _drive_new_to_preview(conn, "R16", "sell R21")
    msg, _ = agent_mod.handle_text(conn, SHOP, "yes")
    assert "Saved" in msg and "Whey Protein 2kg" in msg
    assert "Item 2 of 2" in msg  # advances with the next prompt
    assert _stock(conn, "Whey Protein 2kg") == 1
    session = get_intake_session(conn, SHOP)
    assert session is not None and session["current"] == 1
    assert session["items_json"][0]["state"] == "done"
    _drive_new_to_preview(conn, "R10", "sell R15")
    msg, _ = agent_mod.handle_text(conn, SHOP, "yes")
    assert "Saved" in msg and "Oreo Cookies 200g" in msg
    assert "Intake done" in msg  # final save appends the summary
    assert "2 saved" in msg and "0 skipped" in msg
    assert "Whey Protein 2kg" in msg and "Oreo Cookies 200g" in msg
    assert _stock(conn, "Oreo Cookies 200g") == 2
    assert get_intake_session(conn, SHOP) is None  # session done
    assert get_pending(conn, SHOP) is None


def test_undo_names_exact_item(conn, shop):
    from app import agent as agent_mod
    start_guided_intake(
        conn, SHOP,
        [_vprod("Whey Protein 2kg", 1), _vprod("Oreo Cookies 200g", 2)],
        [], "add to stock", "t6b")
    _drive_new_to_preview(conn, "R16", "sell R21")
    agent_mod.handle_text(conn, SHOP, "yes")
    _drive_new_to_preview(conn, "R10", "sell R15")
    agent_mod.handle_text(conn, SHOP, "yes")
    msg, _ = agent_mod.handle_text(conn, SHOP, "undo")
    assert "Oreo Cookies 200g" in msg  # names item 2, not item 1
    assert "Whey" not in msg
    assert _stock(conn, "Oreo Cookies 200g") is None  # new row removed
    assert _stock(conn, "Whey Protein 2kg") == 1  # item 1 stock intact


def test_no_double_save_on_repeated_yes(conn, shop):
    from app import agent as agent_mod
    start_guided_intake(conn, SHOP, [_vprod("Whey Protein 2kg", 1)], [],
                        "add to stock", "t6c")
    _drive_new_to_preview(conn, "R16", "sell R21")
    agent_mod.handle_text(conn, SHOP, "yes")
    assert _stock(conn, "Whey Protein 2kg") == 1
    msg, _ = agent_mod.handle_text(conn, SHOP, "yes")
    assert "nothing waiting" in msg.lower()
    assert _stock(conn, "Whey Protein 2kg") == 1  # unchanged


def test_restart_mid_item_resumes(tmp_path, monkeypatch):
    import sqlite3

    from app import agent as agent_mod
    from app.db import connect, init_db
    from app.setup import skip_setup
    db_path = tmp_path / "guided-restart.db"
    monkeypatch.setenv("SISI_DB", str(db_path))
    conn = connect(str(db_path))
    init_db(conn)
    seed_demo(SHOP, conn)
    skip_setup(conn, SHOP)
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?",
                 (SHOP,))
    conn.commit()
    start_guided_intake(
        conn, SHOP,
        [_vprod("Whey Protein 2kg", 1), _vprod("Oreo Cookies 200g", 2)],
        [], "add to stock", "t6d")
    _drive_new_to_preview(conn, "R16", "sell R21")
    agent_mod.handle_text(conn, SHOP, "yes")
    conn.close()  # worker restart before the next question renders
    conn2 = connect(str(db_path))
    try:
        session = get_intake_session(conn2, SHOP)
        assert session is not None and session["current"] == 1
        assert session["field"] == "cost"  # next prompt intact
        assert session["items_json"][0]["state"] == "done"
        _drive_new_to_preview(conn2, "R10", "sell R15")
        msg, _ = agent_mod.handle_text(conn2, SHOP, "yes")
        assert "Intake done" in msg
        assert _stock(conn2, "Whey Protein 2kg") == 1  # saved exactly once
        assert _stock(conn2, "Oreo Cookies 200g") == 2
    finally:
        conn2.close()


def test_no_cash_or_supplier_writes(conn, shop):
    from app import agent as agent_mod
    from app.tools import account_balance
    cash_before = account_balance(conn, SHOP, "cash")
    start_guided_intake(
        conn, SHOP,
        [_vprod("Whey Protein 2kg", 1),
         _vprod("Sasko White Bread 700g", 2, readable=True)],
        [{"product": "Whey Protein 2kg"}],
        "add to stock", "t6e")
    _drive_new_to_preview(conn, "R16", "sell R21")  # stated cost posts
    agent_mod.handle_text(conn, SHOP, "yes")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    agent_mod.handle_text(conn, SHOP, "yes")  # existing, no cost: posts nil
    moves = conn.execute("SELECT kind, amount_cents FROM cash_moves"
                         " WHERE shop_id = ? AND reversed = 0",
                         (SHOP,)).fetchall()
    assert [(m["kind"], m["amount_cents"]) for m in moves] == \
        [("purchase", -1600)]  # ONLY the confirmed cost posts
    assert account_balance(conn, SHOP, "cash") == cash_before - 1600
    assert conn.execute("SELECT COUNT(*) n FROM supplier_moves"
                        " WHERE shop_id = ?", (SHOP,)).fetchone()["n"] == 0


def test_no_skips_item_and_continues(conn, shop):
    from app import agent as agent_mod
    start_guided_intake(
        conn, SHOP,
        [_vprod("Whey Protein 2kg", 1), _vprod("Oreo Cookies 200g", 2)],
        [], "add to stock", "t6f")
    _drive_new_to_preview(conn, "R16", "sell R21")
    msg, _ = agent_mod.handle_text(conn, SHOP, "no")
    assert "Skipped" in msg and "Whey" in msg
    assert "Item 2 of 2" in msg  # continues, never a generic cancel
    assert _stock(conn, "Whey Protein 2kg") is None  # nothing saved
    assert get_intake_session(conn, SHOP)["current"] == 1
    _drive_new_to_preview(conn, "R10", "sell R15")
    msg, _ = agent_mod.handle_text(conn, SHOP, "yes")
    assert "1 saved" in msg and "1 skipped" in msg
    assert get_intake_session(conn, SHOP) is None


def test_both_save_writes_pack_and_single_prices(conn, shop):
    from app import agent as agent_mod
    start_guided_intake(conn, SHOP, [_vprod("Simba Chips Multipack", 2)],
                        [], "add to stock", "t6g")
    answer_intake_session(conn, SHOP, "R20")
    answer_intake_session(conn, SHOP, "skip")
    answer_intake_session(conn, SHOP, "both")
    answer_intake_session(conn, SHOP, "5")
    answer_intake_session(conn, SHOP, "per pack")
    answer_intake_session(conn, SHOP, "R25")
    answer_intake_session(conn, SHOP, "R6")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    msg, _ = agent_mod.handle_text(conn, SHOP, "yes")
    assert "Intake done" in msg
    row = conn.execute("SELECT price_cents, pack_price_cents, pack_name,"
                       " pack_size, stock_qty, cost_cents FROM products"
                       " WHERE shop_id = ? AND name = ?",
                       (SHOP, "Simba Chips Multipack")).fetchone()
    assert row["price_cents"] == 600  # single price
    assert row["pack_price_cents"] == 2500  # pack price
    assert row["pack_name"] == "pack" and row["pack_size"] == 5
    assert row["stock_qty"] == 10 and row["cost_cents"] == 400


def test_guided_confirm_records_photo(conn, shop):
    from app import agent as agent_mod
    start_guided_intake(conn, SHOP, [_vprod("Whey Protein 2kg", 1)], [],
                        "add to stock", "t6h")
    _drive_new_to_preview(conn, "R16", "sell R21")
    agent_mod.handle_text(conn, SHOP, "yes")
    row = conn.execute("SELECT kind FROM photo_hashes"
                       " WHERE shop_id = ? AND hash = ?",
                       (SHOP, "t6h")).fetchone()
    assert row is not None and row["kind"] == "stock"
