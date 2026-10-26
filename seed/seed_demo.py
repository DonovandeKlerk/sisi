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

# name, price_cents, stock_qty, reorder_level, wholesaler, unit
PRODUCTS = [
    ("Sasko White Bread 700g", 1850, 12, 6, "Devland", "loaf"),
    ("Albany Brown Bread 700g", 1900, 8, 6, "Devland", "loaf"),
    ("Iwisa Maize Meal 5kg", 9200, 6, 4, "Jumbo", "bag"),
    ("Tastic Rice 2kg", 6500, 2, 4, "Jumbo", "bag"),
    ("Lucky Star Pilchards 400g", 2850, 24, 12, "Devland", "tin"),
    ("Coke 2L", 3600, 5, 6, "Coke Depot", "bottle"),
    ("Stoney 2L", 3300, 8, 6, "Coke Depot", "bottle"),
    ("Simba Chips 120g", 2400, 3, 10, "Jumbo", "packet"),
    ("Fresh Milk 1L", 2700, 6, 6, "Devland", "bottle"),
    ("Eggs 30s Tray", 9500, 4, 2, "Jumbo", "tray"),
]

CUSTOMERS = ["Thandi", "Sipho", "Lerato", "Jabu", "Nomvula", "Bongani"]

# customer, amount_cents, items_text
CREDIT = [
    ("Sipho", 7300, "2x Sasko White Bread, 1x Coke 2L"),
    ("Thandi", 4550, "1x Tastic Rice 2kg (part), 1x Lucky Star"),
    ("Jabu", 12000, "1x Iwisa 5kg, 1x Eggs 30s (part)"),
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


def seed_demo(shop_id: str = DEMO_SHOP_ID, conn: sqlite3.Connection | None = None) -> str:
    own = conn is None
    conn = conn or connect()
    try:
        init_db(conn)
        wipe_shop(conn, shop_id)
        ts = now_iso()
        conn.execute(
            "INSERT INTO shops (shop_id, name, language) VALUES (?, ?, 'zu')",
            (shop_id, DEMO_SHOP_NAME),
        )
        cust_ids: dict[str, int] = {}
        for name in CUSTOMERS:
            cur = conn.execute(
                "INSERT INTO customers (shop_id, name, created_at) VALUES (?, ?, ?)",
                (shop_id, name, ts),
            )
            cust_ids[name] = cur.lastrowid
        prod_ids: dict[str, int] = {}
        for i, (name, price, qty, reorder, wholesaler, unit) in enumerate(PRODUCTS):
            cur = conn.execute(
                """INSERT INTO products (shop_id, name, price_cents, stock_qty,
                   reorder_level, wholesaler, unit) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (shop_id, name, price, qty, reorder, wholesaler, unit),
            )
            prod_ids[name] = cur.lastrowid
            conn.execute(
                """INSERT INTO stock_moves (shop_id, product_id, delta_qty, reason,
                   action_id, created_at) VALUES (?, ?, ?, 'seed', ?, ?)""",
                (shop_id, cur.lastrowid, qty, f"seed-stock-{i}", ts),
            )
        for i, (cust, amount, items) in enumerate(CREDIT):
            aid = f"seed-credit-{i}"
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the fictional demo shop.")
    parser.add_argument("--shop", default=DEMO_SHOP_ID)
    args = parser.parse_args()
    shop_id = seed_demo(args.shop)
    print(f"Seeded {DEMO_SHOP_NAME} for shop {shop_id}")


if __name__ == "__main__":
    main()
