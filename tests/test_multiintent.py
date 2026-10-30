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


def _script(monkeypatch, tmp_path, plans):
    """Stub the planner with fixed plans."""
    import json
    from types import SimpleNamespace

    import app.interpreter as interp
    import app.llm as llm
    monkeypatch.setattr(llm, "TOKEN_LOG", tmp_path / "usage.log")
    calls = {"n": 0}

    def fake(messages):
        calls["n"] += 1
        body = plans[min(calls["n"] - 1, len(plans) - 1)]
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2))

    monkeypatch.setattr(interp, "propose_plan", fake)
    return calls


def _plan(tasks, understood="ok", question=None):
    return {"understood": understood, "tasks": tasks, "question": question,
            "options": []}


def _t(kind, args=None, quote=""):
    return {"kind": kind, "args": args or {}, "quote": quote}


def _milk_plan():
    return _plan(
        [_t("cash"),
         _t("reminder", {"when": "in 5 minutes", "topic": "check the till"})],
        understood="cash plus reminder")


def _tables(conn):
    return {t: conn.execute(f"SELECT COUNT(*) n FROM {t} WHERE shop_id = ?",
                            (SHOP,)).fetchone()["n"]
            for t in ("stock_moves", "sales", "expenses", "customers",
                      "credit_entries", "payments")}


def test_voice_cash_plus_reminder(conn, monkeypatch, tmp_path):
    import app.agent as agent
    monkeypatch.setattr(agent, "transcribe_audio", lambda *a, **k: TRANSCRIPT)
    _script(monkeypatch, tmp_path, [_milk_plan()])
    sent = now_sast()
    heard, reply, _b = handle_voice(conn, SHOP, b"fake-audio", ".ogg", sent)
    assert "check the till" in heard
    assert "R200.00" in reply and "Reminder set" in reply
    assert "can't ping" not in reply and "keep" not in reply.lower()
    rows = conn.execute("SELECT due_at, summary, status FROM reminders"
                        " WHERE shop_id = ?", (SHOP,)).fetchall()
    assert len(rows) == 1  # the 15-minute ETA is not a reminder
    due = datetime.fromisoformat(rows[0]["due_at"])
    assert due == sent + timedelta(minutes=5)
    assert rows[0]["summary"] == "check the till"
    assert _tables(conn) == {k: 0 for k in _tables(conn)}  # milk untouched


def test_relative_resolves_from_sent_timestamp(conn, monkeypatch, tmp_path):
    _script(monkeypatch, tmp_path, [_plan(
        [_t("reminder", {"when": "in 5 minutes to check"})])])
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


def test_text_and_caption_parity(conn, monkeypatch, tmp_path):
    import app.agent as agent
    _script(monkeypatch, tmp_path, [_milk_plan()])
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


def test_partial_transcription_asks_for_content(conn, monkeypatch, tmp_path):
    _script(monkeypatch, tmp_path, [_plan(
        [_t("cash"), _t("reminder", {"when": "And remind me"})],
        understood="cash plus reminder")])
    msg, _b = handle_text(conn, SHOP,
                          "How much petty cash do we have? And remind me")
    # no time and no topic: cash answered, time asked, nothing persisted
    assert "R200.00" in msg
    assert "When should I remind you" in msg
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) n FROM await_reminder WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0


def test_clarification_preserves_other_tasks(conn, monkeypatch, tmp_path):
    from app.tools import get_pending
    _script(monkeypatch, tmp_path, [_plan(
        [_t("reminder_offer", {"when": "Thursday",
                               "topic": "take this to the shop"}),
         _t("reminder", {"when": "in 5 minutes", "topic": "pack"})],
        understood="offer plus reminder")])
    msg, _b = handle_text(
        conn, SHOP, "I need to take this to the shop on Thursday. "
        "Remind me in 5 minutes to pack.")
    assert "Reminder set" in msg  # saved despite the open question
    assert get_pending(conn, SHOP) is not None  # offer still waiting
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 1


