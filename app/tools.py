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
    query word must appear in the product name ("Sasko bread" finds Sasko White Bread)."""
    q = _normalize_query(query)
    rows = conn.execute("SELECT * FROM products WHERE shop_id = ?", (shop_id,)).fetchall()
    for r in rows:
        if r["name"].lower() == q:
            return r, []
    toks = [t for t in q.split() if len(t) > 1]
    hits = [r for r in rows if toks and all(t in r["name"].lower() for t in toks)]
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
    lines = ["❓ *Which one?*"] + [f"• {p['name']}" for p in ambiguous[:3]]
    return "\n".join(lines)


def _ask_price(name: str) -> str:
    return f"❓ What is the price of {name}?"


# ---------------------------------------------------------------------------
# Pending actions: propose / confirm / cancel
# ---------------------------------------------------------------------------


def get_pending(conn: sqlite3.Connection, shop_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM pending_actions WHERE shop_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
        (shop_id,),
    ).fetchone()


def _propose(conn: sqlite3.Connection, shop_id: str, action_type: str, payload: dict, preview: str) -> str:
    conn.execute(
        "UPDATE pending_actions SET status = 'superseded', resolved_at = ?"
        " WHERE shop_id = ? AND status = 'pending'",
        (now_iso(), shop_id),
    )
    conn.execute(
        "INSERT INTO pending_actions (shop_id, action_type, payload_json, preview_text)"
        " VALUES (?, ?, ?, ?)",
        (shop_id, action_type, json.dumps(payload), preview),
    )
    conn.commit()
    return preview


def confirm_pending(conn: sqlite3.Connection, shop_id: str) -> str:
    ensure_shop(conn, shop_id)
    row = get_pending(conn, shop_id)
    if row is None:
        return "There is nothing waiting for Yes."
    apply = _APPLIERS.get(row["action_type"])
    if apply is None:
        conn.execute("UPDATE pending_actions SET status = 'superseded' WHERE id = ?", (row["id"],))
        conn.commit()
        return "That preview expired. Please ask again."
    action_id = _action_id()
    payload = json.loads(row["payload_json"])
    message, kind, summary, undo = apply(conn, shop_id, payload, action_id)
    if payload.get("photo_hash"):
        photo_kind = {"expiry_note": "expiry", "book_page": "book_page",
                      "receipt": "receipt"}.get(row["action_type"], "")
        record_photo(conn, shop_id, payload["photo_hash"], photo_kind)
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
        margin = f" Your margin is now {round((price - first['new']) * 100 / price)}%."
    suggest = (price + up) if price else None
    if suggest is not None:
        suggest = int(_math.ceil(suggest / 50) * 50)
        payload = {"product_id": first["product_id"], "name": first["name"],
                   "price_cents": suggest, "old_price": price,
                   "for_pack": False, "pack_name": ""}
        _propose(conn, shop_id, "set_price", payload,
                 f"📝 *Set price?*\n• {first['name']} {cents_to_rand(price)}"
                 f" → {cents_to_rand(suggest)}")
        ask = f" Update the price to {cents_to_rand(suggest)}?"
    else:
        ask = " Set a sell price?"
    extra = f" (+{len(increases) - 1} more)" if len(increases) > 1 else ""
    return (f"\n\n⚠️ *{first['name']} cost went up {cents_to_rand(up)}"
            f" at {supplier}.*{margin}{ask}{extra}")


def cancel_pending(conn: sqlite3.Connection, shop_id: str) -> str:
    ensure_shop(conn, shop_id)
    row = get_pending(conn, shop_id)
    if row is None:
        return "There is nothing waiting."
    conn.execute(
        "UPDATE pending_actions SET status = 'cancelled', resolved_at = ? WHERE id = ?",
        (now_iso(), row["id"]),
    )
    conn.commit()
    return "OK, I did not save it."


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
        label = (f"{_fmt_qty(qty)} × {prod['name']} {prod['pack_name']}"
                 f" ({_fmt_qty(units)} units) @ {cents_to_rand(charged)}")
    else:
        total = int(round(single * units))
        short = f"{_fmt_qty(units)}x {prod['name']}"
        label = f"{_fmt_qty(units)} × {prod['name']} @ {cents_to_rand(single)}"
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
        f"✅ *Saved*\n• {cust['name']} now owes {cents_to_rand(bal)}.",
        "credit",
        f"credit {cust['name']} {cents_to_rand(payload['total_cents'])}",
        {"credit_id": cur.lastrowid},
    )


def add_credit(conn: sqlite3.Connection, shop_id: str, customer: str, items: list[dict]) -> str:
    """Propose a credit-book entry. items: [{product, qty}]."""
    ensure_shop(conn, shop_id)
    if not customer or not customer.strip():
        return "Whose name must go on the book?"
    if not items:
        return "What did they take?"
    lines = []
    for item in items:
        try:
            qty = float(item.get("qty", 0))
        except (TypeError, ValueError):
            qty = 0
        name = str(item.get("product", "")).strip()
        if not name or qty <= 0:
            return "Tell me each item and how many."
        line, status, detail = _resolve_priced_line(conn, shop_id, name, qty,
                                                         item.get("unit"))
        if status == "ambiguous":
            return detail
        if status in ("unknown", "unpriced"):
            _store_await_price(conn, shop_id, detail,
                               {"tool": "credit", "customer": customer.strip(), "items": items})
            if status == "unknown":
                cap = detail[:1].upper() + detail[1:]
                return (f"📝 *{cap} isn't on your list yet.*\nReply with the price, e.g."
                        f" '{detail.lower()} R18', and I'll add it and record"
                        f" {customer.strip()}'s credit.")
            if _is_pack_unit(item.get("unit")):
                return (f"{_ask_price(detail)}\nSay one price, e.g. 'R2 each'"
                        f" — or the pack price, e.g. '{_pack_echo(item.get('unit'))} R60'.")
            return _ask_price(detail)
        lines.append(line)
    total = sum(ln["line_total"] for ln in lines)
    payload = {"customer": customer.strip(), "lines": lines, "total_cents": total}
    is_new = _get_customer(conn, shop_id, customer) is None
    with _dry_run(conn):
        _apply_add_credit(conn, shop_id, payload, _action_id())
        cust = _get_customer(conn, shop_id, customer)
        new_bal = balance_cents(conn, shop_id, cust["id"])
    preview = (
        f"📝 *Save to the book?*\n"
        f"• {customer.strip()} +{cents_to_rand(total)} ({_items_text(lines)})\n"
        f"• New balance {cents_to_rand(new_bal)}"
    )
    if not is_new:
        cust = _get_customer(conn, shop_id, customer)
        if cust is not None:
            preview += _limit_warn(conn, shop_id, cust["id"], new_bal)
    if is_new:
        preview += "\n• New customer."
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
        f"✅ *Saved*\n• {payload['customer']} paid {cents_to_rand(payload['amount_cents'])}\n"
        f"• Balance {cents_to_rand(bal)}",
        "payment",
        f"payment {payload['customer']} {cents_to_rand(payload['amount_cents'])}",
        {"payment_id": cur.lastrowid},
    )


def record_payment(conn: sqlite3.Connection, shop_id: str, customer: str, amount_cents: int,
                   account: str | None = None) -> str:
    ensure_shop(conn, shop_id)
    cust = _get_customer(conn, shop_id, customer or "")
    if cust is None:
        return f"No one named {customer} is on the book."
    try:
        amount = int(amount_cents)
    except (TypeError, ValueError):
        return "How much did they pay?"
    if amount <= 0:
        return "How much did they pay?"
    acct = _clean_account(account)
    payload = {"customer_id": cust["id"], "customer": cust["name"], "amount_cents": amount,
               "account": acct}
    with _dry_run(conn):
        _apply_record_payment(conn, shop_id, payload, _action_id())
        new_bal = balance_cents(conn, shop_id, cust["id"])
    preview = (f"📝 *Save payment?*\n• {cust['name']} paid {cents_to_rand(amount)}\n"
               f"• New balance {cents_to_rand(new_bal)}")
    if acct != "cash":
        preview += f"\n• Into {ACCOUNTS[acct]}"
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
    what = payload["category"].capitalize()
    return (
        f"✅ *Expense saved*\n• {what} {cents_to_rand(payload['amount_cents'])}"
        f" from {ACCOUNTS[payload['account']]}\n"
        f"• {ACCOUNTS[payload['account']]} {cents_to_rand(left)}",
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
        return "How much?"
    if amount <= 0:
        return "How much?"
    cat, acct = _clean_category(category), _clean_account(account)
    payload = {"amount_cents": amount, "category": cat, "account": acct,
               "note": (note or "").strip()}
    with _dry_run(conn):
        _apply_record_expense(conn, shop_id, payload, _action_id())
        left = account_balance(conn, shop_id, acct)
    what = cat.capitalize()
    return _propose(
        conn, shop_id, "record_expense", payload,
        f"📝 *Save expense?*\n• {what} {cents_to_rand(amount)} from {ACCOUNTS[acct]}\n"
        f"• {ACCOUNTS[acct]} {cents_to_rand(left + amount)} → {cents_to_rand(left)}",
    )


def _apply_record_drawing(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    _post_cash(conn, shop_id, payload["account"], -payload["amount_cents"], "drawing",
               action_id, note=payload.get("note") or "")
    left = account_balance(conn, shop_id, payload["account"])
    return (
        f"✅ *Drawing saved*\n• Drawing {cents_to_rand(payload['amount_cents'])}"
        f" from {ACCOUNTS[payload['account']]}\n• {ACCOUNTS[payload['account']]}"
        f" {cents_to_rand(left)}",
        "drawing", f"drawing {cents_to_rand(payload['amount_cents'])}", {},
    )


def record_drawing(conn: sqlite3.Connection, shop_id: str, amount_cents: int,
                   account: str | None = None, note: str | None = None) -> str:
    ensure_shop(conn, shop_id)
    try:
        amount = int(amount_cents)
    except (TypeError, ValueError):
        return "How much?"
    if amount <= 0:
        return "How much?"
    acct = _clean_account(account)
    payload = {"amount_cents": amount, "account": acct, "note": (note or "").strip()}
    with _dry_run(conn):
        _apply_record_drawing(conn, shop_id, payload, _action_id())
        left = account_balance(conn, shop_id, acct)
    return _propose(
        conn, shop_id, "record_drawing", payload,
        f"📝 *Save drawing?*\n• Drawing {cents_to_rand(amount)} from {ACCOUNTS[acct]}\n"
        f"• {ACCOUNTS[acct]} {cents_to_rand(left + amount)} → {cents_to_rand(left)}",
    )


def _count_line(counted: int, expected: int) -> str:
    if counted == expected:
        return "• ✅ Matches the book exactly."
    diff = counted - expected
    word = "over" if diff > 0 else "short"
    return (f"• Book says {cents_to_rand(expected)}"
            f" — {cents_to_rand(abs(diff))} {word}")


def _apply_record_count(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    diff = payload["counted_cents"] - payload["expected_cents"]
    if diff:
        _post_cash(conn, shop_id, "cash", diff, "count", action_id,
                   note="over" if diff > 0 else "short")
    return (
        f"✅ *Count saved*\n• Counted {cents_to_rand(payload['counted_cents'])}\n"
        + _count_line(payload["counted_cents"], payload["expected_cents"]),
        "count", f"count {cents_to_rand(payload['counted_cents'])}", {},
    )


def record_count(conn: sqlite3.Connection, shop_id: str, amount_cents: int) -> str:
    """Record a physical cash count ("counted R1,240") and show over/short."""
    ensure_shop(conn, shop_id)
    try:
        counted = int(amount_cents)
    except (TypeError, ValueError):
        return "How much did you count?"
    if counted < 0:
        return "How much did you count?"
    expected = account_balance(conn, shop_id, "cash")
    payload = {"counted_cents": counted, "expected_cents": expected}
    return _propose(
        conn, shop_id, "record_count", payload,
        f"📝 *Save cash count?*\n• Counted {cents_to_rand(counted)}\n"
        + _count_line(counted, expected),
    )


def _apply_supplier_payment(conn: sqlite3.Connection, shop_id: str, payload: dict,
                            action_id: str):
    _post_cash(conn, shop_id, payload["account"], -payload["amount_cents"], "purchase",
               action_id, note=f"paid {payload['supplier']}")
    _post_supplier(conn, shop_id, payload["supplier"], -payload["amount_cents"],
                   action_id, note="payment")
    owed = supplier_balance(conn, shop_id, payload["supplier"])
    return (
        f"✅ *Supplier paid*\n• {payload['supplier']} {cents_to_rand(payload['amount_cents'])}"
        f" from {ACCOUNTS[payload['account']]}\n• Still owed {cents_to_rand(owed)}",
        "supplier_payment", f"supplier {payload['supplier']}", {},
    )


def record_supplier_payment(conn: sqlite3.Connection, shop_id: str, supplier: str,
                            amount_cents: int, account: str | None = None) -> str:
    ensure_shop(conn, shop_id)
    name = (supplier or "").strip()
    if not name:
        return "Which supplier?"
    try:
        amount = int(amount_cents)
    except (TypeError, ValueError):
        return "How much?"
    if amount <= 0:
        return "How much?"
    acct = _clean_account(account)
    owed = supplier_balance(conn, shop_id, name)
    payload = {"supplier": name, "amount_cents": amount, "account": acct}
    return _propose(
        conn, shop_id, "record_supplier_payment", payload,
        f"📝 *Pay supplier?*\n• {name} {cents_to_rand(amount)} from {ACCOUNTS[acct]}\n"
        f"• Owed {cents_to_rand(owed)} → {cents_to_rand(owed - amount)}",
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
    first = f"• {label} = {cents_to_rand(payload['total_cents'])}"
    prow = conn.execute("SELECT pack_name, pack_size FROM products WHERE id = ?",
                        (payload["product_id"],)).fetchone()
    left = f"• Stock left {_fmt_on_hand(stock, prow['pack_name'], prow['pack_size'])}"
    if payload.get("buyer"):
        bal = balance_cents(conn, shop_id, customer_id)
        return (
            f"✅ *Sale saved*\n{first}\n• {payload['buyer']} owes"
            f" {cents_to_rand(payload['total_cents'])} more\n• Balance {cents_to_rand(bal)}"
            f"\n{left}",
            "sale_credit",
            f"sale {payload['product']} to {payload['buyer']}",
            {"sale_id": sale_id, "credit_id": credit_id,
             "product_id": payload["product_id"], "qty": payload["qty"]},
        )
    return (
        f"✅ *Sale saved*\n{first}\n{left}",
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
        return "How many?"
    if qty <= 0:
        return "How many?"
    buyer_name = buyer.strip() if buyer and buyer.strip() else None
    line, status, detail = _resolve_priced_line(conn, shop_id, product or "", qty, unit)
    if status == "ambiguous":
        return detail
    if status in ("unknown", "unpriced"):
        _store_await_price(conn, shop_id, detail,
                           {"tool": "sale", "qty": qty, "buyer": buyer_name, "unit": unit})
        if status == "unknown":
            cap = detail[:1].upper() + detail[1:]
            tail = (f" and I'll add it and record {buyer_name}'s credit." if buyer_name else
                    " and I'll add it and record the sale.")
            return (f"📝 *{cap} isn't on your list yet.*\nReply with the price, e.g."
                    f" '{detail.lower()} R18',{tail}")
        if _is_pack_unit(unit):
            return (f"{_ask_price(detail)}\nSay one price, e.g. 'R2 each'"
                    f" — or the pack price, e.g. '{_pack_echo(unit)} R60'.")
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
    first = f"• {line['label']} = {cents_to_rand(line['line_total'])}"
    left = f"• Stock left {_fmt_on_hand(stock, line['pack_name'], line['pack_size'])}"
    if buyer_name:
        preview = (f"📝 *Save sale?*\n{first} to {buyer_name}\n"
                   f"• Balance {cents_to_rand(buyer_bal)}\n{left}")
        if buyer_new:
            preview += "\n• New customer."
        else:
            cust = _get_customer(conn, shop_id, buyer_name)
            if cust is not None:
                preview += _limit_warn(conn, shop_id, cust["id"], buyer_bal)
    else:
        preview = f"📝 *Save sale?*\n{first} cash\n{left}"
    if line["warn"]:
        preview += (f"\n• ⚠️ No {line['unit_word']} size for {line['name']}"
                    " — counted as singles.")
    if stock < 0:
        preview += "\n• ⚠️ Count says stock is short."
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
        bits.append(f"cost {cents_to_rand(int(round(cost)))}")
    if price is not None:
        bits.append(f"sell {cents_to_rand(price)}")
    if pack is not None:
        bits.append(f"{pack_name} {cents_to_rand(pack)}")
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
        return f"{packs} {word} + {_fmt_qty(loose)} loose ({total})"
    if packs:
        word = pack_name if packs == 1 else _plural_pack(pack_name or "pack")
        return f"{packs} {word} ({total})"
    return total


def _fmt_low_line(name: str, units: float, pack_name: str | None,
                  pack_size: float | None) -> str:
    if units <= 0:
        return f"• {name} — out of stock ❌"
    return f"• {name} — {_fmt_on_hand(units, pack_name, pack_size)} left"


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
            msg = (f"✅ *Stock saved*\n• {payload['name']} added:"
                   f" {hand} on hand{extras}")
        else:
            msg = (f"✅ *Stock saved*\n• {payload['name']} added:"
                   f" {hand} on hand\n• ❓ What is the price?")
        return (msg, "stock", f"stock new {payload['name']}",
                {"product_id": product_id, "qty": payload["qty"], "created": True})
    return (
        f"✅ *Stock saved*\n• {payload['name']}: {hand} on hand{extras}",
        "stock", f"stock {payload['name']} +{_fmt_qty(payload['qty'])}",
        {"product_id": product_id, "qty": payload["qty"], "created": False,
         "old_wholesaler": old_wholesaler, "old_cost": old_cost, "old_price": old_price,
         "old_pack": old_pack},
    )


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
        return "How many?"
    if not name:
        return "Which product?"
    if qty <= 0:
        return "How many?"
    prod, ambiguous = _find_products(conn, shop_id, name)
    if prod is None and ambiguous:
        return _which_one(ambiguous)
    if prod is None and _is_pack_unit(unit):
        return (f"❓ *{name} isn't on the shelf list.*\n"
                f"Add it as singles first? e.g. 'add 200 {name.lower()}'.")
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
            preview = f"📝 *Add stock?*\n• New: {name} × {_fmt_qty(units)}{extras}"
        else:
            preview = (f"📝 *Add stock?*\n• New: {name} × {_fmt_qty(units)}\n"
                       "• Price still needed.")
    elif is_pack:
        preview = (f"📝 *Add stock?*\n• +{_fmt_qty(qty)} {_plural_pack(pack_name)}"
                   f" {prod['name']} ({_fmt_qty(units)} units)\n"
                   f"• Stock {old_hand} → {new_hand}{extras}")
    else:
        preview = (f"📝 *Add stock?*\n• +{_fmt_qty(units)} {prod['name']}\n"
                   f"• Stock {old_hand} → {new_hand}{extras}")
    if no_pack:
        preview += (f"\n• ⚠️ No {_pack_echo(unit)} size for {prod['name']}"
                    " — counted as singles.")
    if cost is not None:
        total_cost = int(round(cost * units))
        if acct == "supplier":
            preview += (f"\n• On {(payload['wholesaler'] or 'supplier')} account"
                        f" {cents_to_rand(total_cost)}")
        else:
            preview += f"\n• Paid {cents_to_rand(total_cost)} from {ACCOUNTS[acct]}"
    if cost is not None and price is not None and cost > price:
        preview += "\n• ⚠️ Cost is higher than sell price."
    return _propose(conn, shop_id, "add_stock", payload, preview)


def _apply_set_price(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    column = "pack_price_cents" if payload.get("for_pack") else "price_cents"
    conn.execute(f"UPDATE products SET {column} = ? WHERE id = ?",
                 (payload["price_cents"], payload["product_id"]))
    old = payload["old_price"]
    new = cents_to_rand(payload["price_cents"])
    what = f"{payload['name']} {payload.get('pack_name')}" if payload.get("for_pack") else payload["name"]
    msg = f"✅ *Price saved*\n• {what} is now {new}." if old is None else \
        f"✅ *Price saved*\n• {what} {cents_to_rand(old)} → {new}."
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
        return (f"❓ *{(product or '').strip()} isn't on the shelf list.*\n"
                "Add it to stock first?")
    if for_pack and not prod["pack_size"]:
        return f"❓ {prod['name']} has no pack size. Set the single price instead?"
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
    preview = f"📝 *Set price?*\n• {what} → {cents_to_rand(price)}" if old is None else \
        f"📝 *Set price?*\n• {what} {cents_to_rand(old)} → {cents_to_rand(price)}"
    return _propose(conn, shop_id, "set_price", payload, preview)


# ---------------------------------------------------------------------------
# Write tools: photo findings (structured data comes from vision in Step 7)
# ---------------------------------------------------------------------------


def _expiry_line(item: dict) -> str:
    detail = f"• {_fmt_qty(item['qty'])} × {item['product']} {_fmt_date(item['expiry_date'])}"
    if item["expiry_date"] < today_str():
        return detail + " — expired. Take them off the shelf."
    return detail + " — expires soon. Sell first."


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
    saved = "✅ *Expiry note saved*\n" + "\n".join(_expiry_line(i) for i in flagged)
    return saved, "expiry", "expiry note", {"item_ids": ids}


def _soon_iso() -> str:
    return (date.fromisoformat(today_str()) + timedelta(days=7)).isoformat()


def check_expiry_photo(conn: sqlite3.Connection, shop_id: str, findings: list[dict],
                       photo_hash: str | None = None) -> str:
    """Propose storing expiry findings. findings: [{product, qty, expiry_date YYYY-MM-DD}]."""
    ensure_shop(conn, shop_id)
    if not findings:
        return "I could not read any dates. Try a closer photo of the date stamp."
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
            return (f"❓ I could not read the date for {name or 'that item'}.\n"
                    "What is it (YYYY-MM-DD)?")
        if not name or qty <= 0:
            return "Tell me each item, how many, and the date on it."
        items.append({"product": name, "qty": qty, "expiry_date": exp})
    flagged = [i for i in items if i["expiry_date"] <= _soon_iso()]
    if not flagged:
        return "✅ All dates look fine. Nothing expiring soon."
    payload = {"items": items, "photo_hash": photo_hash}
    with _dry_run(conn):
        _apply_expiry_note(conn, shop_id, payload, _action_id())
    preview = "⚠️ *Expiry check*\n" + "\n".join(_expiry_line(i) for i in flagged)
    return _propose(conn, shop_id, "expiry_note", payload, preview + "\n\nSave this note?")


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
    return (f"✅ *Slip saved*\n• {n} item{'s' if n != 1 else ''} added to stock.", "slip",
            f"slip {n} items", {"moves": moves, "old_costs": old_costs})


def read_slip_photo(conn: sqlite3.Connection, shop_id: str, items: list[dict],
                    wholesaler: str | None = None) -> str:
    """Propose adding slip lines to stock. items: [{product, qty, cost_cents?}]."""
    ensure_shop(conn, shop_id)
    if not items:
        return "I could not read the slip. Try a flatter photo in good light."
    resolved = []
    for item in items:
        name = str(item.get("product", "")).strip()
        try:
            qty = float(item.get("qty", 0))
        except (TypeError, ValueError):
            qty = 0
        if not name or qty <= 0:
            return "Tell me each slip line and how many."
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
    lines = ["📝 *Save slip to stock?*"]
    for item in resolved:
        line = f"• +{_fmt_qty(item['qty'])} {item['name']} (stock {_fmt_qty(stocks[item['name']])})"
        if item["cost_cents"] is not None:
            line += f" at cost {cents_to_rand(item['cost_cents'])}"
        if item["product_id"] is None:
            line += " [new]"
        lines.append(line)
    return _propose(conn, shop_id, "read_slip", payload, "\n".join(lines))


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


def get_balance(conn: sqlite3.Connection, shop_id: str, customer: str) -> str:
    ensure_shop(conn, shop_id)
    cust = _get_customer(conn, shop_id, customer or "")
    if cust is None:
        return f"No one named {customer} is on the book."
    bal = balance_cents(conn, shop_id, cust["id"])
    if bal > 0:
        return f"💳 *{cust['name']}* owes {cents_to_rand(bal)}."
    if bal == 0:
        return f"✅ *{cust['name']}* owes nothing. The book is clean."
    return f"✅ *{cust['name']}* is paid up with {cents_to_rand(-bal)} credit."


def list_debtors(conn: sqlite3.Connection, shop_id: str) -> str:
    ensure_shop(conn, shop_id)
    rows = conn.execute("SELECT * FROM customers WHERE shop_id = ? ORDER BY name", (shop_id,)).fetchall()
    debtors = [(r["name"], balance_cents(conn, shop_id, r["id"])) for r in rows]
    debtors = [(n, b) for n, b in debtors if b > 0]
    if not debtors:
        return "✅ Nobody owes anything. The book is clean."
    debtors.sort(key=lambda t: -t[1])
    today = today_str()
    lines = ["💳 *On the book*"]
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
                    age = f" ⚠️ {days}d old"
        lines.append(f"• {n} — {cents_to_rand(b)}{age}")
    total = sum(b for _, b in debtors)
    return "\n".join(lines) + f"\n\nTotal: {cents_to_rand(total)}."


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
        return "Say it like 'add staff 0821234567 Thandi'."
    if digits == re.sub(r"\D", "", shop_id):
        return "That's your own number — you're already the owner."
    conn.execute("INSERT INTO staff (shop_id, phone, name) VALUES (?, ?, ?)"
                 " ON CONFLICT (shop_id, phone) DO UPDATE SET name = excluded.name",
                 (shop_id, digits, (name or "").strip()[:40] or digits))
    conn.commit()
    return (f"✅ *Staff added*\n• {(name or '').strip() or digits} ({digits})"
            " can record sales, credit and book photos.")


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
        return ("⛔ *Owners only*\nStaff can record sales, credit and book photos"
                " — profit, reports and settings stay with the owner.")
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
            who = r["recorded_by"] or "Owner"
            t = tallies.setdefault(who, {"n": 0, "s": 0})
            t["n"] += r["n"]
            t["s"] += r["s"]
    if not tallies:
        return "📝 *Who recorded today*\n\nNothing recorded yet today."
    lines = ["📝 *Who recorded today*"]
    for who in sorted(tallies):
        t = tallies[who]
        lines.append(f"• {who}: {t['n']} entries, {cents_to_rand(t['s'])}")
    return "\n".join(lines)


def set_expiry(conn: sqlite3.Connection, shop_id: str, product: str,
               when: str) -> str:
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    day = parse_expiry_date(when or "")
    if day is None:
        return "When does it expire? Say 'Friday', 'tomorrow' or a date."
    prod, _ = _find_products(conn, shop_id, product or "")
    qty = prod["stock_qty"] if prod else 0
    name = prod["name"] if prod else (product or "").strip()
    if not name:
        return "Which item?"
    conn.execute(
        "INSERT INTO expiry_items (shop_id, product_name, qty, expiry_date, note)"
        " VALUES (?, ?, ?, ?, 'owner')",
        (shop_id, name, qty, day))
    conn.commit()
    return (f"⏰ *Expiry noted*\n• {name} {_fmt_date(day)}\n"
            "• I'll warn you 2 days before.")


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
        return "🔕 *Books reminder off*\n• I'll stay quiet in the evenings."
    if word == "on":
        conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'nudge_off'",
                     (shop_id,))
        conn.commit()
        return "🔔 *Books reminder on*\n• I'll nudge you every evening."
    if not _re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", word):
        return "Say it like 'nudge at 20:00', 'nudge off' or 'nudge on'."
    conn.execute("INSERT INTO flags (shop_id, key, value) VALUES (?, 'nudge_time', ?)"
                 " ON CONFLICT (shop_id, key) DO UPDATE SET value = excluded.value",
                 (shop_id, word))
    conn.commit()
    return f"🔔 *Books reminder at {word}*\n• I'll ask for the book photo then."


def set_customer_limit(conn: sqlite3.Connection, shop_id: str, customer: str,
                       amount_cents: int) -> str:
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    cust = _get_customer(conn, shop_id, customer or "")
    if cust is None:
        return f"No one named {(customer or '').strip()} is on the book."
    try:
        amount = int(amount_cents)
    except (TypeError, ValueError):
        return "What limit? Say it like 'set Sipho limit R300'."
    if amount <= 0:
        return "What limit? Say it like 'set Sipho limit R300'."
    conn.execute("UPDATE customers SET credit_limit_cents = ? WHERE id = ?",
                 (amount, cust["id"]))
    conn.commit()
    return (f"✅ *Limit saved*\n• {cust['name']} may owe up to {cents_to_rand(amount)}")


def _limit_warn(conn: sqlite3.Connection, shop_id: str, cust_id: int,
                new_bal: int) -> str:
    row = conn.execute("SELECT name, credit_limit_cents FROM customers WHERE id = ?",
                       (cust_id,)).fetchone()
    if row is None or row["credit_limit_cents"] is None:
        return ""
    if new_bal > row["credit_limit_cents"]:
        return (f"\n• ⚠️ Over {row['name']}'s {cents_to_rand(row['credit_limit_cents'])}"
                " limit.")
    return ""


def draft_reminder(conn: sqlite3.Connection, shop_id: str, customer: str) -> str:
    import os as _os

    from app import reports as _reports
    ensure_shop(conn, shop_id)
    cust = _get_customer(conn, shop_id, customer or "")
    if cust is None:
        return f"No one named {customer} is on the book."
    bal = balance_cents(conn, shop_id, cust["id"])
    if bal <= 0:
        return f"{cust['name']} owes nothing, no reminder needed."
    shop = ensure_shop(conn, shop_id)["name"]
    out = (f"✉️ *Reminder for {cust['name']}*\n\nForward this yourself:\n\n"
           f"\"Hi {cust['name']}, a reminder of {cents_to_rand(bal)} on the book"
           f" at {shop}. Please pay when you can. Thank you!\"")
    data = _reports.statement_data(conn, shop_id, cust["name"])
    base = _os.environ.get("SISI_PUBLIC_URL", "").rstrip("/")
    if data is not None and base:
        _reports.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        _queue_statement(conn, shop_id, data, _reports.shop_brand(conn, shop_id),
                         base)
        out += "\n\n📎 Statement card below — forward it too."
    return out


_CATEGORY_ORDER = ["Bread", "Dairy", "Drinks", "Groceries", "Household", "Airtime"]
_CATEGORY_HEADS = {"Bread": "🍞 *Bread*", "Dairy": "🥛 *Dairy*", "Drinks": "🥤 *Drinks*",
                   "Groceries": "🛒 *Groceries*", "Household": "🧼 *Household*",
                   "Airtime": "📱 *Airtime & data*"}


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
    head = f"📦 *{shop['name']}: stock on hand*"
    if not rows:
        return (f"{head}\n\nThe shelves are empty — nothing in stock yet.\n\n"
                "Add some like this: 'add 20 bread at R18'.")
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["category"] or "Other", []).append(r)
    order = [c for c in _CATEGORY_ORDER if c in groups]
    order += sorted(set(groups) - set(_CATEGORY_ORDER) - {"Other"})
    if "Other" in groups:
        order.append("Other")
    value = sum((r["price_cents"] or 0) * max(0, r["stock_qty"]) for r in rows)
    n = len(rows)
    blocks = [f"{head}\n{n} item{'s' if n != 1 else ''} · worth {cents_to_rand(int(value))}"]
    for cat in order:
        lines = [_CATEGORY_HEADS.get(cat, f"🏷️ *{cat}*")]
        for r in groups[cat]:
            hand = _fmt_on_hand(r["stock_qty"], r["pack_name"], r["pack_size"])
            if r["price_cents"] is not None:
                line = f"• {r['name']}: {hand} × {cents_to_rand(r['price_cents'])}"
                if r["pack_size"]:
                    line += " each"
            else:
                line = f"• {r['name']}: {hand} (no price)"
            lines.append(line + _stock_flag(r["stock_qty"], r["reorder_level"]))
        blocks.append("\n".join(lines))
    low = [r for r in rows if r["stock_qty"] <= r["reorder_level"]]
    if low:
        lines = ["⚠️ *Running low*"]
        for r in sorted(low, key=lambda r: r["name"]):
            lines.append(_fmt_low_line(r["name"], r["stock_qty"],
                                       r["pack_name"], r["pack_size"]))
        blocks.append("\n".join(lines))
    else:
        blocks.append("✅ Nothing is running low.")
    blocks.append("Reply 'restock' for an order list")
    return "\n\n".join(blocks)


def _best_seller_lines(conn: sqlite3.Connection, shop_id: str, days: int) -> list[str]:
    cutoff = ((datetime.now(timezone.utc) - timedelta(days=days))
              .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")
    rows = conn.execute(
        "SELECT product_name, SUM(qty) q, SUM(total_cents) t FROM sales"
        " WHERE shop_id = ? AND reversed = 0 AND created_at >= ?"
        " GROUP BY product_name ORDER BY t DESC LIMIT 5",
        (shop_id, cutoff)).fetchall()
    return [f"{i}. {r['product_name']} — {_fmt_qty(r['q'])} sold, {cents_to_rand(r['t'])}"
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
        return f"No sales in the last {days} days yet."
    return f"⭐ *Top sellers (last {days} days)*\n" + "\n".join(lines)


def low_stock(conn: sqlite3.Connection, shop_id: str) -> str:
    ensure_shop(conn, shop_id)
    rows = conn.execute(
        "SELECT * FROM products WHERE shop_id = ? AND stock_qty <= reorder_level ORDER BY name",
        (shop_id,)).fetchall()
    if not rows:
        return "✅ Stock looks fine. Nothing is low."
    lines = ["⚠️ *Low stock*"]
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
        return "✅ Nothing to buy. Stock is fine."
    groups: dict[str, list[str]] = {}
    for r in rows:
        need = max(1, int(r["reorder_level"] * 2 - r["stock_qty"]))
        have = _fmt_qty(r["stock_qty"])
        if r["pack_size"]:
            packs = math.ceil(need / r["pack_size"])
            word = r["pack_name"] if packs == 1 else _plural_pack(r["pack_name"])
            groups.setdefault(r["wholesaler"] or "No wholesaler", []).append(
                f"• {r['name']} — have {have}, order {packs} {word}")
        else:
            groups.setdefault(r["wholesaler"] or "No wholesaler", []).append(
                f"• {r['name']} — have {have}, order {need}")
    blocks = ["🛒 *Buy tomorrow*"]
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
        return f"💰 *Cash-up {day}*\n\nNothing recorded yet."
    out = (f"💰 *Cash-up {day}*\n"
           f"• Cash sales {cents_to_rand(cash['s'])} ({cash['n']} sales)\n"
           f"• Book sales {cents_to_rand(book)}\n"
           f"• Payments in {cents_to_rand(pay)}\n\n"
           f"💵 *In the drawer:* {cents_to_rand(cash['s'] + pay)}")
    sellers = _best_seller_lines(conn, shop_id, 7)
    if sellers:
        out += "\n\n⭐ *Top sellers (7 days)*\n" + "\n".join(sellers)
    return out


COMPLIANCE_TOPICS = {
    "overview": (
        "🏛️ *Spaza compliance*\n"
        "Info only, not legal advice.\n"
        "\n"
        "• Municipal business registration\n"
        "• Trading permit from the municipality\n"
        "• Certificate of Acceptability (food safety)\n"
        "• Spaza Shop Support Fund: up to R100,000 — spazashopfund.co.za\n"
        "\n"
        "⚠️ Applications are FREE. Nobody needs to pay an agent.\n"
        "\n"
        "More: gov.za, thedtic.gov.za, sanews.gov.za"
    ),
    "register": (
        "🏛️ *Municipal registration*\n"
        "Every spaza must register at its municipality.\n"
        "\n"
        "Ask your municipal office how to register now and what papers to bring.\n"
        "\n"
        "More: gov.za"
    ),
    "permit": (
        "🏛️ *Trading permit*\n"
        "Issued by your municipality after registration checks.\n"
        "\n"
        "Only about 15,000 of 44,696 verified shops hold permits, so apply early.\n"
        "\n"
        "More: gov.za"
    ),
    "coa": (
        "🏛️ *Certificate of Acceptability*\n"
        "Food-safety certificate from municipal Environmental Health.\n"
        "\n"
        "You need it before selling food. Ask your municipality for an inspection.\n"
        "\n"
        "More: gov.za"
    ),
    "fund": (
        "💰 *Spaza Shop Support Fund*\n"
        "Up to R100,000 per shop, gated by registration paperwork.\n"
        "\n"
        "Apply only at spazashopfund.co.za.\n"
        "\n"
        "⚠️ Applications are FREE. Nobody needs to pay an agent."
        " Report anyone asking for payment to the police."
    ),
    "scam": (
        "⚠️ *Scam warning*\n"
        "Government warns of fake paid application assistants.\n"
        "\n"
        "Applications are FREE. Nobody needs to pay an agent.\n"
        "\n"
        "Deal only with municipal offices and official sites (gov.za, spazashopfund.co.za)."
    ),
}


def compliance_guide(conn: sqlite3.Connection, shop_id: str, topic: str = "overview") -> str:
    ensure_shop(conn, shop_id)
    key = (topic or "overview").strip().lower()
    aliases = {"registration": "register", "permit": "permit", "acceptability": "coa",
               "certificate": "coa", "fund": "fund", "spaza fund": "fund",
               "scam": "scam", "scams": "scam", "overview": "overview"}
    return COMPLIANCE_TOPICS.get(aliases.get(key, "overview"), COMPLIANCE_TOPICS["overview"])


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
    return ("⚠️ *Reset shop?*\n• This erases ALL your stock, books and reports.\n"
            "• Reply 'yes erase everything' to go ahead, or anything else to keep it.")


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
    ):
        conn.execute(f"DELETE FROM {table} WHERE shop_id = ?", (shop_id,))
    conn.execute(
        "UPDATE shops SET consent_given_at = NULL, language = 'en', address = '',"
        " phone = '', logo_path = '', logo_orig_path = '' WHERE shop_id = ?",
        (shop_id,),
    )
    conn.commit()
    return ("✅ *Done.*\nI erased everything I had for this number."
            " Send anything to start fresh.")


# ---------------------------------------------------------------------------
# Undo: reverse the last confirmed action exactly (LIFO, seed rows excluded)
# ---------------------------------------------------------------------------


def _undo_credit(conn: sqlite3.Connection, shop_id: str, undo: dict) -> str:
    row = conn.execute(
        "SELECT e.*, c.name FROM credit_entries e JOIN customers c ON c.id = e.customer_id"
        " WHERE e.id = ?", (undo["credit_id"],)).fetchone()
    conn.execute("UPDATE credit_entries SET reversed = 1 WHERE id = ?", (undo["credit_id"],))
    bal = balance_cents(conn, shop_id, row["customer_id"])
    return (f"Undone: removed {row['name']}'s {cents_to_rand(row['amount_cents'])} credit."
            f" Balance back to {cents_to_rand(bal)}.")


def _undo_payment(conn: sqlite3.Connection, shop_id: str, undo: dict, action_id: str) -> str:
    row = conn.execute(
        "SELECT p.*, c.name FROM payments p JOIN customers c ON c.id = p.customer_id"
        " WHERE p.id = ?", (undo["payment_id"],)).fetchone()
    conn.execute("UPDATE payments SET reversed = 1 WHERE id = ?", (undo["payment_id"],))
    _reverse_moves(conn, shop_id, action_id)
    bal = balance_cents(conn, shop_id, row["customer_id"])
    return (f"Undone: removed {row['name']}'s {cents_to_rand(row['amount_cents'])} payment."
            f" Balance back to {cents_to_rand(bal)}.")


def _undo_expense(conn: sqlite3.Connection, shop_id: str, undo: dict, action_id: str) -> str:
    row = conn.execute("SELECT * FROM expenses WHERE id = ?", (undo["expense_id"],)).fetchone()
    conn.execute("UPDATE expenses SET reversed = 1 WHERE id = ?", (undo["expense_id"],))
    _reverse_moves(conn, shop_id, action_id)
    return (f"Undone: removed the {cents_to_rand(row['amount_cents'])}"
            f" {row['category']} expense.")


def _undo_cash_only(conn: sqlite3.Connection, shop_id: str, undo: dict, action_id: str,
                    kind: str) -> str:
    _reverse_moves(conn, shop_id, action_id)
    return f"Undone: removed the {kind}."


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
        return "Sales last 7 days", start, today
    if p in ("month", "this month", "last 30 days"):
        return "Sales this month", today[:7] + "-01", today
    return "Sales today", today, today


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
        return f"📊 *{title}*\n\nNothing sold yet."
    n = cash["n"]
    return (f"📊 *{title}*\n"
            f"• Cash {cents_to_rand(cash['s'])} ({n} sale{'s' if n != 1 else ''})\n"
            f"• Credit {cents_to_rand(book)}\n"
            f"• Total {cents_to_rand(cash['s'] + book)}")


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
    label = date(int(m[:4]), int(m[5:7]), 1).strftime("%B %Y")
    return (f"📈 *Profit for {label}*\n"
            f"• Sales {cents_to_rand(sales)}\n"
            f"• Cost of sales {cents_to_rand(cogs)}\n"
            f"• Gross {cents_to_rand(gross)}{margin}\n"
            f"• Expenses {cents_to_rand(exp)}\n"
            f"• Net {cents_to_rand(gross - exp)}")


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
                 note: str = "") -> None:
    conn.execute("INSERT OR IGNORE INTO photo_hashes (shop_id, hash, kind, note)"
                 " VALUES (?, ?, ?, ?)", (shop_id, digest, kind, note))
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
        return "flagged", {"line_no": line_no, "text": text, "reason": "unreadable"}
    if kind == "payment":
        if not cust:
            return "flagged", {"line_no": line_no, "text": text,
                               "reason": "no name — who paid?"}
        if not price_cents:
            return "flagged", {"line_no": line_no, "text": text,
                               "reason": "no amount — how much did they pay?"}
        row = _get_customer(conn, shop_id, cust)
        if row is None:
            return "flagged", {"line_no": line_no, "text": text,
                               "reason": f"no one named {cust} is on the book"}
        return "payment", {"line_no": line_no, "customer": row["name"], "amount": price_cents}
    if not (item or "").strip():
        return "flagged", {"line_no": line_no, "text": text, "reason": "unreadable"}
    prod, ambiguous = _resolve_book_item(conn, shop_id, item)
    if prod is None:
        if ambiguous:
            names = ", ".join(p["name"] for p in ambiguous[:2])
            return "flagged", {"line_no": line_no, "text": text,
                               "reason": f"which one: {names}?"}
        return "flagged", {"line_no": line_no, "text": text, "reason": "not on shelf list"}
    if prod["price_cents"] is None:
        return "flagged", {"line_no": line_no, "text": text,
                           "reason": f"{prod['name']} has no price yet"}
    units = qty if qty > 0 else 1
    list_total = int(round(prod["price_cents"] * units))
    total = price_cents if price_cents else list_total
    entry = {"line_no": line_no, "product_id": prod["id"], "name": prod["name"],
             "units": units, "total": total, "list_total": list_total,
             "short": _book_short(units, prod["name"])}
    if kind == "credit":
        if not cust:
            return "flagged", {"line_no": line_no, "text": text,
                               "reason": "no name — who took it?"}
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
    blocks = [f"📒 *Today's book: {payload['display_date']}*"]
    if cash:
        lines = ["💵 *Cash sales*"]
        for e in cash:
            line = f"• {e['short']} = {cents_to_rand(e['total'])}"
            if e["total"] != e["list_total"]:
                line += f" ⚠️ (list {cents_to_rand(e['list_total'])})"
            lines.append(line)
        blocks.append("\n".join(lines))
    credit_lines = []
    for e in credit:
        row = _get_customer(conn, shop_id, e["customer"])
        bal = balance_cents(conn, shop_id, row["id"]) + e["total"] if row else e["total"]
        who = f"{e['customer']} (new)" if e["new_customer"] else e["customer"]
        credit_lines.append(f"• {who}: {e['short']} = {cents_to_rand(e['total'])}"
                            f" → owes {cents_to_rand(bal)}")
    for e in payments:
        row = _get_customer(conn, shop_id, e["customer"])
        bal = balance_cents(conn, shop_id, row["id"]) - e["amount"] if row else -e["amount"]
        credit_lines.append(f"• {e['customer']} paid {cents_to_rand(e['amount'])}"
                            f" → owes {cents_to_rand(bal)}")
    if credit_lines:
        blocks.append("📝 *Credit*\n" + "\n".join(credit_lines))
    if flagged:
        lines = ["❓ *Couldn't read*"]
        for e in flagged:
            lines.append(f"• line {e['line_no']} '{e['text'][:60]}' — {e['reason']}")
        blocks.append("\n".join(lines))
    cash_total = sum(e["total"] for e in cash)
    credit_total = sum(e["total"] for e in credit)
    pay_total = sum(e["amount"] for e in payments)
    expected = account_balance(conn, shop_id, "cash") + cash_total + pay_total
    blocks.append("💰 *Totals*\n"
                  f"• Cash sales {cents_to_rand(cash_total)}\n"
                  f"• Credit given {cents_to_rand(credit_total)}\n"
                  f"• Credit payments {cents_to_rand(pay_total)}\n"
                  f"• Expected cash in hand {cents_to_rand(expected)}")
    if payload.get("page_total") is not None:
        written = cash_total + credit_total
        if written == payload["page_total"]:
            blocks.append(f"✅ Matches your page total {cents_to_rand(written)}")
        else:
            blocks.append(f"⚠️ Page says {cents_to_rand(payload['page_total'])},"
                          f" lines add to {cents_to_rand(written)}")
    if payload.get("crossed_out"):
        n = payload["crossed_out"]
        blocks.append(f"Skipped {n} crossed-out line{'s' if n != 1 else ''}.")
    blocks.append("Reply Yes to save, or correct a line, e.g. 'line 3 is 4 bread'")
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
        out = "❓ Nothing to save from this page."
        if payload["flagged"]:
            out += "\n" + "\n".join(f"• line {e['line_no']} '{e['text'][:60]}'"
                                    f" — {e['reason']}" for e in payload["flagged"])
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
        parts.append(f"Cash {cents_to_rand(cash_total)}")
    if credit_total:
        parts.append(f"credit {cents_to_rand(credit_total)}")
    if pay_total:
        parts.append(f"payments {cents_to_rand(pay_total)}")
    msg = ("✅ *Book saved*\n• " + " · ".join(parts) + "\n\n"
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
    return f"Undone: removed the book page from {undo.get('display_date', 'that day')}."


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
        return "No book preview to correct."
    payload = json.loads(pend["payload_json"])
    if line_no < 1 or line_no > (payload.get("line_count") or 0):
        return f"Line {line_no} isn't on this page."
    parsed = _parse_correction(rest)
    if parsed is None:
        return "I couldn't read that correction. Try 'line 3 is 4 bread'."
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
                                         "reason": f"which one: {names}?"}
        elif prod["price_cents"] is None:
            section, entry = "flagged", {"line_no": line_no, "text": rest,
                                         "reason": f"{prod['name']} has no price yet"}
        else:
            total = int(round(prod["price_cents"] * parsed["qty"]))
            entry = {"line_no": line_no, "product_id": prod["id"], "name": prod["name"],
                     "units": parsed["qty"], "total": total, "list_total": total,
                     "short": _book_short(parsed["qty"], prod["name"])}
            if kind == "credit":
                if not customer:
                    return "Which customer? e.g. 'line 3 is 4 bread Sipho'."
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
        return "flagged", {"line_no": line_no, "text": text, "reason": "unreadable"}
    prod, ambiguous = _resolve_book_item(conn, shop_id, item)
    if prod is None and not ambiguous:
        for word, cat in RECEIPT_EXPENSE_WORDS.items():
            if word in item.lower():
                total = ln.get("line_total_cents")
                if total is None:
                    return "flagged", {"line_no": line_no, "text": text,
                                       "reason": "no amount I can use"}
                return "expense", {"line_no": line_no, "category": cat, "total": total}
        return "stock", _receipt_stock_entry(None, item, ln, line_no, text)
    if prod is None:
        names = ", ".join(p["name"] for p in ambiguous[:2])
        return "flagged", {"line_no": line_no, "text": text,
                           "reason": f"which one: {names}?"}
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
        return {"line_no": line_no, "text": text, "reason": "pack size unknown",
                "unresolved": True}
    if units is None:
        lt = ln.get("line_total_cents")
        uc = ln.get("unit_cost_cents")
        if lt and uc:
            units = round(lt / uc, 2)
    if units is None:
        return {"line_no": line_no, "text": text, "reason": "no quantity I can use",
                "unresolved": True}
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
    blocks = [f"🧾 *Purchase: {payload['supplier']}*"]
    lines = []
    for e in stock:
        if e.get("unresolved"):
            flagged.append({"line_no": e["line_no"], "text": e.get("text", ""),
                            "reason": e.get("reason", "")})
            continue
        bit = f"• +{_fmt_qty(e['units'])} {e['name']}"
        if e.get("pack_word"):
            bit += f" ({e['pack_word']})"
        if e.get("new"):
            bit += " [new]"
        avg = _receipt_avg_cost(conn, shop_id, e)
        if avg is None:
            bit += " @ ?"
        else:
            bit += f" @ {cents_to_rand(int(round(avg)))}"
            if not e.get("new"):
                old = conn.execute("SELECT cost_cents FROM products WHERE id = ?",
                                   (e["product_id"],)).fetchone()["cost_cents"]
                if old is not None and int(round(avg)) != int(round(old)):
                    bit += f" (was {cents_to_rand(int(round(old)))})"
        have = 0 if e.get("new") else conn.execute(
            "SELECT stock_qty FROM products WHERE id = ?", (e["product_id"],)).fetchone()["stock_qty"]
        bit += f" → stock {_fmt_qty(have)} → {_fmt_qty(have + e['units'])}"
        lines.append(bit)
    for e in expenses:
        lines.append(f"• {e['category'].capitalize()} {cents_to_rand(e['total'])} → expenses")
    if lines:
        blocks.append("\n".join(lines))
    if flagged:
        blocks.append("❓ *Couldn't read*\n" + "\n".join(
            f"• line {e['line_no']} '{e.get('text', '')[:60]}' — {e['reason']}"
            for e in sorted(flagged, key=lambda e: e["line_no"])))
    computed = sum(e["total"] for e in stock if not e.get("unresolved")
                   and e["total"] is not None)
    computed += sum(e["total"] for e in expenses)
    tail = []
    if payload.get("total") is not None:
        if computed == payload["total"]:
            tail.append(f"• Total {cents_to_rand(computed)} ✅")
        else:
            tail.append(f"• ⚠️ Lines add to {cents_to_rand(computed)},"
                        f" slip says {cents_to_rand(payload['total'])}")
    if payload.get("vat") is not None:
        tail.append(f"• Incl. VAT {cents_to_rand(payload['vat'])}")
    acct = payload["account"]
    label = payload["supplier"] + " account" if acct == "supplier" else ACCOUNTS[acct]
    tail.append(f"• Paid from: {label}")
    if payload.get("account_guessed"):
        tail[-1] += " (say 'from …' to change)"
    blocks.append("\n".join(tail))
    blocks.append("Reply Yes to save, or 'line 2 is ...' / 'from petty cash' to correct.")
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
            return (f"🧾 Already filed {supplier} {receipt_no}"
                    f" ({cents_to_rand(total or 0)}). Send a different slip.")
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
        out = "❓ Nothing to save from this slip."
        if payload["flagged"]:
            out += "\n" + "\n".join(f"• line {e['line_no']} '{e.get('text', '')[:60]}'"
                                    f" — {e['reason']}" for e in payload["flagged"])
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
        money_line = f"• Owed {payload['supplier']} {cents_to_rand(total)}"
    else:
        _post_cash(conn, shop_id, acct, -total, "purchase", action_id,
                   note=f"receipt {payload['supplier']}")
        money_line = f"• {ACCOUNTS[acct]} {cents_to_rand(account_balance(conn, shop_id, acct))}"
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
        bits.append(f"{n_stock} stock")
    if n_exp:
        bits.append(f"{n_exp} expense")
    msg = (f"✅ *Receipt saved*\n• {payload['supplier']} {cents_to_rand(total)}"
           f" — {', '.join(bits)}\n{money_line}")
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
    return f"Undone: removed the {undo.get('supplier', 'supplier')} receipt."


def correct_receipt_line(conn: sqlite3.Connection, shop_id: str, line_no: int, rest: str) -> str:
    pend = get_pending(conn, shop_id)
    if pend is None or pend["action_type"] != "receipt":
        return "No receipt preview to correct."
    payload = json.loads(pend["payload_json"])
    if line_no < 1 or line_no > (payload.get("line_count") or 0):
        return f"Line {line_no} isn't on this slip."
    toks = (rest or "").split()
    try:
        qty = float(toks[0])
        toks = toks[1:]
    except (ValueError, IndexError):
        return "Say the units too, e.g. 'line 2 is 12 coke'."
    if qty <= 0 or not toks:
        return "Say the units too, e.g. 'line 2 is 12 coke'."
    cost = None
    if len(toks) >= 2:
        m = re.fullmatch(r"@?r?(\d+(?:\.\d{1,2})?)", toks[-1])
        if m:
            cost = int(round(float(m.group(1)) * 100))
            toks = toks[:-1]
    if not toks:
        return "Say the units too, e.g. 'line 2 is 12 coke'."
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
        return "No receipt preview to correct."
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
    label = date(int(m[:4]), int(m[5:7]), 1).strftime("%B %Y")
    if not rows:
        return f"No receipts filed for {label}."
    short = {"cash": "till", "petty": "petty", "bank": "bank", "supplier": "supplier"}
    lines = [f"🧾 *Receipts: {label}*"]
    for r in rows:
        no = f" {r['receipt_no']}" if r["receipt_no"] else ""
        lines.append(f"• {_fmt_date(r['created_at'][:10])} {r['supplier']}{no}"
                     f" {cents_to_rand(r['total_cents'] or 0)}"
                     f" ({short.get(r['account'], r['account'])})")
    lines.append(f"\nTotal {cents_to_rand(sum(r['total_cents'] or 0 for r in rows))}")
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
        return "What should I call your shop?"
    conn.execute("UPDATE shops SET name = ? WHERE shop_id = ?", (name[:60], shop_id))
    conn.commit()
    return f"✅ *Shop name saved*\n• {name[:60]}"


def set_shop_address(conn: sqlite3.Connection, shop_id: str, address: str) -> str:
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    conn.execute("UPDATE shops SET address = ? WHERE shop_id = ?",
                 ((address or "").strip()[:120], shop_id))
    conn.commit()
    return "✅ *Address saved*"


def set_shop_phone(conn: sqlite3.Connection, shop_id: str, phone: str) -> str:
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    conn.execute("UPDATE shops SET phone = ? WHERE shop_id = ?",
                 ((phone or "").strip()[:30], shop_id))
    conn.commit()
    return "✅ *Shop phone saved*"


def show_shop_profile(conn: sqlite3.Connection, shop_id: str) -> str:
    blocked = _staff_blocked()
    if blocked:
        return blocked
    import os as _os

    from app import reports as _reports

    from app.agent import queue_media as _queue
    brand = _reports.shop_brand(conn, shop_id)
    lines = ["🏪 *Shop profile*", f"• {brand['name']}"]
    if brand["address"]:
        lines.append(f"• {brand['address']}")
    if brand["phone"]:
        lines.append(f"• {brand['phone']}")
    lines.append("• Logo: yes ✅" if brand["logo"] else "• Logo: not yet")
    if not brand["logo"]:
        lines.append("Send your logo as a photo with caption 'logo'.")
    _reports.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
    card = str(_reports.REPORTS_DIR / f"{safe}-profile.png")
    _reports.render_shop_card(brand, card)
    token = _reports.store_card(conn, shop_id, "profile", card)
    base = _os.environ.get("SISI_PUBLIC_URL", "").rstrip("/")
    if base:
        _queue(shop_id, [{"kind": "image",
                          "url": f"{base}/media/{token}?f=png",
                          "caption": "Your shop header", "filename": ""}])
        lines.append("\n📎 Header preview below.")
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
        done = _logo.process_upload(image or b"")
    except Exception:
        return "❌ That file is not a photo. Send your logo as a photo."
    if done["original"] is None:
        return "❌ That file is not a photo. Send your logo as a photo."
    _reports.ensure_dirs()
    safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
    orig_path = str(_reports.LOGOS_DIR / f"{safe}-orig.png")
    cut_path = str(_reports.LOGOS_DIR / f"{safe}-cutout.png")
    with open(orig_path, "wb") as f:
        f.write(done["original"])
    if done["used_cutout"]:
        with open(cut_path, "wb") as f:
            f.write(done["cutout"])
        display = cut_path
        note = "I removed the background."
    else:
        badge_path = str(_reports.LOGOS_DIR / f"{safe}-badge.png")
        with open(badge_path, "wb") as f:
            f.write(done["badge"])
        display = badge_path
        note = "I kept it on a white badge so it stays readable."
    conn.execute("UPDATE shops SET logo_path = ?, logo_orig_path = ? WHERE shop_id = ?",
                 (display, orig_path, shop_id))
    conn.execute("INSERT INTO flags (shop_id, key, value) VALUES (?, 'logo-mode', 'cutout')"
                 " ON CONFLICT (shop_id, key) DO UPDATE SET value = 'cutout'",
                 (shop_id,))
    conn.execute("INSERT INTO flags (shop_id, key, value) VALUES (?, 'logo-review', '1')"
                 " ON CONFLICT (shop_id, key) DO UPDATE SET value = '1'",
                 (shop_id,))
    conn.commit()
    brand = _reports.shop_brand(conn, shop_id)
    out = (f"✅ *Logo saved*\n• {note}\n"
           "• Logo looks good? Reply Yes, or 'use original logo'.")
    base = _os.environ.get("SISI_PUBLIC_URL", "").rstrip("/")
    if base:
        card = str(_reports.REPORTS_DIR / f"{safe}-profile.png")
        _reports.render_shop_card(brand, card)
        token = _reports.store_card(conn, shop_id, "profile", card)
        _queue(shop_id, [{"kind": "image", "url": f"{base}/media/{token}?f=png",
                          "caption": "Your new header", "filename": ""}])
        out += "\n\n📎 Preview below."
    return out


def switch_logo(conn: sqlite3.Connection, shop_id: str, mode: str) -> str:
    """Switch the header logo between the cut-out and the original upload."""
    import os as _os

    from app import reports as _reports

    from app.agent import queue_media as _queue
    blocked = _staff_blocked()
    if blocked:
        return blocked
    ensure_shop(conn, shop_id)
    brand = _reports.shop_brand(conn, shop_id)
    if not brand["logo"] and not brand["logo_orig"]:
        return "No logo yet — send your logo as a photo with caption 'logo'."
    if mode == "original" and not brand["logo_orig"]:
        return "I only have one version of your logo."
    conn.execute("INSERT INTO flags (shop_id, key, value) VALUES (?, 'logo-mode', ?)"
                 " ON CONFLICT (shop_id, key) DO UPDATE SET value = excluded.value",
                 (shop_id, mode))
    conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'logo-review'",
                 (shop_id,))
    conn.commit()
    brand = _reports.shop_brand(conn, shop_id)
    what = "original" if mode == "original" else "background-free logo"
    _reports.ensure_dirs()
    out = f"✅ *Using your {what} on documents.*"
    base = _os.environ.get("SISI_PUBLIC_URL", "").rstrip("/")
    if base:
        safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
        card = str(_reports.REPORTS_DIR / f"{safe}-profile.png")
        _reports.render_shop_card(brand, card)
        token = _reports.store_card(conn, shop_id, "profile", card)
        _queue(shop_id, [{"kind": "image", "url": f"{base}/media/{token}?f=png",
                          "caption": "Your header", "filename": ""}])
        out += "\n\n📎 Preview below."
    return out


def confirm_logo(conn: sqlite3.Connection, shop_id: str) -> str | None:
    """Accept the fresh logo after the Yes-review. None when not reviewing."""
    row = conn.execute("SELECT value FROM flags WHERE shop_id = ? AND key = 'logo-review'",
                       (shop_id,)).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'logo-review'",
                 (shop_id,))
    conn.commit()
    return "✅ *Great — your logo will head your documents.*"


def _queue_statement(conn: sqlite3.Connection, shop_id: str, data: dict,
                     brand: dict, base: str) -> None:
    from app.agent import queue_media as _queue
    from app import reports as _reports
    safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
    slug = "".join(c.lower() if c.isalnum() else "-" for c in data["name"]).strip("-")
    prefix = str(_reports.REPORTS_DIR / f"{safe}-statement-{slug}")
    pages = _reports.render_statement_pngs(data, brand, prefix)
    pdf = f"{prefix}.pdf"
    _reports.render_statement_pdf(data, brand, pdf)
    media = []
    for n, page in enumerate(pages, 1):
        token = _reports.store_card(conn, shop_id, f"stmt-{slug}-p{n}", page)
        media.append({"kind": "image", "url": f"{base}/media/{token}?f=png",
                      "caption": f"Statement · {data['name']}"
                      + (f" ({n}/{len(pages)})" if len(pages) > 1 else ""),
                      "filename": ""})
    token = _reports.store_card(conn, shop_id, f"stmt-{slug}-pdf", pdf)
    media.append({"kind": "file", "url": f"{base}/media/{token}?f=pdf",
                  "caption": f"Statement PDF · {data['name']}",
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
        return f"No one named {(customer or '').strip()} is on the book."
    if data["total"] <= 0 and not data["items"]:
        return f"{data['name']} owes nothing — no statement needed."
    brand = _reports.shop_brand(conn, shop_id)
    _reports.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    sent = _queue_statement(conn, shop_id, data, brand,
                            _os.environ.get("SISI_PUBLIC_URL", "").rstrip("/"))
    tail = (f"Forward this to {data['name']} 👆" if sent
            else "PNG + PDF saved on file.")
    return (f"🧾 *Statement · {data['name']}*\n"
            f"• Outstanding {cents_to_rand(data['total'])}\n\n{tail}")


def statements_for_all(conn: sqlite3.Connection, shop_id: str) -> str:
    blocked = _staff_blocked()
    if blocked:
        return blocked
    import os as _os

    from app import reports as _reports
    ensure_shop(conn, shop_id)
    rows = conn.execute(
        "SELECT c.id, c.name, MIN(e.created_at) first FROM customers c"
        " JOIN credit_entries e ON e.customer_id = c.id AND e.reversed = 0"
        " WHERE c.shop_id = ? GROUP BY c.id ORDER BY first",
        (shop_id,)).fetchall()
    made = []
    brand = _reports.shop_brand(conn, shop_id)
    _reports.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    base = _os.environ.get("SISI_PUBLIC_URL", "").rstrip("/")
    for r in rows:
        data = _reports.statement_data(conn, shop_id, r["name"])
        if data is None or data["total"] <= 0:
            continue
        _queue_statement(conn, shop_id, data, brand, base)
        made.append(f"• {data['name']} — {cents_to_rand(data['total'])}")
    if not made:
        return "✅ Nobody owes anything. The book is clean."
    tail = "Forward each card to its customer 👆" if base else "Cards saved on file."
    return "🧾 *Statements for everyone who owes*\n" + "\n".join(made) + f"\n\n{tail}"


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
        groups.setdefault(ln["wholesaler"] or "No wholesaler", []).append(ln)
    blocks = ["📋 *Order draft*"]
    for w, items in groups.items():
        bl = [f"*{w}*"]
        for ln in items:
            if ln["packs"]:
                word = ln["pack_name"] if ln["packs"] == 1 else _plural_pack(ln["pack_name"])
                qty = f"{_fmt_qty(ln['packs'])} {word} ({_fmt_qty(ln['units'])} units)"
            else:
                qty = f"{_fmt_qty(ln['units'])} units"
            line = f"• {ln['name']} — {qty}"
            line += (f" ≈ {cents_to_rand(ln['est_cents'])}"
                     if ln["est_cents"] is not None else " (no cost yet)")
            bl.append(line)
        blocks.append("\n".join(bl))
    known = [ln["est_cents"] for ln in payload["lines"] if ln["est_cents"] is not None]
    unknown = len(payload["lines"]) - len(known)
    total = f"🛒 *Order total* ≈ {cents_to_rand(sum(known))}"
    if unknown:
        total += f" (excl. {unknown} without cost)"
    blocks.append(total)
    blocks.append("Say 'send it' to forward, or 'add …' / 'remove …' to edit.")
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
        return "✅ Nothing to order. Stock is fine."
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
        return "✅ Nothing to order. Stock is fine."
    payload = {"lines": lines, "sent": []}
    _save_order_draft(conn, shop_id, payload)
    return _render_order(shop["name"], payload)


def send_order(conn: sqlite3.Connection, shop_id: str, wholesaler: str | None = None) -> str:
    """Format one wholesaler's order lines as a forwardable message. Never sends it."""
    shop = ensure_shop(conn, shop_id)
    draft = get_order_draft(conn, shop_id)
    if not draft or not draft.get("lines"):
        return "No order draft. Say 'order stock' first."
    groups: dict[str, list] = {}
    for ln in draft["lines"]:
        groups.setdefault(ln["wholesaler"] or "No wholesaler", []).append(ln)
    if wholesaler:
        want = (wholesaler or "").strip().lower()
        match = next((g for g in groups if want in g.lower() or g.lower() in want), None)
        if match is None:
            return f"❓ Nothing for {wholesaler.strip()} on this order."
        groups = {match: groups[match]}
    if len(groups) > 1:
        return "Which wholesaler? Say 'send it Jumbo'."
    w, items = next(iter(groups.items()))
    if w in (draft.get("sent") or []):
        return "That order was already sent. Say 'order stock' to build a new one."
    draft["sent"] = sorted(set(draft.get("sent") or []) | {w})
    _save_order_draft(conn, shop_id, draft)
    lines = []
    for ln in items:
        if ln["packs"]:
            word = ln["pack_name"] if ln["packs"] == 1 else _plural_pack(ln["pack_name"])
            lines.append(f"• {ln['name']} x {_fmt_qty(ln['packs'])} {word}")
        else:
            lines.append(f"• {ln['name']} x {_fmt_qty(ln['units'])}")
    out = f"Hello {w}! Order from {shop['name']}:\n\n" + "\n".join(lines)
    known = [ln["est_cents"] for ln in items if ln["est_cents"] is not None]
    if known:
        out += f"\n\nTotal ± {cents_to_rand(sum(known))}"
    out += "\nThank you!"
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
    _reports.render_order_png(payload, _reports.shop_brand(conn, shop_id),
                              card, today_str())
    token = _reports.store_card(conn, shop_id, "order", card)
    _queue(shop_id, [{"kind": "image", "url": f"{base}/media/{token}?f=png",
                      "caption": "Order card — forward with the message",
                      "filename": ""}])


