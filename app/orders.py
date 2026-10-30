"""Customer orders and multi-task plans.

One owner message (photo caption, text, or voice transcript) can carry
several jobs: "Sipho: 2 bread, 5 chappies. Remind me Thursday 06:30 to
pack it. Summarise for Thursday." This module understands the message,
caption, chat history and any attached vision reading together, builds
ONE combined plan, and previews it before anything is saved.

Rules the plan engine follows:
- A customer order is never a sale, stock move, or debt until the owner
  records that separately. Confirming a plan only saves the order list,
  any new customers, reminders, and (ledger-backed) statements.
- Prices come only from the price list. Missing prices become questions.
- Plans survive restarts (SQLite) and expire after 24 hours.
- Every write lands in its own `actions` row, so Undo keeps working.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import math
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from app.db import DB_ERROR, cents_to_rand, now_iso
from app.i18n import t as _t

log = logging.getLogger("sisi.orders")

PLAN_TTL = timedelta(hours=24)
"""Plans older than this are treated as expired."""

ORDER_CUES = (
    "order", "orders", "ordered", "ordering", "oda", "i-oda",
    "bestel", "bestelling", "thenga",
)
"""Words that mark a message as a customer order (any language)."""

_CUE_RES = tuple(re.compile(r"\b%s\b" % re.escape(cue), re.IGNORECASE)
                 for cue in ("oda", "i-oda", "bestel", "bestelling",
                             "thenga"))

PACK_WORDS = (
    "box", "boxes", "tray", "trays", "pack", "packs", "case", "cases",
    "dozen", "dozens", "bag", "bags", "sack", "sacks", "carton", "cartons",
    "bale", "bales", "outer", "inners", "shrink",
)

SALE_VERBS = (
    "sold", "sell", "sale", "took", "take", "credit", "on the book",
    "on book", "thengisa", "thatha", "verkoop", "krediet", "op die boek",
    "ekutyeni",
)
"""Verbs that mark a sale/credit recording, never a customer order."""

STOCK_VERBS = (
    "add", "restock", "bought", "buy", "buying", "got", "received",
    "stock", "cost", "thenga", "koop", "gekoop", "voorraad",
)
"""Verbs that mark a stock intake, never a customer order."""

PAY_WORDS = ("paid", "payment", "khokha", "betaal", "hlawul", "settle")

SUMMARY_CUES = ("summar", "opsom", "finyezo", "shwankathela", "isishwankathelo")

_CUST_SPLIT_RES = (
    re.compile(r"^([^:,;.!?]+?)\s*:\s*(.+)$"),
    re.compile(r"^(.+?)\s+[-–]\s+([^,;]+?)$"),
)
_FOR_RE = re.compile(r"^(.+?)\s+(?:for|vir|ka)\s+([A-Za-z][\w'\- ]*?)\.?$")
_ORDERED_RE = re.compile(
    r"^([A-Za-z][\w'\- ]*?)\s+(?:ordered?|oda(?:ed|ing)?|bestel(?:de|t)?)\s+(.+)$",
    re.IGNORECASE)
_QTY_FIRST_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*[x×]?\s+(.+)$")
_QTY_LAST_RE = re.compile(r"^(.+?)\s*[x×]\s*(\d+(?:\.\d+)?)$")
_QTY_TRAIL_RE = re.compile(r"^(.+?)\s+(\d+(?:\.\d+)?)$")
_PACK_RE = re.compile(
    r"^(a|an|\d+(?:\.\d+)?)\s+(%s)\s+(?:of\s+)?(.+)$" % "|".join(PACK_WORDS),
    re.IGNORECASE)
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


# --- message splitting ------------------------------------------------------


def split_segments(text: str) -> list[str]:
    """Split a message into task segments (lines and sentences)."""
    parts: list[str] = []
    for chunk in re.split(r"\n+|[;]+", text or ""):
        for bit in re.split(r"(?<=[.!?])\s+", chunk):
            bit = bit.strip(" .!?\t")
            if bit:
                parts.append(bit)
    return parts


def _has_cue(text: str) -> bool:
    """True when the text carries an explicit order word."""
    low = text or ""
    if re.search(r"\borders?\b|\bordered\b|\bordering\b", low,
                 re.IGNORECASE):
        return True
    return any(rx.search(low) is not None for rx in _CUE_RES)


def _has_blocker(text: str) -> bool:
    """True for sale/stock/payment phrasing (never a customer order)."""
    low = (text or "").lower()
    if any(w in low for w in SALE_VERBS + STOCK_VERBS + PAY_WORDS):
        return True
    return "r" in low and re.search(r"\br\s?\d", low) is not None


_SELF_ORDER_RE = re.compile(
    r"^(i|we|mina|thina|ek|ons|mna)\s+"
    r"(?:ordered?|ordering|oda(?:ed|ing)?|bestel\w*|theng\w*)\s+",
    re.IGNORECASE)
_ARRIVE_RE = re.compile(
    r"\barriv\w*\b|\bcoming\b|\bon\s+(?:its|the)\s+way\b"
    r"|\bkom\b|\bop\s+pad\b",
    re.IGNORECASE)


def is_supplier_segment(text: str) -> bool:
    """True for owner-side delivery context (never a customer order).

    'I ordered milk, it's arriving in 15 minutes' is background about
    an incoming supplier delivery — not a customer order, received
    stock, or a completed purchase.
    """
    seg = (text or "").strip()
    if _SELF_ORDER_RE.match(seg):
        return True
    return _ARRIVE_RE.search(seg) is not None


# --- matching ---------------------------------------------------------------


def match_customer(conn: sqlite3.Connection, shop_id: str,
                   name: str) -> tuple[sqlite3.Row | None, bool, str]:
    """Match a name to the roster: (row_or_None, is_new, display_name)."""
    display = " ".join((name or "").strip().split())
    if not display:
        return None, False, "?"
    rows = conn.execute("SELECT * FROM customers WHERE shop_id = ?",
                        (shop_id,)).fetchall()
    for row in rows:
        if row["name"].strip().lower() == display.lower():
            return row, False, row["name"]
    lower_map = {row["name"].strip().lower(): row for row in rows}
    hits = difflib.get_close_matches(display.lower(), list(lower_map),
                                     n=1, cutoff=0.82)
    if hits:
        row = lower_map[hits[0]]
        return row, False, row["name"]
    return None, True, display[:1].upper() + display[1:] if display else "?"


def match_product(conn: sqlite3.Connection, shop_id: str,
                  query: str) -> sqlite3.Row | None:
    """Match free text to one product, or None when unsure."""
    from app.tools import _find_products
    query = (query or "").strip()
    if not query:
        return None
    unique, ambiguous = _find_products(conn, shop_id, query)
    if unique is not None:
        return unique
    if len(ambiguous) == 1:
        return ambiguous[0]
    rows = conn.execute("SELECT * FROM products WHERE shop_id = ?",
                        (shop_id,)).fetchall()
    lower_map = {row["name"].strip().lower(): row for row in rows}
    hits = difflib.get_close_matches(query.lower(), list(lower_map),
                                     n=1, cutoff=0.8)
    if hits:
        return lower_map[hits[0]]
    return None


# --- order line parsing ------------------------------------------------------


def _qty_item(text: str) -> tuple[float, str, float | None]:
    """Parse '2 bread' / 'bread x2' / 'a box of chappies'.

    Returns (qty_units_or_count, item_text, pack_count_or_None).
    """
    text = (text or "").strip()
    low = text.lower()
    for word, num in _NUMBER_WORDS.items():
        if low == word or low.startswith(word + " "):
            text = f"{num} {text[len(word):].strip()}"
            break
    m = _PACK_RE.match(text)
    if m:
        count = 1.0 if m.group(1).lower() in ("a", "an") else float(m.group(1))
        return count, m.group(3).strip(), count
    m = _QTY_FIRST_RE.match(text)
    if m:
        return float(m.group(1)), m.group(2).strip(), None
    m = _QTY_LAST_RE.match(text)
    if m:
        return float(m.group(2)), m.group(1).strip(), None
    m = _QTY_TRAIL_RE.match(text)
    if m and not re.search(r"\d\s*(ml|l|g|kg)$", m.group(1).lower()):
        return float(m.group(2)), m.group(1).strip(), None
    return 1.0, text, None


def _make_line(conn: sqlite3.Connection, shop_id: str, customer: str,
               raw: str, source: str) -> dict | None:
    """Parse one 'qty item' fragment into a plan line. None when empty."""
    raw = (raw or "").strip(" ,.")
    if not raw or not re.search(r"[A-Za-z]", raw):
        return None
    qty, item_text, packs = _qty_item(raw)
    if not item_text:
        return None
    low_item = item_text.lower()
    if low_item in ("order", "my order", "the order", "orders", "oda",
                    "my oda", "bestelling", "my bestelling"):
        return None
    import app.agent as agent
    from app.reminders import REMIND_TRIGGERS
    if any(t in low_item for t in agent._STATEMENT_TRIGGERS) \
            or any(t in low_item for t in REMIND_TRIGGERS):
        return None
    product = match_product(conn, shop_id, item_text)
    units = qty
    if product is not None and packs is not None and product["pack_size"]:
        units = packs * product["pack_size"]
    cust_row, is_new, display = match_customer(conn, shop_id, customer)
    price = None
    if product is not None:
        price = product["price_cents"]
    return {"line_no": 0, "customer": display,
            "customer_id": cust_row["id"] if cust_row else None,
            "customer_new": bool(is_new and display != "?"),
            "item": product["name"] if product else item_text,
            "product_id": product["id"] if product else None,
            "qty": units, "price_cents": price,
            "matched": product is not None, "source": source}


def _split_items(fragment: str) -> list[str]:
    """Split '2 bread, 5 chappies and 1 coke' into item fragments.

    Identical repeats ('milk, milk') are voice disfluencies, not extra
    units: each distinct fragment is kept once.
    """
    bits = re.split(r",|;|\band\b|\+|&", fragment, flags=re.IGNORECASE)
    out: list[str] = []
    seen: set[str] = set()
    for bit in bits:
        bit = bit.strip()
        if not bit:
            continue
        key = re.sub(r"[^a-z0-9]", "", bit.lower())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(bit)
    return out


def _split_ok(conn: sqlite3.Connection, shop_id: str, fragment: str) -> bool:
    """A customer split is only real when the rest looks like items.

    'Sipho: 2 bread' splits; 'stuur vir my die strokie van Sipho'
    (an Afrikaans statement request) must not become customer 'my die
    strokie van Sipho' with item 'stuur'.
    """
    if re.search(r"\d", fragment):
        return True
    return any(match_product(conn, shop_id, bit) is not None
               for bit in _split_items(fragment))


def parse_order_lines(conn: sqlite3.Connection, shop_id: str,
                      text: str) -> list[dict]:
    """Parse customer order lines from free text (caption/history/caption)."""
    from app.reminders import PRONOUNS, REMIND_TRIGGERS
    lines: list[dict] = []
    for seg in split_segments(text):
        if _has_blocker(seg):
            continue
        if is_supplier_segment(seg):
            continue  # incoming supplier delivery, not a customer order
        if re.match(r"\s*line\s+\d+\s+is\s+", seg, re.IGNORECASE):
            continue  # a correction, not a new order line
        if any(t in f" {seg.lower()} " for t in REMIND_TRIGGERS):
            continue  # reminder segments are reminder tasks, not order lines
        if any(cue in seg.lower() for cue in SUMMARY_CUES):
            continue  # summary segments are summary tasks, not order lines
        customer, fragment = "", seg
        m = _CUST_SPLIT_RES[0].match(seg) or _CUST_SPLIT_RES[1].match(seg)
        if m and re.search(r"[A-Za-z]", m.group(1)) \
                and not re.search(r"\d", m.group(1)) \
                and len(m.group(1).split()) <= 3 \
                and _split_ok(conn, shop_id, m.group(2)):
            customer, fragment = m.group(1).strip(), m.group(2)
        else:
            m = _FOR_RE.match(seg)
            if m and len(m.group(2).split()) <= 3 \
                    and _split_ok(conn, shop_id, m.group(1)):
                fragment, customer = m.group(1).strip(), m.group(2).strip()
            else:
                m = _ORDERED_RE.match(seg)
                if m and len(m.group(1).split()) <= 3 \
                        and _split_ok(conn, shop_id, m.group(2)):
                    customer, fragment = m.group(1).strip(), m.group(2).strip()
        if customer.strip().lower() in PRONOUNS:
            customer = ""  # 'I'/'it'/'me' are pronouns, never customers
        for bit in _split_items(fragment):
            if _has_blocker(bit):
                continue
            line = _make_line(conn, shop_id, customer, bit, "caption")
            if line is not None:
                lines.append(line)
    last_named = ""
    for line in lines:
        if line["customer"] not in ("", "?"):
            last_named = line["customer"]
        elif last_named:
            row, is_new, display = match_customer(conn, shop_id, last_named)
            line["customer"] = display
            line["customer_id"] = row["id"] if row else None
            line["customer_new"] = bool(is_new)
    for i, line in enumerate(lines, 1):
        line["line_no"] = i
    return [ln for ln in lines
            if ln["matched"] or ln["customer"] not in ("", "?")
            or _has_cue(text)]


def vision_to_order_lines(conn: sqlite3.Connection, shop_id: str,
                          doc: dict, customer: str = "") -> list[dict]:
    """Turn vision book/receipt lines into order lines (photo evidence)."""
    if not isinstance(doc, dict):
        return []
    raw_lines: list[dict] = []
    if doc.get("doc_type") == "book_page":
        for ln in (doc.get("book") or {}).get("lines", []) or []:
            if not isinstance(ln, dict):
                continue
            raw_lines.append({
                "text": ln.get("item") or ln.get("text") or "",
                "qty": ln.get("qty") or 1,
                "customer": ln.get("customer") or customer,
            })
    elif doc.get("doc_type") == "receipt":
        for ln in (doc.get("receipt") or {}).get("lines", []) or []:
            if not isinstance(ln, dict):
                continue
            qty = ln.get("qty_units") or 1
            try:
                qty = float(qty)
            except (TypeError, ValueError):
                qty = 1
            raw_lines.append({
                "text": ln.get("item") or ln.get("text") or "",
                "qty": qty if qty > 0 else 1,
                "customer": customer,
            })
    lines: list[dict] = []
    for raw in raw_lines:
        product = match_product(conn, shop_id, raw["text"])
        cust_row, is_new, display = match_customer(conn, shop_id,
                                                  raw["customer"])
        lines.append({
            "line_no": 0, "customer": display,
            "customer_id": cust_row["id"] if cust_row else None,
            "customer_new": bool(is_new and display != "?"),
            "item": product["name"] if product else raw["text"],
            "product_id": product["id"] if product else None,
            "qty": raw["qty"],
            "price_cents": product["price_cents"] if product else None,
            "matched": product is not None, "source": "photo",
        })
    for i, line in enumerate(lines, 1):
        line["line_no"] = i
    return lines


# --- reminder / statement / summary tasks ------------------------------------


def _reminder_tasks(text: str, now: datetime | None = None) -> list[dict]:
    """Parse every 'remind me …' segment into reminder tasks."""
    from app.reminders import REMIND_TRIGGERS, parse_request, reminder_spec_ok
    tasks: list[dict] = []
    for seg in split_segments(text):
        low = f" {seg.lower()} "
        if not any(t in low for t in REMIND_TRIGGERS):
            continue
        parsed = parse_request(seg, now)
        if parsed is None:
            continue
        if not reminder_spec_ok(seg, parsed):
            continue  # 'remind them' is composite work, not a plan task
        tasks.append({"kind": "reminder", "due": parsed["due"].isoformat(),
                      "summary": parsed["summary"],
                      "recurrence": parsed["recurrence"], "segment": seg})
    return tasks


def _statement_specs(conn: sqlite3.Connection, shop_id: str,
                     text: str) -> list[dict]:
    """Detect statement requests per segment, deferred until Yes."""
    import app.agent as agent
    specs: list[dict] = []
    names = [r["name"] for r in conn.execute(
        "SELECT name FROM customers WHERE shop_id = ?", (shop_id,)).fetchall()]
    for seg in split_segments(text):
        word = agent._normalize(seg)
        if "this month" in word and "statement" not in word:
            continue
        if re.search(r"\b(paid|payment|khokha|settled?|ibhadalile|betaal|"
                     r"hlawul\w*)\b", word):
            continue
        has_stmt = agent._has_statement_word(word)
        has_debtor = any(d in word for d in agent._DEBTOR_WORDS)
        has_all = any(re.search(rf"\b{a}\b", word)
                      for a in agent._ALL_WORDS)
        if not has_stmt and not (has_all and has_debtor):
            continue
        hits = agent._customers_in_text(word, names)
        if has_stmt and len(hits) >= 2:
            specs.append({"kind": "statement", "mode": "named",
                          "names": hits})
            continue
        num = re.search(r"\d+", word)
        if has_stmt and num and has_debtor:
            specs.append({"kind": "statement", "mode": "top",
                          "n": int(num.group())})
            continue
        if has_all and (has_debtor or (has_stmt and not hits)):
            specs.append({"kind": "statement", "mode": "all"})
            continue
        if has_stmt and hits:
            specs.append({"kind": "statement", "mode": "named",
                          "names": hits})
    return specs


def _summary_tasks(text: str) -> list[dict]:
    """Detect 'summarise for Thursday' style summary requests."""
    from app.reminders import WEEKDAYS
    tasks: list[dict] = []
    for seg in split_segments(text):
        low = seg.lower()
        if not any(cue in low for cue in SUMMARY_CUES):
            continue
        label = ""
        for name in sorted(WEEKDAYS, key=len, reverse=True):
            if re.search(rf"\b{name}\b", low):
                label = name.capitalize()
                break
        tasks.append({"kind": "summary", "label": label or " ", "segment": seg})
    return tasks


# --- plan store ---------------------------------------------------------------


def _row_to_plan(row: sqlite3.Row) -> dict:
    """Decode a task_plans row into a plan dict."""
    try:
        body = json.loads(row["plan_json"] or "{}")
    except ValueError:
        body = {}
    body.update({"plan_id": row["plan_id"], "status": row["status"],
                 "created_at": row["created_at"],
                 "expires_at": row["expires_at"],
                 "message_key": row["message_key"]})
    return body


def expire_plans(conn: sqlite3.Connection, shop_id: str) -> None:
    """Mark past-due plans expired (lazy expiry, no cron needed)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    conn.execute("UPDATE task_plans SET status = 'expired'"
                 " WHERE shop_id = ? AND status = 'proposed'"
                 " AND expires_at != '' AND expires_at < ?", (shop_id, now))
    conn.commit()


