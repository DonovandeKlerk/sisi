"""Scheduled nudges: 19:00 book reminder, Sunday snapshot, expiry warnings.

All entry points take an explicit `now` (SAST) so tests can mock the clock.
`tick()` only plans outbound jobs; `run_due()` plans and sends them.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

log = logging.getLogger("sisi.schedule")

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


def _nudge_text(conn: sqlite3.Connection, shop_id: str, day: str) -> str:
    from app.i18n import t
    from app.reminders import due_today
    from app.tools import _runout_rows
    base = t("nudge_base")
    open_items = []
    for r in due_today(conn, shop_id, day)[:4]:
        open_items.append(t("nudge_open", hm=r["due_at"][11:16],
                            summary=r["summary"]))
    low = conn.execute("SELECT COUNT(*) n FROM products WHERE shop_id = ?"
                       " AND stock_qty <= reorder_level", (shop_id,)).fetchone()["n"]
    if low == 1:
        open_items.append(t("nudge_buy_one"))
    elif low:
        open_items.append(t("nudge_buy_many", n=low))
    for r in _runout_rows(conn, shop_id, today=day)[:3]:
        open_items.append(t("nudge_runout", name=r["name"], when=r["when"]))
    owed = conn.execute(
        "SELECT COUNT(*) n FROM customers c WHERE c.shop_id = ? AND ("
        "  (SELECT COALESCE(SUM(amount_cents), 0) FROM credit_entries e"
        "   WHERE e.customer_id = c.id AND e.reversed = 0)"
        " - (SELECT COALESCE(SUM(amount_cents), 0) FROM payments p"
        "   WHERE p.customer_id = c.id AND p.reversed = 0)) > 0",
        (shop_id,)).fetchone()["n"]
    if owed == 1:
        open_items.append(t("nudge_chase_one"))
    elif owed:
        open_items.append(t("nudge_chase_many", n=owed))
    if not open_items:
        return base + "\n\n" + t("nudge_checklist") + "\n" + t("nudge_clear")
    return base + "\n\n" + t("nudge_checklist") + "\n" + "\n".join(open_items)


def _fmt_left(qty: float, pack_size, pack_name: str) -> str:
    from app.i18n import t
    from app.tools import _plural_pack
    qty = int(qty or 0)
    if pack_size and pack_size > 1 and qty >= pack_size:
        packs, loose = divmod(qty, int(pack_size))
        word = (pack_name or "pack") if packs == 1 else _plural_pack(pack_name or "pack")
        return t("left_packs", p=packs, pack=word, l=loose)
    return t("left_loose", q=qty)


def _debtor_rows(conn: sqlite3.Connection, shop_id: str) -> list[dict]:
    from app.tools import balance_cents
    out = []
    for cust in conn.execute("SELECT id, name, credit_limit_cents FROM customers"
                             " WHERE shop_id = ?", (shop_id,)).fetchall():
        owed = balance_cents(conn, shop_id, cust["id"])
        if owed <= 0:
            continue
        oldest = conn.execute(
            "SELECT substr(MIN(created_at), 1, 10) d FROM credit_entries"
            " WHERE customer_id = ? AND reversed = 0", (cust["id"],)).fetchone()["d"]
        out.append({"name": cust["name"], "owed": owed, "oldest": oldest or "",
                    "limit": cust["credit_limit_cents"]})
    return out


def _day_result(conn: sqlite3.Connection, shop_id: str, day: str):
    """(sales_cents, profit_cents) for a SAST day. None when nothing sold."""
    rng = "substr(datetime(s.created_at, '+2 hours'), 1, 10) = ?"
    row = conn.execute(
        f"SELECT COALESCE(SUM(s.total_cents), 0) sales,"
        f" COALESCE(SUM(s.qty * COALESCE(p.cost_cents, 0)), 0) cogs"
        f" FROM sales s LEFT JOIN products p ON p.id = s.product_id"
        f" WHERE s.shop_id = ? AND s.reversed = 0 AND {rng}",
        (shop_id, day)).fetchone()
    if not row["sales"]:
        return None
    return row["sales"], row["sales"] - row["cogs"]


def daily_plan(conn: sqlite3.Connection, shop_id: str, day: str) -> str:
    """☀️ Today's plan: reminders, buying, debtors, cash, yesterday, one tip."""
    from datetime import date as _date
    from app.db import cents_to_rand
    from app.i18n import day_stamp, t
    from app.reminders import due_today
    from app.tools import account_balance, get_order_draft
    _d = _date.fromisoformat(day)
    when = day_stamp(_d.weekday(), f"{_d.day:02d}", _d.month)
    blocks = [t("plan_head", when=when)]
    rems = due_today(conn, shop_id, day)
    if rems:
        blocks.append(t("plan_due") + "\n" + "\n".join(
            t("plan_due_line", hm=r["due_at"][11:16], summary=r["summary"])
            for r in rems[:6]))
    stuck = conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?"
                         " AND status IN ('blocked', 'failed')",
                         (shop_id,)).fetchone()["n"]
    if stuck:
        blocks.append(t("rem_plan_stuck", n=stuck))
    low = conn.execute("SELECT name, stock_qty, pack_size, pack_name FROM products"
                       " WHERE shop_id = ? AND stock_qty <= reorder_level"
                       " ORDER BY stock_qty LIMIT 6", (shop_id,)).fetchall()
    if low:
        blocks.append(t("plan_low") + "\n" + "\n".join(
            t("plan_low_line", name=r["name"],
              left=_fmt_left(r["stock_qty"], r["pack_size"], r["pack_name"]))
            for r in low))
    soon = (_date.fromisoformat(day) + timedelta(days=2)).isoformat()
    expiring = conn.execute("SELECT product_name, expiry_date FROM expiry_items"
                            " WHERE shop_id = ? AND expiry_date <= ?"
                            " ORDER BY expiry_date LIMIT 5",
                            (shop_id, soon)).fetchall()
    if expiring:
        lines = []
        for r in expiring:
            if r["expiry_date"] < day:
                lines.append(t("plan_expired", name=r["product_name"]))
            else:
                lines.append(t("plan_expires", name=r["product_name"],
                               md=r["expiry_date"][5:]))
        blocks.append(t("plan_expiring") + "\n" + "\n".join(lines))
    debtors = [d for d in _debtor_rows(conn, shop_id)]
    if debtors:
        def _rank(d):
            overdue = d["oldest"] and d["oldest"] <= (
                _date.fromisoformat(day) - timedelta(days=7)).isoformat()
            over = d["limit"] is not None and d["owed"] > d["limit"]
            return (not (overdue or over), -d["owed"])
        lines = []
        for d in sorted(debtors, key=_rank)[:5]:
            tag = ""
            if d["oldest"]:
                age = (_date.fromisoformat(day)
                       - _date.fromisoformat(d["oldest"])).days
                if age > 7:
                    tag = t("plan_days", age=age)
            if d["limit"] is not None and d["owed"] > d["limit"]:
                tag = t("plan_over_limit")
            lines.append(t("plan_chase_line", name=d["name"],
                           owed=cents_to_rand(d["owed"]), tag=tag))
        blocks.append(t("plan_chase") + "\n" + "\n".join(lines))
    draft = get_order_draft(conn, shop_id)
    if draft is not None:
        need = sum(ln["est_cents"] for ln in draft["lines"]
                   if ln["est_cents"] is not None)
        have = account_balance(conn, shop_id, "cash")
        blocks.append(t("plan_cash", have=cents_to_rand(have),
                        need=cents_to_rand(need)))
    yesterday = (_date.fromisoformat(day) - timedelta(days=1)).isoformat()
    result = _day_result(conn, shop_id, yesterday)
    if result is not None:
        sales, profit = result
        blocks.append(t("plan_yesterday", sales=cents_to_rand(sales),
                        profit=cents_to_rand(profit)))
    tip = _plan_tip(debtors, low)
    if tip:
        blocks.append(t("plan_tip", tip=tip))
    if _flag(conn, shop_id, "plan-welcomed") is None:
        _set_flag(conn, shop_id, "plan-welcomed")
        blocks.append(t("plan_optout"))
    return "\n\n".join(blocks)


