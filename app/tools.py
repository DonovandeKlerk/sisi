"""Shop tools with dry-run preview + deterministic confirm/undo.

Write tools (add_credit, record_payment, record_sale, add_stock, set_price,
check_expiry_photo, read_slip_photo) run their writes inside a rolled-back
transaction to produce an exact preview, store a pending action, and only
execute for real when the owner replies Yes. Read tools answer directly.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone

from app.db import cents_to_rand, now_iso, today_str
from app.i18n import acct_name as _acct_name
from app.i18n import cat_name as _cat_name
from app.i18n import doc_lang as _doc_lang
from app.i18n import owner_lang as _owner_lang
from app.i18n import month_year as _month_year
from app.i18n import rcpt_short as _rcpt_short
from app.i18n import t as _t

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _action_id() -> str:
    return "a" + uuid.uuid4().hex[:11]


@contextlib.contextmanager
def _dry_run(conn: sqlite3.Connection):
    """Run writes that are always rolled back, so previews are exact."""
    conn.execute("SAVEPOINT sisi_dry")
    try:
        yield
    finally:
        conn.execute("ROLLBACK TO sisi_dry")
        conn.execute("RELEASE sisi_dry")


def _fmt_qty(qty: float) -> str:
    if float(qty).is_integer():
        return str(int(qty))
    return f"{qty:g}"


def _fmt_date(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d %b")
    except ValueError:
        return iso


def ensure_shop(conn: sqlite3.Connection, shop_id: str, name: str | None = None) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM shops WHERE shop_id = ?", (shop_id,)).fetchone()
    if row:
        return row
    conn.execute(
        "INSERT INTO shops (shop_id, name) VALUES (?, ?)",
        (shop_id, name or f"Shop {shop_id}"),
    )
    conn.commit()
    return conn.execute("SELECT * FROM shops WHERE shop_id = ?", (shop_id,)).fetchone()


def _get_customer(conn: sqlite3.Connection, shop_id: str, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM customers WHERE shop_id = ? AND name = ?",
        (shop_id, name.strip()),
    ).fetchone()


def _get_or_create_customer(conn: sqlite3.Connection, shop_id: str, name: str) -> tuple[sqlite3.Row, bool]:
    row = _get_customer(conn, shop_id, name)
    if row:
        return row, False
    cur = conn.execute(
        "INSERT INTO customers (shop_id, name) VALUES (?, ?)",
        (shop_id, name.strip()),
    )
    return conn.execute("SELECT * FROM customers WHERE id = ?", (cur.lastrowid,)).fetchone(), True


_NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}


def _normalize_query(query: str) -> str:
    """Lowercase, number words to digits, litre/liter to l ("two litre Coke" -> "2 l coke")."""
    out = []
    for word in query.strip().lower().replace(",", " ").split():
        word = _NUMBER_WORDS.get(word, word)
        if word in ("litre", "litres", "liter", "liters"):
            word = "l"
        out.append(word)
    return " ".join(out)


def _find_products(conn: sqlite3.Connection, shop_id: str, query: str):
    """Returns (unique_row_or_None, ambiguous_rows). Exact match wins, else every
    query word must appear in the product name ("Sasko bread" finds Sasko White Bread).
    Local words in any language match via ITEM_EQUIV ("isonka" finds bread)."""
    from app.i18n import ITEM_EQUIV
    q = _normalize_query(query)
    rows = conn.execute("SELECT * FROM products WHERE shop_id = ?", (shop_id,)).fetchall()
    for r in rows:
        if r["name"].lower() == q:
            return r, []
    toks = [t for t in q.split() if len(t) > 1]
    equiv = [{t} | set(ITEM_EQUIV.get(t, ())) for t in toks]
    hits = [r for r in rows
            if toks and all(any(e in r["name"].lower() for e in cands)
                            for cands in equiv)]
    if not hits:  # fallback: a full product name inside a longer phrase
        hits = [r for r in rows if r["name"].lower() in q]
    if len(hits) == 1:
        return hits[0], []
    return None, hits


_SINGLE_WORDS = {"single", "singles", "each", "loose", "unit", "units",
                 "piece", "pieces"}
_PACK_WORDS = {"box", "boxes", "tray", "trays", "pack", "packs", "packet", "packets",
               "case", "cases", "carton", "cartons", "sleeve", "sleeves",
               "bundle", "bundles", "crate", "crates",
               "ibhokisi", "amabhokisi", "itreyi", "amatreyi",
               "ipakethe", "amapakethe", "iphakethe", "amaphakethe",
               "ikhathoni", "amakhathoni"}


def _resolve_units(pack_name: str | None, pack_size: float | None,
                   qty: float, unit: str | None) -> tuple[float, bool, bool]:
    """Convert a quoted qty to base units. Returns (units, is_pack, no_pack_warn).

    Bare numbers and single-words are singles. Pack words convert via the
    product's pack size; an unknown word assumes singles (the preview shows
    the reading so the owner can say No once).
    """
    u = (unit or "").strip().lower()
    toks = {t.strip(" .!?,") for t in u.split()}
    if not u or toks & _SINGLE_WORDS:
        return qty, False, False
    pack_hit = (bool(toks & _PACK_WORDS) or any(t.endswith("-pack") for t in toks)
                or (pack_name and pack_name.lower() in u))
    if not pack_hit:
        return qty, False, False
    if pack_size:
        return qty * pack_size, True, False
    return qty, False, True


def balance_cents(conn: sqlite3.Connection, shop_id: str, customer_id: int) -> int:
    credit = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) s FROM credit_entries"
        " WHERE shop_id = ? AND customer_id = ? AND reversed = 0",
        (shop_id, customer_id),
    ).fetchone()["s"]
    paid = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) s FROM payments"
        " WHERE shop_id = ? AND customer_id = ? AND reversed = 0",
        (shop_id, customer_id),
    ).fetchone()["s"]
    return credit - paid


def _items_text(lines: list[dict]) -> str:
    return ", ".join(ln.get("short") or f"{_fmt_qty(ln['qty'])}x {ln['name']}" for ln in lines)


_PACK_SINGULAR = {"boxes": "box", "trays": "tray", "packs": "pack", "packets": "packet",
                  "cases": "case", "cartons": "carton", "sleeves": "sleeve",
                  "bundles": "bundle", "crates": "crate"}


def _pack_echo(unit: str | None) -> str:
    for tok in (unit or "").lower().split():
        tok = tok.strip(" .!?,")
        if tok in _PACK_WORDS:
            return _PACK_SINGULAR.get(tok, tok)
        if tok.endswith("-pack"):
            return "pack"
    return "pack"


def _which_one(ambiguous) -> str:
    lines = [_t("which_one_head")] + [_t("which_one_line", name=p["name"])
                                     for p in ambiguous[:3]]
    return "\n".join(lines)


def _ask_price(name: str) -> str:
    return _t("ask_price", name=name)


# ---------------------------------------------------------------------------
# Pending actions: propose / confirm / cancel
# ---------------------------------------------------------------------------


def get_pending(conn: sqlite3.Connection, shop_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM pending_actions WHERE shop_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
        (shop_id,),
    ).fetchone()


def _propose(conn: sqlite3.Connection, shop_id: str, action_type: str, payload: dict, preview: str,
             *, commit: bool = True) -> str:
    conn.execute(
        "UPDATE pending_actions SET status = 'superseded', resolved_at = ?"
        " WHERE shop_id = ? AND status = 'pending'",
        (now_iso(), shop_id),
    )
    conn.execute(
        "INSERT INTO pending_actions (shop_id, action_type, payload_json, preview_text,"
        " created_at) VALUES (?, ?, ?, ?, ?)",
        (shop_id, action_type, json.dumps(payload), preview, now_iso()),
    )
    if commit:
        conn.commit()
    return preview


def confirm_pending(conn: sqlite3.Connection, shop_id: str) -> str:
    ensure_shop(conn, shop_id)
    row = get_pending(conn, shop_id)
    if row is None:
        return _t("nothing_for_yes")
    apply = _APPLIERS.get(row["action_type"])
    if apply is None:
        conn.execute("UPDATE pending_actions SET status = 'superseded' WHERE id = ?", (row["id"],))
        conn.commit()
        return _t("preview_expired")
    action_id = _action_id()
    payload = json.loads(row["payload_json"])
    if row["action_type"] == "stock_intake":
        blockers = _intake_blockers(payload)
        if blockers:
            return _t("intake_still", items="; ".join(blockers))
    message, kind, summary, undo = apply(conn, shop_id, payload, action_id)
    if payload.get("photo_hash"):
        photo_kind = {"expiry_note": "expiry", "book_page": "book_page",
                      "receipt": "receipt",
                      "stock_intake": "stock",
                      "stock_intake_item": "stock"}.get(row["action_type"], "")
        record_photo(conn, shop_id, payload["photo_hash"], photo_kind,
                     commit=False)
    conn.execute(
        "INSERT INTO actions (action_id, shop_id, kind, summary, data_json) VALUES (?, ?, ?, ?, ?)",
        (action_id, shop_id, kind, summary, json.dumps(undo)),
    )
    conn.execute(
        "UPDATE pending_actions SET status = 'confirmed', resolved_at = ? WHERE id = ?",
        (now_iso(), row["id"]),
    )
    conn.commit()
    if row["action_type"] in ("book_page", "record_count"):
        from app.schedule import maybe_day_summary
        message += maybe_day_summary(conn, shop_id)
    if row["action_type"] == "receipt" and undo.get("cost_increases"):
        message += _chain_price_alert(conn, shop_id, undo["cost_increases"],
                                      undo.get("supplier") or "")
    return message


def _chain_price_alert(conn: sqlite3.Connection, shop_id: str, increases: list,
                       supplier: str) -> str:
    """After a receipt with higher costs, propose a price update (Yes applies)."""
    import math as _math
    first = increases[0]
    prod = conn.execute("SELECT price_cents FROM products WHERE id = ?",
                        (first["product_id"],)).fetchone()
    price = prod["price_cents"] if prod else None
    up = first["new"] - first["old"]
    margin = ""
    if price:
        margin = _t("chain_margin",
                    pct=round((price - first["new"]) * 100 / price))
    suggest = (price + up) if price else None
    if suggest is not None:
        suggest = int(_math.ceil(suggest / 50) * 50)
        payload = {"product_id": first["product_id"], "name": first["name"],
                   "price_cents": suggest, "old_price": price,
                   "for_pack": False, "pack_name": ""}
        _propose(conn, shop_id, "set_price", payload,
                 _t("chain_set_price", name=first["name"],
                    old=cents_to_rand(price), new=cents_to_rand(suggest)))
        ask = _t("chain_ask_update", price=cents_to_rand(suggest))
    else:
        ask = _t("chain_ask_set")
    extra = _t("chain_more", n=len(increases) - 1) if len(increases) > 1 else ""
    return _t("price_alert", name=first["name"], up=cents_to_rand(up),
              sup=supplier, margin=margin, ask=ask, extra=extra)


def cancel_pending(conn: sqlite3.Connection, shop_id: str) -> str:
    ensure_shop(conn, shop_id)
    row = get_pending(conn, shop_id)
    if row is None:
        return _t("nothing_waiting")
    conn.execute(
        "UPDATE pending_actions SET status = 'cancelled', resolved_at = ? WHERE id = ?",
        (now_iso(), row["id"]),
    )
    conn.commit()
    return _t("not_saved")


# ---------------------------------------------------------------------------
# Write tools: credit book
# ---------------------------------------------------------------------------


def _resolve_priced_line(conn: sqlite3.Connection, shop_id: str, product: str,
                         qty: float, unit: str | None = None):
    """Resolve one purchasable line (never guess a price).

    Returns (line_or_None, status, detail) with status one of ok, ambiguous,
    unknown, unpriced. Callers pause unknown/unpriced lines for the owner.
    qty is quoted in singles unless unit names a pack; line["qty"] is always
    base units for stock.
    """
    prod, ambiguous = _find_products(conn, shop_id, product)
    if prod is None:
        if ambiguous:
            return None, "ambiguous", _which_one(ambiguous)
        return None, "unknown", product
    units, is_pack, no_pack = _resolve_units(prod["pack_name"], prod["pack_size"], qty, unit)
    single, pack_price = prod["price_cents"], prod["pack_price_cents"]
    if single is None and not (is_pack and pack_price is not None):
        return None, "unpriced", prod["name"]
    if is_pack:
        charged = pack_price if pack_price is not None else int(round(single * prod["pack_size"]))
        total = int(round(charged * qty))
        short = f"{_fmt_qty(qty)}x {prod['name']} {prod['pack_name']}"
        label = _t("label_units", q=_fmt_qty(qty), name=prod['name'],
                   pack=prod['pack_name'], u=_fmt_qty(units),
                   price=cents_to_rand(charged))
    else:
        total = int(round(single * units))
        short = f"{_fmt_qty(units)}x {prod['name']}"
        label = _t("label_single", q=_fmt_qty(units), name=prod['name'],
                   price=cents_to_rand(single))
    return {
        "product_id": prod["id"],
        "name": prod["name"],
        "qty": units,
        "price_cents": single,
        "line_total": total,
        "is_pack": is_pack,
        "short": short,
        "label": label,
        "warn": no_pack,
        "unit_word": _pack_echo(unit) if no_pack else "",
        "pack_name": prod["pack_name"] or "",
        "pack_size": prod["pack_size"],
    }, "ok", ""


def ensure_product_price(conn: sqlite3.Connection, shop_id: str, name: str, price_cents: int) -> None:
    """Insert-or-set a product's sell price (owner-stated, never guessed)."""
    ensure_shop(conn, shop_id)
    prod, _ = _find_products(conn, shop_id, name)
    if prod is None:
        conn.execute(
            "INSERT INTO products (shop_id, name, price_cents) VALUES (?, ?, ?)",
            (shop_id, name.strip(), price_cents),
        )
    else:
        conn.execute("UPDATE products SET price_cents = ? WHERE id = ?",
                     (price_cents, prod["id"]))
    conn.commit()


def ensure_pack_price(conn: sqlite3.Connection, shop_id: str, name: str, pack_price: int) -> None:
    """Set a known pack product's pack price and derive its single price."""
    ensure_shop(conn, shop_id)
    prod, _ = _find_products(conn, shop_id, name)
    if prod is None or not prod["pack_size"]:
        return
    conn.execute("UPDATE products SET pack_price_cents = ?, price_cents = ? WHERE id = ?",
                 (pack_price, int(round(pack_price / prod["pack_size"])), prod["id"]))
    conn.commit()


def find_product(conn: sqlite3.Connection, shop_id: str, name: str):
    """Best-match product row or None (for the agent's price-reply path)."""
    prod, _ = _find_products(conn, shop_id, name)
    return prod


def _store_await_price(conn: sqlite3.Connection, shop_id: str, product: str, spec: dict) -> None:
    conn.execute("DELETE FROM await_price WHERE shop_id = ?", (shop_id,))
    conn.execute("INSERT INTO await_price (shop_id, product, spec_json) VALUES (?, ?, ?)",
                 (shop_id, product, json.dumps(spec)))
    conn.commit()


def get_await_price(conn: sqlite3.Connection, shop_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM await_price WHERE shop_id = ? ORDER BY id DESC LIMIT 1",
                        (shop_id,)).fetchone()


def clear_await_price(conn: sqlite3.Connection, shop_id: str) -> None:
    conn.execute("DELETE FROM await_price WHERE shop_id = ?", (shop_id,))
    conn.commit()


def set_await_reminder(conn: sqlite3.Connection, shop_id: str, due_iso: str,
                       sent_iso: str = "") -> None:
    conn.execute("DELETE FROM await_reminder WHERE shop_id = ?", (shop_id,))
    conn.execute("INSERT INTO await_reminder (shop_id, due_iso, sent_iso)"
                 " VALUES (?, ?, ?)", (shop_id, due_iso, sent_iso or ""))
    conn.commit()


def get_await_reminder(conn: sqlite3.Connection, shop_id: str):
    return conn.execute("SELECT * FROM await_reminder WHERE shop_id = ?",
                        (shop_id,)).fetchone()


def clear_await_reminder(conn: sqlite3.Connection, shop_id: str) -> None:
    conn.execute("DELETE FROM await_reminder WHERE shop_id = ?", (shop_id,))
    conn.commit()