def get_plan(conn: sqlite3.Connection, shop_id: str) -> dict | None:
    """The newest live proposed plan, or None."""
    expire_plans(conn, shop_id)
    row = conn.execute("SELECT * FROM task_plans WHERE shop_id = ?"
                       " AND status = 'proposed'"
                       " ORDER BY created_at DESC, plan_id DESC LIMIT 1",
                       (shop_id,)).fetchone()
    return _row_to_plan(row) if row else None


def get_plan_by_key(conn: sqlite3.Connection, shop_id: str,
                    message_key: str) -> dict | None:
    """Find a plan by idempotency key (any live status)."""
    expire_plans(conn, shop_id)
    row = conn.execute("SELECT * FROM task_plans WHERE shop_id = ?"
                       " AND message_key = ? AND status IN ('proposed',"
                       " 'confirmed') ORDER BY created_at DESC LIMIT 1",
                       (shop_id, message_key)).fetchone()
    return _row_to_plan(row) if row else None


def get_plan_by_id(conn: sqlite3.Connection, shop_id: str,
                   plan_id: str) -> dict | None:
    """Find a plan by id (any status; confirmed plans feed reminders)."""
    row = conn.execute("SELECT * FROM task_plans WHERE shop_id = ?"
                       " AND plan_id = ?", (shop_id, plan_id)).fetchone()
    return _row_to_plan(row) if row else None


