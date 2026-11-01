"""Reset + reseed the TEST shop only (Modal run, never deployed).

Usage (from repo root):
    .venv/bin/modal run scripts/reseed_test_shop.py::main --confirm RESET-BONGI --shop <live-shop>

Wipes ALL business data for shop 27710000002 (Bongi's Spaza Shop) and
reseeds a small deterministic demo dataset. REFUSES to touch any other
shop. The shops row (name, language, channel, logo paths) and logo files
are preserved; everything else for this shop is deleted, including
send_queue rows, volume report files and reminders.
"""

import modal

image = (
    modal.Image.debian_slim()
    .pip_install_from_requirements("requirements.txt")
    .add_local_dir("app", remote_path="/root/app",
                   ignore=["__pycache__", "*.pyc", "*.pyo"])
    .add_local_dir("seed", remote_path="/root/seed",
                   ignore=["__pycache__", "*.pyc", "*.pyo"])
)

app = modal.App(
    "sisi-reseed",
    image=image,
    secrets=[modal.secret.Secret.from_name("sisi-env")],
    volumes={"/data": modal.volume.Volume.from_name("sisi-data")},
)

DEFAULT_SHOP = "27710000002"
TEST_NAME = "Bongi’s Spaza Shop"  # curly apostrophe, as stored
CONFIRM = "RESET-BONGI"

# name, price, cost, stock, reorder, wholesaler, unit, category,
# pack_name, pack_size, pack_price
PRODUCTS = [
    ("Albany Brown Bread 700g", 2150, 1650, 14, 6, "Devland Cash & Carry", "loaf", "Bread", "", None, None),
    ("Sasko White Bread 700g", 2100, 1600, 12, 6, "Devland Cash & Carry", "loaf", "Bread", "", None, None),
    ("Fresh Milk 1L", 2800, 2300, 9, 6, "Devland Cash & Carry", "bottle", "Dairy", "", None, None),
    ("Coke 2L", 3600, 3000, 10, 6, "Jumbo Amanzimtoti", "bottle", "Drinks", "", None, None),
    ("Stoney 2L", 3300, 2700, 8, 6, "Jumbo Amanzimtoti", "bottle", "Drinks", "", None, None),
    ("Eggs", 350, 300, 60, 30, "Jumbo Amanzimtoti", "egg", "Groceries", "tray", 30, 9500),
    ("Chappies", 100, 90, 100, 40, "Jumbo Amanzimtoti", "piece", "Groceries", "box", 100, 9000),
    ("Iwisa Maize Meal 5kg", 9200, 7800, 6, 3, "Jumbo Amanzimtoti", "bag", "Groceries", "", None, None),
    ("Tastic Rice 2kg", 6500, 5400, 2, 4, "Jumbo Amanzimtoti", "bag", "Groceries", "", None, None),
    ("Lucky Star Pilchards 400g", 2900, 2400, 18, 10, "Devland Cash & Carry", "tin", "Groceries", "", None, None),
    ("Simba Chips 120g", 2400, 2000, 15, 8, "Jumbo Amanzimtoti", "packet", "Groceries", "", None, None),
    ("Ricoffy 750g", 10500, 8900, 4, 2, "Devland Cash & Carry", "jar", "Groceries", "", None, None),
    ("Candles", 600, 500, 24, 12, "Jumbo Amanzimtoti", "candle", "Household", "packet", 6, 4000),
    ("Paraffin 1L", 3000, 2500, 7, 4, "Jumbo Amanzimtoti", "bottle", "Household", "", None, None),
    ("Toilet Paper 10s", 10500, 8900, 5, 3, "Devland Cash & Carry", "pack", "Household", "", None, None),
    ("Vodacom Airtime R30", 3000, 2850, 20, 5, "Jumbo Amanzimtoti", "voucher", "Airtime", "", None, None),
]

CUSTOMERS = ["Sipho", "Thandi", "Mama Zodwa", "Bongani", "Lerato", "Jabu", "Nomvula"]

# customer, amount_cents, items_text
CREDIT = [
    ("Sipho", 6800, "2x Coke 2L (part), 1x Simba Chips 120g"),
    ("Thandi", 12500, "2x Iwisa 5kg (part), 1x Lucky Star"),
    ("Mama Zodwa", 18400, "groceries, October"),
    ("Bongani", 4500, "3x Albany Brown Bread, 2x Eggs"),
    ("Lerato", 3200, "1x Paraffin 1L (part)"),
    ("Nomvula", 9950, "1x Ricoffy 750g (part), 1x Tastic Rice 2kg (part)"),
]

