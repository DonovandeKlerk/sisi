"""Guided setup: 6 steps for a new number, reset demo/shop, or 'setup'.

State lives in flags (setup-step, setup-done, setup-return, setup-reseed)
so a quiet owner resumes at the same step next time they message.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date

STEPS = {
    1: "🏪",
    2: "📍",
    3: "📞",
    4: "🖼️",
    5: "💵",
    6: "🗣️",
}
TOTAL = 6

CHANGE_WORDS = {
    "name": 1, "business name": 1, "shop name": 1,
    "address": 2,
    "phone": 3, "number": 3, "contact": 3, "contact number": 3,
    "logo": 4, "picture": 4,
    "float": 5, "cash": 5, "money": 5,
    "language": 6, "zulu": 6, "english": 6,
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
    if _flag(conn, shop_id, "setup-done"):
        return False
    if setup_step(conn, shop_id):
        return True
    for table in ("products", "sales", "credit_entries"):
        n = conn.execute(f"SELECT COUNT(*) n FROM {table} WHERE shop_id = ?",
                         (shop_id,)).fetchone()["n"]
        if n:
            _set_flag(conn, shop_id, "setup-done", "1")
            return False
    return True


def start_setup(conn: sqlite3.Connection, shop_id: str, reseed: bool = False) -> str:
    _set_flag(conn, shop_id, "setup-step", "1")
    conn.execute("DELETE FROM flags WHERE shop_id = ? AND key IN"
                 " ('setup-done', 'setup-return', 'setup-reseed', 'setup-paused')",
                 (shop_id,))
    if reseed:
        _set_flag(conn, shop_id, "setup-reseed", "1")
    conn.commit()
    return step_prompt(conn, shop_id, 1, intro=True)


def step_prompt(conn: sqlite3.Connection, shop_id: str, step: int, intro: bool = False) -> str:
    from app.tools import ensure_shop
    head = f"Step {step} of {TOTAL} {STEPS[step]}"
    if step == 1:
        body = ("🏪 *Let's set up your shop.*\n\nStep 1 of 6 🏪\n"
                "What is your business name?" if intro else
                f"{head}\nWhat is your business name?")
    elif step == 2:
        body = f"{head}\nWhat is your shop address? (or 'skip')"
    elif step == 3:
        body = (f"{head}\nI'll use {shop_id} as your contact number."
                "\nReply Yes to keep it, or send another number.")
    elif step == 4:
        body = (f"{head}\nSend your shop logo as a photo — I'll remove its"
                " background. (or 'skip' for a name-only header)")
    elif step == 5:
        body = (f"{head}\nWhat is your opening float? Reply like"
                " '1500 cash, 300 petty'.")
    else:
        body = f"{head}\nWhich language? Reply English or isiZulu."
    paused = _flag(conn, shop_id, "setup-paused")
    if paused and not intro:
        conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'setup-paused'",
                     (shop_id,))
        conn.commit()
        return f"Let's finish setting up, step {step} of {TOTAL} {STEPS[step]}\n\n{body}"
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
    """Handle one setup reply. Returns the next prompt or the finish card."""
    from app.agent import _parse_float
    from app.tools import set_float, set_shop_address, set_shop_name, set_shop_phone
    step = setup_step(conn, shop_id) or 1
    word = " ".join((text or "").strip().lower().split())
    if word == "back":
        return _goto(conn, shop_id, max(1, step - 1))
    if word.startswith("change "):
        target = CHANGE_WORDS.get(word[7:].strip())
        if target is None:
            return ("Which part? Say 'change name', 'address', 'phone',"
                    f" 'logo', 'float' or 'language'.\n\n{step_prompt(conn, shop_id, step)}")
        _set_flag(conn, shop_id, "setup-return", str(step))
        return _goto(conn, shop_id, target)
    if step == 1:
        name = (text or "").strip()
        if not name or len(name) < 2:
            return f"Give me your business name.\n\n{step_prompt(conn, shop_id, 1)}"
        set_shop_name(conn, shop_id, name[:60])
        return _advance(conn, shop_id, 1, f"✅ *{name[:60]}* — nice name.")
    if step == 2:
        if word != "skip":
            set_shop_address(conn, shop_id, (text or "").strip()[:120])
        return _advance(conn, shop_id, 2, None)
    if step == 3:
        if word in ("yes", "y", "yebo", "yeah", "ja", "ewe", "ok", "okay", "sharp",
                    "shap", "sure"):
            set_shop_phone(conn, shop_id, sender)
        else:
            digits = "".join(c for c in (text or "") if c.isdigit())
            if len(digits) < 9:
                return ("Send a valid number, or reply Yes to keep"
                        f" {sender}.\n\n{step_prompt(conn, shop_id, 3)}")
            set_shop_phone(conn, shop_id, digits)
        return _advance(conn, shop_id, 3, None)
    if step == 4:
        if word == "skip":
            return _advance(conn, shop_id, 4, None)
        return ("Send your logo as a photo, or reply 'skip'.\n\n"
                f"{step_prompt(conn, shop_id, 4)}")
    if step == 5:
        amounts = _parse_float(f"float {(text or '')}")
        if not amounts:
            return ("Tell me the float like '1500 cash, 300 petty'.\n\n"
                    f"{step_prompt(conn, shop_id, 5)}")
        set_float(conn, shop_id, amounts)
        return _advance(conn, shop_id, 5, None)
    if word in ("english", "en"):
        conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?",
                     (shop_id,))
        conn.commit()
        return _finish(conn, shop_id)
    if word in ("isizulu", "zulu", "zu"):
        conn.execute("UPDATE shops SET language = 'zu' WHERE shop_id = ?",
                     (shop_id,))
        conn.commit()
        return _finish(conn, shop_id)
    return f"Reply English or isiZulu.\n\n{step_prompt(conn, shop_id, 6)}"


def _goto(conn: sqlite3.Connection, shop_id: str, step: int) -> str:
    _set_flag(conn, shop_id, "setup-step", str(step))
    return step_prompt(conn, shop_id, step)


def _advance(conn: sqlite3.Connection, shop_id: str, step: int, ack: str | None) -> str:
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
    """Step-4 logo upload during setup. None when setup doesn't want a photo."""
    from app.tools import save_shop_logo
    if setup_step(conn, shop_id) != 4:
        return None
    out = save_shop_logo(conn, shop_id, image or b"")
    if out.startswith("❌"):
        return out
    return f"{out}\n\n{_advance(conn, shop_id, 4, None)}"


def _finish(conn: sqlite3.Connection, shop_id: str) -> str:
    import os as _os
    from app import reports as _reports
    from app.agent import queue_media as _queue
    conn.execute("DELETE FROM flags WHERE shop_id = ? AND key IN"
                 " ('setup-step', 'setup-return', 'setup-paused')", (shop_id,))
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
    out = (f"✅ *You're all set, {brand['name']}!*\n\nTry these:\n"
           "• 'sold 2 bread' — record a sale\n"
           "• 'Sipho took 1 coke' — credit sale\n"
           "• 'cash up' — today's totals")
    base = _os.environ.get("SISI_PUBLIC_URL", "").rstrip("/")
    if base:
        safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
        card = str(_reports.REPORTS_DIR / f"{safe}-profile.png")
        _reports.render_shop_card(brand, card)
        token = _reports.store_card(conn, shop_id, "profile", card)
        _queue(shop_id, [{"kind": "image", "url": f"{base}/media/{token}?f=png",
                          "caption": "Your branded header", "filename": ""}])
        out += "\n\n📎 Your header preview below."
    return out