def save_plan(conn: sqlite3.Connection, shop_id: str, plan: dict) -> dict:
    """Insert a plan (superseding older proposals) and return it."""
    from app.tools import ensure_shop
    ensure_shop(conn, shop_id)
    conn.execute("UPDATE task_plans SET status = 'superseded'"
                 " WHERE shop_id = ? AND status = 'proposed'",
                 (shop_id,))
    created = now_iso()
    expires = (datetime.now(timezone.utc) + PLAN_TTL).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    plan = dict(plan)
    plan.setdefault("plan_id", uuid.uuid4().hex[:12])
    conn.execute("INSERT INTO task_plans (shop_id, plan_id, message_key,"
                 " created_at, expires_at, status, plan_json)"
                 " VALUES (?, ?, ?, ?, ?, 'proposed', ?)",
                 (shop_id, plan["plan_id"], plan.get("message_key", ""),
                  created, expires, json.dumps(plan)))
    conn.commit()
    plan.update({"status": "proposed", "created_at": created,
                 "expires_at": expires})
    return plan


def plan_followup_message(conn: sqlite3.Connection, shop_id: str) -> str | None:
    """'Already saved' / 'expired' for a Yes with no live proposal.

    Only the most recent plan answers, and only within an hour of its
    creation — a stray Yes days later must not resurrect old news.
    """
    row = conn.execute("SELECT status, created_at FROM task_plans"
                       " WHERE shop_id = ?"
                       " ORDER BY created_at DESC LIMIT 1",
                       (shop_id,)).fetchone()
    if row is None:
        return None
    try:
        created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if datetime.now(timezone.utc) - created > timedelta(hours=1):
        return None
    if row["status"] == "confirmed":
        return _t("ord_already")
    if row["status"] == "expired":
        return _t("ord_expired")
    return None