def _apply_add_credit(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    cust, _ = _get_or_create_customer(conn, shop_id, payload["customer"])
    cur = conn.execute(
        "INSERT INTO credit_entries (shop_id, customer_id, amount_cents, items_text,"
        " action_id, recorded_by) VALUES (?, ?, ?, ?, ?, ?)",
        (shop_id, cust["id"], payload["total_cents"], _items_text(payload["lines"]),
         action_id, _rec()),
    )
    bal = balance_cents(conn, shop_id, cust["id"])
    return (
        _t("credit_saved", name=cust['name'], bal=cents_to_rand(bal)),
        "credit",
        f"credit {cust['name']} {cents_to_rand(payload['total_cents'])}",
        {"credit_id": cur.lastrowid},
    )


def add_credit(conn: sqlite3.Connection, shop_id: str, customer: str, items: list[dict]) -> str:
    """Propose a credit-book entry. items: [{product, qty}]."""
    ensure_shop(conn, shop_id)
    if not customer or not customer.strip():
        return _t("credit_whose")
    if not items:
        return _t("credit_what")
    lines = []
    for item in items:
        try:
            qty = float(item.get("qty", 0))
        except (TypeError, ValueError):
            qty = 0
        name = str(item.get("product", "")).strip()
        if not name or qty <= 0:
            return _t("credit_each")
        line, status, detail = _resolve_priced_line(conn, shop_id, name, qty,
                                                         item.get("unit"))
        if status == "ambiguous":
            return detail
        if status in ("unknown", "unpriced"):
            _store_await_price(conn, shop_id, detail,
                               {"tool": "credit", "customer": customer.strip(), "items": items})
            if status == "unknown":
                cap = detail[:1].upper() + detail[1:]
                return _t("credit_unknown", cap=cap, low=detail.lower(),
                                cust=customer.strip())
            if _is_pack_unit(item.get("unit")):
                return _t("pack_price_ask", ask=_ask_price(detail),
                          echo=_pack_echo(item.get("unit")))
            return _ask_price(detail)
        lines.append(line)
    total = sum(ln["line_total"] for ln in lines)
    payload = {"customer": customer.strip(), "lines": lines, "total_cents": total}
    is_new = _get_customer(conn, shop_id, customer) is None
    with _dry_run(conn):
        _apply_add_credit(conn, shop_id, payload, _action_id())
        cust = _get_customer(conn, shop_id, customer)
        new_bal = balance_cents(conn, shop_id, cust["id"])
    preview = _t("credit_preview", cust=customer.strip(),
                       total=cents_to_rand(total), items=_items_text(lines),
                       bal=cents_to_rand(new_bal))
    if not is_new:
        cust = _get_customer(conn, shop_id, customer)
        if cust is not None:
            preview += _limit_warn(conn, shop_id, cust["id"], new_bal)
    if is_new:
        preview += _t("credit_new")
    return _propose(conn, shop_id, "add_credit", payload, preview)


def _apply_record_payment(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    cur = conn.execute(
        "INSERT INTO payments (shop_id, customer_id, amount_cents, action_id, recorded_by)"
        " VALUES (?, ?, ?, ?, ?)",
        (shop_id, payload["customer_id"], payload["amount_cents"], action_id, _rec()),
    )
    _post_cash(conn, shop_id, payload.get("account", "cash"), payload["amount_cents"],
               "credit_payment", action_id, ref_type="payment", ref_id=cur.lastrowid)
    bal = balance_cents(conn, shop_id, payload["customer_id"])
    return (
        _t("pay_saved", name=payload['customer'],
           amt=cents_to_rand(payload['amount_cents']),
           bal=cents_to_rand(bal)),
        "payment",
        f"payment {payload['customer']} {cents_to_rand(payload['amount_cents'])}",
        {"payment_id": cur.lastrowid},
    )


def record_payment(conn: sqlite3.Connection, shop_id: str, customer: str, amount_cents: int,
                   account: str | None = None) -> str:
    ensure_shop(conn, shop_id)
    cust = _get_customer(conn, shop_id, customer or "")
    if cust is None:
        return _t("no_one_named", name=customer)
    try:
        amount = int(amount_cents)
    except (TypeError, ValueError):
        return _t("pay_howmuch")
    if amount <= 0:
        return _t("pay_howmuch")
    acct = _clean_account(account)
    payload = {"customer_id": cust["id"], "customer": cust["name"], "amount_cents": amount,
               "account": acct}
    with _dry_run(conn):
        _apply_record_payment(conn, shop_id, payload, _action_id())
        new_bal = balance_cents(conn, shop_id, cust["id"])
    preview = _t("pay_preview", name=cust['name'],
                       amt=cents_to_rand(amount),
                       bal=cents_to_rand(new_bal))
    if acct != "cash":
        preview += _t("pay_into", acct=_acct_name(acct))
    return _propose(conn, shop_id, "record_payment", payload, preview)


def _apply_record_expense(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    cur = conn.execute(
        "INSERT INTO expenses (shop_id, category, amount_cents, account, note, action_id,"
        " recorded_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (shop_id, payload["category"], payload["amount_cents"], payload["account"],
         payload.get("note") or "", action_id, _rec()),
    )
    _post_cash(conn, shop_id, payload["account"], -payload["amount_cents"], "expense",
               action_id, category=payload["category"], ref_type="expense",
               ref_id=cur.lastrowid)
    left = account_balance(conn, shop_id, payload["account"])
    what = _cat_name(payload["category"])
    acct = _acct_name(payload["account"])
    return (
        _t("exp_saved", what=what, amt=cents_to_rand(payload['amount_cents']),
           acct=acct, left=cents_to_rand(left)),
        "expense", f"expense {what} {cents_to_rand(payload['amount_cents'])}",
        {"expense_id": cur.lastrowid},
    )


def record_expense(conn: sqlite3.Connection, shop_id: str, amount_cents: int,
                   category: str | None = None, account: str | None = None,
                   note: str | None = None) -> str:
    ensure_shop(conn, shop_id)
    try:
        amount = int(amount_cents)
    except (TypeError, ValueError):
        return _t("how_much")
    if amount <= 0:
        return _t("how_much")
    cat, acct = _clean_category(category), _clean_account(account)
    payload = {"amount_cents": amount, "category": cat, "account": acct,
               "note": (note or "").strip()}
    with _dry_run(conn):
        _apply_record_expense(conn, shop_id, payload, _action_id())
        left = account_balance(conn, shop_id, acct)
    what = _cat_name(cat)
    return _propose(
        conn, shop_id, "record_expense", payload,
        _t("exp_preview", what=what, amt=cents_to_rand(amount),
           acct=_acct_name(acct), before=cents_to_rand(left + amount),
           left=cents_to_rand(left)),
    )


def _apply_record_drawing(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    _post_cash(conn, shop_id, payload["account"], -payload["amount_cents"], "drawing",
               action_id, note=payload.get("note") or "")
    left = account_balance(conn, shop_id, payload["account"])
    acct = _acct_name(payload["account"])
    return (
        _t("draw_saved", amt=cents_to_rand(payload['amount_cents']),
           acct=acct, left=cents_to_rand(left)),
        "drawing", f"drawing {cents_to_rand(payload['amount_cents'])}", {},
    )


def record_drawing(conn: sqlite3.Connection, shop_id: str, amount_cents: int,
                   account: str | None = None, note: str | None = None) -> str:
    ensure_shop(conn, shop_id)
    try:
        amount = int(amount_cents)
    except (TypeError, ValueError):
        return _t("how_much")
    if amount <= 0:
        return _t("how_much")
    acct = _clean_account(account)
    payload = {"amount_cents": amount, "account": acct, "note": (note or "").strip()}
    with _dry_run(conn):
        _apply_record_drawing(conn, shop_id, payload, _action_id())
        left = account_balance(conn, shop_id, acct)
    return _propose(
        conn, shop_id, "record_drawing", payload,
        _t("draw_preview", amt=cents_to_rand(amount), acct=_acct_name(acct),
           before=cents_to_rand(left + amount), left=cents_to_rand(left)),
    )


def _count_line(counted: int, expected: int) -> str:
    if counted == expected:
        return _t("count_match")
    diff = counted - expected
    word = _t("over_word") if diff > 0 else _t("short_word")
    return _t("count_diff", exp=cents_to_rand(expected),
              diff=cents_to_rand(abs(diff)), word=word)


def _apply_record_count(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    diff = payload["counted_cents"] - payload["expected_cents"]
    if diff:
        _post_cash(conn, shop_id, "cash", diff, "count", action_id,
                   note="over" if diff > 0 else "short")
    return (
        _t("count_saved", n=cents_to_rand(payload['counted_cents']),
           line=_count_line(payload["counted_cents"],
                            payload["expected_cents"])),
        "count", f"count {cents_to_rand(payload['counted_cents'])}", {},
    )


def record_count(conn: sqlite3.Connection, shop_id: str, amount_cents: int) -> str:
    """Record a physical cash count ("counted R1,240") and show over/short."""
    ensure_shop(conn, shop_id)
    try:
        counted = int(amount_cents)
    except (TypeError, ValueError):
        return _t("count_howmuch")
    if counted < 0:
        return _t("count_howmuch")
    expected = account_balance(conn, shop_id, "cash")
    payload = {"counted_cents": counted, "expected_cents": expected}
    return _propose(
        conn, shop_id, "record_count", payload,
        _t("count_preview", n=cents_to_rand(counted),
           line=_count_line(counted, expected)),
    )


def _apply_supplier_payment(conn: sqlite3.Connection, shop_id: str, payload: dict,
                            action_id: str):
    _post_cash(conn, shop_id, payload["account"], -payload["amount_cents"], "purchase",
               action_id, note=f"paid {payload['supplier']}")
    _post_supplier(conn, shop_id, payload["supplier"], -payload["amount_cents"],
                   action_id, note="payment")
    owed = supplier_balance(conn, shop_id, payload["supplier"])
    acct = _acct_name(payload["account"])
    return (
        _t("sup_saved", sup=payload['supplier'],
           amt=cents_to_rand(payload['amount_cents']), acct=acct,
           owed=cents_to_rand(owed)),
        "supplier_payment", f"supplier {payload['supplier']}", {},
    )


def record_supplier_payment(conn: sqlite3.Connection, shop_id: str, supplier: str,
                            amount_cents: int, account: str | None = None) -> str:
    ensure_shop(conn, shop_id)
    name = (supplier or "").strip()
    if not name:
        return _t("sup_which")
    try:
        amount = int(amount_cents)
    except (TypeError, ValueError):
        return _t("how_much")
    if amount <= 0:
        return _t("how_much")
    acct = _clean_account(account)
    owed = supplier_balance(conn, shop_id, name)
    payload = {"supplier": name, "amount_cents": amount, "account": acct}
    return _propose(
        conn, shop_id, "record_supplier_payment", payload,
        _t("sup_preview", sup=name, amt=cents_to_rand(amount),
           acct=_acct_name(acct), owed=cents_to_rand(owed),
           after=cents_to_rand(owed - amount)),
    )


# ---------------------------------------------------------------------------
# Write tools: sales and stock
# ---------------------------------------------------------------------------


def _apply_record_sale(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    customer_id = None
    credit_id = None
    if payload.get("buyer"):
        cust, _ = _get_or_create_customer(conn, shop_id, payload["buyer"])
        customer_id = cust["id"]
        cur = conn.execute(
            "INSERT INTO credit_entries (shop_id, customer_id, amount_cents, items_text,"
            " action_id, recorded_by) VALUES (?, ?, ?, ?, ?, ?)",
            (shop_id, customer_id, payload["total_cents"],
             f"{payload.get('short') or _fmt_qty(payload['qty']) + 'x ' + payload['product']} (sale)",
             action_id, _rec()),
        )
        credit_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO sales (shop_id, product_id, product_name, qty, total_cents, customer_id,"
        " action_id, recorded_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (shop_id, payload["product_id"], payload["product"], payload["qty"],
         payload["total_cents"], customer_id, action_id, _rec()),
    )
    sale_id = cur.lastrowid
    conn.execute("UPDATE products SET stock_qty = stock_qty - ? WHERE id = ?",
                 (payload["qty"], payload["product_id"]))
    conn.execute(
        "INSERT INTO stock_moves (shop_id, product_id, delta_qty, reason, action_id)"
        " VALUES (?, ?, ?, 'sale', ?)",
        (shop_id, payload["product_id"], -payload["qty"], action_id),
    )
    stock = conn.execute("SELECT stock_qty FROM products WHERE id = ?",
                         (payload["product_id"],)).fetchone()["stock_qty"]
    if not payload.get("buyer"):
        _post_cash(conn, shop_id, "cash", payload["total_cents"], "sale", action_id,
                   ref_type="sale", ref_id=sale_id)
    label = payload.get("label") or f"{_fmt_qty(payload['qty'])} × {payload['product']}"
    first = _t("sale_line", label=label,
               total=cents_to_rand(payload['total_cents']))
    prow = conn.execute("SELECT pack_name, pack_size FROM products WHERE id = ?",
                        (payload["product_id"],)).fetchone()
    left = _t("sale_left", hand=_fmt_on_hand(stock, prow['pack_name'],
                                             prow['pack_size']))
    if payload.get("buyer"):
        bal = balance_cents(conn, shop_id, customer_id)
        return (
            _t("sale_saved_credit", first=first, buyer=payload['buyer'],
               amt=cents_to_rand(payload['total_cents']),
               bal=cents_to_rand(bal), left=left),
            "sale_credit",
            f"sale {payload['product']} to {payload['buyer']}",
            {"sale_id": sale_id, "credit_id": credit_id,
             "product_id": payload["product_id"], "qty": payload["qty"]},
        )
    return (
        _t("sale_saved_cash", first=first, left=left),
        "sale_cash",
        f"sale {payload['product']} cash",
        {"sale_id": sale_id, "product_id": payload["product_id"], "qty": payload["qty"]},
    )


def record_sale(conn: sqlite3.Connection, shop_id: str, product: str, qty: float = 1,
                buyer: str | None = None, unit: str | None = None) -> str:
    """Propose a sale. buyer=None means cash; a name means credit sale.

    qty is single units unless unit names a pack ("box", "tray", "ibhokisi").
    """
    ensure_shop(conn, shop_id)
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        return _t("how_many")
    if qty <= 0:
        return _t("how_many")
    buyer_name = buyer.strip() if buyer and buyer.strip() else None
    line, status, detail = _resolve_priced_line(conn, shop_id, product or "", qty, unit)
    if status == "ambiguous":
        return detail
    if status in ("unknown", "unpriced"):
        _store_await_price(conn, shop_id, detail,
                           {"tool": "sale", "qty": qty, "buyer": buyer_name, "unit": unit})
        if status == "unknown":
            cap = detail[:1].upper() + detail[1:]
            if buyer_name:
                tail = _t("sale_tail_credit", buyer=buyer_name)
            else:
                tail = _t("sale_tail_cash")
            return _t("sale_unknown", cap=cap, low=detail.lower(), tail=tail)
        if _is_pack_unit(unit):
            return _t("pack_price_ask", ask=_ask_price(detail),
                      echo=_pack_echo(unit))
        return _ask_price(detail)
    payload = {"product_id": line["product_id"], "product": line["name"], "qty": line["qty"],
               "total_cents": line["line_total"], "buyer": buyer_name,
               "label": line["label"], "short": line["short"]}
    buyer_new = bool(buyer_name) and _get_customer(conn, shop_id, buyer_name) is None
    with _dry_run(conn):
        _apply_record_sale(conn, shop_id, payload, _action_id())
        stock = conn.execute("SELECT stock_qty FROM products WHERE id = ?",
                             (line["product_id"],)).fetchone()["stock_qty"]
        buyer_bal = None
        if buyer_name:
            cust = _get_customer(conn, shop_id, buyer_name)
            buyer_bal = balance_cents(conn, shop_id, cust["id"])
    first = _t("sale_line", label=line['label'],
                   total=cents_to_rand(line['line_total']))
    left = _t("sale_left", hand=_fmt_on_hand(stock, line['pack_name'],
                                             line['pack_size']))
    if buyer_name:
        preview = _t("sale_preview_credit", first=first, buyer=buyer_name,
                     bal=cents_to_rand(buyer_bal), left=left)
        if buyer_new:
            preview += _t("sale_new_cust")
        else:
            cust = _get_customer(conn, shop_id, buyer_name)
            if cust is not None:
                preview += _limit_warn(conn, shop_id, cust["id"], buyer_bal)
    else:
        preview = _t("sale_preview_cash", first=first, left=left)
    if line["warn"]:
        preview += _t("warn_singles", echo=line['unit_word'],
                      name=line['name'])
    if stock < 0:
        preview += _t("warn_short")
    return _propose(conn, shop_id, "record_sale", payload, preview)


def _coerce_pos_int(val) -> int | None:
    try:
        val = int(val)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def _fmt_price_extras(cost: float | None, price: int | None,
                      pack: int | None = None, pack_name: str = "pack") -> str:
    bits = []
    if cost is not None:
        bits.append(_t("extra_cost", c=cents_to_rand(int(round(cost)))))
    if price is not None:
        bits.append(_t("extra_sell", p=cents_to_rand(price)))
    if pack is not None:
        bits.append(_t("extra_pack", pack=pack_name,
                       c=cents_to_rand(pack)))
    return f" ({', '.join(bits)})" if bits else ""


def _plural_pack(pack_name: str) -> str:
    return {"box": "boxes"}.get(pack_name, pack_name + "s")


def _fmt_on_hand(units: float, pack_name: str | None, pack_size: float | None) -> str:
    """Stock level as packs plus loose units ("1 box + 37 loose (137)")."""
    if not pack_size:
        return _fmt_qty(units)
    if units <= 0:
        return "0"
    packs = int(units // pack_size)
    loose = round(units - packs * pack_size, 2)
    total = _fmt_qty(units)
    if packs and loose:
        word = pack_name if packs == 1 else _plural_pack(pack_name or "pack")
        return _t("onhand_packs", p=packs, word=word, l=_fmt_qty(loose),
                  total=total)
    if packs:
        word = pack_name if packs == 1 else _plural_pack(pack_name or "pack")
        return _t("onhand_packs_noloose", p=packs, word=word, total=total)
    return total


def _fmt_low_line(name: str, units: float, pack_name: str | None,
                  pack_size: float | None) -> str:
    if units <= 0:
        return _t("low_out", name=name)
    return _t("low_left", name=name,
              hand=_fmt_on_hand(units, pack_name, pack_size))


def _is_pack_unit(unit: str | None) -> bool:
    toks = {(t.strip(" .!?,")) for t in (unit or "").lower().split()}
    return bool(toks & _PACK_WORDS) or any(t.endswith("-pack") for t in toks)


def _apply_add_stock(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    cost, price = payload.get("cost_cents"), payload.get("price_cents")
    pack = payload.get("pack_price_cents")
    if payload["product_id"] is None:
        cur = conn.execute(
            "INSERT INTO products (shop_id, name, wholesaler, cost_cents, price_cents)"
            " VALUES (?, ?, ?, ?, ?)",
            (shop_id, payload["name"], payload.get("wholesaler") or "", cost, price),
        )
        product_id, created, old_wholesaler = cur.lastrowid, True, ""
        old_cost = old_price = old_pack = None
    else:
        product_id, created = payload["product_id"], False
        row = conn.execute("SELECT wholesaler, cost_cents, price_cents, pack_price_cents,"
                           " pack_name FROM products WHERE id = ?", (product_id,)).fetchone()
        old_wholesaler = row["wholesaler"]
        old_cost, old_price, old_pack = row["cost_cents"], row["price_cents"], row["pack_price_cents"]
        if payload.get("wholesaler") and not old_wholesaler:
            conn.execute("UPDATE products SET wholesaler = ? WHERE id = ?",
                         (payload["wholesaler"], product_id))
        if cost is not None:
            conn.execute("UPDATE products SET cost_cents = ? WHERE id = ?", (cost, product_id))
        if price is not None:
            conn.execute("UPDATE products SET price_cents = ? WHERE id = ?", (price, product_id))
        if pack is not None:
            conn.execute("UPDATE products SET pack_price_cents = ? WHERE id = ?",
                         (pack, product_id))
    conn.execute("UPDATE products SET stock_qty = stock_qty + ? WHERE id = ?",
                 (payload["qty"], product_id))
    conn.execute(
        "INSERT INTO stock_moves (shop_id, product_id, delta_qty, reason, action_id)"
        " VALUES (?, ?, ?, 'restock', ?)",
        (shop_id, product_id, payload["qty"], action_id),
    )
    stock = conn.execute("SELECT stock_qty FROM products WHERE id = ?",
                         (product_id,)).fetchone()["stock_qty"]
    if cost is not None:
        total_cost = int(round(cost * payload["qty"]))
        acct = payload.get("account", "cash")
        if acct == "supplier":
            _post_supplier(conn, shop_id, payload.get("wholesaler") or "Supplier",
                           total_cost, action_id, note=f"stock {payload['name']}")
        else:
            _post_cash(conn, shop_id, acct, -total_cost, "purchase", action_id,
                       note=f"stock {payload['name']}")
    extras = _fmt_price_extras(cost, price, pack, payload.get("pack_name") or "pack")
    prow = conn.execute("SELECT pack_name, pack_size FROM products WHERE id = ?",
                        (product_id,)).fetchone()
    hand = _fmt_on_hand(stock, prow["pack_name"], prow["pack_size"])
    if created:
        if extras:
            msg = _t("stock_saved_new", name=payload['name'], hand=hand,
                     extras=extras)
        else:
            msg = (_t("stock_saved_new", name=payload['name'], hand=hand,
                      extras="") + _t("stock_no_price"))
        return (msg, "stock", f"stock new {payload['name']}",
                {"product_id": product_id, "qty": payload["qty"], "created": True})
    return (
        _t("stock_saved", name=payload['name'], hand=hand, extras=extras),
        "stock", f"stock {payload['name']} +{_fmt_qty(payload['qty'])}",
        {"product_id": product_id, "qty": payload["qty"], "created": False,
         "old_wholesaler": old_wholesaler, "old_cost": old_cost, "old_price": old_price,
         "old_pack": old_pack},
    )


_MULTIPACK_RE = re.compile(
    r"multi[\s-]?pack|\b\d+\s*[x×]\s*\d+|\bx\s*\d+\b|\d+\s*-\s*pack",
    re.IGNORECASE)


def _intake_label(prod: dict) -> str:
    return " ".join(str(prod.get(k) or "") for k in
                    ("brand", "variant", "size")).strip()


def _intake_match(conn, shop_id, name):
    """Careful inventory match: unique row, ambiguous names, or new."""
    prod, ambiguous = _find_products(conn, shop_id, name)
    if prod is not None:
        return prod["id"], [], False
    return None, [p["name"] for p in ambiguous[:3]], not ambiguous


def _intake_line(conn, shop_id, name, entry, seen, supplier):
    qty = None
    source = None
    try:
        q = float((entry or {}).get("qty", 0))
    except (TypeError, ValueError):
        q = 0
    if q > 0:
        qty, source = q, "stated"
    elif isinstance(seen, dict) and (seen.get("visible_count") or 0) > 0:
        qty, source = float(seen["visible_count"]), "seen"
    unit = str((entry or {}).get("unit") or "").strip() or None
    size_txt = " ".join(str((seen or {}).get(k) or "") for k in
                        ("brand", "variant", "size"))
    multi = bool(_MULTIPACK_RE.search(size_txt)) or _is_pack_unit(unit)
    pid, ambiguous, new = _intake_match(
        conn, shop_id, name) if name and name != "?" else (None, [], False)
    pack_size = None
    if pid is not None:
        row = conn.execute("SELECT pack_size, price_cents FROM products"
                           " WHERE id = ?", (pid,)).fetchone()
        pack_size = row["pack_size"] if row else None
        sell = row["price_cents"] if row else None
        sell_source = "shoplist" if sell else None
    else:
        sell, sell_source = None, None
    if _coerce_pos_int((entry or {}).get("sell_cents")):
        sell = _coerce_pos_int(entry["sell_cents"])
        sell_source = "stated"
    cost = _coerce_pos_int((entry or {}).get("cost_cents"))
    pack_q = bool(multi and (pid is None or not pack_size))
    if pack_q:
        packs_seen = qty
        qty, source = None, None
    else:
        packs_seen = None
    if pid is not None and pack_size and unit is None and multi:
        unit = conn.execute("SELECT pack_name FROM products WHERE id = ?",
                            (pid,)).fetchone()["pack_name"]
    return {"name": name, "qty": qty, "qty_source": source, "unit": unit,
            "cost_cents": cost, "cost_source": "stated" if cost else None,
            "sell_cents": sell, "sell_source": sell_source,
            "product_id": pid, "ambiguous": ambiguous, "new": new,
            "pack_q": pack_q, "packs_seen": packs_seen,
            "note": str((seen or {}).get("notes") or "")[:80]}


def _intake_needs(payload):
    """Everything still missing (asked). Confirm blocks on qty/product."""
    needs = []
    for i, ln in enumerate(payload.get("lines", []), 1):
        tag = f"line {i}"
        if not ln.get("name") or ln["name"] == "?":
            needs.append(f"{tag} name")
        elif ln.get("ambiguous"):
            needs.append(f"{tag} which")
        if ln.get("pack_q"):
            needs.append(f"{tag} singles-per-pack")
        elif ln.get("qty") is None:
            needs.append(f"{tag} qty")
        if ln.get("sell_cents") is None:
            needs.append(f"{tag} selling price")
        if ln.get("cost_cents") is None:
            needs.append(f"{tag} cost")
    return needs


def _intake_blockers(payload):
    """Missing product identity or quantity: Yes waits for these."""
    out = []
    for i, ln in enumerate(payload.get("lines", []), 1):
        tag = f"line {i}"
        if not ln.get("name") or ln["name"] == "?":
            out.append(f"{tag} name")
        elif ln.get("ambiguous"):
            out.append(f"{tag} which ({' / '.join(ln['ambiguous'])})")
        if ln.get("pack_q"):
            out.append(f"{tag} singles-per-pack")
        elif ln.get("qty") is None:
            out.append(f"{tag} qty")
    return out


def _render_intake(conn, shop_id, payload):
    from app.db import cents_to_rand
    parts = [_t("intake_head",
                photo=_t("intake_from_photo") if payload.get("photo_hash")
                else "")]
    for i, ln in enumerate(payload.get("lines", []), 1):
        if ln.get("qty") is None:
            qtybit = _t("intake_qty_q")
        else:
            qtybit = f"×{_fmt_qty(ln['qty'])}"
            if ln.get("qty_source") == "seen":
                qtybit = _t("intake_seen", n=_fmt_qty(ln["qty"]))
            if ln.get("unit"):
                qtybit += f" {ln['unit']}"
        if ln.get("sell_cents") is not None:
            key = "intake_sell_said" if ln.get("sell_source") == "stated" \
                else "intake_sell_list"
            sellbit = _t(key, amt=cents_to_rand(ln["sell_cents"]))
        else:
            sellbit = _t("intake_sell_q")
        costbit = _t("intake_cost", amt=cents_to_rand(ln["cost_cents"])) \
            if ln.get("cost_cents") is not None else _t("intake_cost_q")
        tags = ""
        if ln.get("new"):
            tags += " " + _t("intake_new")
        if ln.get("ambiguous"):
            tags += " ❓" + _t("intake_which",
                              names=" / ".join(ln["ambiguous"]))
        if ln.get("pack_q"):
            tags += " ❓" + _t("intake_packq")
        name = ln.get("name") or _t("intake_noname")
        parts.append(f"• {i}. {name} — {qtybit} · {sellbit} · {costbit}"
                     + tags)
    needs = _intake_needs(payload)
    if needs:
        parts.append(_t("intake_ask", needs="; ".join(needs)))
    else:
        parts.append(_t("intake_ready"))
    return "\n".join(parts)


def propose_stock_intake(conn: sqlite3.Connection, shop_id: str,
                         products: list, items: list,
                         supplier: str | None = None,
                         photo_hash: str | None = None,
                         prior: dict | None = None) -> str:
    """One editable intake preview for photo (or multi-item) stock adds.

    Vision products and planner items merge into lines with explicit
    sources; nothing is invented. Unresolved lines block Yes until the
    owner answers. No payment is recorded: cost stays None (no posting)
    until the owner states it and confirms. A prior pending payload
    keeps its stated answers when the planner re-enters mid-intake.
    """
    ensure_shop(conn, shop_id)
    products = [p for p in products or [] if isinstance(p, dict)]
    planned = [e for e in items or []
               if isinstance(e, dict) and str(e.get("product") or "").strip()]
    old_lines = (prior or {}).get("lines") or []
    old_by_name = {}
    for ln in old_lines:
        if isinstance(ln, dict) and ln.get("name"):
            old_by_name.setdefault(ln["name"].lower(), ln)
    used: set[int] = set()
    lines = []
    singles = len(planned) == 1 and len(products) == 1
    for e in planned:
        name = " ".join(str(e.get("product")).split())
        seen = None
        words = set(name.lower().split())
        for i, p in enumerate(products):
            if i in used:
                continue
            label = _intake_label(p).lower()
            if words & set(label.split()):
                seen, used = p, used | {i}
                break
        if seen is None and singles:
            # one named item, one seen product: same referent
            seen, used = products[0], {0}
        line = _intake_line(conn, shop_id, name, e, seen, supplier)
        old = old_by_name.pop(name.lower(), None)
        if old is not None:
            _intake_carry(line, old)
        lines.append(line)
    for i, p in enumerate(products):
        if i in used:
            continue
        label = _intake_label(p)
        lines.append(_intake_line(conn, shop_id, label or "?", {}, p,
                                  supplier))
    for ln in old_by_name.values():
        lines.append(ln)  # earlier lines survive planner re-entry
    if not lines:
        return _t("intake_noproducts")
    payload = {"lines": lines, "supplier": (supplier or "").strip()
               or (prior or {}).get("supplier", ""),
               "photo_hash": photo_hash or (prior or {}).get("photo_hash",
                                                             "")}
    return _propose(conn, shop_id, "stock_intake", payload,
                    _render_intake(conn, shop_id, payload))


def _intake_carry(line, old):
    """Keep stated answers (and seen evidence) across re-proposals.

    Precedence per field: newly stated > previously stated > newly
    seen/derived > previously seen. A resolved pack answer is never
    re-derived into a question.
    """
    if line.get("qty_source") != "stated" and old.get("qty") is not None:
        if old.get("qty_source") == "stated" or line.get("qty") is None:
            line["qty"] = old["qty"]
            line["qty_source"] = old.get("qty_source")
            line["unit"] = old.get("unit")
    for amount, source in (("cost_cents", "cost_source"),
                           ("sell_cents", "sell_source")):
        if old.get(amount) is not None and (
                old.get(source) == "stated"
                or line.get(amount) is None):
            line[amount] = old[amount]
            line[source] = old.get(source)
    if old.get("product_id") is not None:
        line["product_id"] = old["product_id"]
        line["ambiguous"] = []
        line["new"] = False
    if not old.get("pack_q") and old.get("qty") is not None:
        line["pack_q"] = False
        line["packs_seen"] = None
        if line.get("qty") is None:
            line["qty"] = old["qty"]
            line["qty_source"] = old.get("qty_source")
            line["unit"] = old.get("unit")


def _apply_stock_intake(conn, shop_id, payload, action_id):
    msgs = []
    undone = []
    for ln in payload.get("lines", []):
        if ln.get("qty") is None or (
                ln.get("product_id") is None and not ln.get("new")):
            continue  # gated at confirm; never half-saved
        units, _, _ = _resolve_units(
            _pack_of(conn, ln.get("product_id")),
            _pack_size_of(conn, ln), ln["qty"], ln.get("unit"))
        # shoplist prices are display-only; only a stated price is written
        sell = ln.get("sell_cents") \
            if ln.get("sell_source") == "stated" else None
        msg, _k, _s, undo = _apply_add_stock(conn, shop_id, {
            "product_id": ln.get("product_id"), "name": ln["name"],
            "qty": units, "wholesaler": payload.get("supplier") or "",
            "cost_cents": ln.get("cost_cents"), "price_cents": sell,
            "pack_price_cents": None, "pack_name": "",
            "account": "cash"}, action_id)
        undo["name"] = ln["name"]
        msgs.append(msg)
        undone.append(undo)
    return ("\n".join(msgs), "stock_intake",
            f"stock intake ({len(undone)} lines)", {"lines": undone})


def _pack_of(conn, pid):
    if pid is None:
        return None
    row = conn.execute("SELECT pack_name FROM products WHERE id = ?",
                       (pid,)).fetchone()
    return row["pack_name"] if row else None


def _pack_size_of(conn, ln=None, pid=None):
    pid = pid if pid is not None else (ln or {}).get("product_id")
    if pid is None:
        return None
    row = conn.execute("SELECT pack_size FROM products WHERE id = ?",
                       (pid,)).fetchone()
    return row["pack_size"] if row else None


def _undo_stock_intake(conn, shop_id, undo, action_id):
    names = []
    for one in undo.get("lines", []):
        pid = one["product_id"]
        name = conn.execute("SELECT name FROM products WHERE id = ?",
                            (pid,)).fetchone()["name"]
        _restore_stock(conn, shop_id, pid, -one["qty"], action_id)
        if not one.get("created"):
            sets, params = ["wholesaler = ?"], [one.get("old_wholesaler", "")]
            for col, key in (("cost_cents", "old_cost"),
                             ("price_cents", "old_price"),
                             ("pack_price_cents", "old_pack")):
                if key in one:
                    sets.append(f"{col} = ?")
                    params.append(one[key])
            conn.execute(f"UPDATE products SET {', '.join(sets)} WHERE id = ?",
                         (*params, pid))
        else:
            conn.execute("DELETE FROM stock_moves WHERE product_id = ?",
                         (pid,))
            conn.execute("DELETE FROM products WHERE id = ?", (pid,))
        names.append(name)
    _reverse_moves(conn, shop_id, action_id)
    return _t("intake_undone", names=", ".join(names))


# ---------------------------------------------------------------------------
# Guided intake sessions: one active photo-intake conversation per shop.
# ---------------------------------------------------------------------------

_INTAKE_SESSION_TTL = timedelta(hours=24)

_INTAKE_STOPWORDS = frozenset("""
    a an the and or of to in on for with is are be as at from by it its
    this that these those i me my we us our you your he she they them their
    please kindly add added stock intake shop shops here there just now all
    if so do does don't dont not no yes get got have has had will would
    can could should our
    """.split())

# Placeholder words a planner may invent ("Photo item 4"); never grounding.
_INTAKE_PLACEHOLDER_WORDS = frozenset(
    {"photo", "photos", "item", "items", "product", "products", "unknown"})

_WORD_RE = re.compile(r"[a-z0-9]+")


def _content_words(text: str) -> set[str]:
    words = set(_WORD_RE.findall((text or "").lower()))
    return {w for w in words
            if len(w) > 1 and not w.isdigit() and w not in _INTAKE_STOPWORDS}


def ground_intake_items(items: list, products: list, text: str) -> list:
    """Drop planned items grounded in neither vision labels nor caption words.

    A planned item survives only when one of its words overlaps a word from
    the vision product labels or a caption content word. Never raises:
    on any unexpected input the items pass through unchanged.
    """
    try:
        labels: set[str] = set()
        for prod in products or []:
            if not isinstance(prod, dict):
                continue
            for key in ("brand", "variant", "size", "label", "name", "notes"):
                labels.update(_content_words(str(prod.get(key) or "")))
        caption_words = _content_words(text)
        kept = []
        for entry in items or []:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            name = entry.get("product") or entry.get("name") or ""
            raw = _content_words(str(name))
            if not raw:
                kept.append(entry)  # nothing to judge by; fail open
                continue
            words = raw - _INTAKE_PLACEHOLDER_WORDS
            if words & labels or words & caption_words:
                kept.append(entry)
        return kept
    except Exception:
        return items if isinstance(items, list) else []


def _dump_intake_json(value, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _load_intake_json(raw: str | None, default):
    try:
        return json.loads(raw or "") if raw else default
    except (ValueError, TypeError):
        return default


def save_intake_session(conn: sqlite3.Connection, shop_id: str,
                        session: dict, *, commit: bool = True) -> None:
    """Upsert the active guided-intake session for a shop (24h expiry)."""
    ensure_shop(conn, shop_id)
    now = now_iso()
    expires = (datetime.now(timezone.utc) + _INTAKE_SESSION_TTL).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    conn.execute(
        """INSERT INTO intake_sessions
             (shop_id, photo_digest, caption, vision_json, items_json,
              current, field, status, stash_json, updated_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(shop_id) DO UPDATE SET
             photo_digest = excluded.photo_digest, caption = excluded.caption,
             vision_json = excluded.vision_json, items_json = excluded.items_json,
             current = excluded.current, field = excluded.field,
             status = excluded.status, stash_json = excluded.stash_json,
             updated_at = excluded.updated_at, expires_at = excluded.expires_at""",
        (shop_id,
         session.get("photo_digest") or "",
         session.get("caption") or "",
         _dump_intake_json(session.get("vision_json"), "[]"),
         _dump_intake_json(session.get("items_json"), "[]"),
         session.get("current") or 0,
         session.get("field"),
         session.get("status") or "active",
         _dump_intake_json(session.get("stash"), "{}"),
         now, expires),
    )
    if commit:
        conn.commit()


def get_intake_session(conn: sqlite3.Connection, shop_id: str) -> dict | None:
    """Return the active session dict, or None when absent/expired/done."""
    row = conn.execute("SELECT * FROM intake_sessions WHERE shop_id = ?",
                       (shop_id,)).fetchone()
    if row is None:
        return None
    if (row["status"] or "") == "done":
        return None
    if row["expires_at"] and row["expires_at"] <= now_iso():
        conn.execute("DELETE FROM intake_sessions WHERE shop_id = ?",
                     (shop_id,))
        conn.commit()
        return None
    return {
        "photo_digest": row["photo_digest"] or "",
        "caption": row["caption"] or "",
        "vision_json": _load_intake_json(row["vision_json"], []),
        "items_json": _load_intake_json(row["items_json"], []),
        "current": row["current"] or 0,
        "field": row["field"],
        "status": row["status"] or "active",
        "stash": _load_intake_json(row["stash_json"], {}),
    }


def clear_intake_session(conn: sqlite3.Connection, shop_id: str,
                         *, commit: bool = True) -> None:
    conn.execute("DELETE FROM intake_sessions WHERE shop_id = ?",
                 (shop_id,))
    if commit:
        conn.commit()


# ---------------------------------------------------------------------------
# Guided new-product flow: one question per message, per-item preview.
# ---------------------------------------------------------------------------

# Number words 1-100 for guided answers (_NUMBER_WORDS only covers 1-10).
_GUIDED_NUMBER_WORDS = {w: int(v) for w, v in _NUMBER_WORDS.items()}
_GUIDED_NUMBER_WORDS.update({
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90, "hundred": 100,
})

_GUIDED_MONEY_RES = (
    re.compile(r"[Rr]\s*(\d+(?:\.\d{1,2})?)"),
    re.compile(r"(\d+\.\d{1,2})"),
    re.compile(r"(\d+(?:\.\d{1,2})?)\s*rand\b", re.IGNORECASE),
    re.compile(r"(\d+(?:\.\d{1,2})?)"),
)
_GUIDED_COST_RE = re.compile(r"\bcosts?\b", re.IGNORECASE)
_GUIDED_SELL_RE = re.compile(r"\bsells?\b|\bselling\b|\bprice\b",
                             re.IGNORECASE)
_GUIDED_MONEY_WORDS = {"rand", "rands", "cost", "costs", "sell", "sells",
                       "selling", "price", "prices"}
_GUIDED_SINGLES_WORDS = {"single", "singles", "loose", "each",
                         "individual", "individually", "packet", "packets"}
_GUIDED_WHOLE_WORDS = {"whole", "pack", "packs", "entire", "box", "boxes"}


def _guided_word_number(text: str) -> int | None:
    """First number-word value (1-100) in text; tens compounds included."""
    toks = re.findall(r"[a-z]+", (text or "").lower())
    for i, tok in enumerate(toks):
        val = _GUIDED_NUMBER_WORDS.get(tok)
        if val is None:
            continue
        if tok == "one" and toks[i + 1:i + 2] == ["hundred"]:
            return 100
        if val < 100 and val % 10 == 0 and i + 1 < len(toks):
            low = _GUIDED_NUMBER_WORDS.get(toks[i + 1])
            if low is not None and 1 <= low <= 9:
                return val + low
        return val
    return None


def _guided_amount_cents(text: str) -> int | None:
    """Money in text as cents: R-prefix, decimal, 'N rand', bare, words."""
    msg = text or ""
    for rx in _GUIDED_MONEY_RES:
        m = rx.search(msg)
        if m:
            return int(round(float(m.group(1)) * 100))
    num = _guided_word_number(msg)
    return num * 100 if num is not None else None


def _guided_qty(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)", text or "")
    if m:
        qty = float(m.group(1))
        return qty if qty > 0 else None
    num = _guided_word_number(text)
    return float(num) if num else None


def _guided_is_skip(text: str) -> bool:
    norm = re.sub(r"\s+", " ", (text or "").strip().lower().strip(" .!?"))
    if "don't know" in norm or "dont know" in norm:
        return True
    return bool(re.search(r"\bskip\b", norm))


def _guided_attempted(text: str) -> bool:
    """True when the text looks like an intake answer (not new business)."""
    msg = text or ""
    if re.search(r"\d", msg):
        return True
    toks = set(re.findall(r"[a-z']+", msg.lower()))
    return bool(toks & _GUIDED_MONEY_WORDS
                or toks & set(_GUIDED_NUMBER_WORDS)
                or toks & _SINGLE_WORDS or toks & _PACK_WORDS)


def _guided_price_target(text: str, field: str) -> str:
    """Which price field an answer names; defaults to the open field."""
    has_cost = bool(_GUIDED_COST_RE.search(text or ""))
    has_sell = bool(_GUIDED_SELL_RE.search(text or ""))
    if has_cost and not has_sell:
        return "cost"
    if has_sell and not has_cost:
        return "sell"
    return field


def _guided_pack_word(text: str) -> str | None:
    for tok in re.findall(r"[a-z-]+", (text or "").lower()):
        if tok in _PACK_WORDS or tok.endswith("-pack"):
            return tok
    return None


def _guided_sell_unit(text: str) -> str | None:
    toks = set(re.findall(r"[a-z]+", (text or "").lower()))
    if "both" in toks:
        return "both"
    if toks & _GUIDED_SINGLES_WORDS:
        return "singles"
    if toks & _GUIDED_WHOLE_WORDS:
        return "pack"
    return None


_GUIDED_PACK_NOUNS = {"pack", "packs", "whole", "entire", "box", "boxes"}
_GUIDED_SINGLE_NOUNS = {"packet", "packets", "single", "singles", "loose",
                        "each", "individual", "individually"}
_GUIDED_TOTAL_WORDS = {"both", "total", "totals", "altogether", "together",
                       "all"}
_GUIDED_TOTAL_RES = (
    re.compile(r"\bfor\s+both\b", re.IGNORECASE),
    re.compile(r"\bfor\s+all\b", re.IGNORECASE),
    re.compile(r"\bfor\s+\d+\s*packs?\b", re.IGNORECASE),
    re.compile(r"\btotals?\b", re.IGNORECASE),
    re.compile(r"\baltogether\b", re.IGNORECASE),
    re.compile(r"\bin\s+total\b", re.IGNORECASE),
)
_GUIDED_FOR_N_PACKS_RE = re.compile(r"\bfor\s+(\d+)\s*packs?\b", re.IGNORECASE)
_GUIDED_N_PACKS_RE = re.compile(r"\b\d+(?:\.\d+)?\s*packs?\b", re.IGNORECASE)
_GUIDED_PER_PACK_RES = (
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:per|/)\s*packs?\b", re.IGNORECASE),
    re.compile(r"(\d+(?:\.\d+)?)\s*in\s+(?:each|every|a|one)\s+packs?\b",
               re.IGNORECASE),
    re.compile(r"packs?\s+of\s+(\d+(?:\.\d+)?)", re.IGNORECASE),
    re.compile(r"(?:each|every|both|all)\s+packs?\s+(?:has|have|holds?|"
               r"contains?)\s*(\d+(?:\.\d+)?)", re.IGNORECASE),
    re.compile(r"\b(\d+(?:\.\d+)?)\s*-\s*packs?\b", re.IGNORECASE),
)


def _guided_cost_total(text: str) -> bool:
    """True when a stated amount covers several packs ("R20 for both packs").

    Never fires on plain amounts ("R32") or non-pack counts ("sell for
    eighteen"): a total needs total words or an explicit pack count.
    """
    msg = text or ""
    if any(rx.search(msg) for rx in _GUIDED_TOTAL_RES):
        return True
    words = "|".join(sorted(_GUIDED_NUMBER_WORDS, key=len, reverse=True))
    return bool(re.search(rf"\bfor\s+(?:{words})\s+packs?\b", msg,
                          re.IGNORECASE))


def _guided_cost_total_words(text: str) -> bool:
    """True when a cost-basis answer confirms the total ("both together")."""
    toks = set(re.findall(r"[a-z]+", (text or "").lower()))
    return bool(toks & _GUIDED_TOTAL_WORDS)


def _guided_cost_basis(text: str) -> str | None:
    """Which unit a cost amount names: pack ("per pack") or single ("each").

    A pack noun wins ("each pack" is per-pack); bare "each" is per-single,
    matching the sell_unit synonyms.
    """
    toks = set(re.findall(r"[a-z]+", (text or "").lower()))
    if toks & _GUIDED_PACK_NOUNS:
        return "pack"
    if toks & _GUIDED_SINGLE_NOUNS:
        return "single"
    return None


def _guided_pack_noun(text: str) -> bool:
    return bool(set(re.findall(r"[a-z]+", (text or "").lower()))
                & _GUIDED_PACK_NOUNS)


def _guided_number_words_to_digits(text: str) -> str:
    out = text or ""
    for word in sorted(_GUIDED_NUMBER_WORDS, key=len, reverse=True):
        out = re.sub(rf"\b{word}\b", str(_GUIDED_NUMBER_WORDS[word]), out,
                     flags=re.IGNORECASE)
    return out


def _guided_per_pack(text: str, bare_ok: bool = True) -> float | None:
    """Singles-per-pack count in text ("five in each pack" -> 5).

    Explicit per-pack phrasing wins; a bare lone number counts only when
    bare_ok (the per_pack field itself, never as a packs-field cross-fill).
    A bare pack count ("2 packs") never fills per_pack.
    """
    if re.search(r"[Rr]\s*\d", text or ""):
        return None  # money is not a count; the caller clarifies
    norm = _guided_number_words_to_digits(text)
    for rx in _GUIDED_PER_PACK_RES:
        m = rx.search(norm)
        if m:
            try:
                val = float(m.group(1))
            except ValueError:
                continue
            return val if val > 0 else None
    if _GUIDED_N_PACKS_RE.search(norm):
        return None
    if "both" in set(re.findall(r"[a-z]+", norm.lower())):
        return None
    if not bare_ok:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", norm)
    if m:
        val = float(m.group(1))
        return val if val > 0 else None
    return None


def _guided_packs_count(text: str) -> float | None:
    """Outer-pack count in text ("2 packs", "both" -> 2, bare "2")."""
    if re.search(r"[Rr]\s*\d", text or ""):
        return None  # money is not a count; the caller clarifies
    qty = _guided_qty(text)
    if qty is not None:
        return qty
    if "both" in set(re.findall(r"[a-z]+", (text or "").lower())):
        return 2.0
    return None


def _guided_cross_fill_packs(item: dict, text: str) -> None:
    """Fill pack count/unit from explicit phrasing ("for 3 packs", "per pack").

    Only fills what is still unknown; stated answers are never overwritten.
    """
    if item.get("packs") is None:
        count = None
        m = _GUIDED_FOR_N_PACKS_RE.search(text or "")
        if m:
            count = float(m.group(1))
        else:
            for word, val in _GUIDED_NUMBER_WORDS.items():
                if re.search(rf"\bfor\s+{word}\s+packs?\b", text or "",
                             re.IGNORECASE):
                    count = float(val)
                    break
            else:
                toks = set(re.findall(r"[a-z]+", (text or "").lower()))
                if "both" in toks and _guided_pack_word(text):
                    count = 2.0
        if count:
            item["packs"] = count
    if not item.get("unit"):
        pack_word = _guided_pack_word(text)
        if pack_word:
            item["unit"] = pack_word


def _guided_stash_cost(item: dict, cents: int, text: str) -> None:
    """Hold a totalled cost for the basis question (assigns nothing yet)."""
    item["cost_stash"] = cents
    item["cost_total"] = True
    _guided_cross_fill_packs(item, text)


def _guided_fill_packs_from_count(item: dict, text: str) -> bool:
    """Fill packs from an explicit pack count ("2 packs", "both packs")."""
    norm = _guided_number_words_to_digits(text)
    m = re.search(r"(\d+(?:\.\d+)?)\s*packs?\b", norm)
    if m:
        try:
            val = float(m.group(1))
        except ValueError:
            return False
        if val > 0:
            item["packs"] = val
            return True
        return False
    toks = set(re.findall(r"[a-z]+", norm.lower()))
    if "both" in toks and _guided_pack_word(text):
        item["packs"] = 2.0
        return True
    return False


def _guided_pack_context(item: dict) -> bool:
    if _is_pack_unit(item.get("unit")):
        return True
    if _MULTIPACK_RE.search(item.get("name") or ""):
        return True
    if item.get("pack_q") or item.get("cost_stash") is not None:
        return True
    return bool(item.get("per_pack") or item.get("packs"))


def _guided_is_existing(item: dict) -> bool:
    """True for a careful inventory match (unique row, resolved identity)."""
    return item.get("product_id") is not None \
        and not item.get("ambiguous") and not item.get("new")


# Affirmations accepted by confirm-qty (mirrors agent.YES_WORDS; tools
# cannot import agent). Whole-message match only.
_GUIDED_YES_WORDS = {"yes", "y", "yebo", "yeah", "ja", "ewe", "ok",
                     "okay", "sharp", "shap", "sure"}


def _guided_is_yes(text: str) -> bool:
    norm = re.sub(r"\s+", " ", (text or "").strip().lower()
                  .strip(" .!?")).strip()
    return norm in _GUIDED_YES_WORDS


def _guided_seen_qty(item: dict) -> float | None:
    """Numeric staged seen count, if any (qty itself is still None)."""
    for raw in (item.get("seen_label"), item.get("packs_seen")):
        try:
            qty = float(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if qty > 0:
            return qty
    return None


# ---------------------------------------------------------------------------
# Task 5: conversation commands + photo/dup choices.
# ---------------------------------------------------------------------------

_GUIDED_CMD_BACK = {"back", "go back"}
_GUIDED_CMD_SKIP_ITEM = {"skip item", "skip this item"}
_GUIDED_CMD_PAUSE = {"pause", "later"}
_GUIDED_CMD_RESUME = {"resume", "continue"}
# Mirrors agent.CANCEL_WORDS; tools cannot import agent.
_GUIDED_CMD_CANCEL = {"cancel", "cancel that", "never mind", "nevermind",
                      "kanselleer", "kanselleer dit", "khansela",
                      "ngiyayekisa", "rhoxisa"}
_GUIDED_CMD_CANCEL_ALL = {"cancel all", "cancel everything"}
_GUIDED_CMD_ADD = {"add"}
_GUIDED_CMD_NEW = {"new"}
_GUIDED_CMD_RESTOCK = {"restock"}
# Mirrors agent.NO_WORDS.
_GUIDED_NO_WORDS = {"no", "n", "nope", "cha", "hayi", "nee", "aowa"}

# Fields the owner cannot skip: dodged with garbage they get g_required,
# while skippable price fields get g_invalid. Explicit "skip" stays
# g_invalid everywhere (Task 2 contract).
_GUIDED_REQUIRED_FIELDS = frozenset(
    {"name", "which", "qty", "packs", "sell_unit", "per_pack", "cost_basis"})

_GUIDED_ACK_TOKENS = {
    "thank", "thanks", "thx", "hello", "hi", "hey", "heita", "sawubona",
    "sanibona", "molo", "molweni", "dumela", "dumelang", "morning",
    "afternoon", "evening", "bye", "goodbye", "ngiyabonga", "dankie",
    "enkosi", "cool", "great", "nice", "awesome", "perfect",
}
_GUIDED_INTENT_TOKENS = {
    "cash", "float", "till", "petty", "bank", "owe", "owes", "owing",
    "owed", "debt", "debtor", "debtors", "balance", "balances", "statement",
    "statements", "receipt", "receipts", "slip", "slips", "invoice",
    "invoices", "order", "orders", "ordering", "supplier", "suppliers",
    "wholesaler", "wholesalers", "restock", "remind", "reminder",
    "reminders", "reminded", "snooze", "expense", "expenses", "paid",
    "pay", "paying", "payment", "payments", "profit", "profits", "report",
    "reports", "month", "monthly", "credit", "credits", "book", "booking",
    "booked", "customer", "customers", "menu", "menus", "help", "spaza",
    "permit", "permits", "licence", "license", "licences", "register",
    "registration", "fund", "funding", "funded", "compliance", "scam",
    "scams", "electricity", "rent", "wage", "wages", "transport",
    "cashup", "counted", "count", "stock", "stocks", "shelf", "shelves",
    "shefu", "isitokwe", "voorraad", "expire", "expires", "expired",
    "expiry", "expiries", "sold", "send", "sending", "sale", "sales",
    "afford", "affordable",
}
_GUIDED_Q_WORDS = {
    "what", "how", "why", "when", "where", "who", "whom", "whose",
    "which", "can", "could", "would", "should", "will", "do", "does",
    "did", "is", "are", "was", "were", "have", "has", "had",
}

_DUP_STASH_KEY = "intake-dup-photo"


def _guided_command(text: str) -> str:
    """Normalize a message for whole-message command matching."""
    return re.sub(r"\s+", " ", (text or "").strip().lower()
                  ).strip(" .!?,:;\"'")


def _guided_unrelated(text: str, field: str | None) -> bool:
    """True when the text is new business for the planner, not an answer.

    Greetings, business-intent words, and (outside the free-text name
    field) questions and question words all belong to the planner; vague
    fragments like "yesterday maybe" do not and get clarified instead.
    """
    msg = text or ""
    toks = set(re.findall(r"[a-z']+", msg.lower()))
    if toks & _GUIDED_ACK_TOKENS or toks & _GUIDED_INTENT_TOKENS:
        return True
    if field != "name":
        if "?" in msg or toks & _GUIDED_Q_WORDS:
            return True
    return False


def _guided_reject(item: dict, field: str) -> str:
    """Clarify a dodged question: required fields insist, prices ask."""
    hint = ask_intake_field(item, field)
    if field in _GUIDED_REQUIRED_FIELDS:
        return _t("g_required", hint=hint)
    return _t("g_invalid", hint=hint)


def _guided_reject_or_pass(item: dict, field: str, msg: str) -> str | None:
    """Clarify an attempted answer, pass new business to the planner."""
    if not _guided_attempted(msg) and _guided_unrelated(msg, field):
        return None
    return _guided_reject(item, field)


def _save_dup_stash(conn: sqlite3.Connection, shop_id: str,
                    stash: dict) -> None:
    """Stash a duplicate photo's vision so 'restock' can reuse it."""
    ensure_shop(conn, shop_id)
    conn.execute("INSERT INTO flags (shop_id, key, value)"
                 " VALUES (?, ?, ?)"
                 " ON CONFLICT(shop_id, key) DO UPDATE SET value = ?",
                 (shop_id, _DUP_STASH_KEY, json.dumps(stash),
                  json.dumps(stash)))
    conn.commit()


def _load_dup_stash(conn: sqlite3.Connection, shop_id: str) -> dict | None:
    try:
        row = conn.execute("SELECT value FROM flags WHERE shop_id = ?"
                           " AND key = ?", (shop_id, _DUP_STASH_KEY)
                           ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        stash = json.loads(row["value"] or "")
    except (ValueError, TypeError):
        return None
    if not isinstance(stash, dict) or not stash:
        return None
    try:
        saved = datetime.fromisoformat(
            str(stash.get("saved_at") or "").replace("Z", "+00:00"))
    except ValueError:
        saved = None
    if saved is not None and datetime.now(timezone.utc) - saved \
            > _INTAKE_SESSION_TTL:
        _clear_dup_stash(conn, shop_id)  # stale: "restock" is order business
        return None
    return stash


def _clear_dup_stash(conn: sqlite3.Connection, shop_id: str) -> None:
    try:
        conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = ?",
                     (shop_id, _DUP_STASH_KEY))
        conn.commit()
    except sqlite3.OperationalError:
        pass


def _guided_drop_item_pending(conn: sqlite3.Connection, shop_id: str) -> None:
    """Silently supersede a live per-item preview (session moves on)."""
    pend = get_pending(conn, shop_id)
    if pend is not None and pend["action_type"] == "stock_intake_item":
        conn.execute("UPDATE pending_actions SET status = 'superseded',"
                     " resolved_at = ? WHERE id = ?",
                     (now_iso(), pend["id"]))
        conn.commit()


def guided_reprompt(conn: sqlite3.Connection, shop_id: str) -> str:
    """Re-ask the session's current question (or resurface its preview)."""
    session = get_intake_session(conn, shop_id)
    items = (session or {}).get("items_json") or []
    idx = (session or {}).get("current") or 0
    if session is None or idx < 0 or idx >= len(items):
        return _t("intake_no_preview")
    if session.get("field") is None:
        pend = get_pending(conn, shop_id)
        if pend is not None:
            return pend["preview_text"]  # live question wins, never cloned
        return propose_intake_item(conn, shop_id, idx)
    item = items[idx]
    head = _t("g_item", i=idx + 1, n=len(items),
              name=item.get("name") or "?")
    return f"{head}\n{ask_intake_field(item, session['field'])}"


def _guided_remaining(session: dict) -> int:
    items = session.get("items_json") or []
    idx = session.get("current") or 0
    return sum(1 for e in items[idx:]
               if e.get("state") in (None, "pending"))


def _guided_cancel_all(conn: sqlite3.Connection, shop_id: str,
                       session: dict) -> str:
    n = _guided_remaining(session)
    _guided_drop_item_pending(conn, shop_id)
    _clear_dup_stash(conn, shop_id)
    clear_intake_session(conn, shop_id)
    return _t("g_cancel_done", n=n)


def _guided_clear_field(item: dict, field: str) -> None:
    """Undo one trailed answer so its question can be re-asked."""
    if field in ("name", "which"):
        # Identity redo: "which" answers cannot be restored, so both go
        # back to the name question; user-stated prices survive.
        item["name"] = "?"
        item["product_id"], item["ambiguous"], item["new"] = None, [], False
        if item.get("sell_source") == "shoplist":
            item["sell_cents"], item["sell_source"] = None, None
        if item.get("per_pack_source") != "stated":
            item["per_pack"], item["per_pack_source"] = None, None
        if item.get("pack_sell_source") not in ("stated", "skipped"):
            item["pack_sell_cents"], item["pack_sell_source"] = None, None
    elif field == "qty":
        old = item.get("qty")
        item["qty"], item["qty_source"], item["stock_units"] = None, None, None
        if old is not None and item.get("packs") == old:
            item["packs"] = None  # "2 packs" answered both at once
    elif field == "cost":
        item["cost_cents"], item["cost_source"] = None, None
        item["cost_basis"], item["cost_total"], item["cost_stash"] = \
            None, None, None
    elif field == "sell":
        item["sell_cents"], item["sell_source"] = None, None
        item["sell_both_asked"] = False
    elif field == "sell_unit":
        item["sell_unit"] = None
    elif field == "per_pack":
        item["per_pack"], item["per_pack_source"] = None, None
    elif field == "packs":
        item["packs"] = None
    elif field == "cost_basis":
        item["cost_basis"] = None
        if item.get("cost_total") and item.get("cost_source") == "stated" \
                and item.get("cost_stash") is None \
                and item.get("cost_cents") is not None:
            item["cost_stash"] = item["cost_cents"]  # total back on hold
            item["cost_cents"], item["cost_source"] = None, None
    elif field == "pack_sell":
        item["pack_sell_cents"], item["pack_sell_source"] = None, None


def _guided_back(conn: sqlite3.Connection, shop_id: str,
                 session: dict) -> str:
    """Redo the last answer, else the previous item, else g_back_none."""
    items = session.get("items_json") or []
    idx = session.get("current") or 0
    item = items[idx]
    trail = item.get("trail") or []
    if trail:
        last = trail.pop()
        item["trail"] = trail
        _guided_clear_field(item, last)
        field = "name" if last == "which" else last
        session["field"] = field
        _guided_drop_item_pending(conn, shop_id)
        save_intake_session(conn, shop_id, session)
        return ask_intake_field(item, field)
    if idx > 0:
        session["current"] = idx - 1
        prev = items[idx - 1]
        nxt = _guided_next_field(prev)
        session["field"] = nxt
        _guided_drop_item_pending(conn, shop_id)
        save_intake_session(conn, shop_id, session)
        if nxt is None:
            return propose_intake_item(conn, shop_id, idx - 1)
        head = _t("g_item", i=idx, n=len(items),
                  name=prev.get("name") or "?")
        return f"{head}\n{ask_intake_field(prev, nxt)}"
    return _t("g_back_none")


def _guided_skip_item(conn: sqlite3.Connection, shop_id: str,
                      session: dict) -> str:
    """Skip the whole current item and ask the next one (Task 6 summarizes)."""
    items = session.get("items_json") or []
    idx = session.get("current") or 0
    name = (items[idx].get("name") or "").strip() or "?"
    items[idx]["state"] = "skipped"
    _guided_drop_item_pending(conn, shop_id)
    nxt_idx = idx + 1
    while nxt_idx < len(items) and items[nxt_idx].get("state") not in (
            None, "pending"):
        nxt_idx += 1
    skipped = _t("g_skipped", name=name)
    if nxt_idx >= len(items):
        done = _guided_summary(items)
        clear_intake_session(conn, shop_id)
        return f"{skipped}\n{done}"
    session["current"] = nxt_idx
    nxt_item = items[nxt_idx]
    nxt = _guided_next_field(nxt_item)
    session["field"] = nxt
    save_intake_session(conn, shop_id, session)
    if nxt is None:
        return f"{skipped}\n" + propose_intake_item(conn, shop_id, nxt_idx)
    head = _t("g_item", i=nxt_idx + 1, n=len(items),
              name=nxt_item.get("name") or "?")
    return f"{skipped}\n{head}\n{ask_intake_field(nxt_item, nxt)}"


def _guided_add_photo(conn: sqlite3.Connection, shop_id: str,
                      session: dict) -> str | None:
    """Merge the stashed mid-session photo's products into the intake."""
    stash = session.get("stash") or {}
    if stash.get("kind") != "photo":
        return None
    fresh = [ln for ln in _guided_build_items(
        conn, shop_id, stash.get("vision") or [], [],
        stash.get("caption") or "", None)
        if str(ln.get("name") or "").strip().casefold() not in {
            str(e.get("name") or "").strip().casefold()
            for e in session.get("items_json") or []}]
    for key in ("kind", "digest", "caption", "vision"):
        stash.pop(key, None)
    session["stash"] = stash
    if not fresh:
        save_intake_session(conn, shop_id, session)
        return f"{_t('intake_noproducts')}\n" + guided_reprompt(conn, shop_id)
    session["items_json"].extend(fresh)
    save_intake_session(conn, shop_id, session)
    head = _t("g_head", done=session.get("current") or 0,
              total=len(session["items_json"]))
    names = ", ".join(ln.get("name") or "?" for ln in fresh)
    found = _t("g_found", n=len(fresh), names=names)
    return f"{head}\n{found}\n" + guided_reprompt(conn, shop_id)


def _guided_new_photo(conn: sqlite3.Connection, shop_id: str,
                      session: dict) -> str | None:
    """Restart the intake from the stashed mid-session photo."""
    stash = session.get("stash") or {}
    if stash.get("kind") != "photo":
        return None
    return start_guided_intake(conn, shop_id, stash.get("vision") or [], [],
                               stash.get("caption") or "",
                               stash.get("digest") or "")


def _guided_restock_photo(conn: sqlite3.Connection,
                          shop_id: str) -> str | None:
    """Start a guided intake from the stashed duplicate photo, if any."""
    stash = _load_dup_stash(conn, shop_id)
    if stash is None:
        return None
    _clear_dup_stash(conn, shop_id)
    return start_guided_intake(conn, shop_id, stash.get("products") or [],
                               [], stash.get("caption") or "",
                               stash.get("digest") or "")


def _guided_resume(conn: sqlite3.Connection, shop_id: str,
                   session: dict) -> str:
    """Reactivate a paused session and re-ask its current question."""
    was_paused = session.get("status") == "paused"
    session["status"] = "active"
    save_intake_session(conn, shop_id, session)
    prompt = guided_reprompt(conn, shop_id)
    if was_paused:
        return f"{_t('g_resumed')}\n{prompt}"
    return prompt


def _guided_cancel_step(conn: sqlite3.Connection, shop_id: str,
                        session: dict) -> str:
    """Ask before cancelling; a repeat confirms."""
    stash = session.get("stash") or {}
    if stash.get("cancel_armed"):
        return _guided_cancel_all(conn, shop_id, session)
    stash["cancel_armed"] = True
    session["stash"] = stash
    save_intake_session(conn, shop_id, session)
    return _t("g_cancel_ask", n=_guided_remaining(session))


def _guided_next_field(item: dict) -> str | None:
    """First still-open field for a guided item (None = ready for preview).

    New items: name -> which -> packs/qty -> cost -> sell -> sell_unit ->
    per_pack -> packs -> cost_basis -> pack_sell -> sell(single) -> preview.
    Existing products fast-path to preview, except pack-context items,
    which still go through the sell_unit/per_pack/basis questions.
    """
    if not (item.get("name") or "").strip() or item.get("name") == "?":
        return "name"
    if item.get("ambiguous"):
        return "which"
    if item.get("pack_q") and item.get("packs") is None:
        return "packs"
    if item.get("qty") is None and not item.get("pack_q"):
        return "qty"
    if item.get("cost_stash") is not None:
        return "cost_basis"
    if _guided_is_existing(item) and not _guided_pack_context(item):
        return None
    if not _guided_is_existing(item):
        if item.get("cost_cents") is None \
                and item.get("cost_source") != "skipped":
            return "cost"
        if item.get("sell_cents") is None \
                and item.get("sell_source") != "skipped":
            return "sell"
    if item.get("sell_unit") is None and _guided_pack_context(item):
        return "sell_unit"
    if item.get("sell_unit") in ("singles", "both") \
            and item.get("per_pack") is None:
        return "per_pack"
    if item.get("packs") is None and _guided_pack_context(item):
        return "packs"
    if item.get("cost_cents") is not None and item.get("cost_basis") is None \
            and item.get("cost_source") == "stated" \
            and item.get("sell_unit") in ("singles", "both") \
            and _guided_pack_context(item):
        return "cost_basis"
    if item.get("sell_unit") == "both" \
            and item.get("pack_sell_cents") is None \
            and item.get("pack_sell_source") != "skipped":
        return "pack_sell"
    if item.get("sell_unit") == "both" \
            and item.get("pack_sell_cents") is not None \
            and item.get("sell_cents") is None \
            and not item.get("sell_both_asked"):
        return "sell"
    return None


def ask_intake_field(item: dict, field: str) -> str:
    """Render the one question for an open guided-intake field."""
    name = (item.get("name") or "").strip() or "?"
    if field == "which":
        return _t("g_ask_which",
                  names=" / ".join(item.get("ambiguous") or []))
    if field == "qty":
        if _guided_is_existing(item):
            return _t("g_ask_qty_confirm", name=name,
                      seen=item.get("seen_label") or "?")
        return _t("g_ask_qty", name=name,
                  seen=item.get("seen_label") or "?")
    if field == "cost":
        return _t("g_ask_cost", name=name)
    if field == "sell":
        per = ""
        if item.get("sell_unit") == "both" \
                and item.get("pack_sell_cents") is not None:
            per = _t("g_per_single")  # re-asked as the single price
        return _t("g_ask_sell", name=name, per=per)
    if field == "pack_sell":
        return _t("g_ask_sell", name=name, per=_t("g_per_pack"))
    if field == "sell_unit":
        if item.get("per_pack") is not None \
                or _MULTIPACK_RE.search(name or ""):
            return _t("g_ask_unit_pack", name=name)
        return _t("g_ask_unit", name=name)
    if field == "per_pack":
        return _t("g_ask_perpack", name=name)
    if field == "packs":
        return _t("g_ask_packs", name=name)
    if field == "cost_basis":
        amt = item.get("cost_stash")
        if amt is None:
            amt = item.get("cost_cents") or 0
        return _t("g_ask_costbasis", amt=cents_to_rand(int(amt)))
    return _t("g_ask_name")


def _guided_line(conn: sqlite3.Connection, shop_id: str, name: str,
                 entry: dict, seen: dict | None,
                 supplier: str | None) -> dict:
    line = _intake_line(conn, shop_id, name, entry, seen, supplier)
    seen_label = ""
    if isinstance(seen, dict):
        count = seen.get("visible_count")
        if count:
            try:
                seen_label = _fmt_qty(float(count))
            except (TypeError, ValueError):
                seen_label = str(count)
        elif str(seen.get("notes") or "").strip():
            seen_label = str(seen["notes"]).strip()[:40]
        else:
            seen_label = "?"
    qty = line.get("qty")
    packs = None
    if line.get("packs_seen"):
        try:
            packs = float(line["packs_seen"])
        except (TypeError, ValueError):
            packs = None
    if packs is None and qty is not None \
            and _is_pack_unit(line.get("unit")):
        packs = float(qty)
    line.update({
        "cost_basis": None, "cost_total": None, "cost_stash": None,
        "cost_normalized": False,
        "sell_unit": None, "per_pack": None, "packs": packs,
        "pack_sell_cents": None, "pack_sell_source": None,
        "sell_both_asked": False, "state": "pending",
        "seen_label": seen_label,
        "stock_units": float(qty) if qty is not None else None,
        "supplier": (supplier or "").strip(),
    })
    _guided_fill_pack_knowns(conn, line)
    return line


def _guided_fill_pack_knowns(conn: sqlite3.Connection, item: dict) -> None:
    """Pre-fill pack structure from a matched product row (never overwrite).

    A known pack_size answers per_pack and a known pack price answers
    pack_sell, so restocks never re-ask what the shelf list already knows.
    """
    pid = item.get("product_id")
    if pid is None:
        return
    row = conn.execute("SELECT pack_size, pack_price_cents FROM products"
                       " WHERE id = ?", (pid,)).fetchone()
    if row is None:
        return
    if item.get("per_pack") is None and row["pack_size"]:
        try:
            item["per_pack"] = float(row["pack_size"])
        except (TypeError, ValueError):
            pass
    if item.get("pack_sell_cents") is None and row["pack_price_cents"]:
        item["pack_sell_cents"] = row["pack_price_cents"]
        item["pack_sell_source"] = "shoplist"


def _guided_restock(item: dict) -> None:
    """Best-effort live stock_units update as pack answers arrive."""
    totals = guided_pack_totals(item)
    if totals["stock_units"] is not None:
        item["stock_units"] = totals["stock_units"]


def guided_pack_totals(item: dict) -> dict:
    """Normalize a guided item's pack answers to stock/price totals.

    Pure helper (no session writes); Task 6's applier calls this to build
    the add_stock payload. Mirrors `_apply_add_stock` costing: per-single
    cost is pack cost / pack_size as a float, rounded only in totals
    (integral results come back as int).

    Returns {stock_units, cost_cents, price_cents, pack_price_cents,
    pack_name, pack_size, complete}:
    - stock_units: singles (singles/both) or packs (whole) or qty.
    - cost_cents: per stock unit (per-single for singles/both, per-pack
      for whole). A total ("R20 for both packs") is split across packs.
    - price_cents: single sell price (None when selling whole packs with
      a stated price — that price is the pack price instead).
    - pack_price_cents: pack sell price (both, or whole with stated sell).
    - pack_name/pack_size: for the product row ("both" writes all four).
    - complete: all needed pack answers are known.
    """
    packs = item.get("packs")
    per_pack = item.get("per_pack")
    try:
        packs = float(packs) if packs is not None else None
        per_pack = float(per_pack) if per_pack is not None else None
    except (TypeError, ValueError):
        packs, per_pack = None, None
    if packs is not None and packs <= 0:
        packs = None
    if per_pack is not None and per_pack <= 0:
        per_pack = None
    qty = item.get("qty")
    try:
        qty = float(qty) if qty is not None else None
    except (TypeError, ValueError):
        qty = None
    unit = (item.get("sell_unit") or "").strip().lower()
    if unit == "whole":
        unit = "pack"
    cost = item.get("cost_cents")
    basis = item.get("cost_basis")
    total = bool(item.get("cost_total"))
    sell = item.get("sell_cents")
    pack_sell = item.get("pack_sell_cents")

    if unit in ("singles", "both"):
        stock = packs * per_pack if packs and per_pack else qty
        complete = bool(packs and per_pack)
    elif unit == "pack":
        stock = packs if packs is not None else qty
        complete = stock is not None
    else:
        stock = qty
        complete = stock is not None

    per_pack_cost = cost
    if total and cost is not None and packs:
        per_pack_cost = cost / packs
        complete = complete and packs is not None
    norm_cost: int | float | None = None
    if cost is not None and item.get("cost_normalized"):
        norm_cost = cost  # propose already normalized this line: idempotent
    elif cost is not None:
        if unit in ("singles", "both"):
            if basis == "pack" and per_pack_cost is not None and per_pack:
                norm_cost = per_pack_cost / per_pack
            elif basis == "single":
                norm_cost = per_pack_cost
            else:
                norm_cost = per_pack_cost
                complete = False
        elif unit == "pack":
            norm_cost = per_pack_cost
        else:
            norm_cost = cost
        if isinstance(norm_cost, float) and norm_cost.is_integer():
            norm_cost = int(norm_cost)

    if unit == "pack":
        if pack_sell is not None:
            price, pack_price = None, pack_sell
            if sell is not None and item.get("sell_source") == "shoplist":
                price = sell  # row keeps its single price; packs are sold
        elif sell is not None and item.get("sell_source") == "stated":
            price, pack_price = None, sell  # the one stated price is per-pack
        else:
            price, pack_price = sell, None
    elif unit == "both":
        price, pack_price = sell, pack_sell
    else:
        price, pack_price = sell, None

    if _guided_pack_context(item):
        pack_name = _pack_echo(item.get("unit")) or "pack"
    else:
        pack_name = ""
    return {
        "stock_units": stock, "cost_cents": norm_cost,
        "price_cents": price, "pack_price_cents": pack_price,
        "pack_name": pack_name, "pack_size": per_pack,
        "complete": bool(complete),
    }


def _guided_build_items(conn: sqlite3.Connection, shop_id: str,
                        products: list, items: list, caption: str,
                        supplier: str | None) -> list:
    normed = []
    for entry in items or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("product") or entry.get("name") or "").strip()
        if not name:
            continue
        merged = dict(entry)
        merged["product"] = " ".join(name.split())
        normed.append(merged)
    planned = ground_intake_items(normed, products, caption)
    products = [p for p in products or [] if isinstance(p, dict)]
    used: set[int] = set()
    out = []
    singles = len(planned) == 1 and len(products) == 1
    for entry in planned:
        name = " ".join(str(entry.get("product")).split())
        seen = None
        words = set(name.lower().split())
        for i, prod in enumerate(products):
            if i in used:
                continue
            if words & set(_intake_label(prod).lower().split()):
                seen, used = prod, used | {i}
                break
        if seen is None and singles:
            seen, used = products[0], {0}
        out.append(_guided_line(conn, shop_id, name, entry, seen, supplier))
    for i, prod in enumerate(products):
        if i in used:
            continue
        label = _intake_label(prod)
        out.append(_guided_line(conn, shop_id, label or "?", {}, prod,
                                supplier))
    return out


def start_guided_intake(conn: sqlite3.Connection, shop_id: str,
                        products: list, items: list, caption: str,
                        photo_hash: str,
                        supplier: str | None = None) -> str:
    """Begin (replacing any active session): overview + first prompt."""
    ensure_shop(conn, shop_id)
    built = _guided_build_items(conn, shop_id, products, items, caption,
                                supplier)
    if not built:
        return _t("intake_noproducts")
    _guided_drop_item_pending(conn, shop_id)  # a fresh session replaces
    session = {
        "photo_digest": photo_hash or "",
        "caption": caption or "",
        "vision_json": [p for p in products or [] if isinstance(p, dict)],
        "items_json": built,
        "current": 0,
        "field": _guided_next_field(built[0]),
        "status": "active",
        "stash": {},
    }
    save_intake_session(conn, shop_id, session)
    head = _t("g_head", done=0, total=len(built))
    names = ", ".join(line.get("name") or "?" for line in built)
    found = _t("g_found", n=len(built), names=names)
    if session["field"] is None:
        return f"{head}\n{found}\n" + propose_intake_item(conn, shop_id, 0)
    first = _t("g_item", i=1, n=len(built),
               name=built[0].get("name") or "?")
    return (f"{head}\n{found}\n{first}\n"
            f"{ask_intake_field(built[0], session['field'])}")


def _guided_line_detail(item: dict) -> str:
    qty = item.get("qty")
    totals = guided_pack_totals(item)
    stock = totals["stock_units"]
    packs = item.get("packs")
    pack_qty = bool(item.get("sell_unit") in ("singles", "both")
                    and packs and stock)
    if pack_qty:
        qtybit = _t("g_pack_qty", p=_fmt_qty(packs),
                    unit=item.get("unit") or "packs",
                    s=_fmt_qty(stock))
    elif qty is None:
        qtybit = _t("intake_qty_q")
    elif item.get("qty_source") == "seen":
        qtybit = _t("intake_seen", n=_fmt_qty(qty))
    else:
        qtybit = f"×{_fmt_qty(qty)}"
    if not pack_qty and item.get("unit"):
        qtybit += f" {item['unit']}"
    if item.get("sell_unit") == "both" and item.get("sell_cents") is not None \
            and item.get("pack_sell_cents") is not None:
        sellbit = _t("g_both_sell",
                     single=cents_to_rand(item["sell_cents"]),
                     pack=cents_to_rand(item["pack_sell_cents"]))
    elif item.get("sell_unit") == "both" \
            and item.get("pack_sell_cents") is not None:
        sellbit = _t("g_pack_sell",
                     amt=cents_to_rand(item["pack_sell_cents"]))
    elif totals["pack_price_cents"] is not None \
            and item.get("sell_unit") == "pack":
        sellbit = _t("g_pack_sell",
                     amt=cents_to_rand(totals["pack_price_cents"]))
    elif item.get("sell_cents") is not None:
        if item.get("sell_source") == "shoplist":
            sellbit = _t("g_reuse_price",
                         amt=cents_to_rand(item["sell_cents"]))
        else:
            key = "intake_sell_said" \
                if item.get("sell_source") == "stated" \
                else "intake_sell_list"
            sellbit = _t(key, amt=cents_to_rand(item["sell_cents"]))
    else:
        sellbit = _t("intake_sell_q")
    costbit = _t("intake_cost", amt=cents_to_rand(item["cost_cents"])) \
        if item.get("cost_cents") is not None else _t("intake_cost_q")
    tags = f" {_t('g_new_tag')}" if item.get("new") else ""
    return f"• {qtybit} · {sellbit} · {costbit}{tags}"


def propose_intake_item(conn: sqlite3.Connection, shop_id: str,
                        idx: int, *, commit: bool = True) -> str:
    """Propose the per-item Yes/No preview for items[idx]."""
    session = get_intake_session(conn, shop_id)
    items = (session or {}).get("items_json") or []
    if session is None or idx < 0 or idx >= len(items):
        return _t("intake_no_preview")
    item = items[idx]
    if item.get("pack_q") and item.get("qty") is None \
            and item.get("packs") is not None:
        item["qty"] = item["packs"]
        item["qty_source"] = item.get("qty_source") or "stated"
        item["unit"] = item.get("unit") or "packs"
        item["pack_q"] = False
    totals = guided_pack_totals(item)
    if totals["stock_units"] is not None:
        item["stock_units"] = totals["stock_units"]
    if item.get("cost_cents") is not None and totals["cost_cents"] is not None:
        if totals["cost_cents"] != item["cost_cents"]:
            item["cost_stated_cents"] = item["cost_cents"]
            item["cost_cents"] = totals["cost_cents"]
        item["cost_normalized"] = True
    session["current"] = idx
    session["field"] = None
    save_intake_session(conn, shop_id, session, commit=commit)
    head = _t("g_item", i=idx + 1, n=len(items),
              name=item.get("name") or "?")
    payload = {"idx": idx, "item": item,
               "supplier": item.get("supplier") or "",
               "photo_hash": session.get("photo_digest") or ""}
    return _propose(conn, shop_id, "stock_intake_item", payload,
                    f"{head}\n{_guided_line_detail(item)}\n"
                    f"{_t('g_preview_ask')}", commit=commit)


def _guided_qty_bit(item: dict, totals: dict, stock) -> str:
    """Short qty display for saves and summaries ("x3", "x2 packs (10 singles)")."""
    unit = (item.get("sell_unit") or "").strip().lower()
    packs = item.get("packs")
    if unit in ("singles", "both") and packs and stock is not None:
        return _t("g_pack_qty", p=_fmt_qty(packs),
                  unit=item.get("unit") or "packs",
                  s=_fmt_qty(stock))
    if stock is None:
        return ""
    bit = f"×{_fmt_qty(stock)}"
    if unit == "pack":
        word = item.get("unit") or totals.get("pack_name") or ""
        if word:
            bit += f" {word}"
    return bit


def _guided_summary(items: list) -> str:
    """Final intake summary: saved lines (name x qty), skipped, pending counts."""
    saved = [e for e in items if e.get("state") == "done"]
    skipped = [e for e in items if e.get("state") == "skipped"]
    pending = [e for e in items if e.get("state") in (None, "pending")]
    lines = [_t("g_summary", saved=len(saved), skipped=len(skipped),
                pending=len(pending))]
    for entry in saved:
        totals = guided_pack_totals(entry)
        qty = _guided_qty_bit(entry, totals, totals["stock_units"])
        lines.append(f"• {(entry.get('name') or '?').strip()} {qty}".rstrip())
    for entry in skipped:
        lines.append(_t("g_skipped", name=(entry.get("name") or "?").strip()))
    return "\n".join(lines)


def _guided_noop_undo(conn: sqlite3.Connection, shop_id: str,
                      item: dict, name: str) -> dict:
    """No-op "stock" undo for a stale done-state re-entry (never re-saves).

    Restores the product row's current values with qty 0, so undoing the
    duplicate confirm changes nothing.
    """
    row = None
    if item.get("product_id") is not None:
        row = conn.execute("SELECT id, wholesaler, cost_cents, price_cents,"
                           " pack_price_cents FROM products WHERE id = ?",
                           (item["product_id"],)).fetchone()
    if row is None:
        row = conn.execute("SELECT id, wholesaler, cost_cents, price_cents,"
                           " pack_price_cents FROM products"
                           " WHERE shop_id = ? AND name = ?"
                           " ORDER BY id DESC LIMIT 1",
                           (shop_id, name)).fetchone()
    if row is None:  # product deleted between save and retry; nothing to undo
        return {"product_id": item.get("product_id"), "qty": 0,
                "created": False}
    return {"product_id": row["id"], "qty": 0, "created": False,
            "old_wholesaler": row["wholesaler"],
            "old_cost": row["cost_cents"], "old_price": row["price_cents"],
            "old_pack": row["pack_price_cents"]}


def _apply_stock_intake_item(conn: sqlite3.Connection, shop_id: str,
                             payload: dict, action_id: str):
    """Save one guided item atomically, then advance or summarize.

    Converts the previewed line to an add_stock payload via
    guided_pack_totals (cost passes through untouched, int-or-float),
    marks the item done, and appends the next prompt or the final
    summary. Kind "stock" so undo names the product. Own action_id per
    item keeps saves and undos per-item and atomic.

    Never commits: confirm_pending's single commit covers the stock
    writes, session advance, next-item propose, photo record, and
    actions row atomically.
    """
    item = dict(payload.get("item") or {})
    try:
        idx = int(payload.get("idx", 0))
    except (TypeError, ValueError):
        idx = 0
    name = (item.get("name") or "").strip() or "?"
    totals = guided_pack_totals(item)
    stock = totals["stock_units"]
    if stock is None:
        stock = item.get("qty") or 0  # defensive; propose guarantees qty
    unit = (item.get("sell_unit") or "").strip().lower()
    if unit == "whole":
        unit = "pack"
    # Read the session BEFORE any writes: get_intake_session may commit
    # (lazy expiry cleanup), which must never flush half an apply.
    session = get_intake_session(conn, shop_id)
    items = (session or {}).get("items_json") or []
    summary = f"stock {name} +{_fmt_qty(stock)}"
    saved_line = _t("g_item_saved", name=name,
                    qty=_guided_qty_bit(item, totals, stock))
    if session is not None and 0 <= idx < len(items) \
            and items[idx].get("state") == "done":
        # Stale re-entry (retry-Yes after this item already saved):
        # acknowledge without writing stock twice.
        return (f"{saved_line}\n{_t('g_no_preview')}", "stock",
                summary, _guided_noop_undo(conn, shop_id, item, name))
    # Shoplist prices are display-only; only stated prices are written.
    if unit == "pack":
        stated = item.get("pack_sell_source") == "stated" \
            or item.get("sell_source") == "stated"
        price = None
        pack_price = totals["pack_price_cents"] if stated else None
    elif unit == "both":
        price = totals["price_cents"] \
            if item.get("sell_source") == "stated" else None
        pack_price = totals["pack_price_cents"] \
            if item.get("pack_sell_source") == "stated" else None
    else:
        price = totals["price_cents"] \
            if item.get("sell_source") == "stated" else None
        pack_price = None
    _msg, _k, _s, undo = _apply_add_stock(conn, shop_id, {
        "product_id": item.get("product_id"), "name": name,
        "qty": stock,
        "wholesaler": payload.get("supplier") or item.get("supplier") or "",
        "cost_cents": totals["cost_cents"], "price_cents": price,
        "pack_price_cents": pack_price,
        "pack_name": totals["pack_name"] or "",
        "account": "cash"}, action_id)
    # _apply_add_stock only inserts cost/price for new rows: fill the
    # pack columns here (same values it wrote for existing rows, so
    # its old_pack undo stash stays correct; new rows undo by delete).
    row_fix: dict[str, object] = {}
    if pack_price is not None:
        row_fix["pack_price_cents"] = pack_price
    if unit == "both" and totals["pack_name"] and totals["pack_size"]:
        prow = conn.execute("SELECT pack_name, pack_size FROM products"
                            " WHERE id = ?",
                            (undo["product_id"],)).fetchone()
        undo["old_pack_name"] = prow["pack_name"] if prow else None
        undo["old_pack_size"] = prow["pack_size"] if prow else None
        row_fix["pack_name"] = totals["pack_name"]
        row_fix["pack_size"] = totals["pack_size"]
    if row_fix:
        cols = ", ".join(f"{col} = ?" for col in row_fix)
        conn.execute(f"UPDATE products SET {cols} WHERE id = ?",
                     (*row_fix.values(), undo["product_id"]))
    if session is None or idx != session.get("current") \
            or idx < 0 or idx >= len(items):
        # The Yes still stands (this item is saved); only the guided
        # context is gone, so there is nothing to advance to.
        return (f"{saved_line}\n{_t('g_no_preview')}", "stock",
                summary, undo)
    items[idx]["state"] = "done"
    nxt_idx = idx + 1
    while nxt_idx < len(items) and items[nxt_idx].get("state") not in (
            None, "pending"):
        nxt_idx += 1
    if nxt_idx >= len(items):
        done = _guided_summary(items)
        clear_intake_session(conn, shop_id, commit=False)
        return (f"{saved_line}\n{done}", "stock", summary, undo)
    session["current"] = nxt_idx
    nxt = _guided_next_field(items[nxt_idx])
    session["field"] = nxt
    save_intake_session(conn, shop_id, session, commit=False)
    if nxt is None:
        return (f"{saved_line}\n"
                f"{propose_intake_item(conn, shop_id, nxt_idx, commit=False)}",
                "stock", summary, undo)
    head = _t("g_item", i=nxt_idx + 1, n=len(items),
              name=items[nxt_idx].get("name") or "?")
    return (f"{saved_line}\n{head}\n"
            f"{ask_intake_field(items[nxt_idx], nxt)}",
            "stock", summary, undo)


def guided_skip_pending(conn: sqlite3.Connection, shop_id: str) -> str:
    """No on an item preview: skip that item and carry on (never generic)."""
    pend = get_pending(conn, shop_id)
    session = get_intake_session(conn, shop_id)
    items = (session or {}).get("items_json") or []
    try:
        idx = (json.loads(pend["payload_json"]) or {}).get("idx", 0) \
            if pend is not None else 0
        idx = int(idx)
    except (ValueError, TypeError):
        idx = 0
    if session is None or pend is None \
            or pend["action_type"] != "stock_intake_item" \
            or idx < 0 or idx >= len(items):
        return cancel_pending(conn, shop_id)
    session["current"] = idx
    return _guided_skip_item(conn, shop_id, session)


def _guided_set_name(conn: sqlite3.Connection, shop_id: str, item: dict,
                     text: str) -> None:
    name = " ".join(text.split())
    item["name"] = name
    pid, ambiguous, new = _intake_match(conn, shop_id, name)
    item["product_id"], item["ambiguous"], item["new"] = pid, ambiguous, new
    if pid is not None and item.get("sell_cents") is None:
        row = conn.execute("SELECT price_cents FROM products WHERE id = ?",
                           (pid,)).fetchone()
        if row and row["price_cents"]:
            item["sell_cents"], item["sell_source"] = row["price_cents"], \
                "shoplist"
    _guided_fill_pack_knowns(conn, item)


def _guided_pick_which(conn: sqlite3.Connection, shop_id: str, item: dict,
                       text: str) -> bool:
    options = item.get("ambiguous") or []
    if not options:
        return True
    msg = (text or "").strip().lower()
    pick = None
    if re.fullmatch(r"\d+", msg):
        num = int(msg)
        if 1 <= num <= len(options):
            pick = options[num - 1]
    if pick is None:
        for name in options:
            low = name.lower()
            if msg == low or msg in low or low in msg:
                pick = name
                break
    if pick is None:
        return False
    prod, _ = _find_products(conn, shop_id, pick)
    item["name"] = pick
    item["product_id"] = prod["id"] if prod else None
    item["ambiguous"] = []
    item["new"] = prod is None
    if prod is not None and prod["price_cents"] \
            and item.get("sell_cents") is None:
        item["sell_cents"], item["sell_source"] = prod["price_cents"], \
            "shoplist"
    _guided_fill_pack_knowns(conn, item)
    return True


_GUIDED_PREVIEW_LINE_RE = re.compile(r"(?i)\bline\s+\d")


def _guided_looks_money(text: str) -> bool:
    """True when the text states money (never a plain count)."""
    msg = text or ""
    if re.search(r"[Rr]\s*\d", msg) or re.search(r"\d+\.\d", msg):
        return True
    return bool(re.search(r"(?i)\b\d+\s*rands?\b", msg))


def _guided_apply_preview_correction(item: dict, msg: str) -> bool:
    """Apply an at-preview fix to the line. False when nothing matched."""
    has_cost = bool(_GUIDED_COST_RE.search(msg))
    has_sell = bool(_GUIDED_SELL_RE.search(msg))
    if has_cost != has_sell:  # one named price: "sell R25", "cost R18"
        cents = _coerce_pos_int(_guided_amount_cents(msg))
        if cents is None:
            return False
        target = "cost" if has_cost else "sell"
        if target == "sell" and item.get("sell_unit") == "both" \
                and _guided_pack_noun(msg):
            target = "pack_sell"  # "pack price R30" corrects the pack price
        if target == "cost" and _guided_cost_total(msg) \
                and (_guided_pack_context(item) or _guided_pack_word(msg)):
            _guided_stash_cost(item, cents, msg)  # reopens cost_basis
        else:
            item[f"{target}_cents"] = cents
            item[f"{target}_source"] = "stated"
            _guided_cross_fill_packs(item, msg)
            if target == "cost" and item.get("cost_basis") is None:
                basis = _guided_cost_basis(msg)
                if basis is not None:
                    item["cost_basis"] = basis
            if target == "sell" and item.get("sell_unit") is None \
                    and _guided_pack_context(item):
                preset = _guided_sell_unit(msg)
                if preset is not None:
                    item["sell_unit"] = preset
            if target == "sell" and item.get("sell_unit") == "both":
                item["sell_both_asked"] = True
        return True
    pp = _guided_per_pack(msg, bare_ok=False)
    if pp is not None:  # explicit "5 in each pack" only; bare "5" is qty
        item["per_pack"] = pp
        item["per_pack_source"] = "stated"
        _guided_restock(item)
        return True
    if _guided_fill_packs_from_count(item, msg):  # "2 packs", "both packs"
        if item.get("qty") is None:
            item["qty"] = item["packs"]
            item["qty_source"] = "stated"
            item["pack_q"] = False
        item["unit"] = _guided_pack_word(msg) or item.get("unit") or "packs"
        _guided_restock(item)
        return True
    if not (has_cost or has_sell) and not _guided_looks_money(msg) \
            and not _GUIDED_PREVIEW_LINE_RE.search(msg):
        qty = _guided_qty(msg)
        if qty is not None:
            item["qty"] = qty
            item["qty_source"] = "stated"
            pack_word = _guided_pack_word(msg)
            if pack_word:
                item["unit"] = pack_word
            if pack_word or _is_pack_unit(item.get("unit")):
                item["packs"] = qty
                item["pack_q"] = False
            _guided_restock(item)
            return True
    unit = _guided_sell_unit(msg)
    if unit is not None:  # "singles", "whole", "both"
        item["sell_unit"] = unit
        _guided_restock(item)
        return True
    return False


def _guided_answer_preview(conn: sqlite3.Connection, shop_id: str,
                           session: dict, msg: str) -> str | None:
    """Fix the previewed line from a correction, else clarify or pass on."""
    items = session.get("items_json") or []
    idx = session.get("current") or 0
    item = items[idx]
    cmd = _guided_command(msg)
    if cmd in _GUIDED_YES_WORDS or cmd in _GUIDED_NO_WORDS:
        return None  # bare Yes/No belongs to confirm/cancel, never edits
    if cmd == "skip":
        return _guided_skip_item(conn, shop_id, session)
    if _guided_apply_preview_correction(item, msg):
        nxt = _guided_next_field(item)
        session["field"] = nxt
        save_intake_session(conn, shop_id, session)
        if nxt is None:
            return propose_intake_item(conn, shop_id, idx)
        return ask_intake_field(item, nxt)
    if not _guided_attempted(msg) and _guided_unrelated(msg, None):
        return None
    return _t("g_invalid", hint=_t("g_preview_ask"))


def answer_intake_session(conn: sqlite3.Connection, shop_id: str,
                          text: str) -> str | None:
    """Answer the session's open question. None when no session/unrelated."""
    session = get_intake_session(conn, shop_id)
    msg = (text or "").strip()
    cmd = _guided_command(msg)
    if cmd in _GUIDED_CMD_RESTOCK:
        out = _guided_restock_photo(conn, shop_id)
        if out is not None:
            return out
        # No dup stash: "restock" is order business, parsed below as text.
    if session is None:
        return None
    items = session.get("items_json") or []
    idx = session.get("current") or 0
    field = session.get("field")
    if idx < 0 or idx >= len(items):
        return None
    stash = session.get("stash") or {}
    if cmd not in _GUIDED_CMD_CANCEL and cmd not in _GUIDED_CMD_CANCEL_ALL:
        stash.pop("cancel_armed", None)  # anything else disarms cancel
    if session.get("status") == "paused":
        if cmd in _GUIDED_CMD_RESUME:
            return _guided_resume(conn, shop_id, session)
        if cmd in _GUIDED_CMD_CANCEL_ALL:
            return _guided_cancel_all(conn, shop_id, session)
        if cmd in _GUIDED_CMD_CANCEL:
            return _guided_cancel_step(conn, shop_id, session)
        if cmd in _GUIDED_CMD_ADD:
            return _guided_add_photo(conn, shop_id, session)
        if cmd in _GUIDED_CMD_NEW:
            return _guided_new_photo(conn, shop_id, session)
        if _guided_unrelated(msg, field):
            return None
        return _t("g_paused")
    if cmd in _GUIDED_CMD_BACK:
        return _guided_back(conn, shop_id, session)
    if cmd in _GUIDED_CMD_SKIP_ITEM:
        return _guided_skip_item(conn, shop_id, session)
    if cmd in _GUIDED_CMD_PAUSE:
        session["status"] = "paused"
        save_intake_session(conn, shop_id, session)
        return _t("g_paused")
    if cmd in _GUIDED_CMD_RESUME:
        return _guided_resume(conn, shop_id, session)
    if cmd in _GUIDED_CMD_CANCEL_ALL:
        return _guided_cancel_all(conn, shop_id, session)
    if cmd in _GUIDED_CMD_CANCEL:
        return _guided_cancel_step(conn, shop_id, session)
    if cmd in _GUIDED_CMD_ADD:
        return _guided_add_photo(conn, shop_id, session)
    if cmd in _GUIDED_CMD_NEW:
        return _guided_new_photo(conn, shop_id, session)
    item = items[idx]
    if field is None:
        return _guided_answer_preview(conn, shop_id, session, msg)
    if not msg:
        return _guided_reject(item, field)
    if field == "name":
        if _guided_is_skip(msg) or "?" in msg:
            return _t("g_invalid", hint=ask_intake_field(item, field))
        if cmd in _GUIDED_YES_WORDS or cmd in _GUIDED_NO_WORDS:
            return _t("g_invalid", hint=ask_intake_field(item, field))
        if _guided_unrelated(msg, field):
            return None
        _guided_set_name(conn, shop_id, item, msg)
    elif field == "which":
        if not _guided_pick_which(conn, shop_id, item, msg):
            if _guided_is_skip(msg):
                return _t("g_invalid", hint=ask_intake_field(item, field))
            if cmd in _GUIDED_YES_WORDS or cmd in _GUIDED_NO_WORDS:
                return _t("g_invalid", hint=ask_intake_field(item, field))
            return _guided_reject_or_pass(item, field, msg)
    elif field == "qty":
        target = _guided_price_target(msg, field)
        if target in ("cost", "sell"):
            # Named price wins (correction); qty itself is re-asked.
            cents = _coerce_pos_int(_guided_amount_cents(msg))
            if cents is not None:
                if target == "cost" and _guided_cost_total(msg) \
                        and (_guided_pack_context(item)
                             or _guided_pack_word(msg)):
                    _guided_stash_cost(item, cents, msg)
                else:
                    item[f"{target}_cents"] = cents
                    item[f"{target}_source"] = "stated"
                    _guided_cross_fill_packs(item, msg)
                save_intake_session(conn, shop_id, session)
                return ask_intake_field(item, field)
        pp = _guided_per_pack(msg, bare_ok=False)
        norm = _guided_number_words_to_digits(msg)
        qtoks = set(re.findall(r"[a-z]+", norm.lower()))
        if pp is not None and not _GUIDED_N_PACKS_RE.search(norm) \
                and "both" not in qtoks:
            # "five in each pack": fills per_pack; packs still asked.
            item["per_pack"] = pp
            item["per_pack_source"] = "stated"
            if item.get("packs") is not None and item.get("qty") is None:
                item["qty"] = item["packs"]
                item["qty_source"] = "stated"
                item["unit"] = item.get("unit") or "packs"
                item["pack_q"] = False
            _guided_restock(item)
        else:
            qty = _guided_qty(msg)
            if qty is None:
                if _guided_is_yes(msg):
                    seen_qty = _guided_seen_qty(item)
                    if seen_qty is not None:
                        item["qty"] = seen_qty
                        item["qty_source"] = "seen"
                        item["stock_units"] = float(seen_qty)
                    else:
                        return _t("g_invalid",
                                  hint=ask_intake_field(item, field))
                elif _guided_is_skip(msg):
                    return _t("g_invalid",
                              hint=ask_intake_field(item, field))
                else:
                    return _guided_reject_or_pass(item, field, msg)
            else:
                item["qty"] = qty
                item["qty_source"] = "stated"
                pack_word = _guided_pack_word(msg)
                if pack_word:
                    item["unit"] = pack_word
                if pack_word or _is_pack_unit(item.get("unit")):
                    item["packs"] = qty
                    item["pack_q"] = False
                if pp is not None:
                    item["per_pack"] = pp  # "2 packs of 5" fills both
                    item["per_pack_source"] = "stated"
                _guided_restock(item)
    elif field in ("cost", "sell"):
        if _guided_is_skip(msg):
            item[f"{field}_source"] = "skipped"  # stays None, never re-asked
            if field == "sell" and item.get("sell_unit") == "both":
                item["sell_both_asked"] = True  # re-ask declined; no loop
        else:
            target = _guided_price_target(msg, field)
            if target == "sell" and field == "sell" \
                    and item.get("sell_unit") == "both" \
                    and _guided_pack_noun(msg):
                target = "pack_sell"  # "pack price R30" corrects pack price
            cents = _coerce_pos_int(_guided_amount_cents(msg))
            if cents is None:
                return _guided_reject_or_pass(item, field, msg)
            if target == "cost" and _guided_cost_total(msg) \
                    and (_guided_pack_context(item)
                         or _guided_pack_word(msg)):
                _guided_stash_cost(item, cents, msg)
            else:
                # Named field wins even when filled (correction); open re-asked.
                item[f"{target}_cents"] = cents
                item[f"{target}_source"] = "stated"
                _guided_cross_fill_packs(item, msg)
                if target == "cost" and item.get("cost_basis") is None:
                    basis = _guided_cost_basis(msg)
                    if basis is not None:
                        item["cost_basis"] = basis
                if target == "sell" and item.get("sell_unit") is None \
                        and _guided_pack_context(item):
                    preset = _guided_sell_unit(msg)
                    if preset is not None:
                        item["sell_unit"] = preset
                if target == "sell" and item.get("sell_unit") == "both":
                    item["sell_both_asked"] = True
    elif field == "sell_unit":
        unit = _guided_sell_unit(msg)
        if unit is None:
            if _guided_is_skip(msg):
                return _t("g_invalid", hint=ask_intake_field(item, field))
            return _guided_reject_or_pass(item, field, msg)
        item["sell_unit"] = unit
        _guided_restock(item)
    elif field == "per_pack":
        filled_packs = _guided_fill_packs_from_count(item, msg)
        pp = _guided_per_pack(msg)
        if pp is None:
            if not filled_packs:
                if _guided_is_skip(msg):
                    return _t("g_invalid",
                              hint=ask_intake_field(item, field))
                return _guided_reject_or_pass(item, field, msg)
            # packs corrected; per_pack itself is re-asked below.
        else:
            item["per_pack"] = pp
            item["per_pack_source"] = "stated"
        _guided_restock(item)
    elif field == "packs":
        pp = _guided_per_pack(msg, bare_ok=False)
        norm = _guided_number_words_to_digits(msg)
        ptoks = set(re.findall(r"[a-z]+", norm.lower()))
        if pp is not None:
            item["per_pack"] = pp
            item["per_pack_source"] = "stated"
        count = None
        if pp is None or _GUIDED_N_PACKS_RE.search(norm) or "both" in ptoks:
            count = _guided_packs_count(msg)
        if count is None:
            if pp is not None:
                pass  # per_pack filled; packs itself is re-asked below.
            elif _guided_is_skip(msg):
                return _t("g_invalid", hint=ask_intake_field(item, field))
            else:
                return _guided_reject_or_pass(item, field, msg)
        else:
            item["packs"] = count
            if item.get("qty") is None:
                item["qty"] = count
                item["qty_source"] = "stated"
                item["pack_q"] = False
            pack_word = _guided_pack_word(msg)
            item["unit"] = pack_word or item.get("unit") or "packs"
        _guided_restock(item)
    elif field == "cost_basis":
        basis = _guided_cost_basis(msg)
        total_words = _guided_cost_total_words(msg)
        if basis is None and not total_words:
            if _guided_is_skip(msg):
                return _t("g_invalid", hint=ask_intake_field(item, field))
            return _guided_reject_or_pass(item, field, msg)
        if basis is None:
            item["cost_basis"] = "pack"  # total confirmed: split over packs
        else:
            item["cost_basis"] = basis
            if basis == "single":
                # Per-single overrides a total; pack words only pick the
                # pack horn (an explicit "for N packs" total stands).
                item["cost_total"] = None
        if item.get("cost_stash") is not None:
            item["cost_cents"] = item["cost_stash"]
            item["cost_source"] = "stated"
            item["cost_stash"] = None
        _guided_cross_fill_packs(item, msg)
    elif field == "pack_sell":
        if _guided_is_skip(msg):
            item["pack_sell_source"] = "skipped"
        else:
            has_cost = bool(_GUIDED_COST_RE.search(msg))
            has_sell = bool(_GUIDED_SELL_RE.search(msg))
            if has_cost and not has_sell:
                target = "cost"
            elif has_sell and not has_cost and not _guided_pack_noun(msg):
                target = "sell"
            else:
                target = "pack_sell"
            cents = _coerce_pos_int(_guided_amount_cents(msg))
            if cents is None:
                return _guided_reject_or_pass(item, field, msg)
            item[f"{target}_cents"] = cents
            item[f"{target}_source"] = "stated"
            if target == "sell" and item.get("sell_unit") == "both":
                item["sell_both_asked"] = True
    else:
        return None
    nxt = _guided_next_field(item)
    if nxt != field:
        trail = item.get("trail")
        if not isinstance(trail, list):
            trail = item["trail"] = []
        trail.append(field)  # corrections re-ask; only progress trails
    session["field"] = nxt
    save_intake_session(conn, shop_id, session)
    if nxt is None:
        return propose_intake_item(conn, shop_id, idx)
    return ask_intake_field(item, nxt)


_INTAKE_AMT_RE = re.compile(r"[Rr]?\s*(\d+(?:\.\d{1,2})?)")
_INTAKE_PER_PACK_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:per|in(?:side)?(?:\s+of)?(?:\s+a)?|/)\s*pack", re.IGNORECASE)
_INTAKE_PACK_OF_RE = re.compile(
    r"pack\s*(?:of\s*)?(\d+(?:\.\d+)?)", re.IGNORECASE)


def _intake_amounts(text):
    return [int(round(float(m.group(1)) * 100))
            for m in _INTAKE_AMT_RE.finditer(text or "")]


def _repropose_intake(conn, shop_id, payload):
    payload.pop("price_q", None)
    return _propose(conn, shop_id, "stock_intake", payload,
                    _render_intake(conn, shop_id, payload))


def _intake_set_price(payload, n, cost, sell):
    ln = payload["lines"][n - 1]
    if cost is not None:
        ln["cost_cents"], ln["cost_source"] = cost, "stated"
    if sell is not None:
        ln["sell_cents"], ln["sell_source"] = sell, "stated"


_INTAKE_FILLER = {"loaf", "loaves", "bottle", "bottles", "tin", "tins",
                  "jar", "jars", "sachet", "sachets", "piece", "pieces",
                  "item", "items"}


def _intake_apply_rest(conn, shop_id, ln, rest):
    """Fill one line from '3 packs, cost R18, sell R21' / '6 per pack'."""
    low = f" {(rest or '').strip()} "
    per_pack = False
    m = _INTAKE_PER_PACK_RE.search(low) or _INTAKE_PACK_OF_RE.search(low)
    if m and ln.get("pack_q"):
        try:
            per = float(m.group(1))
        except ValueError:
            per = 0
        if per > 0 and (ln.get("packs_seen") or 0) > 0:
            ln["qty"] = ln["packs_seen"] * per
            ln["qty_source"], ln["unit"] = "stated", None
            ln["pack_q"], ln["packs_seen"] = False, None
            per_pack = True
            low = _INTAKE_PER_PACK_RE.sub(" ", low)
            low = _INTAKE_PACK_OF_RE.sub(" ", low)
    cost = sell = None
    for key in ("cost", "sell"):
        mm = re.search(rf"\b{key}\w*\s*:?\s*[Rr]?\s*(\d+(?:\.\d{{1,2}})?)",
                       low)
        if mm:
            val = int(round(float(mm.group(1)) * 100))
            if key == "cost":
                cost = val
            else:
                sell = val
    words = re.sub(r"\bcost\w*\s*:?\s*[Rr]?\s*\d+(?:\.\d{1,2})?", " ", low,
                   flags=re.IGNORECASE)
    words = re.sub(r"\bsell\w*\s*:?\s*[Rr]?\s*\d+(?:\.\d{1,2})?", " ", words,
                   flags=re.IGNORECASE)
    toks = words.replace(",", " ").split()
    qty = None
    unit = None
    if toks:
        try:
            qty = float(toks[0])
            rest_words = toks[1:]
        except ValueError:
            qty = None
            rest_words = toks
        if qty is not None and qty <= 0:
            qty = None
        if qty is not None and rest_words and _is_pack_unit(rest_words[0]):
            unit = rest_words[0]
            rest_words = rest_words[1:]
    else:
        rest_words = []
    if qty is not None:
        if unit is not None and _is_pack_unit(unit) and \
                (ln.get("product_id") is None
                 or not _pack_size_of(conn, ln)):
            ln["pack_q"], ln["packs_seen"] = True, qty
            ln["qty"], ln["qty_source"] = None, None
        else:
            ln["qty"], ln["qty_source"], ln["unit"] = qty, "stated", unit
        rest_words = [w for w in rest_words if w.lower() not in _INTAKE_FILLER]
    name = " ".join(rest_words).strip(" .")
    renamed = False
    if name and not (qty is not None and not rest_words):
        pid, ambiguous, new = _intake_match(conn, shop_id, name)
        ln["name"] = name
        ln["product_id"], ln["ambiguous"], ln["new"] = pid, ambiguous, new
        if pid is not None:
            row = conn.execute("SELECT price_cents FROM products WHERE id = ?",
                               (pid,)).fetchone()
            if row and row["price_cents"] and ln.get("sell_cents") is None:
                ln["sell_cents"], ln["sell_source"] = row["price_cents"], \
                    "shoplist"
        renamed = True
    return per_pack or cost is not None or sell is not None \
        or qty is not None or renamed


def correct_intake_line(conn: sqlite3.Connection, shop_id: str, line_no: int,
                        rest: str) -> str:
    pend = get_pending(conn, shop_id)
    if pend is None or pend["action_type"] != "stock_intake":
        return _t("intake_no_preview")
    payload = json.loads(pend["payload_json"])
    if line_no < 1 or line_no > len(payload.get("lines", [])):
        return _t("intake_line_missing", n=line_no,
                  m=len(payload.get("lines", [])))
    ln = payload["lines"][line_no - 1]
    cost = sell = None
    low = f" {(rest or '').strip()} "
    for key in ("cost", "sell"):
        mm = re.search(rf"\b{key}\w*\s*:?\s*[Rr]?\s*(\d+(?:\.\d{{1,2}})?)",
                       low)
        if mm:
            val = int(round(float(mm.group(1)) * 100))
            if key == "cost":
                cost = val
            else:
                sell = val
    ok = _intake_apply_rest(conn, shop_id, ln, rest)
    if cost is not None or sell is not None:
        _intake_set_price(payload, line_no, cost, sell)
        ok = True
    if not ok:
        return _t("intake_bad_answer")
    return _repropose_intake(conn, shop_id, payload)


def answer_intake(conn: sqlite3.Connection, shop_id: str, text: str):
    """Fill the pending intake from a free-form answer. None to pass on."""
    pend = get_pending(conn, shop_id)
    if pend is None or pend["action_type"] != "stock_intake":
        return None
    payload = json.loads(pend["payload_json"])
    lines = payload.get("lines", [])
    if not lines or "?" in (text or ""):
        return None
    msg = (text or "").strip()
    if not msg:
        return None
    price_q = payload.get("price_q") or {}
    if price_q and re.fullmatch(r"(cost|sell)\w*", msg.lower()):
        _intake_set_price(payload, price_q["n"],
                          price_q["amount"] if msg.lower().startswith("cost")
                          else None,
                          None if msg.lower().startswith("cost")
                          else price_q["amount"])
        return _repropose_intake(conn, shop_id, payload)
    if re.fullmatch(r"[Rr]?\s*\d+(?:\.\d{1,2})?", msg):
        amount = _intake_amounts(msg)[0]
        looks_money = msg.lower().startswith("r") or "." in msg
        if not looks_money:
            qty_missing = [i + 1 for i, ln in enumerate(lines)
                           if ln.get("qty") is None and not ln.get("pack_q")]
            if len(qty_missing) == 1:
                lines[qty_missing[0] - 1]["qty"] = float(amount) / 100
                lines[qty_missing[0] - 1]["qty_source"] = "stated"
                return _repropose_intake(conn, shop_id, payload)
            pack_missing = [i + 1 for i, ln in enumerate(lines)
                            if ln.get("pack_q")]
            if len(pack_missing) == 1 and not qty_missing:
                ln = lines[pack_missing[0] - 1]
                per = float(amount) / 100
                if per > 0 and (ln.get("packs_seen") or 0) > 0:
                    ln["qty"] = ln["packs_seen"] * per
                    ln["qty_source"], ln["unit"] = "stated", None
                    ln["pack_q"], ln["packs_seen"] = False, None
                    return _repropose_intake(conn, shop_id, payload)
        missing = [i + 1 for i, ln in enumerate(lines)
                   if ln.get("cost_cents") is None
                   or ln.get("sell_cents") is None]
        if len(missing) == 1:
            ln = lines[missing[0] - 1]
            if (ln.get("cost_cents") is None) != \
                    (ln.get("sell_cents") is None):
                if ln.get("cost_cents") is None:
                    _intake_set_price(payload, missing[0], amount, None)
                else:
                    _intake_set_price(payload, missing[0], None, amount)
                return _repropose_intake(conn, shop_id, payload)
            payload["price_q"] = {"n": missing[0], "amount": amount}
            conn.execute("UPDATE pending_actions SET payload_json = ?"
                         " WHERE id = ?", (json.dumps(payload), pend["id"]))
            conn.commit()
            from app.db import cents_to_rand
            return _t("intake_costsell", amt=cents_to_rand(amount))
        return None
    segs = [s.strip() for s in
            re.split(r",|;|\band\b|&|\+", msg, flags=re.IGNORECASE)
            if s.strip()]
    if not segs:
        return None
    targets = []
    scope = None
    for seg in segs:
        m = re.match(r"line\s+(\d+)\s*:\s*(.+)$", seg, re.IGNORECASE)
        if m:
            n = int(m.group(1))
            if n < 1 or n > len(lines):
                return _t("intake_line_missing", n=n, m=len(lines))
            scope = n
            targets.append((n, m.group(2)))
            continue
        words = set(seg.lower().split())
        best = None
        for i, ln in enumerate(lines, 1):
            label = (ln.get("name") or "").lower()
            if words & set(label.split()):
                best = i
                break
        if best is None and scope is not None and (
                _intake_amounts(seg) or "cost" in seg.lower()
                or "sell" in seg.lower() or "pack" in seg.lower()
                or re.search(r"\d", seg)):
            best = scope  # 'line 1: cost R18, sell R21'
        if best is None and ("cost" in seg.lower()) != ("sell" in seg.lower()):
            want = "cost_cents" if "cost" in seg.lower() else "sell_cents"
            cands = [i for i, ln in enumerate(lines, 1)
                     if ln.get(want) is None]
            if len(cands) == 1:
                best = cands[0]
        if best is None and len(lines) == 1 and (
                _intake_amounts(seg) or re.search(r"\d", seg)
                or "cost" in seg.lower() or "sell" in seg.lower()
                or "pack" in seg.lower()):
            best = 1
        if best is None:
            return None
        scope = best
        targets.append((best, seg))
    applied = False
    for n, rest in targets:
        if _intake_apply_rest(conn, shop_id, lines[n - 1], rest):
            low = f" {rest} "
            cost = sell = None
            for key in ("cost", "sell"):
                mm = re.search(
                    rf"\b{key}\w*\s*:?\s*[Rr]?\s*(\d+(?:\.\d{{1,2}})?)", low,
                    re.IGNORECASE)
                if mm:
                    val = int(round(float(mm.group(1)) * 100))
                    if key == "cost":
                        cost = val
                    else:
                        sell = val
            if cost is not None or sell is not None:
                _intake_set_price(payload, n, cost, sell)
            applied = True
    if not applied:
        return None
    return _repropose_intake(conn, shop_id, payload)


def add_stock(conn: sqlite3.Connection, shop_id: str, product: str, qty: float,
              wholesaler: str | None = None, cost_cents: int | None = None,
              price_cents: int | None = None, unit: str | None = None,
              pack_price_cents: int | None = None, account: str | None = None) -> str:
    """Add stock. qty is singles unless unit names a pack; a pack cost is
    converted to cost per single."""
    ensure_shop(conn, shop_id)
    name = (product or "").strip()
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        return _t("how_many")
    if not name:
        return _t("stock_which")
    if qty <= 0:
        return _t("how_many")
    prod, ambiguous = _find_products(conn, shop_id, name)
    if prod is None and ambiguous:
        return _which_one(ambiguous)
    if prod is None and _is_pack_unit(unit):
        return _t("stock_pack_unknown", name=name, low=name.lower())
    if prod is None:
        units, is_pack, no_pack = qty, False, False
        pack_name, pack_size = "", None
    else:
        pack_name, pack_size = prod["pack_name"], prod["pack_size"]
        units, is_pack, no_pack = _resolve_units(pack_name, pack_size, qty, unit)
    quoted_cost = _coerce_pos_int(cost_cents)
    cost = quoted_cost / pack_size if (is_pack and quoted_cost) else quoted_cost
    price = _coerce_pos_int(price_cents)
    pack = _coerce_pos_int(pack_price_cents)
    acct = str(account or "cash").strip().lower()
    acct = acct if acct in ("cash", "petty", "bank", "supplier") else "cash"
    payload = {"product_id": prod["id"] if prod else None, "name": prod["name"] if prod else name,
               "qty": units, "wholesaler": (wholesaler or "").strip(),
               "cost_cents": cost, "price_cents": price, "pack_price_cents": pack,
               "pack_name": pack_name, "account": acct}
    with _dry_run(conn):
        _apply_add_stock(conn, shop_id, payload, _action_id())
        pid = conn.execute("SELECT id FROM products WHERE shop_id = ? AND name = ?",
                           (shop_id, payload["name"])).fetchone()["id"]
        stock = conn.execute("SELECT stock_qty FROM products WHERE id = ?",
                             (pid,)).fetchone()["stock_qty"]
    extras = _fmt_price_extras(cost, price, pack, pack_name or "pack")
    old_hand = _fmt_on_hand(prod["stock_qty"], pack_name, pack_size) if prod else ""
    new_hand = _fmt_on_hand(stock, pack_name, pack_size)
    if prod is None:
        if extras:
            preview = _t("stock_prev_new", name=name, q=_fmt_qty(units),
                         extras=extras)
        else:
            preview = _t("stock_prev_new_noprice", name=name,
                         q=_fmt_qty(units))
    elif is_pack:
        preview = _t("stock_prev_pack", q=_fmt_qty(qty),
                     packs=_plural_pack(pack_name), name=prod['name'],
                     u=_fmt_qty(units), old=old_hand, new=new_hand,
                     extras=extras)
    else:
        preview = _t("stock_prev", u=_fmt_qty(units), name=prod['name'],
                     old=old_hand, new=new_hand, extras=extras)
    if no_pack:
        preview += _t("warn_singles", echo=_pack_echo(unit),
                      name=prod['name'])
    if cost is not None:
        total_cost = int(round(cost * units))
        if acct == "supplier":
            preview += _t("stock_supplier",
                          sup=payload['wholesaler'] or 'supplier',
                          total=cents_to_rand(total_cost))
        else:
            preview += _t("stock_paid", total=cents_to_rand(total_cost),
                          acct=_acct_name(acct))
    if cost is not None and price is not None and cost > price:
        preview += _t("stock_cost_high")
    return _propose(conn, shop_id, "add_stock", payload, preview)


def _apply_set_price(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    column = "pack_price_cents" if payload.get("for_pack") else "price_cents"
    conn.execute(f"UPDATE products SET {column} = ? WHERE id = ?",
                 (payload["price_cents"], payload["product_id"]))
    old = payload["old_price"]
    new = cents_to_rand(payload["price_cents"])
    what = f"{payload['name']} {payload.get('pack_name')}" if payload.get("for_pack") else payload["name"]
    msg = _t("price_saved_new", what=what, new=new) if old is None else \
        _t("price_saved", what=what, old=cents_to_rand(old), new=new)
    return msg, "price", f"price {what} {new}", \
        {"product_id": payload["product_id"], "old_price": old,
         "for_pack": bool(payload.get("for_pack")), "pack_name": payload.get("pack_name") or ""}


def set_price(conn: sqlite3.Connection, shop_id: str, product: str, price_cents: int,
              for_pack: bool = False) -> str:
    ensure_shop(conn, shop_id)
    prod, ambiguous = _find_products(conn, shop_id, product or "")
    if prod is None:
        if ambiguous:
            return _which_one(ambiguous)
        return _t("prod_not_on_shelf", name=(product or '').strip())
    if for_pack and not prod["pack_size"]:
        return _t("pack_no_size", name=prod['name'])
    try:
        price = int(price_cents)
    except (TypeError, ValueError):
        return _ask_price(prod["name"])
    if price <= 0:
        return _ask_price(prod["name"])
    if for_pack:
        old = prod["pack_price_cents"]
        what = f"{prod['name']} {prod['pack_name']}"
    else:
        old = prod["price_cents"]
        what = prod["name"]
    payload = {"product_id": prod["id"], "name": prod["name"],
               "price_cents": price, "old_price": old,
               "for_pack": bool(for_pack), "pack_name": prod["pack_name"] or ""}
    preview = _t("price_preview_new", what=what,
                       price=cents_to_rand(price)) if old is None else \
        _t("price_preview", what=what, old=cents_to_rand(old),
           price=cents_to_rand(price))
    return _propose(conn, shop_id, "set_price", payload, preview)


# ---------------------------------------------------------------------------
# Write tools: photo findings (structured data comes from vision in Step 7)
# ---------------------------------------------------------------------------


def _expiry_line(item: dict) -> str:
    if item["expiry_date"] < today_str():
        return _t("exp_line_expired", q=_fmt_qty(item['qty']),
                  name=item['product'], date=_fmt_date(item['expiry_date']))
    return _t("exp_line_soon", q=_fmt_qty(item['qty']), name=item['product'],
              date=_fmt_date(item['expiry_date']))


def _apply_expiry_note(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    ids = []
    for item in payload["items"]:
        cur = conn.execute(
            "INSERT INTO expiry_items (shop_id, product_name, qty, expiry_date, action_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (shop_id, item["product"], item["qty"], item["expiry_date"], action_id),
        )
        ids.append(cur.lastrowid)
    flagged = [i for i in payload["items"] if i["expiry_date"] <= _soon_iso()]
    saved = _t("exp_saved_head") + "\n" + "\n".join(_expiry_line(i) for i in flagged)
    return saved, "expiry", "expiry note", {"item_ids": ids}


def _soon_iso() -> str:
    return (date.fromisoformat(today_str()) + timedelta(days=7)).isoformat()


def check_expiry_photo(conn: sqlite3.Connection, shop_id: str, findings: list[dict],
                       photo_hash: str | None = None) -> str:
    """Propose storing expiry findings. findings: [{product, qty, expiry_date YYYY-MM-DD}]."""
    ensure_shop(conn, shop_id)
    if not findings:
        return _t("exp_photo_none")
    items = []
    for f in findings:
        name = str(f.get("product", "")).strip()
        try:
            qty = float(f.get("qty", 0))
        except (TypeError, ValueError):
            qty = 0
        exp = str(f.get("expiry_date", "")).strip()
        try:
            date.fromisoformat(exp)
        except ValueError:
            return _t("exp_photo_baddate", name=name or 'that item')
        if not name or qty <= 0:
            return _t("exp_photo_each")
        items.append({"product": name, "qty": qty, "expiry_date": exp})
    flagged = [i for i in items if i["expiry_date"] <= _soon_iso()]
    if not flagged:
        return _t("exp_photo_fine")
    payload = {"items": items, "photo_hash": photo_hash}
    with _dry_run(conn):
        _apply_expiry_note(conn, shop_id, payload, _action_id())
    preview = _t("exp_preview_head") + "\n" + "\n".join(_expiry_line(i) for i in flagged)
    return _propose(conn, shop_id, "expiry_note", payload,
                    preview + _t("exp_save_ask"))


def propose_reminder_offer(conn: sqlite3.Connection, shop_id: str, due_iso: str,
                           summary: str) -> str:
    """Offer a reminder for a mentioned future date (Yes saves it)."""
    from app.reminders import detect_context, offer_preview
    from datetime import datetime as _dt
    due = _dt.fromisoformat(due_iso)
    payload = {"due_at": due_iso, "summary": summary,
               "context": detect_context(conn, shop_id, summary),
               "recurrence": "none"}
    return _propose(conn, shop_id, "reminder_offer", payload,
                    offer_preview(due))


def _apply_reminder_offer(conn: sqlite3.Connection, shop_id: str, payload: dict,
                          action_id: str):
    from app.reminders import confirm_text, save_reminder
    from datetime import datetime as _dt
    due = _dt.fromisoformat(payload["due_at"])
    rid = save_reminder(conn, shop_id, due, payload.get("summary", "Reminder"),
                        payload.get("context") or {},
                        payload.get("recurrence", "none"),
                        record_action=False)
    return (confirm_text(due, payload.get("summary", "Reminder"),
                         payload.get("recurrence", "none")),
            "reminder", f"reminder: {payload.get('summary', 'Reminder')}",
            {"reminder_id": rid})


def _apply_slip(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    moves, created_ids, old_costs = [], [], {}
    for item in payload["items"]:
        if item["product_id"] is None:
            cur = conn.execute(
                "INSERT INTO products (shop_id, name, wholesaler) VALUES (?, ?, ?)",
                (shop_id, item["name"], payload.get("wholesaler") or ""),
            )
            pid, created = cur.lastrowid, True
            created_ids.append(pid)
        else:
            pid, created = item["product_id"], False
        if item.get("cost_cents") is not None:
            old_costs[pid] = conn.execute("SELECT cost_cents FROM products WHERE id = ?",
                                          (pid,)).fetchone()["cost_cents"]
            conn.execute("UPDATE products SET cost_cents = ? WHERE id = ?",
                         (item["cost_cents"], pid))
        conn.execute("UPDATE products SET stock_qty = stock_qty + ? WHERE id = ?",
                     (item["qty"], pid))
        conn.execute(
            "INSERT INTO stock_moves (shop_id, product_id, delta_qty, reason, action_id)"
            " VALUES (?, ?, ?, 'slip', ?)",
            (shop_id, pid, item["qty"], action_id),
        )
        moves.append({"product_id": pid, "qty": item["qty"], "created": created})
    n = len(moves)
    msg = _t("slip_saved_one") if n == 1 else _t("slip_saved_many", n=n)
    return (msg, "slip",
            f"slip {n} items", {"moves": moves, "old_costs": old_costs})


def read_slip_photo(conn: sqlite3.Connection, shop_id: str, items: list[dict],
                    wholesaler: str | None = None) -> str:
    """Propose adding slip lines to stock. items: [{product, qty, cost_cents?}]."""
    ensure_shop(conn, shop_id)
    if not items:
        return _t("slip_none")
    resolved = []
    for item in items:
        name = str(item.get("product", "")).strip()
        try:
            qty = float(item.get("qty", 0))
        except (TypeError, ValueError):
            qty = 0
        if not name or qty <= 0:
            return _t("slip_each")
        prod, ambiguous = _find_products(conn, shop_id, name)
        if prod is None and ambiguous:
            return _which_one(ambiguous)
        cost = item.get("cost_cents")
        try:
            cost = int(cost) if cost is not None else None
        except (TypeError, ValueError):
            cost = None
        resolved.append({"product_id": prod["id"] if prod else None,
                         "name": prod["name"] if prod else name, "qty": qty, "cost_cents": cost})
    payload = {"items": resolved, "wholesaler": (wholesaler or "").strip()}
    with _dry_run(conn):
        _apply_slip(conn, shop_id, payload, _action_id())
        stocks = {}
        for item in resolved:
            pid = conn.execute("SELECT id FROM products WHERE shop_id = ? AND name = ?",
                               (shop_id, item["name"])).fetchone()["id"]
            stocks[item["name"]] = conn.execute("SELECT stock_qty FROM products WHERE id = ?",
                                                (pid,)).fetchone()["stock_qty"]
    lines = [_t("slip_prev_head")]
    for item in resolved:
        line = _t("slip_line", q=_fmt_qty(item['qty']), name=item['name'],
                  s=_fmt_qty(stocks[item['name']]))
        if item["cost_cents"] is not None:
            line += _t("slip_at_cost",
                       c=cents_to_rand(item['cost_cents']))
        if item["product_id"] is None:
            line += _t("slip_new")
        lines.append(line)
    return _propose(conn, shop_id, "read_slip", payload, "\n".join(lines))


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


def get_balance(conn: sqlite3.Connection, shop_id: str, customer: str) -> str:
    ensure_shop(conn, shop_id)
    cust = _get_customer(conn, shop_id, customer or "")
    if cust is None:
        return _t("no_one_named", name=customer)
    bal = balance_cents(conn, shop_id, cust["id"])
    if bal > 0:
        return _t("bal_owes", name=cust['name'], amt=cents_to_rand(bal))
    if bal == 0:
        return _t("bal_clean", name=cust['name'])
    return _t("bal_credit", name=cust['name'], amt=cents_to_rand(-bal))


def list_debtors(conn: sqlite3.Connection, shop_id: str) -> str:
    ensure_shop(conn, shop_id)
    rows = conn.execute("SELECT * FROM customers WHERE shop_id = ? ORDER BY name", (shop_id,)).fetchall()
    debtors = [(r["name"], balance_cents(conn, shop_id, r["id"])) for r in rows]
    debtors = [(n, b) for n, b in debtors if b > 0]
    if not debtors:
        return _t("debt_clean")
    debtors.sort(key=lambda t: -t[1])
    today = today_str()
    lines = [_t("debt_head")]
    for n, b in debtors:
        cust = _get_customer(conn, shop_id, n)
        age = ""
        if cust is not None:
            first = conn.execute(
                "SELECT MIN(substr(created_at, 1, 10)) m FROM credit_entries"
                " WHERE shop_id = ? AND customer_id = ? AND reversed = 0",
                (shop_id, cust["id"])).fetchone()["m"]
            if first:
                days = (date.fromisoformat(today) - date.fromisoformat(first)).days
                if days > 7:
                    age = _t("debt_age", d=days)
        lines.append(_t("debt_line", name=n, amt=cents_to_rand(b), age=age))
    total = sum(b for _, b in debtors)
    return "\n".join(lines) + _t("debt_total", total=cents_to_rand(total))


_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}


def parse_expiry_date(text: str) -> str | None:
    """'Friday', 'tomorrow', 'in 5 days', '2026-11-03' -> ISO date. None if lost."""
    from datetime import timedelta as _td
    word = (text or "").strip().lower()
    today = date.fromisoformat(today_str())
    if word in ("today",):
        return today.isoformat()
    if word in ("tomorrow", "tmr"):
        return (today + _td(days=1)).isoformat()
    m = re.fullmatch(r"in (\d{1,3}) days?", word)
    if m:
        return (today + _td(days=int(m.group(1)))).isoformat()
    if word in _WEEKDAYS:
        delta = (_WEEKDAYS[word] - today.weekday()) % 7 or 7
        return (today + _td(days=delta)).isoformat()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", word)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    return None


def add_staff(conn: sqlite3.Connection, shop_id: str, phone: str,
              name: str) -> str:
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 9:
        return _t("staff_hint")
    if digits == re.sub(r"\D", "", shop_id):
        return _t("staff_own")
    conn.execute("INSERT INTO staff (shop_id, phone, name) VALUES (?, ?, ?)"
                 " ON CONFLICT (shop_id, phone) DO UPDATE SET name = excluded.name",
                 (shop_id, digits, (name or "").strip()[:40] or digits))
    conn.commit()
    return _t("staff_added", name=(name or '').strip() or digits,
                    digits=digits)


def resolve_sender(conn: sqlite3.Connection,
                   sender: str) -> tuple[str, str | None]:
    """Map a sender number to (owner_shop, staff_name_or_None)."""
    row = conn.execute("SELECT shop_id, name FROM staff WHERE phone = ?",
                       (re.sub(r"\D", "", sender or ""),)).fetchone()
    if row is None:
        return sender, None
    return row["shop_id"], row["name"] or row["shop_id"]


def _staff_blocked() -> str | None:
    from app.agent import _STAFF_NAME
    if _STAFF_NAME.get():
        return _t("owners_only")
    return None


def _rec() -> str:
    """Display name to stamp on entries: staff name, or '' for the owner."""
    from app.agent import _STAFF_NAME
    return _STAFF_NAME.get() or ""


def who_recorded_today(conn: sqlite3.Connection, shop_id: str) -> str:
    ensure_shop(conn, shop_id)
    day = today_str()
    rng = "substr(datetime(created_at, '+2 hours'), 1, 10) = ?"
    tallies: dict[str, dict] = {}
    for table, col in (("sales", "total_cents"), ("credit_entries", "amount_cents"),
                       ("payments", "amount_cents"), ("expenses", "amount_cents")):
        for r in conn.execute(
                f"SELECT recorded_by, COUNT(*) n, COALESCE(SUM({col}), 0) s"
                f" FROM {table} WHERE shop_id = ? AND reversed = 0 AND {rng}"
                " GROUP BY recorded_by", (shop_id, day)).fetchall():
            who = r["recorded_by"] or _t("owner_word")
            t = tallies.setdefault(who, {"n": 0, "s": 0})
            t["n"] += r["n"]
            t["s"] += r["s"]
    if not tallies:
        return _t("who_empty")
    lines = [_t("who_head")]
    for who in sorted(tallies):
        t = tallies[who]
        lines.append(_t("who_line", who=who, n=t['n'],
                        amt=cents_to_rand(t['s'])))
    return "\n".join(lines)


def set_expiry(conn: sqlite3.Connection, shop_id: str, product: str,
               when: str) -> str:
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    day = parse_expiry_date(when or "")
    if day is None:
        return _t("set_expiry_ask")
    prod, _ = _find_products(conn, shop_id, product or "")
    qty = prod["stock_qty"] if prod else 0
    name = prod["name"] if prod else (product or "").strip()
    if not name:
        return _t("set_expiry_which")
    conn.execute(
        "INSERT INTO expiry_items (shop_id, product_name, qty, expiry_date, note)"
        " VALUES (?, ?, ?, ?, 'owner')",
        (shop_id, name, qty, day))
    conn.commit()
    return _t("set_expiry_ok", name=name, date=_fmt_date(day))


def set_nudge(conn: sqlite3.Connection, shop_id: str, setting: str) -> str:
    """Change the 19:00 books reminder: 'HH:MM', 'off' or 'on'."""
    blocked = _staff_blocked()
    if blocked:
        return blocked
    import re as _re
    ensure_shop(conn, shop_id)
    word = (setting or "").strip().lower()
    if word == "off":
        conn.execute("INSERT INTO flags (shop_id, key, value) VALUES (?, 'nudge_off', '1')"
                     " ON CONFLICT (shop_id, key) DO UPDATE SET value = '1'",
                     (shop_id,))
        conn.commit()
        return _t("nudge_off_ok")
    if word == "on":
        conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'nudge_off'",
                     (shop_id,))
        conn.commit()
        return _t("nudge_on_ok")
    if not _re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", word):
        return _t("nudge_hint")
    conn.execute("INSERT INTO flags (shop_id, key, value) VALUES (?, 'nudge_time', ?)"
                 " ON CONFLICT (shop_id, key) DO UPDATE SET value = excluded.value",
                 (shop_id, word))
    conn.commit()
    return _t("nudge_at_ok", at=word)


def set_customer_limit(conn: sqlite3.Connection, shop_id: str, customer: str,
                       amount_cents: int) -> str:
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    cust = _get_customer(conn, shop_id, customer or "")
    if cust is None:
        return _t("no_one_named", name=(customer or '').strip())
    try:
        amount = int(amount_cents)
    except (TypeError, ValueError):
        return _t("limit_ask")
    if amount <= 0:
        return _t("limit_ask")
    conn.execute("UPDATE customers SET credit_limit_cents = ? WHERE id = ?",
                 (amount, cust["id"]))
    conn.commit()
    return _t("limit_saved", name=cust['name'],
                    amt=cents_to_rand(amount))


def set_customer_language(conn: sqlite3.Connection, shop_id: str, customer: str,
                          lang_word: str) -> str:
    """'set Sipho language isiXhosa': statements for them use that language."""
    from app.i18n import NAMES, normalize_lang
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    cust = _get_customer(conn, shop_id, customer or "")
    if cust is None:
        return _t("no_one_named", name=(customer or '').strip())
    code = normalize_lang(lang_word)
    if code is None:
        return _t("ag_lang_unknown")
    conn.execute("UPDATE customers SET language = ? WHERE id = ?",
                 (code, cust["id"]))
    conn.commit()
    return _t("ag_cust_lang_ok", name=cust['name'], lang=NAMES[code])


def set_owner_language(conn: sqlite3.Connection, shop_id: str,
                     lang_word: str) -> str:
    """'speak Afrikaans': the owner's saved language for scheduled sends."""
    from app.i18n import CURRENT_LANG, NAMES, normalize_lang
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    code = normalize_lang(lang_word)
    if code is None:
        return _t("ag_lang_unknown")
    conn.execute("UPDATE shops SET language = ? WHERE shop_id = ?",
                 (code, shop_id))
    conn.commit()
    CURRENT_LANG.set(code)
    return _t("ag_lang_ok", lang=NAMES[code])


def statement_lang(conn: sqlite3.Connection, shop_id: str, cust_id: int) -> str:
    """Statement language: the customer's when known, else the owner's."""
    from app.i18n import normalize_lang, owner_lang
    try:
        row = conn.execute("SELECT language FROM customers WHERE id = ?",
                           (cust_id,)).fetchone()
    except Exception:
        row = None
    code = normalize_lang(row["language"]) if row is not None else None
    return code or owner_lang(conn, shop_id)


def _limit_warn(conn: sqlite3.Connection, shop_id: str, cust_id: int,
                new_bal: int) -> str:
    row = conn.execute("SELECT name, credit_limit_cents FROM customers WHERE id = ?",
                       (cust_id,)).fetchone()
    if row is None or row["credit_limit_cents"] is None:
        return ""
    if new_bal > row["credit_limit_cents"]:
        return _t("limit_over", name=row['name'],
                  limit=cents_to_rand(row['credit_limit_cents']))
    return ""


def draft_reminder(conn: sqlite3.Connection, shop_id: str, customer: str) -> str:
    import os as _os

    from app import reports as _reports
    ensure_shop(conn, shop_id)
    cust = _get_customer(conn, shop_id, customer or "")
    if cust is None:
        return _t("no_one_named", name=customer)
    bal = balance_cents(conn, shop_id, cust["id"])
    if bal <= 0:
        return _t("draft_clean", name=cust['name'])
    shop = ensure_shop(conn, shop_id)["name"]
    lang = statement_lang(conn, shop_id, cust["id"])
    with _doc_lang(lang):
        quote = _t("draft_quote", name=cust['name'],
                   amt=cents_to_rand(bal), shop=shop)
        data = _reports.statement_data(conn, shop_id, cust["name"])
    out = _t("draft_head", name=cust['name']) + quote
    base = _os.environ.get("SISI_PUBLIC_URL", "").rstrip("/")
    if data is not None and base:
        _reports.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        _queue_statement(conn, shop_id, data, _reports.shop_brand(conn, shop_id),
                         base, lang)
        out += _t("draft_card")
    return out


def ask_clarify(conn: sqlite3.Connection, shop_id: str, question: str,
                options: list | str | None = None) -> str:
    """Render ONE clarifying question with numbered options (max 3).

    Called by the model instead of guessing between real alternatives.
    The options double as reply buttons via llm.chat_full.
    """
    ensure_shop(conn, shop_id)
    if isinstance(options, str):
        options = [options]
    opts: list[str] = []
    for o in options or []:
        o = str(o).strip()
        if o and o not in opts:
            opts.append(o)
    opts = opts[:3]
    lines = ["❓ " + (question or "").strip()]
    for i, o in enumerate(opts, 1):
        lines.append(f"{i}) {o}")
    if opts:
        lines.append("")
        lines.append(_t("clarify_hint"))
    return "\n".join(lines).strip()


_CATEGORY_ORDER = ["Bread", "Dairy", "Drinks", "Groceries", "Household", "Airtime"]
_CATEGORY_HEAD_KEYS = {"Bread": "cat_bread", "Dairy": "cat_dairy",
                       "Drinks": "cat_drinks", "Groceries": "cat_groceries",
                       "Household": "cat_household", "Airtime": "cat_airtime"}


def _cat_head(cat: str) -> str:
    key = _CATEGORY_HEAD_KEYS.get(cat)
    return _t(key) if key else _t("cat_other", cat=cat)


def _stock_flag(qty: float, reorder: float) -> str:
    if qty <= 0:
        return " ❌"
    if qty <= reorder:
        return " ⚠️"
    return ""


def list_stock(conn: sqlite3.Connection, shop_id: str) -> str:
    shop = ensure_shop(conn, shop_id)
    rows = conn.execute("SELECT * FROM products WHERE shop_id = ? ORDER BY name",
                        (shop_id,)).fetchall()
    head = _t("stock_head", shop=shop['name'])
    if not rows:
        return head + _t("stock_empty")
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["category"] or "Other", []).append(r)
    order = [c for c in _CATEGORY_ORDER if c in groups]
    order += sorted(set(groups) - set(_CATEGORY_ORDER) - {"Other"})
    if "Other" in groups:
        order.append("Other")
    value = sum((r["price_cents"] or 0) * max(0, r["stock_qty"]) for r in rows)
    n = len(rows)
    count = _t("stock_count_one", n=n, v=cents_to_rand(int(value))) if n == 1 \
        else _t("stock_count_many", n=n, v=cents_to_rand(int(value)))
    blocks = [f"{head}\n{count}"]
    for cat in order:
        lines = [_cat_head(cat)]
        for r in groups[cat]:
            hand = _fmt_on_hand(r["stock_qty"], r["pack_name"], r["pack_size"])
            if r["price_cents"] is not None:
                line = _t("stock_line", name=r['name'], hand=hand,
                          price=cents_to_rand(r['price_cents']))
                if r["pack_size"]:
                    line += _t("stock_each")
            else:
                line = _t("stock_line_noprice", name=r['name'], hand=hand)
            lines.append(line + _stock_flag(r["stock_qty"], r["reorder_level"]))
        blocks.append("\n".join(lines))
    low = [r for r in rows if r["stock_qty"] <= r["reorder_level"]]
    if low:
        lines = [_t("low_head")]
        for r in sorted(low, key=lambda r: r["name"]):
            lines.append(_fmt_low_line(r["name"], r["stock_qty"],
                                       r["pack_name"], r["pack_size"]))
        blocks.append("\n".join(lines))
    else:
        blocks.append(_t("low_none"))
    blocks.append(_t("restock_hint"))
    return "\n\n".join(blocks)


def _best_seller_lines(conn: sqlite3.Connection, shop_id: str, days: int) -> list[str]:
    cutoff = ((datetime.now(timezone.utc) - timedelta(days=days))
              .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")
    rows = conn.execute(
        "SELECT product_name, SUM(qty) q, SUM(total_cents) t FROM sales"
        " WHERE shop_id = ? AND reversed = 0 AND created_at >= ?"
        " GROUP BY product_name ORDER BY t DESC LIMIT 5",
        (shop_id, cutoff)).fetchall()
    return [_t("seller_line", i=i, name=r['product_name'],
                     q=_fmt_qty(r['q']), amt=cents_to_rand(r['t']))
            for i, r in enumerate(rows, 1)]


def best_sellers(conn: sqlite3.Connection, shop_id: str, days: int = 7) -> str:
    ensure_shop(conn, shop_id)
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 7
    if days <= 0:
        days = 7
    lines = _best_seller_lines(conn, shop_id, days)
    if not lines:
        return _t("sellers_none", d=days)
    return _t("sellers_head", d=days) + "\n" + "\n".join(lines)


def low_stock(conn: sqlite3.Connection, shop_id: str) -> str:
    ensure_shop(conn, shop_id)
    rows = conn.execute(
        "SELECT * FROM products WHERE shop_id = ? AND stock_qty <= reorder_level ORDER BY name",
        (shop_id,)).fetchall()
    if not rows:
        return _t("low_ok")
    lines = [_t("low_head2")]
    for r in rows:
        lines.append(_fmt_low_line(r["name"], r["stock_qty"],
                                   r["pack_name"], r["pack_size"]))
    return "\n".join(lines)


def restock_list(conn: sqlite3.Connection, shop_id: str) -> str:
    ensure_shop(conn, shop_id)
    rows = conn.execute(
        "SELECT * FROM products WHERE shop_id = ? AND stock_qty <= reorder_level"
        " ORDER BY wholesaler, name",
        (shop_id,)).fetchall()
    if not rows:
        return _t("restock_none")
    groups: dict[str, list[str]] = {}
    for r in rows:
        need = max(1, int(r["reorder_level"] * 2 - r["stock_qty"]))
        have = _fmt_qty(r["stock_qty"])
        if r["pack_size"]:
            packs = math.ceil(need / r["pack_size"])
            word = r["pack_name"] if packs == 1 else _plural_pack(r["pack_name"])
            groups.setdefault(r["wholesaler"] or _t("no_wholesaler"), []).append(
                _t("restock_line_pack", name=r['name'], have=have, p=packs,
                   word=word))
        else:
            groups.setdefault(r["wholesaler"] or _t("no_wholesaler"), []).append(
                _t("restock_line", name=r['name'], have=have, n=need))
    blocks = [_t("restock_head")]
    for w, items in groups.items():
        blocks.append(f"*{w}*\n" + "\n".join(items))
    return "\n\n".join(blocks)


def cash_up(conn: sqlite3.Connection, shop_id: str, day: str | None = None) -> str:
    ensure_shop(conn, shop_id)
    day = day or today_str()
    # created_at is UTC; the shop day is SAST (UTC+2), so shift before comparing.
    day_expr = "substr(datetime(created_at, '+2 hours'), 1, 10)"
    cash = conn.execute(
        "SELECT COALESCE(SUM(total_cents), 0) s, COUNT(*) n FROM sales"
        f" WHERE shop_id = ? AND customer_id IS NULL AND reversed = 0 AND {day_expr} = ?",
        (shop_id, day)).fetchone()
    book = conn.execute(
        "SELECT COALESCE(SUM(total_cents), 0) s FROM sales"
        f" WHERE shop_id = ? AND customer_id IS NOT NULL AND reversed = 0 AND {day_expr} = ?",
        (shop_id, day)).fetchone()["s"]
    pay = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) s FROM payments"
        f" WHERE shop_id = ? AND reversed = 0 AND {day_expr} = ?",
        (shop_id, day)).fetchone()["s"]
    if not cash["s"] and not book and not pay:
        return _t("cash_none", day=day)
    out = (_t("cash_head", day=day) + "\n"
           + _t("cash_body", cash=cents_to_rand(cash['s']), n=cash['n'],
                book=cents_to_rand(book), pay=cents_to_rand(pay),
                drawer=cents_to_rand(cash['s'] + pay)))
    sellers = _best_seller_lines(conn, shop_id, 7)
    if sellers:
        out += "\n\n" + _t("cash_sellers7") + "\n" + "\n".join(sellers)
    return out


COMPLIANCE_TOPICS = {
    "overview": "comp_overview",
    "register": "comp_register",
    "permit": "comp_permit",
    "coa": "comp_coa",
    "fund": "comp_fund",
    "scam": "comp_scam",
}


def compliance_guide(conn: sqlite3.Connection, shop_id: str, topic: str = "overview") -> str:
    ensure_shop(conn, shop_id)
    key = (topic or "overview").strip().lower()
    aliases = {"registration": "register", "permit": "permit", "acceptability": "coa",
               "certificate": "coa", "fund": "fund", "spaza fund": "fund",
               "scam": "scam", "scams": "scam", "overview": "overview"}
    topic = aliases.get(key, "overview")
    return _t(COMPLIANCE_TOPICS.get(topic, "comp_overview"))


def demo_reset(conn: sqlite3.Connection, shop_id: str) -> str:
    """Wipe demo data, run setup, then reseed the 30-day books (demo number)."""
    blocked = _staff_blocked()
    if blocked:
        return blocked
    from seed.seed_demo import wipe_shop
    from seed.seed_random import DEMO_NUMBER
    from app.setup import start_setup
    wipe_shop(conn, shop_id)
    ensure_shop(conn, shop_id)
    return start_setup(conn, shop_id, reseed=(shop_id == DEMO_NUMBER))


def reset_shop(conn: sqlite3.Connection, shop_id: str) -> str:
    """Danger prompt before wiping a real shop. Confirm with 'yes erase everything'."""
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    return _t("reset_ask")


def confirm_reset_shop(conn: sqlite3.Connection, shop_id: str) -> str:
    """Wipe a real shop after confirmation, then run setup. No reseed."""
    blocked = _staff_blocked()
    if blocked:
        return blocked
    from seed.seed_demo import wipe_shop
    from app.setup import start_setup
    wipe_shop(conn, shop_id)
    ensure_shop(conn, shop_id)
    return start_setup(conn, shop_id)


def _remove_shop_files(conn: sqlite3.Connection, shop_id: str, *logos: str) -> None:
    import os as _os
    from app import reports as _reports
    paths = [p for p in logos if p]
    safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
    paths += [str(_reports.LOGOS_DIR / f"{safe}-{v}.png") for v in ("cutout", "badge")]
    paths += [str(_reports.LOGOS_DIR / f"{safe}-staged-{v}.png") for v in ("tile", "orig")]
    for row in conn.execute("SELECT pdf_path, png_path FROM reports WHERE shop_id = ?",
                            (shop_id,)).fetchall():
        paths += [row["pdf_path"], row["png_path"]]
    for p in paths:
        try:
            if p:
                _os.remove(p)
        except OSError:
            pass


def delete_my_data(conn: sqlite3.Connection, shop_id: str) -> str:
    """Erase everything stored for one shop (POPIA). Keeps only an empty shop row."""
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    shop = ensure_shop(conn, shop_id)
    _remove_shop_files(conn, shop_id, shop["logo_path"] if "logo_path" in shop.keys() else "",
                       shop["logo_orig_path"] if "logo_orig_path" in shop.keys() else "")
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
        "task_plans",
        "clarify_context",
    ):
        conn.execute(f"DELETE FROM {table} WHERE shop_id = ?", (shop_id,))
    conn.execute(
        "UPDATE shops SET consent_given_at = NULL, language = 'en', address = '',"
        " phone = '', logo_path = '', logo_orig_path = '' WHERE shop_id = ?",
        (shop_id,),
    )
    conn.commit()
    return _t("deleted")


# ---------------------------------------------------------------------------
# Undo: reverse the last confirmed action exactly (LIFO, seed rows excluded)
# ---------------------------------------------------------------------------


def _undo_credit(conn: sqlite3.Connection, shop_id: str, undo: dict) -> str:
    row = conn.execute(
        "SELECT e.*, c.name FROM credit_entries e JOIN customers c ON c.id = e.customer_id"
        " WHERE e.id = ?", (undo["credit_id"],)).fetchone()
    conn.execute("UPDATE credit_entries SET reversed = 1 WHERE id = ?", (undo["credit_id"],))
    bal = balance_cents(conn, shop_id, row["customer_id"])
    return _t("undo_credit", name=row['name'],
                    amt=cents_to_rand(row['amount_cents']),
                    bal=cents_to_rand(bal))


def _undo_payment(conn: sqlite3.Connection, shop_id: str, undo: dict, action_id: str) -> str:
    row = conn.execute(
        "SELECT p.*, c.name FROM payments p JOIN customers c ON c.id = p.customer_id"
        " WHERE p.id = ?", (undo["payment_id"],)).fetchone()
    conn.execute("UPDATE payments SET reversed = 1 WHERE id = ?", (undo["payment_id"],))
    _reverse_moves(conn, shop_id, action_id)
    bal = balance_cents(conn, shop_id, row["customer_id"])
    return _t("undo_payment", name=row['name'],
                    amt=cents_to_rand(row['amount_cents']),
                    bal=cents_to_rand(bal))


def _undo_expense(conn: sqlite3.Connection, shop_id: str, undo: dict, action_id: str) -> str:
    row = conn.execute("SELECT * FROM expenses WHERE id = ?", (undo["expense_id"],)).fetchone()
    conn.execute("UPDATE expenses SET reversed = 1 WHERE id = ?", (undo["expense_id"],))
    _reverse_moves(conn, shop_id, action_id)
    return _t("undo_expense", amt=cents_to_rand(row['amount_cents']),
                    cat=_cat_name(row['category']).lower())


def _undo_cash_only(conn: sqlite3.Connection, shop_id: str, undo: dict, action_id: str,
                    kind: str) -> str:
    _reverse_moves(conn, shop_id, action_id)
    kind_key = {"drawing": "kind_drawing", "cash count": "kind_count",
                "supplier payment": "kind_supplier"}.get(kind)
    return _t("undo_cashonly",
              kind=_t(kind_key) if kind_key else kind)


ACCOUNTS = {"cash": "Till", "petty": "Petty cash", "bank": "Bank"}
EXPENSE_CATEGORIES = ["transport", "electricity", "rent", "airtime", "wages", "other"]


def _clean_account(account) -> str:
    a = str(account or "cash").strip().lower()
    return a if a in ACCOUNTS else "cash"


def _clean_category(category) -> str:
    c = str(category or "other").strip().lower()
    return c if c in EXPENSE_CATEGORIES else "other"


def account_balance(conn: sqlite3.Connection, shop_id: str, account: str) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) s FROM cash_moves"
        " WHERE shop_id = ? AND account = ? AND reversed = 0",
        (shop_id, account)).fetchone()["s"]


def supplier_balance(conn: sqlite3.Connection, shop_id: str, supplier: str) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) s FROM supplier_moves"
        " WHERE shop_id = ? AND supplier = ? AND reversed = 0",
        (shop_id, (supplier or "").strip())).fetchone()["s"]