def test_repeated_yes_creates_nothing(conn, monkeypatch, tmp_path):
    _script(monkeypatch, tmp_path, [_milk_plan()])
    handle_text(conn, SHOP, TRANSCRIPT)
    msg, _b = handle_text(conn, SHOP, "Yes")
    assert "nothing waiting" in msg
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 1


def test_stale_relative_time_asks_again(conn, monkeypatch, tmp_path):
    from app.reminders import now_sast as _now
    _script(monkeypatch, tmp_path, [
        _plan([_t("reminder", {"when": "in 5 minutes to check"})]),
        _plan([_t("cash"), _t("reminder", {"when": "in 5 minutes"})])])
    sent = _now() - timedelta(minutes=10)
    msg, _b = handle_text(conn, SHOP, "remind me in 5 minutes to check",
                          sent)
    assert "already passed" in msg and "new time" in msg
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0
    msg, _b = handle_text(conn, SHOP,
                          "How much petty cash? Remind me in 5 minutes x",
                          sent)
    assert "R200.00" in msg and "already passed" in msg


def test_reminder_delivery_states(conn, monkeypatch, tmp_path):
    from app.reminders import record_dlr, send_due
    _script(monkeypatch, tmp_path, [_plan(
        [_t("reminder", {"when": "in 5 minutes to check"})])])
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


def test_names_never_split(conn, monkeypatch, tmp_path):
    _script(monkeypatch, tmp_path, [_plan(
        [_t("reminder",
            {"when": "remind me Thursday 6:30 to collect from Sipho and Thandi"})])])
    handle_text(conn, SHOP,
                "remind me Thursday 6:30 to collect from Sipho and Thandi")
    rows = conn.execute("SELECT summary FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchall()
    assert len(rows) == 1
    assert "sipho and thandi" in rows[0]["summary"]


def test_milk_words_never_record(conn, monkeypatch, tmp_path):
    _script(monkeypatch, tmp_path, [_plan(
        [_t("chat", {"reply": "Noted, thanks."})])])
    handle_text(conn, SHOP, "Milk delivery arriving in about 15 minutes")
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0
    assert _tables(conn) == {k: 0 for k in _tables(conn)}


def test_affordability_needs_amount_first(conn, monkeypatch, tmp_path):
    _script(monkeypatch, tmp_path, [
        _plan([_t("affordability", {})]),
        _plan([_t("affordability", {"amount_cents": 20000})]),
        _plan([_t("affordability", {"amount_cents": 200000})])])
    msg, _b = handle_text(conn, SHOP, "can we afford to pay the supplier")
    assert "How much is the payment" in msg
    msg, _b = handle_text(conn, SHOP, "can we afford R200")
    assert "R1200.00" in msg and "covers R200.00" in msg
    msg, _b = handle_text(conn, SHOP, "can we afford R2000")
    assert "Not quite" in msg and "R1200.00" in msg


@pytest.fixture()
def _follow_shop(conn, tmp_path, monkeypatch):
    from app import reports
    from app.agent import MEDIA_OUTBOX
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(reports, "LOGOS_DIR", tmp_path / "logos")
    monkeypatch.setenv("SISI_PUBLIC_URL", "https://x.example")
    MEDIA_OUTBOX.clear()
    ensure_shop(conn, SHOP, "Followup Spaza")
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?", (SHOP,))
    conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, cost_cents, stock_qty)"
        " VALUES (?, 'Bread', 2000, 1500, 50)", (SHOP,))
    conn.execute("INSERT INTO customers (shop_id, name) VALUES (?, 'Sipho')",
                 (SHOP,))
    conn.commit()


def _pending_payload(conn):
    import json
    from app.tools import get_pending
    row = get_pending(conn, SHOP)
    assert row is not None
    return json.loads(row["payload_json"])


def _exec_plan(conn, tasks):
    from app.interpreter import execute_plan
    return execute_plan(conn, SHOP, _plan(tasks), {"text": "", "sent_at": None})