def _plan_tip(debtors: list[dict], low) -> str:
    from app.i18n import t
    if debtors:
        top = max(debtors, key=lambda d: d["owed"])
        from app.db import cents_to_rand
        return t("tip_chase", name=top["name"],
                 owed=cents_to_rand(top["owed"]))
    if low:
        return t("tip_lowest", name=low[0]["name"])
    return t("tip_count")


def set_daily_plan(conn: sqlite3.Connection, shop_id: str, setting: str) -> str:
    """Change the morning plan: 'HH:MM', 'off' or 'on'."""
    from app.i18n import t
    from app.reminders import extract_time
    from app.tools import ensure_shop
    ensure_shop(conn, shop_id)
    word = (setting or "").strip().lower()
    if word == "off":
        _set_flag(conn, shop_id, "plan_off")
        return t("plan_off_ok")
    if word == "on":
        conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'plan_off'",
                     (shop_id,))
        conn.commit()
        at = _flag(conn, shop_id, "plan_time") or "06:00"
        return t("plan_on_ok", at=at)
    hm = extract_time(word)
    if hm is None:
        return t("plan_hint")
    at = f"{hm[0]:02d}:{hm[1]:02d}"
    _set_flag(conn, shop_id, "plan_time", at)
    conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'plan_off'",
                 (shop_id,))
    conn.commit()
    return t("plan_set_ok", at=at)


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
    from app.i18n import t
    out = (t("sch_day_head", n=sales['n'],
            sales=tools.cents_to_rand(sales['s']),
            credit=tools.cents_to_rand(credit)) + "\n"
           + t("sch_day_cash", cash=tools.cents_to_rand(cash)) + "\n"
           + t("sch_day_good"))
    crew = conn.execute(
        "SELECT recorded_by, COUNT(*) n FROM sales"
        f" WHERE shop_id = ? AND reversed = 0 AND {rng} AND recorded_by != ''"
        " GROUP BY recorded_by", (shop_id, day)).fetchall()
    if crew:
        out += t("sch_day_staff_h") + "\n" + "\n".join(
            t("sch_day_staff", who=r['recorded_by'], n=r['n']) for r in crew)
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
    from app.i18n import t
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
    runs = tools._runout_rows(conn, shop_id, today=day)[:1]
    if owed > 0 and any(b > 0 for _, b in debtors):
        top = max(((n, b) for n, b in debtors if b > 0), key=lambda t: t[1])
        tip = t("sch_tip_chase", name=top[0],
                amt=tools.cents_to_rand(top[1]))
    elif runs:
        tip = t("sch_tip_runout", name=runs[0]["name"],
                when=runs[0]["when"])
    elif slowest["name"] != "—":
        tip = t("sch_tip_push", name=slowest['name'])
    else:
        tip = t("sch_tip_float")
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
        "SELECT shop_id, channel FROM shops"
        " WHERE channel IN ('meta', 'vonage', 'infobip')"
    ).fetchall()
    for shop in shops:
        shop_id, channel = shop["shop_id"], shop["channel"]
        from app.i18n import CURRENT_LANG, owner_lang
        from app.i18n import t
        CURRENT_LANG.set(owner_lang(conn, shop_id))
        nudge_at = _flag(conn, shop_id, "nudge_time") or "19:00"
        nudge_off = _flag(conn, shop_id, "nudge_off") == "1"
        if (not nudge_off and hm >= nudge_at
                and _flag(conn, shop_id, f"nudge-{day}") is None):
            _set_flag(conn, shop_id, f"nudge-{day}")
            if not _recorded_today(conn, shop_id, day):
                _set_flag(conn, shop_id, f"want-books-{day}", "1")
                jobs.append({"shop": shop_id, "channel": channel,
                             "text": _nudge_text(conn, shop_id, day),
                             "media": [], "key": f"nudge-{day}"})
        plan_at = _flag(conn, shop_id, "plan_time") or "06:00"
        plan_off = _flag(conn, shop_id, "plan_off") == "1"
        try:
            _ph, _pm = (int(x) for x in plan_at.split(":"))
            late_min = (now.hour * 60 + now.minute) - (_ph * 60 + _pm)
        except ValueError:
            late_min = 999
        if (not plan_off and 0 <= late_min < 120
                and _flag(conn, shop_id, f"plan-{day}") is None):
            _set_flag(conn, shop_id, f"plan-{day}")
            jobs.append({"shop": shop_id, "channel": channel,
                         "text": daily_plan(conn, shop_id, day), "media": [],
                         "key": f"plan-{day}"})
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
                       "caption": t("sch_snap_cap"), "filename": ""}]
                     if base else [])
            jobs.append({"shop": shop_id, "channel": channel,
                         "text": t("sch_snap_text")
                         if base else t("sch_snap_saved"),
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
                    "text": t("sch_exp_warn", name=item['product_name'],
                              qty=item['qty']),
                    "media": [], "key": f"exp-{item['id']}"})
    return jobs