def _post_cash(conn: sqlite3.Connection, shop_id: str, account: str, amount: int,
               kind: str, action_id: str, category: str = "", note: str = "",
               ref_type: str = "", ref_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO cash_moves (shop_id, account, amount_cents, kind, category,"
        " note, ref_type, ref_id, action_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (shop_id, account, int(amount), kind, category, note, ref_type, ref_id, action_id),
    )


def _post_supplier(conn: sqlite3.Connection, shop_id: str, supplier: str, amount: int,
                   action_id: str, note: str = "") -> None:
    conn.execute(
        "INSERT INTO supplier_moves (shop_id, supplier, amount_cents, note, action_id)"
        " VALUES (?, ?, ?, ?, ?)",
        (shop_id, supplier.strip(), int(amount), note, action_id),
    )


def _reverse_moves(conn: sqlite3.Connection, shop_id: str, action_id: str) -> None:
    conn.execute("UPDATE cash_moves SET reversed = 1 WHERE shop_id = ? AND action_id = ?",
                 (shop_id, action_id))
    conn.execute("UPDATE supplier_moves SET reversed = 1 WHERE shop_id = ? AND action_id = ?",
                 (shop_id, action_id))


_SAST_DAY = "substr(datetime(created_at, '+2 hours'), 1, 10)"
_SAST_MONTH = "substr(datetime(created_at, '+2 hours'), 1, 7)"


def _period_bounds(period: str) -> tuple[str, str, str]:
    """Returns (title, start_day, end_day) in SAST calendar days."""
    today = today_str()
    p = (period or "today").strip().lower()
    if p in ("week", "7 days", "last 7 days", "this week"):
        start = (date.fromisoformat(today) - timedelta(days=6)).isoformat()
        return _t("sales_title7"), start, today
    if p in ("month", "this month", "last 30 days"):
        return _t("sales_title_month"), today[:7] + "-01", today
    return _t("sales_title_today"), today, today


def sales_summary(conn: sqlite3.Connection, shop_id: str, period: str = "today") -> str:
    ensure_shop(conn, shop_id)
    title, start, end = _period_bounds(period)
    rng = f"{_SAST_DAY} >= ? AND {_SAST_DAY} <= ?"
    cash = conn.execute(
        "SELECT COALESCE(SUM(total_cents), 0) s, COUNT(*) n FROM sales"
        f" WHERE shop_id = ? AND customer_id IS NULL AND reversed = 0 AND {rng}",
        (shop_id, start, end)).fetchone()
    book = conn.execute(
        "SELECT COALESCE(SUM(total_cents), 0) s FROM sales"
        f" WHERE shop_id = ? AND customer_id IS NOT NULL AND reversed = 0 AND {rng}",
        (shop_id, start, end)).fetchone()["s"]
    if not cash["s"] and not book:
        return _t("sales_none", title=title)
    n = cash["n"]
    cnt = _t("sales_1", n=n) if n == 1 else _t("sales_n", n=n)
    return _t("sales_body", title=title, cash=cents_to_rand(cash['s']),
              cnt=cnt, book=cents_to_rand(book),
              total=cents_to_rand(cash['s'] + book))


def _month_cost_of_sales(conn: sqlite3.Connection, shop_id: str, month: str) -> tuple[int, int]:
    """Returns (sales_cents, cogs_cents) for a YYYY-MM month at current unit cost."""
    rows = conn.execute(
        "SELECT s.qty, p.cost_cents, s.total_cents FROM sales s"
        " LEFT JOIN products p ON p.id = s.product_id"
        f" WHERE s.shop_id = ? AND s.reversed = 0 AND {_SAST_MONTH} = ?",
        (shop_id, month)).fetchall()
    sales = sum(r["total_cents"] for r in rows)
    cogs = int(round(sum((r["qty"] or 0) * (r["cost_cents"] or 0) for r in rows)))
    return sales, cogs


def profit_month(conn: sqlite3.Connection, shop_id: str, month: str | None = None) -> str:
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    m = (month or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", m):
        m = today_str()[:7]
    sales, cogs = _month_cost_of_sales(conn, shop_id, m)
    exp = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) s FROM expenses"
        f" WHERE shop_id = ? AND reversed = 0 AND {_SAST_MONTH} = ?",
        (shop_id, m)).fetchone()["s"]
    gross = sales - cogs
    margin = f" ({round(gross * 100 / sales)}%)" if sales else ""
    label = _month_year(int(m[:4]), int(m[5:7]))
    return _t("profit_body", label=label, sales=cents_to_rand(sales),
              cogs=cents_to_rand(cogs), gross=cents_to_rand(gross),
              margin=margin, exp=cents_to_rand(exp),
              net=cents_to_rand(gross - exp))


