"""Reminders: NLU times, offers, context, exactly-once sending, recurrence."""

from datetime import datetime

import pytest

from app.agent import handle_text
from app.reminders import (
    SAST,
    advance,
    cancel_numbered,
    detect_context,
    extract_time,
    list_reminders,
    move_numbered,
    now_sast,
    offer_preview,
    offer_scan,
    parse_bare_time,
    parse_request,
    save_reminder,
    send_due,
    snooze_last,
)
from app.tools import build_order, ensure_shop, set_float


SHOP = "27710000022"
MON_10AM = datetime(2026, 10, 26, 10, 0)  # Monday, naive -> SAST


def _open_window(conn, shop, when):
    conn.execute("INSERT INTO messages_seen (message_id, shop_id, created_at)"
                 " VALUES (?, ?, ?)",
                 (f"seen-{when.isoformat()}", shop, when.isoformat()))
    conn.commit()



@pytest.fixture(autouse=True)
def _shop(conn):
    import app.setup as setup
    ensure_shop(conn, SHOP, "Reminder Spaza")
    setup.skip_setup(conn, SHOP)


def test_thursday_time_and_summary():
    parsed = parse_request("remind me Thursday 5:30 to take this order to the shop",
                           MON_10AM)
    assert parsed["due"] == datetime(2026, 10, 29, 5, 30, tzinfo=SAST)
    assert parsed["summary"] == "take this order to the shop"
    assert parsed["recurrence"] == "none"


def test_tomorrow_defaults_7am():
    parsed = parse_request("remind me tomorrow to collect from Sipho", MON_10AM)
    assert parsed["due"] == datetime(2026, 10, 27, 7, 0, tzinfo=SAST)
    assert parsed["summary"] == "collect from sipho"


def test_every_monday_weekly():
    parsed = parse_request("every Monday 7am remind me to pay rent", MON_10AM)
    assert parsed["recurrence"] == "weekly"
    assert parsed["due"] == datetime(2026, 11, 2, 7, 0, tzinfo=SAST)
    assert parsed["summary"] == "pay rent"


def test_past_time_rolls_to_tomorrow():
    parsed = parse_request("remind me at 9am to open the shop", MON_10AM)
    assert parsed["due"] == datetime(2026, 10, 27, 9, 0, tzinfo=SAST)


def test_past_weekday_rolls_a_week():
    thu_evening = datetime(2026, 10, 29, 18, 0)
    parsed = parse_request("remind me Thursday 5:30 to open", thu_evening)
    assert parsed["due"] == datetime(2026, 11, 5, 5, 30, tzinfo=SAST)


def test_isizulu_tomorrow_and_weekday():
    parsed = parse_request("ngikhumbuze kusasa ukukhokha irenti", MON_10AM)
    assert parsed["due"] == datetime(2026, 10, 27, 7, 0, tzinfo=SAST)
    assert parsed["summary"] == "ukukhokha irenti"
    parsed = parse_request("ngikhumbuze ulwesine 5:30 ukuya esitolo", MON_10AM)
    assert parsed["due"] == datetime(2026, 10, 29, 5, 30, tzinfo=SAST)


def test_daily_and_monthly_recurrence():
    assert parse_request("remind me daily at 6pm to count cash",
                         MON_10AM)["recurrence"] == "daily"
    assert parse_request("remind me every month on payday to pay rent",
                         MON_10AM)["recurrence"] == "monthly"


def test_offer_shop_trip_defaults_0530():
    found = offer_scan("I need to take this to the shop on Thursday", MON_10AM)
    assert found["due"] == datetime(2026, 10, 29, 5, 30, tzinfo=SAST)
    assert offer_preview(found["due"]) == (
        "⏰ Want a reminder Thursday at 05:30? Reply Yes or give a time.")


def test_offer_other_defaults_0700():
    found = offer_scan("I must call the bank on Friday", MON_10AM)
    assert found["due"] == datetime(2026, 10, 30, 7, 0, tzinfo=SAST)


def test_offer_guards():
    assert offer_scan("I need to take this to the shop Thursday at 5pm",
                      MON_10AM) is None  # explicit time
    assert offer_scan("What must I buy Thursday?", MON_10AM) is None
    assert offer_scan("remind me Thursday to open", MON_10AM) is None
    assert offer_scan("I like bread", MON_10AM) is None


