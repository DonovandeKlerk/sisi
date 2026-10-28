"""Message router. Yes/No/Undo are handled deterministically here, never by the model."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from app import reports
from app.db import today_str
from app.i18n import (CURRENT_LANG, NAMES, STRINGS, detect, normalize_lang,
                      owner_lang, t as _t)
from app.setup import (
    answer as setup_answer,
    flush_logo,
    mark_paused,
    needs_setup,
    setup_step,
    start_setup,
)
from app.llm import chat as llm_chat
from app.llm import save_turn
from app.vision import VisionError, read_expiry_photo, read_shop_photo
from app.voice import VoiceError, transcribe_audio
from app.tools import (
    _is_pack_unit,
    _pack_echo,
    add_credit,
    build_order,
    cancel_pending,
    edit_order_add,
    edit_order_remove,
    get_order_draft,
    set_float,
    check_expiry_photo,
    clear_await_price,
    confirm_pending,
    correct_book_line,
    correct_receipt_account,
    correct_receipt_line,
    delete_my_data,
    demo_reset,
    ensure_pack_price,
    ensure_product_price,
    ensure_shop,
    find_product,
    get_await_price,
    get_pending,
    photo_seen,
    parse_month_name,
    propose_book_page,
    propose_receipt,
    propose_reminder_offer,
    add_staff,
    confirm_reset_shop,
    customer_statement,
    record_sale,
    resend_last_media,
    resolve_sender,
    reset_shop,
    confirm_logo,
    save_shop_logo,
    switch_logo,
    redo_logo,
    set_customer_language,
    set_customer_limit,
    set_expiry,
    set_owner_language,
    set_nudge,
    who_recorded_today,
    statements_for_all,
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
REDO_LOGO_WORDS = {"redo logo", "fix logo", "lungisa ilogo",
                   "phinda wenze ilogo", "maak logo reg", "doen logo oor",
                   "yenzi ilogo kwakhona"}
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
                        "setup", "reset shop", "yes erase everything"}


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


_STATEMENT_TRIGGERS = ("statement", "isitatimende", "sitatimende", "receipt",
                       "irisithi", "risithi", "iresithi", "slip",
                       "staat", "strokie", "afskrif", "istatimenti",
                       "isiliphu",
                       "something to send", "to send to", "send it to",
                       "send this to", "make a picture", "picture for")


def _customer_in_text(word: str, names: list[str]) -> str | None:
    for name in sorted(names, key=len, reverse=True):
        if not name.strip():
            continue
        # optional isiZulu possessive prefix: sikaSipho, kaSipho, owaSipho
        if re.search(rf"\b(?:sika|ka|owa|wa)?{re.escape(name.strip().lower())}\b",
                     word):
            return name
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


def try_reminder_command(conn: sqlite3.Connection, shop_id: str,
                         text: str) -> str | None:
    """Reminder commands and direct 'remind me' requests. None otherwise."""
    from app.reminders import (cancel_numbered, confirm_text, detect_context,
                               list_reminders, move_numbered, parse_request,
                               parse_snooze_minutes, save_reminder,
                               snooze_last)
    word = _normalize(text or "")
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
    if "?" in word:
        return None
    parsed = parse_request(text)
    if parsed is None:
        return None
    if parsed["summary"].lower().startswith(_QUESTION_STARTS):
        return None  # 'remind me how much…' is a question for the books
    context = detect_context(conn, shop_id, text)
    save_reminder(conn, shop_id, parsed["due"], parsed["summary"], context,
                  parsed["recurrence"])
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


def try_reminder_offer(conn: sqlite3.Connection, shop_id: str,
                       text: str) -> str | None:
    """Offer a reminder for a mentioned future date. None otherwise."""
    from app.reminders import offer_scan
    found = offer_scan(text)
    if found is None:
        return None
    return propose_reminder_offer(conn, shop_id, found["due"].isoformat(),
                                  found["summary"])


def try_deterministic(conn: sqlite3.Connection, shop_id: str, text: str):
    """Returns (message, buttons_or_None), or None if the LLM should handle it."""
    ensure_shop(conn, shop_id)
    word = _normalize(text or "")
    if _STAFF_NAME.get() and (word in _STAFF_BLOCKED_EXACT
                              or word.startswith(_STAFF_BLOCKED_PREFIX)):
        from app.tools import _staff_blocked
        return _staff_blocked(), None
    m = re.match(r"(speak|khuluma|praat|thetha)\s+(.+)$", word)
    if m and normalize_lang(m.group(2).strip()) is not None:
        return set_owner_language(conn, shop_id, m.group(2).strip()), None
    m = re.match(r"add staff ([\d\s+()\-]{9,}) (.+)$", (text or "").strip(),
                   re.IGNORECASE)
    if m and _STAFF_NAME.get() is None:
        return add_staff(conn, shop_id, m.group(1), m.group(2).strip()), None
    if word in ("who recorded today", "who recorded", "staff today"):
        return who_recorded_today(conn, shop_id), None
    if word == "float" or word.startswith("float "):
        return set_float(conn, shop_id, _parse_float(word)), None
    statement = try_statement_request(conn, shop_id, text)
    if statement is not None:
        return statement, None
    reminder = try_reminder_command(conn, shop_id, text)
    if reminder is not None:
        return reminder, None
    if word in ORDER_WORDS:
        return build_order(conn, shop_id), None
    if word == "send it" or word.startswith("send it "):
        return send_order(conn, shop_id, word[7:].strip() or None), None
    if word == "use original logo":
        return switch_logo(conn, shop_id, "original"), None
    if word == "remove logo background":
        return switch_logo(conn, shop_id, "cutout"), None
    if word in REDO_LOGO_WORDS:
        return redo_logo(conn, shop_id), None
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
    if word in YES_WORDS:
        if get_pending(conn, shop_id) is None:
            waiting = get_await_price(conn, shop_id)
            if waiting is not None:
                return (_t("ag_price_needed",
                           product=waiting['product']), None)
            seen = confirm_logo(conn, shop_id)
            if seen is not None:
                return seen, None
        return confirm_pending(conn, shop_id), None
    if word in NO_WORDS:
        if get_pending(conn, shop_id) is None and get_await_price(conn, shop_id) is not None:
            clear_await_price(conn, shop_id)
            return _t("ag_sale_dropped"), None
        clear_await_price(conn, shop_id)
        return cancel_pending(conn, shop_id), None
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
        return DEMO_HELP, None
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
        return resend_last_media(conn, shop_id), None
    if word == "logo":
        image = _fresh_photo(shop_id)
        if image is None:
            return _t("logo_howto"), None
        LAST_PHOTO.pop(shop_id, None)
        return save_shop_logo(conn, shop_id, image), None
    offer = try_reminder_offer(conn, shop_id, text)
    if offer is not None:
        return offer, None
    return None


def route_to_llm(conn: sqlite3.Connection, shop_id: str, text: str) -> str:
    return llm_chat(conn, shop_id, text)


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
    return add_credit(conn, shop_id, spec["customer"], spec["items"])


def handle_voice(conn: sqlite3.Connection, shop_id: str, audio: bytes,
               suffix: str = ".ogg"):
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
    reply, buttons = handle_text(conn, shop_id, transcript)
    shown = transcript if len(transcript) <= 100 else transcript[:97] + "…"
    return transcript, _t("ag_heard", shown=shown, reply=reply), buttons


_PHOTO_EMOJI = {"expiry": "📸", "book_page": "📒", "receipt": "🧾"}


def handle_photo(conn: sqlite3.Connection, shop_id: str, image: bytes,
                 caption: str = ""):
    """Classify a photo (logo, expiry, book page, receipt) and propose saving it.

    Returns (doc_or_None, reply, buttons). Nothing is saved until Yes.
    """
    shop_id, staff = _enter(conn, shop_id)
    ensure_shop(conn, shop_id)
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
    if (caption or "").strip().lower() == "logo":
        reply = save_shop_logo(conn, shop_id, image or b"")
        save_turn(conn, shop_id, "user", "[logo photo]")
        save_turn(conn, shop_id, "assistant", reply)
        return None, reply, None
    digest = hashlib.sha256(image or b"").hexdigest()
    seen = photo_seen(conn, shop_id, digest)
    if seen is not None:
        day = (seen["created_at"] or "")[:10]
        emoji = _PHOTO_EMOJI.get(seen["kind"], "📸")
        return None, _t("ag_photo_dup", emoji=emoji, day=day), None
    before = get_pending(conn, shop_id)
    before_id = before["id"] if before else None
    try:
        doc = read_shop_photo(image, shop_id)
    except VisionError as e:
        return None, str(e), None
    save_turn(conn, shop_id, "user", "[photo]")
    kind = doc.get("doc_type", "other")
    if kind == "expiry":
        reply = check_expiry_photo(conn, shop_id, doc.get("expiry", {}).get("items", []),
                                   photo_hash=digest)
    elif kind == "book_page":
        reply = propose_book_page(conn, shop_id, doc.get("book", {}), photo_hash=digest)
    elif kind == "receipt":
        reply = propose_receipt(conn, shop_id, doc.get("receipt", {}), photo_hash=digest)
    else:
        reply = _t("ag_photo_unknown")
        save_turn(conn, shop_id, "assistant", reply)
        return doc, reply, None
    save_turn(conn, shop_id, "assistant", reply)
    after = get_pending(conn, shop_id)
    buttons = ["Yes", "No"] if after is not None and after["id"] != before_id else None
    return doc, reply, buttons


def handle_text(conn: sqlite3.Connection, shop_id: str, text: str):
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
    direct = try_deterministic(conn, shop_id, text)
    if direct is not None:
        message, _ = direct
        save_turn(conn, shop_id, "user", text)
        save_turn(conn, shop_id, "assistant", message)
    else:
        priced = try_price_reply(conn, shop_id, text)
        if priced is not None:
            message = priced
            save_turn(conn, shop_id, "user", text)
            save_turn(conn, shop_id, "assistant", message)
        else:
            corrected = try_line_correction(conn, shop_id, text)
            if corrected is None:
                corrected = try_account_correction(conn, shop_id, text)
            if corrected is not None:
                message = corrected
                save_turn(conn, shop_id, "user", text)
                save_turn(conn, shop_id, "assistant", message)
            else:
                message = route_to_llm(conn, shop_id, text)  # saves turns itself
    message = _maybe_first_offer(conn, shop_id, message)
    logo_note = flush_logo(conn, shop_id)
    if logo_note:
        message = f"{message}\n\n{logo_note}"
    after = get_pending(conn, shop_id)
    buttons = ["Yes", "No"] if after is not None and after["id"] != before_id else None
    return message, buttons


def _maybe_first_offer(conn: sqlite3.Connection, shop_id: str, message: str) -> str:
    """On the 1st, once per month, offer last month's books when it has sales."""
    today = today_str()
    if not today.endswith("-01"):
        return message
    month = reports.prev_month(today[:7])
    key = f"offer-{month}"
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
    """Rewrite one pending book/receipt line ("line 3 is 4 bread"). None otherwise."""
    pend = get_pending(conn, shop_id)
    if pend is None or pend["action_type"] not in ("book_page", "receipt"):
        return None
    m = re.match(r"\s*line\s+(\d+)\s+is\s+(.+)$", text or "", re.IGNORECASE)
    if not m:
        return None
    if pend["action_type"] == "receipt":
        return correct_receipt_line(conn, shop_id, int(m.group(1)), m.group(2).strip())
    return correct_book_line(conn, shop_id, int(m.group(1)), m.group(2).strip())


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
