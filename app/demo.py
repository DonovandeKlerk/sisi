"""Join-code demo mode: try Sisi without the WHAPI allowlist.

A stranger who texts the join code (default SISI-DEMO, optional language
suffix like SISI-DEMO-ZU) gets their own isolated copy of the premade
demo shop, a short welcome, the overview card and the main menu. Real
allowlisted shops are untouched. Each demo number is capped at
SISI_DEMO_MSG_LIMIT real inbound messages (default 10); the join code
itself (and re-sending it) does not count. After the limit Sisi sends a
closing note and ignores further traffic (at most one close reminder per
day). Strangers with no code get a short tip once a day, with no LLM
call. A global daily join cap (SISI_DEMO_DAILY_CAP) stops runaway cost.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timezone

from app.db import SAST, now_iso, today_str
from app.i18n import CURRENT_LANG, LANGS, t as _t

log = logging.getLogger("sisi.demo")

DEFAULT_JOIN_CODE = "SISI-DEMO"
DEFAULT_MSG_LIMIT = 10
DEFAULT_DAILY_CAP = 50
SYSTEM_SHOP = "__demo__"

# Optional language suffix on the join code, e.g. SISI-DEMO-ZU.
_LANG_SUFFIX = {
    "EN": "en", "ENG": "en", "ENGLISH": "en",
    "ZU": "zu", "ZULU": "zu", "ISIZULU": "zu",
    "AF": "af", "AFR": "af", "AFRIKAANS": "af",
    "XH": "xh", "XHOSA": "xh", "ISIXHOSA": "xh",
    "ST": "st", "SOTHO": "st", "SESOTHO": "st",
    "TN": "tn", "TSWANA": "tn", "SETSWANA": "tn",
}


def join_code_base() -> str:
    raw = (os.environ.get("SISI_DEMO_JOIN_CODE", "") or DEFAULT_JOIN_CODE)
    return re.sub(r"[^A-Za-z0-9\-]", "", raw).strip("-").upper() or DEFAULT_JOIN_CODE


def msg_limit() -> int:
    try:
        return max(1, int(os.environ.get("SISI_DEMO_MSG_LIMIT", "")
                          or DEFAULT_MSG_LIMIT))
    except ValueError:
        return DEFAULT_MSG_LIMIT


def daily_cap() -> int:
    try:
        return max(0, int(os.environ.get("SISI_DEMO_DAILY_CAP", "")
                          or DEFAULT_DAILY_CAP))
    except ValueError:
        return DEFAULT_DAILY_CAP


def _code_re() -> re.Pattern[str]:
    base = re.escape(join_code_base())
    # Match base or base-LANG anywhere in the message (case-insensitive).
    return re.compile(rf"(?<![A-Za-z0-9]){base}(?:-([A-Za-z]+))?(?![A-Za-z0-9])",
                      re.IGNORECASE)


def extract_join(text: str | None) -> tuple[bool, str]:
    """Return (matched, lang_code). lang defaults to English."""
    if not text:
        return False, "en"
    m = _code_re().search(text)
    if not m:
        return False, "en"
    suffix = (m.group(1) or "").upper()
    lang = _LANG_SUFFIX.get(suffix, "en") if suffix else "en"
    if lang not in LANGS:
        lang = "en"
    return True, lang


def is_demo_shop(conn: sqlite3.Connection, shop_id: str) -> bool:
    try:
        row = conn.execute(
            "SELECT is_demo FROM shops WHERE shop_id = ?", (shop_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    if row is None:
        return False
    try:
        return int(row["is_demo"] or 0) == 1
    except (TypeError, ValueError, KeyError):
        return False


def demo_msgs_used(conn: sqlite3.Connection, shop_id: str) -> int:
    try:
        row = conn.execute(
            "SELECT demo_msgs FROM shops WHERE shop_id = ?", (shop_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None:
        return 0
    try:
        return int(row["demo_msgs"] or 0)
    except (TypeError, ValueError, KeyError):
        return 0


def _gate_get(conn: sqlite3.Connection, shop_id: str, key: str) -> str | None:
    try:
        row = conn.execute(
            "SELECT value FROM demo_gate WHERE shop_id = ? AND key = ?",
            (shop_id, key),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["value"] if row else None


def _gate_set(conn: sqlite3.Connection, shop_id: str, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO demo_gate (shop_id, key, value) VALUES (?, ?, ?)"
        " ON CONFLICT(shop_id, key) DO UPDATE SET value = excluded.value",
        (shop_id, key, value),
    )
    conn.commit()


def _sast_day() -> str:
    return datetime.now(timezone.utc).astimezone(SAST).strftime("%Y-%m-%d")


def joins_today(conn: sqlite3.Connection) -> int:
    raw = _gate_get(conn, SYSTEM_SHOP, f"joins-{_sast_day()}") or "0"
    try:
        return int(raw)
    except ValueError:
        return 0


def _bump_joins(conn: sqlite3.Connection) -> int:
    day = _sast_day()
    key = f"joins-{day}"
    n = joins_today(conn) + 1
    _gate_set(conn, SYSTEM_SHOP, key, str(n))
    return n


def cap_reached(conn: sqlite3.Connection) -> bool:
    cap = daily_cap()
    if cap <= 0:
        return True  # 0 = demos disabled
    return joins_today(conn) >= cap


def tip_already_sent(conn: sqlite3.Connection, shop_id: str) -> bool:
    return _gate_get(conn, shop_id, "tip-day") == _sast_day()


def mark_tip_sent(conn: sqlite3.Connection, shop_id: str) -> None:
    _gate_set(conn, shop_id, "tip-day", _sast_day())


def exhausted_notice_sent_today(conn: sqlite3.Connection, shop_id: str) -> bool:
    return _gate_get(conn, shop_id, "exhausted-day") == _sast_day()


def mark_exhausted_notice(conn: sqlite3.Connection, shop_id: str) -> None:
    _gate_set(conn, shop_id, "exhausted-day", _sast_day())


def tip_text() -> str:
    """How-to-try tip. English only — we don't know the stranger's language.
    No LLM. Includes the live join code so env overrides stay accurate.
    """
    code = join_code_base()
    return (
        f"Sawubona! To try *Sisi* (a WhatsApp shop assistant), reply with "
        f"*{code}*.\n\n"
        f"You'll get a sample spaza shop for {msg_limit()} messages — "
        f"no app, no login.\n\n"
        f"Full access: message Donovan or visit the Sisi page on Version One."
    )


def exhausted_text(lang: str = "en") -> str:
    token = CURRENT_LANG.set(lang if lang in LANGS else "en")
    try:
        return _t("demo_exhausted", n=msg_limit(), code=join_code_base())
    finally:
        CURRENT_LANG.reset(token)


def welcome_text(lang: str = "en") -> str:
    token = CURRENT_LANG.set(lang if lang in LANGS else "en")
    try:
        return _t("demo_welcome", n=msg_limit(), code=join_code_base())
    finally:
        CURRENT_LANG.reset(token)


def cap_full_text() -> str:
    return (
        "Sisi's free demo is full for today. Please try again tomorrow, "
        "or message Donovan for full access."
    )


def _ensure_demo_logo(conn: sqlite3.Connection, shop_id: str) -> None:
    """Write a simple branded tile so the overview card isn't an initials badge."""
    try:
        from pathlib import Path
        from PIL import Image, ImageDraw, ImageFont
        from app import reports
        reports.ensure_dirs()
        safe = "".join(c for c in shop_id if c.isdigit()) or "shop"
        path = reports.LOGOS_DIR / f"{safe}-cutout.png"
        if path.exists():
            conn.execute(
                "UPDATE shops SET logo_path = ? WHERE shop_id = ? AND "
                "(logo_path IS NULL OR logo_path = '')",
                (str(path), shop_id),
            )
            conn.commit()
            return
        img = Image.new("RGBA", (512, 512), (16, 120, 80, 255))
        draw = ImageDraw.Draw(img)
        draw.ellipse((48, 48, 464, 464), fill=(255, 255, 255, 255))
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 96)
        except OSError:
            font = ImageFont.load_default()
        label = "MZ"
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((512 - tw) / 2, (512 - th) / 2 - 8), label,
                  fill=(16, 120, 80, 255), font=font)
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path)
        conn.execute(
            "UPDATE shops SET logo_path = ? WHERE shop_id = ?",
            (str(path), shop_id),
        )
        conn.commit()
    except Exception:
        log.warning("demo logo failed", exc_info=True)