def test_save_and_confirm_through_chat(conn):
    msg, buttons = handle_text(conn, SHOP, "remind me tomorrow 7am to pay rent")
    assert buttons == ["Set reminder", "Menu"]
    assert msg.startswith("⏰ *Reminder set*")
    assert "pay rent" in msg and "07:00" in msg
    rows = conn.execute("SELECT * FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "pending"


def test_question_not_a_reminder(conn):
    from app.agent import try_reminder_command
    assert try_reminder_command(conn, SHOP, "remind me how much Sipho owes") is None


def test_list_cancel_move(conn):
    save_reminder(conn, SHOP, now_sast(MON_10AM), "first", {}, "none")
    save_reminder(conn, SHOP, datetime(2026, 10, 27, 7, 0, tzinfo=SAST),
                  "second", {}, "weekly")
    msg = list_reminders(conn, SHOP)
    assert msg.startswith("⏰ *My reminders*")
    assert "1. Mon 26 Oct at 10:00 — first" in msg
    assert "2. Tue 27 Oct at 07:00 — second ↻ weekly" in msg
    assert move_numbered(conn, SHOP, 1, "6am", MON_10AM) == \
        "⏰ Reminder 1 moved to Tue 27 Oct at 06:00."
    assert cancel_numbered(conn, SHOP, 2) == "⏰ Reminder 2 cancelled."
    assert cancel_numbered(conn, SHOP, 9).startswith("No reminder with that number")


def test_offer_yes_and_time_flows(conn, monkeypatch):
    import app.reminders as _rem
    # Controlled clock: Monday 26 Oct 2026, so "Friday" is 30 Oct no
    # matter when the suite runs (it broke once Friday actually came).
    frozen = datetime(2026, 10, 26, 10, 0, tzinfo=SAST)
    orig = _rem.now_sast
    monkeypatch.setattr(_rem, "now_sast",
                        lambda now=None: orig(frozen) if now is None
                        else orig(now))
    msg, buttons = handle_text(conn, SHOP,
                               "Sipho will pay on Friday")
    assert buttons == ["Yes", "No"]
    assert msg == "⏰ Want a reminder Friday at 07:00? Reply Yes or give a time."
    msg, _ = handle_text(conn, SHOP, "yes")
    assert msg.startswith("⏰ *Reminder set*")
    handle_text(conn, SHOP, "Thandi will pay on Friday")
    msg, _ = handle_text(conn, SHOP, "6am")
    assert "Fri 30 Oct at 06:00" in msg


def test_undo_cancels_reminder(conn):
    handle_text(conn, SHOP, "remind me tomorrow 7am to pay rent")
    msg, _ = handle_text(conn, SHOP, "undo")
    assert msg == "Undone: reminder cancelled."
    row = conn.execute("SELECT status FROM reminders WHERE shop_id = ?",
                       (SHOP,)).fetchone()
    assert row["status"] == "cancelled"


def test_exactly_once_and_delayed(conn):
    _open_window(conn, SHOP, datetime(2026, 10, 26, 9, 30, tzinfo=SAST))
    rid = save_reminder(conn, SHOP, datetime(2026, 10, 26, 9, 0, tzinfo=SAST),
                        "open shop", {}, "none")
    assert send_due(conn, MON_10AM) == [rid]
    assert send_due(conn, MON_10AM) == []
    queued = conn.execute("SELECT payload_json FROM send_queue").fetchone()
    assert "(delayed)" in queued["payload_json"]  # 1h late
    row = conn.execute("SELECT status FROM reminders WHERE id = ?",
                       (rid,)).fetchone()
    assert row["status"] == "queued"


def test_recurrence_advances(conn):
    _open_window(conn, SHOP, datetime(2026, 10, 26, 9, 30, tzinfo=SAST))
    rid = save_reminder(conn, SHOP, datetime(2026, 10, 26, 9, 59, tzinfo=SAST),
                        "count", {}, "daily")
    assert send_due(conn, MON_10AM) == [rid]
    row = conn.execute("SELECT due_at, status FROM reminders WHERE id = ?",
                       (rid,)).fetchone()
    assert row["status"] == "queued"  # fired instance keeps delivery state
    nxt = conn.execute("SELECT due_at, status FROM reminders WHERE id = ?",
                       (rid + 1,)).fetchone()
    assert nxt["status"] == "pending"
    assert nxt["due_at"] == datetime(2026, 10, 27, 9, 59,
                                     tzinfo=SAST).isoformat()
    assert advance(datetime(2026, 10, 26, 9, 0), "weekly") == \
        datetime(2026, 11, 2, 9, 0)
    assert advance(datetime(2026, 1, 31, 9, 0), "monthly") == \
        datetime(2026, 2, 28, 9, 0)


def test_snooze_after_fire(conn):
    _open_window(conn, SHOP, datetime(2026, 10, 26, 9, 30, tzinfo=SAST))
    rid = save_reminder(conn, SHOP, datetime(2026, 10, 26, 9, 59, tzinfo=SAST),
                        "count", {}, "none")
    send_due(conn, MON_10AM)
    assert snooze_last(conn, SHOP, 30, MON_10AM) == "😴 Snoozed to 10:30."
    row = conn.execute("SELECT due_at, status FROM reminders WHERE id = ?",
                       (rid,)).fetchone()
    assert row["status"] == "pending"
    assert row["due_at"].startswith("2026-10-26T10:30")
    msg, _ = handle_text(conn, SHOP, "snooze 30 min")
    assert msg.startswith("😴 Snoozed to")


def test_snooze_with_nothing(conn):
    msg, _ = handle_text(conn, SHOP, "snooze")
    assert msg.startswith("Nothing to snooze")


def test_order_context_recomputed_at_send(conn):
    conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, cost_cents, stock_qty,"
        " reorder_level, wholesaler) VALUES (?, 'Bread', 2000, 1500, 0, 10, 'Jumbo')",
        (SHOP,))
    conn.commit()
    build_order(conn, SHOP)  # draft exists at save time
    set_float(conn, SHOP, {"cash": 50000})
    parsed = parse_request("remind me Thursday 5:30 to take this order to the shop",
                           MON_10AM)
    assert detect_context(conn, SHOP,
                          "remind me Thursday 5:30 to take this order to the shop") == \
        {"kind": "order"}
    save_reminder(conn, SHOP, parsed["due"], parsed["summary"], {"kind": "order"})
    conn.execute("UPDATE products SET stock_qty = 100 WHERE shop_id = ?",
                 (SHOP,))  # restocked after saving
    conn.commit()
    _open_window(conn, SHOP, datetime(2026, 10, 29, 5, 0, tzinfo=SAST))
    send_due(conn, datetime(2026, 10, 29, 5, 31))
    queued = conn.execute("SELECT payload_json FROM send_queue").fetchone()
    assert "Nothing to order" in queued["payload_json"]  # recomputed, not stale


