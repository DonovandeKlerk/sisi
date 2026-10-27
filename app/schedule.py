"""Scheduled nudges: 19:00 book reminder, Sunday snapshot, expiry warnings.

All entry points take an explicit `now` (SAST) so tests can mock the clock.
`tick()` only plans outbound jobs; `run_due()` plans and sends them.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

SAST = timezone(timedelta(hours=2))


def sast_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).astimezone(SAST)
    if now.tzinfo is None:
        return now.replace(tzinfo=SAST)
    return now.astimezone(SAST)


def _flag(conn: sqlite3.Connection, shop_id: str, key: str) -> str | None:
    row = conn.execute("SELECT value FROM flags WHERE shop_id = ? AND key = ?",
                       (shop_id, key)).fetchone()
    return row["value"] if row else None


def _set_flag(conn: sqlite3.Connection, shop_id: str, key: str,
              value: str = "1") -> None:
    conn.execute("INSERT INTO flags (shop_id, key, value) VALUES (?, ?, ?)"
                 " ON CONFLICT (shop_id, key) DO UPDATE SET value = excluded.value",
                 (shop_id, key, value))
    conn.commit()


def _recorded_today(conn: sqlite3.Connection, shop_id: str, day: str) -> bool:
    for table in ("sales", "credit_entries", "payments", "cash_moves"):
        n = conn.execute(
            f"SELECT COUNT(*) n FROM {table} WHERE shop_id = ? AND reversed = 0"
            " AND substr(datetime(created_at, '+2 hours'), 1, 10) = ?",
            (shop_id, day)).fetchone()["n"]
        if n:
            return True
    return False


def _nudge_text(conn: sqlite3.Connection, shop_id: str) -> str:
    return ("📒 *7pm books*\nNothing recorded today yet — send a photo of today's"
            " book page, or reply with your count, e.g. 'counted R1240'.")


def _day_summary(conn: sqlite3.Connection, shop_id: str, day: str) -> str:
    from app import tools
    rng = "substr(datetime(created_at, '+2 hours'), 1, 10) = ?"
    sales = conn.execute(
        f"SELECT COALESCE(SUM(total_cents), 0) s, COUNT(*) n FROM sales"
        f" WHERE shop_id = ? AND reversed = 0 AND {rng}",
        (shop_id, day)).fetchone()
    credit = conn.execute(
        f"SELECT COALESCE(SUM(total_cents), 0) s FROM sales"
        f" WHERE shop_id = ? AND customer_id IS NOT NULL AND reversed = 0 AND {rng}",
        (shop_id, day)).fetchone()["s"]
    cash = tools.account_balance(conn, shop_id, "cash")
    out = (f"📊 *Today* · {sales['n']} sales {tools.cents_to_rand(sales['s'])}"
           f" ({tools.cents_to_rand(credit)} on book)\n"
           f"💰 Cash in hand {tools.cents_to_rand(cash)}\n"
           "Good work today. ✅")
    crew = conn.execute(
        "SELECT recorded_by, COUNT(*) n FROM sales"
        f" WHERE shop_id = ? AND reversed = 0 AND {rng} AND recorded_by != ''"
        " GROUP BY recorded_by", (shop_id, day)).fetchall()
    if crew:
        out += "\n\n👥 *Per staff*\n" + "\n".join(
            f"• {r['recorded_by']}: {r['n']} sales" for r in crew)
    return out


def maybe_day_summary(conn: sqlite3.Connection, shop_id: str) -> str:
    """3-line summary after a nudge-prompted book photo or count. Once daily."""
    from app.db import today_str
    day = today_str()
    if _flag(conn, shop_id, f"want-books-{day}") != "1":
        return ""
    _set_flag(conn, shop_id, f"want-books-{day}", "done")
    return "\n\n" + _day_summary(conn, shop_id, day)


def _snapshot_data(conn: sqlite3.Connection, shop_id: str, day: str) -> dict:
    from app import tools
    start = (datetime.strptime(day, "%Y-%m-%d").date()
             - timedelta(days=6)).isoformat()
    rng = "substr(datetime(created_at, '+2 hours'), 1, 10) >= ?"
    rows = conn.execute(
        "SELECT s.qty, s.total_cents, s.product_name, p.cost_cents, p.stock_qty,"
        " p.id pid FROM sales s LEFT JOIN products p ON p.id = s.product_id"
        f" WHERE s.shop_id = ? AND s.reversed = 0 AND {rng}",
        (shop_id, start)).fetchall()
    sales = sum(r["total_cents"] for r in rows)
    cogs = int(round(sum((r["qty"] or 0) * (r["cost_cents"] or 0) for r in rows)))
    by_product: dict[str, int] = {}
    for r in rows:
        by_product[r["product_name"]] = by_product.get(r["product_name"], 0) + r["total_cents"]
    best = max(by_product.items(), key=lambda t: t[1]) if by_product else ("—", 0)
    sold_ids = {r["pid"] for r in rows if r["pid"] is not None}
    prods = conn.execute("SELECT * FROM products WHERE shop_id = ?",
                         (shop_id,)).fetchall()
    slow = sorted(
        ({"name": p["name"],
          "cents": int(round((p["stock_qty"] or 0) * (p["cost_cents"] or 0)))}
         for p in prods if p["id"] not in sold_ids and (p["stock_qty"] or 0) > 0),
        key=lambda d: -d["cents"])
    slowest = slow[0] if slow else {"name": "—", "cents": 0}
    debtors = [(r["name"], tools.balance_cents(conn, shop_id, r["id"]))
               for r in conn.execute("SELECT id, name FROM customers WHERE shop_id = ?",
                                     (shop_id,)).fetchall()]
    owed = sum(b for _, b in debtors if b > 0)
    cash = tools.account_balance(conn, shop_id, "cash")
    if owed > 0 and any(b > 0 for _, b in debtors):
        top = max(((n, b) for n, b in debtors if b > 0), key=lambda t: t[1])
        tip = f"Chase {top[0]}'s {tools.cents_to_rand(top[1])} first this week."
    elif slowest["name"] != "—":
        tip = f"Push {slowest['name']} to the front — it is standing still."
    else:
        tip = "Shelves are moving. Keep the float topped up."
    return {"sales": sales, "gross": sales - cogs, "best": best[0],
            "best_cents": best[1], "slowest": slowest["name"],
            "slow_cents": slowest["cents"], "cash": cash, "owed": owed,
            "tip": tip}


def tick(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Plan due jobs. Each job: {shop, channel, text, media, key}."""
    from app import reports
    now = sast_now(now)
    day = now.strftime("%Y-%m-%d")
    hm = now.strftime("%H:%M")
    jobs: list[dict] = []
    shops = conn.execute(
        "SELECT shop_id, channel FROM shops WHERE channel IN ('meta', 'vonage')"
    ).fetchall()
    for shop in shops:
        shop_id, channel = shop["shop_id"], shop["channel"]
        nudge_at = _flag(conn, shop_id, "nudge_time") or "19:00"
        nudge_off = _flag(conn, shop_id, "nudge_off") == "1"
        if (not nudge_off and hm >= nudge_at
                and _flag(conn, shop_id, f"nudge-{day}") is None):
            _set_flag(conn, shop_id, f"nudge-{day}")
            if not _recorded_today(conn, shop_id, day):
                _set_flag(conn, shop_id, f"want-books-{day}", "1")
                jobs.append({"shop": shop_id, "channel": channel,
                             "text": _nudge_text(conn, shop_id), "media": [],
                             "key": f"nudge-{day}"})
        if (not nudge_off and now.weekday() == 6 and hm >= nudge_at
                and _flag(conn, shop_id, f"snap-{day}") is None):
            _set_flag(conn, shop_id, f"snap-{day}")
            data = _snapshot_data(conn, shop_id, day)
            brand = reports.shop_brand(conn, shop_id)
            reports.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
            card = str(reports.REPORTS_DIR / f"{safe}-snapshot-{day}.png")
            reports.render_snapshot_png(data, brand, card, day)
            token = reports.store_card(conn, shop_id, f"snap-{day}", card)
            base = os.environ.get("SISI_PUBLIC_URL", "").rstrip("/")
            media = ([{"kind": "image", "url": f"{base}/media/{token}?f=png",
                       "caption": "Your Sunday snapshot", "filename": ""}]
                     if base else [])
            jobs.append({"shop": shop_id, "channel": channel,
                         "text": "📊 *Sunday snapshot*\nYour week at a glance 👆"
                         if base else "📊 *Sunday snapshot*\nCard saved on file.",
                         "media": media, "key": f"snap-{day}"})
        for item in conn.execute(
                "SELECT id, product_name, qty, expiry_date FROM expiry_items"
                " WHERE shop_id = ?", (shop_id,)).fetchall():
            try:
                left = (datetime.strptime(item["expiry_date"], "%Y-%m-%d").date()
                        - now.date()).days
            except (ValueError, TypeError):
                continue
            if left == 2 and _flag(conn, shop_id, f"exp-{item['id']}") is None:
                _set_flag(conn, shop_id, f"exp-{item['id']}")
                jobs.append({
                    "shop": shop_id, "channel": channel,
                    "text": (f"⏰ *{item['product_name']} expires in 2 days*\n"
                             f"• {item['qty']} on the shelf\n"
                             "• Move it to the front, or run a quick discount."),
                    "media": [], "key": f"exp-{item['id']}"})
    return jobs


def run_due(now: datetime | None = None) -> list[dict]:
    """Plan and send every due job. Returns the sent jobs (for logs/tests)."""
    from app.db import connect, init_db
    from app.vonage import VonageSender
    from app.whatsapp import get_sender
    conn = connect()
    try:
        init_db(conn)
        jobs = tick(conn, now)
    finally:
        conn.close()
    for job in jobs:
        sender = VonageSender() if job["channel"] == "vonage" else get_sender()
        sender.send_text(job["shop"], job["text"])
        for item in job["media"]:
            if item["kind"] == "file":
                sender.send_file(job["shop"], item["url"],
                                 item.get("filename") or "sisi.pdf",
                                 item.get("caption") or "")
            else:
                sender.send_image(job["shop"], item["url"],
                                  item.get("caption") or "")
    return jobs
