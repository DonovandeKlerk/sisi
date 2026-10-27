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


def test_other_text_goes_to_llm(conn, monkeypatch):
    seed_demo(SHOP, conn)
    assert try_deterministic(conn, SHOP, "Sipho took two bread") is None
    assert try_deterministic(conn, SHOP, "yes but change it") is None
    monkeypatch.setattr(agent, "llm_chat", lambda c, s, t: "roger")
    msg, buttons = handle_text(conn, SHOP, "Sipho took two bread")
    assert (msg, buttons) == ("roger", None)


def test_new_preview_gets_yes_no_buttons(conn, monkeypatch):
    seed_demo(SHOP, conn)

    def fake_llm(c, shop_id, text):
        add_credit(c, shop_id, "Sipho", [{"product": "Coke 2L", "qty": 1}])
        return "Save?"

    monkeypatch.setattr(agent, "llm_chat", fake_llm)
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