def cancel_plan(conn: sqlite3.Connection, shop_id: str) -> str:
    """Drop the live proposed plan. Nothing is saved or changed."""
    plan = get_plan(conn, shop_id)
    if plan is None:
        return _t("ord_cancelled")
    conn.execute("UPDATE task_plans SET status = 'cancelled'"
                 " WHERE shop_id = ? AND plan_id = ?",
                 (shop_id, plan["plan_id"]))
    conn.commit()
    return _t("ord_cancelled")


def _update_plan_lines(conn: sqlite3.Connection, shop_id: str,
                       plan: dict, lines: list[dict]) -> dict:
    """Replace a plan's order lines and persist (corrections)."""
    for i, line in enumerate(lines, 1):
        line["line_no"] = i
    plan = dict(plan)
    plan["order_lines"] = lines
    conn.execute("UPDATE task_plans SET plan_json = ?"
                 " WHERE shop_id = ? AND plan_id = ?",
                 (json.dumps(plan), shop_id, plan["plan_id"]))
    conn.commit()
    return plan


def message_key_for(text: str, photo_digest: str = "") -> str:
    """Idempotency key for a message (text and/or photo digest)."""
    bits = (text or "") + "\x00" + (photo_digest or "")
    return hashlib.sha1(bits.encode("utf-8")).hexdigest()[:16]


CLARIFY_TTL = timedelta(hours=24)
"""A clarification answer resumes the same media within this long."""


