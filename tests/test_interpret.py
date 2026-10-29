"""Unified interpretation eval: planner proposes, code executes.

The planner (model) is scripted here so the suite runs offline; each
case asserts what CODE guarantees: validated tasks execute, filler
and invented entities never persist, failures stay local, and state
stays per-shop. Live model quality is probed separately by
scripts/eval_live.py against representative cases.
"""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import agent
from app.reminders import now_sast
from app.tools import ensure_shop, set_float
from seed.seed_demo import seed_demo

SHOP = "27710000071"
OTHER = "27710000072"


@pytest.fixture(autouse=True)
def _no_setup(conn):
    from app.setup import skip_setup
    skip_setup(conn, SHOP)
    skip_setup(conn, OTHER)


@pytest.fixture()
def shop(conn):
    seed_demo(SHOP, conn)
    set_float(conn, SHOP, {"cash": 120000, "petty": 30000})
    return SHOP


@pytest.fixture()
def sent():
    return now_sast(None).replace(second=0, microsecond=0)


def _script(monkeypatch, plans, tmp_path=None):
    """Stub the planner with fixed plan outputs. Returns call counter."""
    import app.interpreter as interp
    if tmp_path is not None:
        import app.llm as llm
        monkeypatch.setattr(llm, "TOKEN_LOG", tmp_path / "usage.log")
    calls = {"n": 0, "contexts": []}

    def fake(messages):
        calls["n"] += 1
        calls["contexts"].append(messages[-1]["content"])
        body = plans[min(calls["n"] - 1, len(plans) - 1)]
        if isinstance(body, Exception):
            raise body
        content = body if isinstance(body, str) else json.dumps(body)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5,
                                  total_tokens=15))
    monkeypatch.setattr(interp, "propose_plan", fake)
    return calls


def _plan(tasks, understood="ok", question=None, options=None):
    return {"understood": understood, "tasks": tasks, "question": question,
            "options": options or []}


def _t(kind, args=None, quote=""):
    return {"kind": kind, "args": args or {}, "quote": quote}


def _counts(conn, shop=SHOP):
    return {t: conn.execute(
        "SELECT COUNT(*) n FROM %s WHERE shop_id = ?" % t, (shop,)).fetchone()["n"]
        for t in ("customers", "task_plans", "pending_actions", "reminders",
                  "credit_entries", "payments", "sales", "stock_moves")}


# --- the milk case: cash + contextual reminder, never an order ----------------


def test_milk_cash_plus_reminder(conn, shop, sent, monkeypatch, tmp_path):
    calls = _script(monkeypatch, [_plan(
        [_t("cash"), _t("reminder", {"when": "in about five minutes",
                                     "topic": "Prepare for the milk delivery"})],
        understood="cash question plus 5-minute milk reminder")], tmp_path)
    before = _counts(conn)
    msg, _ = agent.handle_text(
        conn, SHOP, "Milk arriving in fifteen minutes, petty cash available"
        " to pay for it, and can you send me a reminder in about five"
        " minutes", sent)
    assert calls["n"] == 1
    assert "👂" in msg  # multi-task echo of understanding
    assert "R300.00" in msg and "Reminder set" in msg
    assert "Customer order" not in msg
    row = conn.execute("SELECT due_at, summary, status FROM reminders"
                       " WHERE shop_id = ?", (SHOP,)).fetchone()
    assert row["due_at"] == (sent + timedelta(minutes=5)).isoformat()
    assert row["summary"] == "Prepare for the milk delivery"
    assert row["status"] == "pending"
    counts = _counts(conn)
    assert counts["customers"] == 6 and counts["task_plans"] == 0
    assert counts["credit_entries"] == before["credit_entries"]
    assert counts["payments"] == before["payments"]
    assert counts["reminders"] == before["reminders"] + 1


def test_planner_context_sees_state_and_shop(conn, shop, sent, monkeypatch):
    calls = _script(monkeypatch, [_plan([_t("chat", {"reply": "Sharp."})])])
    agent.handle_text(conn, SHOP, "hi there", sent)
    ctx = calls["contexts"][0]
    assert "NEW MESSAGE" in ctx and "OPEN STATE" in ctx
    assert "CASH:" in ctx and "CUSTOMERS:" in ctx and "PRODUCTS:" in ctx


