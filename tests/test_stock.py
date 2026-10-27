"""Stock list, best sellers, priced restocks, category migration."""

import sqlite3

from app.db import init_db
from app.tools import (
    add_stock,
    best_sellers,
    confirm_pending,
    list_stock,
    record_sale,
    undo_last,
)

SHOP = "s1"


def _shop_with_products(conn):
    conn.execute("INSERT INTO shops (shop_id, name) VALUES ('s1', 'S')")
    conn.executemany(
        """INSERT INTO products (shop_id, name, price_cents, stock_qty, reorder_level,
           wholesaler, category) VALUES (?,?,?,?,?,?,?)""",
        [
            ("s1", "White Bread", 1950, 12, 6, "Jumbo", "Bread"),
            ("s1", "Brown Bread", 2000, 1, 6, "Jumbo", "Bread"),
            ("s1", "Coke 2L", 3600, 5, 6, "Jumbo", "Drinks"),
            ("s1", "Mystery Tin", None, 7, 2, "Jumbo", "Groceries"),
        ],
    )
    conn.commit()


def test_list_stock_grouped_by_category_with_low_flags(conn):
    from app.tools import list_stock as ls

    _shop_with_products(conn)
    out = ls(conn, SHOP)
    assert out.index("🍞 *Bread*") < out.index("🥤 *Drinks*") < out.index("🛒 *Groceries*")
    assert "• White Bread: 12 × R19.50" in out
    assert "• Brown Bread: 1 × R20.00 ⚠️" in out
    assert "• Coke 2L: 5 × R36.00 ⚠️" in out
    assert "• Mystery Tin: 7 (no price)" in out


def test_list_stock_empty_shop_shows_how_to_add(conn):
    conn.execute("INSERT INTO shops (shop_id, name) VALUES ('s1', 'S')")
    out = list_stock(conn, SHOP)
    assert "empty" in out and "add 20 bread at R18" in out


def test_best_sellers_ranked_by_revenue(conn):
    _shop_with_products(conn)
    record_sale(conn, SHOP, "Coke 2L", 3)
    confirm_pending(conn, SHOP)
    record_sale(conn, SHOP, "White Bread", 2)
    confirm_pending(conn, SHOP)
    out = best_sellers(conn, SHOP)
    assert "Coke 2L — 3 sold, R108.00" in out
    assert out.index("Coke 2L") < out.index("White Bread")


def test_best_sellers_empty(conn):
    _shop_with_products(conn)
    assert best_sellers(conn, SHOP) == "No sales in the last 7 days yet."


def test_add_stock_with_cost_and_sell_price(conn):
    _shop_with_products(conn)
    msg = add_stock(conn, SHOP, "White Bread", 20, cost_cents=1500, price_cents=1950)
    assert "• Stock 12 → 32 (cost R15.00, sell R19.50)" in msg
    confirm_pending(conn, SHOP)
    row = conn.execute("SELECT stock_qty, cost_cents, price_cents FROM products"
                       " WHERE shop_id = ? AND name = ?", (SHOP, "White Bread")).fetchone()
    assert (row["stock_qty"], row["cost_cents"], row["price_cents"]) == (32, 1500, 1950)


def test_add_stock_new_item_with_prices(conn):
    _shop_with_products(conn)
    msg = add_stock(conn, SHOP, "Simba", 10, cost_cents=1900, price_cents=2400)
    assert "• New: Simba × 10 (cost R19.00, sell R24.00)" in msg
    confirm_pending(conn, SHOP)
    assert undo_last(conn, SHOP) == "Undone: removed Simba again."


def test_add_stock_cost_above_sell_warns(conn):
    _shop_with_products(conn)
    msg = add_stock(conn, SHOP, "White Bread", 5, cost_cents=2000, price_cents=1950)
    assert "• ⚠️ Cost is higher than sell price." in msg


def test_add_stock_undo_restores_old_cost_and_price(conn):
    _shop_with_products(conn)
    conn.execute("UPDATE products SET cost_cents = 1500, price_cents = 1950"
                 " WHERE shop_id = ? AND name = ?", (SHOP, "White Bread"))
    conn.commit()
    add_stock(conn, SHOP, "White Bread", 5, cost_cents=1600, price_cents=2100)
    confirm_pending(conn, SHOP)
    undo_last(conn, SHOP)
    row = conn.execute("SELECT cost_cents, price_cents FROM products"
                       " WHERE shop_id = ? AND name = ?", (SHOP, "White Bread")).fetchone()
    assert (row["cost_cents"], row["price_cents"]) == (1500, 1950)


def test_category_column_migrates_old_db():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE products (id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " shop_id TEXT NOT NULL, name TEXT NOT NULL)")
    init_db(c)
    cols = [r[1] for r in c.execute("PRAGMA table_info(products)")]
    assert "category" in cols
    c.close()
