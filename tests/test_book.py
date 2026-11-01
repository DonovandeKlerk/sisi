"""End-of-day book pages: preview, save, correct, undo book."""

from test_format import assert_format

from app.agent import handle_text, take_buttons, take_media
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
    handle_text(conn, SHOP, "skip line 7")  # unicorn: no match, skip it
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
    handle_text(conn, SHOP, "skip line 7")
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
    handle_text(conn, SHOP, "skip line 7")
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
    handle_text(conn, SHOP, "skip line 7")  # unicorn: no match, skip it
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
    assert "Lines 6, 7 still need answers" in gate
    assert "line 6 is 2 brown bread" in gate
    assert get_pending(conn, SHOP) is not None
    msg, _ = handle_text(conn, SHOP, "line 6 is 2 sasko Sipho")
    assert "2x Sasko White Bread 700g" in msg
    handle_text(conn, SHOP, "skip line 7")
    assert confirm_pending(conn, SHOP).startswith("✅ *Book saved*")


def test_book_which_prefixes_match_translations():
    from app.i18n import STRINGS
    from app.tools import _BOOK_NOSHELF, _BOOK_WHICH_PREFIXES

    assert len(_BOOK_WHICH_PREFIXES) == 4
    for lang, prefix in (("en", "which one:"), ("zu", "ikuphi:"),
                         ("af", "watter een:"), ("xh", "yeyiphi:")):
        rendered = STRINGS[lang]["book_reason_which"].format(names="a, b")
        assert rendered.lower().startswith(prefix), lang
        assert prefix in _BOOK_WHICH_PREFIXES
    assert len(_BOOK_NOSHELF) == 4
    for lang in ("en", "zu", "af", "xh"):
        assert STRINGS[lang]["book_reason_noshelf"] in _BOOK_NOSHELF, lang


def _two_ambiguous():
    book = _book()
    book["lines"] = [ln for ln in book["lines"] if ln["line_no"] != 7] + [
        {"line_no": 7, "text": "1 brd", "item": "brd", "qty": 1,
         "price_cents": None, "kind": "cash", "customer": ""},
    ]
    return book


def test_book_tap_full_label_binds_first(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)
    propose_book_page(conn, SHOP, _two_ambiguous())
    msg, buttons = handle_text(conn, SHOP, "Albany Brown Bread 700g")
    assert buttons is None
    assert "• Sipho: 2x Albany Brown Bread 700g = R38.00" in msg
    queued = take_buttons(SHOP)
    assert len(queued) == 1
    assert "Line 7" in queued[0]["body"]
    assert queued[0]["buttons"] == ["Sasko White Bread 700g",
                                   "Albany Brown Bread 700g"]


def test_book_tap_short_word_binds(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)
    propose_book_page(conn, SHOP, _book())
    msg, _ = handle_text(conn, SHOP, "brown")
    assert "• Sipho: 2x Albany Brown Bread 700g = R38.00" in msg
    assert take_buttons(SHOP) == []  # all answered, no next question


def test_book_tap_ambiguous_reasks(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)
    propose_book_page(conn, SHOP, _book())
    msg, _ = handle_text(conn, SHOP, "bread")
    assert "line 6 — which one?" in msg
    assert "Answer the open lines first" in confirm_pending(conn, SHOP)
    take_buttons(SHOP)


def test_book_nomatch_suggests_and_binds(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)
    out = propose_book_page(conn, SHOP, {
        "date": "2026-10-21", "page_total_cents": None, "crossed_out": 0,
        "lines": [{"line_no": 6, "text": "2 cooldrinks 36", "item": "cooldrinks",
                   "qty": 2, "price_cents": 3600, "kind": "cash",
                   "customer": ""}]})
    assert "line 6 — which one?" in out
    assert "Answer the open lines first" in confirm_pending(conn, SHOP)
    msg, _ = handle_text(conn, SHOP, "Stoney 2L")
    assert "• 2x Stoney 2L = R36.00" in msg
    assert assert_format(confirm_pending(conn, SHOP)).startswith(
        "✅ *Book saved*")
    stoney = conn.execute("SELECT stock_qty FROM products WHERE shop_id = ? "
                          "AND name = 'Stoney 2L'",
                          (SHOP,)).fetchone()["stock_qty"]
    assert stoney == 6
    assert "• Till R36.00" in cash_balances(conn, SHOP)