def _avg_daily_sales(conn: sqlite3.Connection, shop_id: str, product_id: int,
                     start_day: str) -> float:
    s = conn.execute(
        "SELECT COALESCE(SUM(qty), 0) s FROM sales WHERE shop_id = ? AND product_id = ?"
        " AND reversed = 0 AND substr(datetime(created_at, '+2 hours'), 1, 10) >= ?",
        (shop_id, product_id, start_day)).fetchone()["s"]
    return s / 7


def photo_seen(conn: sqlite3.Connection, shop_id: str, digest: str):
    return conn.execute("SELECT * FROM photo_hashes WHERE shop_id = ? AND hash = ?",
                        (shop_id, digest)).fetchone()


def record_photo(conn: sqlite3.Connection, shop_id: str, digest: str, kind: str,
                 note: str = "", *, commit: bool = True) -> None:
    conn.execute("INSERT OR IGNORE INTO photo_hashes (shop_id, hash, kind, note)"
                 " VALUES (?, ?, ?, ?)", (shop_id, digest, kind, note))
    if commit:
        conn.commit()


BOOK_ALIASES = {
    "chap": "chappies", "chappie": "chappies", "chappis": "chappies",
    "brd": "bread", "bred": "bread",
    "stny": "stoney", "ston": "stoney",
    "mlk": "milk", "ammasi": "amasi",
    "egg": "eggs", "mae": "maize", "sug": "sugar",
    "sim": "simba", "chps": "simba",
}