def edit_order_add(conn: sqlite3.Connection, shop_id: str, product: str,
                   qty: float, unit: str | None = None):
    shop = ensure_shop(conn, shop_id)
    draft = get_order_draft(conn, shop_id)
    if not draft:
        return "No order draft. Say 'order stock' first."
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        return "How many?"
    if qty <= 0:
        return "How many?"
    prod, ambiguous = _find_products(conn, shop_id, product or "")
    if prod is None:
        if ambiguous:
            return _which_one(ambiguous)
        return (f"❓ *{(product or '').strip()} isn't on the shelf list.*\n"
                "Add it to stock first?")
    units, is_pack, no_pack = _resolve_units(prod["pack_name"], prod["pack_size"], qty, unit)
    if no_pack:
        first = prod["name"].split()[0].lower()
        return (f"❓ {prod['name']} has no {_pack_echo(unit)} size."
                f" Add singles? e.g. 'add {first} 12'.")
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
        return "No order draft. Say 'order stock' first."
    prod, _ = _find_products(conn, shop_id, product or "")
    pid = prod["id"] if prod else None
    kept = [ln for ln in draft["lines"] if ln["product_id"] != pid]
    if len(kept) == len(draft["lines"]):
        return f"❓ {(product or '').strip()} isn't on the order. Say 'order stock' to see it."
    if not kept:
        conn.execute("DELETE FROM order_drafts WHERE shop_id = ?", (shop_id,))
        conn.commit()
        return "Order cleared. Say 'order stock' to rebuild."
    draft["lines"] = kept
    _save_order_draft(conn, shop_id, draft)
    return _render_order(shop["name"], draft)