def test_order_context_cash_vs_needed(conn):
    conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, cost_cents, stock_qty,"
        " reorder_level, wholesaler) VALUES (?, 'Bread', 2000, 1500, 0, 10, 'Jumbo')",
        (SHOP,))
    conn.commit()
    build_order(conn, SHOP)
    set_float(conn, SHOP, {"cash": 50000})
    save_reminder(conn, SHOP, datetime(2026, 10, 29, 5, 30, tzinfo=SAST),
                  "take this order to the shop", {"kind": "order"})
    _open_window(conn, SHOP, datetime(2026, 10, 29, 5, 0, tzinfo=SAST))
    send_due(conn, datetime(2026, 10, 29, 5, 31))
    import json as _json
    queued = conn.execute("SELECT payload_json FROM send_queue").fetchone()
    body = _json.loads(queued["payload_json"])["body"]
    assert "🛒 *Order total*" in body
    assert "Cash in hand R500.00 vs order R" in body


def test_customer_context(conn):
    sipho = conn.execute("INSERT INTO customers (shop_id, name) VALUES (?, 'Sipho')",
                         (SHOP,)).lastrowid
    conn.execute("INSERT INTO credit_entries (shop_id, customer_id, amount_cents,"
                 " items_text, action_id, created_at) VALUES (?, ?, 4000, 'Bread', ?, ?)",
                 (SHOP, sipho, "rem-ce-0", "2026-10-05T10:00:00.000Z"))
    conn.commit()
    assert detect_context(conn, SHOP, "remind me tomorrow to collect from Sipho") == \
        {"kind": "customer", "name": "Sipho"}
    save_reminder(conn, SHOP, datetime(2026, 10, 27, 7, 0, tzinfo=SAST),
                  "collect from sipho", {"kind": "customer", "name": "Sipho"})
    _open_window(conn, SHOP, datetime(2026, 10, 27, 6, 30, tzinfo=SAST))
    send_due(conn, datetime(2026, 10, 27, 7, 1))
    queued = conn.execute("SELECT payload_json FROM send_queue").fetchone()
    assert "Sipho owes R40.00" in queued["payload_json"]


@pytest.mark.parametrize("text,want", [
    ("6", (6, 0)), ("06", (6, 0)), ("6:00", (6, 0)), ("06:00", (6, 0)),
    ("5:30", (5, 30)), ("6am", (6, 0)), ("6 am", (6, 0)), ("6pm", (18, 0)),
    ("18h00", (18, 0)), ("6h30", (6, 30)), ("6h", (6, 0)),
    ("yes 6", (6, 0)), ("yes 06:00", (6, 0)), ("yes, 06:00", (6, 0)),
    ("yes! 5:30?", (5, 30)), ("yebo 6am", (6, 0)), ("ja, 18h00", (18, 0)),
    ("at 17:30 please", (17, 30)),
])
def test_extract_time_lenient(text, want):
    assert extract_time(text) == want


@pytest.mark.parametrize("text", ["yes", "no", "skip", "pick a time", "",
                                  "half past five", "morning"])
def test_extract_time_none_without_digits(text):
    assert extract_time(text) is None


def test_parse_bare_time_accepts_h_format():
    assert parse_bare_time("18h00") == (18, 0)
    assert parse_bare_time("6h30") == (6, 30)
    assert parse_bare_time("yes 6") is None  # combos need extract_time