def test_book_nomatch_without_suggestions_then_skip(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)
    out = propose_book_page(conn, SHOP, _book())
    assert "what is this?" not in out  # first question is still line 6
    handle_text(conn, SHOP, "line 6 sasko")
    take_buttons(SHOP)
    assert "what is this?" in confirm_pending(conn, SHOP)  # unicorn, no taps
    assert take_buttons(SHOP) == []
    msg, _ = handle_text(conn, SHOP, "skip line 7")
    assert "unicorn" not in msg
    assert assert_format(confirm_pending(conn, SHOP)).startswith(
        "✅ *Book saved*")


def test_book_skip_last_line_cancels(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    propose_book_page(conn, SHOP, {
        "date": "2026-10-21", "page_total_cents": None, "crossed_out": 0,
        "lines": [{"line_no": 1, "text": "1 unicorn", "item": "unicorn",
                   "qty": 1, "price_cents": None, "kind": "cash",
                   "customer": ""}]})
    assert get_pending(conn, SHOP) is not None
    msg, _ = handle_text(conn, SHOP, "skip line 1")
    assert "Nothing to save" in msg
    assert get_pending(conn, SHOP) is None


def test_book_correction_to_nomatch_stays_asked(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)
    propose_book_page(conn, SHOP, _book())
    handle_text(conn, SHOP, "line 6 sasko")
    msg, _ = handle_text(conn, SHOP, "line 7 is 1 dragon")
    assert "what is this?" in msg or "which one?" in msg
    assert "Answer the open lines first" in confirm_pending(conn, SHOP)
    take_buttons(SHOP)


def test_book_skip_resolved_is_noop(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    propose_book_page(conn, SHOP, _book())
    msg, _ = handle_text(conn, SHOP, "skip line 2")
    assert "• 2x Coke 2L = R72.00" in msg  # resolved line kept
    assert "Answer the open lines first" in confirm_pending(conn, SHOP)


def _card_book():
    return {
        "date": "2026-10-21",
        "page_total_cents": None,
        "crossed_out": 0,
        "lines": [
            {"line_no": 1, "text": "2 coke 72", "item": "coke", "qty": 2,
             "price_cents": 7200, "kind": "cash", "customer": ""},
            {"line_no": 2, "text": "2 milk 56", "item": "milk", "qty": 2,
             "price_cents": 5600, "kind": "card", "customer": ""},
            {"line_no": 3, "text": "1 milk Thandi", "item": "milk", "qty": 1,
             "price_cents": None, "kind": "credit", "customer": "Thandi"},
        ],
    }


def test_book_card_posts_to_bank(conn):
    from app.tools import account_balance, cash_up

    seed_demo(SHOP, conn)
    _set_en(conn)
    out = assert_format(propose_book_page(conn, SHOP, _card_book()))
    assert "💳 *Card sales*" in out
    assert "• 2x Fresh Milk 1L = R56.00" in out
    assert "💳 *Card sales R56.00 → bank R56.00*" in out
    assert "Expected cash in hand R72.00" in out  # drawer excludes card
    saved = assert_format(confirm_pending(conn, SHOP))
    assert "card R56.00" in saved
    assert "• Card sales R56.00 (1 sales)" in saved
    assert "• Till R72.00" in cash_balances(conn, SHOP)
    assert account_balance(conn, SHOP, "bank") == 5600
    assert "• Cash sales R72.00 (1 sales)" in cash_up(conn, SHOP,
                                                     "2026-10-21")
    milk = conn.execute("SELECT stock_qty FROM products WHERE shop_id = ? "
                        "AND name = 'Fresh Milk 1L'", (SHOP,)).fetchone()
    assert milk["stock_qty"] == 3  # card + credit lines both moved stock


def test_book_card_correction(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    propose_book_page(conn, SHOP, _card_book())
    msg, _ = handle_text(conn, SHOP, "line 1 is 2 coke card")
    assert "💳 *Card sales*" in msg
    assert "• 2x Coke 2L = R72.00" in msg
    assert "💵 *Cash sales*" not in msg


def test_book_card_ambiguous_asked_then_banked(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)
    out = propose_book_page(conn, SHOP, {
        "date": "2026-10-21", "page_total_cents": None, "crossed_out": 0,
        "lines": [{"line_no": 4, "text": "2 brd 44", "item": "brd", "qty": 2,
                   "price_cents": 4400, "kind": "card", "customer": ""}]})
    assert "line 4 — which one?" in out
    msg, _ = handle_text(conn, SHOP, "line 4 brown")
    assert "• 2x Albany Brown Bread 700g = R44.00" in msg
    assert confirm_pending(conn, SHOP).startswith("✅ *Book saved*")
    from app.tools import account_balance
    assert account_balance(conn, SHOP, "bank") == 4400
    assert "• Till R0.00" in cash_balances(conn, SHOP)
    take_buttons(SHOP)


def _supply_book():
    return {
        "date": "2026-10-21",
        "page_total_cents": None,
        "crossed_out": 0,
        "lines": [
            {"line_no": 1, "text": "2 coke 72", "item": "coke", "qty": 2,
             "price_cents": 7200, "kind": "cash", "customer": ""},
            {"line_no": 15, "text": "Supplier: 5 bread R80 cash",
             "item": "bread", "qty": 5, "price_cents": 8000,
             "kind": "supplier", "customer": ""},
            {"line_no": 16, "text": "1 milk damaged - threw away",
             "item": "milk", "qty": 1, "price_cents": None,
             "kind": "wastage", "customer": ""},
        ],
    }


def _prod(conn, name):
    return conn.execute("SELECT * FROM products WHERE shop_id = ? AND name = ?",
                        (SHOP, name)).fetchone()


def test_book_supply_and_waste_flow(conn):
    from app.tools import undo_last

    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)
    out = assert_format(propose_book_page(conn, SHOP, _supply_book()))
    assert "line 15 — which one?" in out  # bread still ambiguous
    msg, _ = handle_text(conn, SHOP, "line 15 brown")
    assert "• 5x Albany Brown Bread 700g = R80.00 (Till)" in msg
    assert "🚚 *Supplier R80.00 paid from Till*" in msg
    assert "• 1x Fresh Milk 1L" in msg  # waste section
    assert "Expected cash in hand -R8.00" in msg  # 72 - 80
    assert assert_format(confirm_pending(conn, SHOP)).startswith(
        "✅ *Book saved*")
    assert _prod(conn, "Albany Brown Bread 700g")["stock_qty"] == 13
    assert _prod(conn, "Albany Brown Bread 700g")["cost_cents"] == 1600
    assert _prod(conn, "Fresh Milk 1L")["stock_qty"] == 5
    assert "• Till -R8.00" in cash_balances(conn, SHOP)
    undo_last(conn, SHOP)
    assert _prod(conn, "Albany Brown Bread 700g")["stock_qty"] == 8
    assert _prod(conn, "Albany Brown Bread 700g")["cost_cents"] is None
    assert _prod(conn, "Fresh Milk 1L")["stock_qty"] == 6
    assert "• Till R0.00" in cash_balances(conn, SHOP)


def test_book_supply_asks_qty_then_saves(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)
    out = propose_book_page(conn, SHOP, {
        "date": "2026-10-21", "page_total_cents": None, "crossed_out": 0,
        "lines": [{"line_no": 15, "text": "Supplier: bread R80 cash",
                   "item": "bread", "qty": None, "price_cents": 8000,
                   "kind": "supplier", "customer": ""}]})
    assert "line 15 — which one?" in out
    msg, _ = handle_text(conn, SHOP, "brown")
    assert "how many?" in msg
    assert "R80.00" in msg
    msg, _ = handle_text(conn, SHOP, "5")
    assert "• 5x Albany Brown Bread 700g = R80.00 (Till)" in msg
    assert assert_format(confirm_pending(conn, SHOP)).startswith(
        "✅ *Book saved*")
    assert _prod(conn, "Albany Brown Bread 700g")["stock_qty"] == 13
    assert "• Till -R80.00" in cash_balances(conn, SHOP)


def test_book_supply_asks_cost(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)
    out = propose_book_page(conn, SHOP, {
        "date": "2026-10-21", "page_total_cents": None, "crossed_out": 0,
        "lines": [{"line_no": 15, "text": "Supplier: 5 coke",
                   "item": "coke", "qty": 5, "price_cents": None,
                   "kind": "supplier", "customer": ""}]})
    assert "how much?" in out
    msg, _ = handle_text(conn, SHOP, "line 15 is R80")
    assert "• 5x Coke 2L = R80.00 (Till)" in msg
    assert confirm_pending(conn, SHOP).startswith("✅ *Book saved*")
    assert _prod(conn, "Coke 2L")["stock_qty"] == 10


def test_book_supply_correction_keeps_supplier(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)
    propose_book_page(conn, SHOP, {
        "date": "2026-10-21", "page_total_cents": None, "crossed_out": 0,
        "lines": [{"line_no": 15, "text": "Supplier: bread R80 cash",
                   "item": "bread", "qty": None, "price_cents": 8000,
                   "kind": "supplier", "customer": ""}]})
    handle_text(conn, SHOP, "brown")  # binds Albany, asks qty
    take_buttons(SHOP)
    msg, _ = handle_text(conn, SHOP, "line 15 is stoney")
    assert "how many?" in msg  # product fixed, qty still open
    assert "Stoney 2L" in msg
    msg, _ = handle_text(conn, SHOP, "line 15 is 4")
    assert "• 4x Stoney 2L = R80.00 (Till)" in msg
    assert confirm_pending(conn, SHOP).startswith("✅ *Book saved*")


def test_book_supply_skip(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    propose_book_page(conn, SHOP, _supply_book())
    msg, _ = handle_text(conn, SHOP, "skip line 15")
    assert "Supplier" not in msg
    assert confirm_pending(conn, SHOP).startswith("✅ *Book saved*")
    assert _prod(conn, "Albany Brown Bread 700g")["stock_qty"] == 8
    assert _prod(conn, "Fresh Milk 1L")["stock_qty"] == 5  # waste kept


def test_book_waste_correction(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    propose_book_page(conn, SHOP, _card_book())
    msg, _ = handle_text(conn, SHOP, "line 1 is 2 coke waste")
    assert "🗑️ *Damaged / thrown away*" in msg
    assert "• 2x Coke 2L" in msg
    assert confirm_pending(conn, SHOP).startswith("✅ *Book saved*")
    assert _prod(conn, "Coke 2L")["stock_qty"] == 3
    assert "• Till R0.00" in cash_balances(conn, SHOP)  # no money moved


def test_book_credit_shows_list_flag(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    out = assert_format(propose_book_page(conn, SHOP, {
        "date": "2026-10-21", "page_total_cents": None, "crossed_out": 0,
        "lines": [{"line_no": 3, "text": "2 coke Thandi 80", "item": "coke",
                   "qty": 2, "price_cents": 8000, "kind": "credit",
                   "customer": "Thandi"}]}))
    assert "• Thandi: 2x Coke 2L = R80.00" in out
    assert "⚠️ (list R72.00)" in out
    assert confirm_pending(conn, SHOP).startswith("✅ *Book saved*")


def test_book_skip_off_page(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    propose_book_page(conn, SHOP, _book())
    msg, _ = handle_text(conn, SHOP, "skip line 9")
    assert "isn't on this page" in msg


def test_book_tap_no_match_falls_through(conn):
    from app.agent import try_book_tap

    seed_demo(SHOP, conn)
    _set_en(conn)
    propose_book_page(conn, SHOP, _book())
    assert try_book_tap(conn, SHOP, "hello there") is None
    assert try_book_tap(conn, "27noshop", "brown") is None


def test_book_numbered_tap_binds(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)
    propose_book_page(conn, SHOP, _two_ambiguous())
    handle_text(conn, SHOP, "line 6 sasko")
    take_buttons(SHOP)
    msg, _ = handle_text(conn, SHOP, "2")
    assert "• 1x Albany Brown Bread 700g = R19.00" in msg
    assert assert_format(confirm_pending(conn, SHOP)).startswith(
        "✅ *Book saved*")


def test_book_yes_gate_queues_buttons(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)
    propose_book_page(conn, SHOP, _book())
    msg, buttons = handle_text(conn, SHOP, "yes")
    assert "Answer the open lines first" in msg
    assert buttons is None
    queued = take_buttons(SHOP)
    assert len(queued) == 1
    assert "Line 6" in queued[0]["body"]
    assert "Sasko White Bread 700g" in queued[0]["buttons"]


def test_book_photo_proposal_queues_option_buttons(conn, monkeypatch):
    import json
    from types import SimpleNamespace

    import app.agent as agent_mod
    import app.interpreter as interp
    from app.agent import handle_photo

    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)

    def fake_read(image, shop=""):
        return {"doc_type": "book_page",
                "expiry": {"stamp_count": 0, "items": []},
                "book": _book(),
                "receipt": {"supplier": "", "date": "", "receipt_no": "",
                            "lines": [], "vat_cents": None,
                            "total_cents": None, "payment": ""}}

    def fake(messages):
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(
                    {"understood": "day book photo",
                     "tasks": [{"kind": "book_page", "args": {},
                                "quote": ""}],
                     "question": None, "options": []})))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2))

    monkeypatch.setattr(agent_mod, "read_shop_photo", fake_read)
    monkeypatch.setattr(interp, "propose_plan", fake)
    _, reply, buttons = handle_photo(conn, SHOP, b"book-photo-tap")
    assert "Today's book" in reply
    assert buttons is None  # options go as tap buttons, not Yes/No
    queued = take_buttons(SHOP)
    assert len(queued) == 1
    assert "Line 6" in queued[0]["body"]
    assert queued[0]["buttons"] == ["Sasko White Bread 700g",
                                   "Albany Brown Bread 700g"]


def test_book_save_queues_daily_pdf(conn, monkeypatch, tmp_path):
    from app import reports

    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setenv("SISI_PUBLIC_URL", "https://x.example")
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)
    take_media(SHOP)
    propose_book_page(conn, SHOP, _book())
    handle_text(conn, SHOP, "line 6 sasko")
    take_buttons(SHOP)
    handle_text(conn, SHOP, "skip line 7")
    take_buttons(SHOP)
    saved = assert_format(confirm_pending(conn, SHOP))
    assert saved.startswith("✅ *Book saved*")
    items = take_media(SHOP)
    assert len(items) == 1
    assert items[0]["kind"] == "file"
    assert items[0]["url"].startswith("https://x.example/media/")
    assert items[0]["filename"] == "sisi-daily-sales-2026-10-21.pdf"
    assert "Daily sales" in items[0]["caption"]
    safe = "".join(c for c in SHOP if c.isdigit()) or "shop"
    pdf = (tmp_path / "reports" /
           f"{safe}-daily-sales-2026-10-21.pdf").read_bytes()
    assert pdf.startswith(b"%PDF")
    assert b"R106.00" in pdf  # cash sales
    assert b"R64.00" in pdf  # credit incl. the answered line
    assert b"R156.00" in pdf  # cash from today
    assert b"R170.00" in pdf  # lines vs page total mismatch
    assert b"Cash from today" in pdf
    assert b"Expected cash in hand" not in pdf  # chat-only balance label


