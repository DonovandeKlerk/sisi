"""SAST calendar dates, server-computed weekdays, shop-name quotes.

Regression: statements showed "Saturday 2026-11-01" (a Sunday) and the
daily sales PDF "Sunday 2026-10-21" (a Wednesday) — the model derived
weekdays itself because no date context reached the prompt, and today_str()
used server-local (UTC) time. Shop names also kept curly quotes verbatim.
"""

import datetime as _dt

from app.db import today_line, today_str
from app.i18n import CURRENT_LANG, day_stamp, due_short, due_stamp


class _FakeDT(_dt.datetime):
    """2026-10-31 23:30 UTC = 2026-11-01 01:30 SAST (next day)."""

    @classmethod
    def now(cls, tz=None):
        base = _dt.datetime(2026, 10, 31, 23, 30, tzinfo=_dt.timezone.utc)
        return base if tz is None else base.astimezone(tz)


class _FakeDate(_dt.date):
    """Server-local calendar still on 31 Oct while SAST is 01 Nov."""

    @classmethod
    def today(cls):
        return _dt.date(2026, 10, 31)


def test_today_str_is_sast_not_utc(monkeypatch):
    monkeypatch.delenv("SISI_TODAY", raising=False)
    monkeypatch.setattr("app.db.datetime", _FakeDT)
    monkeypatch.setattr("app.db.date", _FakeDate)
    assert today_str() == "2026-11-01"


def test_today_line_weekdays_for_reported_dates(monkeypatch):
    monkeypatch.setenv("SISI_TODAY", "2026-11-01")
    assert today_line() == "Sunday 2026-11-01 (Africa/Johannesburg)"
    monkeypatch.setenv("SISI_TODAY", "2026-10-21")
    assert today_line() == "Wednesday 2026-10-21 (Africa/Johannesburg)"


def test_prompt_date_context_names_weekday(monkeypatch):
    from app.llm import _today_context

    monkeypatch.setenv("SISI_TODAY", "2026-11-01")
    ctx = _today_context()
    assert "Sunday 2026-11-01 (Africa/Johannesburg)" in ctx
    assert "Never compute a weekday yourself" in ctx


def test_reminder_stamps_monday_02_nov_all_langs():
    day = _dt.date(2026, 11, 2)
    assert day.weekday() == 0  # Monday
    assert due_short(0, "02", 11, "09:00", lang="en") == "Mon 02 Nov at 09:00"
    assert due_short(0, "02", 11, "09:00", lang="zu") == "Mso 02 Nov ngo-09:00"
    assert due_short(0, "02", 11, "09:00", lang="af") == "Ma 02 Nov om 09:00"
    assert due_short(0, "02", 11, "09:00", lang="xh") == "Mvu 02 Nov ngo-09:00"
    assert due_stamp(0, "02", 11, "09:00", lang="en") == "Mon 02 Nov 09:00"
    assert day_stamp(day.weekday(), "02", 11, lang="en") == "Mon 02 Nov"
    CURRENT_LANG.set("zu")
    assert day_stamp(0, "02", 11) == "Mso 02 Nov"


def test_set_shop_name_straightens_curly_quotes(conn):
    from app.tools import ensure_shop, set_shop_name

    shop = "27710000042"
    ensure_shop(conn, shop)
    reply = set_shop_name(conn, shop, "Bongi\u2019s Spaza Shop")
    assert "Bongi's Spaza Shop" in reply
    row = conn.execute("SELECT name FROM shops WHERE shop_id = ?",
                       (shop,)).fetchone()
    assert row["name"] == "Bongi's Spaza Shop"


def test_setup_step1_echo_uses_saved_name(conn):
    from app.agent import handle_text

    shop = "27710000043"
    msg, _ = handle_text(conn, shop, "hi")
    assert "Step 1 of 7" in msg
    msg, _ = handle_text(conn, shop, "Bongi\u2019s Spaza Shop")
    assert "Bongi's Spaza Shop" in msg
    assert "\u2019" not in msg
    row = conn.execute("SELECT name FROM shops WHERE shop_id = ?",
                       (shop,)).fetchone()
    assert row["name"] == "Bongi's Spaza Shop"