def save_clarify_context(conn: sqlite3.Connection, shop_id: str,
                         photo_digest: str, caption: str,
                         vision_doc: dict | None) -> None:
    """Keep the ambiguous photo + reading so a text answer resumes it.

    One row per shop (a new photo supersedes); survives restarts.
    """
    from app.tools import ensure_shop
    ensure_shop(conn, shop_id)
    expires = (datetime.now(timezone.utc) + CLARIFY_TTL).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    try:
        vision_json = json.dumps(vision_doc or {})
    except (TypeError, ValueError):
        vision_json = "{}"
    conn.execute("INSERT INTO clarify_context (shop_id, photo_digest, caption,"
                 " vision_json, created_at, expires_at)"
                 " VALUES (?, ?, ?, ?, ?, ?)"
                 " ON CONFLICT (shop_id) DO UPDATE SET photo_digest = excluded.photo_digest,"
                 " caption = excluded.caption, vision_json = excluded.vision_json,"
                 " created_at = excluded.created_at, expires_at = excluded.expires_at",
                 (shop_id, photo_digest or "", (caption or "")[:500],
                  vision_json, now_iso(), expires))
    conn.commit()


def get_clarify_context(conn: sqlite3.Connection, shop_id: str) -> dict | None:
    """The live clarification context, or None when absent/expired."""
    row = conn.execute("SELECT * FROM clarify_context WHERE shop_id = ?",
                       (shop_id,)).fetchone()
    if row is None:
        return None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    if row["expires_at"] and row["expires_at"] < now:
        conn.execute("DELETE FROM clarify_context WHERE shop_id = ?",
                     (shop_id,))
        conn.commit()
        return None
    try:
        vision = json.loads(row["vision_json"] or "{}")
    except ValueError:
        vision = {}
    return {"photo_digest": row["photo_digest"], "caption": row["caption"],
            "vision": vision if isinstance(vision, dict) else {}}


def clear_clarify_context(conn: sqlite3.Connection, shop_id: str) -> None:
    """Drop the clarification context (answered, superseded, or pivoted)."""
    conn.execute("DELETE FROM clarify_context WHERE shop_id = ?", (shop_id,))
    conn.commit()


def resume_plan_from_clarify(conn: sqlite3.Connection, shop_id: str,
                             text: str) -> tuple[dict | None, bool]:
    """Build a plan from a clarification answer plus the stored media.

    Returns (plan_or_None, context_used). The original caption's tasks
    merge with the answer, so a multi-task request survives the
    clarification round-trip. Non-answers leave normal routing alone.
    """
    ctx = get_clarify_context(conn, shop_id)
    if ctx is None:
        return None, False
    if not parse_order_lines(conn, shop_id, text or "") \
            and not _has_cue(text or ""):
        return None, False
    combined = "\n".join(b for b in (ctx["caption"], text or "") if b)
    plan, _question = build_plan(
        conn, shop_id, combined, ctx["vision"] or None,
        ctx["photo_digest"], from_photo=True)
    return plan, plan is not None


# --- plan building ------------------------------------------------------------


def _vision_customer(conn: sqlite3.Connection, shop_id: str,
                     text: str) -> str:
    """A customer named in the caption ('for Sipho'), for vision lines."""
    import app.agent as agent
    names = [r["name"] for r in conn.execute(
        "SELECT name FROM customers WHERE shop_id = ?", (shop_id,)).fetchall()]
    hit = agent._customer_in_text(agent._normalize(text), names)
    if hit is not None:
        return hit
    for seg in split_segments(text):
        m = _FOR_RE.match(seg)
        if m:
            return m.group(2).strip()
    return _history_customer(conn, shop_id, names)


def _history_customer(conn: sqlite3.Connection, shop_id: str,
                      names: list[str]) -> str:
    """Latest roster name in recent user turns (caption said nobody)."""
    try:
        rows = conn.execute(
            "SELECT content FROM conversation_turns WHERE shop_id = ?"
            " AND role = 'user' ORDER BY id DESC LIMIT 6",
            (shop_id,)).fetchall()
    except DB_ERROR:
        return ""
    for row in rows:
        low = (row["content"] or "").lower()
        for name in names:
            if name.strip() and re.search(r"\b%s\b" % re.escape(name.lower()),
                                          low):
                return name
    return ""


def _fill_history_customers(conn: sqlite3.Connection, shop_id: str,
                            lines: list[dict]) -> list[dict]:
    """Attribute '?' lines to the customer under discussion, if any."""
    if not lines or any(ln.get("customer") not in ("", "?") for ln in lines):
        return lines
    names = [r["name"] for r in conn.execute(
        "SELECT name FROM customers WHERE shop_id = ?", (shop_id,)).fetchall()]
    hist = _history_customer(conn, shop_id, names)
    if not hist:
        return lines
    row, _new, display = match_customer(conn, shop_id, hist)
    for ln in lines:
        if ln.get("customer") in ("", "?"):
            ln["customer"] = display
            ln["customer_id"] = row["id"] if row else None
            ln["customer_new"] = False
    return lines


def build_plan(conn: sqlite3.Connection, shop_id: str, text: str,
               vision_doc: dict | None = None,
               photo_digest: str = "",
               message_key: str = "",
               from_photo: bool = False,
               now: datetime | None = None) -> tuple[dict | None, str | None]:
    """Build a multi-task plan from text + vision, or (None, question|None).

    Returns (plan, None) when order lines were found, (None, question)
    when the message is order-ish but nothing solid could be extracted,
    and (None, None) when it carries no order task at all.
    """
    text = text or ""
    lines = parse_order_lines(conn, shop_id, text)
    if isinstance(vision_doc, dict) and \
            vision_doc.get("doc_type") in ("book_page", "receipt") and \
            not any(ln.get("matched") for ln in lines):
        customer = _vision_customer(conn, shop_id, text)
        if customer or _has_cue(text):
            vision_lines = vision_to_order_lines(conn, shop_id, vision_doc,
                                                 customer)
            if vision_lines:
                lines = vision_lines
    lines = _fill_history_customers(conn, shop_id, lines)
    if from_photo:
        keen = lines
    else:
        keen = [ln for ln in lines
                if ln["customer"] not in ("", "?") or _has_cue(text)]
    if not keen:
        from app.reminders import REMIND_TRIGGERS as _TRIG
        plain = [s for s in split_segments(text)
                 if not any(t in f" {s.lower()} " for t in _TRIG)]
        if any(_has_cue(s) and not _has_blocker(s)
               and not is_supplier_segment(s) for s in plain):
            return None, _t("ord_clarify")
        return None, None
    lines = keen
    reminders = _reminder_tasks(text, now)
    seen_rem: set[tuple[str, str, str]] = set()
    deduped = []
    for task in reminders:
        key = (task["due"], task["summary"], task["recurrence"])
        if key not in seen_rem:
            seen_rem.add(key)
            deduped.append(task)
    reminders = deduped
    statements = _statement_specs(conn, shop_id, text)
    summaries = _summary_tasks(text)
    vision_brief: dict = {}
    if isinstance(vision_doc, dict):
        vision_brief = {"doc_type": vision_doc.get("doc_type", "other")}
    plan = {
        "message_key": message_key or message_key_for(text, photo_digest),
        "caption": text[:500],
        "photo_digest": photo_digest,
        "vision": vision_brief,
        "order_lines": lines,
        "reminders": reminders,
        "statements": statements,
        "summaries": summaries,
    }
    return plan, None


