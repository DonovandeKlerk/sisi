"""LLM loop with the model stubbed out (offline)."""

import json
from types import SimpleNamespace

import pytest

import app.llm as llm
from app.db import init_db  # noqa: F401 - re-exported for clarity
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID


def _call(call_id, name, args):
    raw = args if isinstance(args, str) else json.dumps(args)
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=raw))


def _resp(content=None, calls=None, pt=10, ct=5):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls or []))],
        usage=SimpleNamespace(prompt_tokens=pt, completion_tokens=ct, total_tokens=pt + ct),
    )


def _stub(monkeypatch, tmp_path, script):
    """script: list of responses (or exceptions) returned by successive _complete calls."""
    monkeypatch.setattr(llm, "TOKEN_LOG", tmp_path / "usage.log")
    calls = {"n": 0, "last_messages": None}

    def fake(messages):
        calls["n"] += 1
        calls["last_messages"] = messages
        item = script[min(calls["n"] - 1, len(script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(llm, "_complete", fake)
    return calls


def test_tool_loop_proposes_credit(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    tool_calls = [_call("c1", "add_credit", {
        "customer": "Sipho",
        "items": [{"product": "Sasko White Bread 700g", "qty": 2},
                  {"product": "Coke 2L", "qty": 1}],
    })]
    calls = _stub(monkeypatch, tmp_path, [
        _resp(calls=tool_calls),
        _resp("Save: Sipho +R73.00 on the book?"),
    ])
    out = llm.chat(conn, SHOP, "Sipho took two Sasko bread and a 2L Coke")
    assert out == "Save: Sipho +R73.00 on the book?"
    assert calls["n"] == 2
    pending = conn.execute(
        "SELECT * FROM pending_actions WHERE shop_id = ? AND status = 'pending'", (SHOP,)).fetchone()
    assert pending is not None and "New balance R146.00" in pending["preview_text"]
    tool_msgs = [m for m in calls["last_messages"] if isinstance(m, dict) and m.get("role") == "tool"]
    assert len(tool_msgs) == 1 and tool_msgs[0]["tool_call_id"] == "c1"
    log = (tmp_path / "usage.log").read_text().strip().split("\n")
    assert len(log) == 2 and '"total_tokens": 15' in log[0]


def test_history_saved_and_capped(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    _stub(monkeypatch, tmp_path, [_resp("ok")])
    llm.chat(conn, SHOP, "hello")
    rows = conn.execute(
        "SELECT role FROM conversation_turns WHERE shop_id = ? ORDER BY id", (SHOP,)).fetchall()
    assert [r["role"] for r in rows] == ["user", "assistant"]
    for i in range(15):
        llm.chat(conn, SHOP, f"msg {i}")
    count = conn.execute(
        "SELECT COUNT(*) c FROM conversation_turns WHERE shop_id = ?", (SHOP,)).fetchone()["c"]
    assert count == llm.HISTORY_TURNS


def test_bad_tool_args_surfaced_to_model(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _stub(monkeypatch, tmp_path, [
        _resp(calls=[_call("c1", "get_balance", "{oops")]),
        _resp(calls=[_call("c2", "no_such_tool", {})]),
        _resp("done"),
    ])
    assert llm.chat(conn, SHOP, "balances?") == "done"
    assert calls["n"] == 3
    tool_msgs = [m for m in calls["last_messages"] if isinstance(m, dict) and m.get("role") == "tool"]
    assert "not JSON" in tool_msgs[0]["content"]
    assert "Unknown tool" in tool_msgs[1]["content"]


def test_max_rounds_falls_back(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _stub(monkeypatch, tmp_path, [
        _resp(calls=[_call("c1", "list_debtors", {})])] * 10)
    out = llm.chat(conn, SHOP, "debtors?")
    assert out == "That one is tricky. Ask me again in fewer words?"
    assert calls["n"] == llm.MAX_TOOL_ROUNDS


def test_api_error_is_graceful(conn, monkeypatch, tmp_path, capsys):
    seed_demo(SHOP, conn)
    err = RuntimeError("connection reset")
    err.body = {"error": {"message": "boom"}}  # full body must surface in logs
    _stub(monkeypatch, tmp_path, [err])
    out = llm.chat(conn, SHOP, "hi")
    assert out == "My signal dropped just now. Please say that again."
    assert "boom" in capsys.readouterr().err


def test_missing_key_raises_clearly(monkeypatch):
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MODEL_API_KEY"):
        llm._get_client()
