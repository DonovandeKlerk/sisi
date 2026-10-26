"""Seed script builds the fictional MaZulu's Spaza."""

from seed.seed_demo import DEMO_SHOP_ID, seed_demo


def test_seed_demo_shop(conn):
    seed_demo(DEMO_SHOP_ID, conn)
    shop = conn.execute("SELECT * FROM shops WHERE shop_id = ?", (DEMO_SHOP_ID,)).fetchone()
    assert shop["name"] == "MaZulu's Spaza"
    assert conn.execute("SELECT COUNT(*) c FROM customers WHERE shop_id = ?",
                        (DEMO_SHOP_ID,)).fetchone()["c"] == 6
    assert conn.execute("SELECT COUNT(*) c FROM products WHERE shop_id = ?",
                        (DEMO_SHOP_ID,)).fetchone()["c"] == 10
    credit_total = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) s FROM credit_entries WHERE shop_id = ?",
        (DEMO_SHOP_ID,)).fetchone()["s"]
    assert credit_total == 7300 + 4550 + 12000
    milk = conn.execute(
        "SELECT * FROM expiry_items WHERE shop_id = ? AND expiry_date = '2026-10-30'",
        (DEMO_SHOP_ID,)).fetchall()
    assert len(milk) == 1 and milk[0]["product_name"] == "Fresh Milk 1L"


def test_seed_is_idempotent(conn):
    seed_demo(DEMO_SHOP_ID, conn)
    seed_demo(DEMO_SHOP_ID, conn)
    assert conn.execute("SELECT COUNT(*) c FROM customers WHERE shop_id = ?",
                        (DEMO_SHOP_ID,)).fetchone()["c"] == 6