# customer, amount_cents (credit collected during the 5-day window)
PAYMENTS = [("Thandi", 5000), ("Mama Zodwa", 6000), ("Lerato", 3200)]

# day_offset (-5..-1), method, product, qty. Prices come from PRODUCTS.
SALES = [
    (-5, "cash", "Albany Brown Bread 700g", 3), (-5, "cash", "Fresh Milk 1L", 2),
    (-5, "cash", "Coke 2L", 1), (-5, "cash", "Eggs", 10),
    (-5, "card", "Iwisa Maize Meal 5kg", 1),
    (-4, "cash", "Sasko White Bread 700g", 2), (-4, "cash", "Stoney 2L", 2),
    (-4, "cash", "Chappies", 15), (-4, "cash", "Lucky Star Pilchards 400g", 1),
    (-4, "card", "Tastic Rice 2kg", 1),
    (-3, "cash", "Albany Brown Bread 700g", 2), (-3, "cash", "Simba Chips 120g", 3),
    (-3, "cash", "Fresh Milk 1L", 1), (-3, "cash", "Candles", 2),
    (-3, "card", "Ricoffy 750g", 1),
    (-2, "cash", "Coke 2L", 2), (-2, "cash", "Eggs", 6),
    (-2, "cash", "Paraffin 1L", 1), (-2, "cash", "Vodacom Airtime R30", 2),
    (-2, "card", "Toilet Paper 10s", 1),
    (-1, "cash", "Sasko White Bread 700g", 3), (-1, "cash", "Stoney 2L", 1),
    (-1, "cash", "Chappies", 10), (-1, "cash", "Lucky Star Pilchards 400g", 2),
    (-1, "card", "Iwisa Maize Meal 5kg", 1),
]

EXTRA_WIPE_TABLES = (
    "task_plans", "clarify_context", "await_reminder", "reminders",
    "intake_sessions", "error_reports",
)


