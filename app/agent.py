"""Message router. Yes/No/Undo are handled deterministically here, never by the model."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from app.llm import chat as llm_chat
from app.llm import save_turn
from app.vision import VisionError, read_expiry_photo
from app.voice import VoiceError, transcribe_audio
from app.tools import (
    _is_pack_unit,
    _pack_echo,
    add_credit,
    cancel_pending,
    check_expiry_photo,
    clear_await_price,
    confirm_pending,
    delete_my_data,
    demo_reset,
    ensure_pack_price,
    ensure_product_price,
    ensure_shop,
    find_product,
    get_await_price,
    get_pending,
    record_sale,
    undo_last,
)

YES_WORDS = {"yes", "y", "yebo", "yeah", "ja", "ewe", "ok", "okay", "sharp", "shap", "sure"}
NO_WORDS = {"no", "n", "nope", "cha", "hayi", "nee", "aowa"}
UNDO_WORDS = {"undo"}
RESET_WORDS = {"demo reset", "reset demo"}
DELETE_WORDS = {"delete my data", "erase my data", "forget me"}
HELP_WORDS = {"demo help", "help", "what can you do", "commands"}

DEMO_HELP = (
    "❓ *Demo help — try me*\n"
    "\n"
    "• *stock list* — 'what do we have in stock?'\n"
    "• *add stock* — 'add 20 bread at R18'\n"
    "• *sale* — 'sold 2 coke'\n"
    "• *credit sale* — 'Sipho took 2 bread'\n"
    "• *who owes* — 'who owes?'\n"
    "• *cash-up* — 'cash up'\n"
    "• *low stock* — 'what is low?'\n"
    "• *restock* — 'what must I buy tomorrow?'\n"
    "• *expiry photo* — send a photo of a date stamp\n"
    "• *compliance* — 'can I get the spaza fund?'\n"
    "• *scam check* — 'is this fund agent a scam?'\n"
    "• *undo* — 'undo'\n"
    "• *delete my data* — wipes this number\n"
    "\n"
    "🔒 *Privacy:* your records stay on this number. Reply 'delete my data' to erase them."
)
_FILLER = ("thank you", "thanks", "please", "sisi", "ta")


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


def try_deterministic(conn: sqlite3.Connection, shop_id: str, text: str):
    """Returns (message, buttons_or_None), or None if the LLM should handle it."""
    ensure_shop(conn, shop_id)
    word = _normalize(text or "")
    if word in YES_WORDS:
        if get_pending(conn, shop_id) is None:
            waiting = get_await_price(conn, shop_id)
            if waiting is not None:
                return (f"Still need the price — reply with it, e.g. '{waiting['product']} R18'.",
                        None)
        return confirm_pending(conn, shop_id), None
    if word in NO_WORDS:
        if get_pending(conn, shop_id) is None and get_await_price(conn, shop_id) is not None:
            clear_await_price(conn, shop_id)
            return "OK, I dropped that sale.", None
        clear_await_price(conn, shop_id)
        return cancel_pending(conn, shop_id), None
    if word in UNDO_WORDS:
        return undo_last(conn, shop_id), None
    if word in RESET_WORDS:
        return demo_reset(conn, shop_id), None
    if word in DELETE_WORDS:
        return delete_my_data(conn, shop_id), None
    if word in HELP_WORDS:
        return DEMO_HELP, None
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
        return (f"One price at a time — reply with just the {product} price,"
                f" e.g. '{product} R18'.")
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
            return (f"❓ *{product} has no {_pack_echo(text)} size yet.*\n"
                    f"Reply with the single price, e.g. '{product.lower()} R2'.")
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
    ensure_shop(conn, shop_id)
    extra = [r["name"] for r in conn.execute(
        "SELECT name FROM customers WHERE shop_id = ?", (shop_id,)).fetchall()]
    extra += [r["name"] for r in conn.execute(
        "SELECT name FROM products WHERE shop_id = ?", (shop_id,)).fetchall()]
    try:
        transcript = transcribe_audio(audio, suffix, shop_id, extra).strip()
    except VoiceError as e:
        return None, str(e), None
    if not transcript:
        return None, "I couldn't hear anything in that voice note. Send it again?", None
    reply, buttons = handle_text(conn, shop_id, transcript)
    shown = transcript if len(transcript) <= 100 else transcript[:97] + "…"
    return transcript, f"Heard: “{shown}”\n{reply}", buttons


def handle_photo(conn: sqlite3.Connection, shop_id: str, image: bytes):
    """Read expiry dates from a photo and propose saving the findings.

    Returns (findings_or_None, reply, buttons). Nothing is saved until Yes.
    """
    ensure_shop(conn, shop_id)
    before = get_pending(conn, shop_id)
    before_id = before["id"] if before else None
    try:
        findings = read_expiry_photo(image, shop_id)
    except VisionError as e:
        return None, str(e), None
    save_turn(conn, shop_id, "user", "[photo]")
    reply = check_expiry_photo(conn, shop_id, findings)
    save_turn(conn, shop_id, "assistant", reply)
    after = get_pending(conn, shop_id)
    buttons = ["Yes", "No"] if after is not None and after["id"] != before_id else None
    return findings, reply, buttons


def handle_text(conn: sqlite3.Connection, shop_id: str, text: str):
    ensure_shop(conn, shop_id)
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
            message = route_to_llm(conn, shop_id, text)  # saves turns itself
    after = get_pending(conn, shop_id)
    buttons = ["Yes", "No"] if after is not None and after["id"] != before_id else None
    return message, buttons
