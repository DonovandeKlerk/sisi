"""Standing reply-style rule, enforced on every deterministic reply.

Emoji section headings, *bold* labels, one item per line, blank line between
sections, R0.00 amounts, warnings/errors/confirms marked. No line may top
120 characters; anything over 300 characters must have line breaks.
"""

from app.agent import DEMO_HELP, handle_text, handle_voice, try_deterministic
from app.main import CONSENT_LINE, UNSUPPORTED_REPLY
from app.tools import (
    add_credit,
    add_stock,
    best_sellers,
    cancel_pending,
    cash_up,
    check_expiry_photo,
    compliance_guide,
    confirm_pending,
    delete_my_data,
    demo_reset,
    draft_reminder,
    get_balance,
    get_pending,
    list_debtors,
    list_stock,
    low_stock,
    read_slip_photo,
    record_payment,
    record_sale,
    restock_list,
    set_price,
    undo_last,
)
from app.voice import CANT_HEAR
from app.vision import CANT_READ
from app.whatsapp import split_message
from seed.seed_demo import DEMO_SHOP_ID, seed_demo
from seed.seed_random import DEMO_NUMBER, seed_random_shop

SHOP = DEMO_SHOP_ID


def assert_format(text):
    assert isinstance(text, str) and text.strip()
    for line in text.split("\n"):
        assert len(line) <= 120, line
    if len(text) > 300:
        assert "\n" in text
    return text


def test_stock_list_format(conn):
    seed_random_shop(DEMO_NUMBER, conn, seed=7)
    out = assert_format(list_stock(conn, DEMO_NUMBER))
    assert out.startswith("📦 *Sisi Spaza: stock on hand*")
    assert "items · worth R" in out
    for head in ["🍞 *Bread*", "🥛 *Dairy*", "🥤 *Drinks*", "🛒 *Groceries*",
                 "🧼 *Household*", "📱 *Airtime & data*"]:
        assert head in out, head
    assert "⚠️ *Running low*" in out
    assert out.rstrip().endswith("Reply 'restock' for an order list")
    assert "❌" in out  # seeded out-of-stock item flagged inline


def test_stock_list_empty_and_full_shelves(conn):
    conn.execute("INSERT INTO shops (shop_id, name) VALUES ('s9', 'Empty Shop')")
    out = assert_format(list_stock(conn, "s9"))
    assert out.startswith("📦 *Empty Shop: stock on hand*")
    assert "add 20 bread at R18" in out
    seed_demo(SHOP, conn)
    conn.execute("UPDATE products SET stock_qty = 99 WHERE shop_id = ?", (SHOP,))
    out = assert_format(list_stock(conn, SHOP))
    assert "✅ Nothing is running low." in out


def test_debtors_and_balances_format(conn):
    seed_demo(SHOP, conn)
    out = assert_format(list_debtors(conn, SHOP))
    assert out.startswith("💳 *On the book*") and "\n\nTotal: R" in out
    assert assert_format(get_balance(conn, SHOP, "Sipho")) == "💳 *Sipho* owes R73.00."
    assert get_balance(conn, SHOP, "Nobody") == "No one named Nobody is on the book."
    assert_format(draft_reminder(conn, SHOP, "Sipho"))
    assert "Forward this yourself" in draft_reminder(conn, SHOP, "Sipho")


def test_cash_up_format_with_best_sellers(conn):
    seed_demo(SHOP, conn)
    assert_format(cash_up(conn, SHOP)).startswith("💰 *Cash-up")
    record_sale(conn, SHOP, "Coke 2L", 2)
    confirm_pending(conn, SHOP)
    record_payment(conn, SHOP, "Sipho", 5000)
    confirm_pending(conn, SHOP)
    out = assert_format(cash_up(conn, SHOP))
    assert "💰 *Cash-up" in out and "💵 *In the drawer:* R122.00" in out
    assert "⭐ *Top sellers (7 days)*" in out
    assert "Coke 2L — 2 sold, R72.00" in out


def test_best_sellers_low_and_restock_format(conn):
    seed_demo(SHOP, conn)
    assert best_sellers(conn, SHOP) == "No sales in the last 7 days yet."
    record_sale(conn, SHOP, "Coke 2L", 2)
    confirm_pending(conn, SHOP)
    out = assert_format(best_sellers(conn, SHOP))
    assert out.startswith("⭐ *Top sellers (last 7 days)*")
    out = assert_format(low_stock(conn, SHOP))
    assert out.startswith("⚠️ *Low stock*")
    out = assert_format(restock_list(conn, SHOP))
    assert out.startswith("🛒 *Buy tomorrow*") and "*Devland*" in out


