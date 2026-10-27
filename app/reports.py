"""Month-end management accounts: data, branded PDF and PNG summary card."""

from __future__ import annotations

import calendar
import math
import os
import secrets
from datetime import date, timedelta
from pathlib import Path

from app import tools
from app.db import cents_to_rand, today_str

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

REPORTS_DIR = Path(os.environ.get("SISI_REPORTS", "data/reports"))


def month_label(month: str) -> str:
    return date(int(month[:4]), int(month[5:7]), 1).strftime("%B %Y")


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
        insights.append(f"Best seller: {best[0]['name']}"
                        f" ({cents_to_rand(best[0]['cents'])}). Keep it in stock.")
    if slow:
        insights.append(f"Slow mover: {slow[0]['name']} did not sell —"
                        f" {cents_to_rand(slow[0]['cents'])} is sitting on the shelf.")
    if debtors:
        insights.append(f"Chase {debtors[0]['name']}:"
                        f" {cents_to_rand(debtors[0]['cents'])} is the biggest debt.")
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
        f"📊 *Month-end: {d['label']}*",
        f"• Sales {cents_to_rand(d['sales'])}",
        f"• Gross {cents_to_rand(d['gross'])}{margin}",
        f"• Expenses {cents_to_rand(d['exp_total'])}",
        f"• Net {cents_to_rand(d['net'])}",
        "",
        "💰 *Cash*",
        f"• In hand {cents_to_rand(d['cash'])} · petty {cents_to_rand(d['petty'])}",
        f"• Owed to you {cents_to_rand(d['debt_total'])}",
    ]
    if d["insights"]:
        lines += ["", "💡 *Insights*"] + [f"• {i}" for i in d["insights"][:3]]
    return "\n".join(lines)


