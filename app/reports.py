"""Month-end management accounts: data, branded PDF and PNG summary card."""

from __future__ import annotations

import calendar
import logging
import math
import os
import secrets
from datetime import date, timedelta
from pathlib import Path

from app import tools
from app.db import cents_to_rand, today_str
from app.i18n import acct_name as _acct_name
from app.i18n import cat_name as _cat_name
from app.i18n import doc_lang, month_year as _month_year, owner_lang
from app.i18n import t as _t

log = logging.getLogger("sisi.reports")

# Sisi brand theme (theme-factory custom): warm South African spaza feel —
# deep till-green, marigold accent, cream paper, dark ink.
SISI = {
    "primary": "#14532D",   # deep green
    "accent": "#F59E0B",    # marigold
    "paper": "#FFFBEB",     # cream
    "ink": "#1C1917",       # near-black
    "muted": "#78716C",
    "line": "#E7E5E4",
    "good": "#15803D",
    "bad": "#B91C1C",
}

def _reports_dir() -> Path:
    override = globals().get("REPORTS_DIR")
    if override is not None:
        Path(override).mkdir(parents=True, exist_ok=True)
        return override
    d = Path(os.environ.get("SISI_REPORTS", "data/reports"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _logos_dir() -> Path:
    override = globals().get("LOGOS_DIR")
    if override is not None:
        Path(override).mkdir(parents=True, exist_ok=True)
        return override
    d = Path(os.environ.get("SISI_LOGOS", "data/logos"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def __getattr__(name: str):
    # Read at use time: Modal sets SISI_* after import, and tests override env.
    # (Module __getattr__ only serves external attribute access; code inside
    # this module must use _reports_dir()/_logos_dir() instead.)
    if name == "REPORTS_DIR":
        return _reports_dir()
    if name == "LOGOS_DIR":
        return _logos_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def ensure_dirs() -> None:
    """Create every generated-file folder (reports, logos, receipts, statements)."""
    for d in (_reports_dir(), _logos_dir(), _reports_dir() / "receipts",
              _reports_dir() / "statements"):
        d.mkdir(parents=True, exist_ok=True)


def persist_media() -> None:
    """Publish generated files + DB rows so other containers can serve them.

    On Modal the worker that renders a card/PDF is not the web container
    that serves /media/<id>; commit the shared volume after writing.
    No-op unless SISI_MEDIA_VOLUME names the volume (Modal only).
    Never raises: a failed commit must not break sending (logged instead).
    """
    name = os.environ.get("SISI_MEDIA_VOLUME", "")
    if not name:
        return
    try:
        import modal
        modal.Volume.from_name(name).commit()
    except Exception:
        log.warning("media volume commit failed", exc_info=True)


def refresh_media() -> None:
    """Reload the shared volume before serving /media (cross-container reads).

    Picks up files + DB rows committed by worker containers since this
    container's snapshot. No-op unless SISI_MEDIA_VOLUME is set. Never raises.
    """
    name = os.environ.get("SISI_MEDIA_VOLUME", "")
    if not name:
        return
    try:
        import modal
        modal.Volume.from_name(name).reload()
    except Exception:
        log.warning("media volume reload failed", exc_info=True)



def shop_brand(conn, shop_id: str) -> dict:
    """Owner-supplied branding for document headers. Never invented."""
    tools.ensure_shop(conn, shop_id)
    shop = tools.ensure_shop(conn, shop_id)
    keys = shop.keys()
    display = shop["logo_path"] or ""
    orig = shop["logo_orig_path"] if "logo_orig_path" in keys else ""
    if not orig:
        orig = display  # legacy single-file logos
    mode = conn.execute("SELECT value FROM flags WHERE shop_id = ? AND key = 'logo-mode'",
                        (shop_id,)).fetchone()
    logo = orig if (mode and mode["value"] == "original") else display
    if logo and not Path(logo).exists():
        logo = ""
    if orig and not Path(orig).exists():
        orig = ""
    try:
        from app.setup import needs_repolish, request_polish
        if orig and needs_repolish(conn, shop_id):
            request_polish(conn, shop_id)
    except Exception:
        log.warning("logo repolish trigger failed", exc_info=True)
    return {"name": shop["name"], "address": shop["address"] or "",
            "phone": shop["phone"] or "", "logo": logo, "logo_orig": orig,
            "language": shop["language"] or "en"}


def maybe_logo_nudge(conn, shop_id: str) -> str:
    """One gentle reminder to send a logo. Empty when already nudged/has logo."""
    if shop_brand(conn, shop_id)["logo"]:
        return ""
    if conn.execute("SELECT value FROM flags WHERE shop_id = ? AND key = 'logo-nudge'",
                    (shop_id,)).fetchone():
        return ""
    conn.execute("INSERT OR IGNORE INTO flags (shop_id, key, value)"
                 " VALUES (?, 'logo-nudge', '1')", (shop_id,))
    conn.commit()
    return _t("logo_nudge")


def month_label(month: str) -> str:
    """'September 2026' in the current language."""
    return _month_year(int(month[:4]), int(month[5:7]))


def prev_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    if m == 1:
        return f"{y - 1}-12"
    return f"{y}-{m - 1:02d}"


def default_month() -> str:
    """On the 1st the books mean last month; otherwise month-to-date."""
    today = today_str()
    if today.endswith("-01"):
        return prev_month(today[:7])
    return today[:7]


def _month_sum(conn, shop_id: str, table: str, column: str, month: str) -> int:
    return conn.execute(
        f"SELECT COALESCE(SUM({column}), 0) s FROM {table}"
        f" WHERE shop_id = ? AND reversed = 0 AND {tools._SAST_MONTH} = ?",
        (shop_id, month)).fetchone()["s"]


def _cash_by_kind(conn, shop_id: str, month: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT kind, COALESCE(SUM(amount_cents), 0) s FROM cash_moves"
        f" WHERE shop_id = ? AND reversed = 0 AND {tools._SAST_MONTH} = ? GROUP BY kind",
        (shop_id, month)).fetchall()
    return {r["kind"]: r["s"] for r in rows}


def month_data(conn, shop_id: str, month: str) -> dict:
    """All month-end figures in cents (ints). Balance sheet balances by construction."""
    tools.ensure_shop(conn, shop_id)
    shop = tools.ensure_shop(conn, shop_id)
    sales, cogs = tools._month_cost_of_sales(conn, shop_id, month)
    kinds = _cash_by_kind(conn, shop_id, month)
    exp_rows = conn.execute(
        "SELECT category, COALESCE(SUM(amount_cents), 0) s FROM expenses"
        f" WHERE shop_id = ? AND reversed = 0 AND {tools._SAST_MONTH} = ?"
        " GROUP BY category ORDER BY s DESC",
        (shop_id, month)).fetchall()
    expenses = [{"category": r["category"], "cents": r["s"]} for r in exp_rows]
    exp_total = sum(e["cents"] for e in expenses)
    purchases = -min(0, kinds.get("purchase", 0))
    sup_rows = conn.execute(
        "SELECT supplier, COALESCE(SUM(amount_cents), 0) s FROM supplier_moves"
        " WHERE shop_id = ? AND reversed = 0 GROUP BY supplier HAVING s > 0"
        " ORDER BY s DESC", (shop_id,)).fetchall()
    creditors = [{"supplier": r["supplier"], "cents": r["s"]} for r in sup_rows]
    cash = tools.account_balance(conn, shop_id, "cash")
    petty = tools.account_balance(conn, shop_id, "petty")
    bank = tools.account_balance(conn, shop_id, "bank")
    prods = conn.execute(
        "SELECT * FROM products WHERE shop_id = ? ORDER BY category, name",
        (shop_id,)).fetchall()
    stock_lines = []
    closing_value = 0
    for p in prods:
        qty = p["stock_qty"] or 0
        cost = p["cost_cents"] or 0
        value = int(round(qty * cost)) if qty > 0 else 0
        closing_value += value
        stock_lines.append({
            "name": p["name"], "category": p["category"] or "Other",
            "on_hand": tools._fmt_on_hand(qty, p["pack_name"], p["pack_size"]),
            "qty": qty, "cost_cents": cost, "value_cents": value,
            "low": qty <= (p["reorder_level"] or 0),
        })
    opening_value = max(0, closing_value - purchases + cogs)
    draws = -min(0, kinds.get("drawing", 0))
    collected = _month_sum(conn, shop_id, "payments", "amount_cents", month)
    cash_sales = conn.execute(
        "SELECT COALESCE(SUM(total_cents), 0) s FROM sales"
        f" WHERE shop_id = ? AND customer_id IS NULL AND reversed = 0"
        f" AND {tools._SAST_MONTH} = ?", (shop_id, month)).fetchone()["s"]
    credit_sales = sales - cash_sales
    paid_cash = -min(0, kinds.get("purchase", 0))
    exp_cash = -min(0, kinds.get("expense", 0))
    count_adj = kinds.get("count", 0)
    closing_cash = cash + petty + bank
    opening_cash = closing_cash - cash_sales - collected - count_adj + paid_cash + exp_cash + draws
    custs = conn.execute("SELECT id, name FROM customers WHERE shop_id = ?",
                         (shop_id,)).fetchall()
    debtors = []
    for c in custs:
        bal = tools.balance_cents(conn, shop_id, c["id"])
        if bal > 0:
            debtors.append({"name": c["name"], "cents": bal})
    debtors.sort(key=lambda d: -d["cents"])
    debt_total = sum(d["cents"] for d in debtors)
    cred_total = sum(c["cents"] for c in creditors)
    assets = closing_value + closing_cash + debt_total
    equity = assets - cred_total
    gross = sales - cogs
    net = gross - exp_total
    sold_ids = {r["product_id"] for r in conn.execute(
        "SELECT DISTINCT product_id FROM sales WHERE shop_id = ? AND reversed = 0"
        f" AND {tools._SAST_MONTH} = ?", (shop_id, month)).fetchall()
        if r["product_id"] is not None}
    slow = sorted(
        ({"name": p["name"], "cents": int(round((p["stock_qty"] or 0) * (p["cost_cents"] or 0)))}
         for p in prods if p["id"] not in sold_ids and (p["stock_qty"] or 0) > 0),
        key=lambda d: -d["cents"])[:3]
    best = conn.execute(
        "SELECT product_name, SUM(qty) q, COALESCE(SUM(total_cents), 0) t FROM sales"
        f" WHERE shop_id = ? AND reversed = 0 AND {tools._SAST_MONTH} = ?"
        " GROUP BY product_name ORDER BY t DESC LIMIT 3",
        (shop_id, month)).fetchall()
    best = [{"name": r["product_name"], "qty": r["q"], "cents": r["t"]} for r in best]
    start = (date(int(month[:4]), int(month[5:7]), 1).replace(day=1)).isoformat()
    end_day = calendar.monthrange(int(month[:4]), int(month[5:7]))[1]
    days = end_day if month != today_str()[:7] else int(today_str()[8:10])
    reorder = []
    for p in prods:
        if (p["stock_qty"] or 0) > (p["reorder_level"] or 0):
            continue
        sold = conn.execute(
            "SELECT COALESCE(SUM(qty), 0) s FROM sales WHERE shop_id = ? AND product_id = ?"
            " AND reversed = 0 AND substr(datetime(created_at, '+2 hours'), 1, 10) >= ?",
            (shop_id, p["id"], start)).fetchone()["s"]
        target = max((p["reorder_level"] or 0) * 2,
                     math.ceil((sold / max(days, 1)) * 7))
        need = max(0.0, target - (p["stock_qty"] or 0))
        if not need:
            continue
        if p["pack_size"]:
            packs = math.ceil(need / p["pack_size"])
            units = packs * p["pack_size"]
        else:
            packs, units = 0, need
        est = int(round(units * (p["cost_cents"] or 0))) if p["cost_cents"] else None
        reorder.append({"name": p["name"], "wholesaler": p["wholesaler"] or "",
                        "packs": packs, "pack_name": p["pack_name"] or "",
                        "units": units, "est_cents": est})
    reorder.sort(key=lambda r: (r["wholesaler"], r["name"]))
    insights = []
    if best:
        insights.append(_t("doc_ins_best", name=best[0]['name'],
                           amt=cents_to_rand(best[0]['cents'])))
    if slow:
        insights.append(_t("doc_ins_slow", name=slow[0]['name'],
                           amt=cents_to_rand(slow[0]['cents'])))
    if debtors:
        insights.append(_t("doc_ins_chase", name=debtors[0]['name'],
                           amt=cents_to_rand(debtors[0]['cents'])))
    return {
        "shop": shop["name"], "month": month, "label": month_label(month),
        "sales": sales, "cash_sales": cash_sales, "credit_sales": credit_sales,
        "cogs": cogs, "gross": gross, "expenses": expenses, "exp_total": exp_total,
        "net": net, "purchases": purchases,
        "opening_value": opening_value, "closing_value": closing_value,
        "cash": cash, "petty": petty, "bank": bank,
        "opening_cash": opening_cash, "closing_cash": closing_cash,
        "collected": collected, "paid_cash": paid_cash, "exp_cash": exp_cash,
        "draws": draws, "count_adj": count_adj,
        "debtors": debtors, "debt_total": debt_total,
        "creditors": creditors, "cred_total": cred_total,
        "assets": assets, "equity": equity,
        "stock_lines": stock_lines, "reorder": reorder,
        "best": best, "slow": slow, "insights": insights,
    }


def month_short(conn, shop_id: str, month: str) -> str:
    """WhatsApp text summary of the month-end figures."""
    d = month_data(conn, shop_id, month)
    margin = f" ({round(d['gross'] * 100 / d['sales'])}%)" if d["sales"] else ""
    lines = [
        f"📊 *{_t('doc_title_month')}: {d['label']}*",
        _t("rep_m_sales", x=cents_to_rand(d['sales'])),
        _t("rep_m_gross", x=cents_to_rand(d['gross']), m=margin),
        _t("rep_m_exp", x=cents_to_rand(d['exp_total'])),
        _t("rep_m_net", x=cents_to_rand(d['net'])),
        "",
        _t("rep_m_cash_h"),
        _t("rep_m_inhand", c=cents_to_rand(d['cash']),
           p=cents_to_rand(d['petty'])),
        _t("rep_m_owed", x=cents_to_rand(d['debt_total'])),
    ]
    if d["insights"]:
        lines += ["", _t("rep_m_ins_h")] + [f"• {i}" for i in d["insights"][:3]]
    return "\n".join(lines)


def save_report_files(conn, shop_id: str, month: str) -> dict:
    """Render PDF + PNG, store row, return paths + token."""
    ensure_dirs()
    safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
    pdf_path = str(_reports_dir() / f"{safe}-{month}.pdf")
    png_path = str(_reports_dir() / f"{safe}-{month}.png")
    d = month_data(conn, shop_id, month)
    brand = shop_brand(conn, shop_id)
    render_pdf(d, pdf_path, brand)
    render_card(d, png_path, brand)
    token = secrets.token_hex(16)
    conn.execute(
        "INSERT INTO reports (shop_id, month, pdf_path, png_path, token)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (shop_id, month) DO UPDATE SET pdf_path = excluded.pdf_path,"
        " png_path = excluded.png_path, token = excluded.token,"
        " created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
        (shop_id, month, pdf_path, png_path, token))
    conn.commit()
    return {"pdf": pdf_path, "png": png_path, "token": token}


def get_report(conn, shop_id: str, month: str):
    return conn.execute("SELECT * FROM reports WHERE shop_id = ? AND month = ?",
                        (shop_id, month)).fetchone()


def store_card(conn, shop_id: str, key: str, png_path: str) -> str:
    """File a one-off card (profile, statement, snapshot, order) for /media."""
    token = secrets.token_hex(16)
    conn.execute(
        "INSERT INTO reports (shop_id, month, png_path, token)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT (shop_id, month) DO UPDATE SET png_path = excluded.png_path,"
        " token = excluded.token,"
        " created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
        (shop_id, key, png_path, token))
    conn.commit()
    return token


def get_report_by_token(conn, token: str):
    return conn.execute("SELECT * FROM reports WHERE token = ?", (token,)).fetchone()


def build_month_end(conn, shop_id: str, month: str | None = None,
                    base_url: str = "") -> tuple[str, list[dict]]:
    """Generate (or re-send) a month-end report. Returns (text, media items).

    Past months reuse stored files; the current month always regenerates.
    Media items are queued by the caller when base_url is set.
    """
    m = month or default_month()
    row = get_report(conn, shop_id, m)
    if (row and m < today_str()[:7]
            and Path(row["pdf_path"]).exists() and Path(row["png_path"]).exists()):
        reused = True
    else:
        # The filed documents always use the owner's saved language, even
        # when the triggering message was written in another language.
        with doc_lang(owner_lang(conn, shop_id)):
            save_report_files(conn, shop_id, m)
        row = get_report(conn, shop_id, m)
        reused = False
    text = month_short(conn, shop_id, m)
    if reused:
        text += _t("rep_resend")
    text += maybe_logo_nudge(conn, shop_id)
    media: list[dict] = []
    base = (base_url or "").rstrip("/")
    if base and row:
        media = [
            {"kind": "image",
             "url": f"{base}/media/{row['token']}?f=png",
             "caption": _t("rep_cap_card", label=month_label(m)),
             "filename": ""},
            {"kind": "file",
             "url": f"{base}/media/{row['token']}?f=pdf",
             "caption": _t("rep_cap_pdf", label=month_label(m)),
             "filename": f"sisi-month-end-{m}.pdf"},
        ]
        text += _t("rep_files_below")
    else:
        text += _t("rep_files_saved")
    return text, media


# ---------------------------------------------------------------- PDF

def _esc(text: str) -> str:
    """Escape ReportLab Paragraph markup chars in a translated string."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pdf_styles():
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.colors import HexColor
    ink, muted, primary = (HexColor(SISI["ink"]), HexColor(SISI["muted"]),
                           HexColor(SISI["primary"]))
    return {
        "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=22,
                                leading=26, textColor=primary),
        "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=15,
                             leading=19, textColor=primary, spaceAfter=8),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11,
                             leading=14, textColor=ink, spaceBefore=6),
        "body": ParagraphStyle("body", fontName="Helvetica", fontSize=10,
                               leading=14, textColor=ink),
        "muted": ParagraphStyle("muted", fontName="Helvetica", fontSize=9,
                                leading=12, textColor=muted),
        "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=9.5, leading=12),
        "cell_b": ParagraphStyle("cell_b", fontName="Helvetica-Bold", fontSize=9.5,
                                 leading=12),
        "cell_r": ParagraphStyle("cell_r", fontName="Helvetica", fontSize=9.5,
                                 leading=12, alignment=2),
        "cell_br": ParagraphStyle("cell_br", fontName="Helvetica-Bold", fontSize=9.5,
                                  leading=12, alignment=2),
    }


def _pdf_table(rows, widths, header=True):
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import Table, TableStyle
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, HexColor(SISI["primary"])),
        ("LINEBELOW", (0, -1), (-1, -1), 1.2, HexColor(SISI["primary"])),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [HexColor("#FFFFFF"),
                                              HexColor(SISI["paper"])]),
    ]
    if not header:
        style = [s for s in style if s[0] != "LINEBELOW"] + [
            ("LINEBELOW", (0, -1), (-1, -1), 1.2, HexColor(SISI["primary"]))]
    return Table(rows, colWidths=widths, style=TableStyle(style))


def _kv_rows(st, items, bold_last=True):
    from reportlab.platypus import Paragraph
    rows = [[Paragraph(k, st["cell"]), Paragraph(v, st["cell_r"])] for k, v in items]
    if bold_last and rows:
        k, v = items[-1]
        rows[-1] = [Paragraph(k, st["cell_b"]), Paragraph(v, st["cell_br"])]
    return rows


def _money_bar(story, st, parts, width=440):
    """Simple horizontal bar visual for P&L proportions."""
    from reportlab.graphics.shapes import Drawing, Line, Rect, String
    from reportlab.lib.colors import HexColor
    total = max(sum(v for _, v, _ in parts), 1)
    d = Drawing(width, 46)
    x = 0.0
    for label, value, color in parts:
        w = max(value / total * width, 0)
        if w <= 0:
            continue
        d.add(Rect(x, 18, w, 16, fillColor=HexColor(color), strokeColor=None))
        if w > 60:
            d.add(String(x + 5, 23, f"{label} {cents_to_rand(value)}",
                         fontName="Helvetica", fontSize=8,
                         fillColor=HexColor("#FFFFFF")))
        x += w
    d.add(Line(0, 14, width, 14, strokeColor=HexColor(SISI["line"])))
    story.append(d)


def statement_data(conn, shop_id: str, customer: str) -> dict | None:
    """Ledger-truth statement: item lines, payments, outstanding total."""
    tools.ensure_shop(conn, shop_id)
    cust = tools._get_customer(conn, shop_id, customer or "")
    if cust is None:
        return None
    rows = conn.execute(
        "SELECT s.created_at, s.product_name, s.qty, s.total_cents,"
        " p.pack_name, p.pack_size FROM sales s"
        " LEFT JOIN products p ON p.id = s.product_id"
        " WHERE s.shop_id = ? AND s.customer_id = ? AND s.reversed = 0"
        " ORDER BY s.created_at", (shop_id, cust["id"])).fetchall()
    items = [{
        "date": (r["created_at"] or "")[:10],
        "item": r["product_name"],
        "qty": tools._fmt_on_hand(r["qty"] or 0, r["pack_name"], r["pack_size"]),
        "cents": r["total_cents"],
    } for r in rows]
    if not items:
        for e in conn.execute(
                "SELECT created_at, items_text, amount_cents FROM credit_entries"
                " WHERE shop_id = ? AND customer_id = ? AND reversed = 0"
                " ORDER BY created_at", (shop_id, cust["id"])).fetchall():
            items.append({"date": (e["created_at"] or "")[:10],
                          "item": e["items_text"] or _t("stmt_item_credit"),
                          "qty": "", "cents": e["amount_cents"]})
    pays = [{
        "date": (r["created_at"] or "")[:10], "cents": r["amount_cents"],
    } for r in conn.execute(
        "SELECT created_at, amount_cents FROM payments"
        " WHERE shop_id = ? AND customer_id = ? AND reversed = 0"
        " ORDER BY created_at", (shop_id, cust["id"])).fetchall()]
    return {"name": cust["name"], "date": today_str(), "items": items,
            "payments": pays,
            "total": tools.balance_cents(conn, shop_id, cust["id"])}


def _statement_page(data: dict, brand: dict, path: str, items: list,
                    page_no: int, pages: int) -> str:
    from PIL import Image, ImageDraw
    W, H = 1080, 1520
    img = Image.new("RGB", (W, H), _hex(SISI["paper"]))
    y = _png_brand_header(img, brand,
                            f"{_t('doc_title_stmt')} · {data['name']}",
                            data["date"])
    dr = ImageDraw.Draw(img)
    f_mid, f_small, f_big = _card_font(44), _card_font(34), _card_font(60)
    for it in items:
        dr.text((60, y), f"{it['date']}  {it['item']}"[:48], font=f_small,
                fill=_hex(SISI["ink"]))
        y += 50
        left = f"  {it['qty']}" if it["qty"] else ""
        dr.text((60, y), left, font=f_small, fill=_hex(SISI["muted"]))
        val = cents_to_rand(it["cents"])
        dr.text((W - 60 - dr.textlength(val, font=f_small), y), val,
                font=f_small, fill=_hex(SISI["ink"]))
        y += 62
    if page_no == pages:
        y += 10
        dr.line([60, y, W - 60, y], fill=_hex(SISI["line"]), width=3)
        y += 24
        if data["payments"]:
            dr.text((60, y), _t("doc_payments"), font=f_mid,
                    fill=_hex(SISI["primary"]))
            y += 60
            for p in data["payments"][-6:]:
                dr.text((60, y), p["date"], font=f_small,
                        fill=_hex(SISI["muted"]))
                val = "−" + cents_to_rand(p["cents"])
                dr.text((W - 60 - dr.textlength(val, font=f_small), y), val,
                        font=f_small, fill=_hex(SISI["good"]))
                y += 50
            y += 10
        dr.text((60, y), _t("doc_total_out"), font=f_mid,
                fill=_hex(SISI["ink"]))
        y += 62
        dr.text((60, y), cents_to_rand(data["total"]), font=f_big,
                fill=_hex(SISI["primary"]))
        y += 110
        ask = _t("stmt_ask")
        ask = ask.replace(" \U0001f64f", "")  # PNG fonts have no emoji
        dr.text((60, y), ask, font=f_small, fill=_hex(SISI["ink"]))
    if pages > 1:
        dr.text((W - 200, H - 90), f"{page_no}/{pages}", font=f_small,
                fill=_hex(SISI["muted"]))
    _png_footer(img)
    img.save(path, "PNG")
    return path


def render_statement_pngs(data: dict, brand: dict, prefix: str,
                          per_page: int = 10) -> list[str]:
    chunks = [data["items"][i:i + per_page]
              for i in range(0, max(len(data["items"]), 1), per_page)]
    if len(chunks[-1]) > 5:
        chunks.append([])  # totals get their own page
    pages = len(chunks)
    return [_statement_page(data, brand, f"{prefix}-p{n + 1}.png", chunk,
                            n + 1, pages)
            for n, chunk in enumerate(chunks)]


def render_statement_pdf(data: dict, brand: dict, path: str) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer)
    from reportlab.platypus import Paragraph as P
    st = _pdf_styles()
    doc = SimpleDocTemplate(path, pagesize=A4, topMargin=18 * mm,
                            bottomMargin=18 * mm,
                            title=f"Statement {data['name']}", author="Sisi",
                            pageCompression=0)
    W = A4[0] - doc.leftMargin - doc.rightMargin
    story: list = []
    _pdf_brand_header(story, st, brand,
                      f"{_t('doc_title_stmt')} · {data['name']}",
                      data["date"], W)
    rows = [[P(f"<b>{_t('doc_th_date')}</b>", st["cell"]),
             P(f"<b>{_t('doc_th_item')}</b>", st["cell"]),
             P(f"<b>{_t('doc_th_qty')}</b>", st["cell"]),
             P(f"<b>{_t('doc_th_amount')}</b>", st["cell_r"])]]
    for it in data["items"]:
        rows.append([P(it["date"], st["cell"]), P(it["item"], st["cell"]),
                     P(it["qty"], st["cell"]),
                     P(cents_to_rand(it["cents"]), st["cell_r"])])
    story.append(_pdf_table(rows, [W * 0.18, W * 0.42, W * 0.22, W * 0.18]))
    story.append(Spacer(1, 6))
    if data["payments"]:
        story.append(Paragraph(_t("doc_payments"), st["h2"]))
        story.append(_pdf_table(_kv_rows(st, [
            (p["date"], "−" + cents_to_rand(p["cents"]))
            for p in data["payments"]], bold_last=False), [W - 130, 130]))
        story.append(Spacer(1, 6))
    story.append(Paragraph(
        f"<b>\U0001f4b0 {_t('doc_total_out')}:"
        f" {cents_to_rand(data['total'])}</b>", st["body"]))
    story.append(Spacer(1, 4))
    story.append(Paragraph(_t("stmt_ask"), st["body"]))
    doc.build(story, onFirstPage=_pdf_footer, onLaterPages=_pdf_footer)
    return path


def _pdf_footer(canvas, doc) -> None:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(HexColor(SISI["muted"]))
    canvas.drawCentredString(A4[0] / 2, 12 * mm,
                             f"{_t('doc_footer')} · {_t('doc_page')} {doc.page}")
    canvas.restoreState()


def _pdf_brand_header(story: list, st: dict, brand: dict, doc_title: str,
                      date_str: str, width: float) -> None:
    """Owner logo + shop name/address/phone + document title (or name-only)."""
    from reportlab.platypus import Image as RLImage
    from reportlab.platypus import Paragraph, Spacer
    cells: list = []
    logo_w = 0.0
    if brand["logo"]:
        try:
            from PIL import Image as PILImage
            with PILImage.open(brand["logo"]) as im:
                aspect = im.width / max(im.height, 1)
            logo_h = 30.0
            logo_w = min(max(logo_h * aspect, 12.0), 90.0)
            cells.append(RLImage(brand["logo"], width=logo_w, height=logo_h))
        except Exception:
            cells.append(Paragraph("", st["body"]))
    lines = [f"<b>{brand['name']}</b>"]
    contact = " · ".join(p for p in (brand["address"], brand["phone"]) if p)
    if contact:
        lines.append(contact)
    lines.append(f"{doc_title} · {date_str}")
    cells.append(Paragraph("<br/>".join(lines), st["body"]))
    widths = [logo_w + 8, width - logo_w - 8] if brand["logo"] else [width]
    story.append(_pdf_table([cells if brand["logo"] else cells], widths, header=False))
    story.append(Spacer(1, 6))


def render_pdf(d: dict, path: str, brand: dict | None = None) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import (PageBreak, Paragraph, SimpleDocTemplate, Spacer)
    st = _pdf_styles()
    doc = SimpleDocTemplate(path, pagesize=A4, topMargin=18 * mm,
                            bottomMargin=18 * mm, title=f"Sisi month-end {d['label']}",
                            author="Sisi", pageCompression=0)
    W = A4[0] - doc.leftMargin - doc.rightMargin
    story: list = []
    _pdf_brand_header(story, st, brand or {"name": d["shop"], "address": "",
                                           "phone": "", "logo": ""},
                      _t("doc_title_month"), d["label"], W)
    story += [
        Paragraph(_esc(_t("doc_pl")), st["h1"]),
    ]
    _money_bar(story, st, [(_t("doc_sales"), d["sales"], SISI["primary"]),
                           (_t("doc_bar_costs"), d["cogs"], SISI["accent"]),
                           (_t("doc_bar_exp"), d["exp_total"], "#A8A29E")], W)
    story.append(Spacer(1, 6))
    margin = f" ({round(d['gross'] * 100 / d['sales'])}%)" if d["sales"] else ""
    story.append(_pdf_table(_kv_rows(st, [
        (_t("doc_sales"), cents_to_rand(d["sales"])),
        (_t("doc_opening_stock"), cents_to_rand(d["opening_value"])),
        (_t("doc_purchases"), cents_to_rand(d["purchases"])),
        (_t("doc_closing_stock"), f"({cents_to_rand(d['closing_value'])})"),
        (_t("doc_cogs"), cents_to_rand(d["cogs"])),
        (_t("doc_gross") + margin, cents_to_rand(d["gross"])),
    ]), [W - 130, 130]))
    story.append(Paragraph(_t("doc_expenses"), st["h2"]))
    exp_items = [(_cat_name(e["category"]), cents_to_rand(e["cents"]))
                 for e in d["expenses"]] or [(_t("doc_none"), cents_to_rand(0))]
    exp_items.append((_t("doc_net"), cents_to_rand(d["net"])))
    story.append(_pdf_table(_kv_rows(st, exp_items), [W - 130, 130]))
    story.append(PageBreak())
    story.append(Paragraph(_t("doc_bs"), st["h1"]))
    story.append(_pdf_table(_kv_rows(st, [
        (_t("doc_stock_cost"), cents_to_rand(d["closing_value"])),
        (_acct_name("cash"), cents_to_rand(d["cash"])),
        (_acct_name("petty"), cents_to_rand(d["petty"])),
        (_acct_name("bank"), cents_to_rand(d["bank"])),
        (_t("doc_debtors"), cents_to_rand(d["debt_total"])),
        (_t("doc_assets"), cents_to_rand(d["assets"])),
    ]), [W - 130, 130]))
    story.append(Spacer(1, 6))
    story.append(_pdf_table(_kv_rows(st, [
        (_t("doc_suppliers"), cents_to_rand(d["cred_total"])),
        (_t("doc_equity"), cents_to_rand(d["equity"])),
        (_t("doc_lia"), cents_to_rand(d["cred_total"] + d["equity"])),
    ]), [W - 130, 130]))
    story.append(PageBreak())
    story.append(Paragraph(_t("doc_cf"), st["h1"]))
    story.append(_pdf_table(_kv_rows(st, [
        (_t("doc_open_cash"), cents_to_rand(d["opening_cash"])),
        (_t("doc_cash_sales"), "+" + cents_to_rand(d["cash_sales"])),
        (_t("doc_collected"), "+" + cents_to_rand(d["collected"])),
        (_t("doc_count_adj"), cents_to_rand(d["count_adj"])),
        (_t("doc_paid"), "−" + cents_to_rand(d["paid_cash"])),
        (_t("doc_exp_paid"), "−" + cents_to_rand(d["exp_cash"])),
        (_t("doc_draws"), "−" + cents_to_rand(d["draws"])),
        (_t("doc_close_cash"), cents_to_rand(d["closing_cash"])),
    ]), [W - 130, 130]))
    story.append(PageBreak())
    story.append(Paragraph(_t("doc_stock"), st["h1"]))
    story.append(Paragraph(_t("doc_stock_sub", n=len(d['stock_lines']),
                             v=cents_to_rand(d['closing_value'])),
                           st["muted"]))
    story.append(Spacer(1, 4))
    from reportlab.platypus import Paragraph as P
    rows = [[P(f"<b>{_t('doc_th_item')}</b>", st["cell"]),
             P(f"<b>{_t('doc_th_onhand')}</b>", st["cell"]),
             P(f"<b>{_t('doc_th_cost')}</b>", st["cell_r"]),
             P(f"<b>{_t('doc_th_value')}</b>", st["cell_r"])]]
    for s in d["stock_lines"]:
        rows.append([P(s["name"], st["cell"]), P(s["on_hand"], st["cell"]),
                     P(cents_to_rand(s["cost_cents"]), st["cell_r"]),
                     P(cents_to_rand(s["value_cents"]), st["cell_r"])])
    story.append(_pdf_table(rows, [W * 0.4, W * 0.3, W * 0.15, W * 0.15]))
    story.append(PageBreak())
    story.append(Paragraph(_t("doc_reorder"), st["h1"]))
    if d["reorder"]:
        r_rows = [[P(f"<b>{_t('doc_th_item')}</b>", st["cell"]),
                   P(f"<b>{_t('doc_th_qty')}</b>", st["cell"]),
                   P(f"<b>{_t('doc_th_est')}</b>", st["cell_r"])]]
        last_w = None
        for r in d["reorder"]:
            if r["wholesaler"] != last_w:
                r_rows.append([P(f"<b>{r['wholesaler'] or _t('no_wholesaler')}</b>",
                                 st["cell"]), P("", st["cell"]), P("", st["cell"])])
                last_w = r["wholesaler"]
            qty = (f"{r['packs']} {r['pack_name'] or _t('doc_packs')}"
                   if r["packs"] else f"{r['units']} {_t('units_word')}")
            r_rows.append([P(r["name"], st["cell"]), P(qty, st["cell"]),
                           P(cents_to_rand(r["est_cents"]) if r["est_cents"] is not None
                             else "—", st["cell_r"])])
        story.append(_pdf_table(r_rows, [W * 0.45, W * 0.3, W * 0.25]))
    else:
        story.append(Paragraph(_t("doc_no_reorder"), st["body"]))
    story.append(Spacer(1, 10))
    story.append(Paragraph(_t("doc_insights_h"), st["h1"]))
    for i in d["insights"] or [_t("doc_no_data")]:
        story.append(Paragraph(f"• {i}", st["body"]))
        story.append(Spacer(1, 3))
    if d["best"]:
        story.append(Spacer(1, 4))
        story.append(Paragraph(_t("doc_best"), st["h2"]))
        story.append(_pdf_table(_kv_rows(st, [
            (b["name"], _t("doc_sold", qty=b['qty'],
                           amt=cents_to_rand(b['cents'])))
            for b in d["best"]], bold_last=False), [W - 170, 170]))
    doc.build(story, onFirstPage=_pdf_footer, onLaterPages=_pdf_footer)
    return path


# ---------------------------------------------------------------- PNG card

def _card_font(size: int):
    from PIL import ImageFont
    for path in ("/System/Library/Fonts/Helvetica.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/dejavu/DejaVuSans.ttf",
                 "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _hex(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _png_brand_header(img, brand: dict, doc_title: str, date_str: str,
                      height: int = 300) -> int:
    """Shared green header: logo, shop name, contact, doc title. Returns y."""
    from PIL import ImageDraw
    W = img.width
    dr = ImageDraw.Draw(img)
    dr.rectangle([0, 0, W, height], fill=_hex(SISI["primary"]))
    dr.rectangle([0, height, W, height + 16], fill=_hex(SISI["accent"]))
    _paste_logo(img, brand["logo"], (W - 220, 60, 160))
    dr.text((60, 50), brand["name"], font=_card_font(64), fill="white")
    contact = " · ".join(p for p in (brand["address"], brand["phone"]) if p)
    y = 140
    if contact:
        dr.text((60, y), contact[:60], font=_card_font(36), fill="white")
        y += 60
    dr.text((60, y), f"{doc_title} · {date_str}", font=_card_font(44),
            fill="#FDE68A")
    return height + 60


def _png_footer(img, y: int | None = None) -> None:
    from PIL import ImageDraw
    dr = ImageDraw.Draw(img)
    dr.text((60, img.height - 90), _t("doc_footer"), font=_card_font(36),
            fill=_hex(SISI["muted"]))


def make_sample_logo(path: str, initial: str = "S") -> str:
    """Demo-only sample logo. Real shops always supply their own."""
    from PIL import Image, ImageDraw
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (512, 512), _hex(SISI["primary"]))
    dr = ImageDraw.Draw(img)
    dr.ellipse([56, 56, 456, 456], outline=_hex(SISI["accent"]), width=18)
    f = _card_font(280)
    w = dr.textlength(initial, font=f)
    dr.text(((512 - w) / 2, 90), initial, font=f, fill="white")
    img.save(path, "PNG")
    return path


def render_snapshot_png(data: dict, brand: dict, path: str, day: str) -> str:
    from PIL import Image, ImageDraw
    W, H = 1080, 1350
    img = Image.new("RGB", (W, H), _hex(SISI["paper"]))
    y = _png_brand_header(img, brand, _t("doc_snap_title"), day)
    dr = ImageDraw.Draw(img)
    f_mid, f_small = _card_font(44), _card_font(36)
    rows = [(_t("doc_week_sales"), cents_to_rand(data["sales"])),
            (_t("doc_gross"), cents_to_rand(data["gross"])),
            (_t("doc_cash_hand"), cents_to_rand(data["cash"])),
            (_t("doc_owed_you"), cents_to_rand(data["owed"]))]
    for label, value in rows:
        dr.text((60, y), label, font=f_mid, fill=_hex(SISI["ink"]))
        vw = dr.textlength(value, font=f_mid)
        dr.text((W - 60 - vw, y), value, font=f_mid,
                fill=_hex(SISI["primary"]))
        y += 100
    y += 10
    dr.line([60, y, W - 60, y], fill=_hex(SISI["line"]), width=3)
    y += 30
    dr.text((60, y), _t("doc_best_line", x=data['best'])[:52], font=f_small,
            fill=_hex(SISI["ink"]))
    y += 60
    dr.text((60, y), _t("doc_slow_line", x=data['slowest'])[:52],
            font=f_small, fill=_hex(SISI["ink"]))
    y += 90
    dr.text((60, y), _t("doc_tip"), font=f_mid,
            fill=_hex(SISI["primary"]))
    y += 70
    words, line = data["tip"].split(), ""
    for w in words:
        test = (line + " " + w).strip()
        if dr.textlength(test, font=f_small) > W - 120:
            dr.text((60, y), line, font=f_small, fill=_hex(SISI["ink"]))
            y += 52
            line = w
        else:
            line = test
    if line and y < H - 160:
        dr.text((60, y), line, font=f_small, fill=_hex(SISI["ink"]))
    _png_footer(img)
    img.save(path, "PNG")
    return path


def render_order_png(payload: dict, brand: dict, path: str, day: str) -> str:
    from PIL import Image, ImageDraw
    W, H = 1080, 1520
    img = Image.new("RGB", (W, H), _hex(SISI["paper"]))
    y = _png_brand_header(img, brand, _t("doc_order_title"), day)
    dr = ImageDraw.Draw(img)
    f_mid, f_small = _card_font(44), _card_font(34)
    groups: dict[str, list] = {}
    for ln in payload["lines"]:
        groups.setdefault(ln["wholesaler"] or _t("no_wholesaler"),
                        []).append(ln)
    for w, items in groups.items():
        if y > H - 320:
            break
        dr.text((60, y), w[:40], font=f_mid, fill=_hex(SISI["primary"]))
        y += 60
        for ln in items:
            if y > H - 260:
                break
            if ln["packs"]:
                qty = f"{ln['packs']} {ln['pack_name'] or _t('doc_packs')}"
            else:
                qty = f"{ln['units']} {_t('units_word')}"
            dr.text((60, y), f"• {ln['name']} x {qty}"[:48], font=f_small,
                    fill=_hex(SISI["ink"]))
            if ln["est_cents"] is not None:
                val = cents_to_rand(ln["est_cents"])
                dr.text((W - 60 - dr.textlength(val, font=f_small), y), val,
                        font=f_small, fill=_hex(SISI["muted"]))
            y += 52
        y += 14
    known = [ln["est_cents"] for ln in payload["lines"]
             if ln["est_cents"] is not None]
    dr.line([60, H - 220, W - 60, H - 220], fill=_hex(SISI["line"]), width=3)
    dr.text((60, H - 180),
            _t("doc_order_total", x=cents_to_rand(sum(known))),
            font=f_mid, fill=_hex(SISI["ink"]))
    _png_footer(img)
    img.save(path, "PNG")
    return path


def render_shop_card(brand: dict, path: str) -> str:
    from PIL import Image, ImageDraw
    W, H = 1080, 640
    img = Image.new("RGB", (W, H), _hex(SISI["paper"]))
    y = _png_brand_header(img, brand, _t("doc_profile_title"), today_str())
    dr = ImageDraw.Draw(img)
    dr.text((60, y),
            f"{_t('doc_title_stmt')} · {_t('doc_total_out')} R0.00",
            font=_card_font(40), fill=_hex(SISI["ink"]))
    dr.text((60, y + 62), _t("doc_profile_note"),
            font=_card_font(34), fill=_hex(SISI["muted"]))
    _png_footer(img)
    img.save(path, "PNG")
    return path


def render_logo_compare(before_path: str, after_path: str, path: str) -> str:
    """Before/after logo card for the redo-logo command (1080x640 PNG)."""
    from PIL import Image, ImageDraw
    W, H = 1080, 640
    img = Image.new("RGB", (W, H), _hex(SISI["paper"]))
    dr = ImageDraw.Draw(img)
    dr.rectangle([0, 0, W, 150], fill=_hex(SISI["primary"]))
    dr.text((60, 45), _t("logo_redo_title"), font=_card_font(52), fill="white")
    for i, (src, label) in enumerate(((before_path, _t("logo_before")),
                                     (after_path, _t("logo_after")))):
        x0 = 60 + i * 500
        panel = Image.new("RGB", (440, 330), "white")
        try:
            art = Image.open(src).convert("RGBA")
            art.thumbnail((400, 260), Image.LANCZOS)
            panel.paste(art, ((440 - art.width) // 2, 20), art)
        except OSError:
            pass
        img.paste(panel, (x0, 190))
        dr.text((x0, 535), label, font=_card_font(40),
                fill=_hex(SISI["ink"]))
    _png_footer(img)
    img.save(path, "PNG")
    return path


def _paste_logo(img, logo_path: str, box: tuple[int, int, int]) -> None:
    """Paste the owner logo with transparency, scaled to the header height.

    No box or border; a subtle drop shadow keeps cut-outs readable on the
    dark green header. Silent no-op when missing.
    """
    if not logo_path:
        return
    from PIL import Image, ImageFilter
    try:
        logo = Image.open(logo_path).convert("RGBA")
    except OSError:
        return
    x, y, size = box
    logo.thumbnail((size, size), Image.LANCZOS)
    px, py = x + (size - logo.width) // 2, y + (size - logo.height) // 2
    base = img.convert("RGBA")
    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    alpha = logo.getchannel("A").point(lambda v: v * 60 // 255)
    blob = Image.new("RGBA", (logo.width, logo.height), (0, 0, 0, 0))
    blob.putalpha(alpha)
    shadow.paste(blob, (px + 4, py + 6), blob)
    shadow = shadow.filter(ImageFilter.GaussianBlur(6))
    base.alpha_composite(shadow)
    base.paste(logo, (px, py), logo)
    img.paste(base.convert("RGB"))


def render_card(d: dict, path: str, brand: dict | None = None) -> str:
    from PIL import Image, ImageDraw
    b = brand or {"name": d["shop"], "address": "", "phone": "", "logo": ""}
    W, H = 1080, 1350
    img = Image.new("RGB", (W, H), _hex(SISI["paper"]))
    dr = ImageDraw.Draw(img)
    dr.rectangle([0, 0, W, 300], fill=_hex(SISI["primary"]))
    dr.rectangle([0, 300, W, 316], fill=_hex(SISI["accent"]))
    f_title = _card_font(64)
    f_big = _card_font(72)
    f_mid = _card_font(44)
    f_small = _card_font(36)
    _paste_logo(img, b["logo"], (W - 220, 60, 160))
    dr.text((60, 60), b["name"], font=f_title, fill="white")
    dr.text((60, 150), f"{_t('doc_title_month')} · {d['label']}",
            font=f_mid, fill="#FDE68A")
    contact = " · ".join(p for p in (b["address"], b["phone"]) if p)
    if contact:
        dr.text((60, 225), contact[:60], font=f_small, fill="white")
    y = 380
    margin = f"  ({round(d['gross'] * 100 / d['sales'])}%)" if d["sales"] else ""
    rows = [(_t("doc_sales"), cents_to_rand(d["sales"])),
            (_t("doc_gross_short") + margin, cents_to_rand(d["gross"])),
            (_t("doc_expenses"), cents_to_rand(d["exp_total"])),
            (_t("doc_net"), cents_to_rand(d["net"]))]
    for _n, (label, value) in enumerate(rows):
        bold = _n == len(rows) - 1
        dr.text((60, y), label, font=f_mid, fill=_hex(SISI["ink"]))
        vw = dr.textlength(value, font=f_big if bold else f_mid)
        dr.text((W - 60 - vw, y - (8 if bold else 0)), value,
                font=f_big if bold else f_mid,
                fill=_hex(SISI["primary"] if bold else SISI["ink"]))
        y += 110
    y += 10
    dr.line([60, y, W - 60, y], fill=_hex(SISI["line"]), width=3)
    y += 30
    dr.text((60, y),
            _t("doc_cash_hand") + "  " + cents_to_rand(d["cash"] + d["petty"] + d["bank"]),
            font=f_mid, fill=_hex(SISI["ink"]))
    y += 80
    dr.text((60, y),
            _t("doc_owed_you") + "  " + cents_to_rand(d["debt_total"]),
            font=f_mid, fill=_hex(SISI["ink"]))
    y += 110
    dr.line([60, y, W - 60, y], fill=_hex(SISI["line"]), width=3)
    y += 30
    dr.text((60, y), _t("doc_insights"), font=_card_font(48),
            fill=_hex(SISI["primary"]))
    y += 80
    for ins in (d["insights"] or [_t("doc_no_data")])[:3]:
        words, line, lines = ins.split(), "", []
        for w in words:
            test = (line + " " + w).strip()
            if dr.textlength(test, font=f_small) > W - 120:
                lines.append(line)
                line = w
            else:
                line = test
        lines.append(line)
        for ln in lines[:3]:
            if y > H - 160:
                break
            dr.text((60, y), ln, font=f_small, fill=_hex(SISI["ink"]))
            y += 52
        y += 12
        if y > H - 160:
            break
    dr.text((60, H - 90), _t("doc_footer"), font=f_small,
            fill=_hex(SISI["muted"]))
    img.save(path, "PNG")
    return path