def test_book_save_without_base_url_no_media(conn, monkeypatch):
    monkeypatch.delenv("SISI_PUBLIC_URL", raising=False)
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_media(SHOP)
    propose_book_page(conn, SHOP, _book())
    handle_text(conn, SHOP, "line 6 sasko")
    handle_text(conn, SHOP, "skip line 7")
    assert confirm_pending(conn, SHOP).startswith("✅ *Book saved*")
    assert take_media(SHOP) == []


def test_book_unreadable_line_asks_no_question(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)
    out = propose_book_page(conn, SHOP, {
        "date": "2026-10-21", "page_total_cents": None, "crossed_out": 0,
        "lines": [
            {"line_no": 1, "text": "2 coke 72", "item": "coke", "qty": 2,
             "price_cents": 7200, "kind": "cash", "customer": ""},
            {"line_no": 15, "text": "Supplier: bread R80 cash",
             "item": "bread", "qty": 1, "price_cents": 8000,
             "kind": "unreadable", "customer": ""}]})
    assert "which one?" not in out  # no pointless question
    assert out.rstrip().endswith(
        "Reply Yes to save, or correct a line, e.g. 'line 3 is 4 bread'")
    msg, _ = handle_text(conn, SHOP, "yes")
    assert assert_format(msg).startswith("✅ *Book saved*")
    assert take_buttons(SHOP) == []  # nothing to answer, no buttons


