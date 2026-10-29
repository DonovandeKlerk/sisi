"""Supplier-delivery context vs customer orders (the live voice-note bug).

Transcript: "I ordered milk, milk, it's arriving in fifteen minutes how
much petty cash do I have available to pay for it. Also send me a
reminder in about five minutes please."

It was routed to Customer order: two invented milk lines, a customer
named "I"/"It", the cash question and reminder swallowed as unmatched
order lines. These tests pin the fix: supplier context is never a
customer order, disfluencies don't multiply, pronouns are never
customers, and cash + relative-time reminders route deterministically.
"""

from datetime import datetime, timedelta

import pytest

from app import agent
from app.orders import build_plan, parse_order_lines
from app.reminders import SAST, parse_request
from app.tools import ensure_shop, set_float
from seed.seed_demo import seed_demo
from tests.test_format import assert_format

SHOP = "27710000077"
TRANSCRIPT = ("I ordered milk, milk, it's arriving in fifteen minutes how "
              "much petty cash do I have available to pay for it. Also send "
              "me a reminder in about five minutes please.")
SENT = datetime(2026, 10, 29, 12, 0, tzinfo=SAST)


@pytest.fixture(autouse=True)
def _no_setup(conn):
    from app.setup import skip_setup
    skip_setup(conn, SHOP)


@pytest.fixture()
def shop(conn):
    seed_demo(SHOP, conn)
    set_float(conn, SHOP, {"cash": 120000, "petty": 30000})
    return SHOP


def _counts(conn):
    q = lambda t: conn.execute(  # noqa: E731
        "SELECT COUNT(*) n FROM %s WHERE shop_id = ?" % t, (SHOP,)).fetchone()["n"]
    return {t: q(t) for t in ("customers", "task_plans", "pending_actions",
                              "credit_entries", "payments", "sales")}


def _stock(conn):
    return {r["id"]: r["stock_qty"] for r in conn.execute(
        "SELECT id, stock_qty FROM products WHERE shop_id = ?", (SHOP,))}


# --- supplier delivery is never a customer order -----------------------------


@pytest.mark.parametrize("text", [
    "I ordered milk, it's arriving in fifteen minutes",
    "I ordered milk, milk, it's arriving in fifteen minutes",
    "The milk arrives in fifteen minutes",
    "We ordered bread, it's on the way",
    "My Jumbo delivery is coming now",
    "I ordered 2 bread",
    "Ordered stock arriving tomorrow, what do we owe?",
])
def test_supplier_context_builds_no_plan(conn, shop, text):
    plan, question = build_plan(conn, SHOP, text, None, "")
    assert plan is None
    assert question is None  # not even an order clarification


@pytest.mark.parametrize("text", [
    "Sipho: 2 bread, 5 chappies",
    "Sipho ordered 2 bread",
    "2 bread for Sipho",
    "Bongani: 1 Coke 2L",
])
def test_genuine_customer_orders_preserved(conn, shop, text):
    plan, _ = build_plan(conn, SHOP, text, None, "")
    assert plan is not None
    assert plan["order_lines"]
    assert all(ln["customer"] not in ("", "?", "I", "It")
               for ln in plan["order_lines"])


@pytest.mark.parametrize("text", [
    "I ordered 2 bread for Sipho",
    "2 bread for me",
    "how much to pay for it",
    "give it 2 bread",  # pronoun-heavy, no real customer
])
def test_pronouns_never_become_customers(conn, shop, text):
    lines = parse_order_lines(conn, SHOP, text)
    named = [ln["customer"] for ln in lines if ln["customer"]]
    assert all(c.lower() not in ("i", "it", "me") for c in named)
    plan, _ = build_plan(conn, SHOP, text, None, "")
    if plan is not None:
        for ln in plan["order_lines"]:
            assert ln["customer"].lower() not in ("i", "it", "me", "you")


def test_repeated_item_is_one_line(conn, shop):
    lines = parse_order_lines(conn, SHOP, "Sipho: milk, milk")
    assert len(lines) == 1
    assert lines[0]["qty"] == 1


# --- cash + relative reminder through the transcript --------------------------