def test_statement_defers_until_credit_confirmed(conn, _follow_shop, monkeypatch,
                                                 tmp_path):
    from app.agent import MEDIA_OUTBOX, take_media
    calls = _script(monkeypatch, tmp_path, [_plan(
        [_t("credit", {"customer": "Sipho",
                       "items": [{"product": "Bread", "qty": 2}]}),
         _t("statement", {"customer": "Sipho"})],
        understood="credit plus statement")])
    msg, buttons = handle_text(conn, SHOP, "put bread on Sipho's book and state it")
    assert calls["n"] == 1  # pins the planner path (no vacuous pass)
    assert "Save to the book?" in msg
    assert "I'll send that once you confirm" in msg
    assert "Outstanding" not in msg  # statement held back, not rendered stale
    assert take_media(SHOP) == []  # no card queued either
    assert buttons == ["Yes", "No"]
    assert _pending_payload(conn)["plan_followups"] == [
        {"kind": "statement", "args": {"customer": "Sipho"}}]
    assert MEDIA_OUTBOX == {}


def test_confirm_runs_deferred_statement(conn, _follow_shop):
    from app.agent import take_media
    msg, buttons = _exec_plan(conn, [
        _t("credit", {"customer": "Sipho",
                      "items": [{"product": "Bread", "qty": 2}]}),
        _t("statement", {"customer": "Sipho"})])
    assert "Save to the book?" in msg
    assert "Outstanding" not in msg
    msg, _ = handle_text(conn, SHOP, "yes")
    assert "Outstanding R40.00" in msg  # settled ledger, includes the credit
    kinds = [m["kind"] for m in take_media(SHOP)]
    assert kinds == ["image", "file"]
    # Repeat Yes cannot rerun the followup.
    msg2, _ = handle_text(conn, SHOP, "yes")
    assert "Outstanding" not in msg2
    assert take_media(SHOP) == []


def test_cancel_runs_deferred_statement_unchanged(conn, _follow_shop):
    from app.agent import take_media
    _exec_plan(conn, [
        _t("credit", {"customer": "Sipho",
                      "items": [{"product": "Bread", "qty": 2}]}),
        _t("statement", {"customer": "Sipho"})])
    msg, _ = handle_text(conn, SHOP, "no")
    # Followup ran on the settled (unchanged) ledger; zero balance
    # short-circuits to text with no card.
    assert "owes nothing" in msg
    assert take_media(SHOP) == []


def test_read_before_write_runs_immediately(conn, _follow_shop):
    msg, _ = _exec_plan(conn, [
        _t("balance", {"customer": "Sipho"}),
        _t("credit", {"customer": "Sipho",
                      "items": [{"product": "Bread", "qty": 2}]})])
    assert "owes nothing" in msg  # fresh pre-write balance, correct then
    assert "Save to the book?" in msg
    assert "I'll send that once you confirm" not in msg


def test_unrelated_read_after_write_runs_immediately(conn, _follow_shop):
    msg, _ = _exec_plan(conn, [
        _t("credit", {"customer": "Sipho",
                      "items": [{"product": "Bread", "qty": 2}]}),
        _t("compliance", {"topic": "overview"})])
    assert "Save to the book?" in msg
    assert "I'll send that once you confirm" not in msg
    assert "Spaza Shop Support Fund" in msg or "spazashopfund" in msg


def test_no_proposal_runs_reads_immediately(conn, _follow_shop):
    msg, _ = _exec_plan(conn, [
        _t("credit", {"customer": "Sipho",
                      "items": [{"product": "Nope", "qty": 1}]}),
        _t("balance", {"customer": "Sipho"})])
    assert "I'll send that once you confirm" not in msg
    assert "owes nothing" in msg  # nothing to wait for: answered at once


