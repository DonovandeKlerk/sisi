"""Multi-intent voice-note regression: cash question + relative reminder.

A voice note ("milk in 15 minutes, petty cash?, remind me in 5 minutes")
used to reach the model, which has no reminder tool and invented "I can't
ping you" plus "keep this chat open". Reminders are now extracted
deterministically and the rest routes normally. Milk is never recorded.
"""

from datetime import datetime, timedelta

import pytest

from app.agent import handle_photo, handle_text, handle_voice
from app.reminders import SAST, now_sast, parse_msg_ts, parse_relative
from app.setup import skip_setup
from app.tools import ensure_shop, set_float

SHOP = "27710000333"

TRANSCRIPT = ("Milk delivery arriving in about 15 minutes. "
              "How much petty cash do we have? "
              "Remind me in 5 minutes to check the till.")


@pytest.fixture(autouse=True)
def _setup(conn):
    skip_setup(conn, SHOP)
    ensure_shop(conn, SHOP, "Multi Spaza")
    set_float(conn, SHOP, {"cash": 100000, "petty": 20000})


@pytest.fixture()
def _mock_llm(monkeypatch):
    import app.agent as agent
    monkeypatch.setattr(agent, "route_to_llm",
                        lambda c, s, t: ("[reads] " + t[:80], None))


def _tables(conn):
    return {t: conn.execute(f"SELECT COUNT(*) n FROM {t} WHERE shop_id = ?",
                            (SHOP,)).fetchone()["n"]
            for t in ("stock_moves", "sales", "expenses", "customers",
                      "credit_entries", "payments")}


def test_voice_cash_plus_reminder(conn, _mock_llm, monkeypatch):
    import app.agent as agent
    monkeypatch.setattr(agent, "transcribe_audio", lambda *a, **k: TRANSCRIPT)
    sent = now_sast()
    heard, reply, _b = handle_voice(conn, SHOP, b"fake-audio", ".ogg", sent)
    assert "check the till" in heard
    assert "[reads]" in reply and "Reminder set" in reply
    assert "can't ping" not in reply and "keep" not in reply.lower()
    rows = conn.execute("SELECT due_at, summary, status FROM reminders"
                        " WHERE shop_id = ?", (SHOP,)).fetchall()
    assert len(rows) == 1  # the 15-minute ETA is not a reminder
    due = datetime.fromisoformat(rows[0]["due_at"])
    assert due == sent + timedelta(minutes=5)
    assert rows[0]["summary"] == "check the till"
    assert _tables(conn) == {k: 0 for k in _tables(conn)}  # milk untouched


def test_relative_resolves_from_sent_timestamp(conn):
    sent = now_sast().replace(second=0, microsecond=0)
    handle_text(conn, SHOP, "remind me in 5 minutes to check", sent)
    row = conn.execute("SELECT due_at FROM reminders WHERE shop_id = ?",
                       (SHOP,)).fetchone()
    assert datetime.fromisoformat(row["due_at"]) == sent + timedelta(minutes=5)


def test_parse_relative_units():
    now = datetime(2026, 10, 29, 6, 0, tzinfo=SAST)
    assert parse_relative("remind me in 5 minutes", now) == \
        now + timedelta(minutes=5)
    assert parse_relative("in 2 hours", now) == now + timedelta(hours=2)
    assert parse_relative("in an hour", now) == now + timedelta(hours=1)
    assert parse_relative("in half an hour", now) == \
        now + timedelta(minutes=30)
    assert parse_relative("remind me Thursday", now) is None


def test_parse_msg_ts_formats():
    from datetime import timezone
    assert parse_msg_ts("1761705600") == \
        datetime.fromtimestamp(1761705600, tz=SAST)
    assert parse_msg_ts("2026-10-29T04:00:00Z") == \
        datetime(2026, 10, 29, 6, 0, tzinfo=SAST)
    assert parse_msg_ts("2026-10-29T06:00:00+02:00") == \
        datetime(2026, 10, 29, 6, 0, tzinfo=SAST)
    assert parse_msg_ts("2026-10-28T10:00:00.000+0000") is not None
    assert parse_msg_ts("") is None and parse_msg_ts("junk") is None