def _resolve_book_item(conn: sqlite3.Connection, shop_id: str, text: str):
    """Resolve shorthand/abbreviated book text to a product (never guess)."""
    q = (text or "").strip().lower()
    q = BOOK_ALIASES.get(q, q)
    return _find_products(conn, shop_id, q)


def _book_short(units: float, name: str) -> str:
    return f"{_fmt_qty(units)}x {name}"


def _resolve_book_line(conn: sqlite3.Connection, shop_id: str, line_no: int, text: str,
                       item: str, qty: float, price_cents: int | None, kind: str,
                       customer: str):
    """Returns (section, entry) with section in cash|credit|payment|flagged."""
    cust = (customer or "").strip()
    if kind == "unreadable":
        return "flagged", {"line_no": line_no, "text": text,
                           "reason": _t("book_reason_unreadable")}
    if kind == "payment":
        if not cust:
            return "flagged", {"line_no": line_no, "text": text,
                               "reason": _t("book_reason_paid_noname")}
        if not price_cents:
            return "flagged", {"line_no": line_no, "text": text,
                               "reason": _t("book_reason_paid_noamt")}
        row = _get_customer(conn, shop_id, cust)
        if row is None:
            return "flagged", {"line_no": line_no, "text": text,
                               "reason": _t("book_reason_noname", name=cust)}
        return "payment", {"line_no": line_no, "customer": row["name"], "amount": price_cents}
    if not (item or "").strip():
        return "flagged", {"line_no": line_no, "text": text,
                           "reason": _t("book_reason_unreadable")}
    prod, ambiguous = _resolve_book_item(conn, shop_id, item)
    if prod is None:
        if ambiguous:
            names = ", ".join(p["name"] for p in ambiguous[:2])
            return "flagged", {"line_no": line_no, "text": text,
                               "reason": _t("book_reason_which", names=names)}
        return "flagged", {"line_no": line_no, "text": text,
                           "reason": _t("book_reason_noshelf")}
    if prod["price_cents"] is None:
        return "flagged", {"line_no": line_no, "text": text,
                           "reason": _t("book_reason_noprice",
                                        name=prod['name'])}
    units = qty if qty > 0 else 1
    list_total = int(round(prod["price_cents"] * units))
    total = price_cents if price_cents else list_total
    entry = {"line_no": line_no, "product_id": prod["id"], "name": prod["name"],
             "units": units, "total": total, "list_total": list_total,
             "short": _book_short(units, prod["name"])}
    if kind == "credit":
        if not cust:
            return "flagged", {"line_no": line_no, "text": text,
                               "reason": _t("book_reason_took_noname")}
        row = _get_customer(conn, shop_id, cust)
        entry["customer"] = row["name"] if row else cust
        entry["new_customer"] = row is None
        return "credit", entry
    return "cash", entry