def save_report_files(conn, shop_id: str, month: str) -> dict:
    """Render PDF + PNG, store row, return paths + token."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
    pdf_path = str(REPORTS_DIR / f"{safe}-{month}.pdf")
    png_path = str(REPORTS_DIR / f"{safe}-{month}.png")
    d = month_data(conn, shop_id, month)
    render_pdf(d, pdf_path)
    render_card(d, png_path)
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
        save_report_files(conn, shop_id, m)
        row = get_report(conn, shop_id, m)
        reused = False
    text = month_short(conn, shop_id, m)
    if reused:
        text += "\n\n🔁 Re-sending your saved files."
    media: list[dict] = []
    base = (base_url or "").rstrip("/")
    if base and row:
        media = [
            {"kind": "image",
             "url": f"{base}/media/{row['token']}?f=png",
             "caption": f"{month_label(m)} at a glance", "filename": ""},
            {"kind": "file",
             "url": f"{base}/media/{row['token']}?f=pdf",
             "caption": f"Full month-end PDF · {month_label(m)}",
             "filename": f"sisi-month-end-{m}.pdf"},
        ]
        text += "\n\n📎 Card + full PDF below."
    else:
        text += "\n\n📎 PDF + card saved."
    return text, media


# ---------------------------------------------------------------- PDF

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


def render_pdf(d: dict, path: str) -> str:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import (PageBreak, Paragraph, SimpleDocTemplate, Spacer)
    st = _pdf_styles()
    doc = SimpleDocTemplate(path, pagesize=A4, topMargin=18 * mm,
                            bottomMargin=18 * mm, title=f"Sisi month-end {d['label']}",
                            author="Sisi")
    W = A4[0] - doc.leftMargin - doc.rightMargin
    story = [
        Paragraph(f"{d['shop']} — Month-end {d['label']}", st["title"]),
        Spacer(1, 4),
        Paragraph("Prepared by Sisi · figures in rand", st["muted"]),
        Spacer(1, 10),
        Paragraph("1 · Profit &amp; Loss", st["h1"]),
    ]
    _money_bar(story, st, [("Sales", d["sales"], SISI["primary"]),
                           ("Costs", d["cogs"], SISI["accent"]),
                           ("Expenses", d["exp_total"], "#A8A29E")], W)
    story.append(Spacer(1, 6))
    margin = f" ({round(d['gross'] * 100 / d['sales'])}%)" if d["sales"] else ""
    story.append(_pdf_table(_kv_rows(st, [
        ("Sales", cents_to_rand(d["sales"])),
        ("Opening stock", cents_to_rand(d["opening_value"])),
        ("Purchases", cents_to_rand(d["purchases"])),
        ("Closing stock", f"({cents_to_rand(d['closing_value'])})"),
        ("Cost of sales", cents_to_rand(d["cogs"])),
        ("Gross profit" + margin, cents_to_rand(d["gross"])),
    ]), [W - 130, 130]))
    story.append(Paragraph("Expenses", st["h2"]))
    exp_items = [(e["category"].capitalize(), cents_to_rand(e["cents"]))
                 for e in d["expenses"]] or [("None recorded", cents_to_rand(0))]
    exp_items.append(("Net profit", cents_to_rand(d["net"])))
    story.append(_pdf_table(_kv_rows(st, exp_items), [W - 130, 130]))
    story.append(PageBreak())
    story.append(Paragraph("2 · Balance Sheet", st["h1"]))
    story.append(_pdf_table(_kv_rows(st, [
        ("Stock at cost", cents_to_rand(d["closing_value"])),
        ("Cash in hand", cents_to_rand(d["cash"])),
        ("Petty cash", cents_to_rand(d["petty"])),
        ("Bank", cents_to_rand(d["bank"])),
        ("Owed to you (debtors)", cents_to_rand(d["debt_total"])),
        ("Total assets", cents_to_rand(d["assets"])),
    ]), [W - 130, 130]))
    story.append(Spacer(1, 6))
    story.append(_pdf_table(_kv_rows(st, [
        ("Owed to suppliers", cents_to_rand(d["cred_total"])),
        ("Owner's equity", cents_to_rand(d["equity"])),
        ("Liabilities + equity", cents_to_rand(d["cred_total"] + d["equity"])),
    ]), [W - 130, 130]))
    story.append(PageBreak())
    story.append(Paragraph("3 · Cash Flow", st["h1"]))
    story.append(_pdf_table(_kv_rows(st, [
        ("Opening cash", cents_to_rand(d["opening_cash"])),
        ("Cash sales", "+" + cents_to_rand(d["cash_sales"])),
        ("Credit collected", "+" + cents_to_rand(d["collected"])),
        ("Till count adjustments", cents_to_rand(d["count_adj"])),
        ("Purchases paid", "−" + cents_to_rand(d["paid_cash"])),
        ("Expenses paid", "−" + cents_to_rand(d["exp_cash"])),
        ("Drawings", "−" + cents_to_rand(d["draws"])),
        ("Closing cash", cents_to_rand(d["closing_cash"])),
    ]), [W - 130, 130]))
    story.append(PageBreak())
    story.append(Paragraph("4 · Stock on Hand", st["h1"]))
    story.append(Paragraph(f"{len(d['stock_lines'])} lines ·"
                           f" worth {cents_to_rand(d['closing_value'])} at cost",
                           st["muted"]))
    story.append(Spacer(1, 4))
    from reportlab.platypus import Paragraph as P
    rows = [[P("<b>Item</b>", st["cell"]), P("<b>On hand</b>", st["cell"]),
             P("<b>Cost</b>", st["cell_r"]), P("<b>Value</b>", st["cell_r"])]]
    for s in d["stock_lines"]:
        rows.append([P(s["name"], st["cell"]), P(s["on_hand"], st["cell"]),
                     P(cents_to_rand(s["cost_cents"]), st["cell_r"]),
                     P(cents_to_rand(s["value_cents"]), st["cell_r"])])
    story.append(_pdf_table(rows, [W * 0.4, W * 0.3, W * 0.15, W * 0.15]))
    story.append(PageBreak())
    story.append(Paragraph("5 · Reorder Suggestions", st["h1"]))
    if d["reorder"]:
        r_rows = [[P("<b>Item</b>", st["cell"]), P("<b>Qty</b>", st["cell"]),
                   P("<b>Est. cost</b>", st["cell_r"])]]
        last_w = None
        for r in d["reorder"]:
            if r["wholesaler"] != last_w:
                r_rows.append([P(f"<b>{r['wholesaler'] or 'No wholesaler'}</b>",
                                 st["cell"]), P("", st["cell"]), P("", st["cell"])])
                last_w = r["wholesaler"]
            qty = (f"{r['packs']} {r['pack_name'] or 'packs'}"
                   if r["packs"] else f"{r['units']} units")
            r_rows.append([P(r["name"], st["cell"]), P(qty, st["cell"]),
                           P(cents_to_rand(r["est_cents"]) if r["est_cents"] is not None
                             else "—", st["cell_r"])])
        story.append(_pdf_table(r_rows, [W * 0.45, W * 0.3, W * 0.25]))
    else:
        story.append(Paragraph("Nothing to reorder. Stock is fine.", st["body"]))
    story.append(Spacer(1, 10))
    story.append(Paragraph("6 · Insights", st["h1"]))
    for i in d["insights"] or ["Not enough data yet — keep selling!"]:
        story.append(Paragraph(f"• {i}", st["body"]))
        story.append(Spacer(1, 3))
    if d["best"]:
        story.append(Spacer(1, 4))
        story.append(Paragraph("Best sellers", st["h2"]))
        story.append(_pdf_table(_kv_rows(st, [
            (b["name"], f"{b['qty']} sold · {cents_to_rand(b['cents'])}")
            for b in d["best"]], bold_last=False), [W - 170, 170]))
    doc.build(story)
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


def render_card(d: dict, path: str) -> str:
    from PIL import Image, ImageDraw
    W, H = 1080, 1350
    img = Image.new("RGB", (W, H), _hex(SISI["paper"]))
    dr = ImageDraw.Draw(img)
    dr.rectangle([0, 0, W, 300], fill=_hex(SISI["primary"]))
    dr.rectangle([0, 300, W, 316], fill=_hex(SISI["accent"]))
    f_title = _card_font(64)
    f_big = _card_font(72)
    f_mid = _card_font(44)
    f_small = _card_font(36)
    dr.text((60, 60), d["shop"], font=f_title, fill="white")
    dr.text((60, 150), f"Month-end · {d['label']}", font=f_mid, fill="#FDE68A")
    y = 380
    margin = f"  ({round(d['gross'] * 100 / d['sales'])}%)" if d["sales"] else ""
    rows = [("Sales", cents_to_rand(d["sales"])),
            ("Gross" + margin, cents_to_rand(d["gross"])),
            ("Expenses", cents_to_rand(d["exp_total"])),
            ("Net profit", cents_to_rand(d["net"]))]
    for label, value in rows:
        bold = label.startswith("Net")
        dr.text((60, y), label, font=f_mid, fill=_hex(SISI["ink"]))
        vw = dr.textlength(value, font=f_big if bold else f_mid)
        dr.text((W - 60 - vw, y - (8 if bold else 0)), value,
                font=f_big if bold else f_mid,
                fill=_hex(SISI["primary"] if bold else SISI["ink"]))
        y += 110
    y += 10
    dr.line([60, y, W - 60, y], fill=_hex(SISI["line"]), width=3)
    y += 30
    dr.text((60, y), "Cash in hand  " + cents_to_rand(d["cash"] + d["petty"] + d["bank"]),
            font=f_mid, fill=_hex(SISI["ink"]))
    y += 80
    dr.text((60, y), "Owed to you  " + cents_to_rand(d["debt_total"]),
            font=f_mid, fill=_hex(SISI["ink"]))
    y += 110
    dr.line([60, y, W - 60, y], fill=_hex(SISI["line"]), width=3)
    y += 30
    dr.text((60, y), "Insights", font=_card_font(48), fill=_hex(SISI["primary"]))
    y += 80
    for ins in (d["insights"] or ["Not enough data yet — keep selling!"])[:3]:
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
            dr.text((60, y), ln, font=f_small, fill=_hex(SISI["ink"]))
            y += 52
        y += 12
    dr.text((60, H - 90), "Prepared by Sisi", font=f_small, fill=_hex(SISI["muted"]))
    img.save(path, "PNG")
    return path