def test_text_and_caption_parity(conn, _mock_llm, monkeypatch):
    import app.agent as agent
    sent = now_sast()
    msg, _b = handle_text(conn, SHOP, TRANSCRIPT, sent)
    assert "Reminder set" in msg
    monkeypatch.setattr(agent, "read_shop_photo",
                        lambda image, shop="": {"doc_type": "other"})
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, "PNG")
    _d, reply, _b = handle_photo(conn, SHOP, buf.getvalue(), TRANSCRIPT,
                                 sent)
    assert "Reminder set" in reply
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 2


def test_partial_transcription_asks_for_content(conn, _mock_llm, monkeypatch):
    import app.agent as agent
    monkeypatch.setattr(agent, "llm_chat_full",
                        lambda c, s, t: ("What should I remind you about?", None))
    msg, _b = handle_text(conn, SHOP,
                          "How much petty cash do we have? And remind me")
    # no time and no topic: the model asks; nothing is persisted
    assert "remind you about" in msg
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) n FROM await_reminder WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0


def test_clarification_preserves_other_tasks(conn, _mock_llm):
    from app.tools import get_pending
    msg, _b = handle_text(
        conn, SHOP, "I need to take this to the shop on Thursday. "
        "Remind me in 5 minutes to pack.")
    assert "Reminder set" in msg  # saved despite the open question
    assert get_pending(conn, SHOP) is not None  # offer still waiting
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 1


def test_repeated_yes_creates_nothing(conn, _mock_llm):
    handle_text(conn, SHOP, TRANSCRIPT)
    msg, _b = handle_text(conn, SHOP, "Yes")
    assert "nothing waiting" in msg
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 1


def test_stale_relative_time_asks_again(conn, _mock_llm):
    from app.reminders import now_sast as _now
    sent = _now() - timedelta(minutes=10)
    msg, _b = handle_text(conn, SHOP, "remind me in 5 minutes to check",
                          sent)
    assert "already passed" in msg and "new time" in msg
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0
    msg, _b = handle_text(conn, SHOP,
                          "How much petty cash? Remind me in 5 minutes x",
                          sent)
    assert "[reads]" in msg and "already passed" in msg


def test_reminder_delivery_states(conn, _mock_llm):
    from app.reminders import record_dlr, send_due
    from app.sendqueue import drain
    sent = now_sast()
    handle_text(conn, SHOP, "remind me in 5 minutes to check", sent)
    conn.execute("INSERT INTO messages_seen (message_id, shop_id, created_at)"
                 " VALUES ('mm-1', ?, ?)", (SHOP, sent.isoformat()))
    conn.commit()
    rid = send_due(conn, sent + timedelta(minutes=6))[0]
    assert conn.execute("SELECT status FROM reminders WHERE id = ?",
                        (rid,)).fetchone()["status"] == "queued"

    class _Stub:
        def send_text(self, to, body):
            return "pv-mm"

    drain(conn, SHOP, _Stub(), sleep=lambda s: None)
    assert record_dlr(conn, "pv-mm", "delivered") is True
    assert record_dlr(conn, "pv-mm", "failed", "late dup") is True
    row = conn.execute("SELECT status FROM reminders WHERE id = ?",
                       (rid,)).fetchone()
    assert row["status"] == "delivered"


def test_names_never_split(conn):
    handle_text(conn, SHOP,
                "remind me Thursday 6:30 to collect from Sipho and Thandi")
    rows = conn.execute("SELECT summary FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchall()
    assert len(rows) == 1
    assert "sipho and thandi" in rows[0]["summary"]


def test_milk_words_never_record(conn, _mock_llm):
    handle_text(conn, SHOP, "Milk delivery arriving in about 15 minutes")
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0
    assert _tables(conn) == {k: 0 for k in _tables(conn)}


def test_affordability_needs_amount_first(conn):
    msg, _b = handle_text(conn, SHOP, "can we afford to pay the supplier")
    assert "How much is the payment" in msg
    msg, _b = handle_text(conn, SHOP, "can we afford R200")
    assert "R1200.00" in msg and "covers R200.00" in msg
    msg, _b = handle_text(conn, SHOP, "can we afford R2000")
    assert "Not quite" in msg and "R1200.00" in msg