def test_write_previews_and_confirms_format(conn):
    seed_demo(SHOP, conn)
    previews = [
        add_credit(conn, SHOP, "Sipho", [{"product": "Coke 2L", "qty": 1}]),
        record_sale(conn, SHOP, "Coke 2L", 2),
        record_sale(conn, SHOP, "Coke 2L", 1, buyer="Sipho"),
        record_payment(conn, SHOP, "Sipho", 5000),
        add_stock(conn, SHOP, "Coke 2L", 6),
        add_stock(conn, SHOP, "New Tin", 6, cost_cents=1900, price_cents=2400),
        set_price(conn, SHOP, "Coke 2L", 3700),
    ]
    for p in previews:
        assert assert_format(p).startswith("📝 *")
    assert "New: New Tin × 6 (cost R19.00, sell R24.00)" in previews[5]
    saved = assert_format(confirm_pending(conn, SHOP))
    assert saved.startswith("✅ *Price saved*") and "R37.00" in saved
    add_credit(conn, SHOP, "Sipho", [{"product": "Coke 2L", "qty": 1}])
    assert assert_format(confirm_pending(conn, SHOP)).startswith("✅ *Saved*")
    assert assert_format(undo_last(conn, SHOP)).startswith("Undone:")


def test_expiry_and_slip_format(conn):
    seed_demo(SHOP, conn)
    out = assert_format(check_expiry_photo(conn, SHOP, [
        {"product": "Milk 1L", "qty": 2, "expiry_date": "2020-01-01"},
        {"product": "Yoghurt", "qty": 1, "expiry_date": "2999-01-01"},
    ]))
    assert out.startswith("⚠️ *Expiry check*") and out.endswith("Save this note?")
    saved = assert_format(confirm_pending(conn, SHOP))
    assert saved.startswith("✅ *Expiry note saved*")
    out = assert_format(read_slip_photo(conn, SHOP, [
        {"product": "Coke 2L", "qty": 6, "cost_cents": 3000},
        {"product": "New Tin", "qty": 3},
    ], wholesaler="Jumbo"))
    assert out.startswith("📝 *Save slip to stock?*")
    assert assert_format(confirm_pending(conn, SHOP)).startswith("✅ *Slip saved*")


def test_compliance_and_help_format(conn):
    seed_demo(SHOP, conn)
    for topic in ["overview", "register", "permit", "coa", "fund", "scam"]:
        out = assert_format(compliance_guide(conn, SHOP, topic))
        assert out.startswith(("🏛️ *", "💰 *", "⚠️ *")), topic
    assert "spazashopfund.co.za" in compliance_guide(conn, SHOP)
    out = assert_format(DEMO_HELP)
    assert out.startswith("❓ *Demo help") and "🔒 *Privacy:*" in out


def test_questions_and_errors_format(conn):
    seed_demo(SHOP, conn)
    assert assert_format(add_stock(conn, SHOP, "bread", 2)).startswith("❓ *Which one?*")
    assert assert_format(set_price(conn, SHOP, "Unicorn", 100)).startswith("❓ *")
    assert assert_format(record_sale(conn, SHOP, "Milk 1L", 0)) == "How many?"
    assert "🔒" not in assert_format(delete_my_data(conn, SHOP))
    assert assert_format(demo_reset(conn, SHOP)).startswith("🎉 Fresh start.")
    assert_format(CONSENT_LINE)
    assert_format(UNSUPPORTED_REPLY)
    assert_format(CANT_HEAR)
    assert_format(CANT_READ)


def test_await_price_replies_format(conn):
    seed_demo(SHOP, conn)
    out = assert_format(record_sale(conn, SHOP, "Vetkoek", 2, buyer="Sipho"))
    assert out.startswith("📝 *Vetkoek isn't on your list yet.*")
    msg, _ = handle_text(conn, SHOP, "yes")
    assert assert_format(msg).startswith("Still need the price")
    msg, _ = handle_text(conn, SHOP, "vetkoek R15 and milk R22")
    assert assert_format(msg).startswith("One price at a time")
    msg, _ = handle_text(conn, SHOP, "no")
    assert assert_format(msg) == "OK, I dropped that sale."
    assert get_pending(conn, SHOP) is None
    assert try_deterministic(conn, SHOP, "yes")[0] == "There is nothing waiting for Yes."
    assert_format(cancel_pending(conn, SHOP))


def test_heard_line_truncates_long_transcripts(conn, monkeypatch):
    seed_demo(SHOP, conn)
    monkeypatch.setattr("app.agent.transcribe_audio", lambda *a, **k: "x" * 200)
    monkeypatch.setattr("app.agent.route_to_llm", lambda c, s, t: "ok")
    _, reply, _ = handle_voice(conn, SHOP, b"fake", ".ogg")
    assert assert_format(reply).split("\n")[0].endswith("…”")


def test_split_message_at_section_boundaries():
    assert split_message("short") == ["short"]
    body = "s1\n\n" + "x" * 1400 + "\n\n" + "y" * 200
    chunks = split_message(body)
    assert len(chunks) == 2 and all(len(c) <= 1500 for c in chunks)
    assert "\n\n".join(chunks) == body
    assert len(split_message("word " * 400)) >= 2  # no sections: word-split