def run_due(now: datetime | None = None) -> list[dict]:
    """Plan and send every due job. Returns the sent jobs (for logs/tests)."""
    from app.db import connect, init_db
    from app.providers import WrongProviderError, sender_for_channel
    from app.sendqueue import QueuedSender
    conn = connect()
    try:
        init_db(conn)
        jobs = tick(conn, now)
        queues: dict[str, QueuedSender] = {}
        for job in jobs:
            channel = job["channel"] or "meta"
            try:
                sender = sender_for_channel(channel)
            except WrongProviderError as e:
                log.error("schedule: %s; job %s skipped", e, job["key"])
                continue
            out = queues.get(job["shop"])
            if out is None:
                out = QueuedSender.wrap(sender, conn, channel)
                queues[job["shop"]] = out
            from app.i18n import CURRENT_LANG, owner_lang
            from app.mediasend import with_media_links

            CURRENT_LANG.set(owner_lang(conn, job["shop"]))
            out.send_text(job["shop"],
                          with_media_links(job["text"], job["media"]))
            for item in job["media"]:
                if item["kind"] == "file":
                    out.send_file(job["shop"], item["url"],
                                  item.get("filename") or "sisi.pdf",
                                  item.get("caption") or "")
                else:
                    out.send_image(job["shop"], item["url"],
                                   item.get("caption") or "")
        for out in queues.values():
            out.flush()
    finally:
        conn.close()
    return jobs
