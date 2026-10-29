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

from datetime import timedelta

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


@pytest.fixture(autouse=True)
def _no_setup(conn):
    from app.setup import skip_setup
    skip_setup(conn, SHOP)


@pytest.fixture()
def shop(conn):
    seed_demo(SHOP, conn)
    set_float(conn, SHOP, {"cash": 120000, "petty": 30000})
    return SHOP


@pytest.fixture(autouse=True)
def _plan_router(monkeypatch, tmp_path):
    """Script the planner like a good reading: reminders split out, the
    rest answered (cash/affordability) or left as background context."""
    import json
    import re
    from types import SimpleNamespace

    import app.interpreter as interp
    import app.llm as llm
    from app.orders import _has_blocker, is_supplier_segment
    from app.reminders import REMIND_TRIGGERS
    monkeypatch.setattr(llm, "TOKEN_LOG", tmp_path / "usage.log")

    def fake(messages):
        content = messages[-1]["content"]
        lines = content.split("\n")
        msg = lines[1].strip() if lines and lines[0].startswith("NEW MESSAGE") \
            else ""
        from app.reminders import reminder_clause
        tasks = []
        rest = []
        for bit in re.split(r"[.?!]", msg):
            bit = bit.strip()
            if not bit:
                continue
            if any(t in f" {bit.lower()} " for t in REMIND_TRIGGERS):
                seg = reminder_clause(bit)
                tasks.append({"kind": "reminder",
                              "args": {"when": seg}, "quote": seg})
            else:
                rest.append(bit)
        leftover = " ".join(rest)
        low = leftover.lower()
        if "available" in low and "pay" in low:
            tasks.append({"kind": "affordability", "args": {}, "quote": ""})
        elif "petty cash" in low or "cash in hand" in low:
            tasks.append({"kind": "cash", "args": {}, "quote": ""})
        elif leftover and not is_supplier_segment(leftover) \
                and not _has_blocker(leftover) \
                and (":" in leftover or "order" in low):
            tasks.append({"kind": "order", "args": {}, "quote": leftover})
        body = {"understood": "delivery test", "tasks": tasks,
                "question": None, "options": []}
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2))

    monkeypatch.setattr(interp, "propose_plan", fake)


@pytest.fixture()
def sent():
    from app.reminders import now_sast
    return now_sast(None).replace(second=0, microsecond=0)


def _due(sent, minutes=5):
    from datetime import timedelta
    return (sent + timedelta(minutes=minutes)).isoformat()


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


def test_transcript_answers_cash_and_schedules(conn, shop, sent):
    before, stock_before = _counts(conn), _stock(conn)
    msg, _ = agent.handle_text(conn, SHOP, TRANSCRIPT, sent)
    assert_format(msg)
    assert "R300.00" in msg  # petty cash answered
    assert "How much is the payment" in msg  # amount asked, none claimed
    assert "Reminder set" in msg
    row = conn.execute("SELECT due_at, summary, status, context_json FROM reminders"
                       " WHERE shop_id = ?", (SHOP,)).fetchone()
    assert row["due_at"] == _due(sent)  # ts + 5, not + 15
    assert row["summary"] == "milk delivery"
    assert row["status"] == "pending"
    assert _counts(conn) == before  # no customers/orders/debts/payments
    assert _stock(conn) == stock_before


@pytest.mark.parametrize("text,expected", [
    ("The milk van arrives in fifteen minutes. How much petty cash is "
     "available to pay the driver? Remind me in five minutes to prepare.",
     "Prepare for the milk van delivery"),
    ("Milk delivery on its way, arriving in about fifteen minutes. What "
     "petty cash is available to pay for it? Send me a reminder in 5 minutes.",
     "milk delivery"),
])
def test_cash_plus_reminder_paraphrases(conn, shop, sent, text, expected, monkeypatch):
    msg, _ = agent.handle_text(conn, SHOP, text, sent)
    assert "R300.00" in msg and "Reminder set" in msg
    row = conn.execute("SELECT due_at, summary FROM reminders WHERE shop_id = ?",
                       (SHOP,)).fetchone()
    assert row["due_at"] == _due(sent)
    assert row["summary"] == expected