# --- procurement (shortfall vs live stock) ------------------------------------


def procure_lines(conn: sqlite3.Connection, shop_id: str,
                  lines: list[dict]) -> list[dict]:
    """Shortfall draft lines for order lines (packs + est cost).

    Reads live stock and cost prices; writes nothing. Unmatched lines
    are skipped — they are questions, not guesses.
    """
    need: dict[int, float] = {}
    for ln in lines or []:
        if ln.get("product_id") and (ln.get("qty") or 0) > 0:
            need[ln["product_id"]] = need.get(ln["product_id"], 0.0) \
                + float(ln["qty"])
    out: list[dict] = []
    for pid, qty in sorted(need.items()):
        row = conn.execute("SELECT * FROM products WHERE id = ? AND shop_id = ?",
                           (pid, shop_id)).fetchone()
        if row is None:
            continue
        short = max(0.0, qty - (row["stock_qty"] or 0.0))
        if short <= 0:
            continue
        if row["pack_size"]:
            packs = math.ceil(short / row["pack_size"])
            units = packs * row["pack_size"]
        else:
            packs, units = None, short
        est = int(round(units * row["cost_cents"])) \
            if row["cost_cents"] is not None else None
        out.append({"product_id": row["id"], "name": row["name"],
                    "wholesaler": row["wholesaler"] or "", "units": units,
                    "packs": packs, "pack_name": row["pack_name"] or "",
                    "est_cents": est})
    return out


def render_procurement(lines: list[dict]) -> str:
    """Format shortfall lines grouped by wholesaler (no writes)."""
    from app.tools import _fmt_qty, _plural_pack
    groups: dict[str, list] = {}
    for ln in lines:
        groups.setdefault(ln["wholesaler"] or _t("no_wholesaler"), []).append(ln)
    blocks = [_t("ord_need_head")]
    for w, items in groups.items():
        bl = [f"*{w}*"]
        for ln in items:
            if ln["packs"]:
                word = ln["pack_name"] if ln["packs"] == 1 \
                    else _plural_pack(ln["pack_name"])
                line = _t("order_line_pack", name=ln["name"],
                          q=_fmt_qty(ln["packs"]), word=word,
                          u=_fmt_qty(ln["units"]))
            else:
                line = _t("order_line", name=ln["name"],
                          u=_fmt_qty(ln["units"]))
            line += (_t("order_est", total=cents_to_rand(ln["est_cents"]))
                     if ln["est_cents"] is not None else _t("order_nocost"))
            bl.append(line)
        blocks.append("\n".join(bl))
    known = [ln["est_cents"] for ln in lines
             if ln["est_cents"] is not None]
    blocks.append(_t("order_total", total=cents_to_rand(sum(known))))
    return "\n\n".join(blocks)


# --- preview + summaries ------------------------------------------------------


def _fmt_qty_num(qty: float) -> str:
    """Format an order quantity (whole numbers without decimals)."""
    from app.tools import _fmt_qty
    return _fmt_qty(qty)


def customer_summaries(conn: sqlite3.Connection, shop_id: str,
                       lines: list[dict], label: str = "") -> str:
    """Per-customer order summaries (information only, never statements)."""
    from app.tools import balance_cents
    groups: dict[str, list] = {}
    for ln in lines or []:
        if not ln.get("matched"):
            continue
        groups.setdefault(ln["customer"] or "?", []).append(ln)
    blocks = [_t("ord_sum_head") + (f" · {label}" if label.strip() else "")]
    for customer, items in groups.items():
        bits = [f"{_fmt_qty_num(i['qty'])} {i['item']}" for i in items]
        if all(i.get("price_cents") is not None for i in items):
            total = sum(i["qty"] * i["price_cents"] for i in items)
            blocks.append(f"• {customer}: {', '.join(bits)}"
                          f" = {cents_to_rand(int(round(total)))}")
        else:
            blocks.append(f"• {customer}: {', '.join(bits)}")
    if len(blocks) == 1:
        return ""
    return "\n".join(blocks)


