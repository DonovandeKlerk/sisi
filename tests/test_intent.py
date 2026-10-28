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
    """Stub the model call and count invocations (the loop swallows errors,
    so raising alone cannot prove the fast path — assert calls == 0)."""
    calls = {"n": 0}

    def count(messages):
        calls["n"] += 1
        raise AssertionError("LLM must not run for keyword commands")

    monkeypatch.setattr(llm, "_complete", count)
    return calls


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
    calls = _stub(monkeypatch, tmp_path, [
        _resp(calls=[_call("c1", "list_debtors", {})]),
        _resp(calls=[_call("c2", "draft_reminder", {"customer": "Sipho"})]),
        _resp("Reminded Sipho."),
    ])
    msg, _ = agent.handle_text(conn, SHOP, "who owes me most and remind them")
    assert calls["n"] == 3
    assert msg == "Reminded Sipho."
    tools = " ".join(_tool_texts(calls))
    assert "Sipho" in tools and "R73.00" in tools


def test_08_cash_up_and_low_stock(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _stub(monkeypatch, tmp_path, [
        _resp(calls=[_call("c1", "cash_up", {})]),
        _resp(calls=[_call("c2", "low_stock", {})]),
        _resp("Done both."),
    ])
    msg, _ = agent.handle_text(conn, SHOP, "cash up and show low stock")
    assert calls["n"] == 3
    assert len(_tool_texts(calls)) == 2
    assert msg == "Done both."


def test_09_zulu_composite(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _stub(monkeypatch, tmp_path, [_resp("Kulungile.")])
    msg, _ = agent.handle_text(
        conn, SHOP, "ngubani okweletayo kakhulu futhi ngikhumbuze kusasa")
    assert calls["n"] >= 1
    assert msg == "Kulungile."


def test_10_afrikaans_composite(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _stub(monkeypatch, tmp_path, [_resp("Reg so.")])
    msg, _ = agent.handle_text(
        conn, SHOP, "wie skuld my die meeste en herinner my môre")
    assert calls["n"] >= 1
    assert msg == "Reg so."


def test_11_xhosa_composite(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _stub(monkeypatch, tmp_path, [_resp("Kulungile.")])
    msg, _ = agent.handle_text(
        conn, SHOP, "ngubani onetyala kakhulu kwaye undikhumbuze ngomso")
    assert calls["n"] >= 1
    assert msg == "Kulungile."


def test_12_slang_composite(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _stub(monkeypatch, tmp_path, [_resp("Sharp.")])
    msg, _ = agent.handle_text(
        conn, SHOP, "sho Sipho's balance and remind him Friday")
    assert calls["n"] >= 1
    assert msg == "Sharp."


def test_13_statement_plus_reminder_beats_keyword(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _stub(monkeypatch, tmp_path, [_resp("Both queued.")])
    msg, _ = agent.handle_text(
        conn, SHOP, "statement for Sipho and remind me Friday to collect")
    assert calls["n"] >= 1
    assert msg == "Both queued."


def test_14_sale_then_cash_up_creates_pending(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _stub(monkeypatch, tmp_path, [
        _resp(calls=[_call("c1", "record_sale",
                           {"product": "Sasko White Bread 700g", "qty": 2})]),
        _resp("Preview above."),
    ])
    msg, _ = agent.handle_text(conn, SHOP, "sell 2 bread then cash up")
    assert calls["n"] == 2
    assert get_pending(conn, SHOP) is not None
    assert msg == "Preview above."


# 15-18: ask_clarify -> numbered text + buttons ------------------------------

def test_15_clarify_which_product(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _stub(monkeypatch, tmp_path, [
        _resp(calls=[_call("c1", "ask_clarify",
                           {"question": "Which bread did you sell?",
                            "options": ["White", "Brown", "Rolls"]})]),
        _resp("❓ Which bread did you sell?\n1) White\n2) Brown\n3) Rolls"),
    ])
    msg, buttons = agent.handle_text(
        conn, SHOP, "sell bread and also check who owes")
    assert buttons == ["White", "Brown", "Rolls"]
    assert "1) White" in msg
    assert "1) White" in " ".join(_tool_texts(calls))


def test_16_clarify_which_customer(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _stub(monkeypatch, tmp_path, [
        _resp(calls=[_call("c1", "ask_clarify",
                           {"question": "Which customer paid?",
                            "options": ["Sipho", "Thandi"]})]),
        _resp("❓ Which customer paid?\n1) Sipho\n2) Thandi"),
    ])
    msg, buttons = agent.handle_text(
        conn, SHOP, "pay R100 and send a statement")
    assert buttons == ["Sipho", "Thandi"]
    assert "2) Thandi" in msg


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

def test_19_system_prompt_carries_shop_facts(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    conn.execute(
        "INSERT INTO reminders (shop_id, due_at, summary) VALUES (?, ?, ?)",
        (SHOP, "2026-10-29T05:30:00", "Take order to the shop"))
    conn.commit()
    calls = _stub(monkeypatch, tmp_path, [_resp("noted")])
    agent.handle_text(conn, SHOP, "cash up and check the float")
    system = calls["messages"][0]["content"]
    assert "Sasko White Bread 700g" in system
    assert "Sipho" in system and "R73.00" in system
    assert "Take order to the shop" in system


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


def test_gate_skips_composite_while_pending(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    record_sale(conn, SHOP, "Sasko White Bread 700g", 2)

    def boom(c, s, t):
        raise AssertionError("composite path must not run while pending")

    monkeypatch.setattr(agent, "route_composite", boom)
    _stub(monkeypatch, tmp_path, [_resp("ok")])
    msg, buttons = agent.handle_text(conn, SHOP, "cash up and show low stock")
    assert msg == "ok"
    assert buttons is None