def cash_balances(conn: sqlite3.Connection, shop_id: str) -> str:
    ensure_shop(conn, shop_id)
    bals = {a: account_balance(conn, shop_id, a) for a in ("cash", "petty", "bank")}
    return ("💰 *Cash*\n"
            f"• Till {cents_to_rand(bals['cash'])}\n"
            f"• Petty cash {cents_to_rand(bals['petty'])}\n"
            f"• Bank {cents_to_rand(bals['bank'])}\n\n"
            f"Total {cents_to_rand(sum(bals.values()))}")


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
            skipped.append(f"{ACCOUNTS[acct]} already has"
                           f" {cents_to_rand(account_balance(conn, shop_id, acct))}")
            continue
        _post_cash(conn, shop_id, acct, amount, "opening", f"float-{shop_id}-{acct}",
                   note="opening float")
        lines.append(f"• {ACCOUNTS[acct]} {cents_to_rand(amount)}")
    conn.commit()
    if not lines and not skipped:
        return "❓ How much is the float? e.g. 'float R500'."
    out = ""
    if lines:
        out = "✅ *Opening float set*\n" + "\n".join(lines)
    if skipped:
        tail = "; ".join(skipped) + ". Say 'counted R…' to correct it."
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
        return (f"Undone: removed {cust['name']}'s {cents_to_rand(sale['total_cents'])} sale."
                f" Balance back to {cents_to_rand(bal)}.")
    return (f"Undone: removed the {cents_to_rand(sale['total_cents'])} sale."
            f" Stock back to {_fmt_qty(stock)}.")


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
        conn.execute(f"UPDATE products SET {', '.join(sets)} WHERE id = ?", (*params, pid))
        return f"Undone: removed the stock added. {name} back to {_fmt_qty(stock)}."
    conn.execute("DELETE FROM stock_moves WHERE product_id = ?", (pid,))
    conn.execute("DELETE FROM products WHERE id = ?", (pid,))
    return f"Undone: removed {name} again."


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
        return f"Undone: {what} removed."
    return f"Undone: {what} back to {cents_to_rand(undo['old_price'])}."