def provision_demo(conn: sqlite3.Connection, shop_id: str, lang: str = "en") -> None:
    """Wipe+seed an isolated demo shop for this number and mark setup done."""
    from seed.seed_demo import add_demo_activity, seed_demo
    from app.setup import skip_setup

    lang = lang if lang in LANGS else "en"
    seed_demo(shop_id, conn, language=lang)
    add_demo_activity(conn, shop_id)
    # seed_demo inserts the shop row; stamp demo columns + skip setup.
    conn.execute(
        "UPDATE shops SET is_demo = 1, demo_msgs = 0, demo_joined_at = ?, "
        "language = ? WHERE shop_id = ?",
        (now_iso(), lang, shop_id),
    )
    conn.commit()
    skip_setup(conn, shop_id)
    _ensure_demo_logo(conn, shop_id)
    _bump_joins(conn)
    log.info("demo provisioned shop=%s lang=%s", shop_id, lang)


def increment_demo_msg(conn: sqlite3.Connection, shop_id: str) -> int:
    """Bump the inbound counter; return the new total."""
    conn.execute(
        "UPDATE shops SET demo_msgs = COALESCE(demo_msgs, 0) + 1 "
        "WHERE shop_id = ?",
        (shop_id,),
    )
    conn.commit()
    return demo_msgs_used(conn, shop_id)


def start_demo_reply(conn: sqlite3.Connection, shop_id: str, lang: str) -> str:
    """Welcome text; marks overview+menu to send after the text reply."""
    from app.agent import mark_overview_pending

    CURRENT_LANG.set(lang if lang in LANGS else "en")
    mark_overview_pending(shop_id)
    return welcome_text(lang)




def try_nudge(lang: str = "en") -> tuple[str, list[str]]:
    """Short post-welcome tip + up to 3 example actions (buttons).

    Outbound only — never increments demo_msgs. Examples use MaZulu's
    Spaza seed names/items so taps work on a fresh demo shop.
    """
    lang = lang if lang in LANGS else "en"
    token = CURRENT_LANG.set(lang)
    try:
        body = _t("demo_try_nudge")
        examples = [
            _t("demo_try_sold"),
            _t("demo_try_owes"),
            _t("demo_try_statement"),
        ]
        return body, examples
    finally:
        CURRENT_LANG.reset(token)


def known_demo_sender(conn: sqlite3.Connection, shop_id: str) -> bool:
    return is_demo_shop(conn, shop_id)