def test_followups_never_attach_to_stale_pending(conn, _follow_shop):
    from app.tools import _propose
    _propose(conn, SHOP, "record_sale",
             {"product": "Bread", "qty": 1, "product_id": 1,
              "total_cents": 2000}, "stale preview")
    msg, _ = _exec_plan(conn, [
        _t("credit", {"customer": "Sipho",
                      "items": [{"product": "Nope", "qty": 1}]}),
        _t("balance", {"customer": "Sipho"})])
    assert "owes nothing" in msg  # ran inline, not held on the stale proposal
    assert "plan_followups" not in _pending_payload(conn)


def test_prompt_disambiguates_sale_vs_credit():
    from app.prompts import PLANNER_PROMPT
    assert "sale with buyer" in PLANNER_PROMPT
    assert "stock decreases AND the book increases" in PLANNER_PROMPT
    assert "no goods" in PLANNER_PROMPT


# --- multi-item sales: one proposal holds every line -------------------------
# Live bug (30 Oct 2026): "Sipho bought whey protein and a loaf of bread"
# showed two "Save sale?" previews but Yes saved only the bread — each
# per-item propose superseded the last. Every line must be saved.


@pytest.fixture()
def _two_products(conn, _follow_shop):
    conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, cost_cents, stock_qty)"
        " VALUES (?, 'Whey', 45000, 30000, 5)", (SHOP,))
    conn.commit()


def _stock(conn, name):
    return conn.execute("SELECT stock_qty FROM products WHERE shop_id = ?"
                        " AND name = ?", (SHOP, name)).fetchone()["stock_qty"]


def _sales(conn):
    return conn.execute("SELECT product_name, qty, total_cents, action_id"
                        " FROM sales WHERE shop_id = ? AND reversed = 0",
                        (SHOP,)).fetchall()


def _credit_total(conn):
    from app.tools import _get_customer, balance_cents
    cust = _get_customer(conn, SHOP, "Sipho")
    return balance_cents(conn, SHOP, cust["id"])


def test_two_items_one_proposal(conn, _two_products):
    msg, buttons = _exec_plan(conn, [
        _t("sale", {"buyer": "Sipho",
                    "items": [{"product": "Whey", "qty": 1},
                              {"product": "Bread", "qty": 1}]})])
    assert "Save sale? (2 items)" in msg
    assert "Whey" in msg and "Bread" in msg
    assert msg.count("Save sale?") == 1  # one preview, not two
    assert "R470.00" in msg  # combined total
    assert all(len(line) <= 120 for line in msg.splitlines())
    assert buttons is None  # direct executor; handle_text adds Yes/No
    payload = _pending_payload(conn)
    assert [ln["product"] for ln in payload["lines"]] == ["Whey", "Bread"]


def test_two_sale_tasks_same_buyer_merge(conn, _two_products):
    msg, _ = _exec_plan(conn, [
        _t("sale", {"buyer": "Sipho",
                    "items": [{"product": "Whey", "qty": 1}]}),
        _t("sale", {"buyer": "sipho",
                    "items": [{"product": "Bread", "qty": 1}]})])
    assert "Save sale? (2 items)" in msg
    assert msg.count("Save sale?") == 1
    assert len(_pending_payload(conn)["lines"]) == 2


def test_confirm_saves_every_line(conn, _two_products):
    _exec_plan(conn, [
        _t("sale", {"buyer": "Sipho",
                    "items": [{"product": "Whey", "qty": 1},
                              {"product": "Bread", "qty": 2}]})])
    msg, _ = handle_text(conn, SHOP, "yes")
    assert "Sale saved" in msg and "Whey" in msg and "Bread" in msg
    assert _stock(conn, "Whey") == 4
    assert _stock(conn, "Bread") == 48
    rows = _sales(conn)
    assert {(r["product_name"], r["qty"]) for r in rows} == \
        {("Whey", 1), ("Bread", 2)}
    assert len({r["action_id"].rsplit("-sc", 1)[0] for r in rows}) == 1 \
        # one action head (-sc{i} suffixes); one Undo reverses all
    assert sum(r["total_cents"] for r in rows) == 49000
    assert conn.execute("SELECT COUNT(*) n FROM credit_entries"
                        " WHERE shop_id = ? AND reversed = 0",
                        (SHOP,)).fetchone()["n"] == 2  # one per line, paired
    assert _credit_total(conn) == 49000