def test_exact_commands_skip_planner(conn, shop, sent, monkeypatch):
    calls = _script(monkeypatch, [_plan([_t("chat", {"reply": "nope"})])])
    agent.handle_text(conn, SHOP, "my reminders", sent)
    agent.handle_text(conn, SHOP, "menu", sent)
    agent.handle_text(conn, SHOP, "undo", sent)
    assert calls["n"] == 0


# --- executor robustness: adversarial planner outputs -------------------------


def test_filler_topic_resolves_to_delivery(conn, shop, sent, monkeypatch):
    _script(monkeypatch, [_plan(
        [_t("reminder", {"when": "in about five minutes",
                         "topic": "and can you a"})])])
    msg, _ = agent.handle_text(
        conn, SHOP, "Milk is arriving and can you send me a reminder"
        " in about five minutes", sent)
    assert "Reminder set" in msg
    row = conn.execute("SELECT summary FROM reminders WHERE shop_id = ?",
                       (SHOP,)).fetchone()
    assert row["summary"] == "milk delivery"


def test_bare_topic_without_context_asks(conn, shop, sent, monkeypatch):
    _script(monkeypatch, [_plan(
        [_t("reminder", {"when": "in 5 minutes"})])])
    msg, _ = agent.handle_text(conn, SHOP, "remind me in 5 minutes", sent)
    assert "remind you about" in msg or "khumbu" in msg
    assert _counts(conn)["reminders"] == 0
    msg2, _ = agent.handle_text(conn, SHOP, "count the money")
    assert "Reminder set" in msg2  # follow-up completes, due kept


def test_pronoun_customer_rejected(conn, shop, sent, monkeypatch):
    _script(monkeypatch, [_plan(
        [_t("credit", {"customer": "I",
                       "items": [{"product": "bread", "qty": 2}]})])])
    msg, _ = agent.handle_text(conn, SHOP, "I took 2 bread", sent)
    assert "Which customer" in msg or "khasimende" in msg or "kliënt" in msg
    assert _counts(conn)["pending_actions"] == 0
    assert _counts(conn)["customers"] == 6


def test_order_supplier_veto(conn, shop, sent, monkeypatch):
    _script(monkeypatch, [_plan(
        [_t("order", {"items": [{"product": "milk"}]},
            quote="I ordered milk, it's arriving")])])
    msg, _ = agent.handle_text(conn, SHOP, "I ordered milk, it's arriving",
                               sent)
    assert "customer" in msg.lower() or "khasimende" in msg or "kliënt" in msg
    assert _counts(conn)["task_plans"] == 0


def test_missing_amount_asks(conn, shop, sent, monkeypatch):
    _script(monkeypatch, [_plan([_t("payment", {"customer": "Sipho"})])])
    msg, _ = agent.handle_text(conn, SHOP, "Sipho paid", sent)
    assert "how much" in msg.lower() or "malini" in msg.lower()
    assert _counts(conn)["pending_actions"] == 0


def test_chat_with_amount_rejected(conn, shop, sent, monkeypatch):
    _script(monkeypatch, [_plan(
        [_t("chat", {"reply": "You have R500.00."})])])
    msg, _ = agent.handle_text(conn, SHOP, "how much money?", sent)
    assert "R500" not in msg
    assert _counts(conn)["pending_actions"] == 0


def test_unknown_kind_dropped_question_asked(conn, shop, sent, monkeypatch):
    _script(monkeypatch, [{"understood": "x",
                           "tasks": [{"kind": "teleport", "args": {}}],
                           "question": "What should I do?",
                           "options": []}])
    msg, _ = agent.handle_text(conn, SHOP, "do the thing", sent)
    assert msg == "What should I do?"


def test_invalid_json_asks(conn, shop, sent, monkeypatch):
    _script(monkeypatch, ["not json at all {{{"])
    before = _counts(conn)
    msg, _ = agent.handle_text(conn, SHOP, "sell bread", sent)
    assert msg.strip()
    assert _counts(conn) == before  # zero writes on invalid output


def test_model_failure_is_recoverable(conn, shop, sent, monkeypatch):
    _script(monkeypatch, [RuntimeError("api down")])
    msg, _ = agent.handle_text(conn, SHOP, "sell bread", sent)
    assert msg.strip()
    assert _counts(conn)["pending_actions"] == 0