def _render_book_preview(conn: sqlite3.Connection, shop_id: str, payload: dict) -> str:
    cash = sorted(payload["cash"], key=lambda e: e["line_no"])
    credit = sorted(payload["credit"], key=lambda e: e["line_no"])
    payments = sorted(payload["payments"], key=lambda e: e["line_no"])
    flagged = sorted(payload["flagged"], key=lambda e: e["line_no"])
    blocks = [_t("book_prev_head", date=payload['display_date'])]
    if cash:
        lines = [_t("book_cash_head")]
        for e in cash:
            line = _t("book_cash_line", short=e['short'],
                      total=cents_to_rand(e['total']))
            if e["total"] != e["list_total"]:
                line += _t("book_cash_list",
                           total=cents_to_rand(e['list_total']))
            lines.append(line)
        blocks.append("\n".join(lines))
    credit_lines = []
    for e in credit:
        row = _get_customer(conn, shop_id, e["customer"])
        bal = balance_cents(conn, shop_id, row["id"]) + e["total"] if row else e["total"]
        who = e["customer"]
        if e["new_customer"]:
            who = _t("book_who_new", name=e['customer'])
        credit_lines.append(_t("book_credit_line", who=who, short=e['short'],
                               total=cents_to_rand(e['total']),
                               bal=cents_to_rand(bal)))
    for e in payments:
        row = _get_customer(conn, shop_id, e["customer"])
        bal = balance_cents(conn, shop_id, row["id"]) - e["amount"] if row else -e["amount"]
        credit_lines.append(_t("book_pay_line", name=e['customer'],
                               amt=cents_to_rand(e['amount']),
                               bal=cents_to_rand(bal)))
    if credit_lines:
        blocks.append(_t("book_credit_head") + "\n" + "\n".join(credit_lines))
    if flagged:
        lines = [_t("book_flag_head")]
        for e in flagged:
            lines.append(_t("book_flag_line", n=e['line_no'],
                            text=e['text'][:60], reason=e['reason']))
        blocks.append("\n".join(lines))
    cash_total = sum(e["total"] for e in cash)
    credit_total = sum(e["total"] for e in credit)
    pay_total = sum(e["amount"] for e in payments)
    expected = account_balance(conn, shop_id, "cash") + cash_total + pay_total
    blocks.append(_t("book_totals", cash=cents_to_rand(cash_total),
                        credit=cents_to_rand(credit_total),
                        pay=cents_to_rand(pay_total),
                        exp=cents_to_rand(expected)))
    if payload.get("page_total") is not None:
        written = cash_total + credit_total
        if written == payload["page_total"]:
            blocks.append(_t("book_match", total=cents_to_rand(written)))
        else:
            blocks.append(_t("book_mismatch",
                             page=cents_to_rand(payload['page_total']),
                             lines=cents_to_rand(written)))
    if payload.get("crossed_out") == 1:
        blocks.append(_t("book_crossed_one"))
    elif payload.get("crossed_out"):
        blocks.append(_t("book_crossed_many",
                         n=payload["crossed_out"]))
    blocks.append(_t("book_correct_hint"))
    return "\n\n".join(blocks)


def propose_book_page(conn: sqlite3.Connection, shop_id: str, book: dict,
                      photo_hash: str | None = None) -> str:
    ensure_shop(conn, shop_id)
    book = book or {}
    day = book.get("date") or today_str()
    payload: dict = {"date": day, "display_date": _fmt_date(day),
                     "page_total": book.get("page_total_cents"),
                     "crossed_out": book.get("crossed_out") or 0,
                     "cash": [], "credit": [], "payments": [], "flagged": [],
                     "line_count": 0, "photo_hash": photo_hash}
    for ln in book.get("lines", []) or []:
        payload["line_count"] = max(payload["line_count"], int(ln.get("line_no") or 0))
        section, entry = _resolve_book_line(
            conn, shop_id, int(ln.get("line_no") or 0), str(ln.get("text") or ""),
            str(ln.get("item") or ""), ln.get("qty") or 1, ln.get("price_cents"),
            str(ln.get("kind") or "unreadable"), str(ln.get("customer") or ""))
        entry["kind"] = section
        payload[{"cash": "cash", "credit": "credit", "payment": "payments"}.get(
            section, "flagged")].append(entry)
    if not payload["cash"] and not payload["credit"] and not payload["payments"]:
        out = _t("book_nothing")
        if payload["flagged"]:
            out += "\n" + "\n".join(_t("book_flag_line", n=e['line_no'],
                                        text=e['text'][:60],
                                        reason=e['reason'])
                                     for e in payload["flagged"])
        return out
    return _propose(conn, shop_id, "book_page", payload,
                    _render_book_preview(conn, shop_id, payload))


def _apply_book_page(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    stamp = f"{payload['date']}T12:00:00.000Z"
    sale_ids, credit_ids, payment_ids, new_customers, stock_deltas = [], [], [], [], []
    cash_total = sum(e["total"] for e in payload["cash"])
    credit_total = sum(e["total"] for e in payload["credit"])
    pay_total = sum(e["amount"] for e in payload["payments"])

    def _sell(n, product_id, name, units, total, customer_id=None):
        cur = conn.execute(
            "INSERT INTO sales (shop_id, product_id, product_name, qty, total_cents,"
            " customer_id, action_id, created_at, recorded_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (shop_id, product_id, name, units, total, customer_id, f"{action_id}-s{n}",
             stamp, _rec()))
        sale_ids.append(cur.lastrowid)
        conn.execute("UPDATE products SET stock_qty = stock_qty - ? WHERE id = ?",
                     (units, product_id))
        conn.execute("INSERT INTO stock_moves (shop_id, product_id, delta_qty, reason,"
                     " action_id, created_at) VALUES (?, ?, ?, 'book', ?, ?)",
                     (shop_id, product_id, -units, action_id, stamp))
        stock_deltas.append({"product_id": product_id, "qty": units})

    for i, e in enumerate(payload["cash"]):
        _sell(i, e["product_id"], e["name"], e["units"], e["total"])
    for i, e in enumerate(payload["credit"]):
        cust = _get_customer(conn, shop_id, e["customer"])
        if cust is None:
            cur = conn.execute("INSERT INTO customers (shop_id, name, created_at)"
                               " VALUES (?, ?, ?)", (shop_id, e["customer"], stamp))
            cust_id, created = cur.lastrowid, True
        else:
            cust_id, created = cust["id"], False
        _sell(f"c{i}", e["product_id"], e["name"], e["units"], e["total"], cust_id)
        cur = conn.execute(
            "INSERT INTO credit_entries (shop_id, customer_id, amount_cents, items_text,"
            " action_id, created_at, recorded_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (shop_id, cust_id, e["total"], e["short"], f"{action_id}-c{i}", stamp,
             _rec()))
        credit_ids.append(cur.lastrowid)
        if created:
            new_customers.append(cust_id)
    for i, e in enumerate(payload["payments"]):
        cust = _get_customer(conn, shop_id, e["customer"])
        cur = conn.execute(
            "INSERT INTO payments (shop_id, customer_id, amount_cents, action_id, created_at,"
            " recorded_by) VALUES (?, ?, ?, ?, ?, ?)",
            (shop_id, cust["id"], e["amount"], f"{action_id}-p{i}", stamp, _rec()))
        payment_ids.append(cur.lastrowid)
    if cash_total:
        _post_cash(conn, shop_id, "cash", cash_total, "sale", action_id,
                   note=f"book {payload['date']}")
        conn.execute("UPDATE cash_moves SET created_at = ? WHERE action_id = ? AND kind = 'sale'",
                     (stamp, action_id))
    if pay_total:
        _post_cash(conn, shop_id, "cash", pay_total, "credit_payment", action_id,
                   note=f"book {payload['date']}")
        conn.execute("UPDATE cash_moves SET created_at = ? WHERE action_id = ?"
                     " AND kind = 'credit_payment'", (stamp, action_id))
    parts = []
    if cash_total:
        parts.append(_t("book_part_cash",
                        amt=cents_to_rand(cash_total)))
    if credit_total:
        parts.append(_t("book_part_credit",
                        amt=cents_to_rand(credit_total)))
    if pay_total:
        parts.append(_t("book_part_pay",
                        amt=cents_to_rand(pay_total)))
    msg = (_t("book_saved", parts=" · ".join(parts)) + "\n\n"
           + low_stock(conn, shop_id) + "\n\n" + cash_up(conn, shop_id, payload["date"]))
    return (msg, "book", f"book page {payload['date']}",
            {"sale_ids": sale_ids, "credit_ids": credit_ids, "payment_ids": payment_ids,
             "new_customers": new_customers, "stock": stock_deltas,
             "display_date": payload["display_date"]})


def _undo_book(conn: sqlite3.Connection, shop_id: str, undo: dict, action_id: str) -> str:
    for sid in undo.get("sale_ids", []):
        conn.execute("UPDATE sales SET reversed = 1 WHERE id = ?", (sid,))
    for cid in undo.get("credit_ids", []):
        conn.execute("UPDATE credit_entries SET reversed = 1 WHERE id = ?", (cid,))
    for pid in undo.get("payment_ids", []):
        conn.execute("UPDATE payments SET reversed = 1 WHERE id = ?", (pid,))
    for d in undo.get("stock", []):
        _restore_stock(conn, shop_id, d["product_id"], d["qty"], action_id)
    for cust_id in undo.get("new_customers", []):
        conn.execute("DELETE FROM customers WHERE id = ?", (cust_id,))
    _reverse_moves(conn, shop_id, action_id)
    return _t("undo_book", date=undo.get('display_date', 'that day'))


def _parse_correction(rest: str):
    """'Sipho paid 40' -> payment; '4 bread Sipho credit' -> sale parts."""
    text = (rest or "").strip()
    m = re.match(r"(.+?)\s+paid\s+r?(\d+(?:\.\d{1,2})?)\s*$", text, re.IGNORECASE)
    if m:
        return {"kind": "payment", "customer": m.group(1).strip(),
                "amount": int(round(float(m.group(2)) * 100))}
    toks = text.split()
    if not toks:
        return None
    try:
        qty = float(toks[0])
        toks = toks[1:]
    except ValueError:
        qty = 1
    if not toks:
        return None
    kind = None
    if toks[-1].lower() in ("cash", "credit"):
        kind = toks[-1].lower()
        toks = toks[:-1]
    return {"kind": kind, "qty": qty if qty > 0 else 1, "words": toks}


def correct_book_line(conn: sqlite3.Connection, shop_id: str, line_no: int, rest: str) -> str:
    pend = get_pending(conn, shop_id)
    if pend is None or pend["action_type"] != "book_page":
        return _t("book_no_preview")
    payload = json.loads(pend["payload_json"])
    if line_no < 1 or line_no > (payload.get("line_count") or 0):
        return _t("book_line_missing", n=line_no)
    parsed = _parse_correction(rest)
    if parsed is None:
        return _t("book_bad_correction")
    shop = ensure_shop(conn, shop_id)
    if parsed["kind"] == "payment":
        section, entry = _resolve_book_line(conn, shop_id, line_no, rest, "",
                                            1, parsed["amount"], "payment",
                                            parsed["customer"])
    else:
        words = parsed["words"]
        prod, ambiguous = _resolve_book_item(conn, shop_id, " ".join(words))
        customer = ""
        if prod is None and not ambiguous and len(words) > 1:
            customer = words[-1]
            prod, ambiguous = _resolve_book_item(conn, shop_id, " ".join(words[:-1]))
        kind = parsed["kind"] or ("credit" if customer else "cash")
        if prod is None and not ambiguous:
            section, entry = "flagged", {"line_no": line_no, "text": rest,
                                         "reason": "not on shelf list"}
        elif prod is None:
            names = ", ".join(p["name"] for p in ambiguous[:2])
            section, entry = "flagged", {"line_no": line_no, "text": rest,
                                         "reason": _t("book_reason_which", names=names)}
        elif prod["price_cents"] is None:
            section, entry = "flagged", {"line_no": line_no, "text": rest,
                                         "reason": _t("book_reason_noprice",
                                                     name=prod['name'])}
        else:
            total = int(round(prod["price_cents"] * parsed["qty"]))
            entry = {"line_no": line_no, "product_id": prod["id"], "name": prod["name"],
                     "units": parsed["qty"], "total": total, "list_total": total,
                     "short": _book_short(parsed["qty"], prod["name"])}
            if kind == "credit":
                if not customer:
                    return _t("book_which_cust")
                row = _get_customer(conn, shop_id, customer)
                entry["customer"] = row["name"] if row else customer
                entry["new_customer"] = row is None
                section = "credit"
            else:
                section = "cash"
    entry["kind"] = section
    for key in ("cash", "credit", "payments", "flagged"):
        payload[key] = [e for e in payload[key] if e["line_no"] != line_no]
    payload[{"cash": "cash", "credit": "credit", "payment": "payments"}.get(
        section, "flagged")].append(entry)
    preview = _render_book_preview(conn, shop_id, payload)
    conn.execute("UPDATE pending_actions SET payload_json = ?, preview_text = ? WHERE id = ?",
                 (json.dumps(payload), preview, pend["id"]))
    conn.commit()
    return preview


RECEIPT_EXPENSE_WORDS = {
    "electricity": "electricity", "elec": "electricity", "prepaid": "electricity",
    "power": "electricity", "eskom": "electricity",
    "transport": "transport", "taxi": "transport", "fuel": "transport",
    "petrol": "transport", "diesel": "transport",
    "rent": "rent", "wage": "wages", "wages": "wages", "salary": "wages",
    "staff": "wages",
}

_MONTH_NAMES = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}
_MONTH_NAMES.update({m[:3]: i for m, i in list(_MONTH_NAMES.items())})
# isiZulu, Afrikaans and isiXhosa month names (must match i18n.MON_FULL).
_MONTH_NAMES.update({
    "ujanuwari": 1, "ufebruwari": 2, "umashi": 3, "u-ephreli": 4,
    "uapreli": 4, "uephreli": 4, "umeyi": 5, "ujuni": 6, "ujulayi": 7,
    "u-agasti": 8, "uagasti": 8, "usepthemba": 9, "u-okthoba": 10,
    "uokthoba": 10, "unovemba": 11, "udisemba": 12,
    "januarie": 1, "februarie": 2, "maart": 3, "april": 4, "mei": 5,
    "junie": 6, "julie": 7, "augustus": 8, "oktober": 10, "desember": 12,
    "umatshi": 3, "u-epreli": 4, "uepreli": 4, "useptemba": 9,
    "u-oktobha": 10, "uoktobha": 10,
    # prefix-less forms users often type
    "januwari": 1, "februwari": 2, "mashi": 3, "matshi": 3,
    "meyi": 5, "juni": 6, "julayi": 7, "agasti": 8, "septhemba": 9,
    "septemba": 9, "okthoba": 10, "oktobha": 10, "novemba": 11,
    "disemba": 12,
})


def parse_month_name(text: str) -> str | None:
    """'September'/'sep' -> most recent YYYY-MM by that name."""
    m = _MONTH_NAMES.get((text or "").strip().lower())
    if not m:
        return None
    year, cur = int(today_str()[:4]), int(today_str()[5:7])
    return f"{year if m <= cur else year - 1}-{m:02d}"


def _pos_num(val):
    try:
        val = float(val)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def _receipt_payment_account(payment: str) -> tuple[str, bool]:
    p = (payment or "").lower()
    if "cash" in p:
        return "cash", False
    if "card" in p or "bank" in p or "eft" in p:
        return "bank", False
    if "account" in p or "credit" in p:
        return "supplier", False
    return "cash", True


def _resolve_receipt_line(conn: sqlite3.Connection, shop_id: str, ln: dict):
    """Returns (section, entry): stock|expense|flagged. Unknown stock becomes [new]."""
    line_no, text = int(ln.get("line_no") or 0), str(ln.get("text") or "")
    item = str(ln.get("item") or "").strip()
    if not item:
        return "flagged", {"line_no": line_no, "text": text,
                           "reason": _t("book_reason_unreadable")}
    prod, ambiguous = _resolve_book_item(conn, shop_id, item)
    if prod is None and not ambiguous:
        for word, cat in RECEIPT_EXPENSE_WORDS.items():
            if word in item.lower():
                total = ln.get("line_total_cents")
                if total is None:
                    return "flagged", {"line_no": line_no, "text": text,
                                       "reason": _t("rcpt_reason_noamt")}
                return "expense", {"line_no": line_no, "category": cat, "total": total}
        return "stock", _receipt_stock_entry(None, item, ln, line_no, text)
    if prod is None:
        names = ", ".join(p["name"] for p in ambiguous[:2])
        return "flagged", {"line_no": line_no, "text": text,
                           "reason": _t("book_reason_which", names=names)}
    return "stock", _receipt_stock_entry(prod, prod["name"], ln, line_no, text)


def _receipt_stock_entry(prod, name: str, ln: dict, line_no: int, text: str) -> dict:
    units = _pos_num(ln.get("qty_units"))
    packs = _pos_num(ln.get("packs"))
    size = _pos_num(ln.get("pack_size")) or (prod["pack_size"] if prod else None)
    pack_word = None
    if units is None and packs and size:
        units = packs * size
        word = (prod["pack_name"] if prod and prod["pack_name"] else "pack")
        pack_word = f"{_fmt_qty(packs)} {word if packs == 1 else _plural_pack(word)}"
    if units is None and packs and not size:
        return {"line_no": line_no, "text": text,
                "reason": _t("rcpt_reason_packsize"), "unresolved": True}
    if units is None:
        lt = ln.get("line_total_cents")
        uc = ln.get("unit_cost_cents")
        if lt and uc:
            units = round(lt / uc, 2)
    if units is None:
        return {"line_no": line_no, "text": text,
                "reason": _t("rcpt_reason_noqty"), "unresolved": True}
    uc = ln.get("unit_cost_cents")
    if uc is None and ln.get("line_total_cents"):
        uc = round(ln["line_total_cents"] / units, 2)
    return {"line_no": line_no, "product_id": prod["id"] if prod else None,
            "name": name, "units": units, "unit_cost": uc, "pack_word": pack_word,
            "new": prod is None,
            "total": int(round((uc or 0) * units)) if uc is not None else None}


