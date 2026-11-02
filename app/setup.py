"""Guided setup: 6 steps for a new number, reset demo/shop, or 'setup'.

State lives in flags (setup-step, setup-done, setup-return, setup-reseed)
so a quiet owner resumes at the same step next time they message.
"""

from __future__ import annotations

import logging
import os
import random
import re
import sqlite3
from datetime import date

log = logging.getLogger("sisi")

STEPS = {
    1: "🏪",
    2: "📍",
    3: "📞",
    4: "🖼️",
    5: "💵",
    6: "🗣️",
    7: "☀️",
}
TOTAL = 7

CHANGE_WORDS = {
    "name": 1, "business name": 1, "shop name": 1, "naam": 1, "igama": 1,
    "address": 2, "adres": 2, "ikheli": 2, "idilesi": 2,
    "phone": 3, "number": 3, "contact": 3, "contact number": 3,
    "telefoon": 3, "ifoni": 3, "inombolo": 3,
    "logo": 4, "picture": 4,
    "float": 5, "cash": 5, "money": 5, "imali": 5, "geld": 5,
    "language": 6, "zulu": 6, "english": 6, "taal": 6, "ulimi": 6,
    "ulwimi": 6, "sesotho": 6, "sotho": 6, "setswana": 6, "tswana": 6,
    "puo": 6,
    "plan": 7, "daily plan": 7, "icebo": 7, "dagplan": 7,
}


def _flag(conn: sqlite3.Connection, shop_id: str, key: str) -> str | None:
    row = conn.execute("SELECT value FROM flags WHERE shop_id = ? AND key = ?",
                       (shop_id, key)).fetchone()
    return row["value"] if row else None


def _set_flag(conn: sqlite3.Connection, shop_id: str, key: str, value: str) -> None:
    conn.execute("INSERT INTO flags (shop_id, key, value) VALUES (?, ?, ?)"
                 " ON CONFLICT (shop_id, key) DO UPDATE SET value = excluded.value",
                 (shop_id, key, value))
    conn.commit()


def setup_step(conn: sqlite3.Connection, shop_id: str) -> int | None:
    val = _flag(conn, shop_id, "setup-step")
    try:
        return int(val) if val else None
    except ValueError:
        return None


def needs_setup(conn: sqlite3.Connection, shop_id: str) -> bool:
    """True for a fresh shop that never finished setup. Shops with books pass."""
    from app.db import read_only
    if _flag(conn, shop_id, "setup-done"):
        return False
    if setup_step(conn, shop_id):
        return True
    for table in ("products", "sales", "credit_entries"):
        n = conn.execute(f"SELECT COUNT(*) n FROM {table} WHERE shop_id = ?",
                         (shop_id,)).fetchone()["n"]
        if n:
            if not read_only():
                _set_flag(conn, shop_id, "setup-done", "1")
            return False
    return True


def _clear_retries(conn: sqlite3.Connection, shop_id: str) -> None:
    conn.execute("DELETE FROM flags WHERE shop_id = ? AND key LIKE 'setup-retry-%'",
                 (shop_id,))
    conn.commit()


def _failed_once(conn: sqlite3.Connection, shop_id: str, step: int) -> bool:
    """True when this step already failed once: default, don't re-ask."""
    key = f"setup-retry-{step}"
    if _flag(conn, shop_id, key):
        conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = ?",
                     (shop_id, key))
        conn.commit()
        return True
    _set_flag(conn, shop_id, key, "1")
    return False


def _clear_retry(conn: sqlite3.Connection, shop_id: str, step: int) -> None:
    conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = ?",
                 (shop_id, f"setup-retry-{step}"))
    conn.commit()


def start_setup(conn: sqlite3.Connection, shop_id: str, reseed: bool = False) -> str:
    _set_flag(conn, shop_id, "setup-step", "1")
    conn.execute("DELETE FROM flags WHERE shop_id = ? AND key IN"
                 " ('setup-done', 'setup-return', 'setup-reseed', 'setup-paused')",
                 (shop_id,))
    _clear_retries(conn, shop_id)
    if reseed:
        _set_flag(conn, shop_id, "setup-reseed", "1")
    conn.commit()
    return step_prompt(conn, shop_id, 1, intro=True)


