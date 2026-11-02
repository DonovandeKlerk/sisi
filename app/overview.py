"""Post-task WhatsApp overview card: one branded PNG after each mutating task.

After every successful mutating task (sale recorded, restock, credit sale,
payment in, price change, float/count, an undo that changes the books, …)
Sisi sends ONE branded overview image showing the shop's current standing,
then the main interactive menu. Everything stays in WhatsApp.

Reads (stock list, who owes me, cash-up, statements, month-end, …) never
trigger the card: :func:`books_fingerprint` snapshots the book tables
before/after a turn and only a real ledger change qualifies.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app import reports
from app import tools
from app.db import SAST, cents_to_rand, today_str
from app.i18n import doc_lang, owner_lang
from app.i18n import t as _t

log = logging.getLogger("sisi.overview")

OVERVIEW_KEY = "overview"


def _sast_hm() -> str:
    return datetime.now(timezone.utc).astimezone(SAST).strftime("%H:%M")


def books_fingerprint(conn: sqlite3.Connection, shop_id: str):
    """Hashable snapshot of the book tables. None when unreadable.

    Compares ledger state before/after a turn: sales, credit book,
    payments, expenses, till moves, stock moves, supplier moves, receipts,
    product quantities/prices/costs and expiry items. Anything a read-only
    query touches (reports rows, media, turns, flags, reminders, order
    drafts, customers metadata) is deliberately excluded.
    """
    try:
        fp: list = []
        sums = [
            ("sales", "total_cents", True),
            ("credit_entries", "amount_cents", True),
            ("payments", "amount_cents", True),
            ("expenses", "amount_cents", True),
            ("cash_moves", "amount_cents", True),
            ("supplier_moves", "amount_cents", True),
            ("receipts", "COALESCE(total_cents, 0)", True),
        ]
        for table, col, scoped in sums:
            where = "shop_id = ? AND reversed = 0" if scoped else "shop_id = ?"
            row = conn.execute(
                f"SELECT COUNT(*) n, COALESCE(SUM({col}), 0) s"
                f" FROM {table} WHERE {where}",
                (shop_id,)).fetchone()
            fp.append((table, row["n"], row["s"]))
        row = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(delta_qty), 0) s FROM stock_moves"
            " WHERE shop_id = ?", (shop_id,)).fetchone()
        fp.append(("stock_moves", row["n"], row["s"]))
        prods = conn.execute(
            "SELECT id, stock_qty, price_cents, cost_cents FROM products"
            " WHERE shop_id = ? ORDER BY id", (shop_id,)).fetchall()
        fp.append(("products", tuple(
            (r["id"], r["stock_qty"], r["price_cents"], r["cost_cents"])
            for r in prods)))
        row = conn.execute(
            "SELECT COUNT(*) n FROM expiry_items WHERE shop_id = ?",
            (shop_id,)).fetchone()
        fp.append(("expiry_items", row["n"]))
        return tuple(fp)
    except Exception:
        log.warning("books fingerprint failed", exc_info=True)
        return None


def overview_data(conn: sqlite3.Connection, shop_id: str) -> dict:
    """Today's standing in the shop's language context (call under doc_lang)."""
    tools.ensure_shop(conn, shop_id)
    day = today_str()
    day_expr = tools._SAST_DAY  # noqa: SLF001 - same package, stable SQL
    cash = conn.execute(
        "SELECT COALESCE(SUM(total_cents), 0) s, COUNT(*) n FROM sales"
        f" WHERE shop_id = ? AND customer_id IS NULL AND reversed = 0"
        f" AND pay_method = 'cash' AND {day_expr} = ?",
        (shop_id, day)).fetchone()
    card = conn.execute(
        "SELECT COALESCE(SUM(total_cents), 0) s, COUNT(*) n FROM sales"
        f" WHERE shop_id = ? AND customer_id IS NULL AND reversed = 0"
        f" AND pay_method = 'card' AND {day_expr} = ?",
        (shop_id, day)).fetchone()
    book = conn.execute(
        "SELECT COALESCE(SUM(total_cents), 0) s, COUNT(*) n FROM sales"
        f" WHERE shop_id = ? AND customer_id IS NOT NULL AND reversed = 0"
        f" AND {day_expr} = ?", (shop_id, day)).fetchone()["s"]
    pay = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) s FROM payments"
        f" WHERE shop_id = ? AND reversed = 0 AND {day_expr} = ?",
        (shop_id, day)).fetchone()["s"]
    debtors: list[dict] = []
    for c in conn.execute("SELECT id, name FROM customers WHERE shop_id = ?",
                          (shop_id,)).fetchall():
        bal = tools.balance_cents(conn, shop_id, c["id"])
        if bal > 0:
            debtors.append({"name": c["name"], "cents": bal})
    debtors.sort(key=lambda d: -d["cents"])
    low = [{"name": r["name"], "days_left": r["days_left"]}
           for r in tools._runout_rows(conn, shop_id)[:4]]
    return {
        "day": day,
        "hm": _sast_hm(),
        "cash_cents": cash["s"],
        "cash_n": cash["n"],
        "card_cents": card["s"],
        "card_n": card["n"],
        "book_cents": book,
        "pay_cents": pay,
        "takings_cents": cash["s"] + pay,
        "sales_cents": cash["s"] + card["s"] + book,
        "debt_total": sum(d["cents"] for d in debtors),
        "debtors_n": len(debtors),
        "top_debtor": debtors[0] if debtors else None,
        "low": low,
    }


def _initials(name: str) -> str:
    words = [w for w in (name or "").split() if w and w[0].isalnum()]
    if not words:
        return "S"
    return ("".join(w[0] for w in words[:2])).upper()


def initials_badge_path(shop_id: str, name: str) -> str:
    """Display-only initials badge for shops without a real logo.

    Cached under the logos dir but never written to ``shops.logo_path``,
    so it can never be mistaken for the owner's uploaded logo. The
    one-time :func:`reports.maybe_logo_nudge` still points at the real
    'send a photo then say logo' flow.
    """
    reports.ensure_dirs()
    safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
    tag = "".join(c for c in _initials(name) if c.isalnum()) or "S"
    path = str(reports._logos_dir() / f"{safe}-initials-{tag}.png")  # noqa: SLF001
    if Path(path).exists():
        return path
    from PIL import Image, ImageDraw
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle([0, 0, size - 1, size - 1], radius=56,
                         fill=reports._hex(reports.SISI["primary"])  # noqa: SLF001
                         + (255,))
    dr.rounded_rectangle([10, 10, size - 11, size - 11], radius=48,
                         outline=reports._hex(reports.SISI["accent"])  # noqa: SLF001
                         + (255,),
                         width=8)
    font = reports._card_font(120, bold=True)
    text = tag[:2]
    w = dr.textlength(text, font=font)
    dr.text(((size - w) / 2, size / 2 - 78), text, font=font,
            fill=(255, 255, 255, 255))
    # Keep the alpha channel: _paste_logo blends the rounded corners into
    # the dark header (flattening to RGB baked the corners black/white).
    img.save(path, "PNG")
    return path


def render_overview_png(data: dict, brand: dict, path: str) -> str:
    """Cream paper, green header with logo/badge, big readable numbers."""
    from PIL import Image, ImageDraw
    W, H = 1080, 1600
    img = Image.new("RGB", (W, H), reports._hex(reports.SISI["paper"]))  # noqa: SLF001
    y = reports._png_brand_header(img, brand, _t("ov_title"), data["day"])
    dr = ImageDraw.Draw(img)
    f_big = reports._card_font(72, bold=True)
    f_head = reports._card_font(48, bold=True)
    f_mid = reports._card_font(44)
    f_small = reports._card_font(38)
    ink, primary, muted = (reports._hex(reports.SISI[k])  # noqa: SLF001
                           for k in ("ink", "primary", "muted"))

    def divider(yy: int) -> int:
        dr.line([60, yy, W - 60, yy],
                fill=reports._hex(reports.SISI["line"]),  # noqa: SLF001
                width=3)
        return yy + 34

    def row(yy: int, label: str, value: str) -> int:
        dr.text((60, yy), label, font=f_mid, fill=ink)
        vw = dr.textlength(value, font=f_mid)
        dr.text((W - 60 - vw, yy), value, font=f_mid, fill=ink)
        return yy + 78

    dr.text((60, y), _t("ov_sales_h"), font=f_head, fill=primary)
    y += 78
    y = row(y, _t("ov_cash"), cents_to_rand(data["cash_cents"]))
    if data["card_cents"]:
        y = row(y, _t("ov_card"), cents_to_rand(data["card_cents"]))
    y = row(y, _t("ov_book"), cents_to_rand(data["book_cents"]))
    y = row(y, _t("ov_paid"), cents_to_rand(data["pay_cents"]))
    y += 6
    y = divider(y)
    takings = cents_to_rand(data["takings_cents"])
    dr.text((60, y), _t("ov_takings"), font=f_head, fill=ink)
    vw = dr.textlength(takings, font=f_big)
    dr.text((W - 60 - vw, y - 10), takings, font=f_big, fill=primary)
    y += 108
    y = divider(y)
    dr.text((60, y), _t("ov_low_h"), font=f_head, fill=primary)
    y += 78
    if data["low"]:
        for item in data["low"][:4]:
            label = f"• {item['name']} — {_t('ov_days', n=item['days_left'])}"
            dr.text((60, y), label[:46], font=f_small, fill=ink)
            y += 58
    else:
        dr.text((60, y), "✅ " + _t("ov_ok_stock"), font=f_small, fill=ink)
        y += 58
    y += 10
    y = divider(y)
    dr.text((60, y), _t("ov_owed_h"), font=f_head, fill=primary)
    y += 78
    owed = cents_to_rand(data["debt_total"])
    owed_fill = reports._hex(reports.SISI["bad"] if data["debt_total"]  # noqa: SLF001
                             else reports.SISI["good"])  # noqa: SLF001
    dr.text((60, y), owed, font=f_big, fill=owed_fill)
    y += 108
    if data["top_debtor"]:
        top = data["top_debtor"]
        line = f"• {top['name']} — {cents_to_rand(top['cents'])}"
        if data["debtors_n"] > 1:
            line += f"  (+{data['debtors_n'] - 1})"
        dr.text((60, y), line[:46], font=f_small, fill=muted)
        y += 58
    elif not data["debt_total"]:
        dr.text((60, y), "🎉 " + _t("ov_ok_owed"), font=f_small, fill=ink)
        y += 58
    y = max(y + 30, H - 190)
    dr.text((60, H - 150), _t("ov_updated", time=data["hm"]),
            font=f_small, fill=muted)
    dr.text((60, H - 96), _t("doc_footer"), font=f_small, fill=muted)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")
    return path


def overview_text(data: dict) -> str:
    """Short text standing for when the image itself fails to render."""
    lines = [_t("ov_fallback_head", title=_t("ov_title"),
                updated=_t("ov_updated", time=data["hm"]))]
    low = ", ".join(i["name"] for i in data["low"][:3]) or _t("ov_ok_stock")
    owed = cents_to_rand(data["debt_total"]) if data["debt_total"] \
        else _t("ov_ok_owed")
    rows = [(_t("ov_sales_h"), cents_to_rand(data["sales_cents"])),
            (_t("ov_takings"), cents_to_rand(data["takings_cents"])),
            (_t("ov_low_h"), low),
            (_t("ov_owed_h"), owed)]
    lines += [_t("ov_fallback_line", label=label, value=value)
              for label, value in rows]
    return "\n".join(lines)


def build_overview_card(conn: sqlite3.Connection, shop_id: str,
                        base_url: str = "") -> tuple[str, list[dict]]:
    """Render the overview PNG and file it for /media. Returns (caption, media).

    Captions and card text always use the owner's saved language. The
    caption carries the one-time logo nudge when the shop shows initials;
    otherwise it is empty. No base URL (local/dev) means no media items.
    """
    with doc_lang(owner_lang(conn, shop_id)):
        data = overview_data(conn, shop_id)
        brand = reports.shop_brand(conn, shop_id)
        if not brand["logo"]:
            brand = dict(brand,
                         logo=initials_badge_path(shop_id, brand["name"]))
            caption = reports.maybe_logo_nudge(conn, shop_id)
        else:
            caption = ""
        safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
        path = str(reports._reports_dir() / f"{safe}-overview.png")  # noqa: SLF001
        render_overview_png(data, brand, path)
    token = reports.store_card(conn, shop_id, OVERVIEW_KEY, path)
    base = (base_url or os.environ.get("SISI_PUBLIC_URL", "")).rstrip("/")
    if not base:
        return "", []
    return caption, [{"kind": "image",
                      "url": f"{base}/media/{token}?f=png",
                      "caption": caption,
                      "filename": "",
                      "first": True}]


def queue_overview_and_menu(conn: sqlite3.Connection, shop_id: str) -> str:
    """Send ONE overview image, then the main menu. Never raises.

    Returns text to append to the task reply: the short text standing
    when image generation fails, else "".
    """
    from app.agent import queue_media, show_menu
    try:
        _caption, media = build_overview_card(conn, shop_id)
    except Exception:
        log.warning("overview card failed", exc_info=True)
        try:
            with doc_lang(owner_lang(conn, shop_id)):
                fallback = overview_text(overview_data(conn, shop_id))
        except Exception:
            log.warning("overview fallback failed", exc_info=True)
            fallback = ""
        try:
            show_menu(conn, shop_id)
        except Exception:
            log.warning("overview menu failed", exc_info=True)
        return fallback
    try:
        if media:
            queue_media(shop_id, media)
    except Exception:
        log.warning("overview queue failed", exc_info=True)
        with doc_lang(owner_lang(conn, shop_id)):
            fallback = overview_text(overview_data(conn, shop_id))
        show_menu(conn, shop_id)
        return fallback
    show_menu(conn, shop_id)
    return ""
