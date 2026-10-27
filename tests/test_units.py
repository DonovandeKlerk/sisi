"""Pack/single unit logic: bare numbers are singles, named packs convert."""

import pytest

from app.tools import _resolve_units

# (pack_name, pack_size) for a box-of-100 sweet
CHAPPIES = ("box", 100)


@pytest.mark.parametrize("unit", [None, "", "each", "loose", "singles", "5 single"])
def test_bare_or_single_words_stay_singles(unit):
    assert _resolve_units(*CHAPPIES, 5, unit) == (5, False, False)


@pytest.mark.parametrize("unit", ["box", "2 boxes", "a box of chappies", "ibhokisi",
                                  "amabhokisi", "tray", "a 6-pack", "crate"])
def test_pack_words_convert_to_units(unit):
    units, is_pack, warn = _resolve_units(*CHAPPIES, 2, unit)
    assert (units, is_pack, warn) == (200, True, False)


def test_product_pack_name_matches_even_when_unlisted():
    assert _resolve_units("sleeve", 12, 2, "2 sleeves") == (24, True, False)


def test_pack_word_on_packless_item_warns_and_counts_singles():
    assert _resolve_units("", None, 2, "2 boxes") == (2, False, True)


def test_unknown_word_assumes_singles():
    assert _resolve_units(*CHAPPIES, 2, "glasses") == (2, False, False)


# --- slice B: sales and credit use single units, packs convert ----------------

from app.tools import (  # noqa: E402
    add_credit,
    balance_cents,
    confirm_pending,
    get_balance,
    record_sale,
)

SHOP = "units-shop"


def _seed_units(conn):
    conn.execute("INSERT INTO shops (shop_id, name) VALUES ('units-shop', 'U')")
    conn.execute("INSERT INTO customers (shop_id, name) VALUES ('units-shop', 'Sipho')")
    conn.executemany(
        """INSERT INTO products (shop_id, name, price_cents, stock_qty, reorder_level,
           pack_name, pack_size, pack_price_cents)
           VALUES ('units-shop', ?, ?, ?, ?, ?, ?, ?)""",
        [
            ("Chappies", 100, 137, 50, "box", 100, 8500),
            ("Eggs", 350, 90, 30, "tray", 30, None),
            ("Bread", 2000, 12, 6, "", None, None),
            ("Sweets", None, 0, 10, "box", 50, None),
        ],
    )
    conn.commit()


def test_single_sale_prices_singles(conn):
    _seed_units(conn)
    msg = record_sale(conn, SHOP, "Chappies", 5)
    assert "• 5 × Chappies @ R1.00 = R5.00 cash" in msg
    saved = confirm_pending(conn, SHOP)
    assert "• 5 × Chappies @ R1.00 = R5.00" in saved
    row = conn.execute("SELECT stock_qty FROM products WHERE shop_id = ? AND name = 'Chappies'",
                       (SHOP,)).fetchone()
    assert row["stock_qty"] == 132


def test_pack_sale_uses_pack_price(conn):
    _seed_units(conn)
    msg = record_sale(conn, SHOP, "Chappies", 1, unit="box")
    assert "• 1 × Chappies box (100 units) @ R85.00 = R85.00" in msg
    confirm_pending(conn, SHOP)
    row = conn.execute("SELECT stock_qty FROM products WHERE shop_id = ? AND name = 'Chappies'",
                       (SHOP,)).fetchone()
    assert row["stock_qty"] == 37


def test_pack_sale_derives_price_when_missing(conn):
    _seed_units(conn)
    msg = record_sale(conn, SHOP, "Eggs", 1, unit="tray")
    assert "• 1 × Eggs tray (30 units) @ R105.00 = R105.00" in msg


def test_pack_word_on_packless_item_warns_in_preview(conn):
    _seed_units(conn)
    msg = record_sale(conn, SHOP, "Bread", 2, unit="2 boxes")
    assert "• 2 × Bread @ R20.00 = R40.00 cash" in msg
    assert "⚠️ No box size for Bread — counted as singles." in msg


def test_credit_mixes_singles_and_packs(conn):
    _seed_units(conn)
    msg = add_credit(conn, SHOP, "Sipho", [
        {"product": "Bread", "qty": 2},
        {"product": "Chappies", "qty": 5},
        {"product": "Chappies", "qty": 1, "unit": "box"},
    ])
    assert "• Sipho +R130.00 (2x Bread, 5x Chappies, 1x Chappies box)" in msg
    confirm_pending(conn, SHOP)
    assert get_balance(conn, SHOP, "Sipho") == "💳 *Sipho* owes R130.00."


def test_unpriced_item_asks_single_price(conn):
    _seed_units(conn)
    assert record_sale(conn, SHOP, "Sweets", 2) == "❓ What is the price of Sweets?"


# --- slice C: restock packs, cost conversion, pack pricing --------------------

from app.tools import add_stock, set_price, undo_last  # noqa: E402