def test_voice_two_items_then_statement(conn, _two_products, monkeypatch,
                                        tmp_path):
    import app.agent as agent
    from app.agent import take_media
    monkeypatch.setattr(agent, "transcribe_audio",
                        lambda *a, **k: "Sipho bought whey and bread")
    _script(monkeypatch, tmp_path, [_plan(
        [_t("sale", {"buyer": "Sipho",
                     "items": [{"product": "Whey", "qty": 1},
                               {"product": "Bread", "qty": 1}]}),
         _t("statement", {"customer": "Sipho"})],
        understood="whey and bread on Sipho's account")])
    _heard, reply, _b = handle_voice(conn, SHOP, b"fake-audio", ".ogg")
    assert "Save sale? (2 items)" in reply
    assert "I'll send that once you confirm" in reply
    assert "Outstanding" not in reply
    assert take_media(SHOP) == []
    msg, _ = handle_text(conn, SHOP, "yes")
    assert "Sale saved" in msg and "Outstanding R470.00" in msg
    assert "Whey" in msg and "Bread" in msg
    assert take_media(SHOP) != []  # statement card queued after Yes
    assert _stock(conn, "Whey") == 4 and _stock(conn, "Bread") == 49
    assert _credit_total(conn) == 47000


def test_different_buyers_chain_in_turn(conn, _two_products):
    msg, _ = _exec_plan(conn, [
        _t("sale", {"buyer": "Sipho",
                    "items": [{"product": "Bread", "qty": 1}]}),
        _t("sale", {"buyer": "Thandi",
                    "items": [{"product": "Whey", "qty": 1}]})])
    assert "to Sipho" in msg  # first proposal live ...
    assert "Thandi" not in msg  # ... second held, not superseded away
    assert "I'll send that once you confirm" in msg
    msg, _ = handle_text(conn, SHOP, "yes")
    assert "Sale saved" in msg and "Bread" in msg
    assert "Save sale?" in msg and "Thandi" in msg  # chained proposal
    msg, _ = handle_text(conn, SHOP, "yes")
    assert "Sale saved" in msg and "Whey" in msg
    assert _stock(conn, "Bread") == 49 and _stock(conn, "Whey") == 4
    from app.tools import _get_customer, balance_cents
    than = _get_customer(conn, SHOP, "Thandi")
    assert balance_cents(conn, SHOP, than["id"]) == 45000
    assert _credit_total(conn) == 2000


def test_chained_write_holds_later_read(conn, _two_products):
    _exec_plan(conn, [
        _t("sale", {"buyer": "Sipho",
                    "items": [{"product": "Bread", "qty": 1}]}),
        _t("sale", {"buyer": "Thandi",
                    "items": [{"product": "Whey", "qty": 1}]}),
        _t("statement", {"customer": "Sipho"})])
    msg, _ = handle_text(conn, SHOP, "yes")
    assert "Thandi" in msg  # chained proposal first ...
    assert "Outstanding" not in msg  # ... statement still held behind it
    assert "I'll send that once you confirm" in msg
    msg, _ = handle_text(conn, SHOP, "yes")
    assert "Outstanding R20.00" in msg  # Sipho's settled ledger


def test_unknown_second_item_revives_batch(conn, _two_products):
    msg, _ = _exec_plan(conn, [
        _t("sale", {"buyer": "Sipho",
                    "items": [{"product": "Bread", "qty": 1},
                              {"product": "Fizzbuzz", "qty": 1}]})])
    assert "isn't on your list yet" in msg  # nothing proposed yet
    msg, _ = handle_text(conn, SHOP, "Fizzbuzz R10")
    assert "Save sale? (2 items)" in msg  # whole batch back
    assert "Bread" in msg and "Fizzbuzz" in msg
    handle_text(conn, SHOP, "yes")
    assert _stock(conn, "Bread") == 49
    assert {(r["product_name"], r["qty"]) for r in _sales(conn)} == \
        {("Bread", 1), ("Fizzbuzz", 1)}
    assert _credit_total(conn) == 3000