def step_prompt(conn: sqlite3.Connection, shop_id: str, step: int, intro: bool = False) -> str:
    from app.i18n import t
    from app.tools import ensure_shop
    head = t("setup_head", n=step, total=TOTAL, emoji=STEPS[step])
    if step == 1:
        body = t("setup_intro") if intro else t("setup_name", head=head)
    elif step == 2:
        body = t("setup_address", head=head)
    elif step == 3:
        body = t("setup_phone", head=head, shop=shop_id)
    elif step == 4:
        body = t("setup_logo")
    elif step == 5:
        body = t("setup_float", head=head)
    elif step == 6:
        body = t("setup_lang", head=head)
    else:
        body = t("setup_plan", head=head)
    paused = _flag(conn, shop_id, "setup-paused")
    if paused and not intro:
        conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'setup-paused'",
                     (shop_id,))
        conn.commit()
        return t("setup_resume", n=step, total=TOTAL, emoji=STEPS[step],
                 body=body)
    return body


def mark_paused(conn: sqlite3.Connection, shop_id: str) -> None:
    if setup_step(conn, shop_id):
        _set_flag(conn, shop_id, "setup-paused", "1")


def skip_setup(conn: sqlite3.Connection, shop_id: str) -> None:
    """Mark setup done (tests and imports for shops that already trade)."""
    from app.tools import ensure_shop
    ensure_shop(conn, shop_id)
    conn.execute("DELETE FROM flags WHERE shop_id = ? AND key LIKE 'setup-%'",
                 (shop_id,))
    _set_flag(conn, shop_id, "setup-done", "1")


def answer(conn: sqlite3.Connection, shop_id: str, sender: str, text: str) -> str:
    """One setup reply. No exception may stall the flow: skip and continue."""
    try:
        return _answer(conn, shop_id, sender, text)
    except Exception:
        log.warning("setup step failed; skipping ahead", exc_info=True)
    step = setup_step(conn, shop_id) or 1
    try:
        nxt = _advance(conn, shop_id, step, None)
    except Exception:
        log.warning("setup advance failed; finishing", exc_info=True)
        return _safe_finish(conn, shop_id)
    from app.i18n import t
    return t("setup_skipped", n=step, next=nxt)


def _safe_finish(conn: sqlite3.Connection, shop_id: str) -> str:
    """Last-resort All set card when even advancing raises."""
    try:
        conn.execute("DELETE FROM flags WHERE shop_id = ? AND key IN"
                     " ('setup-step', 'setup-return', 'setup-paused')", (shop_id,))
        _set_flag(conn, shop_id, "setup-done", "1")
    except Exception:
        log.warning("safe finish flag write failed", exc_info=True)
    from app.i18n import t
    return t("setup_safe")


BACK_WORDS = ("back", "buyela", "terug", "kgutlela", "boela")
SKIP_WORDS = ("skip", "yeqa", "slaan oor", "tsiba", "tlola")
CHANGE_PREFIXES = ("change ", "verander ", "shintsha ", "tshintsha ",
                   "fetola ")

SETUP_BUTTONS = {
    2: ["Skip"],
    3: ["Yes, use this number"],
    4: ["Skip"],
    5: ["Skip"],
    7: ["Yes", "Pick a time", "No"],
}
"""Canonical English tap buttons per step (display is localized per turn)."""
SETUP_LIST_BUTTON = "Language"
SETUP_LIST_ROWS = ["English", "isiZulu", "Afrikaans", "isiXhosa", "Sesotho",
                   "Setswana"]
"""Step 6 arrives as a list message (digits 1-6 resolve the fallback)."""