def test_restock_packs_into_units(conn):
    _seed_units(conn)
    msg = add_stock(conn, SHOP, "Chappies", 2, unit="boxes", cost_cents=8500)
    assert "• +2 boxes Chappies (200 units)" in msg
    assert "• Stock 1 box + 37 loose (137) → 3 boxes + 37 loose (337) (cost R0.85)" in msg
    confirm_pending(conn, SHOP)
    row = conn.execute("SELECT stock_qty, cost_cents FROM products"
                       " WHERE shop_id = ? AND name = 'Chappies'", (SHOP,)).fetchone()
    assert row["stock_qty"] == 337 and row["cost_cents"] == 85.0


def test_restock_singles_cost_is_per_unit(conn):
    _seed_units(conn)
    msg = add_stock(conn, SHOP, "Chappies", 10, cost_cents=80)
    assert "• +10 Chappies" in msg and "(cost R0.80)" in msg
    confirm_pending(conn, SHOP)
    cost = conn.execute("SELECT cost_cents FROM products WHERE shop_id = ? AND name = 'Chappies'",
                        (SHOP,)).fetchone()["cost_cents"]
    assert cost == 80


def test_restock_can_set_pack_price(conn):
    _seed_units(conn)
    msg = add_stock(conn, SHOP, "Eggs", 1, unit="tray", cost_cents=9500,
                    pack_price_cents=11000)
    assert "(cost R3.17, tray R110.00)" in msg
    confirm_pending(conn, SHOP)
    row = conn.execute("SELECT stock_qty, pack_price_cents FROM products"
                       " WHERE shop_id = ? AND name = 'Eggs'", (SHOP,)).fetchone()
    assert (row["stock_qty"], row["pack_price_cents"]) == (120, 11000)
    undo_last(conn, SHOP)
    row = conn.execute("SELECT stock_qty, pack_price_cents FROM products"
                       " WHERE shop_id = ? AND name = 'Eggs'", (SHOP,)).fetchone()
    assert (row["stock_qty"], row["pack_price_cents"]) == (90, None)


def test_restock_unknown_pack_asks_for_singles(conn):
    _seed_units(conn)
    msg = add_stock(conn, SHOP, "Fizgers", 2, unit="box")
    assert msg == ("❓ *Fizgers isn't on the shelf list.*\n"
                   "Add it as singles first? e.g. 'add 200 fizgers'.")


def test_set_and_undo_pack_price(conn):
    _seed_units(conn)
    msg = set_price(conn, SHOP, "Eggs", 11000, for_pack=True)
    assert msg == "📝 *Set price?*\n• Eggs tray → R110.00"
    confirm_pending(conn, SHOP)
    assert undo_last(conn, SHOP) == "Undone: Eggs tray price removed."
    assert set_price(conn, SHOP, "Bread", 5000, for_pack=True) == \
        "❓ Bread has no pack size. Set the single price instead?"


# --- slice D: packs-plus-loose display ----------------------------------------

from app.tools import cash_up, list_stock, low_stock, restock_list  # noqa: E402


def test_stock_list_shows_packs_and_loose(conn):
    _seed_units(conn)
    out = list_stock(conn, SHOP)
    assert "• Chappies: 1 box + 37 loose (137) × R1.00 each" in out
    assert "• Eggs: 3 trays (90) × R3.50 each" in out
    assert "• Bread: 12 × R20.00" in out
    conn.execute("UPDATE products SET stock_qty = 37 WHERE shop_id = ? AND name = 'Chappies'",
                 (SHOP,))
    out = list_stock(conn, SHOP)
    assert "• Chappies: 37 × R1.00 each ⚠️" in out
    assert "• Chappies — 37 left" in out
    assert "• Sweets — out of stock ❌" in out


def test_low_stock_shows_packs(conn):
    _seed_units(conn)
    conn.execute("UPDATE products SET stock_qty = 37 WHERE shop_id = ? AND name = 'Chappies'",
                 (SHOP,))
    out = low_stock(conn, SHOP)
    assert "• Chappies — 37 left" in out


def test_restock_orders_whole_packs(conn):
    _seed_units(conn)
    conn.execute("UPDATE products SET stock_qty = 37 WHERE shop_id = ? AND name = 'Chappies'",
                 (SHOP,))
    conn.commit()
    out = restock_list(conn, SHOP)
    assert "• Chappies — have 37, order 1 box" in out


def test_sale_stock_left_shows_packs(conn):
    _seed_units(conn)
    msg = record_sale(conn, SHOP, "Chappies", 2)
    assert "• Stock left 1 box + 35 loose (135)" in msg
    saved = confirm_pending(conn, SHOP)
    assert "• Stock left 1 box + 35 loose (135)" in saved


def test_cash_up_totals_mix_packs_and_singles(conn):
    _seed_units(conn)
    record_sale(conn, SHOP, "Chappies", 1, unit="box")
    confirm_pending(conn, SHOP)
    record_sale(conn, SHOP, "Chappies", 5)
    confirm_pending(conn, SHOP)
    record_sale(conn, SHOP, "Bread", 2)
    confirm_pending(conn, SHOP)
    out = cash_up(conn, SHOP)
    assert "• Cash sales R130.00 (3 sales)" in out
    assert "Chappies — 105 sold, R90.00" in out