def _receipt_avg_cost(conn: sqlite3.Connection, shop_id: str, entry: dict):
    """Weighted-average unit cost after this receipt line (None when unknown)."""
    if entry.get("unresolved") or entry.get("unit_cost") is None:
        return None
    if entry.get("new"):
        return entry["unit_cost"]
    row = conn.execute("SELECT stock_qty, cost_cents FROM products WHERE id = ?",
                       (entry["product_id"],)).fetchone()
    if row is None or row["cost_cents"] is None or row["stock_qty"] <= 0:
        return entry["unit_cost"]
    return ((row["stock_qty"] * row["cost_cents"] + entry["units"] * entry["unit_cost"])
            / (row["stock_qty"] + entry["units"]))


def _render_receipt_preview(conn: sqlite3.Connection, shop_id: str, payload: dict) -> str:
    stock = sorted(payload["stock"], key=lambda e: e["line_no"])
    expenses = sorted(payload["expenses"], key=lambda e: e["line_no"])
    flagged = sorted(payload["flagged"], key=lambda e: e["line_no"])
    blocks = [_t("rcpt_prev_head", sup=payload['supplier'])]
    lines = []
    for e in stock:
        if e.get("unresolved"):
            flagged.append({"line_no": e["line_no"], "text": e.get("text", ""),
                            "reason": e.get("reason", "")})
            continue
        bit = _t("rcpt_stock_line", q=_fmt_qty(e['units']), name=e['name'])
        if e.get("pack_word"):
            bit += f" ({e['pack_word']})"
        if e.get("new"):
            bit += _t("rcpt_new")
        avg = _receipt_avg_cost(conn, shop_id, e)
        if avg is None:
            bit += _t("rcpt_avg_unknown")
        else:
            bit += _t("rcpt_avg", avg=cents_to_rand(int(round(avg))))
            if not e.get("new"):
                old = conn.execute("SELECT cost_cents FROM products WHERE id = ?",
                                   (e["product_id"],)).fetchone()["cost_cents"]
                if old is not None and int(round(avg)) != int(round(old)):
                    bit += _t("rcpt_was",
                              old=cents_to_rand(int(round(old))))
        have = 0 if e.get("new") else conn.execute(
            "SELECT stock_qty FROM products WHERE id = ?", (e["product_id"],)).fetchone()["stock_qty"]
        bit += _t("rcpt_stock_to", a=_fmt_qty(have),
                        b=_fmt_qty(have + e['units']))
        lines.append(bit)
    for e in expenses:
        lines.append(_t("rcpt_exp_line", cat=_cat_name(e['category']),
                        total=cents_to_rand(e['total'])))
    if lines:
        blocks.append("\n".join(lines))
    if flagged:
        blocks.append(_t("rcpt_flag_head") + "\n" + "\n".join(
            _t("book_flag_line", n=e['line_no'], text=e.get('text', '')[:60],
               reason=e['reason'])
            for e in sorted(flagged, key=lambda e: e["line_no"])))
    computed = sum(e["total"] for e in stock if not e.get("unresolved")
                   and e["total"] is not None)
    computed += sum(e["total"] for e in expenses)
    tail = []
    if payload.get("total") is not None:
        if computed == payload["total"]:
            tail.append(_t("rcpt_total_ok", total=cents_to_rand(computed)))
        else:
            tail.append(_t("rcpt_total_bad", lines=cents_to_rand(computed),
                           slip=cents_to_rand(payload['total'])))
    if payload.get("vat") is not None:
        tail.append(_t("rcpt_vat", vat=cents_to_rand(payload['vat'])))
    acct = payload["account"]
    if acct == "supplier":
        label = _t("rcpt_sup_acct", sup=payload["supplier"])
    else:
        label = _acct_name(acct)
    tail.append(_t("rcpt_paid_from", label=label))
    if payload.get("account_guessed"):
        tail[-1] += _t("rcpt_guess")
    blocks.append("\n".join(tail))
    blocks.append(_t("rcpt_correct_hint"))
    return "\n\n".join(blocks)


def propose_receipt(conn: sqlite3.Connection, shop_id: str, receipt: dict,
                    photo_hash: str | None = None) -> str:
    ensure_shop(conn, shop_id)
    receipt = receipt or {}
    supplier = str(receipt.get("supplier") or "").strip() or "Wholesaler"
    receipt_no = str(receipt.get("receipt_no") or "").strip()
    total = receipt.get("total_cents")
    if receipt_no:
        dup = conn.execute(
            "SELECT * FROM receipts WHERE shop_id = ? AND supplier = ? AND receipt_no = ?"
            " AND reversed = 0", (shop_id, supplier, receipt_no)).fetchone()
        if dup is not None:
            return _t("rcpt_dup", sup=supplier, no=receipt_no,
                      total=cents_to_rand(total or 0))
    account, guessed = _receipt_payment_account(str(receipt.get("payment") or ""))
    payload: dict = {"supplier": supplier, "date": receipt.get("date") or "",
                     "receipt_no": receipt_no, "total": total,
                     "vat": receipt.get("vat_cents"), "account": account,
                     "account_guessed": guessed, "stock": [], "expenses": [],
                     "flagged": [], "line_count": 0, "photo_hash": photo_hash}
    for ln in receipt.get("lines", []) or []:
        payload["line_count"] = max(payload["line_count"], int(ln.get("line_no") or 0))
        section, entry = _resolve_receipt_line(conn, shop_id, ln)
        payload[{"stock": "stock", "expense": "expenses"}.get(section, "flagged")].append(entry)
    usable = [e for e in payload["stock"] if not e.get("unresolved")] + payload["expenses"]
    if not usable:
        out = _t("rcpt_nothing")
        if payload["flagged"]:
            out += "\n" + "\n".join(_t("book_flag_line", n=e['line_no'],
                                        text=e.get('text', '')[:60],
                                        reason=e['reason'])
                                     for e in payload["flagged"])
        return out
    return _propose(conn, shop_id, "receipt", payload,
                    _render_receipt_preview(conn, shop_id, payload))


def _apply_receipt(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    moves, expense_ids, increases = [], [], []
    for e in payload["stock"]:
        if e.get("unresolved"):
            continue
        avg = _receipt_avg_cost(conn, shop_id, e) if e.get("unit_cost") is not None else None
        if e.get("new"):
            cur = conn.execute(
                "INSERT INTO products (shop_id, name, cost_cents, stock_qty)"
                " VALUES (?, ?, ?, ?)",
                (shop_id, e["name"], avg, e["units"]))
            pid, created, old_cost = cur.lastrowid, True, None
        else:
            pid, created = e["product_id"], False
            old_cost = conn.execute("SELECT cost_cents FROM products WHERE id = ?",
                                    (pid,)).fetchone()["cost_cents"]
            if avg is not None:
                conn.execute("UPDATE products SET cost_cents = ? WHERE id = ?",
                             (avg, pid))
        conn.execute("UPDATE products SET stock_qty = stock_qty + ? WHERE id = ?",
                     (e["units"], pid))
        conn.execute("INSERT INTO stock_moves (shop_id, product_id, delta_qty, reason,"
                     " action_id, created_at) VALUES (?, ?, ?, 'receipt', ?, ?)",
                     (shop_id, pid, e["units"], action_id, now_iso()))
        moves.append({"product_id": pid, "qty": e["units"], "created": created,
                      "old_cost": old_cost})
        if old_cost is not None and avg is not None and avg > old_cost:
            increases.append({"product_id": pid, "name": e["name"],
                              "old": old_cost, "new": avg})
    for e in payload["expenses"]:
        cur = conn.execute(
            "INSERT INTO expenses (shop_id, category, amount_cents, account, note, action_id,"
            " recorded_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (shop_id, e["category"], e["total"], payload["account"],
             f"receipt {payload['supplier']}", f"{action_id}-e{e['line_no']}", _rec()))
        expense_ids.append(cur.lastrowid)
    total = sum(e["total"] for e in payload["stock"]
                if not e.get("unresolved") and e["total"] is not None)
    total += sum(e["total"] for e in payload["expenses"])
    acct = payload["account"]
    if acct == "supplier":
        _post_supplier(conn, shop_id, payload["supplier"], total, action_id,
                       note=f"receipt {payload['receipt_no']}")
        money_line = _t("rcpt_owed", sup=payload['supplier'],
                        total=cents_to_rand(total))
    else:
        _post_cash(conn, shop_id, acct, -total, "purchase", action_id,
                   note=f"receipt {payload['supplier']}")
        money_line = _t("rcpt_acct_left", acct=_acct_name(acct),
                        left=cents_to_rand(account_balance(conn, shop_id,
                                                           acct)))
    cur = conn.execute(
        "INSERT INTO receipts (shop_id, supplier, date, receipt_no, total_cents, vat_cents,"
        " account, image_hash, data_json, action_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (shop_id, payload["supplier"], payload.get("date") or "", payload["receipt_no"],
         total, payload.get("vat"), acct, payload.get("photo_hash") or "",
         json.dumps({"lines": payload["stock"] + payload["expenses"]}), action_id))
    n_stock = len([e for e in payload["stock"] if not e.get("unresolved")])
    n_exp = len(payload["expenses"])
    bits = []
    if n_stock:
        bits.append(_t("rcpt_bit_stock", n=n_stock))
    if n_exp:
        bits.append(_t("rcpt_bit_exp", n=n_exp))
    msg = _t("rcpt_saved", sup=payload['supplier'],
             total=cents_to_rand(total), bits=', '.join(bits),
             money=money_line)
    return (msg, "receipt", f"receipt {payload['supplier']} {cents_to_rand(total)}",
            {"moves": moves, "expense_ids": expense_ids, "receipt_id": cur.lastrowid,
             "supplier": payload["supplier"], "cost_increases": increases})


def _undo_receipt(conn: sqlite3.Connection, shop_id: str, undo: dict, action_id: str) -> str:
    for m in undo.get("moves", []):
        if m.get("created"):
            conn.execute("DELETE FROM products WHERE id = ?", (m["product_id"],))
        else:
            _restore_stock(conn, shop_id, m["product_id"], -m["qty"], action_id)
            conn.execute("UPDATE products SET cost_cents = ? WHERE id = ?",
                         (m.get("old_cost"), m["product_id"]))
    for eid in undo.get("expense_ids", []):
        conn.execute("UPDATE expenses SET reversed = 1 WHERE id = ?", (eid,))
    conn.execute("UPDATE receipts SET reversed = 1 WHERE id = ?", (undo["receipt_id"],))
    _reverse_moves(conn, shop_id, action_id)
    return _t("undo_receipt", sup=undo.get('supplier', 'supplier'))


def correct_receipt_line(conn: sqlite3.Connection, shop_id: str, line_no: int, rest: str) -> str:
    pend = get_pending(conn, shop_id)
    if pend is None or pend["action_type"] != "receipt":
        return _t("rcpt_no_preview")
    payload = json.loads(pend["payload_json"])
    if line_no < 1 or line_no > (payload.get("line_count") or 0):
        return _t("rcpt_line_missing", n=line_no)
    toks = (rest or "").split()
    try:
        qty = float(toks[0])
        toks = toks[1:]
    except (ValueError, IndexError):
        return _t("rcpt_say_units")
    if qty <= 0 or not toks:
        return _t("rcpt_say_units")
    cost = None
    if len(toks) >= 2:
        m = re.fullmatch(r"@?r?(\d+(?:\.\d{1,2})?)", toks[-1])
        if m:
            cost = int(round(float(m.group(1)) * 100))
            toks = toks[:-1]
    if not toks:
        return _t("rcpt_say_units")
    shop = ensure_shop(conn, shop_id)
    prod, ambiguous = _resolve_book_item(conn, shop_id, " ".join(toks))
    if prod is None and ambiguous:
        names = ", ".join(p["name"] for p in ambiguous[:2])
        entry: dict = {"line_no": line_no, "text": rest, "reason": f"which one: {names}?",
                       "unresolved": True}
    else:
        entry = {"line_no": line_no, "product_id": prod["id"] if prod else None,
                 "name": prod["name"] if prod else " ".join(toks).strip().title(),
                 "units": qty, "unit_cost": cost, "pack_word": None,
                 "new": prod is None,
                 "total": int(round(cost * qty)) if cost is not None else None}
    for key in ("stock", "expenses", "flagged"):
        payload[key] = [e for e in payload[key] if e["line_no"] != line_no]
    if entry.get("unresolved"):
        payload["flagged"].append({k: entry[k] for k in ("line_no", "text", "reason")})
    else:
        payload["stock"].append(entry)
    preview = _render_receipt_preview(conn, shop_id, payload)
    conn.execute("UPDATE pending_actions SET payload_json = ?, preview_text = ? WHERE id = ?",
                 (json.dumps(payload), preview, pend["id"]))
    conn.commit()
    return preview


def correct_receipt_account(conn: sqlite3.Connection, shop_id: str, account: str) -> str:
    pend = get_pending(conn, shop_id)
    if pend is None or pend["action_type"] != "receipt":
        return _t("rcpt_no_preview")
    shop = ensure_shop(conn, shop_id)
    payload = json.loads(pend["payload_json"])
    payload["account"] = account
    payload["account_guessed"] = False
    preview = _render_receipt_preview(conn, shop_id, payload)
    conn.execute("UPDATE pending_actions SET payload_json = ?, preview_text = ? WHERE id = ?",
                 (json.dumps(payload), preview, pend["id"]))
    conn.commit()
    return preview


def list_receipts(conn: sqlite3.Connection, shop_id: str, month: str | None = None) -> str:
    ensure_shop(conn, shop_id)
    m = (month or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}", m):
        pass
    elif m:
        m = parse_month_name(m) or today_str()[:7]
    else:
        m = today_str()[:7]
    rows = conn.execute(
        "SELECT * FROM receipts WHERE shop_id = ? AND reversed = 0"
        f" AND {_SAST_MONTH} = ? ORDER BY created_at", (shop_id, m)).fetchall()
    label = _month_year(int(m[:4]), int(m[5:7]))
    if not rows:
        return _t("rcpt_list_none", label=label)
    lines = [_t("rcpt_list_head", label=label)]
    for r in rows:
        no = f" {r['receipt_no']}" if r["receipt_no"] else ""
        lines.append(_t("rcpt_list_line", date=_fmt_date(r['created_at'][:10]),
                       sup=r['supplier'], no=no,
                       total=cents_to_rand(r['total_cents'] or 0),
                       acct=_rcpt_short(r['account'])))
    lines.append(_t("rcpt_list_total",
                    total=cents_to_rand(sum(r['total_cents'] or 0 for r in rows))))
    return "\n".join(lines)


def month_end_report(conn: sqlite3.Connection, shop_id: str,
                    month: str | None = None) -> str:
    """Generate the month-end PDF + PNG card and queue them as WhatsApp media."""
    blocked = _staff_blocked()
    if blocked:
        return blocked
    import os as _os

    from app import reports as _reports

    from app.agent import queue_media as _queue
    ensure_shop(conn, shop_id)
    m = (month or "").strip()
    if m and not re.fullmatch(r"\d{4}-\d{2}", m):
        m = parse_month_name(m) or ""
    text, media = _reports.build_month_end(
        conn, shop_id, m or None, _os.environ.get("SISI_PUBLIC_URL", ""))
    _queue(shop_id, media)
    return text


def set_shop_name(conn: sqlite3.Connection, shop_id: str, name: str) -> str:
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    name = (name or "").strip()
    if not name:
        return _t("shop_ask_name")
    conn.execute("UPDATE shops SET name = ? WHERE shop_id = ?", (name[:60], shop_id))
    conn.commit()
    return _t("shop_name_ok", name=name[:60])


def set_shop_address(conn: sqlite3.Connection, shop_id: str, address: str) -> str:
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    conn.execute("UPDATE shops SET address = ? WHERE shop_id = ?",
                 ((address or "").strip()[:120], shop_id))
    conn.commit()
    return _t("shop_addr_ok")


def set_shop_phone(conn: sqlite3.Connection, shop_id: str, phone: str) -> str:
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    conn.execute("UPDATE shops SET phone = ? WHERE shop_id = ?",
                 ((phone or "").strip()[:30], shop_id))
    conn.commit()
    return _t("shop_phone_ok")


def show_shop_profile(conn: sqlite3.Connection, shop_id: str) -> str:
    blocked = _staff_blocked()
    if blocked:
        return blocked
    import os as _os

    from app import reports as _reports

    from app.agent import queue_media as _queue
    brand = _reports.shop_brand(conn, shop_id)
    lines = [_t("profile_head"), _t("profile_name", name=brand['name'])]
    if brand["address"]:
        lines.append(f"• {brand['address']}")
    if brand["phone"]:
        lines.append(f"• {brand['phone']}")
    lines.append(_t("profile_logo_yes") if brand["logo"] else _t("profile_logo_no"))
    if not brand["logo"]:
        lines.append(_t("profile_logo_hint"))
    _reports.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
    card = str(_reports.REPORTS_DIR / f"{safe}-profile.png")
    _reports.render_shop_card(brand, card)
    token = _reports.store_card(conn, shop_id, "profile", card)
    base = _os.environ.get("SISI_PUBLIC_URL", "").rstrip("/")
    if base:
        _queue(shop_id, [{"kind": "image",
                          "url": f"{base}/media/{token}?f=png",
                          "caption": _t("logo_cap_shop"), "filename": ""}])
        lines.append(_t("profile_card_below"))
    return "\n".join(lines)


def save_shop_logo(conn: sqlite3.Connection, shop_id: str, image: bytes) -> str:
    import os as _os

    from app import logo as _logo
    from app import reports as _reports

    from app.agent import queue_media as _queue
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    try:
        tile = _logo.render_tile(image or b"")
    except Exception:
        return _t("logo_not_photo")
    _reports.ensure_dirs()
    safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
    orig_path = str(_reports.LOGOS_DIR / f"{safe}-orig.png")
    tile_path = str(_reports.LOGOS_DIR / f"{safe}-tile.png")
    with open(orig_path, "wb") as f:
        f.write(image or b"")
    with open(tile_path, "wb") as f:
        f.write(tile)
    _reports.logo_put(shop_id, tile)
    conn.execute("UPDATE shops SET logo_path = ?, logo_orig_path = ? WHERE shop_id = ?",
                 (tile_path, orig_path, shop_id))
    conn.execute("INSERT INTO flags (shop_id, key, value) VALUES (?, 'logo-review', '1')"
                 " ON CONFLICT (shop_id, key) DO UPDATE SET value = '1'",
                 (shop_id,))
    conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'logo-pending'",
                 (shop_id,))
    conn.commit()
    brand = _reports.shop_brand(conn, shop_id)
    out = _t("logo_saved", note=_t("logo_tile_note"))
    base = _os.environ.get("SISI_PUBLIC_URL", "").rstrip("/")
    if base:
        card = str(_reports.REPORTS_DIR / f"{safe}-profile.png")
        with _doc_lang(_owner_lang(conn, shop_id)):
            _reports.render_shop_card(brand, card)
        token = _reports.store_card(conn, shop_id, "profile", card)
        _queue(shop_id, [{"kind": "image", "url": f"{base}/media/{token}?f=png",
                          "caption": _t("logo_cap_new"), "filename": ""}])
        out += _t("logo_preview_below")
    return out


def _staged_logo_paths(shop_id: str) -> tuple[str, str]:
    """Staged (not yet live) tile + original paths for a shop."""
    from app import reports as _reports
    safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
    return (str(_reports.LOGOS_DIR / f"{safe}-staged-tile.png"),
            str(_reports.LOGOS_DIR / f"{safe}-staged-orig.png"))


def get_staged_logo(conn: sqlite3.Connection, shop_id: str) -> dict | None:
    """The staged logo awaiting Yes ({at, tile, orig}), or None."""
    row = conn.execute("SELECT value FROM flags WHERE shop_id = ?"
                       " AND key = 'logo-staged'", (shop_id,)).fetchone()
    if row is None:
        return None
    try:
        staged = json.loads(row["value"])
    except ValueError:
        return None
    if not isinstance(staged, dict) or not staged.get("tile"):
        return None
    return staged


def stage_shop_logo(conn: sqlite3.Connection, shop_id: str,
                    image: bytes) -> str:
    """Stage a logo photo for Yes/No review; the live logo is untouched.

    Nothing stored changes until the owner replies Yes (apply) or No
    (discard). Consumes any pending logo request.
    """
    import os as _os

    from app import logo as _logo
    from app import reports as _reports

    from app.agent import queue_media as _queue
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    try:
        tile = _logo.render_tile(image or b"")
    except Exception:
        return _t("logo_not_photo")
    _reports.ensure_dirs()
    tile_path, orig_path = _staged_logo_paths(shop_id)
    with open(orig_path, "wb") as f:
        f.write(image or b"")
    with open(tile_path, "wb") as f:
        f.write(tile)
    conn.execute("INSERT INTO flags (shop_id, key, value)"
                 " VALUES (?, 'logo-staged', ?)"
                 " ON CONFLICT (shop_id, key) DO UPDATE"
                 " SET value = excluded.value",
                 (shop_id, json.dumps(
                     {"at": datetime.now(timezone.utc).isoformat(),
                      "tile": tile_path, "orig": orig_path})))
    conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'logo-pending'",
                 (shop_id,))
    conn.commit()
    out = _t("logo_staged")
    base = _os.environ.get("SISI_PUBLIC_URL", "").rstrip("/")
    if base:
        safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
        brand = _reports.shop_brand(conn, shop_id)
        brand = dict(brand, logo=tile_path, logo_orig=orig_path)
        card = str(_reports.REPORTS_DIR / f"{safe}-profile.png")
        with _doc_lang(_owner_lang(conn, shop_id)):
            _reports.render_shop_card(brand, card)
        token = _reports.store_card(conn, shop_id, "profile", card)
        _queue(shop_id, [{"kind": "image", "url": f"{base}/media/{token}?f=png",
                          "caption": _t("logo_cap_new"), "filename": ""}])
    return out


def apply_staged_logo(conn: sqlite3.Connection, shop_id: str) -> str:
    """Yes: replace the live logo with the staged one (undoable)."""
    import base64 as _b64

    from app import reports as _reports
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    staged = get_staged_logo(conn, shop_id)
    if staged is None:
        return _t("logo_staged_none")
    safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
    live_tile = str(_reports.LOGOS_DIR / f"{safe}-tile.png")
    live_orig = str(_reports.LOGOS_DIR / f"{safe}-orig.png")
    old: dict = {"tile": None, "orig": None}
    for key, path in (("tile", live_tile), ("orig", live_orig)):
        try:
            with open(path, "rb") as f:
                old[key] = _b64.b64encode(f.read()).decode("ascii")
        except OSError:
            old[key] = None
    with open(staged["tile"], "rb") as f:
        tile_bytes = f.read()
    with open(staged["orig"], "rb") as f:
        orig_bytes = f.read()
    with open(live_tile, "wb") as f:
        f.write(tile_bytes)
    with open(live_orig, "wb") as f:
        f.write(orig_bytes)
    _reports.logo_put(shop_id, tile_bytes)
    conn.execute("UPDATE shops SET logo_path = ?, logo_orig_path = ?"
                 " WHERE shop_id = ?", (live_tile, live_orig, shop_id))
    conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'logo-staged'",
                 (shop_id,))
    conn.execute("INSERT INTO actions (action_id, shop_id, kind, summary,"
                 " data_json) VALUES (?, ?, 'logo', ?, ?)",
                 (_action_id(), shop_id, "logo: replaced",
                  json.dumps(old)))
    conn.commit()
    return _t("logo_applied")


def discard_staged_logo(conn: sqlite3.Connection, shop_id: str) -> str:
    """No: throw the staged logo away; the live logo is untouched."""
    import os as _os
    ensure_shop(conn, shop_id)
    staged = get_staged_logo(conn, shop_id)
    if staged is None:
        return _t("logo_staged_none")
    for path in (staged.get("tile"), staged.get("orig")):
        try:
            if path:
                _os.remove(path)
        except OSError:
            pass
    conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'logo-staged'",
                 (shop_id,))
    conn.commit()
    return _t("logo_kept")


LOGO_PENDING_TTL = timedelta(minutes=30)
"""A 'redo logo' request stops hijacking photos after this long."""


def _logo_pending_at(conn: sqlite3.Connection, shop_id: str):
    """Timestamp of the logo request, or None when absent/unparseable."""
    row = conn.execute("SELECT value FROM flags WHERE shop_id = ? AND key = 'logo-pending'",
                       (shop_id,)).fetchone()
    if row is None:
        return None
    try:
        return datetime.fromisoformat(row["value"])
    except (ValueError, TypeError):
        return None


def logo_pending(conn: sqlite3.Connection, shop_id: str) -> bool:
    """True when a fresh logo request is still waiting for its photo.

    Stale requests (older than LOGO_PENDING_TTL) are cleared so they
    can never hijack an unrelated photo again.
    """
    at = _logo_pending_at(conn, shop_id)
    if at is None:
        row = conn.execute("SELECT value FROM flags WHERE shop_id = ?"
                           " AND key = 'logo-pending'", (shop_id,)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'logo-pending'",
                     (shop_id,))
        conn.commit()
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - at > LOGO_PENDING_TTL:
        conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'logo-pending'",
                     (shop_id,))
        conn.commit()
        return False
    return True


def clear_logo_pending(conn: sqlite3.Connection, shop_id: str) -> bool:
    """Drop any logo request (fresh or stale). True when one existed."""
    cur = conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'logo-pending'",
                       (shop_id,))
    conn.commit()
    return cur.rowcount > 0


def want_new_logo(conn: sqlite3.Connection, shop_id: str) -> str:
    """'redo/new/change logo': the NEXT image becomes the logo."""
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    stamp = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO flags (shop_id, key, value) VALUES (?, 'logo-pending', ?)"
                 " ON CONFLICT (shop_id, key) DO UPDATE SET value = excluded.value",
                 (shop_id, stamp))
    conn.commit()
    return _t("logo_send_prompt")


def logo_as_is(conn: sqlite3.Connection, shop_id: str) -> str:
    """Logos show as-is now; point the owner at the replacement command."""
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    return _t("logo_as_is")


def confirm_logo(conn: sqlite3.Connection, shop_id: str) -> str | None:
    """Accept the fresh logo after the Yes-review. None when not reviewing."""
    row = conn.execute("SELECT value FROM flags WHERE shop_id = ? AND key = 'logo-review'",
                       (shop_id,)).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'logo-review'",
                 (shop_id,))
    conn.commit()
    return _t("logo_confirmed")


def _queue_statement(conn: sqlite3.Connection, shop_id: str, data: dict,
                     brand: dict, base: str, lang: str) -> None:
    """Render the statement files in `lang`, captions in the message language.

    The files may be forwarded to the customer (their language when known);
    the captions instruct the owner, so they follow the current message.
    """
    from app.agent import queue_media as _queue
    from app import reports as _reports
    safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
    slug = "".join(c.lower() if c.isalnum() else "-" for c in data["name"]).strip("-")
    prefix = str(_reports.REPORTS_DIR / f"{safe}-statement-{slug}")
    with _doc_lang(lang):
        pages = _reports.render_statement_pngs(data, brand, prefix)
        pdf = f"{prefix}.pdf"
        _reports.render_statement_pdf(data, brand, pdf)
    media = []
    for n, page in enumerate(pages, 1):
        token = _reports.store_card(conn, shop_id, f"stmt-{slug}-p{n}", page)
        media.append({"kind": "image", "url": f"{base}/media/{token}?f=png",
                      "caption": _t("stmt_forward", name=data['name'])
                      + (_t("stmt_pages", n=n, total=len(pages)) if len(pages) > 1 else ""),
                      "filename": "", "first": True})
    token = _reports.store_card(conn, shop_id, f"stmt-{slug}-pdf", pdf)
    media.append({"kind": "file", "url": f"{base}/media/{token}?f=pdf",
                  "caption": _t("stmt_pdf_cap", name=data['name']),
                  "filename": f"statement-{slug}.pdf"})
    if base:
        _queue(shop_id, media)
    return len(media) if base else 0


def customer_statement(conn: sqlite3.Connection, shop_id: str, customer: str) -> str:
    blocked = _staff_blocked()
    if blocked:
        return blocked
    import os as _os

    from app import reports as _reports
    ensure_shop(conn, shop_id)
    data = _reports.statement_data(conn, shop_id, customer or "")
    if data is None:
        return _t("no_one_named", name=(customer or '').strip())
    if data["total"] <= 0 and not data["items"]:
        return _t("stmt_clean", name=data['name'])
    cust = _get_customer(conn, shop_id, data["name"])
    lang = statement_lang(conn, shop_id, cust["id"])
    with _doc_lang(lang):
        data = _reports.statement_data(conn, shop_id, data["name"])
    brand = _reports.shop_brand(conn, shop_id)
    _reports.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    sent = _queue_statement(conn, shop_id, data, brand,
                            _os.environ.get("SISI_PUBLIC_URL", "").rstrip("/"),
                            lang)
    tail = (_t("stmt_forward", name=data['name']) if sent
            else _t("stmt_tail_file"))
    return _t("stmt_reply", name=data['name'],
                total=cents_to_rand(data['total']), tail=tail)


def statements_for_customers(conn: sqlite3.Connection, shop_id: str,
                             ids: list[int], head: str) -> str:
    """One statement card (all pages) per customer id, queued in order."""
    blocked = _staff_blocked()
    if blocked:
        return blocked
    import os as _os

    from app import reports as _reports
    ensure_shop(conn, shop_id)
    by_id = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id, name FROM customers WHERE shop_id = ?", (shop_id,))}
    made = []
    brand = _reports.shop_brand(conn, shop_id)
    _reports.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    base = _os.environ.get("SISI_PUBLIC_URL", "").rstrip("/")
    for cid in ids:
        if cid not in by_id:
            continue
        lang = statement_lang(conn, shop_id, cid)
        with _doc_lang(lang):
            data = _reports.statement_data(conn, shop_id, by_id[cid])
        if data is None or data["total"] <= 0:
            continue
        _queue_statement(conn, shop_id, data, brand, base, lang)
        made.append(_t("stmts_line", name=data['name'],
                       total=cents_to_rand(data['total'])))
    if not made:
        return _t("debt_clean")
    tail = _t("stmts_tail_each") if base else _t("stmts_tail_file")
    return head + "\n" + "\n".join(made) + "\n\n" + tail


def statements_for_all(conn: sqlite3.Connection, shop_id: str) -> str:
    ensure_shop(conn, shop_id)
    rows = conn.execute(
        "SELECT c.id, MIN(e.created_at) first FROM customers c"
        " JOIN credit_entries e ON e.customer_id = c.id AND e.reversed = 0"
        " WHERE c.shop_id = ? GROUP BY c.id ORDER BY first",
        (shop_id,)).fetchall()
    return statements_for_customers(conn, shop_id, [r["id"] for r in rows],
                                    _t("stmts_head"))


def statements_top_n(conn: sqlite3.Connection, shop_id: str, n: int) -> str:
    """Statements for the top N debtors by balance (highest first)."""
    ensure_shop(conn, shop_id)
    rows = conn.execute("SELECT id FROM customers WHERE shop_id = ?",
                        (shop_id,)).fetchall()
    owed = [(balance_cents(conn, shop_id, r["id"]), r["id"]) for r in rows]
    owed = [(b, i) for b, i in owed if b > 0]
    owed.sort(reverse=True)
    return statements_for_customers(conn, shop_id,
                                    [i for _, i in owed[:max(n, 0)]],
                                    _t("stmts_head_n", n=n))


def statements_named(conn: sqlite3.Connection, shop_id: str,
                     names: list[str]) -> str:
    """Statements for explicitly named customers, in the order named."""
    ensure_shop(conn, shop_id)
    rows = conn.execute("SELECT id, name FROM customers WHERE shop_id = ?",
                        (shop_id,)).fetchall()
    by_name = {r["name"].strip().lower(): r["id"] for r in rows}
    ids = [by_name[n.strip().lower()] for n in names if n.strip().lower() in by_name]
    return statements_for_customers(conn, shop_id, ids,
                                    _t("stmts_head_some"))


