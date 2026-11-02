"""Intent engine: composite routing, shop context, ask_clarify (offline).

20 phrasings: 6 keyword fast-path (LLM must NOT run), 8 composite
multi-job (LLM must run, incl. isiZulu/Afrikaans/isiXhosa/slang),
4 clarification (ask_clarify -> numbered text + buttons), 2 context/
deterministic-core guards. Model responses are scripted; tools run
for real so tool-result assertions prove real execution.
"""

import json
from types import SimpleNamespace

import app.llm as llm
from app import agent
from app.tools import ask_clarify, get_pending, record_sale
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID


def _call(call_id, name, args):
    raw = args if isinstance(args, str) else json.dumps(args)
    return SimpleNamespace(id=call_id,
                           function=SimpleNamespace(name=name, arguments=raw))


def _resp(content=None, calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=content, tool_calls=calls or []))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                              total_tokens=2),
    )


def _stub(monkeypatch, tmp_path, script):
    monkeypatch.setattr(llm, "TOKEN_LOG", tmp_path / "usage.log")
    calls = {"n": 0, "messages": []}

    def fake(messages):
        calls["n"] += 1
        calls["messages"] = messages
        item = script[min(calls["n"] - 1, len(script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(llm, "_complete", fake)
    return calls


def _no_llm(monkeypatch):
    """Stub every model seam: exact commands must run fully offline."""
    calls = {"n": 0}

    def count(messages):
        calls["n"] += 1
        raise AssertionError("LLM must not run for keyword commands")

    import app.interpreter as interp
    monkeypatch.setattr(llm, "_complete", count)
    monkeypatch.setattr(interp, "propose_plan", count)
    return calls


def _script(monkeypatch, tmp_path, plans):
    """Stub the planner with fixed plans; returns call counter."""
    import json

    import app.interpreter as interp
    monkeypatch.setattr(llm, "TOKEN_LOG", tmp_path / "usage.log")
    calls = {"n": 0, "contexts": []}

    def fake(messages):
        calls["n"] += 1
        calls["contexts"].append(messages[-1]["content"])
        body = plans[min(calls["n"] - 1, len(plans) - 1)]
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2))

    monkeypatch.setattr(interp, "propose_plan", fake)
    return calls


def _plan(tasks, understood="ok", question=None, options=None):
    return {"understood": understood, "tasks": tasks, "question": question,
            "options": options or []}


def _t(kind, args=None, quote=""):
    return {"kind": kind, "args": args or {}, "quote": quote}


def _tool_texts(calls):
    return [m["content"] for m in calls["messages"]
            if isinstance(m, dict) and m.get("role") == "tool"]


# 1-6: keyword fast path — the LLM must never run ---------------------------

def test_01_all_statements_fast_path(conn, monkeypatch):
    seed_demo(SHOP, conn)
    calls = _no_llm(monkeypatch)
    msg, _ = agent.handle_text(conn, SHOP, "all statements")
    assert calls["n"] == 0
    assert msg.strip()


def test_02_my_reminders_fast_path(conn, monkeypatch):
    seed_demo(SHOP, conn)
    calls = _no_llm(monkeypatch)
    msg, _ = agent.handle_text(conn, SHOP, "my reminders")
    assert calls["n"] == 0
    assert msg.strip()


def test_03_order_stock_fast_path(conn, monkeypatch):
    seed_demo(SHOP, conn)
    calls = _no_llm(monkeypatch)
    msg, _ = agent.handle_text(conn, SHOP, "order stock")
    assert calls["n"] == 0
    assert msg.strip()


def test_04_daily_plan_fast_path(conn, monkeypatch):
    seed_demo(SHOP, conn)
    calls = _no_llm(monkeypatch)
    msg, _ = agent.handle_text(conn, SHOP, "daily plan")
    assert calls["n"] == 0
    assert msg.strip()


def test_05_resend_fast_path(conn, monkeypatch):
    seed_demo(SHOP, conn)
    calls = _no_llm(monkeypatch)
    msg, _ = agent.handle_text(conn, SHOP, "resend")
    assert calls["n"] == 0
    assert msg.strip()


def test_06_demo_help_fast_path(conn, monkeypatch):
    seed_demo(SHOP, conn)
    calls = _no_llm(monkeypatch)
    msg, _ = agent.handle_text(conn, SHOP, "demo help")
    assert calls["n"] == 0
    assert msg.strip()


# 7-14: composite multi-job -> LLM with chaining ----------------------------

def test_07_chain_debtors_then_reminder(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _script(monkeypatch, tmp_path, [_plan(
        [_t("debtors"), _t("draft", {"customer": "Sipho"})],
        understood="debtors plus draft")])
    msg, _ = agent.handle_text(conn, SHOP, "who owes me most and remind them")
    assert calls["n"] == 1  # one plan, both tasks executed
    assert "Sipho" in msg and "R73.00" in msg


def test_08_cash_up_and_low_stock(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _script(monkeypatch, tmp_path, [_plan(
        [_t("cashup"), _t("low_stock")], understood="cash up plus low")])
    msg, _ = agent.handle_text(conn, SHOP, "cash up and show low stock")
    assert calls["n"] == 1
    assert "👂" in msg and "low" in msg.lower()


def test_09_zulu_composite(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _script(monkeypatch, tmp_path, [_plan(
        [_t("debtors"),
         _t("reminder", {"when": "kusasa"})], understood="ok")])
    msg, _ = agent.handle_text(
        conn, SHOP, "ngubani okweletayo kakhulu futhi ngikhumbuze kusasa")
    assert calls["n"] == 1
    assert "R73.00" in msg  # debtors answered in the same reply
    assert "khumbu" in msg  # topic asked, original due kept


def test_10_afrikaans_composite(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _script(monkeypatch, tmp_path, [_plan(
        [_t("debtors"),
         _t("reminder", {"when": "môre"})], understood="ok")])
    msg, _ = agent.handle_text(
        conn, SHOP, "wie skuld my die meeste en herinner my môre")
    assert calls["n"] == 1
    assert "R73.00" in msg
    assert "herinner" in msg.lower() or "khumbu" in msg


def test_11_xhosa_composite(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _script(monkeypatch, tmp_path, [_plan(
        [_t("debtors"),
         _t("reminder", {"when": "ngomso"})], understood="ok")])
    msg, _ = agent.handle_text(
        conn, SHOP, "ngubani onetyala kakhulu kwaye undikhumbuze ngomso")
    assert calls["n"] == 1
    assert "R73.00" in msg and "khumbu" in msg


def test_12_slang_composite(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _script(monkeypatch, tmp_path, [_plan(
        [_t("balance", {"customer": "Sipho"}),
         _t("reminder", {"when": "Friday"})], understood="ok")])
    msg, _ = agent.handle_text(
        conn, SHOP, "sho Sipho's balance and remind him Friday")
    assert calls["n"] == 1
    assert "R73.00" in msg
    assert "remind you about" in msg or "khumbu" in msg


def test_13_statement_plus_reminder_beats_keyword(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _script(monkeypatch, tmp_path, [_plan(
        [_t("statement", {"customer": "Sipho"}),
         _t("reminder", {"when": "Friday", "topic": "to collect"})],
        understood="statement plus reminder")])
    msg, _ = agent.handle_text(
        conn, SHOP, "statement for Sipho and remind me Friday to collect")
    assert calls["n"] == 1  # one plan, no keyword hijack
    assert "Reminder set" in msg
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 1


def test_14_sale_then_cash_up_creates_pending(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _script(monkeypatch, tmp_path, [_plan(
        [_t("sale", {"items": [{"product": "Sasko White Bread 700g",
                                "qty": 2}]}),
         _t("cashup")], understood="sale plus cash up")])
    msg, buttons = agent.handle_text(conn, SHOP, "sell 2 bread then cash up")
    assert calls["n"] == 1
    assert get_pending(conn, SHOP) is not None
    assert buttons == ["✅ Yes", "❌ No"]
    assert "R37.00" in msg  # 2 x R18.50 proposed


# 15-18: ask_clarify -> numbered text + buttons ------------------------------

def test_15_clarify_which_product(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _script(monkeypatch, tmp_path, [_plan(
        [], understood="unclear sale",
        question="Which bread did you sell?",
        options=["White", "Brown", "Rolls"])])
    msg, buttons = agent.handle_text(
        conn, SHOP, "sell bread and also check who owes")
    assert calls["n"] == 1
    assert buttons == ["White", "Brown", "Rolls"]
    assert msg == "Which bread did you sell?"


def test_16_clarify_which_customer(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _script(monkeypatch, tmp_path, [_plan(
        [], understood="unclear payment",
        question="Which customer paid?",
        options=["Sipho", "Thandi"])])
    msg, buttons = agent.handle_text(
        conn, SHOP, "pay R100 and send a statement")
    assert calls["n"] == 1
    assert buttons == ["Sipho", "Thandi"]
    assert msg == "Which customer paid?"


def test_17_chat_full_truncates_button_labels(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    _stub(monkeypatch, tmp_path, [
        _resp(calls=[_call("c1", "ask_clarify",
                           {"question": "Which one?",
                            "options": ["Sasko White Bread 700g extra",
                                        "Brown", "Rolls", "Fourth"]})]),
        _resp("asked"),
    ])
    msg, buttons = llm.chat_full(conn, SHOP, "sell bread")
    assert msg == "asked"
    assert buttons == ["Sasko White Bread 70", "Brown", "Rolls"]


def test_18_ask_clarify_tool_renders_numbered(conn):
    seed_demo(SHOP, conn)
    out = ask_clarify(conn, SHOP, "Which account?",
                      ["Cash", "Petty cash", "Bank", "Dropped"])
    assert out.startswith("❓ Which account?")
    assert "1) Cash" in out and "3) Bank" in out
    assert "Dropped" not in out
    assert "1, 2 or 3" in out


# 19-20: live context + deterministic core -----------------------------------

def test_19_planner_context_carries_shop_facts(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    conn.execute(
        "INSERT INTO reminders (shop_id, due_at, summary) VALUES (?, ?, ?)",
        (SHOP, "2026-10-29T05:30:00", "Take order to the shop"))
    conn.commit()
    calls = _script(monkeypatch, tmp_path, [_plan(
        [_t("chat", {"reply": "noted"})])])
    agent.handle_text(conn, SHOP, "cash up and check the float")
    assert calls["n"] == 1
    context = calls["contexts"][0]
    assert "Sasko White Bread 700g" in context
    assert "Sipho" in context and "R73.00" in context
    assert "Take order to the shop" in context


def test_20_core_never_touches_llm(conn, monkeypatch):
    seed_demo(SHOP, conn)
    record_sale(conn, SHOP, "Sasko White Bread 700g", 2)
    assert get_pending(conn, SHOP) is not None
    calls = _no_llm(monkeypatch)
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert get_pending(conn, SHOP) is None
    assert msg.strip()
    msg, _ = agent.handle_text(conn, SHOP, "undo book")
    assert "No saved book page" in msg
    assert calls["n"] == 0


def test_pending_intact_for_read_plans(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    record_sale(conn, SHOP, "Sasko White Bread 700g", 2)
    before = get_pending(conn, SHOP)
    calls = _script(monkeypatch, tmp_path, [_plan(
        [_t("cashup"), _t("low_stock")], understood="cash up plus low")])
    msg, buttons = agent.handle_text(conn, SHOP, "cash up and show low stock")
    assert calls["n"] == 1  # planner sees the open proposal and works around it
    assert "OPEN STATE" in calls["contexts"][0]
    assert "preview awaiting Yes/No" in calls["contexts"][0]
    assert get_pending(conn, SHOP)["id"] == before["id"]  # untouched
    assert buttons is None
