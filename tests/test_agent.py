"""Deterministic Yes/No/Undo routing (offline)."""

from app import agent
from app.agent import handle_text, try_deterministic
from app.tools import add_credit, get_balance
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID


def test_yes_confirms_and_no_cancels(conn):
    seed_demo(SHOP, conn)
    add_credit(conn, SHOP, "Sipho", [{"product": "Coke 2L", "qty": 1}])
    msg, buttons = try_deterministic(conn, SHOP, "Yes please")
    assert "Saved" in msg and buttons is None
    assert get_balance(conn, SHOP, "Sipho") == "💳 *Sipho* owes R109.00."

    add_credit(conn, SHOP, "Sipho", [{"product": "Coke 2L", "qty": 1}])
    msg, _ = try_deterministic(conn, SHOP, "no thanks")
    assert msg == "OK, I did not save it."
    assert get_balance(conn, SHOP, "Sipho") == "💳 *Sipho* owes R109.00."


def test_undo_word_and_case(conn):
    seed_demo(SHOP, conn)
    add_credit(conn, SHOP, "Sipho", [{"product": "Coke 2L", "qty": 1}])
    try_deterministic(conn, SHOP, "yebo")
    msg, _ = try_deterministic(conn, SHOP, "UNDO")
    assert "Undone" in msg
    assert get_balance(conn, SHOP, "Sipho") == "💳 *Sipho* owes R73.00."


def test_multilingual_yes_no(conn):
    seed_demo(SHOP, conn)
    for word in ["yebo", "ja", "ewe", "sharp", "ok"]:
        add_credit(conn, SHOP, "Sipho", [{"product": "Coke 2L", "qty": 1}])
        msg, _ = try_deterministic(conn, SHOP, word)
        assert "Saved" in msg, word
        try_deterministic(conn, SHOP, "undo")
    for word in ["hayi", "cha", "nee"]:
        add_credit(conn, SHOP, "Sipho", [{"product": "Coke 2L", "qty": 1}])
        msg, _ = try_deterministic(conn, SHOP, word)
        assert msg == "OK, I did not save it.", word


def _stub_plan(monkeypatch, tasks, understood="test reading"):
    import json
    from types import SimpleNamespace

    import app.interpreter as interp

    def fake(messages):
        body = {"understood": understood, "tasks": tasks,
                "question": None, "options": []}
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2))

    monkeypatch.setattr(interp, "propose_plan", fake)


def test_other_text_goes_to_planner(conn, monkeypatch):
    seed_demo(SHOP, conn)
    _stub_plan(monkeypatch, [{"kind": "chat", "args": {"reply": "roger"},
                              "quote": ""}])
    msg, buttons = handle_text(conn, SHOP, "Sipho took two bread")
    assert (msg, buttons) == ("roger", None)


def test_new_preview_gets_yes_no_buttons(conn, monkeypatch):
    seed_demo(SHOP, conn)
    _stub_plan(monkeypatch, [{"kind": "credit",
                              "args": {"customer": "Sipho", "items": [
                                  {"product": "Coke 2L", "qty": 1}]},
                              "quote": "Sipho took a Coke"}])
    msg, buttons = handle_text(conn, SHOP, "Sipho took a Coke")
    assert buttons == ["Yes", "No"]
    msg, buttons = handle_text(conn, SHOP, "yes")
    assert buttons is None and "Saved" in msg


def test_demo_reset_command(conn):
    seed_demo(SHOP, conn)
    add_credit(conn, SHOP, "Sipho", [{"product": "Coke 2L", "qty": 1}])
    try_deterministic(conn, SHOP, "yes")
    msg, _ = try_deterministic(conn, SHOP, "demo reset")
    assert msg.startswith("🏪 *Let's set up your shop.*")
    assert "Step 1 of 7" in msg
    assert conn.execute("SELECT COUNT(*) n FROM customers WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0


def test_delete_my_data_command(conn):
    seed_demo(SHOP, conn)
    msg, buttons = try_deterministic(conn, SHOP, "Please delete my data")
    assert msg.startswith("✅ *Done.*") and buttons is None
    assert get_balance(conn, SHOP, "Sipho") == "No one named Sipho is on the book."


def test_demo_help_lists_every_command(conn):
    seed_demo(SHOP, conn)
    msg, buttons = try_deterministic(conn, SHOP, "demo help")
    assert buttons is None
    for word in ["stock list", "add stock", "sale", "credit sale", "who owes",
                 "cash-up", "low stock", "restock", "expiry photo", "compliance",
                 "scam", "delete my data", "undo"]:
        assert word in msg.lower(), word


def _no_planner(monkeypatch):
    import app.interpreter as interp

    def boom(messages):
        raise AssertionError("planner must not be called")

    monkeypatch.setattr(interp, "propose_plan", boom)


def test_fast_social_skips_planner(conn, monkeypatch):
    from app.setup import skip_setup
    from app.tools import ensure_shop
    ensure_shop(conn, SHOP)
    skip_setup(conn, SHOP)
    _no_planner(monkeypatch)
    msg, _ = handle_text(conn, SHOP, "good morning")
    assert "Main menu" in msg
    msg, _ = handle_text(conn, SHOP, "how are you")
    assert "👋" in msg
    msg, _ = handle_text(conn, SHOP, "thanks")
    assert "👍" in msg
    msg, _ = handle_text(conn, SHOP, "thanks sisi")
    assert "👋" in msg  # filler strips to the bare name: greeted, not planned


def test_fast_lookups_skip_planner(conn, monkeypatch):
    seed_demo(SHOP, conn)
    _no_planner(monkeypatch)
    msg, _ = handle_text(conn, SHOP, "who owes me")
    assert "On the book" in msg and "Sipho" in msg
    msg, _ = handle_text(conn, SHOP, "How much does Sipho owe?")
    assert msg == "💳 *Sipho* owes R73.00."
    msg, _ = handle_text(conn, SHOP, "cash in hand")
    assert msg.startswith("💰 *Cash*")
    msg, _ = handle_text(conn, SHOP, "stock list")
    assert "stock on hand" in msg
    msg, _ = handle_text(conn, SHOP, "low stock")
    assert "Low stock" in msg or "Nothing is running low" in msg


def test_fast_lookup_yields_to_multi_intent(conn, monkeypatch):
    import json
    from types import SimpleNamespace

    import app.interpreter as interp
    seed_demo(SHOP, conn)
    calls = {"n": 0}

    def fake(messages):
        calls["n"] += 1
        body = {"understood": "ok",
                "tasks": [{"kind": "debtors", "args": {}, "quote": ""}],
                "question": None, "options": []}
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2))

    monkeypatch.setattr(interp, "propose_plan", fake)
    msg, _ = handle_text(conn, SHOP, "who owes me most and remind them")
    assert calls["n"] == 1  # composite still reaches the planner
    assert "On the book" in msg