def test_partial_failure_keeps_good_task(conn, shop, sent, monkeypatch):
    _script(monkeypatch, [_plan(
        [_t("cash"), _t("statement", {"customer": "I"})],
        understood="cash plus statement")])
    msg, _ = agent.handle_text(conn, SHOP, "cash and statement for it", sent)
    assert "R300.00" in msg  # good task ran
    assert "Which customer" in msg or "khasimende" in msg or "kliënt" in msg


# --- orders, sales, stock through plans ----------------------------------------


def test_order_plan_preview_and_confirm(conn, shop, sent, monkeypatch):
    _script(monkeypatch, [_plan(
        [_t("order", {"customer": "Sipho",
                      "items": [{"product": "bread", "qty": 2}]},
            quote="Sipho: 2 bread")])])
    msg, buttons = agent.handle_text(conn, SHOP, "Sipho wants 2 bread", sent)
    assert buttons == ["Yes", "No"]
    assert "Sipho" in msg and _counts(conn)["task_plans"] == 1
    msg2, _ = agent.handle_text(conn, SHOP, "yes")
    assert _counts(conn)["task_plans"] == 1
    assert "saved" in msg2.lower() or "✅" in msg2
    msg3, _ = agent.handle_text(conn, SHOP, "yes")
    assert "already" in msg3.lower()  # repeated Yes duplicates nothing


def test_credit_sale_proposes_then_undo(conn, shop, sent, monkeypatch):
    _script(monkeypatch, [_plan(
        [_t("credit", {"customer": "Sipho",
                       "items": [{"product": "Fresh Milk 1L", "qty": 2}]})])])
    before = _counts(conn)["credit_entries"]
    msg, buttons = agent.handle_text(conn, SHOP, "Sipho took 2 milk", sent)
    assert buttons == ["Yes", "No"]
    agent.handle_text(conn, SHOP, "yes")
    assert _counts(conn)["credit_entries"] == before + 1
    agent.handle_text(conn, SHOP, "undo")
    assert conn.execute("SELECT COUNT(*) n FROM credit_entries"
                        " WHERE shop_id = ? AND reversed = 0",
                        (SHOP,)).fetchone()["n"] == before


def test_unknown_product_asks_price(conn, shop, sent, monkeypatch):
    _script(monkeypatch, [_plan(
        [_t("sale", {"items": [{"product": "Dragonfruit", "qty": 2}]})])])
    msg, _ = agent.handle_text(conn, SHOP, "sold 2 dragonfruit", sent)
    assert "R" in msg and ("price" in msg.lower() or "malini" in msg.lower()
                           or "hoeveel" in msg.lower() or "yimalini" in msg.lower())
    assert _counts(conn)["sales"] == 0
    msg2, _ = agent.handle_text(conn, SHOP, "dragonfruit R20")
    assert "R40.00" in msg2  # price answer resumes the sale


def test_split_order_and_statement_fire_once(conn, shop, sent, monkeypatch):
    monkeypatch.setenv("SISI_PUBLIC_URL", "https://x.example")
    from app.agent import take_media
    _script(monkeypatch, [_plan(
        [_t("order", {"customer": "Sipho",
                      "items": [{"product": "Bread", "qty": 2}]},
            quote="Sipho: 2 bread. Statement for Sipho."),
         _t("statement", {"customer": "Sipho"})],
        understood="order plus statement")])
    msg, buttons = agent.handle_text(
        conn, SHOP, "Sipho: 2 bread. Statement for Sipho.", sent)
    assert buttons == ["Yes", "No"]
    items = take_media(SHOP)
    assert [i["kind"] for i in items] == ["image", "file"]  # once, not twice
    agent.handle_text(conn, SHOP, "yes")
    assert take_media(SHOP) == []  # plan held no statement copy


def test_shop_isolation(conn, shop, sent, monkeypatch):
    seed_demo(OTHER, conn)
    set_float(conn, OTHER, {"cash": 50000, "petty": 10000})
    _script(monkeypatch, [_plan([_t("cash")])])
    agent.handle_text(conn, SHOP, "cash in hand?", sent)
    _script(monkeypatch, [_plan(
        [_t("reminder", {"when": "in 5 minutes"})])])
    agent.handle_text(conn, OTHER, "remind me in 5 minutes", sent)
    assert conn.execute("SELECT COUNT(*) n FROM await_reminder WHERE shop_id = ?",
                        (OTHER,)).fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) n FROM await_reminder WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0