def _owes_lines(conn: sqlite3.Connection, shop_id: str,
                lines: list[dict]) -> list[str]:
    """Current debtors among the order's matched customers."""
    from app.tools import balance_cents
    seen: set[int] = set()
    out: list[str] = []
    for ln in lines or []:
        cid = ln.get("customer_id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        owed = balance_cents(conn, shop_id, cid)
        if owed > 0:
            out.append(_t("ord_owes", name=ln["customer"],
                          amt=cents_to_rand(owed)))
    return out


def _refresh_line_prices(conn: sqlite3.Connection, shop_id: str,
                         lines: list[dict]) -> list[dict]:
    """Refresh matched lines from the live price list (never stale)."""
    for ln in lines or []:
        if not ln.get("matched") or not ln.get("product_id"):
            continue
        if ln.get("price_override"):
            continue  # the owner stated this price during review
        row = conn.execute("SELECT price_cents, name FROM products"
                           " WHERE id = ? AND shop_id = ?",
                           (ln["product_id"], shop_id)).fetchone()
        if row is None:
            ln["matched"] = False
            ln["price_cents"] = None
            continue
        ln["price_cents"] = row["price_cents"]
        ln["item"] = row["name"]
    return lines


def render_preview(conn: sqlite3.Connection, shop_id: str,
                   plan: dict) -> str:
    """Render one combined preview for every task in the plan."""
    from app.reminders import confirm_text
    from datetime import datetime as _dt
    lines = _refresh_line_prices(conn, shop_id, plan.get("order_lines", []))
    blocks = [_t("ord_head")]
    body: list[str] = []
    unknown: list[str] = []
    for ln in lines:
        if not ln.get("matched"):
            unknown.append(f"• L{ln['line_no']} {ln['customer']}:"
                           f" {ln['item']}")
        elif ln.get("price_cents") is None:
            body.append(_t("ord_line_noprice", n=ln["line_no"],
                           customer=ln["customer"],
                           qty=_fmt_qty_num(ln["qty"]), item=ln["item"]))
        else:
            total = int(round(ln["qty"] * ln["price_cents"]))
            body.append(_t("ord_line", n=ln["line_no"],
                           customer=ln["customer"],
                           qty=_fmt_qty_num(ln["qty"]), item=ln["item"],
                           amt=cents_to_rand(total)))
    blocks.append("\n".join(body) if body else _t("ord_unknown_head"))
    if unknown:
        blocks.append(_t("ord_unknown_head") + "\n" + "\n".join(unknown)
                      + "\n" + _t("ord_unknown_hint"))
    new_names = sorted({ln["customer"] for ln in lines
                        if ln.get("customer_new")})
    for name in new_names:
        blocks.append(_t("ord_newcust", name=name))
    owes = _owes_lines(conn, shop_id, lines)
    if owes:
        blocks.append("\n".join(owes))
    procure = procure_lines(conn, shop_id, lines)
    if procure:
        blocks.append(render_procurement(procure))
    for task in plan.get("summaries", []):
        text = customer_summaries(conn, shop_id, lines,
                                  task.get("label", ""))
        if text:
            blocks.append(text)
    for task in plan.get("reminders", []):
        due = _dt.fromisoformat(task["due"])
        blocks.append(_t("ord_rem_head") + "\n"
                      + confirm_text(due, task["summary"],
                                     task.get("recurrence", "none")))
    for spec in plan.get("statements", []):
        blocks.append(_statement_preview(conn, shop_id, spec))
    blocks.append(_t("ord_hint"))
    return "\n\n".join(b for b in blocks if b)


def _statement_preview(conn: sqlite3.Connection, shop_id: str,
                       spec: dict) -> str:
    """Preview which ledger-backed statements Yes will generate."""
    from app.tools import balance_cents
    mode = spec.get("mode", "named")
    if mode == "all":
        return _t("stmts_head")
    if mode == "top":
        return _t("stmts_head_n", n=spec.get("n", 0))
    out = [_t("stmts_head_some")]
    for name in spec.get("names", []):
        row = conn.execute("SELECT id, name FROM customers"
                           " WHERE shop_id = ? AND name = ?",
                           (shop_id, name)).fetchone()
        if row is None:
            continue
        owed = balance_cents(conn, shop_id, row["id"])
        if owed > 0:
            out.append(_t("ord_owes", name=row["name"],
                          amt=cents_to_rand(owed)))
    return "\n".join(out)


# --- confirm ------------------------------------------------------------------


def confirm_plan(conn: sqlite3.Connection, shop_id: str) -> str:
    """Execute every write in the live plan, exactly once.

    Saves the order list, new customers, reminders, and ledger-backed
    statements. Never writes stock, sales, or debt for unfulfilled
    orders. A second Yes reports 'already saved' without duplicating.
    """
    from app.reminders import confirm_text, save_reminder
    from app.tools import (_action_id, _get_customer, balance_cents,
                           ensure_shop, get_order_draft, statements_for_all,
                           statements_named, statements_top_n)
    from datetime import datetime as _dt
    plan = get_plan(conn, shop_id)
    if plan is None:
        row = conn.execute(
            "SELECT status FROM task_plans WHERE shop_id = ?"
            " ORDER BY created_at DESC LIMIT 1", (shop_id,)).fetchone()
        if row is not None and row["status"] == "expired":
            return _t("ord_expired")
        return _t("ord_already")
    ensure_shop(conn, shop_id)
    lines = _refresh_line_prices(conn, shop_id, plan.get("order_lines", []))
    out = [_t("ord_saved_head")]
    errors = 0
    try:
        for ln in lines:
            if ln.get("customer_id") or not ln.get("customer_new"):
                continue
            if _get_customer(conn, shop_id, ln["customer"]) is not None:
                continue
            cur = conn.execute("INSERT INTO customers (shop_id, name)"
                               " VALUES (?, ?)", (shop_id, ln["customer"]))
            conn.execute("INSERT INTO actions (action_id, shop_id, kind, summary,"
                         " data_json) VALUES (?, ?, 'customer', ?, ?)",
                         (_action_id(), shop_id,
                          f"customer: {ln['customer']}",
                          json.dumps({"customer_id": cur.lastrowid,
                                      "name": ln["customer"]})))
        conn.commit()
    except Exception:
        log.warning("plan %s: customers failed", plan["plan_id"],
                    exc_info=True)
        errors += 1
    added = sorted({ln["customer"] for ln in lines
                    if ln.get("customer_new")})
    try:
        procure = procure_lines(conn, shop_id, lines)
        prev = get_order_draft(conn, shop_id)
        if procure:
            from app.tools import _save_order_draft
            _save_order_draft(conn, shop_id,
                              {"lines": procure, "sent": [],
                               "kind": "customer_order",
                               "plan_id": plan["plan_id"]})
            conn.execute("INSERT INTO actions (action_id, shop_id, kind, summary,"
                         " data_json) VALUES (?, ?, 'order', ?, ?)",
                         (_action_id(), shop_id,
                          f"order: {len(lines)} lines",
                          json.dumps({"plan_id": plan["plan_id"],
                                      "previous": prev})))
            conn.commit()
            out.append(_t("ord_saved_draft", n=len(lines)))
        else:
            out.append(_t("ord_saved_noprocure"))
    except Exception:
        log.warning("plan %s: order draft failed", plan["plan_id"],
                    exc_info=True)
        errors += 1
    if added:
        out.append(_t("ord_saved_cust", names=", ".join(added)))
    for task in plan.get("reminders", []):
        try:
            due = _dt.fromisoformat(task["due"])
            if not _same_reminder_exists(conn, shop_id, task["due"],
                                         task["summary"]):
                context = {"kind": "customer_order",
                           "plan_id": plan["plan_id"],
                           "label": task.get("summary", "")[:60]}
                save_reminder(conn, shop_id, due, task["summary"], context,
                              task.get("recurrence", "none"))
            out.append(confirm_text(due, task["summary"],
                                    task.get("recurrence", "none")))
        except Exception:
            log.warning("plan %s: reminder failed", plan["plan_id"],
                        exc_info=True)
            errors += 1
    for spec in plan.get("statements", []):
        try:
            mode = spec.get("mode", "named")
            if mode == "all":
                out.append(statements_for_all(conn, shop_id))
            elif mode == "top":
                out.append(statements_top_n(conn, shop_id,
                                            int(spec.get("n", 0))))
            else:
                out.append(statements_named(conn, shop_id,
                                            spec.get("names", [])))
        except Exception:
            log.warning("plan %s: statements failed", plan["plan_id"],
                        exc_info=True)
            errors += 1
    try:
        for task in plan.get("summaries", []):
            text = customer_summaries(conn, shop_id, lines,
                                      task.get("label", ""))
            if text:
                out.append(text)
    except Exception:
        log.warning("plan %s: summaries failed", plan["plan_id"],
                    exc_info=True)
        errors += 1
    if errors:
        out[0] = _t("ord_partial_head")
        out.append(_t("ord_part_fail"))
        conn.commit()
        return "\n\n".join(out)
    if plan.get("photo_digest"):
        from app.tools import record_photo
        record_photo(conn, shop_id, plan["photo_digest"], "order")
    conn.execute("UPDATE task_plans SET status = 'confirmed'"
                 " WHERE shop_id = ? AND plan_id = ?",
                 (shop_id, plan["plan_id"]))
    conn.commit()
    log.info("plan %s confirmed for %s (%d lines)", plan["plan_id"],
             shop_id, len(lines))
    return "\n\n".join(out)


def _same_reminder_exists(conn: sqlite3.Connection, shop_id: str, due_iso: str,
                          summary: str) -> bool:
    """True when an identical live reminder already exists (retry-safe)."""
    row = conn.execute("SELECT id FROM reminders WHERE shop_id = ?"
                       " AND due_at = ? AND summary = ?"
                       " AND status != 'cancelled' LIMIT 1",
                       (shop_id, due_iso, summary)).fetchone()
    return row is not None


# --- corrections --------------------------------------------------------------


def correct_plan_line(conn: sqlite3.Connection, shop_id: str, line_no: int,
                      rest: str) -> str:
    """Rewrite one order line ('line 3 is 4 bread for Sipho') and preview."""
    plan = get_plan(conn, shop_id)
    if plan is None:
        return _t("ord_expired")
    lines = plan.get("order_lines", [])
    if line_no < 1 or line_no > len(lines):
        return render_preview(conn, shop_id, plan)
    fresh = parse_order_lines(conn, shop_id, rest)
    if not fresh:
        price = _parse_price_cents(rest)
        if price is not None:
            target = lines[line_no - 1]
            item_words = re.sub(r"\br\s?\d+(?:[.,]\d{1,2})?\b", "",
                                rest, flags=re.IGNORECASE).strip()
            product = match_product(conn, shop_id, item_words) \
                if item_words else None
            if product is not None:
                target["product_id"] = product["id"]
                target["item"] = product["name"]
                target["matched"] = True
            target["price_cents"] = price
            target["price_override"] = True
            plan = _update_plan_lines(conn, shop_id, plan, lines)
            return render_preview(conn, shop_id, plan)
        return render_preview(conn, shop_id, plan)
    lines[line_no - 1:line_no] = fresh
    plan = _update_plan_lines(conn, shop_id, plan, lines)
    return render_preview(conn, shop_id, plan)


def _parse_price_cents(text: str) -> int | None:
    """Parse 'R18' / 'R18.50' into cents. None when absent."""
    m = re.search(r"\br\s?(\d+(?:[.,]\d{1,2})?)", text or "",
                  re.IGNORECASE)
    if not m:
        return None
    try:
        value = int(round(float(m.group(1).replace(",", ".")) * 100))
    except ValueError:
        return None
    return value if value > 0 else None


# --- send-time recompute ------------------------------------------------------


def render_saved_order(conn: sqlite3.Connection, shop_id: str, plan_id: str,
                       label: str = "") -> str:
    """Recompute a confirmed order from live stock and prices (reminders)."""
    from app.tools import account_balance, get_order_draft
    plan = get_plan_by_id(conn, shop_id, plan_id or "")
    if plan is None:
        return _t("rem_cord_gone")
    lines = plan.get("order_lines", [])
    head = _t("rem_cord_head", label=(label or " ").strip() or " ")
    procure = procure_lines(conn, shop_id, lines)
    if procure:
        text = head + "\n" + render_procurement(procure)
        need = sum(ln["est_cents"] for ln in procure
                   if ln["est_cents"] is not None)
        have = account_balance(conn, shop_id, "cash")
        text += _t("rem_cash_vs_need", have=cents_to_rand(have),
                   need=cents_to_rand(need))
        return text
    draft = get_order_draft(conn, shop_id)
    if draft is not None and (draft.get("plan_id") or "") == plan_id:
        return head
    return head
