"""Seed a realistic random spaza shop for the owner's live number.

Idempotent: wipes and reseeds one shop. 25-30 everyday items across
categories with realistic SA prices, random quantities, a few low/out items,
near-expiry notes, credit customers and past sales.

Usage:
    python -m seed.seed_random
    python -m seed.seed_random --shop 27710000002 --seed 42
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import connect, init_db, now_iso, today_str  # noqa: E402
from seed.seed_demo import wipe_shop  # noqa: E402

DEMO_NUMBER = "27710000002"
SHOP_NAME = "Sisi Spaza"
WHOLESALER = "Jumbo Amanzimtoti"

CATEGORIES = ["Bread", "Dairy", "Drinks", "Groceries", "Household", "Airtime"]

# name, category, cost_low, cost_high, sell_low, sell_high (all cents, x50),
# pack (pack_name, pack_size, pack_cost_l, pack_cost_h, pack_sell_l, pack_sell_h) or None.
# singles costs/sells are per single unit; pack costs convert to cost per unit.
CATALOG = [
    ("Sasko White Bread 700g", "Bread", 1500, 1700, 1900, 2200, None),
    ("Albany Brown Bread 700g", "Bread", 1600, 1800, 2000, 2300, None),
    ("Bread Rolls", "Bread", 0, 0, 450, 450, ("packet", 4, 1300, 1600, 1900, 2200)),
    ("Fresh Milk 1L", "Dairy", 2200, 2500, 2700, 3200, None),
    ("Amasi 2L", "Dairy", 3400, 3800, 4200, 4800, None),
    ("Stoney 2L", "Drinks", 2700, 3000, 3400, 3800, None),
    ("Coke 2L", "Drinks", 2900, 3200, 3600, 4000, None),
    ("Orange Juice 2L", "Drinks", 3200, 3600, 4000, 4500, None),
    ("Iwisa Maize Meal 5kg", "Groceries", 7000, 8000, 8500, 10000, None),
    ("Iwisa Maize Meal 10kg", "Groceries", 13500, 15000, 16000, 18500, None),
    ("Sugar 2.5kg", "Groceries", 5200, 6000, 6500, 7500, None),
    ("Tastic Rice 2kg", "Groceries", 4800, 5500, 6000, 7000, None),
    ("Cooking Oil 750ml", "Groceries", 3000, 3500, 3800, 4500, None),
    ("Joko Tea 100s", "Groceries", 4400, 5000, 5500, 6500, None),
    ("Ricoffy 750g", "Groceries", 7800, 8800, 9500, 11000, None),
    ("Eggs", "Groceries", 0, 0, 350, 350, ("tray", 30, 8800, 9800, 10500, 11500)),
    ("Lucky Star Pilchards 400g", "Groceries", 2200, 2500, 2800, 3300, None),
    ("Koo Baked Beans 410g", "Groceries", 1900, 2200, 2500, 3000, None),
    ("Simba Chips 120g", "Groceries", 1800, 2050, 2300, 2700, None),
    ("Chappies", "Groceries", 0, 0, 100, 100, ("box", 100, 7800, 8800, 8500, 9500)),
    ("Table Salt 1kg", "Groceries", 800, 1000, 1200, 1600, None),
    ("Candles", "Household", 0, 0, 600, 600, ("packet", 6, 2700, 3300, 3800, 4200)),
    ("Matches", "Household", 0, 0, 200, 200, ("pack", 10, 1400, 1700, 2000, 2500)),
    ("Paraffin 1L", "Household", 2200, 2600, 2800, 3400, None),
    ("Washing Powder 2kg", "Household", 6000, 7000, 7500, 9000, None),
    ("Bath Soap", "Household", 1300, 1600, 1800, 2400, None),
    ("Toilet Paper 10s", "Household", 7600, 8600, 9500, 11000, None),
    ("Vodacom Airtime R30", "Airtime", 2800, 2900, 3000, 3000, None),
    ("MTN Airtime R30", "Airtime", 2800, 2900, 3000, 3000, None),
    ("Vodacom Data 1GB", "Airtime", 8300, 8500, 8900, 8900, None),
    ("MTN Data 1GB", "Airtime", 8300, 8500, 8900, 8900, None),
]

CREDIT_CUSTOMERS = ["Sipho", "Thandi", "Mama Zodwa", "Bongani"]

SHORT_LIFE = {"Fresh Milk 1L", "Amasi 2L", "Sasko White Bread 700g",
              "Albany Brown Bread 700g", "Bread Rolls", "Eggs", "Chappies"}


def _cents(rng: random.Random, low: int, high: int) -> int:
    return rng.randrange(low, high + 1, 50)


def seed_random_shop(shop_id: str = DEMO_NUMBER,
                     conn: sqlite3.Connection | None = None,
                     seed: int | None = None) -> str:
    rng = random.Random(seed)
    own = conn is None
    conn = conn or connect()
    try:
        init_db(conn)
        wipe_shop(conn, shop_id)
        ts = now_iso()
        today = date.fromisoformat(today_str())
        conn.execute(
            "INSERT INTO shops (shop_id, name, language) VALUES (?, ?, 'zu')",
            (shop_id, SHOP_NAME),
        )
        picked = rng.sample(CATALOG, rng.randint(25, 30))
        low_picks = set(rng.sample([p[0] for p in picked], 4))
        out_of_stock = rng.choice(sorted(low_picks))
        prod_ids: dict[str, int] = {}
        for i, (name, cat, cl, ch, sl, sh, pack) in enumerate(picked):
            if pack:
                pack_name, size, pcl, pch, psl, psh = pack
                pack_cost = _cents(rng, pcl, pch)
                cost, sell = pack_cost / size, _cents(rng, sl, sh)
                pack_price = _cents(rng, psl, psh)
                reorder = size // 2
            else:
                pack_name, size, pack_price = "", None, None
                cost, sell = _cents(rng, cl, ch), _cents(rng, sl, sh)
            if name == out_of_stock:
                qty = 0
                reorder = size // 2 if pack else rng.randint(4, 6)
            elif name in low_picks:
                qty = rng.randint(1, 2)
                reorder = size // 2 if pack else rng.randint(4, 6)
            elif pack:
                qty = rng.randint(size, 3 * size)
            elif cat == "Airtime":
                qty, reorder = rng.randint(6, 15), 3
            else:
                reorder = rng.randint(2, 6)
                qty = reorder + rng.randint(2, 14)
            cur = conn.execute(
                """INSERT INTO products (shop_id, name, price_cents, cost_cents,
                   stock_qty, reorder_level, wholesaler, category,
                   pack_name, pack_size, pack_price_cents)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (shop_id, name, sell, cost, qty, reorder, WHOLESALER, cat,
                 pack_name, size, pack_price),
            )
            prod_ids[name] = cur.lastrowid
            conn.execute(
                """INSERT INTO stock_moves (shop_id, product_id, delta_qty, reason,
                   action_id, created_at) VALUES (?, ?, ?, 'seed', ?, ?)""",
                (shop_id, cur.lastrowid, qty, f"seed-{shop_id}-stock-{i}", ts),
            )
        cust_ids: dict[str, int] = {}
        for name in CREDIT_CUSTOMERS:
            cur = conn.execute(
                "INSERT INTO customers (shop_id, name, created_at) VALUES (?, ?, ?)",
                (shop_id, name, ts),
            )
            cust_ids[name] = cur.lastrowid
        for i, name in enumerate(CREDIT_CUSTOMERS):
            amount = rng.randrange(2000, 15000, 50)
            aid = f"seed-{shop_id}-credit-{i}"
            conn.execute(
                """INSERT INTO credit_entries (shop_id, customer_id, amount_cents,
                   items_text, action_id, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
                (shop_id, cust_ids[name], amount, "assorted groceries", aid, ts),
            )
            conn.execute(
                """INSERT INTO actions (action_id, shop_id, kind, summary, data_json, created_at)
                   VALUES (?, ?, 'seed', ?, ?, ?)""",
                (aid, shop_id, f"seed credit {name}",
                 json.dumps({"customer": name, "amount_cents": amount}), ts),
            )
        names = list(prod_ids)
        sale_no = 0
        for back in (1, 2, 3):
            day = (today - timedelta(days=back)).isoformat()
            for _ in range(rng.randint(3, 6)):
                name = rng.choice(names)
                qty = rng.randint(1, 3)
                price = conn.execute("SELECT price_cents FROM products WHERE id = ?",
                                     (prod_ids[name],)).fetchone()["price_cents"]
                conn.execute(
                    """INSERT INTO sales (shop_id, product_id, product_name, qty,
                       total_cents, action_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (shop_id, prod_ids[name], name, qty, price * qty,
                     f"seed-{shop_id}-sale-{sale_no}", f"{day}T10:00:00.000Z"),
                )
                sale_no += 1
        fresh = [n for n in names if n in SHORT_LIFE] or names
        for name, ahead in zip(rng.sample(fresh, min(2, len(fresh))), (2, 5)):
            conn.execute(
                "INSERT INTO expiry_items (shop_id, product_name, qty, expiry_date, note)"
                " VALUES (?, ?, ?, ?, 'seed')",
                (shop_id, name, rng.randint(1, 4),
                 (today + timedelta(days=ahead)).isoformat()),
            )
        conn.commit()
        return shop_id
    finally:
        if own:
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a random demo shop.")
    parser.add_argument("--shop", default=DEMO_NUMBER)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()
    shop_id = seed_random_shop(args.shop, seed=args.seed)
    print(f"Seeded {SHOP_NAME} for shop {shop_id}")


if __name__ == "__main__":
    main()
