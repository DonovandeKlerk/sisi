"""End-of-day book pages: preview, save, correct, undo book."""

from test_format import assert_format

from app.agent import handle_text
from app.db import today_str
from app.tools import (
    cash_balances,
    confirm_pending,
    get_balance,
    get_pending,
    propose_book_page,
    undo_last,
)
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID


def _book(page_total=13300):
    return {
        "date": "2026-10-21",
        "page_total_cents": page_total,
        "crossed_out": 1,
        "lines": [
            {"line_no": 1, "text": "2 coke 72", "item": "coke", "qty": 2,
             "price_cents": 7200, "kind": "cash", "customer": ""},
            {"line_no": 2, "text": "1 stny 34", "item": "stny", "qty": 1,
             "price_cents": 3400, "kind": "cash", "customer": ""},
            {"line_no": 3, "text": "1 milk Thandi", "item": "milk", "qty": 1,
             "price_cents": None, "kind": "credit", "customer": "Thandi"},
            {"line_no": 4, "text": "Sipho paid 50", "item": "", "qty": 1,
             "price_cents": 5000, "kind": "payment", "customer": "Sipho"},
            {"line_no": 5, "text": "???", "item": "", "qty": 1,
             "price_cents": None, "kind": "unreadable", "customer": ""},
            {"line_no": 6, "text": "2 brd Sipho", "item": "brd", "qty": 2,
             "price_cents": None, "kind": "credit", "customer": "Sipho"},
            {"line_no": 7, "text": "1 unicorn", "item": "unicorn", "qty": 1,
             "price_cents": None, "kind": "cash", "customer": ""},
        ],
    }


def test_book_preview_sections(conn):
    seed_demo(SHOP, conn)
    out = assert_format(propose_book_page(conn, SHOP, _book()))
    assert out.startswith("📒 *Today's book: 21 Oct*")
    assert "💵 *Cash sales*" in out
    assert "• 2x Coke 2L = R72.00" in out
    assert "• 1x Stoney 2L = R34.00 ⚠️ (list R33.00)" in out
    assert "📝 *Credit*" in out
    assert "• Thandi: 1x Fresh Milk 1L = R27.00 → owes R72.50" in out
    assert "❓ *Couldn't read*" in out
    assert "line 5" in out and "line 6" in out and "line 7" in out
    assert "• Cash sales R106.00" in out
    assert "• Credit given R27.00" in out
    assert "• Credit payments R50.00" in out
    assert "Expected cash in hand R156.00" in out
    assert "✅ Matches your page total R133.00" in out
    assert "Skipped 1 crossed-out line." in out
    assert "⏳ Answer the open lines first" in out
    assert out.rstrip().endswith("Reply 'line 6 sasko'")


def test_book_page_total_mismatch(conn):
    seed_demo(SHOP, conn)
    out = propose_book_page(conn, SHOP, _book(page_total=14000))
    assert "⚠️ Page says R140.00, lines add to R133.00" in out


def test_book_confirm_applies_everything(conn):
    seed_demo(SHOP, conn)
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    propose_book_page(conn, SHOP, _book())
    msg, _ = handle_text(conn, SHOP, "line 6 sasko")
    assert "• Sipho: 2x Sasko White Bread 700g = R37.00" in msg
    saved = assert_format(confirm_pending(conn, SHOP))
    assert saved.startswith("✅ *Book saved*")
    assert "⚠️ *Low stock*" in saved or "Stock looks fine" in saved
    assert "💰 *Cash-up" in saved
    coke = conn.execute("SELECT stock_qty FROM products WHERE shop_id = ? AND name = 'Coke 2L'",
                        (SHOP,)).fetchone()["stock_qty"]
    assert coke == 3
    assert "• Till R156.00" in cash_balances(conn, SHOP)
    assert get_balance(conn, SHOP, "Thandi") == "💳 *Thandi* owes R72.50."
    assert get_balance(conn, SHOP, "Sipho") == "💳 *Sipho* owes R60.00."


def test_undo_book_reverses_page(conn):
    seed_demo(SHOP, conn)
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    propose_book_page(conn, SHOP, _book())
    handle_text(conn, SHOP, "line 6 sasko")
    confirm_pending(conn, SHOP)
    msg, _ = handle_text(conn, SHOP, "undo book")
    assert msg == "Undone: removed the book page from 21 Oct."
    coke = conn.execute("SELECT stock_qty FROM products WHERE shop_id = ? AND name = 'Coke 2L'",
                        (SHOP,)).fetchone()["stock_qty"]
    assert coke == 5
    assert "• Till R0.00" in cash_balances(conn, SHOP)
    assert get_balance(conn, SHOP, "Thandi") == "💳 *Thandi* owes R45.50."
    assert get_balance(conn, SHOP, "Sipho") == "💳 *Sipho* owes R73.00."


def test_undo_book_skips_newer_actions(conn):
    from app.tools import record_sale

    seed_demo(SHOP, conn)
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    propose_book_page(conn, SHOP, _book())
    handle_text(conn, SHOP, "line 6 sasko")
    confirm_pending(conn, SHOP)
    record_sale(conn, SHOP, "Simba Chips 120g", 1)
    confirm_pending(conn, SHOP)
    msg, _ = handle_text(conn, SHOP, "undo book")
    assert "21 Oct" in msg
    simba = conn.execute("SELECT stock_qty FROM products WHERE shop_id = ? "
                         "AND name = 'Simba Chips 120g'", (SHOP,)).fetchone()["stock_qty"]
    assert simba == 2  # newer sale intact
    assert "• Till R24.00" in cash_balances(conn, SHOP)  # book moves reversed only


