"""Random Sisi Spaza seeder + per-number reset dispatch."""

from datetime import date, timedelta

from app.agent import try_deterministic
from app.db import today_str
from app.tools import best_sellers, demo_reset, list_debtors, low_stock, record_sale
from seed.seed_demo import DEMO_SHOP_ID, seed_demo
from seed.seed_random import CATEGORIES, DEMO_NUMBER, seed_random_shop

SHOP = DEMO_NUMBER


def _products(conn, shop=SHOP):
    return conn.execute("SELECT * FROM products WHERE shop_id = ?", (shop,)).fetchall()


def test_seeds_25_to_30_items(conn):
    seed_random_shop(SHOP, conn)
    assert 25 <= len(_products(conn)) <= 30


def test_categories_cover_shop_and_prices_have_margin(conn):
    seed_random_shop(SHOP, conn)
    rows = _products(conn)
    cats = {r["category"] for r in rows}
    assert len(cats) >= 5
    assert cats <= set(CATEGORIES)
    for r in rows:
        assert r["price_cents"] is not None and r["cost_cents"] is not None
        assert r["price_cents"] > r["cost_cents"] > 0
        assert r["price_cents"] % 50 == 0


def test_three_to_four_low_or_out(conn):
    seed_random_shop(SHOP, conn)
    flagged = [r for r in _products(conn) if r["stock_qty"] <= r["reorder_level"]]
    assert 3 <= len(flagged) <= 4
    assert any(r["stock_qty"] == 0 for r in flagged)
    assert "⚠️ *Low stock*" in low_stock(conn, SHOP)


def test_two_items_near_expiry(conn):
    seed_random_shop(SHOP, conn)
    soon = (date.fromisoformat(today_str()) + timedelta(days=7)).isoformat()
    rows = conn.execute("SELECT * FROM expiry_items WHERE shop_id = ? AND expiry_date <= ?",
                        (SHOP, soon)).fetchall()
    assert len(rows) == 2


def test_four_credit_customers_with_balances(conn):
    seed_random_shop(SHOP, conn, seed=7)
    out = list_debtors(conn, SHOP)
    for name in ["Sipho", "Thandi", "Mama Zodwa", "Bongani"]:
        assert name in out, name


def test_past_sales_across_days(conn):
    seed_random_shop(SHOP, conn)
    days = conn.execute("SELECT DISTINCT substr(created_at, 1, 10) d FROM sales"
                        " WHERE shop_id = ?", (SHOP,)).fetchall()
    assert len(days) >= 3
    assert "Top sellers" in best_sellers(conn, SHOP)


def test_single_wholesaler(conn):
    seed_random_shop(SHOP, conn)
    wholesalers = {r["wholesaler"] for r in _products(conn)}
    assert wholesalers == {"Jumbo Amanzimtoti"}


def test_reseed_is_random_but_repeatable(conn):
    seed_random_shop("num-a", conn, seed=42)
    first = {(r["name"], r["price_cents"]) for r in _products(conn, "num-a")}
    seed_random_shop("num-b", conn)  # unseeded: random
    other = {(r["name"], r["price_cents"]) for r in _products(conn, "num-b")}
    assert first != other
    seed_random_shop("num-a", conn, seed=42)
    assert {(r["name"], r["price_cents"]) for r in _products(conn, "num-a")} == first


def test_demo_reset_dispatches_per_number(conn):
    seed_demo(DEMO_SHOP_ID, conn)
    msg = demo_reset(conn, DEMO_NUMBER)
    assert msg.startswith("🏪 *Let's set up your shop.*")
    row = conn.execute("SELECT value FROM flags WHERE shop_id = ? AND key = 'setup-reseed'",
                       (DEMO_NUMBER,)).fetchone()
    assert row is not None
    msg = demo_reset(conn, DEMO_SHOP_ID)
    assert msg.startswith("🏪 *Let's set up your shop.*")
    row = conn.execute("SELECT value FROM flags WHERE shop_id = ? AND key = 'setup-reseed'",
                       (DEMO_SHOP_ID,)).fetchone()
    assert row is None


def test_reset_demo_command_for_live_number(conn):
    msg, buttons = try_deterministic(conn, DEMO_NUMBER, "reset demo")
    assert buttons is None and "Step 1 of 6" in msg


def test_reset_clears_await_price_and_conversation(conn):
    seed_random_shop(SHOP, conn)
    record_sale(conn, SHOP, "Unicorn Flakes", 1)
    conn.execute("INSERT INTO conversation_turns (shop_id, role, content)"
                 " VALUES (?, 'user', 'hi')", (SHOP,))
    demo_reset(conn, SHOP)
    assert conn.execute("SELECT COUNT(*) c FROM await_price WHERE shop_id = ?",
                        (SHOP,)).fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM conversation_turns WHERE shop_id = ?",
                        (SHOP,)).fetchone()["c"] == 0
