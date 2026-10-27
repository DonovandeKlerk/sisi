"""Guided setup: 6 steps for a new number, reset demo/shop, or 'setup'.

State lives in flags (setup-step, setup-done, setup-return, setup-reseed)
so a quiet owner resumes at the same step next time they message.
"""

from __future__ import annotations

import logging
import os
import random
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
                 " ('setup-done', 'setup-return', 'setup-reseed', 'setup-paused',"
                 " 'logo-polish', 'logo-polish-started', 'logo-notified')",
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
        body = ("🖼️ *Step 4 of 6: Logo*\nJust send your logo as a photo,"
                " no caption needed. Or reply *skip*.")
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
    return f"⚠️ Skipped step {step} — let's keep going.\n\n{nxt}"


def _safe_finish(conn: sqlite3.Connection, shop_id: str) -> str:
    """Last-resort All set card when even advancing raises."""
    try:
        conn.execute("DELETE FROM flags WHERE shop_id = ? AND key IN"
                     " ('setup-step', 'setup-return', 'setup-paused')", (shop_id,))
        _set_flag(conn, shop_id, "setup-done", "1")
    except Exception:
        log.warning("safe finish flag write failed", exc_info=True)
    return "✅ *You're all set!*\n\nTry 'sold 2 bread' or 'cash up'."


def _answer(conn: sqlite3.Connection, shop_id: str, sender: str, text: str) -> str:
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


POLISH_TIMEOUT_S = 60

LOGO_READY = "🖼️ Your logo is ready"
LOGO_FAILED = ("⚠️ I couldn't clean up your logo. Send a clearer photo"
               " with the caption logo any time.")