def test_daily_pdf_failure_keeps_save(conn, monkeypatch):
    def boom(data, brand, path):
        raise RuntimeError("pdf exploded")

    monkeypatch.setattr("app.reports.render_daily_sales_pdf", boom)
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_media(SHOP)
    propose_book_page(conn, SHOP, _book())
    handle_text(conn, SHOP, "line 6 sasko")
    handle_text(conn, SHOP, "skip line 7")
    assert confirm_pending(conn, SHOP).startswith("✅ *Book saved*")
    assert take_media(SHOP) == []


def test_book_price_challenge_needs_same_pick_twice(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)
    out = propose_book_page(conn, SHOP, {
        "date": "2026-10-21", "page_total_cents": None, "crossed_out": 0,
        "lines": [{"line_no": 1, "text": "1 coke 200", "item": "coke",
                   "qty": 1, "price_cents": 20000, "kind": "cash",
                   "customer": ""}]})
    assert "way off the list (R36.00)" in out  # challenged, not bound
    assert "⏳ Answer the open lines first" in out
    msg, _ = handle_text(conn, SHOP, "line 1 coke")  # same pick: confirmed
    assert "• 1x Coke 2L = R200.00" in msg
    assert assert_format(confirm_pending(conn, SHOP)).startswith(
        "✅ *Book saved*")