def test_cash_answered_alongside_reminder(conn, shop, sent, monkeypatch):
    msg, _ = agent.handle_text(
        conn, SHOP, "The milk van arrives soon. What is my petty cash?"
        " Remind me in five minutes to count the till.", sent)
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


def test_bare_reminder_asks_then_completes(conn, shop, sent, monkeypatch):
    msg, _ = agent.handle_text(
        conn, SHOP, "Cash in hand? Remind me in 5 minutes.", sent)
    assert "R1200.00" in msg
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


def test_reminder_detail_no_drops(conn, shop, sent, monkeypatch):
    agent.handle_text(conn, SHOP, "Cash in hand? Remind me in 5 minutes.", sent)
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


# --- filler can never become the task ------------------------------------------


def test_filler_request_uses_delivery_context(conn, shop, sent):
    # REPRESENTATIVE, not verbatim: the complete 08:22 transcript exists
    # only in the live shop DB (unreachable here) and a truncated echo.
    # This case reconstructs its known shape ("…and can you send me a
    # reminder in about five minutes" + delivery context + cash question),
    # matching the delivered "and can you a." fragment.
    msg, _ = agent.handle_text(
        conn, SHOP, "Milk is arriving in fifteen minutes and can you send me "
        "a reminder in about five minutes. How much petty cash is available "
        "to pay for it?", sent)
    assert "Reminder set" in msg
    row = conn.execute("SELECT due_at, summary FROM reminders WHERE shop_id = ?",
                       (SHOP,)).fetchone()
    assert row["due_at"] == _due(sent)
    assert row["summary"] == "milk delivery"


def test_request_before_context(conn, shop, sent, monkeypatch):
    msg, _ = agent.handle_text(
        conn, SHOP, "Remind me in 5 minutes. The milk is arriving.", sent)
    assert "Reminder set" in msg
    row = conn.execute("SELECT summary FROM reminders WHERE shop_id = ?",
                       (SHOP,)).fetchone()
    assert row["summary"] == "milk delivery"


def test_get_ready_merges_with_delivery(conn, shop, sent, monkeypatch):
    agent.handle_text(conn, SHOP, "Bread is arriving. Remind me in 5 minutes "
                                  "to get ready.", sent)
    row = conn.execute("SELECT summary FROM reminders WHERE shop_id = ?",
                       (SHOP,)).fetchone()
    assert row["summary"] == "Get ready for the bread delivery"


def test_explicit_topic_never_hijacked(conn, shop, sent, monkeypatch):
    agent.handle_text(conn, SHOP, "Milk is arriving. Remind me in 5 minutes "
                                  "to get ready for church.", sent)
    row = conn.execute("SELECT summary FROM reminders WHERE shop_id = ?",
                       (SHOP,)).fetchone()
    assert row["summary"] == "get ready for church"


def test_incomplete_clause_asks_then_completes(conn, shop, sent):
    msg, _ = agent.handle_text(conn, SHOP, "Remind me in 5 minutes to", sent)
    assert "remind you about" in msg or "khumbu" in msg
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0
    msg2, _ = agent.handle_text(conn, SHOP, "count the money")
    assert "Reminder set" in msg2
    row = conn.execute("SELECT due_at, summary FROM reminders WHERE shop_id = ?",
                       (SHOP,)).fetchone()
    assert row["due_at"] == _due(sent)  # original due kept
    assert row["summary"] == "count the money"