def test_correct_book_line(conn):
    seed_demo(SHOP, conn)
    propose_book_page(conn, SHOP, _book())
    msg, _ = handle_text(conn, SHOP, "line 2 is 2 stoney")
    assert "• 2x Stoney 2L = R66.00" in msg
    assert "• Cash sales R138.00" in msg
    msg, _ = handle_text(conn, SHOP, "line 6 is 2 sasko Sipho")
    assert "• Sipho: 2x Sasko White Bread 700g = R37.00" in msg
    msg, _ = handle_text(conn, SHOP, "line 4 is Sipho paid 40")
    assert "• Credit payments R40.00" in msg
    msg, _ = handle_text(conn, SHOP, "line 9 is 2 bread")
    assert "isn't on this page" in msg
    assert get_pending(conn, SHOP) is not None  # still one preview


def test_book_nothing_usable(conn):
    seed_demo(SHOP, conn)
    out = propose_book_page(conn, SHOP, {"date": "", "page_total_cents": None,
                                         "crossed_out": 0, "lines": [
        {"line_no": 1, "text": "???", "item": "", "qty": 1,
         "price_cents": None, "kind": "unreadable", "customer": ""}]})
    assert "Nothing to save" in out
    assert get_pending(conn, SHOP) is None


def _set_en(conn):
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?", (SHOP,))
    conn.commit()


def _coke_stock(conn):
    return conn.execute("SELECT stock_qty FROM products WHERE shop_id = ? "
                        "AND name = 'Coke 2L'", (SHOP,)).fetchone()["stock_qty"]


def test_book_yes_waits_for_answers(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    propose_book_page(conn, SHOP, _book())
    gate = assert_format(confirm_pending(conn, SHOP))
    assert "⏳ Answer the open lines first" in gate
    assert "line 6" in gate and "sasko" in gate.lower()
    assert get_pending(conn, SHOP) is not None  # nothing saved
    assert _coke_stock(conn) == 5
    msg, _ = handle_text(conn, SHOP, "line 6 sasko")
    assert "• Sipho: 2x Sasko White Bread 700g = R37.00" in msg
    saved = assert_format(confirm_pending(conn, SHOP))
    assert saved.startswith("✅ *Book saved*")
    assert get_balance(conn, SHOP, "Sipho") == "💳 *Sipho* owes R60.00."


def test_book_two_ambiguous_lines_asked_in_order(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    book = _book()
    book["lines"] = [ln for ln in book["lines"] if ln["line_no"] != 7] + [
        {"line_no": 7, "text": "1 brd", "item": "brd", "qty": 1,
         "price_cents": None, "kind": "cash", "customer": ""},
    ]
    out = propose_book_page(conn, SHOP, book)
    assert out.rstrip().endswith("Reply 'line 6 sasko'")
    assert "line 6 — which one?" in confirm_pending(conn, SHOP)
    msg, _ = handle_text(conn, SHOP, "line 6 is 2 sasko Sipho")
    assert "Reply 'line 7 sasko'" in msg  # next question surfaces
    assert "line 7 — which one?" in confirm_pending(conn, SHOP)
    msg, _ = handle_text(conn, SHOP, "line 7 brown")
    assert "• 1x Albany Brown Bread 700g = R19.00" in msg
    assert assert_format(confirm_pending(conn, SHOP)).startswith(
        "✅ *Book saved*")


def test_book_answer_keeps_written_price(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    out = propose_book_page(conn, SHOP, {
        "date": "2026-10-21", "page_total_cents": None, "crossed_out": 0,
        "lines": [{"line_no": 3, "text": "2 brd 40", "item": "brd", "qty": 2,
                   "price_cents": 4000, "kind": "cash", "customer": ""}]})
    assert "line 3 — which one?" in out
    msg, _ = handle_text(conn, SHOP, "line 3 brown")
    assert "• 2x Albany Brown Bread 700g = R40.00" in msg
    assert assert_format(confirm_pending(conn, SHOP)).startswith(
        "✅ *Book saved*")
    alb = conn.execute("SELECT stock_qty FROM products WHERE shop_id = ? "
                       "AND name = 'Albany Brown Bread 700g'",
                       (SHOP,)).fetchone()["stock_qty"]
    assert alb == 6
    assert "• Till R40.00" in cash_balances(conn, SHOP)


def test_book_legacy_pending_blocks_yes(conn):
    import json

    seed_demo(SHOP, conn)
    _set_en(conn)
    propose_book_page(conn, SHOP, _book())
    row = get_pending(conn, SHOP)
    payload = json.loads(row["payload_json"])
    del payload["awaiting"]  # pre-migration row
    conn.execute("UPDATE pending_actions SET payload_json = ? WHERE id = ?",
                 (json.dumps(payload), row["id"]))
    conn.commit()
    gate = assert_format(confirm_pending(conn, SHOP))
    assert "Lines 6 still need answers" in gate
    assert "line 6 is 2 brown bread" in gate
    assert get_pending(conn, SHOP) is not None
    msg, _ = handle_text(conn, SHOP, "line 6 is 2 sasko Sipho")
    assert "2x Sasko White Bread 700g" in msg
    assert confirm_pending(conn, SHOP).startswith("✅ *Book saved*")


def test_book_which_prefixes_match_translations():
    from app.i18n import STRINGS
    from app.tools import _BOOK_WHICH_PREFIXES

    assert len(_BOOK_WHICH_PREFIXES) == 4
    for lang, prefix in (("en", "which one:"), ("zu", "ikuphi:"),
                         ("af", "watter een:"), ("xh", "yeyiphi:")):
        rendered = STRINGS[lang]["book_reason_which"].format(names="a, b")
        assert rendered.lower().startswith(prefix), lang
        assert prefix in _BOOK_WHICH_PREFIXES