def setup_buttons(step: int | None) -> list[str] | None:
    """Localized button labels for a setup step (≤3, ≤20 chars each)."""
    if step is None:
        return None
    from app.i18n import t
    if step in (2, 4, 5):
        return [t("btn_skip")]
    if step == 3:
        return [t("btn_use_number")]
    if step == 7:
        return [t("btn_yes"), t("btn_pick_time"), t("btn_no")]
    return None


def setup_list(step: int | None):
    """(button_text, rows) for list steps, else None."""
    if step == 6:
        return SETUP_LIST_BUTTON, list(SETUP_LIST_ROWS)
    return None


def _answer(conn: sqlite3.Connection, shop_id: str, sender: str, text: str) -> str:
    """Handle one setup reply. Returns the next prompt or the finish card."""
    from app.agent import _parse_float
    from app.i18n import CURRENT_LANG, normalize_lang, t
    from app.tools import (set_float, set_shop_address, set_shop_name,
                           set_shop_phone, straight_quotes)
    step = setup_step(conn, shop_id) or 1
    word = "".join(ch for ch in (text or "") if ch.isalnum() or ch in " ',.-?!/:").strip().lower()
    word = " ".join(word.split())
    if word in BACK_WORDS:
        return _goto(conn, shop_id, max(1, step - 1))
    for prefix in CHANGE_PREFIXES:
        if word.startswith(prefix):
            target = CHANGE_WORDS.get(word[len(prefix):].strip())
            if target is None:
                return t("setup_which",
                         prompt=step_prompt(conn, shop_id, step))
            _set_flag(conn, shop_id, "setup-return", str(step))
            return _goto(conn, shop_id, target)
    m = re.match(r"\s*(\d{1,2})\s*[.)]?\s*$", text or "")
    if m is not None:
        btns = setup_buttons(step)
        if btns is not None:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(btns):
                text = btns[idx]
                word = "".join(ch for ch in text if ch.isalnum() or ch in " ',.-?!/:").strip().lower()
                word = " ".join(word.split())
    if step == 1:
        name = straight_quotes((text or "").strip())
        if not name or len(name) < 2:
            if _failed_once(conn, shop_id, 1):
                return _advance(conn, shop_id, 1, None)  # keep Shop <id>
            return t("setup_name_invalid",
                     prompt=step_prompt(conn, shop_id, 1))
        set_shop_name(conn, shop_id, name[:60])
        return _advance(conn, shop_id, 1, t("setup_name_nice", name=name[:60]))
    if step == 2:
        if word not in SKIP_WORDS:
            set_shop_address(conn, shop_id, (text or "").strip()[:120])
        return _advance(conn, shop_id, 2, None)
    if step == 3:
        digits = "".join(c for c in (text or "") if c.isdigit())
        if len(digits) >= 9:
            set_shop_phone(conn, shop_id, digits)
        elif _has_any(word, _PHONE_YES):
            set_shop_phone(conn, shop_id, sender)
        else:
            if _failed_once(conn, shop_id, 3):
                set_shop_phone(conn, shop_id, sender)
                return _advance(conn, shop_id, 3, None)
            return t("setup_phone_invalid", sender=sender,
                     prompt=step_prompt(conn, shop_id, 3))
        return _advance(conn, shop_id, 3, None)
    if step == 4:
        if word in SKIP_WORDS or _failed_once(conn, shop_id, 4):
            _clear_retry(conn, shop_id, 4)
            return _advance(conn, shop_id, 4, None)
        return t("setup_logo_send", prompt=step_prompt(conn, shop_id, 4))
    if step == 5:
        if word in SKIP_WORDS or word in _FLOAT_ZERO:
            return _advance(conn, shop_id, 5, None)
        amounts = _parse_float(f"float {(text or '')}")
        if not amounts:
            if _failed_once(conn, shop_id, 5):
                return _advance(conn, shop_id, 5, None)  # float stays zero
            return t("setup_float_invalid",
                     prompt=step_prompt(conn, shop_id, 5))
        set_float(conn, shop_id, amounts)
        return _advance(conn, shop_id, 5, None)
    if step == 6:
        code = _lenient_lang(word)
        if code is not None:
            conn.execute("UPDATE shops SET language = ? WHERE shop_id = ?",
                         (code, shop_id))
            conn.commit()
            CURRENT_LANG.set(code)
            return _advance(conn, shop_id, 6, None)
        if _failed_once(conn, shop_id, 6):
            return _advance(conn, shop_id, 6, None)  # keep English
        return t("setup_lang_invalid", prompt=step_prompt(conn, shop_id, 6))
    return _answer_plan(conn, shop_id, word, text)