def _sniff_ext(image: bytes) -> str:
    img = image or b""
    if img[:2] == b"\xff\xd8":
        return "jpg"
    if img[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if img[:4] == b"RIFF" and img[8:12] == b"WEBP":
        return "webp"
    return "img"


def handle_photo(conn: sqlite3.Connection, shop_id: str, image: bytes) -> str | None:
    """Step-4 logo upload: save the original, polish in background, advance.

    Never blocks: any bytes are accepted, heavy work runs in a worker.
    None when setup doesn't want a photo.
    """
    from app import reports as _reports
    if setup_step(conn, shop_id) != 4:
        return None
    try:
        _reports.ensure_dirs()
        safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
        orig = str(_reports.LOGOS_DIR / f"{safe}-orig.{_sniff_ext(image)}")
        with open(orig, "wb") as f:
            f.write(image or b"")
        conn.execute("UPDATE shops SET logo_orig_path = ? WHERE shop_id = ?",
                     (orig, shop_id))
        conn.commit()
        request_polish(conn, shop_id)
    except Exception:
        log.warning("setup logo save failed", exc_info=True)
        return f"⚠️ Skipped the logo — let's keep going.\n\n{_advance(conn, shop_id, 4, None)}"
    ack = "✅ Logo received, I'll polish it in the background."
    return f"{ack}\n\n{_advance(conn, shop_id, 4, None)}"


def request_polish(conn: sqlite3.Connection, shop_id: str) -> None:
    """Flag a logo for background polishing; spawn a worker, thread fallback."""
    import threading
    import time
    _set_flag(conn, shop_id, "logo-polish", "pending")
    _set_flag(conn, shop_id, "logo-polish-started", str(int(time.time())))
    spawned = False
    if os.environ.get("SISI_SPAWN") == "1":
        try:
            import modal
            modal.Function.from_name("sisi", "polish_logo").spawn(shop_id)
            spawned = True
        except Exception:
            log.warning("polish spawn failed; using thread", exc_info=True)
    if not spawned:
        thread = threading.Thread(target=finish_polish, args=(shop_id,),
                                  daemon=True)
        thread.start()


def finish_polish(shop_id: str, timeout_s: int = POLISH_TIMEOUT_S) -> str:
    """Background worker body: cut out the logo or record failure. Own conn."""
    import concurrent.futures
    from app import logo as _logo
    from app import reports as _reports
    from app.db import connect, init_db
    from app.tools import ensure_shop
    try:
        conn = connect()
    except Exception:
        log.warning("logo polish: no database", exc_info=True)
        return "failed"
    try:
        init_db(conn)
        ensure_shop(conn, shop_id)
        orig = ensure_shop(conn, shop_id)["logo_orig_path"] or ""
        try:
            raw = open(orig, "rb").read() if orig else b""
        except OSError:
            raw = b""

        def _work():
            return _logo.process_upload(raw)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            try:
                done = pool.submit(_work).result(timeout=timeout_s)
            except Exception:
                done = None
        if not done or done["original"] is None:
            raise ValueError("bad image")
        _reports.ensure_dirs()
        safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
        if done["used_cutout"]:
            display = str(_reports.LOGOS_DIR / f"{safe}-cutout.png")
            with open(display, "wb") as f:
                f.write(done["cutout"])
        else:
            display = str(_reports.LOGOS_DIR / f"{safe}-badge.png")
            with open(display, "wb") as f:
                f.write(done["badge"])
        conn.execute("UPDATE shops SET logo_path = ? WHERE shop_id = ?",
                     (display, shop_id))
        conn.execute("INSERT INTO flags (shop_id, key, value)"
                     " VALUES (?, 'logo-mode', 'cutout')"
                     " ON CONFLICT (shop_id, key) DO UPDATE SET value = 'cutout'",
                     (shop_id,))
        outcome = "done"
    except Exception:
        log.warning("logo polish failed for %s", shop_id, exc_info=True)
        outcome = "failed"
    try:
        _set_flag(conn, shop_id, "logo-polish", outcome)
    finally:
        conn.close()
    return outcome


def polish_state(conn: sqlite3.Connection, shop_id: str) -> str:
    """done | failed | pending (stale pendings count as failed past timeout)."""
    import time
    state = _flag(conn, shop_id, "logo-polish")
    if state != "pending":
        return state or "none"
    try:
        started = int(_flag(conn, shop_id, "logo-polish-started") or "0")
    except ValueError:
        started = 0
    if started and time.time() - started > POLISH_TIMEOUT_S:
        _set_flag(conn, shop_id, "logo-polish", "failed")
        return "failed"
    return "pending"


def _finish(conn: sqlite3.Connection, shop_id: str) -> str:
    from app import reports as _reports
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
    _reports.ensure_dirs()
    out = (f"✅ *You're all set, {brand['name']}!*\n\nTry these:\n"
           "• 'sold 2 bread' — record a sale\n"
           "• 'Sipho took 1 coke' — credit sale\n"
           "• 'cash up' — today's totals")
    if _queue_header_card(conn, shop_id, "Your branded header"):
        out += "\n\n📎 Your header preview below."
    note = flush_logo(conn, shop_id)
    if note:
        out += f"\n\n{note}"
    return out


def _queue_header_card(conn: sqlite3.Connection, shop_id: str,
                       caption: str) -> bool:
    """Queue the branded header preview. False when no public URL is set."""
    from app import reports as _reports
    from app.agent import queue_media as _queue
    base = os.environ.get("SISI_PUBLIC_URL", "").rstrip("/")
    if not base:
        return False
    brand = _reports.shop_brand(conn, shop_id)
    _reports.ensure_dirs()
    safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
    card = str(_reports.REPORTS_DIR / f"{safe}-profile.png")
    _reports.render_shop_card(brand, card)
    token = _reports.store_card(conn, shop_id, "profile", card)
    _queue(shop_id, [{"kind": "image", "url": f"{base}/media/{token}?f=png",
                      "caption": caption, "filename": ""}])
    return True


def logo_result(conn: sqlite3.Connection,
                shop_id: str) -> tuple[str, list[dict]] | None:
    """Terminal polish outcome, once: (text, media) or None.

    Only after setup finished; marks logo-notified so it never repeats.
    """
    if setup_step(conn, shop_id) or not _flag(conn, shop_id, "setup-done"):
        return None
    if _flag(conn, shop_id, "logo-notified"):
        return None
    state = polish_state(conn, shop_id)
    if state == "pending":
        return None
    _set_flag(conn, shop_id, "logo-notified", "1")
    conn.execute("DELETE FROM flags WHERE shop_id = ? AND key = 'logo-polish'",
                 (shop_id,))
    conn.commit()
    if state != "done":
        return (LOGO_FAILED, []) if state == "failed" else None
    from app.agent import MEDIA_OUTBOX
    before = len(MEDIA_OUTBOX.get(shop_id, []))
    if _queue_header_card(conn, shop_id, "Your polished logo"):
        media = MEDIA_OUTBOX.get(shop_id, [])[before:]
        del MEDIA_OUTBOX[shop_id][before:]
        return f"{LOGO_READY} — preview below.", media
    return LOGO_READY, []


def flush_logo(conn: sqlite3.Connection, shop_id: str) -> str | None:
    """Deliver a finished logo polish after setup (queues the card)."""
    got = logo_result(conn, shop_id)
    if not got:
        return None
    text, media = got
    if media:
        from app.agent import queue_media as _queue
        _queue(shop_id, media)
    return text
