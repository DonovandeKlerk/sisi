"""Message router. Yes/No/Undo are handled deterministically here, never by the model."""

from __future__ import annotations

import sqlite3

from app.llm import chat as llm_chat
from app.llm import save_turn
from app.voice import VoiceError, transcribe_audio
from app.tools import (
    cancel_pending,
    confirm_pending,
    demo_reset,
    ensure_shop,
    get_pending,
    undo_last,
)

YES_WORDS = {"yes", "y", "yebo", "yeah", "ja", "ewe", "ok", "okay", "sharp", "shap", "sure"}
NO_WORDS = {"no", "n", "nope", "cha", "hayi", "nee", "aowa"}
UNDO_WORDS = {"undo"}
RESET_WORDS = {"demo reset", "reset demo"}
_FILLER = ("thank you", "thanks", "please", "sisi", "ta")


def _normalize(text: str) -> str:
    word = " ".join(text.strip().lower().split())
    word = word.strip(" .!?,")
    changed = True
    while changed:
        changed = False
        for tail in _FILLER:
            if word.endswith(" " + tail):
                word = word[: -len(tail) - 1].strip(" .!?,")
                changed = True
    return word


def try_deterministic(conn: sqlite3.Connection, shop_id: str, text: str):
    """Returns (message, buttons_or_None), or None if the LLM should handle it."""
    ensure_shop(conn, shop_id)
    word = _normalize(text or "")
    if word in YES_WORDS:
        return confirm_pending(conn, shop_id), None
    if word in NO_WORDS:
        return cancel_pending(conn, shop_id), None
    if word in UNDO_WORDS:
        return undo_last(conn, shop_id), None
    if word in RESET_WORDS:
        return demo_reset(conn, shop_id), None
    return None


def route_to_llm(conn: sqlite3.Connection, shop_id: str, text: str) -> str:
    return llm_chat(conn, shop_id, text)


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
    return transcript, f"Heard: “{transcript}”\n{reply}", buttons


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
        message = route_to_llm(conn, shop_id, text)  # saves turns itself
    after = get_pending(conn, shop_id)
    buttons = ["Yes", "No"] if after is not None and after["id"] != before_id else None
    return message, buttons
