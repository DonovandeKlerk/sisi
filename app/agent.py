"""Message router. Yes/No/Undo are handled deterministically here, never by the model."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from app import reports
from app.db import read_only, today_str
from app.i18n import (CURRENT_LANG, NAMES, STRINGS, detect, normalize_lang,
                      owner_lang, t as _t)
from app.setup import (
    answer as setup_answer,
    mark_paused,
    needs_setup,
    setup_step,
    start_setup,
)
from app.llm import chat_full as llm_chat_full
from app.llm import save_turn
from app.orders import (
    build_plan,
    cancel_plan,
    confirm_plan,
    correct_plan_line,
    get_plan,
    get_plan_by_key,
    plan_followup_message,
    clear_clarify_context,
    render_preview,
    resume_plan_from_clarify,
    save_clarify_context,
    save_plan,
)

log = logging.getLogger("sisi.agent")
from app.vision import VisionError, read_expiry_photo, read_shop_photo
from app.voice import VoiceError, transcribe_audio
from app.tools import (
    _is_pack_unit,
    _pack_echo,
    add_credit,
    answer_intake_session,
    answer_link_choice,
    ask_intake_field,
    build_order,
    cancel_deferred_link,
    cancel_pending,
    get_intake_session,
    guided_reprompt,
    edit_order_add,
    edit_order_remove,
    get_order_draft,
    guided_skip_pending,
    set_float,
    check_expiry_photo,
    clear_await_price,
    clear_await_reminder,
    confirm_pending,
    correct_book_line,
    correct_intake_line,
    correct_link_line,
    correct_receipt_account,
    correct_receipt_line,
    delete_my_data,
    demo_reset,
    ensure_pack_price,
    ensure_product_price,
    ensure_shop,
    find_product,
    get_await_price,
    get_await_reminder,
    get_pending,
    set_await_reminder,
    photo_seen,
    parse_month_name,
    propose_book_page,
    propose_receipt,
    propose_reminder_offer,
    add_staff,
    confirm_reset_shop,
    customer_statement,
    record_sale,
    record_sale_multi,
    resend_last_media,
    resend_last_reply,
    resend_recovery,
    resolve_sender,
    reset_shop,
    resume_deferred_link,
    save_intake_session,
    confirm_logo,
    stage_shop_logo,
    apply_staged_logo,
    discard_staged_logo,
    get_staged_logo,
    want_new_logo,
    clear_logo_pending,
    logo_as_is,
    logo_pending,
    set_customer_language,
    set_customer_limit,
    set_expiry,
    set_owner_language,
    set_nudge,
    who_recorded_today,
    statements_for_all,
    statements_named,
    statements_top_n,
    send_order,
    set_shop_address,
    set_shop_name,
    set_shop_phone,
    show_shop_profile,
    undo_last,
    undo_last_kind,
)

MEDIA_OUTBOX: dict[str, list[dict]] = {}
"""Queued image/file sends per shop; main.py drains after the text reply."""


def queue_media(shop_id: str, items: list[dict]) -> None:
    if items:
        MEDIA_OUTBOX.setdefault(shop_id, []).extend(items)


def take_media(shop_id: str) -> list[dict]:
    return MEDIA_OUTBOX.pop(shop_id, [])


LIST_OUTBOX: dict[str, list[dict]] = {}
"""Queued interactive lists per shop; main.py drains after the text reply."""


def queue_list(shop_id: str, body: str, button_text: str,
               rows: list[str]) -> None:
    if rows:
        LIST_OUTBOX.setdefault(shop_id, []).append(
            {"body": body, "button_text": button_text, "rows": list(rows)})


def take_lists(shop_id: str) -> list[dict]:
    return LIST_OUTBOX.pop(shop_id, [])


MENU_WORDS = {"menu", "hi", "hello", "hey", "imenyu"}

MENU_ROWS = ["Sell", "Stock", "Who owes me", "Statements", "Reports",
             "Reminders", "Settings"]
"""Main menu: each row is typed text the router already handles."""


def show_menu(conn: sqlite3.Connection, shop_id: str) -> str:
    """Queue the main interactive list; the intro text is the reply."""
    _store_options(conn, shop_id, MENU_ROWS)
    queue_list(shop_id, _t("menu_intro"), _t("menu_button"), MENU_ROWS)
    return _t("menu_intro")


_OPTIONS_TTL = timedelta(minutes=10)


def _store_options(conn: sqlite3.Connection, shop_id: str,
                   options: list[str]) -> None:
    """Remember offered buttons/list rows so '1' taps back to typed text."""
    if read_only():
        return  # testing mode: no operational memory writes either
    conn.execute(
        "INSERT INTO flags (shop_id, key, value) VALUES (?, 'last-options', ?)"
        " ON CONFLICT (shop_id, key) DO UPDATE SET value = excluded.value",
        (shop_id, json.dumps({
            "options": list(options),
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        })))
    conn.commit()


def _resolve_numbered(conn: sqlite3.Connection, shop_id: str,
                      text: str) -> str:
    """Map a bare '1'..'10' to the stored option. Otherwise return as-is.

    Skipped while setup runs (digits are answers there) and while a
    reminder-time offer is pending (a digit may be the hour).
    """
    m = re.match(r"\s*(\d{1,2})\s*[.)]?\s*$", text or "")
    if m is None:
        return text
    n = int(m.group(1))
    if n < 1:
        return text
    if setup_step(conn, shop_id) is not None or needs_setup(conn, shop_id):
        return text
    pend = get_pending(conn, shop_id)
    if pend is not None and pend["action_type"] == "reminder_offer":
        return text
    row = conn.execute("SELECT value FROM flags WHERE shop_id = ? AND key = 'last-options'",
                       (shop_id,)).fetchone()
    if row is None:
        return text
    try:
        saved = json.loads(row["value"])
        created = datetime.strptime(
            saved["ts"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except (ValueError, KeyError, TypeError):
        return text
    if datetime.now(timezone.utc) - created > _OPTIONS_TTL:
        return text
    options = saved.get("options") or []
    if n > len(options):
        return text
    return options[n - 1]


_STAFF_NAME: ContextVar[str | None] = ContextVar("sisi_staff", default=None)
"""Staff display name for this turn (None when the sender is the owner)."""


def _enter(conn: sqlite3.Connection, sender: str) -> tuple[str, str | None]:
    shop_id, staff = resolve_sender(conn, sender)
    _STAFF_NAME.set(staff)
    return shop_id, staff


LAST_PHOTO: dict[str, tuple[bytes, datetime]] = {}
"""Last inbound photo bytes per shop, so a 'logo' reply can claim it."""


def _fresh_photo(shop_id: str) -> bytes | None:
    hit = LAST_PHOTO.get(shop_id)
    if hit is None:
        return None
    image, at = hit
    if datetime.now(timezone.utc) - at > timedelta(minutes=10):
        LAST_PHOTO.pop(shop_id, None)
        return None
    return image


YES_WORDS = {"yes", "y", "yebo", "yeah", "ja", "ewe", "ok", "okay", "sharp", "shap", "sure"}
NO_WORDS = {"no", "n", "nope", "cha", "hayi", "nee", "aowa"}
UNDO_WORDS = {"undo"}
RESET_WORDS = {"demo reset", "reset demo"}
NEW_LOGO_WORDS = {"redo logo", "fix logo", "new logo", "change logo",
                  "lungisa ilogo", "phinda wenze ilogo", "ilogo entsha",
                  "shintsha ilogo", "maak logo reg",
                  "doen logo oor", "nuwe logo", "verander logo",
                  "yenzi ilogo kwakhona", "tshintsha ilogo"}
DELETE_WORDS = {"delete my data", "erase my data", "forget me"}
HELP_WORDS = {"demo help", "help", "what can you do", "commands",
              "hulp", "usizo", "uncedo"}

DEMO_HELP = STRINGS["en"]["help_menu"]
"""English help menu (tests import this); live replies use t("help_menu")."""


_FILLER = ("thank you", "thanks", "please", "sisi", "ta")


def _set_message_lang(conn: sqlite3.Connection, shop_id: str,
                        text: str | None) -> str:
    """Reply language: detected from this message, else the owner's saved one.

    Returns the language code. Scheduled sends use the owner's language.
    """
    code = detect(text, default=owner_lang(conn, shop_id))
    CURRENT_LANG.set(code)
    return code


def _normalize(text: str) -> str:
    word = " ".join(text.strip().lower().split())
    word = word.strip(" .!?,")
    changed = True
    while changed:
        changed = False
        for fix in _FILLER:
            if word.endswith(" " + fix):
                word = word[: -len(fix) - 1].strip(" .!?,")
                changed = True
            elif word.startswith(fix + " "):
                word = word[len(fix) + 1:].strip(" .!?,")
                changed = True
    return word


_FLOAT_WORDS = {"cash": "cash", "till": "cash", "petty": "petty",
                "bank": "bank"}
ORDER_WORDS = {"order stock", "what must i buy"}


def _parse_order_add(word: str):
    """'add coke 2l 2 packs' -> ('coke 2l', 2.0, 'packs'). None when unparseable."""
    toks = word.split()[1:]
    idx = [i for i, t in enumerate(toks) if re.fullmatch(r"\d+(?:\.\d+)?", t)]
    if not idx:
        return None
    qpos = idx[-1]
    try:
        qty = float(toks[qpos])
    except ValueError:
        return None
    if qpos == 0:
        rest = toks[1:]
        unit = " ".join(t for t in rest if _is_pack_unit(t)) or None
        product = " ".join(t for t in rest if not _is_pack_unit(t))
    else:
        product = " ".join(toks[:qpos])
        unit = " ".join(toks[qpos + 1:]) or None
    if not product:
        return None
    return product, qty, unit


def _float_num(tok: str) -> int | None:
    m = re.fullmatch(r"r?(\d+(?:\.\d{1,2})?)", tok.strip(" .!?"))
    if not m:
        return None
    cents = int(round(float(m.group(1)) * 100))
    return cents if cents > 0 else None


def _parse_float(text: str) -> dict:
    """'float R1200 petty R300' -> {'cash': 120000, 'petty': 30000}."""
    out: dict[str, int] = {}
    for seg in re.split(r"[,;]|\band\b", (text or "").lower()):
        toks = [t.strip(" .!?") for t in seg.split()]
        pending_label: str | None = None
        unbound: list[int] = []
        for i, tok in enumerate(toks):
            num = _float_num(tok)
            if num is not None:
                if pending_label is not None:
                    out.setdefault(pending_label, num)
                    pending_label = None
                else:
                    unbound.append(num)
                continue
            if tok in _FLOAT_WORDS:
                nxt_is_num = (i + 1 < len(toks)
                              and _float_num(toks[i + 1]) is not None)
                if unbound and not nxt_is_num:
                    out.setdefault(_FLOAT_WORDS[tok], unbound.pop())
                else:
                    pending_label = _FLOAT_WORDS[tok]
        for num in unbound:
            out.setdefault("cash", num)
    return out


_STAFF_BLOCKED_PREFIX = ("month-end", "month end", "monthend", "report ",
                               "statement", "set ", "add staff", "delete ",
                               "reset ", "nudge", "undo", "speak", "khuluma",
                               "praat", "thetha")
_STAFF_BLOCKED_EXACT = {"undo", "demo reset", "reset demo", "delete my data",
                        "erase my data", "forget me",
                        "statements for everyone who owes",
                        "statements for everyone", "all statements",
                        "shop profile", "show shop profile",
                        "my shop profile", "show my shop profile",
                        "logo", "use original logo", "remove logo background",
                        "setup", "reset shop", "yes erase everything",
                        "debug last error"}


def slow_status(text: str) -> str | None:
    """Busy line for slow text work (reports, statements). None when fast."""
    word = _normalize(text or "")
    if word in ("month-end report", "month end report", "monthend report",
                "month-end", "month end", "monthend"):
        return _t("ag_busy_report")
    if word.startswith("report ") and parse_month_name(word[7:]):
        return _t("ag_busy_report")
    if word.startswith(("statement", "statements", "staat", "strokie",
                          "isitatimende", "sitatimende", "istatimenti")) \
            or word in ("statements for everyone who owes",
                        "statements for everyone", "all statements"):
        return _t("ag_busy_stmt")
    if "this month" not in word and (
            word.startswith("receipt") or word.startswith("slip")
            or "make a picture" in word or "something to send" in word
            or "send it to" in word or "send this to" in word):
        return _t("ag_busy_stmt")
    return None


_STATEMENT_TRIGGERS = ("statement", "isitatimende", "sitatimende",
                       "itatimende", "receipt",
                       "irisithi", "risithi", "iresithi", "slip",
                       "staat", "strokie", "afskrif", "istatimenti",
                       "isiliphu",
                       "something to send", "to send to", "send it to",
                       "send this to", "make a picture", "picture for")

_DEBTOR_WORDS = ("debtor", "creditor", "abakwelet", "debiteure", "abatyali",
                 "skuldenaar")
_ALL_WORDS = ("all", "everyone", "bonke", "alle", "almal")
_OWE_WORDS = ("owe", "owes", "owing", "owed", "abakwelet", "abatyala",
              "tyala", "skuld", "skoloto", "etyala")


def _customer_in_text(word: str, names: list[str]) -> str | None:
    for name in sorted(names, key=len, reverse=True):
        if not name.strip():
            continue
        # optional isiZulu possessive prefix: sikaSipho, kaSipho, owaSipho
        if re.search(rf"\b(?:sika|ka|owa|wa)?{re.escape(name.strip().lower())}\b",
                     word):
            return name
    return None


def _has_statement_word(word: str) -> bool:
    if any(t in word for t in _STATEMENT_TRIGGERS):
        return True
    return re.search(r"\bstate\b", word) is not None  # Afrikaans plural


def _customers_in_text(word: str, names: list[str]) -> list[str]:
    """Every roster name mentioned, in the order mentioned (Nguni prefixes ok)."""
    found: list[tuple[int, str]] = []
    for name in names:
        clean = name.strip()
        if not clean:
            continue
        m = re.search(r"\b(?:sika|zika|ka|owa|wa|no|na|u|o)?"
                      + re.escape(clean.lower()) + r"\b", word)
        if m:
            found.append((m.start(), name))
    found.sort()
    return [name for _, name in found]


_CUST_STMT_WORDS = ("statement", "isitatimende", "sitatimende",
                    "itatimende", "istatimenti", "staat", "strokie", "afskrif")


def try_multi_statement_request(conn: sqlite3.Connection, shop_id: str,
                                text: str) -> str | None:
    """Top-N / all-debtors / named-list statement requests. None otherwise."""
    word = _normalize(text or "")
    if "this month" in word and "statement" not in word:
        return None  # 'show my receipts this month' is the receipts list
    if re.search(r"\b(paid|payment|khokha|settled?|ibhadalile|betaal|hlawul\w*)\b",
                 word):
        return None  # payment recordings belong to the credit flow
    has_stmt = _has_statement_word(word)
    has_debtor = any(d in word for d in _DEBTOR_WORDS)
    has_all = any(re.search(rf"\b{a}\b", word) for a in _ALL_WORDS)
    if not has_stmt and not (has_all and has_debtor):
        return None
    names = [r["name"] for r in conn.execute(
        "SELECT name FROM customers WHERE shop_id = ?", (shop_id,)).fetchall()]
    hits = _customers_in_text(word, names)
    if has_stmt and len(hits) >= 2:
        return statements_named(conn, shop_id, hits)
    num = re.search(r"\d+", word)
    if has_stmt and num and has_debtor:
        return statements_top_n(conn, shop_id, int(num.group()))
    receipt_only = (has_stmt and not has_debtor
                    and not any(o in word for o in _OWE_WORDS)
                    and not any(c in word for c in _CUST_STMT_WORDS)
                    and re.search(r"\bstate\b", word) is None)
    if has_all and (has_debtor or (has_stmt and not hits)) \
            and not receipt_only:
        return statements_for_all(conn, shop_id)
    return None


def try_statement_request(conn: sqlite3.Connection, shop_id: str,
                          text: str) -> str | None:
    """Statement/receipt/slip requests naming a customer. None otherwise."""
    word = _normalize(text or "")
    if word.startswith("statement for "):
        return customer_statement(conn, shop_id, word[len("statement for "):])
    if "this month" in word and "statement" not in word:
        return None  # 'show my receipts this month' is the receipts list
    if re.search(r"\b(paid|payment|khokha|settled?|ibhadalile|betaal|hlawul\w*)\b",
                   word):
        return None  # payment recordings belong to the credit flow
    if not any(t in word for t in _STATEMENT_TRIGGERS):
        return None
    names = [r["name"] for r in conn.execute(
        "SELECT name FROM customers WHERE shop_id = ?", (shop_id,)).fetchall()]
    hit = _customer_in_text(word, names)
    if hit is None:
        return None
    return customer_statement(conn, shop_id, hit)


_QUESTION_STARTS = ("how ", "what ", "when ", "who ", "which ", "why ",
                    "is ", "are ", "do ", "does ", "can ", "ngaki", "malini",
                    "ngubani", "nini", "yini", "kanjani",
                    "hoeveel", "wat ", "wanneer", "wie ", "hoekom", "kan ",
                    "yintoni", "kutheni", "yimalini", "ndinga")


def try_reminder_exact(conn: sqlite3.Connection, shop_id: str,
                       text: str) -> str | None:
    """Exact reminder management (list/cancel/move/snooze/retry)."""
    from app.reminders import (cancel_numbered, list_reminders, move_numbered,
                               parse_snooze_minutes, retry_stuck, snooze_last)
    word = _normalize(text or "")
    if word in ("retry reminders", "retry reminder",
                "probeer herinneringe weer", "zama izikhumbuzo futhi",
                "phinda uzame izikhumbuzo"):
        return retry_stuck(conn, shop_id)
    if word in ("my reminders", "reminders", "show reminders",
                "list reminders", "my herinneringe", "herinneringe",
                "wys herinneringe", "izikhumbuzo zam", "bonisa izikhumbuzo"):
        return list_reminders(conn, shop_id)
    m = re.match(r"cancel reminder (\d+)$", word)
    if m:
        return cancel_numbered(conn, shop_id, int(m.group(1)))
    m = re.match(r"move reminder (\d+) to (.+)$", word)
    if m:
        return move_numbered(conn, shop_id, int(m.group(1)),
                             m.group(2).strip())
    mins = parse_snooze_minutes(word)
    if mins is not None:
        return snooze_last(conn, shop_id, mins)
    return None


def try_reminder_offer(conn: sqlite3.Connection, shop_id: str,
                       text: str) -> str | None:
    """Offer a reminder for a mentioned future date. None otherwise."""
    from app.reminders import offer_scan
    found = offer_scan(text)
    if found is None:
        return None
    return propose_reminder_offer(conn, shop_id, found["due"].isoformat(),
                                  found["summary"])


def try_reminder_command(conn: sqlite3.Connection, shop_id: str, text: str,
                         sent_at=None) -> str | None:
    """Reminder commands and direct 'remind me' requests. None otherwise."""
    from app.reminders import (confirm_text, detect_context, parse_request,
                               save_reminder)
    exact = try_reminder_exact(conn, shop_id, text)
    if exact is not None:
        return exact
    word = _normalize(text or "")
    if "?" in word:
        return None
    parsed = parse_request(text, sent_at)
    if parsed is None:
        return None
    if parsed["summary"].lower().startswith(_QUESTION_STARTS):
        return None  # 'remind me how much…' is a question for the books
    from app.reminders import due_passed, reminder_spec_ok, resolve_topic
    if not reminder_spec_ok(text, parsed):
        return None  # 'remind them' is composite work, not a schedule
    if due_passed(parsed["due"], sent_at):
        return _t("rem_past", when=parsed["due"].strftime("%H:%M"))
    topic = resolve_topic(parsed["summary"], text)
    if topic is None:
        sent_iso = sent_at.isoformat() if hasattr(sent_at, "isoformat") \
            else str(sent_at or "")
        set_await_reminder(conn, shop_id, parsed["due"].isoformat(), sent_iso)
        return _t("rem_ask_what", when=parsed["due"].strftime("%H:%M"))
    parsed = dict(parsed, summary=topic)
    context = detect_context(conn, shop_id, text)
    try:
        save_reminder(conn, shop_id, parsed["due"], parsed["summary"],
                      context, parsed["recurrence"])
    except Exception as e:
        log.warning("reminder save failed", exc_info=True)
        return _t("rem_save_failed", err=type(e).__name__)
    return confirm_text(parsed["due"], parsed["summary"],
                        parsed["recurrence"])


def try_offer_time(conn: sqlite3.Connection, shop_id: str,
                   text: str) -> str | None:
    """A bare time answering a reminder offer ('6am' after the proposal)."""
    from app.reminders import parse_bare_time
    pend = get_pending(conn, shop_id)
    if pend is None or pend["action_type"] != "reminder_offer":
        return None
    hm = parse_bare_time(text)
    if hm is None:
        return None
    payload = json.loads(pend["payload_json"])
    due = datetime.fromisoformat(payload["due_at"])
    due = due.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    payload["due_at"] = due.isoformat()
    conn.execute("UPDATE pending_actions SET payload_json = ? WHERE id = ?",
                 (json.dumps(payload), pend["id"]))
    conn.commit()
    return confirm_pending(conn, shop_id)


_ACK_WORDS = frozenset({
    "thanks", "thank you", "thx", "ok", "okay", "sharp", "cool", "great",
    "ngiyabonga", "dankie", "enkosi", "yebo",
})


def try_reminder_detail(conn: sqlite3.Connection, shop_id: str,
                        text: str) -> str | None:
    """Complete a reminder awaiting its topic ('the milk delivery').

    Only answers the open 'what should I remind you about?' question:
    commands, questions, and fresh reminder requests fall through to
    normal routing and the pending reminder keeps waiting (10 min TTL).
    """
    from app.reminders import (_strip_request_bits, confirm_text,
                               detect_context, due_passed, now_sast,
                               parse_relative, parse_request,
                               reminder_spec_ok, save_reminder,
                               summary_is_bare, REMIND_TRIGGERS)
    waiting = get_await_reminder(conn, shop_id)
    if waiting is None:
        return None
    if _await_stale(waiting["created_at"]):
        clear_await_reminder(conn, shop_id)
        return None
    word = _normalize(text or "")
    if word in NO_WORDS:
        clear_await_reminder(conn, shop_id)
        return _t("rem_detail_dropped")
    if word in YES_WORDS or word in UNDO_WORDS or word in CANCEL_WORDS \
            or word in MENU_WORDS or word in HELP_WORDS \
            or word in RESET_WORDS or word in DELETE_WORDS \
            or word == "undo book" or word in _ACK_WORDS \
            or "?" in word:
        if word in _ACK_WORDS:
            clear_await_reminder(conn, shop_id)
        return None  # control words, acks, and questions pass through
    low = f" {word} "
    if any(t in low for t in REMIND_TRIGGERS):
        fresh = parse_request(text, None)
        if fresh is not None and reminder_spec_ok(text, fresh):
            clear_await_reminder(conn, shop_id)
            return None  # a fresh reminder command takes over
    if _intent_hits(word):
        return None  # anything actionable routes normally; keep waiting
    detail = (text or "").strip()
    if len(detail) > 200:
        return None
    now = now_sast(None)
    try:
        due = datetime.fromisoformat(waiting["due_iso"])
    except ValueError:
        clear_await_reminder(conn, shop_id)
        return None
    if due.tzinfo is None:
        due = due.replace(tzinfo=now.tzinfo)
    rel = parse_relative(detail, now)
    if rel is not None:
        due = rel
    cleaned = _strip_request_bits(detail).strip() or detail.strip()
    if summary_is_bare(cleaned):
        if rel is not None:
            set_await_reminder(conn, shop_id, due.isoformat(),
                               waiting["sent_iso"] or "")
        return _t("rem_ask_what", when=due.strftime("%H:%M"))
    sent = None
    if waiting["sent_iso"]:
        try:
            sent = datetime.fromisoformat(waiting["sent_iso"])
        except ValueError:
            sent = None
    if sent is not None:
        passed = due_passed(due, sent, wall=now)
    else:
        passed = due <= now
    if passed:
        clear_await_reminder(conn, shop_id)
        return _t("rem_past", when=due.strftime("%H:%M"))
    try:
        context = detect_context(conn, shop_id, cleaned)
        save_reminder(conn, shop_id, due, cleaned, context, "none")
    except Exception as e:
        log.warning("reminder detail save failed", exc_info=True)
        return _t("rem_save_failed", err=type(e).__name__)
    clear_await_reminder(conn, shop_id)
    return confirm_text(due, cleaned, "none")


_INTENT_FAMILIES: dict[str, tuple[str, ...]] = {
    "statement": ("statement", "receipt", "slip", "invoice", "risiti",
                  "isitatimende", "sitatimende", "itatimende",
                  "istatimenti", "staat", "strokie", "afskrif",
                  "irisithi", "iresithi", "isiliphu", "something to send",
                  "to send to", "send it to", "send this to",
                  "make a picture", "picture for"),
    "reminder": ("remind", "reminder", "khumbuz", "herinner", "snooze"),
    "debtor": ("owe", "owes", "owing", "debt", "balance", "kwelet", "tyala",
               "skuld"),
    "stock": ("stock", "shelf", "shelfu", "isitokwe", "voorraad", "low",
              "expire", "expiry", "running out"),
    "order": ("order", "restock", "buy", "thenga", "koop", "wholesaler",
              "supplier"),
    "expense": ("expense", "paid", "pay", "khokha", "betaal", "hlawula",
                "electricity", "rent", "wage", "transport"),
    "cash": ("cash", "float", "imali", "kontant", "counted", "cashup",
             "till"),
    "sale": ("sold", "sell", "thengisa", "verkoop"),
    "credit": ("credit", "book", "ibhuku", "skoloto", "took"),
    "report": ("report", "month", "profit", "inzuzo", "ingxelo"),
    "compliance": ("permit", "register", "fund", "compliance", "scam"),
}

def _intent_hits(word: str) -> set[str]:
    """Intent families named in the message (4 languages + slang).

    Short signals match whole tokens; longer stems also match inside
    Bantu-concord words (okweletayo, ngikhumbuze, onetyala).
    """
    tokens = re.findall(r"[a-z0-9]+", word or "")
    hits: set[str] = set()
    for fam, signals in _INTENT_FAMILIES.items():
        for sig in signals:
            if " " in sig:
                if sig in (word or ""):
                    hits.add(fam)
                    break
            elif len(sig) >= 5:
                if any(sig in tok for tok in tokens):
                    hits.add(fam)
                    break
            elif sig in tokens:
                hits.add(fam)
                break
    return hits



_AFTER_STATEMENT = ["All statements", "Set reminder", "Menu"]
_AFTER_REMINDERS = ["Set reminder", "Menu"]
_AFTER_ORDER = ["Send it", "Menu"]

CANCEL_WORDS = {"cancel", "cancel that", "never mind", "nevermind",
                "kanselleer", "kanselleer dit", "khansela", "ngiyayekisa",
                "rhoxisa"}

_WAIT_KEY = {"pending": "wait_pending", "plan": "wait_plan",
             "logo": "wait_logo"}


def _parse_ts(value: str) -> datetime:
    """Parse a proposal timestamp (ISO or legacy pending format)."""
    v = (value or "").strip()
    if not v:
        return datetime.min.replace(tzinfo=timezone.utc)
    m = re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:(\d{2})(.*)$", v)
    if m and m.group(2)[:1].isdigit():
        try:
            return datetime.strptime(v, "%Y-%m-%dT%H:%M:%fZ").replace(
                tzinfo=timezone.utc)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _newest_proposal(conn: sqlite3.Connection, shop_id: str):
    """Newest of (pending action, task plan, staged logo) for Yes/No.

    Returns (kind, [older kinds still waiting]), or None when nothing
    is waiting. Yes/No always act on the newest proposal.
    """
    cands: list[tuple[datetime, str]] = []
    pend = get_pending(conn, shop_id)
    if pend is not None:
        cands.append((_parse_ts(pend["created_at"]), "pending"))
    plan = get_plan(conn, shop_id)
    if plan is not None:
        cands.append((_parse_ts(plan["created_at"]), "plan"))
    staged = get_staged_logo(conn, shop_id)
    if staged is not None:
        cands.append((_parse_ts(staged.get("at") or ""), "logo"))
    if not cands:
        return None
    cands.sort()
    return cands[-1][1], [c[1] for c in cands[:-1]]


def _waiting_note(waiting: list[str]) -> str:
    """One line per older proposal still waiting after a Yes."""
    if not waiting:
        return ""
    return "\n\n" + "\n".join(
        _t("still_waiting", what=_t(_WAIT_KEY[w])) for w in waiting)


def _intake_delegatable(conn: sqlite3.Connection, shop_id: str) -> bool:
    """A guided session owns Yes/No/Cancel only when nothing else waits."""
    if get_intake_session(conn, shop_id) is None:
        return False
    return get_pending(conn, shop_id) is None \
        and get_plan(conn, shop_id) is None \
        and get_staged_logo(conn, shop_id) is None \
        and get_await_price(conn, shop_id) is None \
        and get_await_reminder(conn, shop_id) is None


def confirm_newest(conn: sqlite3.Connection, shop_id: str) -> str:
    """Yes: confirm the newest proposal (pending, plan, or staged logo)."""
    pick = _newest_proposal(conn, shop_id)
    if pick is None:
        followup = plan_followup_message(conn, shop_id)
        if followup is not None:
            return followup
        waiting = get_await_price(conn, shop_id)
        if waiting is not None:
            return _t("ag_price_needed", product=waiting["product"])
        seen = confirm_logo(conn, shop_id)
        if seen is not None:
            return seen
        return confirm_pending(conn, shop_id)
    kind, waiting = pick
    if kind == "plan":
        return confirm_plan(conn, shop_id) + _waiting_note(waiting)
    if kind == "logo":
        return apply_staged_logo(conn, shop_id) + _waiting_note(waiting)
    return confirm_pending(conn, shop_id) + _waiting_note(waiting)


def cancel_newest(conn: sqlite3.Connection, shop_id: str) -> str:
    """No: cancel the newest proposal (pending, plan, or staged logo)."""
    pick = _newest_proposal(conn, shop_id)
    if pick is None:
        if get_await_reminder(conn, shop_id):
            clear_await_reminder(conn, shop_id)
            return _t("rem_detail_dropped")
        return cancel_pending(conn, shop_id)
    kind, _waiting = pick
    if kind == "plan":
        return cancel_plan(conn, shop_id)
    if kind == "logo":
        return discard_staged_logo(conn, shop_id)
    return cancel_pending(conn, shop_id)


def try_order_plan(conn: sqlite3.Connection, shop_id: str, text: str,
                   sent_at=None, message_key: str = "",
                   vision_doc: dict | None = None,
                   from_photo: bool = False,
                   photo_digest: str = "") -> str | None:
    """Order-bearing texts become one combined plan preview. None otherwise."""
    plan, from_ctx = resume_plan_from_clarify(conn, shop_id, text or "")
    if plan is None:
        plan, _question = build_plan(conn, shop_id, text or "", vision_doc,
                                     photo_digest, message_key, from_photo,
                                     now=sent_at)
    if plan is None:
        return None
    existing = get_plan_by_key(conn, shop_id, plan["message_key"])
    if existing is not None:
        if existing["status"] == "confirmed":
            return _t("ord_already")
        return render_preview(conn, shop_id, existing)
    saved = save_plan(conn, shop_id, plan)
    if from_ctx:
        clear_clarify_context(conn, shop_id)
    return render_preview(conn, shop_id, saved)



def _store_evidence(digest: str, image: bytes) -> None:
    """Keep photo bytes in the shared Dict so plans survive restarts."""
    try:
        from app import reports as _reports
        _reports.evidence_put(digest, image or b"")
    except Exception:
        log.warning("evidence store failed", exc_info=True)



def try_affordability(conn: sqlite3.Connection, shop_id: str,
                      text: str) -> str | None:
    """'Can we afford R200?' from live balances. Amount first, no guessing."""
    from app.db import cents_to_rand
    from app.tools import account_balance
    word = _normalize(text or "")
    amounts = _price_amounts(text or "")
    triggered = ("afford" in word or "bekostig" in word
                 or "kwanele" in word
                 or ("cover" in word and ("pay" in word or "cost" in word
                                          or bool(amounts)))
                 or ("enough" in word and "pay" in word)
                 or ("available" in word and "pay" in word))
    if not triggered:
        return None
    cash = account_balance(conn, shop_id, "cash")
    petty = account_balance(conn, shop_id, "petty")
    if not amounts:
        return _t("afford_ask", cash=cents_to_rand(cash),
                  petty=cents_to_rand(petty))
    need = amounts[0]
    have = cash + petty
    if have >= need:
        return _t("afford_yes", have=cents_to_rand(have),
                  cash=cents_to_rand(cash), petty=cents_to_rand(petty),
                  need=cents_to_rand(need))
    return _t("afford_no", have=cents_to_rand(have),
              cash=cents_to_rand(cash), petty=cents_to_rand(petty),
              need=cents_to_rand(need))


_SALE_ACTION_WORDS = ("bought", "took", "taken", "add", "added", "put",
                      "puts", "putting", "gave", "sold", "sell", "sells")
"""Goods-moving verbs. Imperative request verbs (give, send, get, show,
make) are NOT here: 'give me a receipt' is the statement itself, not a
sibling sale."""
_SALE_ACTION_STEMS = ("thatha", "thathe")
"""Nguni take/took: substring match so concords (uthathe) still hit."""
_QTY_WORDS = ("one", "two", "three", "four", "five", "six", "seven",
              "eight", "nine", "ten", "eleven", "twelve", "dozen", "half",
              "couple", "few", "several")
_DEBTOR_COUNT_NOUNS = ("creditors?", "debtors?", "debiteure", "customers?",
                       "clients?", "people", "persons?", "statements?",
                       "receipts?", "slips?", "pages?", "copies?",
                       "abakwelet\\w*", "abatyali\\w*")


def _statement_has_sibling(low: str) -> bool:
    """A statement trigger plus a goods-moving verb or a quantity.

    'Sipho took milk and bread, send his statement' is sale +
    statement: the fast path must yield so the planner splits them.
    Bare balance mentions ('he owes R200, send the receipt') stay
    single-intent, as do debtor counts ('statements for 4 creditors').
    """
    scrubbed = re.sub(r"\badds? up\b|\badded up\b", " ", low)
    if any(re.search(rf"\b{v}\b", scrubbed) for v in _SALE_ACTION_WORDS):
        return True
    if any(stem in scrubbed for stem in _SALE_ACTION_STEMS):
        return True
    scrubbed = re.sub(
        r"\b(\d+|" + "|".join(_QTY_WORDS) + r")\s+("
        + "|".join(_DEBTOR_COUNT_NOUNS) + r")\b", " ", scrubbed)
    if re.search(r"\b\d+\b|\b(" + "|".join(_QTY_WORDS) + r")\b", scrubbed):
        return True
    return False


def _looks_multi(text: str) -> bool:
    """Two+ intent families (or clauses): the planner splits the work.

    Single-intent fast paths (affordability, statements) yield here so
    they never swallow a sibling task ('statement for Sipho and remind
    me Friday'). The reminder clause is set aside before counting, so
    its topic ('count the till') is not mistaken for a cash question.
    """
    from app.reminders import REMIND_TRIGGERS, reminder_clause
    bits = [b.strip() for b in re.split(r"[.?!;]", text or "") if b.strip()]
    if len(bits) > 1:
        return True
    if not bits:
        return False
    low = bits[0].lower()
    fams = set()
    if any(t in f" {low} " for t in REMIND_TRIGGERS):
        fams.add("reminder")
        clause = reminder_clause(bits[0]).lower()
        if clause:
            low = low.replace(clause, " ", 1)
    for fam, sigs in _INTENT_FAMILIES.items():
        if fam != "reminder" and any(s in low for s in sigs):
            fams.add(fam)
    if "statement" in fams:
        if _statement_has_sibling(low):
            fams.add("sale")  # goods move too: the planner splits them
        else:
            # debtors/credit name the statement's subject, not a sibling
            fams -= {"debtor", "credit"}
    if ":" in bits[0]:
        fams.add("order")  # 'Sipho: 2 bread' has no keyword
    return len(fams) >= 2


def try_deterministic(conn: sqlite3.Connection, shop_id: str, text: str,
                      sent_at=None):
    """Exact commands and follow-ups in code; free text via the planner.

    Returns (message, buttons_or_None), always. sent_at is the provider
    timestamp (relative times resolve from it).
    """
    ensure_shop(conn, shop_id)
    text = _resolve_numbered(conn, shop_id, text)
    word = _normalize(text or "")
    if _STAFF_NAME.get() and (word in _STAFF_BLOCKED_EXACT
                              or word.startswith(_STAFF_BLOCKED_PREFIX)):
        from app.tools import _staff_blocked
        return _staff_blocked(), None
    if read_only() and (word in YES_WORDS or word in NO_WORDS
                        or word in UNDO_WORDS or word == "undo book"
                        or word in CANCEL_WORDS or word in RESET_WORDS
                        or word in DELETE_WORDS):
        # Testing mode: confirmations are the dangerous ones (a live
        # pending action from another channel must never confirm here).
        return _t("whapi_readonly"), None
    linked = try_link_choice_reply(conn, shop_id, text)
    if linked is not None:
        return linked
    resumed = try_deferred_link_resume(conn, shop_id, text)
    if resumed is not None:
        return resumed
    m = re.match(r"(speak|khuluma|praat|thetha)\s+(.+)$", word)
    if m and normalize_lang(m.group(2).strip()) is not None:
        return set_owner_language(conn, shop_id, m.group(2).strip()), None
    m = re.match(r"add staff ([\d\s+()\-]{9,}) (.+)$", (text or "").strip(),
                   re.IGNORECASE)
    if m and _STAFF_NAME.get() is None:
        return add_staff(conn, shop_id, m.group(1), m.group(2).strip()), None
    if not _looks_multi(text):
        afford = try_affordability(conn, shop_id, text)
        if afford is not None:
            return afford, None
    if word in MENU_WORDS:
        return show_menu(conn, shop_id), None
    if word in ("who recorded today", "who recorded", "staff today"):
        return who_recorded_today(conn, shop_id), None
    if word == "float" or word.startswith("float "):
        return set_float(conn, shop_id, _parse_float(word)), None
    managed = try_reminder_exact(conn, shop_id, text)
    if managed is not None:
        return managed, _AFTER_REMINDERS
    if not _looks_multi(text):
        multi_stmts = try_multi_statement_request(conn, shop_id, text)
        if multi_stmts is not None:
            return multi_stmts, _AFTER_STATEMENT
        statement = try_statement_request(conn, shop_id, text)
        if statement is not None:
            return statement, _AFTER_STATEMENT
    if word in ORDER_WORDS:
        return build_order(conn, shop_id), _AFTER_ORDER
    if word == "send it" or word.startswith("send it "):
        return send_order(conn, shop_id, word[7:].strip() or None), None
    if word == "use original logo":
        return logo_as_is(conn, shop_id), None
    if word == "remove logo background":
        return want_new_logo(conn, shop_id), None
    if word in NEW_LOGO_WORDS:
        return want_new_logo(conn, shop_id), None
    if word.startswith("remove "):
        if get_order_draft(conn, shop_id) is None:
            return _t("order_no_draft"), None
        return edit_order_remove(conn, shop_id, word[7:].strip()), None
    if word.startswith("add ") and get_order_draft(conn, shop_id) is not None \
            and not _price_amounts(word):
        parsed = _parse_order_add(word)
        if parsed is None:
            return _t("ag_order_add_hint"), None
        return edit_order_add(conn, shop_id, *parsed), None
    if word in CANCEL_WORDS:
        if get_plan(conn, shop_id) is not None:
            return cancel_plan(conn, shop_id), None
        if get_staged_logo(conn, shop_id) is not None:
            return discard_staged_logo(conn, shop_id), None
        if clear_logo_pending(conn, shop_id):
            return _t("logo_kept"), None
        if _intake_delegatable(conn, shop_id):
            reply = answer_intake_session(conn, shop_id, text)
            if reply is not None:
                return reply, None
        if get_pending(conn, shop_id) is None:
            dropped = cancel_deferred_link(conn, shop_id)
            if dropped is not None:
                return dropped, None
        clear_await_price(conn, shop_id)
        return cancel_pending(conn, shop_id), None
    if word in YES_WORDS:
        if _intake_delegatable(conn, shop_id):
            reply = answer_intake_session(conn, shop_id, text)
            if reply is not None:
                return reply, None
        return confirm_newest(conn, shop_id), None
    if word in NO_WORDS:
        if _newest_proposal(conn, shop_id) is None \
                and get_await_price(conn, shop_id) is not None:
            clear_await_price(conn, shop_id)
            return _t("ag_sale_dropped"), None
        if _intake_delegatable(conn, shop_id):
            reply = answer_intake_session(conn, shop_id, text)
            if reply is not None:
                return reply, None
        pend = get_pending(conn, shop_id)
        if pend is not None and pend["action_type"] == "stock_intake_item":
            return guided_skip_pending(conn, shop_id), None
        clear_await_price(conn, shop_id)
        return cancel_newest(conn, shop_id), None
    offered = try_offer_time(conn, shop_id, text)
    if offered is not None:
        return offered, None
    if word in UNDO_WORDS:
        return undo_last(conn, shop_id), None
    if word == "undo book":
        return undo_last_kind(conn, shop_id, "book", "No saved book page to undo."), None
    if word in RESET_WORDS:
        return demo_reset(conn, shop_id), None
    if word in DELETE_WORDS:
        return delete_my_data(conn, shop_id), None
    if word in HELP_WORDS:
        if word != "demo help" and not needs_setup(conn, shop_id):
            return show_menu(conn, shop_id), None
        return DEMO_HELP, None
    if word == "debug last error":
        from app.tools import last_error_report
        return last_error_report(conn, shop_id), None
    if word in ("month-end report", "month end report", "monthend report",
                "month-end", "month end", "monthend"):
        text, media = reports.build_month_end(
            conn, shop_id, None, os.environ.get("SISI_PUBLIC_URL", ""))
        queue_media(shop_id, media)
        return text, None
    if word.startswith("report "):
        m = parse_month_name(word[7:])
        if m:
            text, media = reports.build_month_end(
                conn, shop_id, m, os.environ.get("SISI_PUBLIC_URL", ""))
            queue_media(shop_id, media)
            return text, None
    if word.startswith("set shop name "):
        return set_shop_name(conn, shop_id, text.strip()[len("set shop name "):]), None
    if word.startswith("set address "):
        return set_shop_address(conn, shop_id, text.strip()[len("set address "):]), None
    if word.startswith("set phone "):
        return set_shop_phone(conn, shop_id, text.strip()[len("set phone "):]), None
    if word in ("nudge off", "nudge on") or word.startswith("nudge at "):
        setting = word[9:].strip() if word.startswith("nudge at ") else word[6:]
        return set_nudge(conn, shop_id, setting), None
    if word in ("daily plan off", "daily plan on") \
            or word.startswith("daily plan at "):
        from app.schedule import set_daily_plan
        setting = word[14:].strip() if word.startswith("daily plan at ") \
            else word[11:]
        return set_daily_plan(conn, shop_id, setting), None
    if word == "daily plan":
        from app.schedule import daily_plan, sast_now
        return daily_plan(conn, shop_id, sast_now().date().isoformat()), None
    m = re.match(r"(.+) expires? (.+)$", word)
    if m and "expiry" not in word and not word.startswith(
            ("what ", "when ", "which ", "list ", "show ", "check ")):
        return set_expiry(conn, shop_id, m.group(1).strip(), m.group(2).strip()), None
    m = re.match(r"set (.+) limit r?([\d.,]+)$", word)
    if m:
        try:
            cents = int(round(float(m.group(2).replace(",", ".")) * 100))
        except ValueError:
            cents = 0
        return set_customer_limit(conn, shop_id, m.group(1).strip(), cents), None
    m = re.match(r"set (.+) language (.+)$", word)
    if m:
        return set_customer_language(conn, shop_id, m.group(1).strip(),
                                     m.group(2).strip()), None
    if word in ("show my shop profile", "shop profile", "show shop profile",
                "my shop profile"):
        return show_shop_profile(conn, shop_id), None
    if word in ("statements for everyone who owes",
                "statements for everyone", "all statements"):
        return statements_for_all(conn, shop_id), None
    if word == "resend":
        return resend_recovery(conn, shop_id)
    if word == "logo":
        image = _fresh_photo(shop_id)
        if image is None:
            return _t("logo_howto"), None
        LAST_PHOTO.pop(shop_id, None)
        return stage_shop_logo(conn, shop_id, image), ["Yes", "No"]
    priced = try_price_reply(conn, shop_id, text)
    if priced is not None:
        return priced, None
    corrected = try_line_correction(conn, shop_id, text)
    if corrected is None:
        corrected = try_account_correction(conn, shop_id, text)
    if corrected is None:
        corrected = try_intake_reply(conn, shop_id, text)
    if corrected is not None:
        return corrected, None
    from app.orders import get_clarify_context
    if get_clarify_context(conn, shop_id) is not None:
        resumed = try_order_plan(conn, shop_id, text, sent_at)
        if resumed is not None:
            return resumed, ["Yes", "No"]
    detail = try_reminder_detail(conn, shop_id, text)
    if detail is not None:
        return detail, None
    offer = try_reminder_offer(conn, shop_id, text)
    if offer is not None:
        return offer, None
    message, buttons = _interpret(conn, shop_id, text, sent_at)
    return _with_intake_resume(conn, shop_id, message), buttons


_FALLBACK_SIGNALS = re.compile(
    r"\b(cash|petty|float|owe|owes|owing|owed|balance|statement|receipt|"
    r"slip|order|stock|sell|sold|afford|enough|available|pay|price|cost|"
    r"menu|help|spaza)\b|\?|:", re.IGNORECASE)


def _single_reminder(text: str) -> bool:
    """One clause, a reminder trigger, no other intent signals.

    Signals are scanned outside the reminder clause itself, so a topic
    like 'pay rent' is not mistaken for a payment task.
    """
    bits = [b.strip() for b in re.split(r"[.?!;]", text or "") if b.strip()]
    if len(bits) != 1:
        return False
    from app.reminders import REMIND_TRIGGERS, reminder_clause
    low = bits[0].lower()
    if not any(t in f" {low} " for t in REMIND_TRIGGERS):
        return False
    clause = reminder_clause(bits[0])
    rest = bits[0].lower().replace(clause.lower(), " ", 1) if clause else low
    return _FALLBACK_SIGNALS.search(rest) is None


def _interpret(conn: sqlite3.Connection, shop_id: str, text: str, sent_at):
    """Free text through the unified planner (model) + executors (code).

    When the planner is unreachable and the text is a single reminder
    request, the deterministic parser answers instead of a dead end.
    Anything multi-intent waits for the planner (never half-handled).
    """
    from app.interpreter import execute_plan, interpret
    from app.orders import get_clarify_context
    note = f"sender: staff {_STAFF_NAME.get()} (restricted)" \
        if _STAFF_NAME.get() is not None else "sender: owner"
    plan = interpret(conn, shop_id, text, sent_at=sent_at, sender_note=note)
    if not plan["tasks"] \
            and plan["question"] in {_t("llm_dropped"), _t("llm_say_again")} \
            and _single_reminder(text):
        fb = try_reminder_command(conn, shop_id, text, sent_at)
        if fb is not None:
            return fb, _AFTER_REMINDERS
    bundle: dict = {"text": text, "sent_at": sent_at}
    ctx = get_clarify_context(conn, shop_id)
    if ctx is not None:  # follow-up answers reuse the stored photo
        bundle["doc"] = ctx.get("vision") or {}
        bundle["digest"] = ctx.get("photo_digest") or ""
    return execute_plan(conn, shop_id, plan, bundle)


def route_to_llm(conn: sqlite3.Connection, shop_id: str, text: str):
    """Legacy tools-loop fallback (CLI/debug). The message path plans."""
    return llm_chat_full(conn, shop_id, text)


_PRICE_RES = (
    re.compile(r"r\s?(\d+(?:[.,]\d{1,2})?)", re.IGNORECASE),
    re.compile(r"\b(\d+(?:[.,]\d{1,2})?)\s?rand\b", re.IGNORECASE),
)
_AWAIT_TTL = timedelta(minutes=10)


def _price_amounts(text: str) -> list[int]:
    amounts = []
    for rx in _PRICE_RES:
        for m in rx.finditer(text or ""):
            amounts.append(int(round(float(m.group(1).replace(",", ".")) * 100)))
    return [a for a in amounts if a > 0]


def _await_stale(created_at: str) -> bool:
    try:
        created = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - created > _AWAIT_TTL


def try_price_reply(conn: sqlite3.Connection, shop_id: str, text: str) -> str | None:
    """Finish a paused sale/credit when the owner sends the price.

    Returns the resumed tool reply, or None when the LLM should handle it.
    """
    if get_pending(conn, shop_id) is not None:
        return None
    waiting = get_await_price(conn, shop_id)
    if waiting is None:
        return None
    if _await_stale(waiting["created_at"]):
        clear_await_price(conn, shop_id)
        return None
    amounts = _price_amounts(text)
    if not amounts:
        return None
    product = waiting["product"]
    words = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    awaited_words = {w for w in re.findall(r"[a-z0-9]+", product.lower()) if len(w) > 1}
    mentioned = product.lower() in (text or "").lower() or bool(awaited_words & words)
    if len(amounts) > 1:
        if not mentioned:
            return None
        return _t("ag_one_price", product=product)
    if not mentioned:
        others = conn.execute("SELECT name FROM products WHERE shop_id = ?",
                              (shop_id,)).fetchall()
        for other in others:
            if other["name"].lower() == product.lower():
                continue
            needle = other["name"].lower()
            if needle in (text or "").lower() or any(
                    w in needle for w in words if len(w) > 2):
                return None  # the price belongs to a different product
    spec = json.loads(waiting["spec_json"])
    prod = find_product(conn, shop_id, product)
    if _is_pack_unit(text):
        if prod is None or not prod["pack_size"]:
            return _t("ag_pack_price_ask", product=product,
                            echo=_pack_echo(text), low=product.lower())
        ensure_pack_price(conn, shop_id, product, amounts[0])
    else:
        ensure_product_price(conn, shop_id, product, amounts[0])
    clear_await_price(conn, shop_id)
    if spec["tool"] == "sale":
        return record_sale(conn, shop_id, product, spec["qty"],
                           buyer=spec.get("buyer"), unit=spec.get("unit"))
    if spec["tool"] == "sale_multi":
        # The price answer revives the whole batch, not just one line.
        return record_sale_multi(conn, shop_id, spec["items"],
                                 buyer=spec.get("buyer"))
    return add_credit(conn, shop_id, spec["customer"], spec["items"])


def handle_voice(conn: sqlite3.Connection, shop_id: str, audio: bytes,
               suffix: str = ".ogg", sent_at=None):
    """Transcribe a voice note and handle it like text.

    Returns (transcript_or_None, reply, buttons). The owner always sees what
    Sisi heard before anything is proposed or saved.
    """
    shop_id, _ = _enter(conn, shop_id)
    ensure_shop(conn, shop_id)
    CURRENT_LANG.set(owner_lang(conn, shop_id))
    extra = [r["name"] for r in conn.execute(
        "SELECT name FROM customers WHERE shop_id = ?", (shop_id,)).fetchall()]
    extra += [r["name"] for r in conn.execute(
        "SELECT name FROM products WHERE shop_id = ?", (shop_id,)).fetchall()]
    try:
        transcript = transcribe_audio(audio, suffix, shop_id, extra).strip()
    except VoiceError as e:
        return None, str(e), None
    if not transcript:
        return None, _t("ag_voice_empty"), None
    reply, buttons = handle_text(conn, shop_id, transcript, sent_at)
    shown = transcript if len(transcript) <= 100 else transcript[:97] + "…"
    return transcript, _t("ag_heard", shown=shown, reply=reply), buttons


_PHOTO_EMOJI = {"expiry": "📸", "book_page": "📒", "receipt": "🧾",
                "order": "🧾"}


def _dup_photo_reply(conn: sqlite3.Connection, shop_id: str, image: bytes,
                     caption: str, digest: str, seen) -> tuple:
    """Ask-first duplicate: stash the vision, offer 'restock'."""
    from app.tools import _save_dup_stash
    try:
        doc = read_shop_photo(image, shop_id)
    except VisionError:
        doc = None
    products = doc.get("products", []) if isinstance(doc, dict) else []
    _save_dup_stash(conn, shop_id, {
        "digest": digest, "caption": caption or "",
        "kind": seen["kind"] or "", "products": [
            p for p in products if isinstance(p, dict)],
        "saved_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"})
    day = (seen["created_at"] or "")[:10]
    reply = _t("g_dup_ask", day=day)
    save_turn(conn, shop_id, "user", "[photo]")
    save_turn(conn, shop_id, "assistant", reply)
    return doc, reply, None


def _stash_mid_session_photo(conn: sqlite3.Connection, shop_id: str,
                             caption: str, digest: str,
                             doc: dict | None) -> str | None:
    """Stash a new photo that arrives mid-intake; None when not stashed."""
    session = get_intake_session(conn, shop_id)
    if session is None or (session.get("status") or "active") != "active":
        return None
    if digest and digest == (session.get("photo_digest") or ""):
        return guided_reprompt(conn, shop_id)  # same photo: re-ask, no stash
    if caption and (_intent_hits(_normalize(caption)) - {"stock"}):
        return None  # explicit other business flows normally
    if isinstance(doc, dict) and doc.get("doc_type") in ("book_page",
                                                         "receipt",
                                                         "expiry"):
        return None  # unambiguous structured docs flow normally
    stash = session.get("stash") or {}
    products = doc.get("products", []) if isinstance(doc, dict) else []
    stash.update({"kind": "photo", "digest": digest or "",
                  "caption": caption or "",
                  "vision": [p for p in products if isinstance(p, dict)]})
    session["stash"] = stash
    save_intake_session(conn, shop_id, session)
    items = session.get("items_json") or []
    return _t("g_photo_choice", done=(session.get("current") or 0) + 1,
              total=len(items))


def handle_photo(conn: sqlite3.Connection, shop_id: str, image: bytes,
                 caption: str = "", sent_at=None):
    """Route a photo by THIS message's intent (caption + vision), then propose.

    An explicit caption ("logo", an order, a reminder) always beats a stale
    logo request or a blind vision guess. Returns (doc_or_None, reply,
    buttons). Nothing is saved until Yes.
    """
    shop_id, staff = _enter(conn, shop_id)
    ensure_shop(conn, shop_id)
    if read_only():
        # Testing mode: photos propose intakes/logos — block before the
        # vision call (which costs money) and before any writes.
        return None, _t("whapi_readonly"), None
    caption = (caption or "").strip()
    _set_message_lang(conn, shop_id, caption)
    LAST_PHOTO[shop_id] = (image or b"", datetime.now(timezone.utc))
    if staff is None and setup_step(conn, shop_id):
        from app.setup import handle_photo as setup_photo
        from app.setup import step_prompt as setup_prompt
        got = setup_photo(conn, shop_id, image or b"")
        if got is not None:
            save_turn(conn, shop_id, "user", "[photo]")
            save_turn(conn, shop_id, "assistant", got)
            return None, got, None
        step = setup_step(conn, shop_id) or 1
        reply = _t("ag_photo_setup",
                   prompt=setup_prompt(conn, shop_id, step))
        save_turn(conn, shop_id, "user", "[photo]")
        save_turn(conn, shop_id, "assistant", reply)
        return None, reply, None
    if caption.lower() == "logo" or _normalize(caption) in NEW_LOGO_WORDS:
        clear_clarify_context(conn, shop_id)
        reply = stage_shop_logo(conn, shop_id, image or b"")
        save_turn(conn, shop_id, "user", "[logo photo]")
        save_turn(conn, shop_id, "assistant", reply)
        buttons = ["Yes", "No"] \
            if get_staged_logo(conn, shop_id) is not None else None
        if buttons:
            _store_options(conn, shop_id, buttons)
        return None, reply, buttons
    digest = hashlib.sha256(image or b"").hexdigest()
    seen = photo_seen(conn, shop_id, digest)
    if seen is not None:
        return _dup_photo_reply(conn, shop_id, image or b"", caption,
                                digest, seen)
    _store_evidence(digest, image)
    before = get_pending(conn, shop_id)
    before_id = before["id"] if before else None
    if not caption and logo_pending(conn, shop_id):
        clear_clarify_context(conn, shop_id)
        reply = stage_shop_logo(conn, shop_id, image or b"")
        save_turn(conn, shop_id, "user", "[logo photo]")
        save_turn(conn, shop_id, "assistant", reply)
        buttons = ["Yes", "No"] \
            if get_staged_logo(conn, shop_id) is not None else None
        if buttons:
            _store_options(conn, shop_id, buttons)
        return None, reply, buttons
    try:
        doc = read_shop_photo(image, shop_id)
    except VisionError as e:
        doc, vision_error = None, str(e)
    stashed = _stash_mid_session_photo(conn, shop_id, caption, digest, doc)
    if stashed is not None:
        save_turn(conn, shop_id, "user", "[photo]")
        save_turn(conn, shop_id, "assistant", stashed)
        return doc, stashed, None
    save_turn(conn, shop_id, "user", "[photo]")
    from app.interpreter import execute_plan, interpret, media_brief
    brief = media_brief(doc, caption, digest)
    if doc is None:
        brief += f" Reader error: {vision_error}."
    if logo_pending(conn, shop_id):
        brief += " The owner earlier asked for a logo update."
    note = f"sender: staff {_STAFF_NAME.get()} (restricted)" \
        if _STAFF_NAME.get() is not None else "sender: owner"
    before_plan = get_plan(conn, shop_id)
    before_plan_id = before_plan["plan_id"] if before_plan else None
    before_staged = get_staged_logo(conn, shop_id)
    had_logo_pending = logo_pending(conn, shop_id)
    plan = interpret(conn, shop_id, caption or "(no caption)", brief=brief,
                     sent_at=sent_at, sender_note=note)
    reply, buttons = execute_plan(
        conn, shop_id, plan,
        {"text": caption, "sent_at": sent_at, "doc": doc or {},
         "digest": digest, "image": image or b""})
    after = get_pending(conn, shop_id)
    after_plan = get_plan(conn, shop_id)
    proposed = (after is not None and after["id"] != before_id) \
        or (after_plan is not None
            and after_plan["plan_id"] != before_plan_id)
    staged = get_staged_logo(conn, shop_id) is not None \
        and before_staged is None
    if proposed or staged:
        clear_clarify_context(conn, shop_id)
        clear_logo_pending(conn, shop_id)
        if had_logo_pending and not staged:
            reply = (_t("logo_req_dropped") + "\n\n") + reply
        if after is not None and after["action_type"] == "link_choice":
            buttons = ["link", "new"]
        else:
            buttons = ["Yes", "No"]
    elif not plan["tasks"] and plan.get("question"):
        save_clarify_context(conn, shop_id, digest, caption, doc)
    save_turn(conn, shop_id, "assistant", reply)
    if buttons:
        _store_options(conn, shop_id, buttons)
    return doc, reply, buttons


def handle_text(conn: sqlite3.Connection, shop_id: str, text: str,
                sent_at=None):
    shop_id, staff = _enter(conn, shop_id)
    ensure_shop(conn, shop_id)
    if staff is None and needs_setup(conn, shop_id):
        # Setup answers are data (names, numbers, yes), not language
        # signals; the step-6 language answer is authoritative.
        CURRENT_LANG.set(owner_lang(conn, shop_id))
    else:
        _set_message_lang(conn, shop_id, text)
    if staff is None:
        word0 = _normalize(text or "")
        if read_only() and (word0 == "setup" or word0 == "reset shop"
                            or word0 == "yes erase everything"
                            or word0 in RESET_WORDS
                            or needs_setup(conn, shop_id)):
            # Testing mode: setup and resets would (re)write the shop.
            return _t("whapi_readonly"), None
        if word0 == "setup":
            message = start_setup(conn, shop_id)
            save_turn(conn, shop_id, "user", text)
            save_turn(conn, shop_id, "assistant", message)
            return message, None
        if word0 == "reset shop":
            message = reset_shop(conn, shop_id)
            save_turn(conn, shop_id, "user", text)
            save_turn(conn, shop_id, "assistant", message)
            return message, None
        if word0 == "yes erase everything":
            message = confirm_reset_shop(conn, shop_id)
            save_turn(conn, shop_id, "user", text)
            save_turn(conn, shop_id, "assistant", message)
            return message, None
        if word0 in RESET_WORDS:
            message = demo_reset(conn, shop_id)
            save_turn(conn, shop_id, "user", text)
            save_turn(conn, shop_id, "assistant", message)
            return message, None
        if word0 in HELP_WORDS:
            if word0 != "demo help" and not needs_setup(conn, shop_id):
                message = show_menu(conn, shop_id)
                save_turn(conn, shop_id, "user", text)
                save_turn(conn, shop_id, "assistant", message)
                return message, None
            if not read_only():
                mark_paused(conn, shop_id)
            save_turn(conn, shop_id, "user", text)
            save_turn(conn, shop_id, "assistant", _t("help_menu"))
            return _t("help_menu"), None
        if needs_setup(conn, shop_id):
            if setup_step(conn, shop_id) is None:
                message = start_setup(conn, shop_id)
            else:
                message = setup_answer(conn, shop_id, shop_id, text)
            save_turn(conn, shop_id, "user", text)
            save_turn(conn, shop_id, "assistant", message)
            return message, None
    before = get_pending(conn, shop_id)
    before_id = before["id"] if before else None
    before_plan = get_plan(conn, shop_id)
    before_plan_id = before_plan["plan_id"] if before_plan else None
    text = _resolve_numbered(conn, shop_id, text)
    message, det_buttons = try_deterministic(conn, shop_id, text, sent_at)
    save_turn(conn, shop_id, "user", text)
    save_turn(conn, shop_id, "assistant", message)
    message = _maybe_first_offer(conn, shop_id, message)
    after = get_pending(conn, shop_id)
    after_plan = get_plan(conn, shop_id)
    if after is not None and after["id"] != before_id:
        if after["action_type"] == "link_choice":
            buttons = ["link", "new"]
        else:
            buttons = ["Yes", "No"]
    elif after_plan is not None and after_plan["plan_id"] != before_plan_id:
        buttons = ["Yes", "No"]
    else:
        buttons = det_buttons
    if buttons:
        _store_options(conn, shop_id, buttons)
    return message, buttons


def _maybe_first_offer(conn: sqlite3.Connection, shop_id: str, message: str) -> str:
    """On the 1st, once per month, offer last month's books when it has sales."""
    today = today_str()
    if not today.endswith("-01"):
        return message
    month = reports.prev_month(today[:7])
    key = f"offer-{month}"
    if read_only():
        return message  # testing mode: never mark the offer shown
    if conn.execute("SELECT value FROM flags WHERE shop_id = ? AND key = ?",
                    (shop_id, key)).fetchone():
        return message
    conn.execute("INSERT OR IGNORE INTO flags (shop_id, key, value) VALUES (?, ?, '1')",
                 (shop_id, key))
    conn.commit()
    sales = conn.execute(
        "SELECT COALESCE(SUM(total_cents), 0) s FROM sales"
        " WHERE shop_id = ? AND reversed = 0"
        " AND substr(datetime(created_at, '+2 hours'), 1, 7) = ?",
        (shop_id, month)).fetchone()["s"]
    if not sales:
        return message
    return message + _t("ag_first_offer",
                               month=reports.month_label(month))


def try_line_correction(conn: sqlite3.Connection, shop_id: str, text: str) -> str | None:
    """Rewrite one pending order/book/receipt line ("line 3 is 4 bread")."""
    m = re.match(r"\s*line\s+(\d+)\s+is\s+(.+)$", text or "", re.IGNORECASE)
    if not m:
        return None
    if get_plan(conn, shop_id) is not None:
        return correct_plan_line(conn, shop_id, int(m.group(1)),
                                 m.group(2).strip())
    pend = get_pending(conn, shop_id)
    if pend is None or pend["action_type"] not in ("book_page", "receipt",
                                                  "stock_intake",
                                                  "receipt_link"):
        return None
    if pend["action_type"] == "receipt":
        return correct_receipt_line(conn, shop_id, int(m.group(1)), m.group(2).strip())
    if pend["action_type"] == "stock_intake":
        return correct_intake_line(conn, shop_id, int(m.group(1)),
                                   m.group(2).strip())
    if pend["action_type"] == "receipt_link":
        return correct_link_line(conn, shop_id, int(m.group(1)),
                                 m.group(2).strip())
    return correct_book_line(conn, shop_id, int(m.group(1)), m.group(2).strip())


def try_link_choice_reply(conn: sqlite3.Connection, shop_id: str,
                          text: str) -> tuple[str, list[str] | None] | None:
    """A pending link_choice owns the reply, except control commands."""
    pend = get_pending(conn, shop_id)
    if pend is None or pend["action_type"] != "link_choice":
        return None
    word = _normalize(text or "")
    if word in MENU_WORDS or word in HELP_WORDS or word in RESET_WORDS \
            or word in DELETE_WORDS or word in UNDO_WORDS \
            or word in ("undo book", "resend"):
        return None  # control commands pass through; the choice waits
    return answer_link_choice(conn, shop_id, text)


def try_deferred_link_resume(conn: sqlite3.Connection, shop_id: str,
                             text: str) -> tuple[str, list[str] | None] | None:
    """'resume' restores a paused link_choice (None when none waiting).

    This beats the guided-session resume: a paused receipt question is
    the older business, and the session keeps waiting its turn.
    """
    if _normalize(text or "") != "resume":
        return None
    out = resume_deferred_link(conn, shop_id)
    if out is None:
        return None
    return out, ["link", "new"]


def try_intake_reply(conn: sqlite3.Connection, shop_id: str,
                     text: str) -> str | None:
    """Guided session first, then the pending one-shot stock intake."""
    from app.tools import answer_intake, get_pending
    reply = answer_intake_session(conn, shop_id, text)
    if reply is not None:
        return reply
    pend = get_pending(conn, shop_id)
    if pend is None or pend["action_type"] != "stock_intake":
        return None
    probe = re.sub(r"(?i)\bline\s+\d+\s*:", " ", text or "")
    if _looks_multi(probe):
        return None  # new business goes to the planner, intake waits
    return answer_intake(conn, shop_id, text)


def _with_intake_resume(conn: sqlite3.Connection, shop_id: str,
                        message: str) -> str:
    """Append the guided-intake resume line when a session is still open."""
    session = get_intake_session(conn, shop_id)
    if session is None or (session.get("status") or "active") != "active":
        return message
    items = session.get("items_json") or []
    idx = session.get("current") or 0
    if idx < 0 or idx >= len(items):
        return message
    field = session.get("field")
    if field is None:
        question = _t("g_preview_ask")
    else:
        question = ask_intake_field(items[idx], field)
    return (f"{message}\n\n"
            f"{_t('g_resume_line', i=idx + 1, n=len(items), q=question)}")


def try_account_correction(conn: sqlite3.Connection, shop_id: str, text: str) -> str | None:
    """Switch a pending receipt's account ("from petty cash", "on credit")."""
    pend = get_pending(conn, shop_id)
    if pend is None or pend["action_type"] != "receipt":
        return None
    word = _normalize(text or "")
    m = re.match(r"(?:paid\s+)?from\s+(.+)$", word)
    rest = m.group(1) if m else word
    if rest in ("on credit", "on account", "supplier credit"):
        return correct_receipt_account(conn, shop_id, "supplier")
    if m is None:
        return None
    if "supplier" in rest or "credit" in rest or "account" in rest:
        account = "supplier"
    elif "petty" in rest:
        account = "petty"
    elif "bank" in rest:
        account = "bank"
    else:
        account = "cash"
    return correct_receipt_account(conn, shop_id, account)