def test_book_till_count_match(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    out = assert_format(propose_book_page(conn, SHOP, {
        "date": "2026-10-21", "page_total_cents": 7200,
        "page_total_label": "Till", "crossed_out": 0,
        "lines": [{"line_no": 1, "text": "2 coke 72", "item": "coke",
                   "qty": 2, "price_cents": 7200, "kind": "cash",
                   "customer": ""}]}))
    assert "✅ *Till matches the drawer (R72.00).*" in out


def test_book_till_count_diff(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    out = assert_format(propose_book_page(conn, SHOP, {
        "date": "2026-10-21", "page_total_cents": 8000,
        "page_total_label": "counted", "crossed_out": 0,
        "lines": [{"line_no": 1, "text": "2 coke 72", "item": "coke",
                   "qty": 2, "price_cents": 7200, "kind": "cash",
                   "customer": ""}]}))
    assert "⚠️ *Till counted R80.00 — R8.00 over of R72.00.*" in out
    assert "reply 'counted R80.00'" in out


def test_book_supply_no_qty_no_price_plain_prompt(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)
    out = propose_book_page(conn, SHOP, {
        "date": "2026-10-21", "page_total_cents": None, "crossed_out": 0,
        "lines": [{"line_no": 15, "text": "Supplier: coke",
                   "item": "coke", "qty": None, "price_cents": None,
                   "kind": "supplier", "customer": ""}]})
    assert "How many Coke 2L arrived?" in out
    assert "did R0.00 buy?" not in out
    msg, _ = handle_text(conn, SHOP, "5")
    assert "how much?" in msg
    msg, _ = handle_text(conn, SHOP, "line 15 is R80")
    assert "• 5x Coke 2L = R80.00 (Till)" in msg
    assert assert_format(confirm_pending(conn, SHOP)).startswith(
        "✅ *Book saved*")
    assert _prod(conn, "Coke 2L")["stock_qty"] == 10


def test_daily_pdf_till_line(conn, tmp_path):
    from app import reports

    seed_demo(SHOP, conn)
    _set_en(conn)
    payload = {
        "display_date": "21 Oct", "date": "2026-10-21",
        "cash": [{"line_no": 1, "short": "2x Coke 2L", "total": 7200}],
        "credit": [], "card": [], "supply": [], "waste": [],
        "payments": [], "page_total": 7200, "page_total_label": "Till",
    }
    data = reports.daily_sales_data(payload)
    assert data["is_till"] is True
    data["drawer"] = 7200
    pdf = str(tmp_path / "till.pdf")
    reports.render_daily_sales_pdf(data, reports.shop_brand(conn, SHOP), pdf)
    raw = open(pdf, "rb").read()
    assert raw.startswith(b"%PDF")
    assert b"Till matches the drawer" in raw  # parens are PDF-escaped


def test_book_payment_balance_includes_same_page_credit(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    out = assert_format(propose_book_page(conn, SHOP, {
        "date": "2026-10-21", "page_total_cents": None, "crossed_out": 0,
        "lines": [
            {"line_no": 1, "text": "1 milk Thandi", "item": "milk", "qty": 1,
             "price_cents": None, "kind": "credit", "customer": "Thandi"},
            {"line_no": 2, "text": "Thandi paid 15", "item": "", "qty": 1,
             "price_cents": 1500, "kind": "payment", "customer": "Thandi"},
        ]}))
    # Thandi opens at R45.50: +R27.00 milk, then -R15.00 paid.
    assert "• Thandi: 1x Fresh Milk 1L = R27.00 → owes R72.50" in out
    assert "• Thandi paid R15.00 → owes R57.50" in out
    assert assert_format(confirm_pending(conn, SHOP)).startswith(
        "✅ *Book saved*")
    assert get_balance(conn, SHOP, "Thandi") == "💳 *Thandi* owes R57.50."


def test_book_second_page_same_date_warns(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    page = {
        "date": "2026-10-21", "page_total_cents": None, "crossed_out": 0,
        "lines": [{"line_no": 1, "text": "1 coke 36", "item": "coke",
                   "qty": 1, "price_cents": 3600, "kind": "cash",
                   "customer": ""}]}
    out = propose_book_page(conn, SHOP, page)
    assert "already saved" not in out
    assert confirm_pending(conn, SHOP).startswith("✅ *Book saved*")
    out = assert_format(propose_book_page(conn, SHOP, page))
    assert out.startswith("⚠️ *A page for 21 Oct is already saved"
                         " — Yes will add these lines too.*")
    assert assert_format(confirm_pending(conn, SHOP)).startswith(
        "✅ *Book saved*")  # warning only: a real page 2 still saves
    undo_last(conn, SHOP)
    undo_last(conn, SHOP)
    out = propose_book_page(conn, SHOP, page)
    assert "already saved" not in out  # undone saves don't count


def test_book_price_challenge_exact_half(conn):
    seed_demo(SHOP, conn)
    _set_en(conn)
    take_buttons(SHOP)
    out = propose_book_page(conn, SHOP, {
        "date": "2026-10-21", "page_total_cents": None, "crossed_out": 0,
        "lines": [{"line_no": 1, "text": "1 coke 18", "item": "coke",
                   "qty": 1, "price_cents": 1800, "kind": "cash",
                   "customer": ""}]})
    assert "way off the list (R36.00)" in out  # 0.5x binds only on confirm
    assert "⏳ Answer the open lines first" in out
    msg, _ = handle_text(conn, SHOP, "line 1 coke")  # same pick: confirmed
    assert "• 1x Coke 2L = R18.00" in msg
    assert assert_format(confirm_pending(conn, SHOP)).startswith(
        "✅ *Book saved*")