def test_transcript_answers_cash_and_schedules(conn, shop):
    before, stock_before = _counts(conn), _stock(conn)
    msg, _ = agent.handle_text(conn, SHOP, TRANSCRIPT, SENT)
    assert_format(msg)
    assert "R300.00" in msg  # petty cash answered
    assert "How much is the payment" in msg  # amount asked, none claimed
    assert "Reminder set" in msg
    row = conn.execute("SELECT due_at, summary, status, context_json FROM reminders"
                       " WHERE shop_id = ?", (SHOP,)).fetchone()
    assert row["due_at"] == "2026-10-29T12:05:00+02:00"  # ts + 5, not + 15
    assert row["summary"] == "milk delivery"
    assert row["status"] == "pending"
    assert _counts(conn) == before  # no customers/orders/debts/payments
    assert _stock(conn) == stock_before


@pytest.mark.parametrize("text,expected", [
    ("The milk van arrives in fifteen minutes. How much petty cash is "
     "available to pay the driver? Remind me in five minutes to prepare.",
     "prepare"),
    ("Milk delivery on its way, arriving in about fifteen minutes. What "
     "petty cash is available to pay for it? Send me a reminder in 5 minutes.",
     "milk delivery"),
])
def test_cash_plus_reminder_paraphrases(conn, shop, text, expected, monkeypatch):
    monkeypatch.setattr(agent, "route_to_llm",
                        lambda c, s, t: (_ for _ in ()).throw(
                            AssertionError("LLM must not be needed")))
    msg, _ = agent.handle_text(conn, SHOP, text, SENT)
    assert "R300.00" in msg and "Reminder set" in msg
    row = conn.execute("SELECT due_at, summary FROM reminders WHERE shop_id = ?",
                       (SHOP,)).fetchone()
    assert row["due_at"] == "2026-10-29T12:05:00+02:00"
    assert row["summary"] == expected


def test_cash_rest_falls_back_to_llm(conn, shop, monkeypatch):
    monkeypatch.setattr(agent, "route_to_llm",
                        lambda c, s, t: ("Petty cash is R300.00.", None))
    msg, _ = agent.handle_text(
        conn, SHOP, "The milk van arrives soon. What is my petty cash?"
        " Remind me in five minutes to count the till.", SENT)
    assert "R300.00" in msg and "Reminder set" in msg
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 1


def test_past_due_asks_new_time_keeps_cash(conn, shop):
    from app.reminders import now_sast
    old = now_sast(None) - timedelta(minutes=10)
    msg, _ = agent.handle_text(conn, SHOP, TRANSCRIPT, old)
    assert "R300.00" in msg  # cash task preserved
    assert "already passed" in msg or "reply with a new time" in msg
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0


# --- clarification keeps the timestamp ----------------------------------------


