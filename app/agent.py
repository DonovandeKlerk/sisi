"""Message router. Yes/No/Undo are handled deterministically here, never by the model."""

from __future__ import annotations

import sqlite3

from app.tools import (
    cancel_pending,
    confirm_pending,
    demo_reset,
    ensure_shop,
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


def route_to_llm(conn: sqlite3.Connection, shop_id: str, text: str) -> str:  # Step 3
    raise NotImplementedError("Step 3 wires the model")


def handle_text(conn: sqlite3.Connection, shop_id: str, text: str):
    direct = try_deterministic(conn, shop_id, text)
    if direct is not None:
        return direct
    return route_to_llm(conn, shop_id, text), None
