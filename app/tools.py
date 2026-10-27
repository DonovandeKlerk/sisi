"""Shop tools with dry-run preview + deterministic confirm/undo.

Write tools (add_credit, record_payment, record_sale, add_stock, set_price,
check_expiry_photo, read_slip_photo) run their writes inside a rolled-back
transaction to produce an exact preview, store a pending action, and only
execute for real when the owner replies Yes. Read tools answer directly.
"""

from __future__ import annotations

import contextlib
import json
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
    return ", ".join(f"{_fmt_qty(ln['qty'])}x {ln['name']}" for ln in lines)


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
    message, kind, summary, undo = apply(conn, shop_id, json.loads(row["payload_json"]), action_id)
    conn.execute(
        "INSERT INTO actions (action_id, shop_id, kind, summary, data_json) VALUES (?, ?, ?, ?, ?)",
        (action_id, shop_id, kind, summary, json.dumps(undo)),
    )
    conn.execute(
        "UPDATE pending_actions SET status = 'confirmed', resolved_at = ? WHERE id = ?",
        (now_iso(), row["id"]),
    )
    conn.commit()
    return message


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


def _resolve_priced_line(conn: sqlite3.Connection, shop_id: str, product: str, qty: float):
    """Resolve one purchasable line (never guess a price).

    Returns (line_or_None, status, detail) with status one of ok, ambiguous,
    unknown, unpriced. Callers pause unknown/unpriced lines for the owner.
    """
    prod, ambiguous = _find_products(conn, shop_id, product)
    if prod is None:
        if ambiguous:
            names = ", ".join(p["name"] for p in ambiguous[:3])
            return None, "ambiguous", f"Which one: {names}?"
        return None, "unknown", product
    if prod["price_cents"] is None:
        return None, "unpriced", prod["name"]
    return {
        "product_id": prod["id"],
        "name": prod["name"],
        "qty": qty,
        "price_cents": prod["price_cents"],
        "line_total": int(round(prod["price_cents"] * qty)),
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
        "INSERT INTO credit_entries (shop_id, customer_id, amount_cents, items_text, action_id)"
        " VALUES (?, ?, ?, ?, ?)",
        (shop_id, cust["id"], payload["total_cents"], _items_text(payload["lines"]), action_id),
    )
    bal = balance_cents(conn, shop_id, cust["id"])
    return (
        f"Saved. {cust['name']} now owes {cents_to_rand(bal)}.",
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
        line, status, detail = _resolve_priced_line(conn, shop_id, name, qty)
        if status == "ambiguous":
            return detail
        if status in ("unknown", "unpriced"):
            _store_await_price(conn, shop_id, detail,
                               {"tool": "credit", "customer": customer.strip(), "items": items})
            if status == "unknown":
                cap = detail[:1].upper() + detail[1:]
                return (f"{cap} isn't on your list yet. Reply with the price, e.g."
                        f" '{detail.lower()} R18', and I'll add it and record"
                        f" {customer.strip()}'s credit.")
            return f"What is the price of {detail}?"
        lines.append(line)
    total = sum(ln["line_total"] for ln in lines)
    payload = {"customer": customer.strip(), "lines": lines, "total_cents": total}
    is_new = _get_customer(conn, shop_id, customer) is None
    with _dry_run(conn):
        _apply_add_credit(conn, shop_id, payload, _action_id())
        cust = _get_customer(conn, shop_id, customer)
        new_bal = balance_cents(conn, shop_id, cust["id"])
    preview = (
        f"Save: {customer.strip()} +{cents_to_rand(total)} on the book"
        f" ({_items_text(lines)}). New balance {cents_to_rand(new_bal)}."
    )
    if is_new:
        preview += " New customer."
    return _propose(conn, shop_id, "add_credit", payload, preview)


def _apply_record_payment(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    cur = conn.execute(
        "INSERT INTO payments (shop_id, customer_id, amount_cents, action_id) VALUES (?, ?, ?, ?)",
        (shop_id, payload["customer_id"], payload["amount_cents"], action_id),
    )
    bal = balance_cents(conn, shop_id, payload["customer_id"])
    return (
        f"Saved. {payload['customer']} paid {cents_to_rand(payload['amount_cents'])}."
        f" Balance {cents_to_rand(bal)}.",
        "payment",
        f"payment {payload['customer']} {cents_to_rand(payload['amount_cents'])}",
        {"payment_id": cur.lastrowid},
    )


def record_payment(conn: sqlite3.Connection, shop_id: str, customer: str, amount_cents: int) -> str:
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
    payload = {"customer_id": cust["id"], "customer": cust["name"], "amount_cents": amount}
    with _dry_run(conn):
        _apply_record_payment(conn, shop_id, payload, _action_id())
        new_bal = balance_cents(conn, shop_id, cust["id"])
    return _propose(
        conn, shop_id, "record_payment", payload,
        f"Save: {cust['name']} paid {cents_to_rand(amount)}. New balance {cents_to_rand(new_bal)}.",
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
            "INSERT INTO credit_entries (shop_id, customer_id, amount_cents, items_text, action_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (shop_id, customer_id, payload["total_cents"],
             f"{_fmt_qty(payload['qty'])}x {payload['product']} (sale)", action_id),
        )
        credit_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO sales (shop_id, product_id, product_name, qty, total_cents, customer_id, action_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (shop_id, payload["product_id"], payload["product"], payload["qty"],
         payload["total_cents"], customer_id, action_id),
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
    if payload.get("buyer"):
        bal = balance_cents(conn, shop_id, customer_id)
        return (
            f"Saved. {payload['buyer']} owes {cents_to_rand(payload['total_cents'])} more."
            f" Balance {cents_to_rand(bal)}.",
            "sale_credit",
            f"sale {payload['product']} to {payload['buyer']}",
            {"sale_id": sale_id, "credit_id": credit_id,
             "product_id": payload["product_id"], "qty": payload["qty"]},
        )
    return (
        f"Saved sale: {cents_to_rand(payload['total_cents'])} cash. Stock left {_fmt_qty(stock)}.",
        "sale_cash",
        f"sale {payload['product']} cash",
        {"sale_id": sale_id, "product_id": payload["product_id"], "qty": payload["qty"]},
    )


def record_sale(conn: sqlite3.Connection, shop_id: str, product: str, qty: float = 1,
                buyer: str | None = None) -> str:
    """Propose a sale. buyer=None means cash; a name means credit sale."""
    ensure_shop(conn, shop_id)
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        return "How many?"
    if qty <= 0:
        return "How many?"
    buyer_name = buyer.strip() if buyer and buyer.strip() else None
    line, status, detail = _resolve_priced_line(conn, shop_id, product or "", qty)
    if status == "ambiguous":
        return detail
    if status in ("unknown", "unpriced"):
        _store_await_price(conn, shop_id, detail,
                           {"tool": "sale", "qty": qty, "buyer": buyer_name})
        if status == "unknown":
            cap = detail[:1].upper() + detail[1:]
            tail = (f" and I'll add it and record {buyer_name}'s credit." if buyer_name else
                    " and I'll add it and record the sale.")
            return (f"{cap} isn't on your list yet. Reply with the price, e.g."
                    f" '{detail.lower()} R18',{tail}")
        return f"What is the price of {detail}?"
    payload = {"product_id": line["product_id"], "product": line["name"], "qty": qty,
               "total_cents": line["line_total"], "buyer": buyer_name}
    buyer_new = bool(buyer_name) and _get_customer(conn, shop_id, buyer_name) is None
    with _dry_run(conn):
        _apply_record_sale(conn, shop_id, payload, _action_id())
        stock = conn.execute("SELECT stock_qty FROM products WHERE id = ?",
                             (line["product_id"],)).fetchone()["stock_qty"]
        buyer_bal = None
        if buyer_name:
            cust = _get_customer(conn, shop_id, buyer_name)
            buyer_bal = balance_cents(conn, shop_id, cust["id"])
    if buyer_name:
        preview = (f"Save sale: {_fmt_qty(qty)} x {line['name']} ="
                   f" {cents_to_rand(line['line_total'])} to {buyer_name} on the book?"
                   f" Their balance {cents_to_rand(buyer_bal)}. Stock left {_fmt_qty(stock)}.")
        if buyer_new:
            preview += " New customer."
    else:
        preview = (f"Save sale: {_fmt_qty(qty)} x {line['name']} ="
                   f" {cents_to_rand(line['line_total'])} cash? Stock left {_fmt_qty(stock)}.")
    if stock < 0:
        preview += " (Count says stock is short.)"
    return _propose(conn, shop_id, "record_sale", payload, preview)


def _coerce_pos_int(val) -> int | None:
    try:
        val = int(val)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def _fmt_price_extras(cost: int | None, price: int | None) -> str:
    bits = []
    if cost is not None:
        bits.append(f"cost {cents_to_rand(cost)}")
    if price is not None:
        bits.append(f"sell {cents_to_rand(price)}")
    return f" ({', '.join(bits)})" if bits else ""


def _apply_add_stock(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    cost, price = payload.get("cost_cents"), payload.get("price_cents")
    if payload["product_id"] is None:
        cur = conn.execute(
            "INSERT INTO products (shop_id, name, wholesaler, cost_cents, price_cents)"
            " VALUES (?, ?, ?, ?, ?)",
            (shop_id, payload["name"], payload.get("wholesaler") or "", cost, price),
        )
        product_id, created, old_wholesaler = cur.lastrowid, True, ""
        old_cost = old_price = None
    else:
        product_id, created = payload["product_id"], False
        row = conn.execute("SELECT wholesaler, cost_cents, price_cents FROM products WHERE id = ?",
                           (product_id,)).fetchone()
        old_wholesaler, old_cost, old_price = row["wholesaler"], row["cost_cents"], row["price_cents"]
        if payload.get("wholesaler") and not old_wholesaler:
            conn.execute("UPDATE products SET wholesaler = ? WHERE id = ?",
                         (payload["wholesaler"], product_id))
        if cost is not None:
            conn.execute("UPDATE products SET cost_cents = ? WHERE id = ?", (cost, product_id))
        if price is not None:
            conn.execute("UPDATE products SET price_cents = ? WHERE id = ?", (price, product_id))
    conn.execute("UPDATE products SET stock_qty = stock_qty + ? WHERE id = ?",
                 (payload["qty"], product_id))
    conn.execute(
        "INSERT INTO stock_moves (shop_id, product_id, delta_qty, reason, action_id)"
        " VALUES (?, ?, ?, 'restock', ?)",
        (shop_id, product_id, payload["qty"], action_id),
    )
    stock = conn.execute("SELECT stock_qty FROM products WHERE id = ?",
                         (product_id,)).fetchone()["stock_qty"]
    extras = _fmt_price_extras(cost, price)
    if created:
        msg = (f"Saved. {payload['name']} added with stock {_fmt_qty(stock)}{extras}."
               if extras else
               f"Saved. {payload['name']} added with stock {_fmt_qty(stock)}. What is the price?")
        return (msg, "stock", f"stock new {payload['name']}",
                {"product_id": product_id, "qty": payload["qty"], "created": True})
    return (
        f"Saved. {payload['name']} stock is now {_fmt_qty(stock)}{extras}.",
        "stock", f"stock {payload['name']} +{_fmt_qty(payload['qty'])}",
        {"product_id": product_id, "qty": payload["qty"], "created": False,
         "old_wholesaler": old_wholesaler, "old_cost": old_cost, "old_price": old_price},
    )


def add_stock(conn: sqlite3.Connection, shop_id: str, product: str, qty: float,
              wholesaler: str | None = None, cost_cents: int | None = None,
              price_cents: int | None = None) -> str:
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
        names = ", ".join(p["name"] for p in ambiguous[:3])
        return f"Which one: {names}?"
    cost, price = _coerce_pos_int(cost_cents), _coerce_pos_int(price_cents)
    payload = {"product_id": prod["id"] if prod else None, "name": prod["name"] if prod else name,
               "qty": qty, "wholesaler": (wholesaler or "").strip(),
               "cost_cents": cost, "price_cents": price}
    with _dry_run(conn):
        _apply_add_stock(conn, shop_id, payload, _action_id())
        pid = conn.execute("SELECT id FROM products WHERE shop_id = ? AND name = ?",
                           (shop_id, payload["name"])).fetchone()["id"]
        stock = conn.execute("SELECT stock_qty FROM products WHERE id = ?",
                             (pid,)).fetchone()["stock_qty"]
    extras = _fmt_price_extras(cost, price)
    if prod is None:
        preview = (f"Save: new product {name}, stock {_fmt_qty(qty)}{extras}." if extras else
                   f"Save: new product {name}, stock {_fmt_qty(qty)}. Price still needed.")
    else:
        preview = f"Save: +{_fmt_qty(qty)} {prod['name']} stock. New stock {_fmt_qty(stock)}{extras}."
    if cost is not None and price is not None and cost > price:
        preview += " Note: cost is higher than sell price."
    return _propose(conn, shop_id, "add_stock", payload, preview)


def _apply_set_price(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    conn.execute("UPDATE products SET price_cents = ? WHERE id = ?",
                 (payload["price_cents"], payload["product_id"]))
    old = payload["old_price"]
    new = cents_to_rand(payload["price_cents"])
    msg = f"Saved. {payload['name']} price is now {new}." if old is None else \
        f"Saved. {payload['name']} price {cents_to_rand(old)} → {new}."
    return msg, "price", f"price {payload['name']} {new}", \
        {"product_id": payload["product_id"], "old_price": old}


def set_price(conn: sqlite3.Connection, shop_id: str, product: str, price_cents: int) -> str:
    ensure_shop(conn, shop_id)
    prod, ambiguous = _find_products(conn, shop_id, product or "")
    if prod is None:
        if ambiguous:
            names = ", ".join(p["name"] for p in ambiguous[:3])
            return f"Which one: {names}?"
        return f"I don't have {product} on the shelf list. Add it to stock first?"
    try:
        price = int(price_cents)
    except (TypeError, ValueError):
        return f"What is the price of {prod['name']}?"
    if price <= 0:
        return f"What is the price of {prod['name']}?"
    payload = {"product_id": prod["id"], "name": prod["name"],
               "price_cents": price, "old_price": prod["price_cents"]}
    old = prod["price_cents"]
    preview = f"Set {prod['name']} price to {cents_to_rand(price)}?" if old is None else \
        f"Change {prod['name']} price {cents_to_rand(old)} → {cents_to_rand(price)}?"
    return _propose(conn, shop_id, "set_price", payload, preview)


# ---------------------------------------------------------------------------
# Write tools: photo findings (structured data comes from vision in Step 7)
# ---------------------------------------------------------------------------


def _apply_expiry_note(conn: sqlite3.Connection, shop_id: str, payload: dict, action_id: str):
    ids = []
    for item in payload["items"]:
        cur = conn.execute(
            "INSERT INTO expiry_items (shop_id, product_name, qty, expiry_date, action_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (shop_id, item["product"], item["qty"], item["expiry_date"], action_id),
        )
        ids.append(cur.lastrowid)
    return payload["warnings"], "expiry", "expiry note", {"item_ids": ids}


def check_expiry_photo(conn: sqlite3.Connection, shop_id: str, findings: list[dict]) -> str:
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
            return f"I could not read the date for {name or 'that item'}. What is it (YYYY-MM-DD)?"
        if not name or qty <= 0:
            return "Tell me each item, how many, and the date on it."
        items.append({"product": name, "qty": qty, "expiry_date": exp})
    today = today_str()
    soon = (date.fromisoformat(today) + timedelta(days=7)).isoformat()
    warnings = []
    for item in items:
        detail = f"{_fmt_qty(item['qty'])} x {item['product']} {_fmt_date(item['expiry_date'])}"
        if item["expiry_date"] < today:
            warnings.append(f"{detail} has expired. Please take them off the shelf.")
        elif item["expiry_date"] <= soon:
            warnings.append(f"{detail} expires soon. Sell first.")
    if not warnings:
        return "All dates look fine. Nothing expiring soon."
    payload = {"items": items, "warnings": " ".join(warnings) + " Noted."}
    with _dry_run(conn):
        _apply_expiry_note(conn, shop_id, payload, _action_id())
    return _propose(conn, shop_id, "expiry_note",
                    payload, " ".join(warnings) + " Save this expiry note?")


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
    return (f"Saved slip: {n} item{'s' if n != 1 else ''} added to stock.", "slip",
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
            names = ", ".join(p["name"] for p in ambiguous[:3])
            return f"Which one: {names}?"
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
    lines = []
    for item in resolved:
        line = f"+{_fmt_qty(item['qty'])} {item['name']} (stock {_fmt_qty(stocks[item['name']])})"
        if item["cost_cents"] is not None:
            line += f" at cost {cents_to_rand(item['cost_cents'])}"
        if item["product_id"] is None:
            line += " [new]"
        lines.append(line)
    return _propose(conn, shop_id, "read_slip",
                    payload, "Save slip to stock? " + "; ".join(lines) + ".")


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
        return f"{cust['name']} owes {cents_to_rand(bal)}."
    if bal == 0:
        return f"{cust['name']} owes nothing. The book is clean."
    return f"{cust['name']} is paid up with {cents_to_rand(-bal)} credit."


def list_debtors(conn: sqlite3.Connection, shop_id: str) -> str:
    ensure_shop(conn, shop_id)
    rows = conn.execute("SELECT * FROM customers WHERE shop_id = ? ORDER BY name", (shop_id,)).fetchall()
    debtors = [(r["name"], balance_cents(conn, shop_id, r["id"])) for r in rows]
    debtors = [(n, b) for n, b in debtors if b > 0]
    if not debtors:
        return "Nobody owes anything. The book is clean."
    debtors.sort(key=lambda t: -t[1])
    lines = [f"{n} — {cents_to_rand(b)}" for n, b in debtors]
    total = sum(b for _, b in debtors)
    return "On the book:\n" + "\n".join(lines) + f"\nTotal: {cents_to_rand(total)}."


def draft_reminder(conn: sqlite3.Connection, shop_id: str, customer: str) -> str:
    ensure_shop(conn, shop_id)
    cust = _get_customer(conn, shop_id, customer or "")
    if cust is None:
        return f"No one named {customer} is on the book."
    bal = balance_cents(conn, shop_id, cust["id"])
    if bal <= 0:
        return f"{cust['name']} owes nothing, no reminder needed."
    shop = ensure_shop(conn, shop_id)["name"]
    return (f"Here is a draft for {cust['name']} — forward it yourself:\n"
            f"\"Hi {cust['name']}, I hope you are well. A reminder of"
            f" {cents_to_rand(bal)} on the book at {shop}."
            f" Please pay when you can. Thank you!\"")


_CATEGORY_ORDER = ["Bread", "Dairy", "Drinks", "Groceries", "Household", "Airtime"]


def list_stock(conn: sqlite3.Connection, shop_id: str) -> str:
    ensure_shop(conn, shop_id)
    rows = conn.execute("SELECT * FROM products WHERE shop_id = ? ORDER BY name",
                        (shop_id,)).fetchall()
    if not rows:
        return ("The shelves are empty — nothing in stock yet.\n"
                "Add some like this: 'add 20 bread at R18'.")
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["category"] or "Other", []).append(r)
    order = [c for c in _CATEGORY_ORDER if c in groups]
    order += sorted(set(groups) - set(_CATEGORY_ORDER) - {"Other"})
    if "Other" in groups:
        order.append("Other")
    blocks = []
    for cat in order:
        lines = [f"*{cat}*"]
        for r in groups[cat]:
            line = f"{r['name']} — {_fmt_qty(r['stock_qty'])}"
            line += (f" @ {cents_to_rand(r['price_cents'])}"
                     if r["price_cents"] is not None else " (no price)")
            if r["stock_qty"] <= r["reorder_level"]:
                line += " · LOW"
            lines.append(line)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def best_sellers(conn: sqlite3.Connection, shop_id: str, days: int = 7) -> str:
    ensure_shop(conn, shop_id)
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 7
    if days <= 0:
        days = 7
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    rows = conn.execute(
        "SELECT product_name, SUM(qty) q, SUM(total_cents) t FROM sales"
        " WHERE shop_id = ? AND reversed = 0 AND created_at >= ?"
        " GROUP BY product_name ORDER BY t DESC LIMIT 5",
        (shop_id, cutoff)).fetchall()
    if not rows:
        return f"No sales in the last {days} days yet."
    lines = [f"{i}. {r['product_name']} — {_fmt_qty(r['q'])} sold, {cents_to_rand(r['t'])}"
             for i, r in enumerate(rows, 1)]
    return f"Top sellers (last {days} days):\n" + "\n".join(lines)


def low_stock(conn: sqlite3.Connection, shop_id: str) -> str:
    ensure_shop(conn, shop_id)
    rows = conn.execute(
        "SELECT * FROM products WHERE shop_id = ? AND stock_qty <= reorder_level ORDER BY name",
        (shop_id,)).fetchall()
    if not rows:
        return "Stock looks fine. Nothing is low."
    lines = [f"• {r['name']} — {_fmt_qty(r['stock_qty'])} left" for r in rows]
    return "Low stock:\n" + "\n".join(lines)


def restock_list(conn: sqlite3.Connection, shop_id: str) -> str:
    ensure_shop(conn, shop_id)
    rows = conn.execute(
        "SELECT * FROM products WHERE shop_id = ? AND stock_qty <= reorder_level"
        " ORDER BY wholesaler, name",
        (shop_id,)).fetchall()
    if not rows:
        return "Nothing to buy. Stock is fine."
    groups: dict[str, list[str]] = {}
    for r in rows:
        need = max(1, int(r["reorder_level"] * 2 - r["stock_qty"]))
        groups.setdefault(r["wholesaler"] or "No wholesaler", []).append(
            f"{r['name']} (have {_fmt_qty(r['stock_qty'])}, order {need})")
    lines = [f"{w}: " + "; ".join(items) for w, items in groups.items()]
    return "Buy tomorrow:\n" + "\n".join(lines)


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
        return f"Cash-up {day}: nothing recorded yet."
    return (f"Cash-up {day}:\n"
            f"Cash sales: {cents_to_rand(cash['s'])} ({cash['n']} sales)\n"
            f"Book sales: {cents_to_rand(book)}\n"
            f"Payments in: {cents_to_rand(pay)}\n"
            f"In the drawer: {cents_to_rand(cash['s'] + pay)}.")


COMPLIANCE_TOPICS = {
    "overview": (
        "Sisi compliance helper (info only, not legal advice):\n"
        "1. Municipal business registration at your local municipality\n"
        "2. Trading permit from the municipality\n"
        "3. Certificate of Acceptability (food safety) from Environmental Health\n"
        "4. Spaza Shop Support Fund: up to R100,000 per shop — apply at spazashopfund.co.za\n"
        "Warning: applications are FREE. Nobody needs to pay an agent.\n"
        "More: gov.za, thedtic.gov.za, sanews.gov.za."
    ),
    "register": (
        "Municipal registration: every spaza must register with its municipality.\n"
        "Ask your municipal office how to register now and what papers to bring.\n"
        "More: gov.za."
    ),
    "permit": (
        "Trading permit: issued by your municipality after registration checks.\n"
        "Only about 15,000 of 44,696 verified shops hold permits, so apply early.\n"
        "More: gov.za."
    ),
    "coa": (
        "Certificate of Acceptability: food-safety certificate from municipal"
        " Environmental Health. You need it before selling food.\n"
        "Ask your municipality for an Environmental Health inspection.\n"
        "More: gov.za."
    ),
    "fund": (
        "Spaza Shop Support Fund: up to R100,000 per shop, gated by registration paperwork.\n"
        "Apply only at spazashopfund.co.za.\n"
        "Warning: applications are FREE. Nobody needs to pay an agent."
        " Report anyone asking for payment to the police."
    ),
    "scam": (
        "Scam warning: government warns of fake paid application assistants.\n"
        "Applications are FREE. Nobody needs to pay an agent.\n"
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
    """Wipe one shop and reseed demo data. Demo command, not a model tool."""
    from seed.seed_demo import DEMO_SHOP_NAME, seed_demo
    from seed.seed_random import DEMO_NUMBER, SHOP_NAME, seed_random_shop

    if shop_id == DEMO_NUMBER:
        seed_random_shop(shop_id, conn)
        return f"Fresh start. {SHOP_NAME} is loaded again."
    seed_demo(shop_id, conn)
    return f"Fresh start. {DEMO_SHOP_NAME} is loaded again."


def delete_my_data(conn: sqlite3.Connection, shop_id: str) -> str:
    """Erase everything stored for one shop (POPIA). Keeps only an empty shop row."""
    ensure_shop(conn, shop_id)
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
    ):
        conn.execute(f"DELETE FROM {table} WHERE shop_id = ?", (shop_id,))
    conn.execute(
        "UPDATE shops SET consent_given_at = NULL, language = 'en' WHERE shop_id = ?",
        (shop_id,),
    )
    conn.commit()
    return "Done. I erased everything I had for this number. Send anything to start fresh."


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


def _undo_payment(conn: sqlite3.Connection, shop_id: str, undo: dict) -> str:
    row = conn.execute(
        "SELECT p.*, c.name FROM payments p JOIN customers c ON c.id = p.customer_id"
        " WHERE p.id = ?", (undo["payment_id"],)).fetchone()
    conn.execute("UPDATE payments SET reversed = 1 WHERE id = ?", (undo["payment_id"],))
    bal = balance_cents(conn, shop_id, row["customer_id"])
    return (f"Undone: removed {row['name']}'s {cents_to_rand(row['amount_cents'])} payment."
            f" Balance back to {cents_to_rand(bal)}.")


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
    stock = _restore_stock(conn, shop_id, pid, -undo["qty"], action_id)
    if not undo.get("created"):
        sets, params = ["wholesaler = ?"], [undo.get("old_wholesaler", "")]
        if "old_cost" in undo:
            sets.append("cost_cents = ?")
            params.append(undo["old_cost"])
        if "old_price" in undo:
            sets.append("price_cents = ?")
            params.append(undo["old_price"])
        conn.execute(f"UPDATE products SET {', '.join(sets)} WHERE id = ?", (*params, pid))
        return f"Undone: removed the stock added. {name} back to {_fmt_qty(stock)}."
    conn.execute("DELETE FROM stock_moves WHERE product_id = ?", (pid,))
    conn.execute("DELETE FROM products WHERE id = ?", (pid,))
    return f"Undone: removed {name} again."


def _undo_price(conn: sqlite3.Connection, shop_id: str, undo: dict) -> str:
    conn.execute("UPDATE products SET price_cents = ? WHERE id = ?",
                 (undo["old_price"], undo["product_id"]))
    name = conn.execute("SELECT name FROM products WHERE id = ?",
                        (undo["product_id"],)).fetchone()["name"]
    if undo["old_price"] is None:
        return f"Undone: {name} price removed."
    return f"Undone: {name} price back to {cents_to_rand(undo['old_price'])}."


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
    ensure_shop(conn, shop_id)
    row = conn.execute(
        "SELECT * FROM actions WHERE shop_id = ? AND undone = 0 AND kind != 'seed'"
        " ORDER BY rowid DESC LIMIT 1",
        (shop_id,)).fetchone()
    if row is None:
        return "There is nothing to undo."
    undo = json.loads(row["data_json"])
    kind = row["kind"]
    if kind == "credit":
        msg = _undo_credit(conn, shop_id, undo)
    elif kind == "payment":
        msg = _undo_payment(conn, shop_id, undo)
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
    else:
        return "I cannot undo that one."
    conn.execute("UPDATE actions SET undone = 1, undone_at = ? WHERE action_id = ?",
                 (now_iso(), row["action_id"]))
    conn.commit()
    return msg


_APPLIERS = {
    "add_credit": _apply_add_credit,
    "record_payment": _apply_record_payment,
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
          " Use for 'took ... on the book'. No sale is recorded and stock is unchanged.",
          {"customer": {"type": "string"},
           "items": {"type": "array", "items": {"type": "object",
                     "properties": {"product": {"type": "string"}, "qty": {"type": "number"}},
                     "required": ["product", "qty"]}}}, ["customer", "items"]),
    _tool("record_payment", "Record a customer paying into the credit book.",
          {"customer": {"type": "string"}, "amount_cents": {"type": "integer"}},
          ["customer", "amount_cents"]),
    _tool("get_balance", "How much ONE named customer owes on the credit book. Needs a name.",
          {"customer": {"type": "string"}}, ["customer"]),
    _tool("list_debtors", "Everyone who owes on the credit book, with the total.", {}, []),
    _tool("draft_reminder", "Draft a polite payment reminder the owner forwards herself.",
          {"customer": {"type": "string"}}, ["customer"]),
    _tool("record_sale", "Record something SOLD: reduces stock and counts toward cash-up."
          " Use whenever the owner says they sold something."
          " Omit buyer for cash, or name the buyer for a credit sale.",
          {"product": {"type": "string"}, "qty": {"type": "number"},
           "buyer": {"type": "string"}}, ["product"]),
    _tool("add_stock", "Add stock ('add', 'got', 'restock', 'bought for the shop')."
          " Pass wholesaler only when the owner names a specific one; never guess."
          " Pass cost_cents (what the owner paid) and price_cents (sell price) when stated"
          " — 'got 12 Stoney 2L, cost R19 sell R25' sets both.",
          {"product": {"type": "string"}, "qty": {"type": "number"},
           "wholesaler": {"type": "string"}, "cost_cents": {"type": "integer"},
           "price_cents": {"type": "integer"}}, ["product", "qty"]),
    _tool("set_price", "Set a product's selling price in cents.",
          {"product": {"type": "string"}, "price_cents": {"type": "integer"}},
          ["product", "price_cents"]),
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