# --- slice E: pack-price replies, Zulu packs, tool defs ------------------------

from app.agent import handle_text  # noqa: E402
from app.tools import TOOL_DEFS  # noqa: E402


def test_pack_unit_ask_hints_pack_price(conn):
    _seed_units(conn)
    msg = record_sale(conn, SHOP, "Sweets", 1, unit="box")
    assert msg.startswith("❓ What is the price of Sweets?")
    assert "'box R60'" in msg


def test_pack_price_reply_sets_pack_and_derives_single(conn):
    _seed_units(conn)
    record_sale(conn, SHOP, "Sweets", 1, unit="box")
    preview, buttons = handle_text(conn, SHOP, "box R60")
    assert buttons == ["Yes", "No"]
    assert "• 1 × Sweets box (50 units) @ R60.00 = R60.00" in preview
    row = conn.execute("SELECT price_cents, pack_price_cents FROM products"
                       " WHERE shop_id = ? AND name = 'Sweets'", (SHOP,)).fetchone()
    assert (row["price_cents"], row["pack_price_cents"]) == (120, 6000)


def test_single_price_reply_derives_pack_price(conn):
    _seed_units(conn)
    record_sale(conn, SHOP, "Sweets", 1, unit="box")
    preview, _ = handle_text(conn, SHOP, "R1.20")
    assert "• 1 × Sweets box (50 units) @ R60.00 = R60.00" in preview


def test_pack_price_for_unknown_product_asks_singles(conn):
    _seed_units(conn)
    record_sale(conn, SHOP, "Fizgers", 1, unit="box")
    msg, buttons = handle_text(conn, SHOP, "box R60")
    assert buttons is None
    assert msg == ("❓ *Fizgers has no box size yet.*\n"
                   "Reply with the single price, e.g. 'fizgers R2'.")


def test_zulu_pack_words_end_to_end(conn):
    _seed_units(conn)
    assert "1 × Chappies box (100 units)" in record_sale(conn, SHOP, "Chappies", 1,
                                                         unit="ibhokisi")
    assert "1 × Eggs tray (30 units)" in record_sale(conn, SHOP, "Eggs", 1, unit="amatreyi")


def test_tool_defs_carry_unit_params():
    defs = {t["function"]["name"]: t["function"]["parameters"]["properties"] for t in TOOL_DEFS}
    assert "unit" in defs["record_sale"]
    assert "unit" in defs["add_credit"]["items"]["items"]["properties"]
    assert "unit" in defs["add_stock"] and "pack_price_cents" in defs["add_stock"]
    assert "for_pack" in defs["set_price"]


# --- slice F: reseeded pack catalogs -------------------------------------------

from seed.seed_demo import DEMO_SHOP_ID, seed_demo  # noqa: E402
from seed.seed_random import seed_random_shop  # noqa: E402


def _products_by_name(conn, shop):
    return {r["name"]: r for r in
            conn.execute("SELECT * FROM products WHERE shop_id = ?", (shop,)).fetchall()}


def test_random_packs_use_sensible_single_prices(conn):
    for s in range(5):
        shop = f"pk-{s}"
        seed_random_shop(shop, conn, seed=s)
        rows = _products_by_name(conn, shop)
        if "Chappies" in rows:
            c = rows["Chappies"]
            assert c["price_cents"] == 100
            assert (c["pack_name"], c["pack_size"]) == ("box", 100)
            assert c["reorder_level"] == 50
            assert c["pack_price_cents"] and c["cost_cents"] < 100
        if "Eggs" in rows:
            e = rows["Eggs"]
            assert e["price_cents"] == 350
            assert (e["pack_name"], e["pack_size"]) == ("tray", 30)
        if "Candles" in rows:
            assert rows["Candles"]["price_cents"] == 600
        if "Bread Rolls" in rows:
            assert rows["Bread Rolls"]["price_cents"] == 450


def test_random_catalog_has_no_pack_sized_names_or_cigarettes(conn):
    for s in range(10):
        shop = f"nc-{s}"
        seed_random_shop(shop, conn, seed=s)
        names = _products_by_name(conn, shop)
        for banned in ["Chappies 100s", "Eggs 30s Tray", "Loose Eggs each",
                       "Candles 6s", "Lion Matches 10s", "Bread Rolls 4s"]:
            assert banned not in names
        assert not [n for n in names if "cigarette" in n.lower()]


def test_demo_eggs_are_packed_trays(conn):
    seed_demo(DEMO_SHOP_ID, conn)
    rows = _products_by_name(conn, DEMO_SHOP_ID)
    assert "Eggs 30s Tray" not in rows
    eggs = rows["Eggs"]
    assert eggs["price_cents"] == 350
    assert (eggs["pack_name"], eggs["pack_size"]) == ("tray", 30)
    assert eggs["stock_qty"] == 90
    assert get_balance(conn, DEMO_SHOP_ID, "Jabu") == "💳 *Jabu* owes R120.00."
