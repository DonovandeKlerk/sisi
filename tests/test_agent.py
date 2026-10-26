"""Deterministic Yes/No/Undo routing (offline)."""

import pytest

from app.agent import handle_text, try_deterministic
from app.tools import add_credit, get_balance
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID


def test_yes_confirms_and_no_cancels(conn):
    seed_demo(SHOP, conn)
    add_credit(conn, SHOP, "Sipho", [{"product": "Coke 2L", "qty": 1}])
    msg, buttons = try_deterministic(conn, SHOP, "Yes please")
    assert "Saved" in msg and buttons is None
    assert get_balance(conn, SHOP, "Sipho") == "Sipho owes R109.00."

    add_credit(conn, SHOP, "Sipho", [{"product": "Coke 2L", "qty": 1}])
    msg, _ = try_deterministic(conn, SHOP, "no thanks")
    assert msg == "OK, I did not save it."
    assert get_balance(conn, SHOP, "Sipho") == "Sipho owes R109.00."


def test_undo_word_and_case(conn):
    seed_demo(SHOP, conn)
    add_credit(conn, SHOP, "Sipho", [{"product": "Coke 2L", "qty": 1}])
    try_deterministic(conn, SHOP, "yebo")
    msg, _ = try_deterministic(conn, SHOP, "UNDO")
    assert "Undone" in msg
    assert get_balance(conn, SHOP, "Sipho") == "Sipho owes R73.00."


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


def test_other_text_goes_to_llm(conn):
    seed_demo(SHOP, conn)
    assert try_deterministic(conn, SHOP, "Sipho took two bread") is None
    assert try_deterministic(conn, SHOP, "yes but change it") is None
    with pytest.raises(NotImplementedError):
        handle_text(conn, SHOP, "Sipho took two bread")


def test_demo_reset_command(conn):
    seed_demo(SHOP, conn)
    add_credit(conn, SHOP, "Sipho", [{"product": "Coke 2L", "qty": 1}])
    try_deterministic(conn, SHOP, "yes")
    msg, _ = try_deterministic(conn, SHOP, "demo reset")
    assert msg.startswith("Fresh start.")
    assert get_balance(conn, SHOP, "Sipho") == "Sipho owes R73.00."