PLAN_YES = ("yes", "y", "yebo", "yeah", "ja", "ewe", "ee", "eya", "ok",
            "okay", "sharp", "shap", "sure", "on", "please", "yep",
            "yea", "aye", "definitely", "certainly")
PLAN_NO = ("no", "n", "nope", "nah", "nee", "off", "hayi", "cha", "aikona",
           "never", "not", "dont", "don't", "aowa", "tjhe", "nnyaa",
           "nyaa", "skip", "yeqa", "slaan oor", "tsiba", "tlola")
PICK_TIME_WORDS = ("pick a time", "pick time", "choose a time", "choose time",
                   "set a time", "set time", "a time", "another time",
                   "different time")

_PHONE_YES_EXTRA = ("same", "keep", "keep it", "this number", "this one",
                    "current", "sender", "mine", "my number")
_PHONE_YES_TOKENS = ("yes", "y", "yebo", "yeah", "ja", "ewe", "ok", "okay",
                     "sharp", "shap", "sure", "same", "keep", "mine",
                     "current", "sender")
_PHONE_YES = _PHONE_YES_TOKENS + _PHONE_YES_EXTRA

_FLOAT_ZERO = ("none", "no", "nope", "zero", "0", "r0", "nothing", "no float",
               "akukho", "niks", "geen", "hayi")

_LANG_NUMBERS = {"1": "en", "2": "zu", "3": "af", "4": "xh", "5": "st",
                "6": "tn"}
"""Digit answers follow the prompt order: English, isiZulu, Afrikaans, isiXhosa, Sesotho, Setswana."""


def _tokens(word: str) -> list[str]:
    return [tok for tok in re.split(r"[^a-z0-9']+", word or "") if tok]


def _has_any(word: str, options: tuple[str, ...]) -> bool:
    toks = set(_tokens(word))
    for opt in options:
        if " " in opt:
            if opt in (word or ""):
                return True
        elif opt in toks:
            return True
    return False


def _lenient_lang(word: str) -> str | None:
    """Language answer: exact, digit (1-6), or name anywhere in the text."""
    from app.i18n import LANG_CODES, normalize_lang
    code = normalize_lang(word)
    if code is not None:
        return code
    toks = _tokens(word)
    for tok in toks:
        if tok in _LANG_NUMBERS:
            return _LANG_NUMBERS[tok]
    for name, lang in LANG_CODES.items():
        if len(name) >= 2 and name in toks:
            return lang
    for name, lang in LANG_CODES.items():
        if len(name) > 2 and name in (word or ""):
            return lang
    return None


def _answer_plan(conn: sqlite3.Connection, shop_id: str, word: str,
                 text: str) -> str:
    """Step 7: daily plan Yes / time / No, then the finish card."""
    from app.i18n import t
    from app.reminders import extract_time
    hm = extract_time(text or "")
    if hm is not None:
        _set_flag(conn, shop_id, "plan_time", f"{hm[0]:02d}:{hm[1]:02d}")
        conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'plan_off'",
                     (shop_id,))
        conn.commit()
        return _finish(conn, shop_id)
    if _has_any(word, PLAN_NO):
        _set_flag(conn, shop_id, "plan_off", "1")
        return _finish(conn, shop_id)
    if _has_any(word, PLAN_YES):
        conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'plan_off'",
                     (shop_id,))
        conn.commit()
        return _finish(conn, shop_id)
    if word in PICK_TIME_WORDS or _has_any(word, PICK_TIME_WORDS):
        if _failed_once(conn, shop_id, 7):
            return _finish(conn, shop_id)  # asked twice: keep 06:00
        return t("setup_plan_time_q", prompt=step_prompt(conn, shop_id, 7))
    if _failed_once(conn, shop_id, 7):
        return _finish(conn, shop_id)  # unclear twice: keep 06:00, move on
    return t("setup_plan_invalid", prompt=step_prompt(conn, shop_id, 7))