def test_undo_multi_sale_reverses_all(conn, _two_products):
    _exec_plan(conn, [
        _t("sale", {"buyer": "Sipho",
                    "items": [{"product": "Whey", "qty": 1},
                              {"product": "Bread", "qty": 1}]})])
    handle_text(conn, SHOP, "yes")
    msg, _ = handle_text(conn, SHOP, "undo")
    assert "Undone" in msg and "R470.00" in msg
    assert _sales(conn) == []
    assert _stock(conn, "Whey") == 5 and _stock(conn, "Bread") == 50
    assert _credit_total(conn) == 0


def test_cancel_multi_sale_saves_nothing(conn, _two_products):
    _exec_plan(conn, [
        _t("sale", {"buyer": "Sipho",
                    "items": [{"product": "Whey", "qty": 1},
                              {"product": "Bread", "qty": 1}]})])
    msg, _ = handle_text(conn, SHOP, "no")
    assert "did not save" in msg
    assert _sales(conn) == []
    assert _stock(conn, "Whey") == 5 and _stock(conn, "Bread") == 50
    assert _credit_total(conn) == 0


def test_single_item_sale_unchanged(conn, _two_products):
    msg, _ = _exec_plan(conn, [
        _t("sale", {"buyer": "Sipho",
                    "items": [{"product": "Bread", "qty": 1}]})])
    assert "(2 items)" not in msg and "(1 items)" not in msg
    assert " to Sipho" in msg  # legacy single-line preview shape
    assert "lines" not in _pending_payload(conn)  # legacy payload shape
    handle_text(conn, SHOP, "yes")
    assert _credit_total(conn) == 2000


def test_cash_multi_sale_posts_cash(conn, _two_products):
    def _cash():
        return conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) s FROM cash_moves"
            " WHERE shop_id = ? AND reversed = 0", (SHOP,)).fetchone()["s"]
    before = _cash()
    msg, _ = _exec_plan(conn, [
        _t("sale", {"items": [{"product": "Whey", "qty": 1},
                              {"product": "Bread", "qty": 1}]})])
    assert "Save sale? (2 items)" in msg and "Total R470.00 cash" in msg
    handle_text(conn, SHOP, "yes")
    assert len(_sales(conn)) == 2
    assert conn.execute("SELECT COUNT(*) n FROM credit_entries"
                        " WHERE shop_id = ?", (SHOP,)).fetchone()["n"] == 0
    assert _cash() - before == 47000


def test_superseded_write_is_rescued(conn, _two_products):
    # A proposing kind outside the chain set still can't silently kill
    # an earlier plan proposal: the victim is re-asked after Yes.
    msg, _ = _exec_plan(conn, [
        _t("sale", {"buyer": "Sipho",
                    "items": [{"product": "Bread", "qty": 1}]}),
        _t("price_set", {"product": "Bread", "amount_cents": 2500})])
    assert "to Sipho" in msg and "R25.00" in msg  # both previews shown
    assert "I'll send that once you confirm" in msg
    msg, _ = handle_text(conn, SHOP, "yes")
    assert "to Sipho" in msg  # rescued sale re-proposed ...
    assert "Save sale?" in msg
    msg, _ = handle_text(conn, SHOP, "yes")
    assert "Sale saved" in msg  # ... and saved on the second Yes
    assert _credit_total(conn) == 2500  # at the NEW price
    assert conn.execute("SELECT price_cents FROM products"
                        " WHERE shop_id = ? AND name = 'Bread'",
                        (SHOP,)).fetchone()["price_cents"] == 2500


def test_prompt_prefers_single_sale_task():
    from app.prompts import PLANNER_PROMPT
    assert "ONE sale task" in PLANNER_PROMPT