def test_bare_reminder_asks_then_completes(conn, shop, monkeypatch):
    monkeypatch.setattr(agent, "route_to_llm",
                        lambda c, s, t: ("Cash in hand is R1,200.00.", None))
    msg, _ = agent.handle_text(
        conn, SHOP, "Cash in hand? Remind me in 5 minutes.", SENT)
    assert "R1,200.00" in msg
    assert "remind you about" in msg
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0
    msg2, _ = agent.handle_text(conn, SHOP, "to prepare for the delivery")
    assert "Reminder set" in msg2
    row = conn.execute("SELECT due_at, summary FROM reminders WHERE shop_id = ?",
                       (SHOP,)).fetchone()
    assert row["due_at"].endswith("+02:00")
    assert "prepar" in row["summary"]
    assert conn.execute("SELECT COUNT(*) n FROM await_reminder WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0


def test_reminder_detail_no_drops(conn, shop, monkeypatch):
    monkeypatch.setattr(agent, "route_to_llm", lambda c, s, t: ("cash.", None))
    agent.handle_text(conn, SHOP, "Cash in hand? Remind me in 5 minutes.", SENT)
    msg, _ = agent.handle_text(conn, SHOP, "no")
    assert "dropped" in msg or "lahle" in msg or "gelos" in msg
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0


def test_remind_them_stays_composite(conn, shop):
    assert agent.try_reminder_command(conn, SHOP, "remind them Friday") is None
    assert agent.try_reminder_command(conn, SHOP, "remind them") is None
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0


def test_draft_reminder_never_scheduled(conn, shop):
    assert parse_request("draft a reminder for Thandi") is None
    assert agent.try_reminder_command(
        conn, SHOP, "draft a reminder for Thandi") is None
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0


# --- full webhook path with mocked externals -----------------------------------


def _seed_file_db(path):
    import sqlite3

    from app.db import init_db
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    init_db(c)
    seed_demo(SHOP, c)
    set_float(c, SHOP, {"cash": 120000, "petty": 30000})
    from app.setup import skip_setup
    skip_setup(c, SHOP)
    c.close()


def _file_counts(path):
    import sqlite3
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    out = {}
    for t in ("customers", "task_plans", "pending_actions", "credit_entries",
              "payments", "sales", "reminders"):
        out[t] = c.execute("SELECT COUNT(*) n FROM %s WHERE shop_id = ?" % t,
                           (SHOP,)).fetchone()["n"]
    out["stock"] = {r["id"]: r["stock_qty"] for r in c.execute(
        "SELECT id, stock_qty FROM products WHERE shop_id = ?", (SHOP,))}
    rem = c.execute("SELECT due_at, summary, status FROM reminders"
                    " WHERE shop_id = ?", (SHOP,)).fetchall()
    c.close()
    return out, [dict(r) for r in rem]


def test_voice_note_end_to_end(conn, tmp_path, monkeypatch):
    import app.main as main
    from app.whatsapp import FakeSender, IncomingMessage, get_sender
    from app.db import get_db_path
    _seed_file_db(get_db_path())
    monkeypatch.setattr(main, "_download_msg_media", lambda m, l: b"audio")
    monkeypatch.setattr(agent, "transcribe_audio", lambda a, s, sid, x: TRANSCRIPT)
    monkeypatch.setattr(agent, "route_to_llm",
                        lambda c, s, t: (_ for _ in ()).throw(
                            AssertionError("LLM must not be needed")))
    ts = SENT.timestamp()
    before, _ = _file_counts(get_db_path())
    main.process_message(IncomingMessage("mid-e2e-1", SHOP, "media:audio", None,
                                         str(int(ts)), media_id="m1"))
    assert isinstance(get_sender(), FakeSender)
    bodies = " ".join(b["body"] for b in get_sender().pop_shop(SHOP))
    assert "R300.00" in bodies
    assert "How much is the payment" in bodies
    assert "Reminder set" in bodies
    assert "Customer order" not in bodies
    counts, rems = _file_counts(get_db_path())
    assert len(rems) == 1
    assert rems[0]["due_at"] == "2026-10-29T12:05:00+02:00"
    assert rems[0]["summary"] == "milk delivery"
    assert rems[0]["status"] == "pending"
    for table in ("customers", "task_plans", "pending_actions",
                  "credit_entries", "payments", "sales"):
        assert counts[table] == before[table], table
    assert counts["stock"] == before["stock"]
    main.process_message(IncomingMessage("mid-e2e-2", SHOP, "text", "yes", ""))
    counts2, _ = _file_counts(get_db_path())
    assert counts2["reminders"] == 1  # repeated Yes duplicates nothing


def test_photo_caption_parity(conn, shop, monkeypatch):
    monkeypatch.setattr(agent, "read_shop_photo",
                        lambda image, shop="": {"doc_type": "other"})
    monkeypatch.setattr(agent, "route_to_llm",
                        lambda c, s, t: (_ for _ in ()).throw(
                            AssertionError("LLM must not be needed")))
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (120, 80), "white").save(buf, "PNG")
    _doc, reply, _b = agent.handle_photo(conn, SHOP, buf.getvalue(),
                                         TRANSCRIPT, SENT)
    assert "R300.00" in reply and "Reminder set" in reply
    assert "Customer order" not in reply
    row = conn.execute("SELECT due_at, summary FROM reminders WHERE shop_id = ?",
                       (SHOP,)).fetchone()
    assert row["due_at"] == "2026-10-29T12:05:00+02:00"
    assert row["summary"] == "milk delivery"
    assert conn.execute("SELECT COUNT(*) n FROM task_plans WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0


def test_text_parity_end_to_end(conn, tmp_path, monkeypatch):
    import app.main as main
    from app.whatsapp import IncomingMessage, get_sender
    from app.db import get_db_path
    _seed_file_db(get_db_path())
    monkeypatch.setattr(agent, "route_to_llm",
                        lambda c, s, t: (_ for _ in ()).throw(
                            AssertionError("LLM must not be needed")))
    main.process_message(IncomingMessage("mid-e2e-3", SHOP, "text", TRANSCRIPT,
                                         str(int(SENT.timestamp()))))
    bodies = " ".join(b["body"] for b in get_sender().pop_shop(SHOP))
    assert "R300.00" in bodies and "Reminder set" in bodies
    counts, rems = _file_counts(get_db_path())
    assert len(rems) == 1 and rems[0]["summary"] == "milk delivery"
    assert counts["customers"] == 6 and counts["task_plans"] == 0