@app.function(timeout=300)
def reseed(confirm: str = "", shop: str = "") -> str:
    shop_id = (shop or DEFAULT_SHOP).strip()  # live id via --shop, never git
    import json
    import os
    from datetime import date, timedelta

    if confirm != CONFIRM:
        return "REFUSED: pass --confirm RESET-BONGI"

    os.environ["SISI_DB"] = "/data/sisi.db"
    os.environ["SISI_REPORTS"] = "/data/reports"
    os.environ["SISI_LOGOS"] = "/data/logos"
    from app.db import connect
    from seed.seed_demo import wipe_shop

    conn = connect()
    try:
        cols = ["shop_id", "name", "language", "address", "phone",
                "logo_path", "logo_orig_path", "channel", "consent_given_at",
                "created_at"]
        shop = conn.execute(
            f"SELECT {', '.join(cols)} FROM shops WHERE shop_id = ?",
            (shop_id,)).fetchone()
        if shop is None:
            return f"REFUSED: shop {shop_id} does not exist"
        if shop["name"] != TEST_NAME:
            return (f"REFUSED: shop {shop_id} is named {shop['name']!r},"
                    f" expected {TEST_NAME!r}")
        snap = {c: shop[c] for c in cols}
        out = [f"shop: {shop_id} ({snap['name']}) lang={snap['language']}"
               f" ch={snap['channel'] or '-'} logo={snap['logo_path'] or '-'}"]

        for table in EXTRA_WIPE_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE shop_id = ?", (shop_id,))
        conn.execute("DELETE FROM send_queue WHERE to_number = ?", (shop_id,))
        conn.execute("DELETE FROM send_pacing WHERE to_number = ?", (shop_id,))
        wipe_shop(conn, shop_id)
        # Restore the shops row exactly (name, language, logo, consent).
        # Idempotent: a failed run may have deleted everything but shops.
        have = conn.execute("SELECT 1 FROM shops WHERE shop_id = ?",
                            (shop_id,)).fetchone()
        if have is None:
            conn.execute(f"INSERT INTO shops ({', '.join(cols)}) VALUES "
                         f"({', '.join('?' * len(cols))})",
                         tuple(snap[c] for c in cols))
        else:
            conn.execute(f"UPDATE shops SET {', '.join(f'{c} = ?' for c in cols if c != 'shop_id')}"
                         " WHERE shop_id = ?",
                         tuple(snap[c] for c in cols if c != 'shop_id') + (shop_id,))

        # Stale volume files for this shop only; logos are never touched.
        from pathlib import Path
        safe = "".join(c for c in shop_id if c.isdigit())
        reports_dir = Path("/data/reports")
        gone = 0
        if reports_dir.exists():
            for p in reports_dir.glob(f"{safe}-*"):
                if p.is_file():
                    p.unlink()
                    gone += 1
        out.append(f"wiped business data + {gone} report files; shop row + logo kept")

        today = date(2026, 11, 1)  # freeze the demo window: sales 27-31 Oct
        ts = "2026-11-01T06:00:00.000Z"
        prices: dict[str, int] = {}
        for i, (name, price, cost, qty, reorder, wholesaler, unit, cat,
                  pack_name, pack_size, pack_price) in enumerate(PRODUCTS):
            cur = conn.execute(
                """INSERT INTO products (shop_id, name, price_cents, cost_cents,
                   stock_qty, reorder_level, wholesaler, unit, category,
                   pack_name, pack_size, pack_price_cents)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (shop_id, name, price, cost, qty, reorder, wholesaler, unit,
                 cat, pack_name, pack_size, pack_price))
            prices[name] = price
            conn.execute(
                """INSERT INTO stock_moves (shop_id, product_id, delta_qty, reason,
                   action_id, created_at) VALUES (?, ?, ?, 'seed', ?, ?)""",
                (shop_id, cur.lastrowid, qty, f"reseed-stock-{i}", ts))
        cust_ids: dict[str, int] = {}
        for name in CUSTOMERS:
            cur = conn.execute(
                "INSERT INTO customers (shop_id, name, created_at) VALUES (?, ?, ?)",
                (shop_id, name, ts))
            cust_ids[name] = cur.lastrowid
        for i, (cust, amount, items) in enumerate(CREDIT):
            aid = f"reseed-credit-{i}"
            conn.execute(
                """INSERT INTO credit_entries (shop_id, customer_id, amount_cents,
                   items_text, action_id, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
                (shop_id, cust_ids[cust], amount, items, aid,
                 "2026-10-26T10:00:00.000Z"))
            conn.execute(
                """INSERT INTO actions (action_id, shop_id, kind, summary, data_json,
                   created_at) VALUES (?, ?, 'seed', ?, ?, ?)""",
                (aid, shop_id, f"seed credit {cust}",
                 json.dumps({"customer": cust, "amount_cents": amount}),
                 "2026-10-26T10:00:00.000Z"))

        move_no = sale_no = 0

        def _move(account, amount, kind, day_ts, category="", note=""):
            nonlocal move_no
            conn.execute(
                """INSERT INTO cash_moves (shop_id, account, amount_cents, kind,
                   category, note, action_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (shop_id, account, amount, kind, category, note,
                 f"reseed-move-{move_no}", day_ts))
            move_no += 1

        d5 = today - timedelta(days=5)
        _move("cash", 200000, "float", f"{d5}T08:00:00.000Z", note="opening float")
        _move("petty", 20000, "float", f"{d5}T08:00:00.000Z", note="opening float")
        for off, method, name, qty in SALES:
            day = today + timedelta(days=off)
            day_ts = f"{day}T{10 + (sale_no % 6)}:00:00.000Z"
            total = prices[name] * qty
            prod_id = conn.execute(
                "SELECT id FROM products WHERE shop_id = ? AND name = ?",
                (shop_id, name)).fetchone()["id"]
            aid = f"reseed-sale-{sale_no}"
            conn.execute(
                """INSERT INTO sales (shop_id, product_id, product_name, qty,
                   total_cents, action_id, created_at, pay_method)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (shop_id, prod_id, name, qty, total, aid, day_ts, method))
            _move("bank" if method == "card" else "cash", total, "sale",
                  day_ts, note=name)
            sale_no += 1
        for i, (cust, amount) in enumerate(PAYMENTS):
            day = today - timedelta(days=3 - i)
            day_ts = f"{day}T15:00:00.000Z"
            conn.execute(
                """INSERT INTO payments (shop_id, customer_id, amount_cents,
                   action_id, created_at) VALUES (?, ?, ?, ?, ?)""",
                (shop_id, cust_ids[cust], amount, f"reseed-pay-{i}", day_ts))
            _move("cash", amount, "credit_payment", day_ts, note=cust)
        # Suppliers: Jumbo delivery on account + part payment; Devland delivery.
        d4 = today - timedelta(days=4)
        conn.execute(
            """INSERT INTO supplier_moves (shop_id, supplier, amount_cents,
               action_id, created_at) VALUES (?, ?, ?, ?, ?)""",
            (shop_id, "Jumbo Amanzimtoti", 68000, "reseed-sup-0",
             f"{d4}T09:00:00.000Z"))
        conn.execute(
            """INSERT INTO supplier_moves (shop_id, supplier, amount_cents,
               action_id, created_at) VALUES (?, ?, ?, ?, ?)""",
            (shop_id, "Devland Cash & Carry", 12000, "reseed-sup-1",
             f"{d4}T09:00:00.000Z"))
        d2 = today - timedelta(days=2)
        _move("cash", -18000, "supplier_payment", f"{d2}T09:00:00.000Z", note="Jumbo")
        conn.execute(
            """INSERT INTO supplier_moves (shop_id, supplier, amount_cents,
               action_id, created_at) VALUES (?, ?, ?, ?, ?)""",
            (shop_id, "Jumbo Amanzimtoti", -18000, "reseed-suppay",
             f"{d2}T09:00:00.000Z"))
        # Running costs + a bank sweep so the drawer stays sensible.
        d3 = today - timedelta(days=3)
        conn.execute(
            """INSERT INTO expenses (shop_id, category, amount_cents, account,
               action_id, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
            (shop_id, "electricity", 15000, "petty", "reseed-exp-0",
             f"{d3}T11:00:00.000Z"))
        _move("petty", -15000, "expense", f"{d3}T11:00:00.000Z",
              category="electricity")
        _move("cash", -8000, "drawing", f"{d2}T17:00:00.000Z", note="owner")
        for name, qty, ahead in (("Fresh Milk 1L", 2, 2), ("Sasko White Bread 700g", 3, 3)):
            conn.execute(
                """INSERT INTO expiry_items (shop_id, product_name, qty, expiry_date,
                   note) VALUES (?, ?, ?, ?, 'fridge, front row')""",
                (shop_id, name, qty, (today + timedelta(days=ahead)).isoformat()))
        conn.commit()

        cash = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) s FROM cash_moves"
            " WHERE shop_id=? AND account='cash' AND reversed=0",
            (shop_id,)).fetchone()["s"]
        bank = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) s FROM cash_moves"
            " WHERE shop_id=? AND account='bank' AND reversed=0",
            (shop_id,)).fetchone()["s"]
        n_prod = conn.execute("SELECT COUNT(*) n FROM products WHERE shop_id=?",
                              (shop_id,)).fetchone()["n"]
        n_cust = conn.execute("SELECT COUNT(*) n FROM customers WHERE shop_id=?",
                              (shop_id,)).fetchone()["n"]
        n_sales = conn.execute("SELECT COUNT(*) n FROM sales WHERE shop_id=?",
                               (shop_id,)).fetchone()["n"]
        bals = []
        for name in CUSTOMERS:
            cid = cust_ids[name]
            c = conn.execute("SELECT COALESCE(SUM(amount_cents),0) s FROM credit_entries"
                             " WHERE shop_id=? AND customer_id=? AND reversed=0",
                             (shop_id, cid)).fetchone()["s"]
            p = conn.execute("SELECT COALESCE(SUM(amount_cents),0) s FROM payments"
                             " WHERE shop_id=? AND customer_id=? AND reversed=0",
                             (shop_id, cid)).fetchone()["s"]
            bals.append(f"{name} R{(c - p) / 100:.2f}")
        shop2 = conn.execute("SELECT name, language, logo_path FROM shops WHERE shop_id=?",
                             (shop_id,)).fetchone()
        out.append(f"seeded {n_prod} products, {n_cust} customers, {n_sales} sales")
        out.append(f"drawer R{cash / 100:.2f} bank R{bank / 100:.2f}")
        out.append("credit: " + ", ".join(bals))
        out.append(f"shop kept: {shop2['name']} lang={shop2['language']}"
                   f" logo={shop2['logo_path'] or '-'}")
        return "\n".join(out)
    finally:
        conn.close()


@app.local_entrypoint()
def main(confirm: str = "", shop: str = ""):
    print(reseed.remote(confirm, shop))
