"""Owner reminders: natural-language scheduling, context attach, due sending.

Times are Africa/Johannesburg (SAST, fixed +02:00 — no daylight saving).
`due_at` is stored as ISO with the fixed offset so string comparison works.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from app.i18n import CURRENT_LANG, DAY_ON, RECURRENCE, due_short, due_stamp, t

log = logging.getLogger("sisi.reminders")

SAST = timezone(timedelta(hours=2))
DELAYED_AFTER = timedelta(minutes=15)

WINDOW_HOURS = 24
"""Freeform WhatsApp only inside this window after the last inbound message."""

WINDOW_EXEMPT_CHANNELS = ("vonage", "whapi")
"""Channels without an enforced 24h window (sandbox sender, no templates).

Whapi is a linked phone, not a Cloud API number: it sends as a normal
WhatsApp client with no template mechanism, so window-gating would only
block with no recovery path.
"""

WINDOW_GATED_CHANNELS = ("meta", "infobip")
"""Channels where outside-window sends need an approved template."""

STUCK_CLAIM_MINUTES = 10
"""A 'sending' claim older than this is a crashed tick: safe to refire."""

REMIND_TRIGGERS = ("remind me", "remind us", "ngikhumbuze", "ungikhumbuze",
                   "ungakhumbuli", "herinner my", "herinner", "ndikhumbuze",
                   "khumbuza", "remind")
"""'remind' (bare) catches 'send me a reminder …', 'reminds', 'reminded'."""
SHOP_TRIP_WORDS = ("shop", "spaza", "wholesaler", "makro", "jumbo", "boxer",
                   "shoprite", "stock", "order", "supplier", "town",
                   "winkel", "venkile", "ivenkile", "dorp", "toropo")
TRIP_DEFAULT_HM = (5, 30)
DEFAULT_HM = (7, 0)

WEEKDAYS = {
    "monday": 0, "mon": 0, "umsombuluko": 0, "maandag": 0, "umvulo": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "ulwesibili": 1, "dinsdag": 1,
    "ulwesibini": 1,
    "wednesday": 2, "wed": 2, "ulwesithathu": 2, "woensdag": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "ulwesine": 3,
    "donderdag": 3,
    "friday": 4, "fri": 4, "ulwesihlanu": 4, "vrydag": 4,
    "saturday": 5, "sat": 5, "umgqibelo": 5, "saterdag": 5,
    "sunday": 6, "sun": 6, "isonto": 6, "sondag": 6, "icawa": 6,
}
INTENT_WORDS = ("need to", "needs to", "must", "have to", "has to", "going to",
                "will", "should", "kumele", "ngidinga", "dinga", "funeka",
                "mele", "moet", "behoort", "gaan", "wil", "kufuneka",
                "ndifuna")

_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2})|\s*h\s*(\d{2}))?\s*(am|pm)?\b")
_BARE_TIME_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$")


def now_sast(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).astimezone(SAST)
    if now.tzinfo is None:
        return now.replace(tzinfo=SAST)
    return now.astimezone(SAST)


def _parse_time(text: str) -> tuple[int, int] | None:
    """First clock time in the text. Bare numbers are NOT times."""
    for m in _TIME_RE.finditer(text or ""):
        hour = int(m.group(1))
        minute = int(m.group(2) or m.group(3) or 0)
        meridiem = m.group(4)
        if m.group(2) is None and m.group(3) is None and meridiem is None:
            continue  # bare number, e.g. a quantity
        if hour > 23 or minute > 59:
            continue
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        if meridiem is None and hour > 23:
            continue
        return hour, minute
    return None


_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
}
"""Spoken counts in relative times ('in about five minutes')."""

_NUM_ALT = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten" \
    r"|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen" \
    r"|eighteen|nineteen|twenty|thirty|forty|fifty)"
_HEDGE = r"(?:about|around|roughly|approximately|more\s+or\s+less)\s+"

_RELATIVE_RES = (
    re.compile(r"\bin\s+(?:%s)?(%s)\s*(?:minutes?|mins?)\b" % (_HEDGE, _NUM_ALT)),
    re.compile(r"\bin\s+(?:%s)?(%s)\s*(?:hours?|hrs?|h)\b" % (_HEDGE, _NUM_ALT)),
    re.compile(r"\bin\s+(?:%s)?(?:an?\s+)?hour\b" % _HEDGE),
    re.compile(r"\bin\s+(?:%s)?(?:a\s+)?half\s*(?:an?\s+)?hour\b" % _HEDGE),
    re.compile(r"\bin\s+(?:%s)?a\s+minute\b" % _HEDGE),
)


def _num_value(token: str) -> int:
    token = (token or "").strip().lower()
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token, 0)


def parse_relative(text: str, now: datetime) -> datetime | None:
    """'in 5 minutes' / 'in about five minutes' from the message time."""
    low = f" {_clean_fillers(text).lower()} ".replace(",", " ")
    m = _RELATIVE_RES[0].search(low)
    if m:
        return now + timedelta(minutes=_num_value(m.group(1)))
    m = _RELATIVE_RES[1].search(low)
    if m:
        return now + timedelta(hours=_num_value(m.group(1)))
    if _RELATIVE_RES[2].search(low):
        return now + timedelta(hours=1)
    if _RELATIVE_RES[3].search(low):
        return now + timedelta(minutes=30)
    if _RELATIVE_RES[4].search(low):
        return now + timedelta(minutes=1)
    return None


def reminder_clause(text: str) -> str:
    """The reminder clause: from the last boundary before the trigger.

    'Milk arrives in fifteen minutes and can you remind me in five'
    -> 'can you remind me in five', so the delivery ETA never leaks
    into the reminder's own time. Everything after the trigger stays
    ('remind me to buy bread and milk' is kept whole).
    """
    low = f" {(text or '')} "
    first = -1
    for trig in REMIND_TRIGGERS:
        idx = low.lower().find(trig)
        if idx != -1 and (first == -1 or idx < first):
            first = idx
    if first == -1:
        return (text or "").strip()
    head = low[:first]
    cuts = [m.end() for m in
            re.finditer(r",|;|&|\band\b|\bplus\b|\bthen\b|\bso\b",
                        head, flags=re.IGNORECASE)]
    cut = max(cuts) if cuts else 0
    return low[cut:].strip(" ,;:&")


def has_explicit_time(text: str) -> bool:
    """True when the text names its own time (relative, clock, day)."""
    now = now_sast(None)
    if parse_relative(text, now) is not None:
        return True
    low = f" {(text or '').lower()} "
    if _parse_time(low) is not None:
        return True
    return _parse_day(low, now)[1] in ("weekday", "today", "tomorrow")


def parse_bare_time(text: str) -> tuple[int, int] | None:
    """A reply that is only a time ('6am', '17:30'). None otherwise."""
    m = _BARE_TIME_RE.match(text or "")
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    meridiem = m.group(3)
    if hour > 23 or minute > 59:
        return None
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    return hour, minute


def _strip_prefix_day(word: str) -> str:
    return re.sub(r"^(ngo|nga|e)", "", word)


def _parse_day(text: str, now: datetime) -> tuple[str, str]:
    """Returns (iso_date, source). Source: weekday|today|tomorrow|default."""
    low = f" {(text or '').lower()} "
    if "today" in low or "namhlanje" in low or "namhla" in low \
            or "vandag" in low:
        return now.date().isoformat(), "today"
    if "tomorrow" in low or "kusasa" in low or "ngomso" in low \
            or re.search(r"\bm\u00f4?re\b", low):
        return (now.date() + timedelta(days=1)).isoformat(), "tomorrow"
    for token in re.findall(r"[a-z]+", low):
        day = WEEKDAYS.get(token) or WEEKDAYS.get(_strip_prefix_day(token))
        if day is None:
            continue
        ahead = (day - now.weekday()) % 7
        return (now.date() + timedelta(days=ahead)).isoformat(), "weekday"
    return now.date().isoformat(), "default"


def _parse_recurrence(text: str) -> str:
    low = f" {(text or '').lower()} "
    if "every month" in low or "monthly" in low \
            or re.search(r"\belke maand\b", low) or "maandeliks" in low:
        return "monthly"
    if re.search(r"\belke week\b", low) or "weekliks" in low:
        return "weekly"
    if "every week" in low or "weekly" in low or "every day" in low \
            or re.search(r"\bdaily\b", low) or "nsuku zonke" in low \
            or re.search(r"\belke dag\b", low) or "daagliks" in low:
        if "every monday" in low or "every tuesday" in low \
                or "every wednesday" in low or "every thursday" in low \
                or "every friday" in low or "every saturday" in low \
                or "every sunday" in low:
            return "weekly"
        if re.search(r"\belke\s+(maandag|dinsdag|woensdag|donderdag|vrydag"
                     r"|saterdag|sondag)\b", low):
            return "weekly"
        return "daily"
    if re.search(r"\bevery\s+[a-z]+\b", low):
        return "weekly"
    if re.search(r"\belke\s+[a-z]+\b", low):
        return "weekly"
    return "none"


def _default_hm(text: str) -> tuple[int, int]:
    low = (text or "").lower()
    if any(w in low for w in SHOP_TRIP_WORDS):
        return TRIP_DEFAULT_HM
    return DEFAULT_HM


_FILLER_RE = re.compile(r"\b(um+|uh+|er+|ah+)\b[,\s]*")


def _clean_fillers(text: str) -> str:
    """Drop spoken hesitations ('um, remind me in, uh, 5 minutes')."""
    return _FILLER_RE.sub(" ", text or "")


def _strip_request_bits(text: str) -> str:
    low = _clean_fillers(text).replace(",", " ")
    low = re.sub(r"\breminders?\b", " ", low)
    for trig in REMIND_TRIGGERS:
        low = re.sub(rf"\b{re.escape(trig)}\b", " ", low)
    low = re.sub(r"\bin\s+(?:%s)?(?:%s)\s*(?:minutes?|mins?|hours?|hrs?|h)\b"
                 % (_HEDGE, _NUM_ALT), " ", low)
    low = re.sub(r"\bin\s+(?:%s)?(?:an?\s+)?hour\b" % _HEDGE, " ", low)
    low = re.sub(r"\bin\s+(?:%s)?(?:a\s+)?half\s*(?:an?\s+)?hour\b" % _HEDGE,
                 " ", low)
    low = re.sub(r"\bin\s+(?:%s)?a\s+minute\b" % _HEDGE, " ", low)
    low = re.sub(r"\b(please|kindly|also|just)\b", " ", low)
    low = re.sub(r"\b(send|give)\s+me\b", " ", low)
    low = re.sub(r"\b(can|could|will|would)\s+you\b", " ", low)
    low = re.sub(r"\b(\w+)\s+\1\b", r"\1", low, flags=re.IGNORECASE)
    for _ in range(3):
        new = re.sub(r"^(and|plus|also|so|then|well)\b[\s,]*", " ", low).strip()
        if new == low:
            break
        low = new
    low = _TIME_RE.sub(" ", low)
    low = re.sub(r"\b(today|tomorrow|namhlanje|namhla|kusasa|vandag|more"
                 r"|m\u00f4re|ngomso)\b", " ", low)
    for name in sorted(WEEKDAYS, key=len, reverse=True):
        low = re.sub(rf"\b(ngo|nga|e)?{re.escape(name)}\b", " ", low)
    low = re.sub(r"\b(every|daily|weekly|monthly|nsuku zonke|elke|daagliks"
                 r"|weekliks|maandeliks|maand|dag|week)\b", " ", low)
    low = re.sub(r"\s+", " ", low).strip(" .!?,;:")
    low = re.sub(r"^(to|that|om|ukuthi|ukuba|ukuze)\b\s*", "", low).strip()
    low = re.sub(r"^(a|an|the)(\s+|$)", "", low).strip()
    return low


PRONOUNS = frozenset(
    "i me my mine we us our ours you your yours he him his she her hers "
    "it its they them their theirs mina wena yena thina nina bona "
    "ek my ons julle jy jou hy hom sy haar hulle hul mna".split())
"""Words that are never a customer name or a reminder topic on their own."""

_BARE_SUMMARIES = frozenset(
    {"reminder", "reminders", "isikhumbuzi", "herinnering", "isikhumbuzo"})


_THIRD_PERSON = frozenset(
    "he him his she her hers it its they them their theirs "
    "yena bona hy hom sy haar hulle hul".split())


def reminder_spec_ok(text: str, parsed: dict) -> bool:
    """A reminder bit is real when it names a time or a real topic.

    'remind them (Friday)' needs the composite/LLM path to resolve
    who 'them' is; 'remind me Thursday' and 'remind me in 5 minutes'
    parse standalone.
    """
    toks = re.findall(r"[a-z]+", (parsed.get("summary") or "").lower())
    meaningful = any(t not in PRONOUNS and t not in _BARE_SUMMARIES
                     and len(t) > 2 for t in toks)
    if has_explicit_time(text):
        if meaningful or not any(t in _THIRD_PERSON for t in toks):
            return True
        return False
    return meaningful


_FILLER_TOKENS = frozenset(
    "and plus also so then well can could will would shall should may "
    "might must do does did done is are was were be been being have has "
    "had to for of off on a an the please kindly just um uh er ah hey "
    "hi hello there here it there".split())


def summary_is_bare(summary: str) -> bool:
    """True when a summary says nothing ('Reminder', 'and can you a')."""
    toks = re.findall(r"[a-z]+", (summary or "").lower())
    if not toks:
        return True
    return all(t in PRONOUNS or t in _BARE_SUMMARIES
               or t in _FILLER_TOKENS or len(t) <= 2 for t in toks)


_ARRIVE_RE = re.compile(r"\barriv\w*\b|\bcoming\b|\bon\s+(?:its|the)\s+way\b",
                        re.IGNORECASE)
_DELIVERY_ITEM_RES = (
    re.compile(r"([a-z][a-z\- ]{0,24}?)\s+arriv(?:e?s|ing)\b", re.IGNORECASE),
    re.compile(r"(?:ordered?|bringing|delivering|fetching)\s+"
               r"(?:some\s+|the\s+)?([a-z][a-z\- ]{0,24}?)"
               r"(?:,|\.|$|\s+is\b|\s+are\b)", re.IGNORECASE),
    re.compile(r"([a-z][a-z\- ]{0,24}?)\s+delivery\s+(?:is\s+)?"
               r"(?:coming|arriving|on\b)", re.IGNORECASE),
)
_DELIVERY_ARTICLES = frozenset(
    {"the", "a", "an", "some", "my", "our", "this", "that",
     "is", "are", "was", "be", "been", "it"})


def _delivery_item(text: str) -> str | None:
    """The item in supplier-arrival context ('milk'), else None.

    Only fires on arrival language plus a concrete item; pronouns and
    bare stopwords never become a note.
    """
    low = f" {(text or '').lower()} "
    if _ARRIVE_RE.search(low) is None:
        return None
    for rx in _DELIVERY_ITEM_RES:
        m = rx.search(text or "")
        if not m:
            continue
        toks = [w for w in re.findall(r"[a-z]+", m.group(1).lower())
               if w not in _DELIVERY_ARTICLES]
        if not toks or len(toks) > 3:
            continue
        if any(w in PRONOUNS or len(w) < 2 for w in toks):
            continue
        return " ".join(toks)
    return None


def delivery_note(text: str) -> str | None:
    """'milk delivery' from supplier-arrival context, else None."""
    item = _delivery_item(text)
    return f"{item} delivery" if item else None


def _content_tokens(summary: str) -> list[str]:
    return [t for t in re.findall(r"[a-z]+", (summary or "").lower())
            if t not in PRONOUNS and t not in _BARE_SUMMARIES
            and t not in _FILLER_TOKENS and len(t) > 2]


def resolve_topic(summary: str, text: str) -> str | None:
    """A meaningful reminder topic, or None when the owner must say more.

    Conversational filler ('and can you a') resolves to the message's
    delivery context ('milk delivery') where one is clearly supported;
    a lone preparation verb merges with it ('Prepare for the milk
    delivery'). Anything else contentless returns None: ask, don't save.
    """
    if not summary_is_bare(summary):
        toks = _content_tokens(summary)
        item = _delivery_item(text)
        if item is not None and toks and all(
                t in ("get", "ready", "prepare") for t in toks):
            verb = "Get ready" if "get" in toks or "ready" in toks \
                else "Prepare"
            return f"{verb} for the {item} delivery"
        return summary
    return delivery_note(text)


def parse_request(text: str, now: datetime | None = None):
    """Parse 'remind me …'. Returns {due, recurrence, summary} or None."""
    now = now_sast(now)
    low = f" {_clean_fillers(text).lower()} "
    if not any(t in low for t in REMIND_TRIGGERS):
        return None
    if re.search(r"\bdraft\w*\b", low):
        return None  # 'draft a reminder' is a message, not a schedule
    recurrence = _parse_recurrence(low)
    relative = parse_relative(low, now)
    if relative is not None:
        due = relative
    else:
        day, source = _parse_day(low, now)
        hm = _parse_time(low) or _default_hm(low)
        due = datetime.combine(datetime.fromisoformat(day).date(),
                               datetime.min.time().replace(hour=hm[0],
                                                           minute=hm[1]),
                               tzinfo=SAST)
        if due <= now:
            due += timedelta(days=7 if source == "weekday" else 1)
    summary = _strip_request_bits(low).strip() or t("rem_default_summary")
    if len(summary) > 120:
        summary = summary[:117].rstrip() + "…"
    return {"due": due, "recurrence": recurrence, "summary": summary}


def offer_scan(text: str, now: datetime | None = None):
    """Future weekday + intent but no time and no reminder command.

    Returns {due, summary} for the 'Want a reminder …?' offer, else None.
    """
    now = now_sast(now)
    low = f" {(text or '').lower()} "
    if "?" in low:
        return None
    if any(t in low for t in REMIND_TRIGGERS):
        return None
    if _parse_time(low):
        return None
    day, source = _parse_day(low, now)
    if source not in ("weekday", "tomorrow"):
        return None
    if not any(w in low for w in INTENT_WORDS):
        return None
    hm = _default_hm(low)
    due = datetime.combine(datetime.fromisoformat(day).date(),
                           datetime.min.time().replace(hour=hm[0],
                                                       minute=hm[1]),
                           tzinfo=SAST)
    if due <= now:
        due += timedelta(days=7)
    summary = re.sub(r"\s+", " ", (text or "").strip())
    summary = re.sub(rf"\s+(on|op)\s+[a-z]+\.?$", "", summary,
                     flags=re.IGNORECASE)
    if len(summary) > 120:
        summary = summary[:117].rstrip() + "…"
    return {"due": due, "summary": summary or t("rem_default_summary")}


def offer_preview(due: datetime) -> str:
    lang = CURRENT_LANG.get()
    day = DAY_ON.get(lang, DAY_ON["en"])[due.weekday()]
    return t("rem_offer", day=day, hm=due.strftime("%H:%M"))


def _name_hit(word: str, names: list[str]) -> str | None:
    for name in sorted(names, key=len, reverse=True):
        if not name.strip():
            continue
        if re.search(rf"\b(?:sika|ka|owa|wa)?{re.escape(name.strip().lower())}\b",
                     word):
            return name
    return None


def detect_context(conn: sqlite3.Connection, shop_id: str, text: str) -> dict:
    """Attach order/statement/customer/receipt/book context. {} when none."""
    from app.tools import get_order_draft
    low = f" {(text or '').lower()} "
    if ("this order" in low or "the order" in low or "my order" in low
            or "bestelling" in low or "odolo" in low) \
            and get_order_draft(conn, shop_id) is not None:
        return {"kind": "order"}
    names = [r["name"] for r in conn.execute(
        "SELECT name FROM customers WHERE shop_id = ?", (shop_id,)).fetchall()]
    hit = _name_hit(low, names)
    if hit is not None:
        if "statement" in low or "sitatimende" in low or "staat" in low \
                or "strokie" in low or "istatimenti" in low:
            return {"kind": "statement", "name": hit}
        return {"kind": "customer", "name": hit}
    if "receipt" in low or "slip" in low or "risithi" in low \
            or "strokie" in low or "isiliphu" in low:
        row = conn.execute("SELECT id FROM receipts WHERE shop_id = ?"
                           " AND reversed = 0 ORDER BY id DESC LIMIT 1",
                           (shop_id,)).fetchone()
        if row is not None:
            return {"kind": "receipt", "id": row["id"]}
    if "book" in low or "ibhuku" in low or "boek" in low \
            or "incwadi" in low:
        return {"kind": "book"}
    return {}


def _fmt_due(due: datetime) -> str:
    return due_short(due.weekday(), f"{due.day:02d}", due.month,
                     due.strftime("%H:%M"))


def confirm_text(due: datetime, summary: str, recurrence: str) -> str:
    out = t("rem_set", due=_fmt_due(due), summary=summary)
    if recurrence != "none":
        lang = CURRENT_LANG.get()
        rec = RECURRENCE.get(lang, RECURRENCE["en"]).get(recurrence,
                                                         recurrence)
        out += t("rem_repeats", rec=rec)
    return out


def save_reminder(conn: sqlite3.Connection, shop_id: str, due: datetime,
                  summary: str, context: dict | None = None,
                  recurrence: str = "none",
                  record_action: bool = True) -> int:
    """Save a reminder (+ undo row unless the caller records it)."""
    from app.tools import _action_id, ensure_shop
    ensure_shop(conn, shop_id)
    cur = conn.execute(
        "INSERT INTO reminders (shop_id, due_at, summary, context_json, recurrence)"
        " VALUES (?, ?, ?, ?, ?)",
        (shop_id, due.isoformat(), summary or t("rem_default_summary"),
         json.dumps(context or {}), recurrence),
    )
    rid = cur.lastrowid
    if record_action:
        conn.execute(
            "INSERT INTO actions (action_id, shop_id, kind, summary, data_json)"
            " VALUES (?, ?, 'reminder', ?, ?)",
            (_action_id(), shop_id, f"reminder: {summary or 'Reminder'}",
             json.dumps({"reminder_id": rid})),
        )
    conn.commit()
    return rid


def numbered(conn: sqlite3.Connection, shop_id: str) -> list:
    return conn.execute("SELECT * FROM reminders WHERE shop_id = ?"
                        " AND status IN ('pending', 'blocked', 'failed')"
                        " ORDER BY due_at, id", (shop_id,)).fetchall()


def list_reminders(conn: sqlite3.Connection, shop_id: str) -> str:
    rows = numbered(conn, shop_id)
    if not rows:
        return t("rem_list_empty")
    lines = [t("rem_list_head")]
    lang = CURRENT_LANG.get()
    for n, r in enumerate(rows, 1):
        due = datetime.fromisoformat(r["due_at"])
        line = t("rem_list_line", n=n, due=_fmt_due(due),
                 summary=r["summary"])
        if r["recurrence"] != "none":
            rec = RECURRENCE.get(lang, RECURRENCE["en"]).get(r["recurrence"],
                                                             r["recurrence"])
            line += t("rem_list_rec", rec=rec)
        if r["status"] == "blocked":
            line += " " + t("rem_st_blocked",
                            reason=r["blocked_reason"] or t("rem_blocked_window"))
        elif r["status"] == "failed":
            line += " " + t("rem_st_failed",
                            detail=r["dlr_detail"] or "?")
        lines.append(line)
    return "\n".join(lines)


def cancel_numbered(conn: sqlite3.Connection, shop_id: str, n: int) -> str:
    rows = numbered(conn, shop_id)
    if n < 1 or n > len(rows):
        return t("rem_no_number")
    conn.execute("UPDATE reminders SET status = 'cancelled' WHERE id = ?",
                 (rows[n - 1]["id"],))
    conn.commit()
    return t("rem_cancelled", n=n)


def move_numbered(conn: sqlite3.Connection, shop_id: str, n: int,
                  time_text: str, now: datetime | None = None) -> str:
    rows = numbered(conn, shop_id)
    if n < 1 or n > len(rows):
        return t("rem_no_number")
    hm = parse_bare_time(time_text)
    if hm is None:
        return t("rem_move_hint")
    now = now_sast(now)
    due = datetime.fromisoformat(rows[n - 1]["due_at"])
    if due.tzinfo is None:
        due = due.replace(tzinfo=SAST)
    due = due.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if due <= now:
        due += timedelta(days=1)
    conn.execute("UPDATE reminders SET due_at = ? WHERE id = ?",
                 (due.isoformat(), rows[n - 1]["id"]))
    conn.commit()
    return t("rem_moved", n=n, due=_fmt_due(due))


def snooze_last(conn: sqlite3.Connection, shop_id: str, minutes: int,
                now: datetime | None = None) -> str:
    now = now_sast(now)
    row = conn.execute("SELECT value FROM flags WHERE shop_id = ?"
                       " AND key = 'last-fired-reminder'", (shop_id,)).fetchone()
    if row is None:
        return t("rem_no_snooze")
    try:
        rid = int(row["value"])
    except ValueError:
        return t("rem_no_snooze")
    due = now + timedelta(minutes=max(1, minutes))
    cur = conn.execute("UPDATE reminders SET due_at = ?, status = 'pending'"
                       " WHERE id = ? AND shop_id = ?",
                       (due.isoformat(), rid, shop_id))
    conn.commit()
    if cur.rowcount == 0:
        return t("rem_no_snooze")
    return t("rem_snoozed", hm=due.strftime("%H:%M"))


def parse_snooze_minutes(text: str) -> int | None:
    """Minutes from 'snooze 30 min' / 'snooze 2 hours' / 'snooze'. None if no."""
    low = " ".join((text or "").strip().lower().split())
    if low == "snooze":
        return 30
    m = re.match(r"snooze (\d+)\s*(min|mins|minute|minutes|hour|hours|hr|hrs)$",
                 low)
    if not m:
        return None
    minutes = int(m.group(1)) * (60 if m.group(2).startswith(("hour", "hr")) else 1)
    return minutes


def _add_month(due: datetime) -> datetime:
    month = due.month + 1
    year = due.year + (1 if month > 12 else 0)
    month = month if month <= 12 else 1
    day = min(due.day, [31, 29 if year % 4 == 0 and
                        (year % 100 != 0 or year % 400 == 0) else 28,
                        31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return due.replace(year=year, month=month, day=day)


def advance(due: datetime, recurrence: str) -> datetime:
    if recurrence == "daily":
        return due + timedelta(days=1)
    if recurrence == "weekly":
        return due + timedelta(days=7)
    if recurrence == "monthly":
        return _add_month(due)
    return due


def render_context(conn: sqlite3.Connection, shop_id: str,
                   context: dict) -> tuple[str, list[dict]]:
    """Full send-time content for a context. Returns (text, media)."""
    from app.tools import account_balance, balance_cents, build_order, cash_up
    from app.tools import get_order_draft
    kind = (context or {}).get("kind", "")
    if kind == "customer_order":
        from app.orders import render_saved_order
        return render_saved_order(conn, shop_id, context.get("plan_id", ""),
                                  context.get("label", "")), []
    if kind == "order":
        text = build_order(conn, shop_id)
        draft = get_order_draft(conn, shop_id)
        if draft is not None:
            known = [ln["est_cents"] for ln in draft["lines"]
                     if ln["est_cents"] is not None]
            need = sum(known)
            have = account_balance(conn, shop_id, "cash")
            from app.db import cents_to_rand
            text += t("rem_cash_vs_need", have=cents_to_rand(have),
                      need=cents_to_rand(need))
        return text, []
    if kind == "customer":
        name = context.get("name", "")
        cust = conn.execute("SELECT id FROM customers WHERE shop_id = ?"
                            " AND name = ?", (shop_id, name)).fetchone()
        if cust is None:
            return t("rem_cust_gone", name=name), []
        from app.db import cents_to_rand
        owed = balance_cents(conn, shop_id, cust["id"])
        lines = [t("rem_cust_owes", name=name, owed=cents_to_rand(owed))]
        items = conn.execute(
            "SELECT amount_cents, items_text, substr(created_at, 1, 10) d"
            " FROM credit_entries WHERE shop_id = ? AND customer_id = ?"
            " AND reversed = 0 ORDER BY id DESC LIMIT 5",
            (shop_id, cust["id"])).fetchall()
        for it in items:
            head = f"{it['d']} {it['items_text'] or ''}".rstrip()
            lines.append(t("rem_cust_item", head=head,
                           amt=cents_to_rand(it["amount_cents"])))
        lines.append(t("rem_cust_hint", name=name))
        return "\n".join(lines), []
    if kind == "statement":
        from app.agent import take_media
        from app.tools import customer_statement
        text = customer_statement(conn, shop_id, context.get("name", ""))
        return text, take_media(shop_id)
    if kind == "receipt":
        row = conn.execute("SELECT supplier, date, total_cents FROM receipts"
                           " WHERE id = ? AND shop_id = ?", (context.get("id"),
                                                             shop_id)).fetchone()
        if row is None:
            return t("rem_receipt_gone"), []
        from app.db import cents_to_rand
        total = cents_to_rand(row["total_cents"]) if row["total_cents"] \
            is not None else "?"
        head = f"{t('rem_receipt_word')} {row['supplier'] or ''} {row['date'] or ''}"
        return t("rem_receipt_line", head=head.rstrip(), total=total), []
    if kind == "book":
        from app.db import today_str
        return cash_up(conn, shop_id, today_str()), []
    return "", []


def fire_text(summary: str, due: datetime, delayed: bool) -> str:
    when = due_stamp(due.weekday(), f"{due.day:02d}", due.month,
                     due.strftime("%H:%M"))
    head = t("rem_fire", when=when)
    if delayed:
        head += t("rem_delayed")
    return f"{head}\n{summary}"


def due_today(conn: sqlite3.Connection, shop_id: str, day: str) -> list:
    return conn.execute("SELECT * FROM reminders WHERE shop_id = ?"
                        " AND status = 'pending' AND substr(due_at, 1, 10) = ?"
                        " ORDER BY due_at, id", (shop_id, day)).fetchall()


def _parse_seen_ts(value: str):
    """Parse a messages_seen timestamp (ISO or legacy minute format)."""
    v = (value or "").strip()
    if not v:
        return None
    m = re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:(\d{2})(.*)$", v)
    if m and m.group(2)[:1].isdigit():
        try:
            return datetime.strptime(v, "%Y-%m-%dT%H:%M:%fZ").replace(
                tzinfo=timezone.utc)
        except ValueError:
            return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def last_inbound_at(conn: sqlite3.Connection, shop_id: str):
    """Newest inbound message time for a shop, or None when never."""
    row = conn.execute("SELECT MAX(created_at) m FROM messages_seen"
                       " WHERE shop_id = ?", (shop_id,)).fetchone()
    if row is None or not row["m"]:
        return None
    return _parse_seen_ts(row["m"])


def window_open(conn: sqlite3.Connection, shop_id: str, channel: str,
                now: datetime | None = None) -> bool:
    """True when a freeform message may go out on this channel now."""
    if (channel or "").strip().lower() in WINDOW_EXEMPT_CHANNELS:
        return True
    now = now_sast(now)
    last = last_inbound_at(conn, shop_id)
    if last is None:
        return False
    return now - last <= timedelta(hours=WINDOW_HOURS)


PAST_GATE_LAG = timedelta(hours=1)
"""Ask for a new time only when processing lag ate the requested one."""


def due_passed(due: datetime, sent_at, wall=None) -> bool:
    """True when a requested time passed during processing.

    Compares against the wall clock, but only shortly after the message
    was sent: slow voice transcription turning 'in 5 minutes' stale asks
    for a new time, while hours-late ticks keep the delayed-fire path.
    """
    if sent_at is None:
        return False
    wall = now_sast(wall)
    sent = now_sast(sent_at)
    return due <= wall and wall - sent <= PAST_GATE_LAG


def parse_msg_ts(value) -> datetime | None:
    """A provider timestamp (unix seconds or ISO) as an aware datetime."""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return datetime.fromtimestamp(float(s), tz=SAST)
    except (ValueError, OSError, OverflowError):
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def reminder_template() -> tuple[str, str] | None:
    """Configured (name, lang) for outside-window pokes, or None."""
    import os as _os
    name = (_os.environ.get("SISI_REMINDER_TEMPLATE", "") or "").strip()
    if not name:
        return None
    lang = (_os.environ.get("SISI_REMINDER_TEMPLATE_LANG", "") or "").strip()
    return name, lang or "en"


def mark_reminder_submitted(conn: sqlite3.Connection, reminder_id: int,
                            provider_msg_id: str | None) -> None:
    """The provider accepted the send: queued -> submitted (+ its id)."""
    conn.execute("UPDATE reminders SET status = 'submitted',"
                 " provider_msg_id = ?, dlr_detail = ''"
                 " WHERE id = ? AND status = 'queued'",
                 (provider_msg_id or "", reminder_id))
    conn.commit()


def mark_reminder_failed(conn: sqlite3.Connection, reminder_id: int,
                         detail: str) -> None:
    """Terminal send failure. Kept visible; never silently discarded."""
    conn.execute("UPDATE reminders SET status = 'failed', dlr_detail = ?"
                 " WHERE id = ? AND status IN ('queued', 'submitted',"
                 " 'sending')", (detail or "send failed", reminder_id))
    conn.commit()


def record_dlr(conn: sqlite3.Connection, provider_msg_id: str,
               outcome: str, detail: str = "") -> bool:
    """Apply a delivery report. Idempotent and out-of-order safe.

    outcome is 'delivered', 'failed', or 'submitted' (in-flight).
    Delivered is terminal and wins over a stale failure; failures and
    duplicates never resurrect or duplicate anything. Returns True
    when the report matched one of our reminders.
    """
    if not provider_msg_id:
        return False
    row = conn.execute("SELECT id, status FROM reminders"
                       " WHERE provider_msg_id = ? AND provider_msg_id != ''",
                       (provider_msg_id,)).fetchone()
    if row is None:
        log.info("dlr for unknown message %s ignored", provider_msg_id)
        return False
    current = row["status"]
    if current == "delivered":
        return True
    if outcome == "delivered":
        if current in ("queued", "submitted", "sending", "failed"):
            conn.execute("UPDATE reminders SET status = 'delivered',"
                         " dlr_detail = '' WHERE id = ?", (row["id"],))
            conn.commit()
        return True
    if outcome == "failed":
        if current in ("queued", "submitted", "sending"):
            conn.execute("UPDATE reminders SET status = 'failed',"
                         " dlr_detail = ? WHERE id = ?",
                         (detail or "delivery failed", row["id"]))
            conn.commit()
        return True
    if outcome == "submitted" and current == "queued":
        conn.execute("UPDATE reminders SET status = 'submitted' WHERE id = ?",
                     (row["id"],))
        conn.commit()
    return True


def retry_stuck(conn: sqlite3.Connection, shop_id: str,
                now: datetime | None = None) -> str:
    """Owner recovery: re-fire blocked/failed reminders due now."""
    rows = conn.execute("SELECT id FROM reminders WHERE shop_id = ?"
                        " AND status IN ('blocked', 'failed')",
                        (shop_id,)).fetchall()
    if not rows:
        return t("rem_retry_none")
    ids = [r["id"] for r in rows]
    conn.execute("UPDATE reminders SET status = 'pending', dlr_detail = '',"
                 " blocked_reason = ''"
                 f" WHERE id IN ({','.join('?' for _ in ids)})", ids)
    conn.commit()
    fired = set(send_due(conn, now))
    ok = len([i for i in ids if i in fired])
    still = conn.execute("SELECT COUNT(*) n FROM reminders"
                         f" WHERE id IN ({','.join('?' for _ in ids)})"
                         " AND status IN ('blocked', 'failed')",
                         ids).fetchone()["n"]
    ok += len(ids) - ok - still  # reset to pending for a future due time
    return t("rem_retry_done", ok=ok, stuck=still)


def send_due(conn: sqlite3.Connection, now: datetime | None = None) -> list[int]:
    """Fire due reminders into the send queue (state queued, never sent).

    Window-closed reminders on gated channels go out as the configured
    template when one exists, else wait as blocked with a reason.
    Recurring reminders keep their fired instance for delivery tracking
    and schedule the next one as a new row. Claim + enqueue happen in
    one transaction per reminder, so each fires exactly once even when
    ticks overlap. Returns the enqueued reminder ids.
    """
    from app.sendqueue import enqueue
    now = now_sast(now)
    fired: list[int] = []
    stuck_before = (now - timedelta(minutes=STUCK_CLAIM_MINUTES)).isoformat()
    due_rows = conn.execute(
        "SELECT r.*, s.channel FROM reminders r"
        " JOIN shops s ON s.shop_id = r.shop_id"
        " WHERE ((r.status = 'pending' AND r.due_at <= ?)"
        " OR (r.status = 'blocked' AND r.due_at <= ?)"
        " OR (r.status = 'sending'"
        " AND (r.sent_at = '' OR r.sent_at < ?)))"
        " ORDER BY r.due_at, r.id",
        (now.isoformat(), now.isoformat(), stuck_before)).fetchall()
    for row in due_rows:
        claim = conn.execute("UPDATE reminders SET status = 'sending',"
                             " sent_at = ? WHERE id = ?"
                             " AND status IN ('pending', 'blocked',"
                             " 'sending')",
                             (now.isoformat(), row["id"]))
        if claim.rowcount == 0:
            continue
        due = datetime.fromisoformat(row["due_at"])
        if due.tzinfo is None:
            due = due.replace(tzinfo=SAST)
        delayed = now - due > DELAYED_AFTER
        try:
            context = json.loads(row["context_json"] or "{}")
        except ValueError:
            context = {}
        shop_id = row["shop_id"]
        channel = row["channel"] or "meta"
        from app.i18n import owner_lang
        CURRENT_LANG.set(owner_lang(conn, shop_id))
        gated = channel in WINDOW_GATED_CHANNELS
        if gated and not window_open(conn, shop_id, channel, now):
            tpl = reminder_template()
            if tpl is not None and channel == "infobip":
                enqueue(conn, shop_id, channel, "template",
                        {"reminder_id": row["id"], "template": tpl[0],
                         "lang": tpl[1],
                         "params": [row["summary"][:160]]}, commit=False)
                conn.execute("UPDATE reminders SET status = 'queued',"
                             " blocked_reason = '' WHERE id = ?",
                             (row["id"],))
            else:
                conn.execute("UPDATE reminders SET status = 'blocked',"
                             " blocked_reason = ? WHERE id = ?",
                             (t("rem_blocked_window"), row["id"]))
                conn.commit()
                log.info("reminder %s blocked for %s (window shut)",
                         row["id"], shop_id)
                continue
        else:
            text = fire_text(row["summary"], due, delayed)
            ctx_text, media = render_context(conn, shop_id, context)
            if ctx_text:
                text += f"\n\n{ctx_text}"
            from app.mediasend import with_media_links

            text = with_media_links(text, media)
            enqueue(conn, shop_id, channel, "text",
                    {"body": text, "reminder_id": row["id"]}, commit=False)
            for item in media:
                if item.get("kind") == "file":
                    enqueue(conn, shop_id, channel, "file",
                            {"url": item.get("url", ""),
                             "filename": item.get("filename") or "sisi-file",
                             "caption": item.get("caption", "")}, commit=False)
                else:
                    enqueue(conn, shop_id, channel, "image",
                            {"url": item.get("url", ""),
                             "caption": item.get("caption", "")}, commit=False)
            conn.execute("UPDATE reminders SET status = 'queued',"
                         " blocked_reason = '' WHERE id = ?", (row["id"],))
        if row["recurrence"] in ("daily", "weekly", "monthly"):
            nxt = advance(due, row["recurrence"])
            while nxt <= now:
                nxt = advance(nxt, row["recurrence"])
            conn.execute("INSERT INTO reminders (shop_id, due_at, summary,"
                         " context_json, recurrence)"
                         " VALUES (?, ?, ?, ?, ?)",
                         (shop_id, nxt.isoformat(), row["summary"],
                          row["context_json"], row["recurrence"]))
        conn.execute(
            "INSERT INTO flags (shop_id, key, value) VALUES (?, 'last-fired-reminder', ?)"
            " ON CONFLICT (shop_id, key) DO UPDATE SET value = excluded.value",
            (shop_id, str(row["id"])),
        )
        conn.commit()
        fired.append(row["id"])
        log.info("reminder %s queued for %s", row["id"], shop_id)
    return fired
