"""Seed the fictional demo shop "MaZulu's Spaza". Idempotent: wipes and reseeds one shop.

Usage:
    python -m seed.seed_demo
    python -m seed.seed_demo --shop 27710000001
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import connect, init_db, now_iso  # noqa: E402

DEMO_SHOP_ID = "27710000001"
DEMO_SHOP_NAME = "MaZulu's Spaza"

# name, price_cents, stock_qty, reorder_level, wholesaler, unit, category,
# pack_name, pack_size, pack_price_cents (stock and prices are single units)
PRODUCTS = [
    ("Sasko White Bread 700g", 1850, 12, 6, "Devland", "loaf", "Bread", "", None, None),
    ("Albany Brown Bread 700g", 1900, 8, 6, "Devland", "loaf", "Bread", "", None, None),
    ("Iwisa Maize Meal 5kg", 9200, 6, 4, "Jumbo", "bag", "Groceries", "", None, None),
    ("Tastic Rice 2kg", 6500, 2, 4, "Jumbo", "bag", "Groceries", "", None, None),
    ("Lucky Star Pilchards 400g", 2850, 24, 12, "Devland", "tin", "Groceries", "", None, None),
    ("Coke 2L", 3600, 5, 6, "Coke Depot", "bottle", "Drinks", "", None, None),
    ("Stoney 2L", 3300, 8, 6, "Coke Depot", "bottle", "Drinks", "", None, None),
    ("Simba Chips 120g", 2400, 3, 10, "Jumbo", "packet", "Groceries", "", None, None),
    ("Fresh Milk 1L", 2700, 6, 6, "Devland", "bottle", "Dairy", "", None, None),
    ("Eggs", 350, 90, 30, "Jumbo", "egg", "Groceries", "tray", 30, 9500),
]

CUSTOMERS = ["Thandi", "Sipho", "Lerato", "Jabu", "Nomvula", "Bongani"]


# customer, amount_cents, items_text
CREDIT = [
    ("Sipho", 7300, "2x Sasko White Bread, 1x Coke 2L"),
    ("Thandi", 4550, "1x Tastic Rice 2kg (part), 1x Lucky Star"),
    ("Jabu", 12000, "1x Iwisa 5kg, 8x Eggs (part)"),
]

# product_name, qty, expiry_date, note
EXPIRY = [
    ("Fresh Milk 1L", 2, "2026-10-30", "fridge, front row"),
    ("Sasko White Bread 700g", 3, "2026-10-28", "shelf, check daily"),
    ("Fresh Milk 1L", 4, "2026-11-05", "fridge, back row"),
]


def wipe_shop(conn: sqlite3.Connection, shop_id: str) -> None:
    for table in (
        "conversation_turns",
        "messages_seen",
        "pending_actions",
        "await_price",
        "reports",
        "flags",
        "staff",
        "receipts",
        "cash_moves",
        "expenses",
        "supplier_moves",
        "order_drafts",
        "photo_hashes",
        "expiry_items",
        "stock_moves",
        "sales",
        "payments",
        "credit_entries",
        "products",
        "customers",
        "actions",
        "shops",
    ):
        conn.execute(f"DELETE FROM {table} WHERE shop_id = ?", (shop_id,))


def seed_demo(shop_id: str = DEMO_SHOP_ID, conn: sqlite3.Connection | None = None,
             language: str = "zu") -> str:
    own = conn is None
    conn = conn or connect()
    try:
        init_db(conn)
        wipe_shop(conn, shop_id)
        ts = now_iso()
        lang = (language or "en").strip().lower() or "en"
        conn.execute(
            "INSERT INTO shops (shop_id, name, language) VALUES (?, ?, ?)",
            (shop_id, DEMO_SHOP_NAME, lang),
        )
        cust_ids: dict[str, int] = {}
        for name in CUSTOMERS:
            cur = conn.execute(
                "INSERT INTO customers (shop_id, name, created_at) VALUES (?, ?, ?)",
                (shop_id, name, ts),
            )
            cust_ids[name] = cur.lastrowid
        prod_ids: dict[str, int] = {}
        for i, (name, price, qty, reorder, wholesaler, unit, category,
                  pack_name, pack_size, pack_price) in enumerate(PRODUCTS):
            cur = conn.execute(
                """INSERT INTO products (shop_id, name, price_cents, stock_qty,
                   reorder_level, wholesaler, unit, category,
                   pack_name, pack_size, pack_price_cents)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (shop_id, name, price, qty, reorder, wholesaler, unit, category,
                 pack_name, pack_size, pack_price),
            )
            prod_ids[name] = cur.lastrowid
            conn.execute(
                """INSERT INTO stock_moves (shop_id, product_id, delta_qty, reason,
                   action_id, created_at) VALUES (?, ?, ?, 'seed', ?, ?)""",
                (shop_id, cur.lastrowid, qty, f"seed-{shop_id}-stock-{i}", ts),
            )
        for i, (cust, amount, items) in enumerate(CREDIT):
            aid = f"seed-{shop_id}-credit-{i}"
            conn.execute(
                """INSERT INTO credit_entries (shop_id, customer_id, amount_cents,
                   items_text, action_id, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
                (shop_id, cust_ids[cust], amount, items, aid, ts),
            )
            conn.execute(
                """INSERT INTO actions (action_id, shop_id, kind, summary, data_json, created_at)
                   VALUES (?, ?, 'seed', ?, ?, ?)""",
                (aid, shop_id, f"seed credit {cust}",
                 json.dumps({"customer": cust, "amount_cents": amount}), ts),
            )
        for name, qty, exp, note in EXPIRY:
            conn.execute(
                """INSERT INTO expiry_items (shop_id, product_name, qty, expiry_date, note)
                   VALUES (?, ?, ?, ?, ?)""",
                (shop_id, name, qty, exp, note),
            )
        conn.commit()
        return shop_id
    finally:
        if own:
            conn.close()


def add_demo_activity(conn: sqlite3.Connection, shop_id: str) -> None:
    """Add today's cash/card sales + a payment so the overview card is rich.

    Called for join-code demo shops only; the base seed stays lean for tests.
    """
    ts = now_iso()
    prods = {r["name"]: r["id"] for r in conn.execute(
        "SELECT id, name FROM products WHERE shop_id = ?", (shop_id,)).fetchall()}
    cust = {r["name"]: r["id"] for r in conn.execute(
        "SELECT id, name FROM customers WHERE shop_id = ?", (shop_id,)).fetchall()}
    sample_sales = [
        ("Sasko White Bread 700g", 2, 3700, "cash"),
        ("Coke 2L", 1, 3600, "cash"),
        ("Lucky Star Pilchards 400g", 3, 8550, "card"),
        ("Eggs", 12, 4200, "cash"),
    ]
    for i, (pname, qty, total, method) in enumerate(sample_sales):
        pid = prods.get(pname)
        if pid is None:
            continue
        aid = f"seed-{shop_id}-sale-{i}"
        # Skip if already applied (idempotent-ish for re-runs).
        if conn.execute("SELECT 1 FROM actions WHERE action_id = ?",
                        (aid,)).fetchone():
            continue
        conn.execute(
            """INSERT INTO sales (shop_id, product_id, product_name, qty,
               total_cents, pay_method, action_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (shop_id, pid, pname, qty, total, method, aid, ts),
        )
        conn.execute(
            """INSERT INTO stock_moves (shop_id, product_id, delta_qty, reason,
               action_id, created_at) VALUES (?, ?, ?, 'sale', ?, ?)""",
            (shop_id, pid, -qty, aid, ts),
        )
        conn.execute(
            "UPDATE products SET stock_qty = stock_qty - ? WHERE id = ?",
            (qty, pid),
        )
        conn.execute(
            """INSERT INTO actions (action_id, shop_id, kind, summary, data_json, created_at)
               VALUES (?, ?, 'sale', ?, ?, ?)""",
            (aid, shop_id, f"seed sale {pname}",
             json.dumps({"product": pname, "qty": qty, "total_cents": total}), ts),
        )
    if "Sipho" in cust:
        paid = 2000
        aid = f"seed-{shop_id}-pay-0"
        if not conn.execute("SELECT 1 FROM actions WHERE action_id = ?",
                            (aid,)).fetchone():
            conn.execute(
                """INSERT INTO payments (shop_id, customer_id, amount_cents,
                   action_id, created_at) VALUES (?, ?, ?, ?, ?)""",
                (shop_id, cust["Sipho"], paid, aid, ts),
            )
            conn.execute(
                """INSERT INTO actions (action_id, shop_id, kind, summary, data_json, created_at)
                   VALUES (?, ?, 'payment', ?, ?, ?)""",
                (aid, shop_id, "seed payment Sipho",
                 json.dumps({"amount_cents": paid}), ts),
            )
    conn.commit()



def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the fictional demo shop.")
    parser.add_argument("--shop", default=DEMO_SHOP_ID)
    args = parser.parse_args()
    shop_id = seed_demo(args.shop)
    print(f"Seeded {DEMO_SHOP_NAME} for shop {shop_id}")


if __name__ == "__main__":
    main()