def _goto(conn: sqlite3.Connection, shop_id: str, step: int) -> str:
    _set_flag(conn, shop_id, "setup-step", str(step))
    _clear_retry(conn, shop_id, step)
    return step_prompt(conn, shop_id, step)


def _advance(conn: sqlite3.Connection, shop_id: str, step: int, ack: str | None) -> str:
    _clear_retry(conn, shop_id, step)
    nxt = _flag(conn, shop_id, "setup-return")
    if nxt:
        conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'setup-return'",
                     (shop_id,))
        conn.commit()
        out = _goto(conn, shop_id, int(nxt))
        return f"{ack}\n\n{out}" if ack else out
    if step >= TOTAL:
        return _finish(conn, shop_id)
    out = _goto(conn, shop_id, step + 1)
    return f"{ack}\n\n{out}" if ack else out


def handle_photo(conn: sqlite3.Connection, shop_id: str, image: bytes) -> str | None:
    """Step-4 logo upload: save the original as-is, preview it, advance.

    Never blocks: invalid bytes skip the step and the flow continues.
    None when setup doesn't want a photo.
    """
    from app.tools import save_shop_logo
    if setup_step(conn, shop_id) != 4:
        return None
    from app.i18n import t
    try:
        saved = save_shop_logo(conn, shop_id, image or b"")
    except Exception:
        log.warning("setup logo save failed", exc_info=True)
        saved = ""
    if not saved or saved == t("logo_not_photo"):
        return t("setup_logo_skipped", next=_advance(conn, shop_id, 4, None))
    return f"{saved}\n\n{_advance(conn, shop_id, 4, None)}"


def _finish(conn: sqlite3.Connection, shop_id: str) -> str:
    from app import reports as _reports
    conn.execute("DELETE FROM flags WHERE shop_id = ? AND key IN"
                 " ('setup-step', 'setup-return', 'setup-paused')", (shop_id,))
    _clear_retries(conn, shop_id)
    _set_flag(conn, shop_id, "setup-done", "1")
    reseed = _flag(conn, shop_id, "setup-reseed")
    if reseed:
        conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'setup-reseed'",
                     (shop_id,))
        conn.commit()
        from seed.seed_random import _seed_shop_books
        from app.db import now_iso, today_str
        _seed_shop_books(conn, shop_id, random.Random(),
                         date.fromisoformat(today_str()), now_iso())
        conn.commit()
    brand = _reports.shop_brand(conn, shop_id)
    _reports.ensure_dirs()
    from app.i18n import t
    out = t("setup_done", name=brand["name"])
    if _queue_header_card(conn, shop_id, t("cap_header")):
        out += t("setup_card_below")
    return out


def _queue_header_card(conn: sqlite3.Connection, shop_id: str,
                       caption: str) -> bool:
    """Queue the branded header preview. False when no public URL is set."""
    from app import reports as _reports
    from app.agent import queue_media as _queue
    from app.i18n import doc_lang, owner_lang
    base = os.environ.get("SISI_PUBLIC_URL", "").rstrip("/")
    if not base:
        return False
    brand = _reports.shop_brand(conn, shop_id)
    _reports.ensure_dirs()
    safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
    card = str(_reports.REPORTS_DIR / f"{safe}-profile.png")
    with doc_lang(owner_lang(conn, shop_id)):
        _reports.render_shop_card(brand, card)
    token = _reports.store_card(conn, shop_id, "profile", card)
    _queue(shop_id, [{"kind": "image", "url": f"{base}/media/{token}?f=png",
                      "caption": caption, "filename": ""}])
    return True


