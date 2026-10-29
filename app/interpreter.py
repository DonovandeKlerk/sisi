"""Unified interpretation: the model understands, code executes.

One planner call sees the whole message (or transcript/caption), the
media brief, recent history, open state, and live shop facts, and
returns a structured plan. Every task then runs through the existing
validators, previews, confirmations, and persistence — the model can
never write, confirm, schedule, or invent records on its own.

Exact control commands (Yes/No/Undo/menus/resets) and state-driven
follow-ups bypass the planner; everything else is interpreted here so
broad keyword rules can no longer hijack natural requests.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime

from app.i18n import t as _t
from app.llm import PLAN_KINDS, _shop_context, load_history, log_usage, propose_plan
from app.prompts import PLANNER_PROMPT

log = logging.getLogger("sisi.interpreter")

MAX_TASKS = 6
HISTORY_TURNS = 8


class _TaskFail(Exception):
    """A task that needs an answer: the message IS the follow-up question."""


# --- context ---------------------------------------------------------------


def _trim(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _open_state(conn: sqlite3.Connection, shop_id: str) -> list[str]:
    """Pending work the next message may continue, one line each."""
    from app.orders import get_clarify_context, get_plan
    from app.tools import (get_await_price, get_await_reminder, get_order_draft,
                           get_pending)
    lines: list[str] = []
    try:
        plan = get_plan(conn, shop_id)
        if plan is not None:
            n = len(plan.get("order_lines") or [])
            who = ", ".join(sorted({ln.get("customer", "?")
                                    for ln in plan.get("order_lines") or []}))
            lines.append(f"order plan proposed ({n} lines for {who or '?'}),"
                         " awaiting Yes/No")
        pend = get_pending(conn, shop_id)
        if pend is not None:
            lines.append(f"preview awaiting Yes/No:"
                         f" {_trim(pend['preview_text'], 120)}")
        wait = get_await_price(conn, shop_id)
        if wait is not None:
            lines.append(f"awaiting price for: {wait['product']}")
        rem = get_await_reminder(conn, shop_id)
        if rem is not None and rem["due_iso"]:
            try:
                when = datetime.fromisoformat(rem["due_iso"]).strftime("%H:%M")
            except ValueError:
                when = rem["due_iso"]
            lines.append(f"reminder detail needed (due {when})")
        ctx = get_clarify_context(conn, shop_id)
        if ctx is not None:
            vision = ctx.get("vision") or {}
            dtype = vision.get("doc_type", "?") if isinstance(vision, dict) \
                else "?"
            lines.append("photo clarification open"
                         f" (caption: {_trim(ctx['caption'], 80)};"
                         f" earlier photo: {dtype}, no resend needed)")
        draft = get_order_draft(conn, shop_id)
        if draft is not None:
            n = len((draft.get("lines") or []))
            lines.append(f"restock order draft open ({n} lines)")
    except Exception:  # noqa: BLE001 - state is a hint, never fatal
        log.warning("open-state snapshot failed", exc_info=True)
    return lines


def _names(conn: sqlite3.Connection, shop_id: str) -> str:
    try:
        custs = [r["name"] for r in conn.execute(
            "SELECT name FROM customers WHERE shop_id = ? ORDER BY name"
            " LIMIT 20", (shop_id,)).fetchall()]
        prods = [r["name"] for r in conn.execute(
            "SELECT name FROM products WHERE shop_id = ? ORDER BY name"
            " LIMIT 30", (shop_id,)).fetchall()]
        out = []
        if custs:
            out.append("CUSTOMERS: " + ", ".join(custs))
        if prods:
            out.append("PRODUCTS: " + ", ".join(prods))
        return "\n".join(out)
    except Exception:  # noqa: BLE001 - hint only
        return ""


def media_brief(doc: dict | None, caption: str, digest: str) -> str:
    """Compact vision reading, labeled as unverified evidence."""
    if not isinstance(doc, dict):
        return "PHOTO attached but unreadable: ask for a clearer picture."
    kind = doc.get("doc_type", "other")
    goods = []
    for p in doc.get("products", []) or []:
        if not isinstance(p, dict):
            continue
        label = " ".join(x for x in
                         (p.get("brand"), p.get("variant"), p.get("size"))
                         if x) or "unreadable label"
        n = p.get("visible_count")
        goods.append(label + (f" x{n} seen" if n else " (count unclear)")
                     + ("" if p.get("readable") else " [uncertain]")
                     + (f" ({p['notes']})" if p.get("notes") else ""))
    goods_txt = (" Packaged goods seen (unverified): " + "; ".join(goods)
                 + ".") if goods else ""
    cap = f" Caption: {caption!r}." if caption else ""
    if kind == "book_page":
        book = doc.get("book") or {}
        lines = book.get("lines") or []
        bad = [str(i + 1) for i, ln in enumerate(lines)
               if isinstance(ln, dict) and not (ln.get("item") or ln.get("text"))]
        return ("PHOTO EVIDENCE (handwritten book page, unverified — propose"
                f" from it, never auto-save): {len(lines)} lines"
                + (f", unreadable: {', '.join(bad)}" if bad else "")
                + goods_txt + cap)
    if kind == "receipt":
        rec = doc.get("receipt") or {}
        return ("PHOTO EVIDENCE (purchase receipt, unverified — propose from"
                f" it, never auto-save): {rec.get('supplier', '?')}"
                f" total {rec.get('total_cents', '?')}"
                + goods_txt + cap)
    if kind == "expiry":
        items = (doc.get("expiry") or {}).get("items", []) or []
        return (f"PHOTO EVIDENCE (shelf/expiry check): {len(items)} dated items"
                + goods_txt + cap)
    if goods:
        return ("PHOTO EVIDENCE (packaged goods, unverified — propose from"
                " it, never auto-save): " + "; ".join(goods) + "." + cap)
    if caption:
        return f"PHOTO attached (no readable document).{cap}"
    return "PHOTO attached (no readable document, no caption)."


def build_context(conn: sqlite3.Connection, shop_id: str, text: str, *,
                  brief: str = "", voice: bool = False,
                  sender_note: str = "sender: owner",
                  sent_at: datetime | None = None) -> str:
    """Everything the planner needs, in one bounded block."""
    from app.i18n import owner_lang
    from app.tools import account_balance
    from app.db import cents_to_rand
    parts = [f"NEW MESSAGE ({sender_note}; "
             f"owner language: {owner_lang(conn, shop_id)}):",
             _trim(text, 2000)]
    if sent_at is not None and hasattr(sent_at, "isoformat"):
        parts.append(f"(sent at {sent_at.isoformat()}; Africa/Johannesburg)")
    if voice:
        parts.append("(voice-note transcript: may hold disfluencies"
                     " and repetition)")
    if brief:
        parts.append(brief)
    try:
        turns = load_history(conn, shop_id, HISTORY_TURNS)
    except Exception:  # noqa: BLE001 - hint only
        turns = []
    if turns:
        parts.append("RECENT CONVERSATION:")
        for t in turns:
            who = "owner" if t["role"] == "user" else "sisi"
            parts.append(f"- {who}: {_trim(t['content'], 200)}")
    state = _open_state(conn, shop_id)
    parts.append("OPEN STATE:"
                 + (f"\n" + "\n".join(f"- {s}" for s in state) if state
                    else " none"))
    snap = _shop_context(conn, shop_id)
    try:
        cash = cents_to_rand(account_balance(conn, shop_id, "cash"))
        petty = cents_to_rand(account_balance(conn, shop_id, "petty"))
        bank = cents_to_rand(account_balance(conn, shop_id, "bank"))
        snap += f"\nCASH: till {cash}, petty {petty}, bank {bank}"
    except Exception:  # noqa: BLE001 - hint only
        pass
    names = _names(conn, shop_id)
    if names:
        snap += "\n" + names
    if snap.strip():
        parts.append("LIVE SHOP FACTS (use tools/plans to act, never guess):")
        parts.append(_trim(snap, 2500))
    return "\n".join(parts)


# --- interpretation ----------------------------------------------------------


def _parse_plan_body(content: str) -> dict | None:
    """Plan JSON out of model content (tolerates fenced replies)."""
    text = (content or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        body = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _clean_understood(text: str | None) -> str:
    """Echo line: plain words only (the model sometimes trails garbage)."""
    clean = re.sub(r"[^\w\s.,!?':;()\-]", " ", str(text or ""), flags=re.UNICODE)
    return _trim(re.sub(r"\s+", " ", clean), 100)


def validate_plan(body: dict | None) -> dict:
    """Structural check. Returns a clean plan or raises ValueError."""
    if not isinstance(body, dict):
        raise ValueError("plan is not an object")
    tasks = body.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("tasks is not a list")
    clean: list[dict] = []
    for task in tasks[:MAX_TASKS]:
        if not isinstance(task, dict):
            continue
        if task.get("kind") not in PLAN_KINDS:
            continue
        args = task.get("args")
        clean.append({"kind": task["kind"],
                      "quote": _trim(task.get("quote"), 300),
                      "args": args if isinstance(args, dict) else {}})
    question = body.get("question")
    options = body.get("options") or []
    return {
        "understood": _clean_understood(body.get("understood")),
        "tasks": clean,
        "question": _trim(question, 200) if isinstance(question, str)
        and question.strip() else None,
        "options": [o.strip()[:20] for o in options
                    if isinstance(o, str) and o.strip()][:3],
    }


def interpret(conn: sqlite3.Connection, shop_id: str, text: str, *,
              brief: str = "", voice: bool = False,
              sender_note: str = "sender: owner",
              sent_at: datetime | None = None) -> dict:
    """One planned reading of the message. Never raises, never writes.

    Model failure, bad JSON, or an empty plan degrades to a single
    recoverable question — the caller executes zero tasks.
    """
    from app import config
    context = build_context(conn, shop_id, text, brief=brief, voice=voice,
                            sender_note=sender_note, sent_at=sent_at)
    try:
        resp = propose_plan([
            {"role": "system", "content": PLANNER_PROMPT},
            {"role": "user", "content": context},
        ])
    except Exception:  # noqa: BLE001 - outage asks, never crashes
        log.warning("planner call failed", exc_info=True)
        return {"understood": "", "tasks": [],
                "question": _t("llm_dropped"), "options": []}
    try:
        log_usage(shop_id, config.model_name(),
                  getattr(resp, "usage", None), 1)
        content = resp.choices[0].message.content
    except Exception:  # noqa: BLE001 - malformed response asks
        log.warning("planner response unreadable", exc_info=True)
        return {"understood": "", "tasks": [],
                "question": _t("llm_dropped"), "options": []}
    try:
        plan = validate_plan(_parse_plan_body(content))
    except ValueError:
        log.warning("planner output invalid: %r", (content or "")[:200])
        return {"understood": "", "tasks": [],
                "question": _t("llm_say_again"), "options": []}
    if not plan["tasks"] and not plan["question"]:
        plan["question"] = _t("llm_say_again")
    return plan


# --- task validation ---------------------------------------------------------


def _customer(name: str | None) -> str:
    """A roster-safe customer name, else a follow-up question."""
    from app.reminders import PRONOUNS
    clean = " ".join((name or "").strip().split())
    if not clean or clean.lower() in PRONOUNS:
        raise _TaskFail(_t("cust_which"))
    return clean


def _items(raw: object, *, need_qty: bool = False) -> list[dict]:
    """Validated [{product, qty, unit?}]. Quantities stay singles."""
    if not isinstance(raw, list) or not raw:
        raise _TaskFail(_t("how_many"))
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        product = " ".join(str(entry.get("product") or "").split())
        if not product:
            continue
        try:
            qty = float(entry.get("qty", 1))
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            if need_qty:
                raise _TaskFail(_t("how_many"))
            qty = 1
        unit = entry.get("unit")
        out.append({"product": product, "qty": qty,
                    "unit": str(unit) if unit else None})
    if not out:
        raise _TaskFail(_t("how_many"))
    return out


def _cents(value: object) -> int | None:
    try:
        amount = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def _chat_ok(reply: str) -> str:
    """Conversational replies: short, no amounts, no done-claims."""
    clean = (reply or "").strip()
    if not clean or len(clean) > 600:
        raise _TaskFail(_t("llm_say_again"))
    if re.search(r"\bR\s?\d", clean):
        raise _TaskFail(_t("llm_say_again"))
    return clean


# --- executors (each returns reply text; buttons via suggestion) --------------

_SUG_AFTER_REMINDERS = ["Set reminder", "Menu"]
_SUG_AFTER_STATEMENT = ["All statements", "Set reminder", "Menu"]


def _exec_reminder(conn, shop_id, args, bundle):
    import app.agent as agent
    from app.reminders import (parse_request, reminder_clause,
                               resolve_topic, summary_is_bare)
    when = reminder_clause(str(args.get("when") or ""))
    if not when or "?" in when:
        raise _TaskFail(_t("rem_ask_when"))
    full = bundle.get("text") or ""
    given = args.get("topic")
    wording = None
    if isinstance(given, str) and given.strip() \
            and not summary_is_bare(given):
        topic = resolve_topic(given, full) or given.strip()
        wording = topic if not summary_is_bare(topic) else None
    parsed = parse_request(f"remind me {when}", bundle.get("sent_at"))
    if parsed is None:
        raise _TaskFail(_t("llm_say_again"))
    if wording is None:
        topic = resolve_topic(parsed["summary"], full)
        if isinstance(topic, str) and topic.strip() \
                and not summary_is_bare(topic):
            wording = topic
    rec = str(args.get("recurrence") or "").strip().lower()
    rec_words = {"daily": "every day", "weekly": "every week",
                 "monthly": "every month"}.get(rec, "")
    # Append the resolved topic only when `when` carries no wording of
    # its own; otherwise it doubles ("get ready get ready ..."). The
    # wording UPDATE below still applies the final topic.
    canonical = f"remind me {when}".strip()
    if wording is not None and summary_is_bare(parsed["summary"]):
        canonical += f" {wording.strip()}"
    if rec_words:
        canonical += f" {rec_words}"
    before = conn.execute("SELECT COALESCE(MAX(id), 0) m FROM reminders"
                          " WHERE shop_id = ?", (shop_id,)).fetchone()["m"]
    out = agent.try_reminder_command(conn, shop_id, canonical,
                                     bundle.get("sent_at"))
    if out is None:
        from app.reminders import has_explicit_time
        if has_explicit_time(canonical):
            raise _TaskFail(_t("cust_which"))  # pronoun-only topic
        raise _TaskFail(_t("rem_ask_when"))  # no usable time either
    if wording is not None:
        # parsing lowercases; keep the planner's well-formed wording
        row = conn.execute("SELECT id, due_at, recurrence FROM reminders"
                           " WHERE shop_id = ? AND id > ?"
                           " ORDER BY id DESC LIMIT 1",
                           (shop_id, before)).fetchone()
        if row is not None:
            from app.reminders import confirm_text
            conn.execute("UPDATE reminders SET summary = ? WHERE id = ?",
                         (wording.strip()[:120], row["id"]))
            conn.commit()
            out = confirm_text(datetime.fromisoformat(row["due_at"]),
                               wording.strip()[:120], row["recurrence"])
    return out, _SUG_AFTER_REMINDERS


def _exec_reminder_offer(conn, shop_id, args, bundle):
    from app.reminders import due_passed, parse_request
    from app.tools import propose_reminder_offer
    when = str(args.get("when") or "").strip()
    topic = str(args.get("topic") or "").strip()
    if not when or not topic:
        raise _TaskFail(_t("rem_ask_when"))
    parsed = parse_request(f"remind me {when} {topic}", bundle.get("sent_at"))
    if parsed is None:
        raise _TaskFail(_t("rem_ask_when"))
    if due_passed(parsed["due"], bundle.get("sent_at")):
        return _t("rem_past", when=parsed["due"].strftime("%H:%M")), None
    return propose_reminder_offer(conn, shop_id, parsed["due"].isoformat(),
                                  parsed["summary"]), None


def _exec_reminder_list(conn, shop_id, args, bundle):
    from app.reminders import list_reminders
    return list_reminders(conn, shop_id), _SUG_AFTER_REMINDERS


def _exec_reminder_cancel(conn, shop_id, args, bundle):
    from app.reminders import cancel_numbered
    try:
        number = int(args.get("number"))
    except (TypeError, ValueError):
        number = 0
    return cancel_numbered(conn, shop_id, number), None


def _exec_reminder_move(conn, shop_id, args, bundle):
    from app.reminders import move_numbered
    try:
        number = int(args.get("number"))
    except (TypeError, ValueError):
        number = 0
    return move_numbered(conn, shop_id, number,
                         str(args.get("when") or "")), None


def _exec_reminder_snooze(conn, shop_id, args, bundle):
    from app.reminders import snooze_last
    try:
        minutes = int(args.get("minutes"))
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        raise _TaskFail(_t("rem_ask_when"))
    return snooze_last(conn, shop_id, minutes), None


def _exec_order(conn, shop_id, args, bundle):
    import app.agent as agent
    from app.orders import _has_blocker, is_supplier_segment
    quote = str(bundle.get("quote") or "")
    customer = args.get("customer")
    if isinstance(customer, str) and customer.strip():
        from app.reminders import PRONOUNS
        if customer.strip().lower() in PRONOUNS:
            raise _TaskFail(_t("cust_which"))
        customer = " ".join(customer.split())
    else:
        customer = None
    if quote.strip():
        segment = quote.strip()  # planner-quoted span parses verbatim
    else:
        items = args.get("items") or []
        bits = []
        for entry in items if isinstance(items, list) else []:
            if not isinstance(entry, dict):
                continue
            product = str(entry.get("product") or "").strip()
            if not product:
                continue
            try:
                qty = float(entry.get("qty", 1))
            except (TypeError, ValueError):
                qty = 1
            unit = f"{entry['unit']} " if entry.get("unit") else ""
            bits.append(f"{qty:g} {unit}{product}".strip())
        segment = ", ".join(bits)
        if customer and segment:
            segment = f"{customer}: {segment}"
    siblings = bundle.get("sibling_kinds") or set()
    if siblings & {"statement", "statements_all", "reminder"}:
        # sibling tasks own those segments: keep them out of the plan so
        # statements/reminders fire once instead of twice
        from app.orders import split_segments
        from app.reminders import REMIND_TRIGGERS
        kept = []
        for seg in split_segments(segment):
            low = f" {seg.lower()} "
            if siblings & {"statement", "statements_all"} and any(
                    t in low for t in agent._STATEMENT_TRIGGERS):
                continue
            if "reminder" in siblings and any(
                    t in low for t in REMIND_TRIGGERS):
                continue
            kept.append(seg)
        segment = " ".join(kept)
    if not segment.strip():
        raise _TaskFail(_t("ord_clarify"))
    if is_supplier_segment(segment) or _has_blocker(segment):
        raise _TaskFail(_t("ord_cust_or_stock"))
    out = agent.try_order_plan(
        conn, shop_id, segment, bundle.get("sent_at"),
        bundle.get("digest") or "", bundle.get("doc"),
        bool(bundle.get("digest")), bundle.get("digest") or "")
    if out is None:
        raise _TaskFail(_t("ord_clarify"))
    return out, None


def _exec_sale(conn, shop_id, args, bundle):
    from app.tools import record_sale
    items = _items(args.get("items"))
    buyer = args.get("buyer")
    if isinstance(buyer, str) and buyer.strip():
        buyer = _customer(buyer)
    else:
        buyer = None
    return "\n\n".join(record_sale(conn, shop_id, it["product"], it["qty"],
                                  buyer=buyer, unit=it["unit"])
                       for it in items), None


def _exec_credit(conn, shop_id, args, bundle):
    from app.tools import add_credit
    return add_credit(conn, shop_id, _customer(args.get("customer")),
                      _items(args.get("items"))), None


def _exec_payment(conn, shop_id, args, bundle):
    from app.tools import record_payment
    amount = _cents(args.get("amount_cents"))
    if amount is None:
        raise _TaskFail(_t("how_much"))
    return record_payment(conn, shop_id, _customer(args.get("customer")),
                          amount), None


def _exec_stock_add(conn, shop_id, args, bundle):
    from app.tools import (add_stock, get_pending, propose_stock_intake,
                           start_guided_intake)
    raw_items = [e for e in args.get("items") or [] if isinstance(e, dict)]
    doc = bundle.get("doc") or {}
    is_photo = "digest" in bundle or (
        isinstance(doc, dict) and bool(doc.get("products")))
    if is_photo:
        # photo intake: guided one-product-at-a-time session (grounding
        # happens inside start_guided_intake). Text multi-item keeps the
        # one-shot preview below.
        from app.db import now_iso
        if get_pending(conn, shop_id) is not None:
            # _propose used to supersede this; a fresh photo intake still
            # replaces any live preview so Yes can't confirm stale lines.
            conn.execute(
                "UPDATE pending_actions SET status = 'superseded',"
                " resolved_at = ? WHERE shop_id = ? AND status = 'pending'",
                (now_iso(), shop_id))
            conn.commit()
        caption = bundle.get("caption") or bundle.get("cap") \
            or bundle.get("text") or bundle.get("quote") or ""
        products = doc.get("products", []) if isinstance(doc, dict) else []
        return start_guided_intake(
            conn, shop_id, products, raw_items, caption,
            bundle.get("digest") or "", args.get("supplier")), None
    if len(raw_items) > 1:
        # text multi-item: one editable preview, one confirm
        prior = None
        pend = get_pending(conn, shop_id)
        if pend is not None and pend["action_type"] == "stock_intake" \
                and not bundle.get("digest"):
            import json as _json
            prior = _json.loads(pend["payload_json"])
        return propose_stock_intake(
            conn, shop_id, doc.get("products", []) if isinstance(doc, dict)
            else [], raw_items, args.get("supplier"),
            bundle.get("digest") or "", prior), None
    items = _items(args.get("items"), need_qty=True)
    supplier = args.get("supplier")
    parts = []
    for it in items:
        raw = next((e for e in (args.get("items") or [])
                    if isinstance(e, dict)
                    and str(e.get("product") or "").strip() == it["product"]),
                   {})
        parts.append(add_stock(
            conn, shop_id, it["product"], it["qty"],
            wholesaler=str(supplier).strip() if supplier else None,
            cost_cents=_cents(raw.get("cost_cents")),
            unit=it["unit"]))
    return "\n\n".join(parts), None


def _exec_price_set(conn, shop_id, args, bundle):
    from app.tools import set_price
    product = str(args.get("product") or "").strip()
    amount = _cents(args.get("amount_cents"))
    if not product or amount is None:
        raise _TaskFail(_t("how_much"))
    return set_price(conn, shop_id, product, amount), None


def _exec_expense(conn, shop_id, args, bundle):
    from app.tools import record_expense
    amount = _cents(args.get("amount_cents"))
    if amount is None:
        raise _TaskFail(_t("how_much"))
    return record_expense(
        conn, shop_id, amount,
        category=str(args.get("category") or "") or None,
        account=str(args.get("account") or "") or None,
        note=str(args.get("note") or "") or None), None


def _exec_drawing(conn, shop_id, args, bundle):
    from app.tools import record_drawing
    amount = _cents(args.get("amount_cents"))
    if amount is None:
        raise _TaskFail(_t("how_much"))
    return record_drawing(
        conn, shop_id, amount,
        account=str(args.get("account") or "") or None), None


def _exec_count(conn, shop_id, args, bundle):
    from app.tools import record_count
    amount = _cents(args.get("amount_cents"))
    if amount is None:
        raise _TaskFail(_t("how_much"))
    return record_count(conn, shop_id, amount), None


def _exec_supplier_payment(conn, shop_id, args, bundle):
    from app.tools import record_supplier_payment
    supplier = str(args.get("supplier") or "").strip()
    amount = _cents(args.get("amount_cents"))
    if not supplier or amount is None:
        raise _TaskFail(_t("how_much"))
    return record_supplier_payment(
        conn, shop_id, supplier, amount,
        account=str(args.get("account") or "") or None), None


def _exec_cash(conn, shop_id, args, bundle):
    from app.tools import cash_balances
    return cash_balances(conn, shop_id), None


def _exec_sales(conn, shop_id, args, bundle):
    from app.tools import sales_summary
    period = str(args.get("period") or "today").lower()
    if period not in ("today", "yesterday", "week", "month"):
        period = "today"
    return sales_summary(conn, shop_id, period), None


def _exec_profit(conn, shop_id, args, bundle):
    from app.tools import profit_month
    month = str(args.get("month") or "").strip() or None
    return profit_month(conn, shop_id, month), None


def _exec_cashup(conn, shop_id, args, bundle):
    from app.tools import cash_up
    return cash_up(conn, shop_id), None


def _exec_best_sellers(conn, shop_id, args, bundle):
    from app.tools import best_sellers
    return best_sellers(conn, shop_id), None


def _exec_show_profile(conn, shop_id, args, bundle):
    from app.tools import show_shop_profile
    return show_shop_profile(conn, shop_id), None


def _exec_debtors(conn, shop_id, args, bundle):
    from app.tools import list_debtors
    return list_debtors(conn, shop_id), None


def _exec_balance(conn, shop_id, args, bundle):
    from app.tools import get_balance
    return get_balance(conn, shop_id, _customer(args.get("customer"))), None


def _exec_statement(conn, shop_id, args, bundle):
    import app.agent as agent
    out = agent.try_statement_request(
        conn, shop_id, f"statement for {_customer(args.get('customer'))}")
    if out is None:
        raise _TaskFail(_t("cust_which"))
    return out, _SUG_AFTER_STATEMENT


def _exec_statements_all(conn, shop_id, args, bundle):
    import app.agent as agent
    from app.tools import statements_for_all
    names = args.get("names")
    if isinstance(names, list) and names:
        clean = [" ".join(str(n).split()) for n in names
                 if str(n).strip()][:10]
        if clean:
            out = agent.try_multi_statement_request(
                conn, shop_id, "statements for " + ", ".join(clean))
            if out is not None:
                return out, _SUG_AFTER_STATEMENT
            raise _TaskFail(_t("cust_which"))
    try:
        top_n = int(args.get("top_n"))
    except (TypeError, ValueError):
        top_n = 0
    if top_n > 0:
        out = agent.try_multi_statement_request(
            conn, shop_id, f"statements for {top_n} creditors")
        if out is not None:
            return out, _SUG_AFTER_STATEMENT
        raise _TaskFail(_t("cust_which"))
    return statements_for_all(conn, shop_id), _SUG_AFTER_STATEMENT


def _exec_receipts(conn, shop_id, args, bundle):
    from app.tools import list_receipts
    month = str(args.get("month") or "").strip() or None
    return list_receipts(conn, shop_id, month), None


def _exec_report(conn, shop_id, args, bundle):
    import os as _os
    import app.agent as agent
    from app import reports
    from app.tools import parse_month_name
    month = parse_month_name(str(args.get("month") or ""))
    text, media = reports.build_month_end(
        conn, shop_id, month, _os.environ.get("SISI_PUBLIC_URL", ""))
    agent.queue_media(shop_id, media)
    return text, None


def _exec_compliance(conn, shop_id, args, bundle):
    from app.tools import compliance_guide
    return compliance_guide(conn, shop_id,
                            str(args.get("topic") or "overview")), None


def _exec_book_page(conn, shop_id, args, bundle):
    from app.tools import propose_book_page
    doc = bundle.get("doc") or {}
    if not isinstance(doc, dict) or doc.get("doc_type") != "book_page":
        raise _TaskFail(_t("ag_photo_unknown"))
    return propose_book_page(conn, shop_id, doc.get("book", {}),
                             photo_hash=bundle.get("digest", "")), None


def _exec_receipt_doc(conn, shop_id, args, bundle):
    from app.tools import propose_receipt
    doc = bundle.get("doc") or {}
    if not isinstance(doc, dict) or doc.get("doc_type") != "receipt":
        raise _TaskFail(_t("ag_photo_unknown"))
    return propose_receipt(conn, shop_id, doc.get("receipt", {}),
                           photo_hash=bundle.get("digest", "")), None


def _exec_expiry_photo(conn, shop_id, args, bundle):
    from app.tools import check_expiry_photo
    doc = bundle.get("doc") or {}
    if not isinstance(doc, dict) or doc.get("doc_type") != "expiry":
        raise _TaskFail(_t("ag_photo_unknown"))
    return check_expiry_photo(conn, shop_id,
                              doc.get("expiry", {}).get("items", []),
                              photo_hash=bundle.get("digest", "")), None


def _exec_logo(conn, shop_id, args, bundle):
    import app.agent as agent
    image = bundle.get("image")
    if not image:
        return _t("logo_howto"), None
    out = agent.stage_shop_logo(conn, shop_id, image)
    return out, None


def _exec_affordability(conn, shop_id, args, bundle):
    import app.agent as agent
    amount = _cents(args.get("amount_cents"))
    if amount is None:
        return agent.try_affordability(conn, shop_id, "what is available to pay"), None
    rand = amount / 100
    return agent.try_affordability(conn, shop_id, f"can we afford R{rand:g}"), None


def _exec_set_language(conn, shop_id, args, bundle):
    from app.i18n import normalize_lang
    from app.tools import set_owner_language
    lang = str(args.get("lang") or "")
    if normalize_lang(lang) is None:
        raise _TaskFail(_t("llm_say_again"))
    return set_owner_language(conn, shop_id, lang), None


def _exec_set_profile(conn, shop_id, args, bundle):
    from app.tools import (set_shop_address, set_shop_name, set_shop_phone)
    field = str(args.get("field") or "").lower()
    value = str(args.get("value") or "").strip()
    if not value:
        raise _TaskFail(_t("llm_say_again"))
    if field == "name":
        return set_shop_name(conn, shop_id, value), None
    if field == "address":
        return set_shop_address(conn, shop_id, value), None
    if field == "phone":
        return set_shop_phone(conn, shop_id, value), None
    raise _TaskFail(_t("llm_say_again"))


def _exec_float_set(conn, shop_id, args, bundle):
    from app.tools import set_float
    amounts = {}
    for key in ("cash", "petty", "bank"):
        got = _cents(args.get(f"{key}_cents"))
        if got is not None:
            amounts[key] = got
    if not amounts:
        raise _TaskFail(_t("how_much"))
    return set_float(conn, shop_id, amounts), None


def _exec_nudge(conn, shop_id, args, bundle):
    from app.tools import set_nudge
    return set_nudge(conn, shop_id, str(args.get("setting") or "")), None


def _exec_daily_plan(conn, shop_id, args, bundle):
    from app.schedule import daily_plan, sast_now, set_daily_plan
    setting = str(args.get("setting") or "").strip()
    if not setting:
        return daily_plan(conn, shop_id, sast_now().date().isoformat()), None
    return set_daily_plan(conn, shop_id, setting), None


def _exec_staff_add(conn, shop_id, args, bundle):
    from app.tools import add_staff
    digits = re.sub(r"\D", "", str(args.get("number") or ""))
    name = str(args.get("name") or "").strip()
    if len(digits) < 9 or not name:
        raise _TaskFail(_t("llm_say_again"))
    return add_staff(conn, shop_id, digits, name), None


def _exec_who_recorded(conn, shop_id, args, bundle):
    from app.tools import who_recorded_today
    return who_recorded_today(conn, shop_id), None


def _exec_limit_set(conn, shop_id, args, bundle):
    from app.tools import set_customer_limit
    amount = _cents(args.get("amount_cents"))
    if amount is None:
        raise _TaskFail(_t("how_much"))
    return set_customer_limit(conn, shop_id, _customer(args.get("customer")),
                              amount), None


def _exec_expiry_set(conn, shop_id, args, bundle):
    from app.tools import set_expiry
    product = str(args.get("product") or "").strip()
    when = str(args.get("when") or "").strip()
    if not product or not when:
        raise _TaskFail(_t("llm_say_again"))
    return set_expiry(conn, shop_id, product, when), None


def _exec_stock_list(conn, shop_id, args, bundle):
    from app.tools import list_stock
    return list_stock(conn, shop_id), None


def _exec_low_stock(conn, shop_id, args, bundle):
    from app.tools import low_stock
    return low_stock(conn, shop_id), None


def _exec_restock(conn, shop_id, args, bundle):
    from app.tools import build_order
    return build_order(conn, shop_id), None


def _exec_order_send(conn, shop_id, args, bundle):
    from app.tools import send_order
    whole = str(args.get("wholesaler") or "").strip() or None
    return send_order(conn, shop_id, whole), None


def _exec_order_add(conn, shop_id, args, bundle):
    import app.agent as agent
    from app.tools import edit_order_add, get_order_draft
    if get_order_draft(conn, shop_id) is None:
        return _t("order_no_draft"), None
    product = str(args.get("product") or "").strip()
    try:
        qty = float(args.get("qty", 1))
    except (TypeError, ValueError):
        qty = 1
    parsed = agent._parse_order_add(f"add {product} {qty:g}")
    if parsed is None:
        return _t("ag_order_add_hint"), None
    return edit_order_add(conn, shop_id, *parsed), None


def _exec_order_remove(conn, shop_id, args, bundle):
    from app.tools import edit_order_remove, get_order_draft
    if get_order_draft(conn, shop_id) is None:
        return _t("order_no_draft"), None
    product = str(args.get("product") or "").strip()
    if not product:
        raise _TaskFail(_t("llm_say_again"))
    return edit_order_remove(conn, shop_id, product), None


def _exec_draft(conn, shop_id, args, bundle):
    from app.tools import draft_reminder
    return draft_reminder(conn, shop_id, _customer(args.get("customer"))), None


def _exec_chat(conn, shop_id, args, bundle):
    return _chat_ok(str(args.get("reply") or "")), None


_EXECUTORS = {
    "reminder": _exec_reminder, "reminder_offer": _exec_reminder_offer,
    "reminder_list": _exec_reminder_list,
    "reminder_cancel": _exec_reminder_cancel,
    "reminder_move": _exec_reminder_move,
    "reminder_snooze": _exec_reminder_snooze, "order": _exec_order,
    "sale": _exec_sale, "credit": _exec_credit, "payment": _exec_payment,
    "stock_add": _exec_stock_add, "stock_list": _exec_stock_list,
    "low_stock": _exec_low_stock, "restock": _exec_restock,
    "order_send": _exec_order_send, "order_add": _exec_order_add,
    "order_remove": _exec_order_remove, "price_set": _exec_price_set,
    "expense": _exec_expense, "drawing": _exec_drawing, "count": _exec_count,
    "supplier_payment": _exec_supplier_payment, "cash": _exec_cash,
    "sales": _exec_sales, "profit": _exec_profit, "cashup": _exec_cashup,
    "best_sellers": _exec_best_sellers, "show_profile": _exec_show_profile,
    "debtors": _exec_debtors,
    "balance": _exec_balance, "statement": _exec_statement,
    "statements_all": _exec_statements_all, "receipts": _exec_receipts,
    "report": _exec_report, "compliance": _exec_compliance,
    "book_page": _exec_book_page, "receipt_doc": _exec_receipt_doc,
    "expiry_photo": _exec_expiry_photo, "logo": _exec_logo,
    "affordability": _exec_affordability,
    "set_language": _exec_set_language, "set_profile": _exec_set_profile,
    "float_set": _exec_float_set, "nudge": _exec_nudge,
    "daily_plan": _exec_daily_plan, "staff_add": _exec_staff_add,
    "who_recorded": _exec_who_recorded, "limit_set": _exec_limit_set,
    "expiry_set": _exec_expiry_set, "draft": _exec_draft, "chat": _exec_chat,
}


def execute_plan(conn: sqlite3.Connection, shop_id: str, plan: dict,
                 bundle: dict) -> tuple[str, list[str] | None]:
    """Run validated tasks in order. Each failure stays local to its task."""
    tasks = plan.get("tasks") or []
    if not tasks:
        return (plan.get("question") or _t("llm_say_again"),
                plan.get("options") or None)
    parts: list[str] = []
    suggest: list[str] | None = None
    bundle["sibling_kinds"] = {t.get("kind") for t in tasks
                               if isinstance(t, dict)}
    for task in tasks:
        run = _EXECUTORS.get(task.get("kind"))
        if run is None:
            continue
        bundle["quote"] = task.get("quote") or ""
        try:
            text, sug = run(conn, shop_id, task.get("args") or {}, bundle)
        except _TaskFail as e:
            text, sug = str(e), None
        except Exception as e:  # noqa: BLE001 - one task never kills the rest
            log.warning("task %s failed", task.get("kind"), exc_info=True)
            text, sug = _t("task_failed", kind=task.get("kind"),
                           err=type(e).__name__), None
        parts.append(text)
        suggest = suggest or sug
    if not parts:
        return (plan.get("question") or _t("llm_say_again"),
                plan.get("options") or None)
    msg = "\n\n".join(parts)
    if len(tasks) >= 2 and plan.get("understood"):
        msg = "👂 " + plan["understood"] + "\n\n" + msg
    return msg, suggest