def test_disfluent_time_parses(conn, shop, sent, monkeypatch):
    msg, _ = agent.handle_text(
        conn, SHOP, "Cash in hand? Um, remind me in, uh, 5 minutes to check.",
        sent)
    assert "Reminder set" in msg
    row = conn.execute("SELECT due_at, summary FROM reminders WHERE shop_id = ?",
                       (SHOP,)).fetchone()
    assert row["due_at"] == _due(sent)
    assert row["summary"] == "check"


def test_standalone_bare_reminder_asks(conn, shop, sent):
    msg, _ = agent.handle_text(conn, SHOP, "remind me in 5 minutes", sent)
    assert "remind you about" in msg or "khumbu" in msg
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) n FROM await_reminder WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 1


def test_other_commands_survive_clarification(conn, shop, sent):
    agent.handle_text(conn, SHOP, "remind me in 5 minutes", sent)
    msg, _ = agent.handle_text(conn, SHOP, "my reminders")
    assert "reminder" in msg.lower()  # normal command answered
    assert conn.execute("SELECT COUNT(*) n FROM await_reminder WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 1  # still waiting
    msg2, _ = agent.handle_text(conn, SHOP, "the milk delivery")
    assert "Reminder set" in msg2
    row = conn.execute("SELECT due_at, summary FROM reminders WHERE shop_id = ?",
                       (SHOP,)).fetchone()
    assert row["due_at"] == _due(sent)
    assert row["summary"] == "milk delivery"


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


def test_voice_note_end_to_end(conn, tmp_path, monkeypatch, sent):
    import app.main as main
    from app.whatsapp import FakeSender, IncomingMessage, get_sender
    from app.db import get_db_path
    _seed_file_db(get_db_path())
    monkeypatch.setattr(main, "_download_msg_media", lambda m, l: b"audio")
    monkeypatch.setattr(agent, "transcribe_audio", lambda a, s, sid, x: TRANSCRIPT)
    ts = sent.timestamp()
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
    assert rems[0]["due_at"] == _due(sent)
    assert rems[0]["summary"] == "milk delivery"
    assert rems[0]["status"] == "pending"
    for table in ("customers", "task_plans", "pending_actions",
                  "credit_entries", "payments", "sales"):
        assert counts[table] == before[table], table
    assert counts["stock"] == before["stock"]
    main.process_message(IncomingMessage("mid-e2e-2", SHOP, "text", "yes", ""))
    counts2, _ = _file_counts(get_db_path())
    assert counts2["reminders"] == 1  # repeated Yes duplicates nothing


def test_photo_caption_parity(conn, shop, sent, monkeypatch):
    monkeypatch.setattr(agent, "read_shop_photo",
                        lambda image, shop="": {"doc_type": "other"})
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (120, 80), "white").save(buf, "PNG")
    _doc, reply, _b = agent.handle_photo(conn, SHOP, buf.getvalue(),
                                         TRANSCRIPT, sent)
    assert "R300.00" in reply and "Reminder set" in reply
    assert "Customer order" not in reply
    row = conn.execute("SELECT due_at, summary FROM reminders WHERE shop_id = ?",
                       (SHOP,)).fetchone()
    assert row["due_at"] == _due(sent)
    assert row["summary"] == "milk delivery"
    assert conn.execute("SELECT COUNT(*) n FROM task_plans WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0


def test_text_parity_end_to_end(conn, tmp_path, monkeypatch, sent):
    import app.main as main
    from app.whatsapp import IncomingMessage, get_sender
    from app.db import get_db_path
    _seed_file_db(get_db_path())
    main.process_message(IncomingMessage("mid-e2e-3", SHOP, "text", TRANSCRIPT,
                                         str(int(sent.timestamp()))))
    bodies = " ".join(b["body"] for b in get_sender().pop_shop(SHOP))
    assert "R300.00" in bodies and "Reminder set" in bodies
    counts, rems = _file_counts(get_db_path())
    assert len(rems) == 1 and rems[0]["summary"] == "milk delivery"
    assert counts["customers"] == 6 and counts["task_plans"] == 0