def resend_last_media(conn: sqlite3.Connection, shop_id: str) -> str:
    """'resend': re-send the last picture or file that failed after retries."""
    ensure_shop(conn, shop_id)
    row = conn.execute("SELECT value FROM flags WHERE shop_id = ?"
                       " AND key = 'last-failed-media'", (shop_id,)).fetchone()
    failed = {}
    if row is not None:
        try:
            failed = json.loads(row["value"])
        except ValueError:
            failed = {}
    kind = failed.get("kind", "")
    if kind not in ("image", "file") or not failed.get("url"):
        return _t("resend_nothing")
    item = {"kind": kind, "url": failed["url"],
            "caption": failed.get("caption", ""),
            "filename": failed.get("filename", "")}
    channel = ensure_shop(conn, shop_id)["channel"] or "meta"
    if channel in ("vonage", "infobip"):
        from app.mediasend import request_media_send
        request_media_send(conn, shop_id, channel, kind,
                           {k: item[k] for k in ("url", "caption",
                                                "filename")})
    else:
        from app.agent import queue_media as _queue
        _queue(shop_id, [item])
    conn.execute("DELETE FROM flags WHERE shop_id = ?"
                 " AND key = 'last-failed-media'", (shop_id,))
    conn.commit()
    return _t("resend_again")


def get_order_draft(conn: sqlite3.Connection, shop_id: str) -> dict | None:
    row = conn.execute("SELECT payload_json FROM order_drafts WHERE shop_id = ?",
                       (shop_id,)).fetchone()
    return json.loads(row["payload_json"]) if row else None


def _save_order_draft(conn: sqlite3.Connection, shop_id: str, payload: dict) -> None:
    conn.execute("INSERT INTO order_drafts (shop_id, payload_json, updated_at)"
                 " VALUES (?, ?, ?) ON CONFLICT (shop_id) DO UPDATE SET"
                 " payload_json = excluded.payload_json, updated_at = excluded.updated_at",
                 (shop_id, json.dumps(payload), now_iso()))
    conn.commit()


def _render_order(shop_name: str, payload: dict) -> str:
    groups: dict[str, list] = {}
    for ln in payload["lines"]:
        groups.setdefault(ln["wholesaler"] or _t("no_wholesaler"), []).append(ln)
    blocks = [_t("order_draft_head")]
    for w, items in groups.items():
        bl = [f"*{w}*"]
        for ln in items:
            if ln["packs"]:
                word = ln["pack_name"] if ln["packs"] == 1 else _plural_pack(ln["pack_name"])
                line = _t("order_line_pack", name=ln['name'],
                          q=_fmt_qty(ln['packs']), word=word,
                          u=_fmt_qty(ln['units']))
            else:
                line = _t("order_line", name=ln['name'],
                          u=_fmt_qty(ln['units']))
            line += (_t("order_est", total=cents_to_rand(ln['est_cents']))
                     if ln["est_cents"] is not None else _t("order_nocost"))
            bl.append(line)
        blocks.append("\n".join(bl))
    known = [ln["est_cents"] for ln in payload["lines"] if ln["est_cents"] is not None]
    unknown = len(payload["lines"]) - len(known)
    total = _t("order_total", total=cents_to_rand(sum(known)))
    if unknown:
        total += _t("order_excl", n=unknown)
    blocks.append(total)
    blocks.append(_t("order_edit_hint"))
    return "\n\n".join(blocks)


def build_order(conn: sqlite3.Connection, shop_id: str) -> str:
    """Draft a restock order from low items, sales rate and pack sizes."""
    shop = ensure_shop(conn, shop_id)
    rows = conn.execute(
        "SELECT * FROM products WHERE shop_id = ? AND stock_qty <= reorder_level"
        " ORDER BY wholesaler, name", (shop_id,)).fetchall()
    if not rows:
        conn.execute("DELETE FROM order_drafts WHERE shop_id = ?", (shop_id,))
        conn.commit()
        return _t("order_nothing")
    start = (date.fromisoformat(today_str()) - timedelta(days=6)).isoformat()
    lines = []
    for r in rows:
        target = max(r["reorder_level"] * 2,
                     math.ceil(_avg_daily_sales(conn, shop_id, r["id"], start) * 7))
        need = max(0.0, target - r["stock_qty"])
        if not need:
            continue
        if r["pack_size"]:
            packs = math.ceil(need / r["pack_size"])
            units = packs * r["pack_size"]
        else:
            packs, units = None, need
        est = int(round(units * r["cost_cents"])) if r["cost_cents"] is not None else None
        lines.append({"product_id": r["id"], "name": r["name"],
                      "wholesaler": r["wholesaler"] or "", "units": units, "packs": packs,
                      "pack_name": r["pack_name"] or "", "est_cents": est})
    if not lines:
        return _t("order_nothing")
    payload = {"lines": lines, "sent": []}
    _save_order_draft(conn, shop_id, payload)
    return _render_order(shop["name"], payload)


def send_order(conn: sqlite3.Connection, shop_id: str, wholesaler: str | None = None) -> str:
    """Format one wholesaler's order lines as a forwardable message. Never sends it."""
    shop = ensure_shop(conn, shop_id)
    draft = get_order_draft(conn, shop_id)
    if not draft or not draft.get("lines"):
        return _t("order_no_draft")
    groups: dict[str, list] = {}
    for ln in draft["lines"]:
        groups.setdefault(ln["wholesaler"] or _t("no_wholesaler"), []).append(ln)
    if wholesaler:
        want = (wholesaler or "").strip().lower()
        match = next((g for g in groups if want in g.lower() or g.lower() in want), None)
        if match is None:
            return _t("order_no_match", name=wholesaler.strip())
        groups = {match: groups[match]}
    if len(groups) > 1:
        return _t("order_which_ws")
    w, items = next(iter(groups.items()))
    if w in (draft.get("sent") or []):
        return _t("order_already")
    draft["sent"] = sorted(set(draft.get("sent") or []) | {w})
    _save_order_draft(conn, shop_id, draft)
    lines = []
    for ln in items:
        if ln["packs"]:
            word = ln["pack_name"] if ln["packs"] == 1 else _plural_pack(ln["pack_name"])
            lines.append(_t("order_fwd_line_pack", name=ln['name'], q=_fmt_qty(ln['packs']), word=word))
        else:
            lines.append(_t("order_fwd_line", name=ln['name'], q=_fmt_qty(ln['units'])))
    out = _t("order_hello", w=w, shop=shop['name']) + "\n\n" + "\n".join(lines)
    known = [ln["est_cents"] for ln in items if ln["est_cents"] is not None]
    if known:
        out += _t("order_fwd_total", total=cents_to_rand(sum(known)))
    out += _t("order_thanks")
    _queue_order_card(conn, shop_id, {"lines": items})
    return out


def _queue_order_card(conn: sqlite3.Connection, shop_id: str, payload: dict) -> None:
    import os as _os

    from app import reports as _reports

    from app.agent import queue_media as _queue
    base = _os.environ.get("SISI_PUBLIC_URL", "").rstrip("/")
    if not base:
        return
    _reports.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
    card = str(_reports.REPORTS_DIR / f"{safe}-order.png")
    with _doc_lang(_owner_lang(conn, shop_id)):
        _reports.render_order_png(payload, _reports.shop_brand(conn, shop_id),
                                  card, today_str())
    token = _reports.store_card(conn, shop_id, "order", card)
    _queue(shop_id, [{"kind": "image", "url": f"{base}/media/{token}?f=png",
                      "caption": _t("order_card_cap"),
                      "filename": ""}])


def edit_order_add(conn: sqlite3.Connection, shop_id: str, product: str,
                   qty: float, unit: str | None = None):
    shop = ensure_shop(conn, shop_id)
    draft = get_order_draft(conn, shop_id)
    if not draft:
        return _t("order_no_draft")
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        return _t("how_many")
    if qty <= 0:
        return _t("how_many")
    prod, ambiguous = _find_products(conn, shop_id, product or "")
    if prod is None:
        if ambiguous:
            return _which_one(ambiguous)
        return _t("prod_not_on_shelf", name=(product or '').strip())
    units, is_pack, no_pack = _resolve_units(prod["pack_name"], prod["pack_size"], qty, unit)
    if no_pack:
        first = prod["name"].split()[0].lower()
        return _t("order_no_pack", name=prod['name'],
                    echo=_pack_echo(unit), first=first)
    packs = qty if is_pack else None
    est = int(round(units * prod["cost_cents"])) if prod["cost_cents"] is not None else None
    line = {"product_id": prod["id"], "name": prod["name"],
            "wholesaler": prod["wholesaler"] or "", "units": units, "packs": packs,
            "pack_name": prod["pack_name"] or "", "est_cents": est}
    kept = [ln for ln in draft["lines"] if ln["product_id"] != prod["id"]]
    kept.append(line)
    kept.sort(key=lambda ln: (ln["wholesaler"], ln["name"]))
    draft["lines"] = kept
    _save_order_draft(conn, shop_id, draft)
    return _render_order(shop["name"], draft)


def edit_order_remove(conn: sqlite3.Connection, shop_id: str, product: str):
    shop = ensure_shop(conn, shop_id)
    draft = get_order_draft(conn, shop_id)
    if not draft:
        return _t("order_no_draft")
    prod, _ = _find_products(conn, shop_id, product or "")
    pid = prod["id"] if prod else None
    kept = [ln for ln in draft["lines"] if ln["product_id"] != pid]
    if len(kept) == len(draft["lines"]):
        return _t("order_not_on", name=(product or '').strip())
    if not kept:
        conn.execute("DELETE FROM order_drafts WHERE shop_id = ?", (shop_id,))
        conn.commit()
        return _t("order_cleared")
    draft["lines"] = kept
    _save_order_draft(conn, shop_id, draft)
    return _render_order(shop["name"], draft)


def cash_balances(conn: sqlite3.Connection, shop_id: str) -> str:
    ensure_shop(conn, shop_id)
    bals = {a: account_balance(conn, shop_id, a) for a in ("cash", "petty", "bank")}
    return (_t("cash_head2") + "\n"
            + _t("cash_line", acct=_acct_name("cash"),
                 amt=cents_to_rand(bals['cash'])) + "\n"
            + _t("cash_line", acct=_acct_name("petty"),
                 amt=cents_to_rand(bals['petty'])) + "\n"
            + _t("cash_line", acct=_acct_name("bank"),
                 amt=cents_to_rand(bals['bank'])) + "\n\n"
            + _t("cash_total", total=cents_to_rand(sum(bals.values()))))


def set_float(conn: sqlite3.Connection, shop_id: str, amounts: dict) -> str:
    """Set opening floats once per account. Later corrections go via count."""
    ensure_shop(conn, shop_id)
    lines, skipped = [], []
    for acct in ("cash", "petty", "bank"):
        if acct not in (amounts or {}):
            continue
        try:
            amount = int(amounts[acct])
        except (TypeError, ValueError):
            continue
        if amount < 0:
            continue
        has = conn.execute("SELECT COUNT(*) c FROM cash_moves WHERE shop_id = ? AND account = ?",
                           (shop_id, acct)).fetchone()["c"]
        if has:
            skipped.append(_t("float_had", acct=_acct_name(acct),
                              amt=cents_to_rand(account_balance(conn, shop_id,
                                                               acct))))
            continue
        _post_cash(conn, shop_id, acct, amount, "opening", f"float-{shop_id}-{acct}",
                   note="opening float")
        lines.append(_t("float_line", acct=_acct_name(acct),
                            amt=cents_to_rand(amount)))
    conn.commit()
    if not lines and not skipped:
        return _t("float_none")
    out = ""
    if lines:
        out = _t("float_head") + "\n" + "\n".join(lines)
    if skipped:
        tail = "; ".join(skipped) + _t("float_correct")
        out = (out + "\n" + tail) if out else tail
    return out


def _restore_stock(conn: sqlite3.Connection, shop_id: str, product_id: int, qty: float,
                   action_id: str) -> float:
    conn.execute("UPDATE products SET stock_qty = stock_qty + ? WHERE id = ?", (qty, product_id))
    conn.execute(
        "INSERT INTO stock_moves (shop_id, product_id, delta_qty, reason, action_id)"
        " VALUES (?, ?, ?, 'undo', ?)",
        (shop_id, product_id, qty, action_id),
    )
    return conn.execute("SELECT stock_qty FROM products WHERE id = ?",
                        (product_id,)).fetchone()["stock_qty"]


def _undo_sale(conn: sqlite3.Connection, shop_id: str, undo: dict, action_id: str) -> str:
    sale = conn.execute("SELECT * FROM sales WHERE id = ?", (undo["sale_id"],)).fetchone()
    conn.execute("UPDATE sales SET reversed = 1 WHERE id = ?", (undo["sale_id"],))
    _reverse_moves(conn, shop_id, action_id)
    stock = _restore_stock(conn, shop_id, undo["product_id"], undo["qty"], action_id)
    if undo.get("credit_id"):
        conn.execute("UPDATE credit_entries SET reversed = 1 WHERE id = ?", (undo["credit_id"],))
        cust = conn.execute("SELECT * FROM customers WHERE id = ?",
                            (sale["customer_id"],)).fetchone()
        bal = balance_cents(conn, shop_id, cust["id"])
        return _t("undo_sale_credit", name=cust['name'],
                        amt=cents_to_rand(sale['total_cents']),
                        bal=cents_to_rand(bal))
    return _t("undo_sale_cash", amt=cents_to_rand(sale['total_cents']),
              stock=_fmt_qty(stock))


def _undo_stock(conn: sqlite3.Connection, shop_id: str, undo: dict, action_id: str) -> str:
    pid = undo["product_id"]
    name = conn.execute("SELECT name FROM products WHERE id = ?", (pid,)).fetchone()["name"]
    _reverse_moves(conn, shop_id, action_id)
    stock = _restore_stock(conn, shop_id, pid, -undo["qty"], action_id)
    if not undo.get("created"):
        sets, params = ["wholesaler = ?"], [undo.get("old_wholesaler", "")]
        if "old_cost" in undo:
            sets.append("cost_cents = ?")
            params.append(undo["old_cost"])
        if "old_price" in undo:
            sets.append("price_cents = ?")
            params.append(undo["old_price"])
        if "old_pack" in undo:
            sets.append("pack_price_cents = ?")
            params.append(undo["old_pack"])
        if "old_pack_name" in undo:
            sets.append("pack_name = ?")
            params.append(undo["old_pack_name"])
        if "old_pack_size" in undo:
            sets.append("pack_size = ?")
            params.append(undo["old_pack_size"])
        conn.execute(f"UPDATE products SET {', '.join(sets)} WHERE id = ?", (*params, pid))
        return _t("undo_stock", name=name, stock=_fmt_qty(stock))
    conn.execute("DELETE FROM stock_moves WHERE product_id = ?", (pid,))
    conn.execute("DELETE FROM products WHERE id = ?", (pid,))
    return _t("undo_stock_new", name=name)


def _undo_price(conn: sqlite3.Connection, shop_id: str, undo: dict) -> str:
    column = "pack_price_cents" if undo.get("for_pack") else "price_cents"
    conn.execute(f"UPDATE products SET {column} = ? WHERE id = ?",
                 (undo["old_price"], undo["product_id"]))
    name = conn.execute("SELECT name FROM products WHERE id = ?",
                        (undo["product_id"],)).fetchone()["name"]
    if undo.get("for_pack"):
        what = f"{name} {undo.get('pack_name')} price"
    else:
        what = f"{name} price"
    if undo["old_price"] is None:
        return _t("undo_price_gone", what=what)
    return _t("undo_price", what=what, old=cents_to_rand(undo['old_price']))


def _undo_reminder(conn: sqlite3.Connection, shop_id: str, undo: dict) -> str:
    cur = conn.execute("UPDATE reminders SET status = 'cancelled'"
                       " WHERE id = ? AND shop_id = ?"
                       " AND status IN ('pending', 'blocked', 'failed')",
                       (undo.get("reminder_id"), shop_id))
    if cur.rowcount == 0:
        return _t("undo_reminder_gone")
    return _t("undo_reminder")


def _undo_expiry(conn: sqlite3.Connection, shop_id: str, undo: dict) -> str:
    conn.execute(
        f"DELETE FROM expiry_items WHERE id IN ({','.join('?' * len(undo['item_ids']))})",
        undo["item_ids"],
    )
    return _t("undo_expiry")


def _undo_slip(conn: sqlite3.Connection, shop_id: str, undo: dict, action_id: str) -> str:
    for move in undo["moves"]:
        _restore_stock(conn, shop_id, move["product_id"], -move["qty"], action_id)
        if move.get("created"):
            conn.execute("DELETE FROM stock_moves WHERE product_id = ?", (move["product_id"],))
            conn.execute("DELETE FROM products WHERE id = ?", (move["product_id"],))
    for pid, old_cost in (undo.get("old_costs") or {}).items():
        conn.execute("UPDATE products SET cost_cents = ? WHERE id = ?", (old_cost, int(pid)))
    return _t("undo_slip")


def undo_last(conn: sqlite3.Connection, shop_id: str) -> str:
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    row = conn.execute(
        "SELECT * FROM actions WHERE shop_id = ? AND undone = 0 AND kind != 'seed'"
        " ORDER BY rowid DESC LIMIT 1",
        (shop_id,)).fetchone()
    if row is None:
        return _t("nothing_to_undo")
    return _undo_row(conn, shop_id, row)


def undo_last_kind(conn: sqlite3.Connection, shop_id: str, kind: str, nothing_msg: str) -> str:
    """Undo the most recent action of one kind, even when it is not the last."""
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    row = conn.execute(
        "SELECT * FROM actions WHERE shop_id = ? AND undone = 0 AND kind = ?"
        " ORDER BY rowid DESC LIMIT 1",
        (shop_id, kind)).fetchone()
    if row is None:
        return nothing_msg
    return _undo_row(conn, shop_id, row)


def _undo_order(conn: sqlite3.Connection, shop_id: str, undo: dict) -> str:
    """Undo a confirmed customer order: restore the previous order draft."""
    prev = undo.get("previous")
    if prev is None:
        conn.execute("DELETE FROM order_drafts WHERE shop_id = ?", (shop_id,))
    else:
        _save_order_draft(conn, shop_id, prev)
    return _t("undo_order_done")


def _undo_customer(conn: sqlite3.Connection, shop_id: str, undo: dict) -> str:
    """Undo a plan-added customer, unless the book already uses them."""
    cid = undo.get("customer_id")
    name = undo.get("name", "")
    if cid is None:
        return _t("undo_cust_done")
    refs = 0
    for table, column in (("credit_entries", "customer_id"),
                          ("payments", "customer_id"),
                          ("sales", "customer_id")):
        refs += conn.execute(f"SELECT COUNT(*) n FROM {table}"
                             " WHERE shop_id = ? AND customer_id = ?",
                             (shop_id, cid)).fetchone()["n"]
    if refs:
        return _t("undo_cust_kept", name=name)
    conn.execute("DELETE FROM customers WHERE id = ? AND shop_id = ?",
                 (cid, shop_id))
    return _t("undo_cust_done")


def _undo_logo(conn: sqlite3.Connection, shop_id: str, undo: dict) -> str:
    """Undo a logo replacement: restore the previous tile + original."""
    import base64 as _b64

    from app import reports as _reports
    safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
    live_tile = str(_reports.LOGOS_DIR / f"{safe}-tile.png")
    live_orig = str(_reports.LOGOS_DIR / f"{safe}-orig.png")
    for key, path in (("tile", live_tile), ("orig", live_orig)):
        blob = undo.get(key)
        if blob is None:
            try:
                import os as _os
                _os.remove(path)
            except OSError:
                pass
        else:
            with open(path, "wb") as f:
                f.write(_b64.b64decode(blob))
    if undo.get("tile") is None:
        conn.execute("UPDATE shops SET logo_path = '', logo_orig_path = ''"
                     " WHERE shop_id = ?", (shop_id,))
        _reports.logo_put(shop_id, b"")
    else:
        with open(live_tile, "rb") as f:
            _reports.logo_put(shop_id, f.read())
    return _t("undo_logo_done")


def _undo_row(conn: sqlite3.Connection, shop_id: str, row) -> str:
    undo = json.loads(row["data_json"])
    kind = row["kind"]
    if kind == "credit":
        msg = _undo_credit(conn, shop_id, undo)
    elif kind == "payment":
        msg = _undo_payment(conn, shop_id, undo, row["action_id"])
    elif kind == "expense":
        msg = _undo_expense(conn, shop_id, undo, row["action_id"])
    elif kind == "drawing":
        msg = _undo_cash_only(conn, shop_id, undo, row["action_id"], "drawing")
    elif kind == "count":
        msg = _undo_cash_only(conn, shop_id, undo, row["action_id"], "cash count")
    elif kind == "supplier_payment":
        msg = _undo_cash_only(conn, shop_id, undo, row["action_id"], "supplier payment")
    elif kind in ("sale_cash", "sale_credit"):
        msg = _undo_sale(conn, shop_id, undo, row["action_id"])
    elif kind == "stock":
        msg = _undo_stock(conn, shop_id, undo, row["action_id"])
    elif kind == "stock_intake":
        msg = _undo_stock_intake(conn, shop_id, undo, row["action_id"])
    elif kind == "price":
        msg = _undo_price(conn, shop_id, undo)
    elif kind == "expiry":
        msg = _undo_expiry(conn, shop_id, undo)
    elif kind == "reminder":
        msg = _undo_reminder(conn, shop_id, undo)
    elif kind == "slip":
        msg = _undo_slip(conn, shop_id, undo, row["action_id"])
    elif kind == "book":
        msg = _undo_book(conn, shop_id, undo, row["action_id"])
    elif kind == "receipt":
        msg = _undo_receipt(conn, shop_id, undo, row["action_id"])
    elif kind == "order":
        msg = _undo_order(conn, shop_id, undo)
    elif kind == "customer":
        msg = _undo_customer(conn, shop_id, undo)
    elif kind == "logo":
        msg = _undo_logo(conn, shop_id, undo)
    else:
        return _t("cannot_undo")
    conn.execute("UPDATE actions SET undone = 1, undone_at = ? WHERE action_id = ?",
                 (now_iso(), row["action_id"]))
    conn.commit()
    return msg


_APPLIERS = {
    "add_credit": _apply_add_credit,
    "book_page": _apply_book_page,
    "receipt": _apply_receipt,
    "record_payment": _apply_record_payment,
    "record_expense": _apply_record_expense,
    "record_drawing": _apply_record_drawing,
    "record_count": _apply_record_count,
    "record_supplier_payment": _apply_supplier_payment,
    "record_sale": _apply_record_sale,
    "add_stock": _apply_add_stock,
    "stock_intake": _apply_stock_intake,
    "stock_intake_item": _apply_stock_intake_item,
    "set_price": _apply_set_price,
    "expiry_note": _apply_expiry_note,
    "read_slip": _apply_slip,
    "reminder_offer": _apply_reminder_offer,
}


# ---------------------------------------------------------------------------
# Model-facing registry (used by llm.py in Step 3)
# ---------------------------------------------------------------------------


def _tool(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


TOOL_DEFS = [
    _tool("add_credit", "Put items someone TOOK on their credit book (asks for Yes first)."
          " Use for 'took ... on the book'. No sale is recorded and stock is unchanged."
          " qty is ALWAYS single units; pass unit only when the owner names a pack.",
          {"customer": {"type": "string"},
           "items": {"type": "array", "items": {"type": "object",
                     "properties": {"product": {"type": "string"}, "qty": {"type": "number"},
                                    "unit": {"type": "string"}},
                     "required": ["product", "qty"]}}}, ["customer", "items"]),
    _tool("record_payment", "Record a customer paying into the credit book.",
          {"customer": {"type": "string"}, "amount_cents": {"type": "integer"},
           "account": {"type": "string",
                       "description": "cash, petty or bank (default cash)"}},
          ["customer", "amount_cents"]),
    _tool("record_expense", "Record shop spending with a category: transport,"
          " electricity, rent, airtime, wages, other. Use for 'took R200 petty"
          " cash for transport', 'paid R150 electricity'.",
          {"amount_cents": {"type": "integer"}, "category": {"type": "string"},
           "account": {"type": "string",
                       "description": "cash, petty or bank (default cash)"},
           "note": {"type": "string"}}, ["amount_cents"]),
    _tool("record_drawing", "Owner takes cash for personal use ('took R500 for myself').",
          {"amount_cents": {"type": "integer"}, "account": {"type": "string"},
           "note": {"type": "string"}}, ["amount_cents"]),
    _tool("record_count", "Physical cash count. Use for 'counted R1,240': compares"
          " with the book and shows over/short.",
          {"amount_cents": {"type": "integer"}}, ["amount_cents"]),
    _tool("record_supplier_payment", "Pay a wholesaler/supplier account.",
          {"supplier": {"type": "string"}, "amount_cents": {"type": "integer"},
           "account": {"type": "string"}}, ["supplier", "amount_cents"]),
    _tool("cash_balances", "Till, petty cash and bank balances with the total."
          " Use for 'cash in hand?', 'how much petty cash?'.", {}, []),
    _tool("sales_summary", "Cash, credit and total sales for a period.",
          {"period": {"type": "string",
                      "description": "today, week or month"}}, []),
    _tool("profit_month", "Month profit: sales, cost of sales, gross and margin,"
          " expenses, net. Use for 'what is our profit this month?'.",
          {"month": {"type": "string", "description": "YYYY-MM, default this month"}},
          []),
    _tool("list_receipts", "Filed purchase receipts for a month with the total."
          " Use for 'show my receipts this month'.",
          {"month": {"type": "string",
                     "description": "YYYY-MM or month name, default this month"}},
          []),
    _tool("month_end_report", "Month-end management accounts: renders the branded"
          " PDF + PNG summary card and queues them as WhatsApp media. Use for"
          " 'month-end report' or 'report September'.",
          {"month": {"type": "string",
                     "description": "YYYY-MM or month name; omit for the books due"}},
          []),
    _tool("set_shop_name", "Save the OWNER's own spaza shop name. Never invent one.",
          {"name": {"type": "string"}}, ["name"]),
    _tool("set_shop_address", "Save the shop's address (optional).",
          {"address": {"type": "string"}}, ["address"]),
    _tool("set_shop_phone", "Save the shop's phone number (optional).",
          {"phone": {"type": "string"}}, ["phone"]),
    _tool("show_shop_profile", "Show the shop profile header card.",
          {}, []),
    _tool("customer_statement", "Branded PNG + PDF statement for one customer,"
          " queued for the OWNER to forward. Use for 'statement for Sipho'.",
          {"customer": {"type": "string"}}, ["customer"]),
    _tool("statements_for_all", "Branded statements for every debtor,"
          " oldest debt first. Use for 'statements for everyone who owes'.",
          {}, []),
    _tool("set_customer_limit", "Cap what one customer may owe. Warns on later"
          " credit sales over it. Use for 'set Sipho limit R300'.",
          {"customer": {"type": "string"},
           "amount_cents": {"type": "integer"}}, ["customer", "amount_cents"]),
    _tool("set_nudge", "Change the evening books reminder: 'HH:MM', 'off', 'on'.",
          {"setting": {"type": "string"}}, ["setting"]),
    _tool("set_expiry", "Note when stock expires. Sisi warns 2 days before."
          " Use for 'bread expires Friday'.",
          {"product": {"type": "string"}, "when": {"type": "string"}},
          ["product", "when"]),
    _tool("add_staff", "Add a staff number (owner only). Staff can record sales,"
          " credit and book photos. Use for 'add staff 0821234567 Thandi'.",
          {"phone": {"type": "string"}, "name": {"type": "string"}},
          ["phone", "name"]),
    _tool("who_recorded_today", "Breakdown of who recorded entries today.",
          {}, []),
    _tool("get_balance", "How much ONE named customer owes on the credit book. Needs a name.",
          {"customer": {"type": "string"}}, ["customer"]),
    _tool("list_debtors", "Everyone who owes on the credit book, with the total.", {}, []),
    _tool("draft_reminder", "Draft a polite payment reminder the owner forwards herself.",
          {"customer": {"type": "string"}}, ["customer"]),
    _tool("ask_clarify", "Ask the owner ONE short clarifying question instead of"
          " guessing. Use when a request is ambiguous (which product, which"
          " customer, which account). Up to 3 short options; they become reply"
          " buttons. Write the question and options in the owner's language.",
          {"question": {"type": "string"},
           "options": {"type": "array", "items": {"type": "string"}}},
          ["question"]),
    _tool("record_sale", "Record something SOLD: reduces stock and counts toward cash-up."
          " Use whenever the owner says they sold something."
          " Omit buyer for cash, or name the buyer for a credit sale."
          " qty is ALWAYS single units ('5 chappies' is 5 loose sweets); pass unit"
          " only when the owner names a pack ('box', 'tray', 'ibhokisi').",
          {"product": {"type": "string"}, "qty": {"type": "number"},
           "buyer": {"type": "string"}, "unit": {"type": "string"}}, ["product"]),
    _tool("add_stock", "Add stock ('add', 'got', 'restock', 'bought for the shop')."
          " Pass wholesaler only when the owner names a specific one; never guess."
          " qty is singles unless unit names a pack ('2 boxes chappies'). cost_cents"
          " is per pack when unit is a pack (it is converted), per single otherwise;"
          " price_cents is always the single sell price; pack_price_cents the pack"
          " sell price.",
          {"product": {"type": "string"}, "qty": {"type": "number"},
           "wholesaler": {"type": "string"}, "cost_cents": {"type": "integer"},
           "price_cents": {"type": "integer"}, "unit": {"type": "string"},
           "pack_price_cents": {"type": "integer"},
           "account": {"type": "string", "description": "cash, petty, bank or"
                       " supplier when bought on the supplier's account"}},
          ["product", "qty"]),
    _tool("set_price", "Set a product's selling price in cents. Single price by"
          " default; for_pack=true sets the pack sell price ('chappies box R95').",
          {"product": {"type": "string"}, "price_cents": {"type": "integer"},
           "for_pack": {"type": "boolean"}}, ["product", "price_cents"]),
    _tool("low_stock", "ONLY the products at or below reorder level. Not the full list.", {}, []),
    _tool("list_stock", "FULL stock list grouped by category: every item, quantity and sell"
          " price. Use for 'what do we have in stock', 'stock breakdown', 'stock on hand'."
          " Relay the COMPLETE list exactly as returned — never summarize, shorten,"
          " or ask if they want the rest.", {}, []),
    _tool("best_sellers", "Top-selling products by revenue over the last N days (default 7).", {"days": {"type": "integer"}}, []),
    _tool("restock_list", "Restock list grouped by wholesaler.", {}, []),
    _tool("cash_up", "Today's takings and money summary: cash sales, book sales, payments in."
          " Use for 'cash up', 'takings', 'how much did I make', 'how did we do today'.",
          {"day": {"type": "string", "description": "YYYY-MM-DD, default today"}}, []),
    _tool("check_expiry_photo", "Store expiry dates read from a photo.",
          {"findings": {"type": "array", "items": {"type": "object",
            "properties": {"product": {"type": "string"}, "qty": {"type": "number"},
                           "expiry_date": {"type": "string"}},
            "required": ["product", "qty", "expiry_date"]}}}, ["findings"]),
    _tool("read_slip_photo", "Add wholesaler slip lines to stock.",
          {"items": {"type": "array", "items": {"type": "object",
            "properties": {"product": {"type": "string"}, "qty": {"type": "number"},
                           "cost_cents": {"type": "integer"}},
            "required": ["product", "qty"]}}, "wholesaler": {"type": "string"}}, ["items"]),
    _tool("compliance_guide", "Official info: registration, permit, acceptability, fund, scams.",
          {"topic": {"type": "string", "description": "overview, register, permit, coa, fund, scam"}},
          []),
    _tool("undo_last", "Undo the last confirmed action.", {}, []),
]

TOOL_FUNCS = {
    "add_credit": add_credit,
    "record_payment": record_payment,
    "record_expense": record_expense,
    "record_drawing": record_drawing,
    "record_count": record_count,
    "record_supplier_payment": record_supplier_payment,
    "cash_balances": cash_balances,
    "sales_summary": sales_summary,
    "profit_month": profit_month,
    "list_receipts": list_receipts,
    "month_end_report": month_end_report,
    "set_shop_name": set_shop_name,
    "set_shop_address": set_shop_address,
    "set_shop_phone": set_shop_phone,
    "show_shop_profile": show_shop_profile,
    "customer_statement": customer_statement,
    "statements_for_all": statements_for_all,
    "set_customer_limit": set_customer_limit,
    "set_nudge": set_nudge,
    "set_expiry": set_expiry,
    "add_staff": add_staff,
    "who_recorded_today": who_recorded_today,
    "get_balance": get_balance,
    "list_debtors": list_debtors,
    "draft_reminder": draft_reminder,
    "ask_clarify": ask_clarify,
    "record_sale": record_sale,
    "add_stock": add_stock,
    "set_price": set_price,
    "low_stock": low_stock,
    "list_stock": list_stock,
    "best_sellers": best_sellers,
    "restock_list": restock_list,
    "cash_up": cash_up,
    "check_expiry_photo": check_expiry_photo,
    "read_slip_photo": read_slip_photo,
    "compliance_guide": compliance_guide,
    "undo_last": undo_last,
}