def _undo_expiry(conn: sqlite3.Connection, shop_id: str, undo: dict) -> str:
    conn.execute(
        f"DELETE FROM expiry_items WHERE id IN ({','.join('?' * len(undo['item_ids']))})",
        undo["item_ids"],
    )
    return "Undone: removed the expiry note."


def _undo_slip(conn: sqlite3.Connection, shop_id: str, undo: dict, action_id: str) -> str:
    for move in undo["moves"]:
        _restore_stock(conn, shop_id, move["product_id"], -move["qty"], action_id)
        if move.get("created"):
            conn.execute("DELETE FROM stock_moves WHERE product_id = ?", (move["product_id"],))
            conn.execute("DELETE FROM products WHERE id = ?", (move["product_id"],))
    for pid, old_cost in (undo.get("old_costs") or {}).items():
        conn.execute("UPDATE products SET cost_cents = ? WHERE id = ?", (old_cost, int(pid)))
    return "Undone: removed the slip from stock."


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
        return "There is nothing to undo."
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
    elif kind == "price":
        msg = _undo_price(conn, shop_id, undo)
    elif kind == "expiry":
        msg = _undo_expiry(conn, shop_id, undo)
    elif kind == "slip":
        msg = _undo_slip(conn, shop_id, undo, row["action_id"])
    elif kind == "book":
        msg = _undo_book(conn, shop_id, undo, row["action_id"])
    elif kind == "receipt":
        msg = _undo_receipt(conn, shop_id, undo, row["action_id"])
    else:
        return "I cannot undo that one."
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
    "set_price": _apply_set_price,
    "expiry_note": _apply_expiry_note,
    "read_slip": _apply_slip,
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
