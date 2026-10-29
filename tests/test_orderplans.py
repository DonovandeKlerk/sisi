"""Media intent routing + multi-task order plans (the order-photo bug).

An order photo with a multi-task caption was saved as the shop logo.
These tests pin the fix: route media by this message's intent, plan
every task in one preview, confirm all with one Yes, and never invent
stock, sales, or debt.
"""

import io
from datetime import datetime

import pytest

from app import reports
from app.agent import (
    MEDIA_OUTBOX,
    handle_photo,
    handle_text,
    take_media,
    try_order_plan,
)
from app.orders import (
    build_plan,
    correct_plan_line,
    get_plan,
    get_plan_by_key,
    render_preview,
)
from app.tools import (
    ensure_shop,
    get_order_draft,
    logo_pending,
)
from tests.test_format import assert_format

SHOP = "27710000099"
OTHER = "27710000098"


@pytest.fixture(autouse=True)
def _no_setup(conn):
    from app.setup import skip_setup
    skip_setup(conn, SHOP)
    skip_setup(conn, OTHER)


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(reports, "LOGOS_DIR", tmp_path / "logos")
    monkeypatch.delenv("SISI_PUBLIC_URL", raising=False)
    MEDIA_OUTBOX.clear()


@pytest.fixture()
def shop(conn):
    ensure_shop(conn, SHOP, "Order Spaza")
    conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, cost_cents,"
        " stock_qty, reorder_level, wholesaler, category)"
        " VALUES (?, 'Bread', 1800, 1400, 1, 5, 'Jumbo', 'Groceries')",
        (SHOP,))
    conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, cost_cents,"
        " stock_qty, reorder_level, wholesaler, category, pack_name,"
        " pack_size) VALUES (?, 'Chappies', 100, 60, 200, 50, 'Jumbo',"
        " 'Groceries', 'box', 100)",
        (SHOP,))
    conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, cost_cents,"
        " stock_qty, reorder_level, wholesaler, category)"
        " VALUES (?, 'Coke 2L', 2500, 1900, 0, 4, 'Makro', 'Drinks')",
        (SHOP,))
    conn.execute("INSERT INTO products (shop_id, name, stock_qty)"
                 " VALUES (?, 'Salt', 10)", (SHOP,))
    conn.execute("INSERT INTO customers (shop_id, name) VALUES (?, 'Sipho')",
                 (SHOP,))
    conn.execute("INSERT INTO customers (shop_id, name) VALUES (?, 'Bongani')",
                 (SHOP,))
    sipho = conn.execute("SELECT id FROM customers WHERE shop_id = ?"
                         " AND name = 'Sipho'", (SHOP,)).fetchone()["id"]
    conn.execute("INSERT INTO credit_entries (shop_id, customer_id,"
                 " amount_cents, items_text, action_id)"
                 " VALUES (?, ?, 8000, '2 bread', 'ord-seed-1')",
                 (SHOP, sipho))
    conn.commit()
    return SHOP


def _photo_bytes():
    from PIL import Image
    img = Image.new("RGB", (120, 80), "white")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _mock_vision(monkeypatch, doc):
    import app.agent as agent
    monkeypatch.setattr(agent, "read_shop_photo",
                        lambda image, shop="": doc)


CAPTION = ("Sipho: 2 bread, 5 chappies. Bongani: 1 coke 2l. "
           "Remind me Thursday 06:30 to pack this order. "
           "Summarise for Thursday.")


# --- the live bug: order photo misrouted to logo ------------------------------


def test_order_caption_beats_stale_legacy_logo_flag(conn, shop, monkeypatch):
    conn.execute("INSERT INTO flags (shop_id, key, value)"
                 " VALUES (?, 'logo-pending', '1')", (SHOP,))
    conn.commit()
    _mock_vision(monkeypatch, {"doc_type": "other"})
    _doc, reply, buttons = handle_photo(conn, SHOP, _photo_bytes(), CAPTION)
    assert "🧾 *Customer order*" in reply
    assert "New logo?" not in reply
    assert buttons == ["Yes", "No"]
    assert conn.execute("SELECT logo_path FROM shops WHERE shop_id = ?",
                        (SHOP,)).fetchone()["logo_path"] == ""
    assert conn.execute("SELECT value FROM flags WHERE shop_id = ?"
                        " AND key = 'logo-pending'", (SHOP,)).fetchone() is None


def test_order_caption_drops_fresh_logo_request(conn, shop, monkeypatch):
    handle_text(conn, SHOP, "redo logo")
    assert logo_pending(conn, SHOP)
    _mock_vision(monkeypatch, {"doc_type": "other"})
    _doc, reply, _b = handle_photo(conn, SHOP, _photo_bytes(), CAPTION)
    assert "🧾 *Customer order*" in reply
    assert "Dropped the earlier logo request" in reply
    assert not logo_pending(conn, SHOP)


def test_stale_timestamped_pending_ignored(conn, shop, monkeypatch):
    conn.execute("INSERT INTO flags (shop_id, key, value)"
                 " VALUES (?, 'logo-pending', '2020-01-01T00:00:00+00:00')",
                 (SHOP,))
    conn.commit()
    assert not logo_pending(conn, SHOP)
    _mock_vision(monkeypatch, {"doc_type": "other"})
    _doc, reply, _b = handle_photo(conn, SHOP, _photo_bytes(), "thanks")
    assert "New logo?" not in reply
    assert "Customer order" not in reply


def test_logo_words_without_photo_tasks_still_stage(conn, shop, monkeypatch):
    handle_text(conn, SHOP, "new logo")
    _mock_vision(monkeypatch, {"doc_type": "other"})
    _doc, reply, buttons = handle_photo(conn, SHOP, _photo_bytes(), "logo")
    assert "New logo?" in reply
    assert buttons == ["Yes", "No"]


# --- order extraction ---------------------------------------------------------


def test_extract_two_customers(conn, shop):
    plan, question = build_plan(conn, SHOP, "Sipho: 2 bread, 5 chappies")
    assert question is None
    assert [(ln["customer"], ln["item"], ln["qty"]) for ln in plan["order_lines"]] == [
        ("Sipho", "Bread", 2.0), ("Sipho", "Chappies", 5.0)]
    assert plan["order_lines"][0]["customer_new"] is False
    assert plan["order_lines"][0]["price_cents"] == 1800


def test_fuzzy_customer_match(conn, shop):
    plan, _q = build_plan(conn, SHOP, "Sipo: 2 bread")
    assert plan["order_lines"][0]["customer"] == "Sipho"
    assert plan["order_lines"][0]["customer_new"] is False


def test_new_customer_proposed(conn, shop):
    plan, _q = build_plan(conn, SHOP, "Lindiwe: 2 bread")
    assert plan["order_lines"][0]["customer_new"] is True
    preview = assert_format(render_preview(conn, SHOP, plan))
    assert "Lindiwe is new" in preview


def test_pack_quantity_converted_to_units(conn, shop):
    plan, _q = build_plan(conn, SHOP, "a box of chappies for Sipho")
    assert plan["order_lines"][0]["qty"] == 100.0
    assert plan["order_lines"][0]["customer"] == "Sipho"


def test_unmatched_product_is_a_question(conn, shop):
    plan, _q = build_plan(conn, SHOP, "Sipho: 2 unobtainium")
    assert plan["order_lines"][0]["matched"] is False
    preview = assert_format(render_preview(conn, SHOP, plan))
    assert "Couldn't match" in preview
    assert "line N is" in preview


def test_missing_price_asked_not_guessed(conn, shop):
    plan, _q = build_plan(conn, SHOP, "Sipho: 2 salt")
    preview = render_preview(conn, SHOP, plan)
    assert "price needed" in preview
    assert "R" not in preview.split("L1")[1].split("\n")[0]


def test_zu_item_word_matches(conn, shop):
    plan, _q = build_plan(conn, SHOP, "Sipho: 2 isinkwa")
    assert plan["order_lines"][0]["item"] == "Bread"
    assert plan["order_lines"][0]["matched"] is True


def test_sale_stock_payment_texts_never_plans(conn, shop):
    for text in ("sold 2 bread to Sipho", "Sipho took 2 bread on the book",
                 "add 20 bread at R18", "got 12 stoney, cost R19 sell R25",
                 "Sipho paid R50", "counted R1,240", "set Sipho limit R300"):
        plan, question = build_plan(conn, SHOP, text)
        assert (plan, question) == (None, None), text


def test_bare_text_without_customer_needs_cue(conn, shop):
    assert try_order_plan(conn, SHOP, "2 bread") is None
    assert "Customer order" in try_order_plan(conn, SHOP, "2 bread on order")


def test_reminder_to_order_stays_a_reminder(conn, shop):
    plan, question = build_plan(conn, SHOP, "remind me to order bread Thursday")
    assert (plan, question) == (None, None)


# --- multi-task: the full example ---------------------------------------------


def _confirm_example(conn):
    msg, buttons = handle_text(conn, SHOP, CAPTION)
    assert buttons == ["Yes", "No"]
    assert "🧾 *Customer order*" in msg
    assert "⏰ *Reminder*" in msg
    assert "🧍 *Customer summaries*" in msg
    assert "Thursday" in msg
    assert "Sipho owes R80.00" in msg
    assert "L1 Sipho: 2 × Bread = R36.00" in msg
    return handle_text(conn, SHOP, "Yes")


def test_full_example_confirms_everything(conn, shop):
    msg, _b = _confirm_example(conn)
    assert "✅ *Order saved*" in msg
    assert "Order list saved (3 lines)" in msg
    row = conn.execute("SELECT due_at, summary, context_json FROM reminders"
                       " WHERE shop_id = ?", (SHOP,)).fetchone()
    due = datetime.fromisoformat(row["due_at"])
    assert (due.weekday(), due.hour, due.minute) == (3, 6, 30)
    assert "pack this order" in row["summary"]
    import json
    assert json.loads(row["context_json"])["kind"] == "customer_order"
    draft = get_order_draft(conn, SHOP)
    assert draft["kind"] == "customer_order"
    assert draft["lines"]  # bread + coke shortfall
    assert "Sipho: 2 Bread, 5 Chappies = R41.00" in msg


def test_confirm_writes_no_phantom_stock_sales_debt(conn, shop):
    before_stock = conn.execute("SELECT SUM(stock_qty) s FROM products"
                                " WHERE shop_id = ?", (SHOP,)).fetchone()["s"]
    _confirm_example(conn)
    after_stock = conn.execute("SELECT SUM(stock_qty) s FROM products"
                               " WHERE shop_id = ?", (SHOP,)).fetchone()["s"]
    assert before_stock == after_stock
    assert conn.execute("SELECT COUNT(*) n FROM sales WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) n FROM credit_entries"
                        " WHERE shop_id = ?", (SHOP,)).fetchone()["n"] == 1


def test_double_yes_is_idempotent(conn, shop):
    _confirm_example(conn)
    msg, _b = handle_text(conn, SHOP, "Yes")
    assert "Already saved" in msg
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 1


def test_no_drops_everything(conn, shop):
    handle_text(conn, SHOP, CAPTION)
    msg, _b = handle_text(conn, SHOP, "No")
    assert "Nothing was saved" in msg
    assert get_order_draft(conn, SHOP) is None
    assert get_plan(conn, SHOP) is None
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0


def test_cancel_word_drops_plan(conn, shop):
    handle_text(conn, SHOP, CAPTION)
    msg, _b = handle_text(conn, SHOP, "cancel")
    assert "Nothing was saved" in msg
    assert get_plan(conn, SHOP) is None


def test_expired_plan_can_be_redone(conn, shop):
    handle_text(conn, SHOP, CAPTION)
    conn.execute("UPDATE task_plans SET expires_at = '2000-01-01T00:00:00Z'"
                 " WHERE shop_id = ?", (SHOP,))
    conn.commit()
    msg, _b = handle_text(conn, SHOP, "Yes")
    assert "expired" in msg
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 0
    msg, buttons = handle_text(conn, SHOP, CAPTION)
    assert buttons == ["Yes", "No"]
    assert "Customer order" in msg


def test_correction_rewrites_a_line(conn, shop):
    handle_text(conn, SHOP, CAPTION)
    msg, _b = handle_text(conn, SHOP, "line 3 is 2 coke 2l for Bongani")
    assert "L3 Bongani: 2 × Coke 2L = R50.00" in msg
    msg, _b = handle_text(conn, SHOP, "line 9 is 2 bread")
    assert "Customer order" in msg  # bad line number keeps the preview


def test_correction_can_match_an_unknown_line(conn, shop):
    handle_text(conn, SHOP, "Sipho: 2 unobtainium")
    msg, _b = handle_text(conn, SHOP, "line 1 is 2 bread for Sipho")
    assert "L1 Sipho: 2 × Bread = R36.00" in msg
    assert "Couldn't match" not in msg


def test_price_correction_sets_price(conn, shop):
    handle_text(conn, SHOP, "Sipho: 2 salt")
    msg = correct_plan_line(conn, SHOP, 1, "price R22")
    assert "L1 Sipho: 2 × Salt = R44.00" in msg


def test_list_price_set_later_refreshes_preview(conn, shop):
    from app.tools import ensure_product_price
    handle_text(conn, SHOP, "Sipho: 2 salt")
    ensure_product_price(conn, SHOP, "Salt", 2200)
    preview = render_preview(conn, SHOP, get_plan(conn, SHOP))
    assert "L1 Sipho: 2 × Salt = R44.00" in preview


def test_newest_wins_plan_over_pending(conn, shop):
    from app.tools import propose_book_page
    propose_book_page(conn, SHOP, {"date": "2026-10-28", "page_total_cents": None,
                                  "crossed_out": 0, "lines": [
                                      {"line_no": 1, "text": "2 bread",
                                       "item": "Bread", "qty": 2,
                                       "price_cents": None, "kind": "cash",
                                       "customer": ""}]})
    handle_text(conn, SHOP, CAPTION)
    msg, _b = handle_text(conn, SHOP, "Yes")
    assert "✅ *Order saved*" in msg
    assert "earlier preview" in msg
    msg, _b = handle_text(conn, SHOP, "Yes")
    assert "Order saved" not in msg  # second Yes confirms the book


def test_plan_is_per_shop(conn, shop):
    handle_text(conn, SHOP, CAPTION)
    assert get_plan(conn, OTHER) is None
    from app.tools import ensure_shop as _ensure
    _ensure(conn, OTHER, "Other Spaza")


# --- vision + caption together -------------------------------------------------


def test_vision_book_lines_feed_order(conn, shop, monkeypatch):
    _mock_vision(monkeypatch, {"doc_type": "book_page", "book": {
        "date": "2026-10-28", "page_total_cents": None, "crossed_out": 0,
        "lines": [{"line_no": 1, "text": "2 bread", "item": "bread", "qty": 2,
                   "price_cents": None, "kind": "cash", "customer": ""}]}})
    _doc, reply, buttons = handle_photo(conn, SHOP, _photo_bytes(),
                                        "Sipho's Thursday order")
    assert "🧾 *Customer order*" in reply
    assert "L1 Sipho: 2 × Bread = R36.00" in reply
    assert buttons == ["Yes", "No"]


def test_ambiguous_order_asks_without_losing_photo(conn, shop, monkeypatch):
    _mock_vision(monkeypatch, {"doc_type": "other"})
    _doc, reply, buttons = handle_photo(conn, SHOP, _photo_bytes(), "my order")
    assert "what exactly" in reply
    assert buttons is None
    assert get_plan(conn, SHOP) is None
    _doc, reply, _b = handle_photo(conn, SHOP, _photo_bytes(),
                                   "Sipho: 2 bread")
    assert "🧾 *Customer order*" in reply  # photo never asked for twice


def test_logo_or_task_question_when_both_plausible(conn, shop, monkeypatch):
    handle_text(conn, SHOP, "redo logo")
    _mock_vision(monkeypatch, {"doc_type": "other"})
    _doc, reply, _b = handle_photo(conn, SHOP, _photo_bytes(), "my order")
    assert "new logo, or the order" in reply
    assert logo_pending(conn, SHOP)  # still waiting for the answer


def test_book_photo_plus_reminder_caption_does_both(conn, shop, monkeypatch):
    _mock_vision(monkeypatch, {"doc_type": "book_page", "book": {
        "date": "2026-10-28", "page_total_cents": None, "crossed_out": 0,
        "lines": [{"line_no": 1, "text": "2 bread", "item": "bread", "qty": 2,
                   "price_cents": None, "kind": "cash", "customer": ""}]}})
    _doc, reply, buttons = handle_photo(
        conn, SHOP, _photo_bytes(), "remind me Thursday 6:30 to collect")
    from app.tools import get_pending
    assert get_pending(conn, SHOP) is not None  # book preview kept
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 1
    assert buttons == ["Yes", "No"]


def test_reminder_caption_without_order_saves_directly(conn, shop, monkeypatch):
    _mock_vision(monkeypatch, {"doc_type": "other"})
    _doc, reply, _b = handle_photo(
        conn, SHOP, _photo_bytes(), "remind me Thursday 6:30 to collect")
    assert get_plan(conn, SHOP) is None
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 1


def test_chitchat_caption_keeps_old_behavior(conn, shop, monkeypatch):
    _mock_vision(monkeypatch, {"doc_type": "other"})
    _doc, reply, _b = handle_photo(conn, SHOP, _photo_bytes(), "thanks!")
    assert get_plan(conn, SHOP) is None
    assert reply.strip() != ""


def test_duplicate_photo_after_confirm(conn, shop, monkeypatch):
    _mock_vision(monkeypatch, {"doc_type": "other"})
    photo = _photo_bytes()
    handle_photo(conn, SHOP, photo, CAPTION)
    handle_text(conn, SHOP, "Yes")
    _doc, reply, _b = handle_photo(conn, SHOP, photo, CAPTION)
    assert "🧾" in reply  # already recorded as the order


# --- statements, summaries, undo -----------------------------------------------


def test_statements_only_for_confirmed_debt(conn, shop, monkeypatch):
    monkeypatch.setenv("SISI_PUBLIC_URL", "https://x.example")
    msg, _b = handle_text(conn, SHOP, "Sipho: 2 bread. Statement for Sipho.")
    assert "Customer order" in msg
    msg, _b = handle_text(conn, SHOP, "Yes")
    items = take_media(SHOP)
    assert [i["kind"] for i in items] == ["image", "file"]
    assert "Forward this to Sipho" in items[0]["caption"]


def test_summary_task_sends_no_media(conn, shop):
    handle_text(conn, SHOP, CAPTION)
    msg, _b = handle_text(conn, SHOP, "Yes")
    assert "🧍 *Customer summaries*" in msg
    assert take_media(SHOP) == []


def test_undo_order_restores_previous_draft(conn, shop):
    from app.tools import _save_order_draft, build_order, undo_last_kind
    build_order(conn, SHOP)
    prev = get_order_draft(conn, SHOP)
    assert prev is not None
    _confirm_example(conn)
    assert get_order_draft(conn, SHOP)["kind"] == "customer_order"
    msg = undo_last_kind(conn, SHOP, "order", "nothing")
    assert "Order draft undone" in msg
    assert get_order_draft(conn, SHOP)["lines"] == prev["lines"]


def test_undo_customer_removes_only_unused(conn, shop):
    from app.tools import undo_last_kind
    handle_text(conn, SHOP, "Lindiwe: 2 bread")
    handle_text(conn, SHOP, "Yes")
    assert conn.execute("SELECT id FROM customers WHERE shop_id = ?"
                        " AND name = 'Lindiwe'", (SHOP,)).fetchone()
    msg = undo_last_kind(conn, SHOP, "customer", "nothing")
    assert "removed" in msg
    assert conn.execute("SELECT id FROM customers WHERE shop_id = ?"
                        " AND name = 'Lindiwe'", (SHOP,)).fetchone() is None


def test_undo_customer_keeps_booked_customer(conn, shop):
    from app.tools import undo_last_kind
    conn.execute("INSERT INTO customers (shop_id, name) VALUES (?, 'Zama')",
                 (SHOP,))
    zama = conn.execute("SELECT id FROM customers WHERE shop_id = ?"
                        " AND name = 'Zama'", (SHOP,)).fetchone()["id"]
    conn.execute("INSERT INTO credit_entries (shop_id, customer_id,"
                 " amount_cents, action_id) VALUES (?, ?, 100, 'ord-seed-2')",
                 (SHOP, zama))
    import json
    from app.tools import _action_id
    conn.execute("INSERT INTO actions (action_id, shop_id, kind, summary,"
                 " data_json) VALUES (?, ?, 'customer', ?, ?)",
                 (_action_id(), SHOP, "customer: Zama",
                  json.dumps({"customer_id": zama, "name": "Zama"})))
    conn.commit()
    msg = undo_last_kind(conn, SHOP, "customer", "nothing")
    assert "already have book entries" in msg


def test_undo_logo_restores_previous(conn, shop):
    from app.tools import undo_last_kind
    first = _photo_bytes()
    handle_photo(conn, SHOP, first, "logo")
    handle_text(conn, SHOP, "Yes")
    brand = reports.shop_brand(conn, SHOP)
    want = open(brand["logo"], "rb").read()
    handle_text(conn, SHOP, "redo logo")
    handle_photo(conn, SHOP, _photo_bytes(), "")
    handle_text(conn, SHOP, "Yes")
    msg = undo_last_kind(conn, SHOP, "logo", "nothing")
    assert "Logo change undone" in msg
    assert open(reports.shop_brand(conn, SHOP)["logo"], "rb").read() == want


# --- reminders: recompute, fire, schedule ---------------------------------------


def test_reminder_recomputes_at_send_time(conn, shop):
    from app.reminders import render_context
    _confirm_example(conn)
    row = conn.execute("SELECT context_json FROM reminders WHERE shop_id = ?",
                       (SHOP,)).fetchone()
    import json
    text, _media = render_context(conn, SHOP, json.loads(row["context_json"]))
    assert "Bread" in text and "Coke 2L" in text
    assert "🛒" in text
    conn.execute("UPDATE products SET stock_qty = 99 WHERE shop_id = ?",
                 (SHOP,))
    conn.commit()
    text2, _m2 = render_context(conn, SHOP, json.loads(row["context_json"]))
    assert "Coke 2L" not in text2  # shortfall gone after restock


def test_due_reminder_fires_with_order_context(conn, shop):
    from app.db import now_iso
    from app.reminders import send_due
    from app.reminders import now_sast
    _confirm_example(conn)
    conn.execute("UPDATE reminders SET due_at = '2000-01-01T06:30:00+02:00'"
                 " WHERE shop_id = ?", (SHOP,))
    conn.execute("INSERT INTO messages_seen (message_id, shop_id, created_at)"
                 " VALUES ('ord-seen-1', ?, ?)", (SHOP, now_iso()))
    conn.commit()
    fired = send_due(conn, now_sast())
    assert fired != []
    queued = conn.execute("SELECT payload_json FROM send_queue"
                          " WHERE to_number = ?", (SHOP,)).fetchone()
    assert "pack this order" in queued["payload_json"]
    assert "Bread" in queued["payload_json"]


def test_fire_text_shows_local_day_and_time():
    from datetime import timezone as _tz
    from datetime import timedelta
    from app.reminders import fire_text
    sast = _tz(timedelta(hours=2))
    text = fire_text("pack it", datetime(2026, 10, 29, 6, 30, tzinfo=sast),
                     False)
    assert "Thu 29 Oct" in text and "06:30" in text


def test_tick_covers_infobip_shops(conn, shop):
    from datetime import timedelta as _td
    from datetime import timezone as _tz
    from app.schedule import tick
    conn.execute("UPDATE shops SET channel = 'infobip' WHERE shop_id = ?",
                 (SHOP,))
    conn.commit()
    now = datetime.now(_tz(_td(hours=2))).replace(hour=6, minute=5,
                                                  second=0, microsecond=0)
    jobs = tick(conn, now)
    assert any(j["key"].startswith("plan-") for j in jobs)


# --- graceful media + misc ------------------------------------------------------


def test_video_and_document_replies_are_graceful(monkeypatch):
    import sqlite3

    import app.main as main
    from app.db import init_db
    from app.whatsapp import FakeSender, IncomingMessage

    def _fresh():
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        init_db(c)
        return c

    sender = FakeSender()
    monkeypatch.setattr(main, "connect", lambda *a, **k: _fresh())
    main._handle_message(sender, IncomingMessage("v1", SHOP, "media:video",
                                                None, "", channel="meta"))
    assert "can't watch videos" in sender.sent[-1]["body"]
    main._handle_message(sender, IncomingMessage("d1", SHOP, "media:document",
                                                None, "", channel="infobip"))
    assert "can't read files" in sender.sent[-1]["body"]


def test_preview_fits_whatsapp_format(conn, shop):
    plan, _q = build_plan(conn, SHOP, CAPTION)
    assert_format(render_preview(conn, SHOP, plan))


def test_plan_key_roundtrip(conn, shop):
    handle_text(conn, SHOP, CAPTION)
    plan = get_plan(conn, SHOP)
    assert get_plan_by_key(conn, SHOP, plan["message_key"])["plan_id"] == \
        plan["plan_id"]


def test_correct_without_plan_reports_expired(conn, shop):
    assert "expired" in correct_plan_line(conn, SHOP, 1, "2 bread")


def test_customer_inherited_by_following_lines(conn, shop):
    plan, _q = build_plan(conn, SHOP, "Sipho: 2 bread. 5 chappies.")
    assert [ln["customer"] for ln in plan["order_lines"]] == ["Sipho", "Sipho"]


def test_history_attributes_customerless_photo_list(conn, shop, monkeypatch):
    from app.llm import save_turn
    save_turn(conn, SHOP, "user", "Sipho still owes me money")
    _mock_vision(monkeypatch, {"doc_type": "other"})
    _doc, reply, _b = handle_photo(conn, SHOP, _photo_bytes(), "2 bread")
    assert "L1 Sipho: 2 × Bread = R36.00" in reply


def test_statement_requests_never_become_orders(conn, shop):
    for text in ("statement for Sipho", "statements for Sipho and Bongani",
                 "stuur vir my die strokie van Sipho",
                 "ngicela isitatimende sikaSipho",
                 "ndicela istatimenti sikaSipho"):
        plan, question = build_plan(conn, SHOP, text)
        assert (plan, question) == (None, None), text


def test_text_answer_resumes_clarified_photo(conn, shop, monkeypatch):
    import hashlib
    _mock_vision(monkeypatch, {"doc_type": "other"})
    photo = _photo_bytes()
    handle_photo(conn, SHOP, photo, "my order")
    msg, buttons = handle_text(conn, SHOP, "Sipho: 2 bread")
    assert "🧾 *Customer order*" in msg
    assert buttons == ["Yes", "No"]
    plan = get_plan(conn, SHOP)
    assert plan["photo_digest"] == hashlib.sha256(photo).hexdigest()
    assert conn.execute("SELECT COUNT(*) n FROM clarify_context"
                        " WHERE shop_id = ?", (SHOP,)).fetchone()["n"] == 0


def test_resume_merges_original_caption_tasks(conn, shop, monkeypatch):
    _mock_vision(monkeypatch, {"doc_type": "other"})
    handle_photo(conn, SHOP, _photo_bytes(),
                 "My order. Remind me Thursday 06:30 to pack it.")
    msg, _b = handle_text(conn, SHOP, "Sipho: 2 bread")
    assert "🧾 *Customer order*" in msg
    assert "⏰ *Reminder*" in msg and "06:30" in msg
    msg, _b = handle_text(conn, SHOP, "Yes")
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 1


def test_resume_keeps_stored_vision_reading(conn, shop, monkeypatch):
    from app.orders import get_clarify_context
    _mock_vision(monkeypatch, {"doc_type": "book_page", "book": {
        "date": "2026-10-28", "page_total_cents": None, "crossed_out": 0,
        "lines": []}})
    photo = _photo_bytes()
    handle_photo(conn, SHOP, photo, "my order")  # clarify: nothing extracted
    ctx = get_clarify_context(conn, SHOP)
    assert ctx["vision"]["doc_type"] == "book_page"
    msg, _b = handle_text(conn, SHOP, "Sipho: 2 bread")
    assert "L1 Sipho: 2 × Bread = R36.00" in msg
    import hashlib
    assert get_plan(conn, SHOP)["photo_digest"] == hashlib.sha256(photo).hexdigest()


def test_clarify_context_survives_restart(tmp_path, monkeypatch):
    import sqlite3
    from app.db import init_db
    from app.setup import skip_setup
    from app.tools import ensure_shop
    db = str(tmp_path / "restart.db")
    c1 = sqlite3.connect(db)
    c1.row_factory = sqlite3.Row
    init_db(c1)
    skip_setup(c1, SHOP)
    ensure_shop(c1, SHOP, "Restart Spaza")
    c1.execute("INSERT INTO products (shop_id, name, price_cents)"
               " VALUES (?, 'Bread', 1800)", (SHOP,))
    c1.commit()
    import app.agent as agent
    monkeypatch.setattr(agent, "read_shop_photo",
                        lambda image, shop="": {"doc_type": "other"})
    photo = _photo_bytes()
    agent.handle_photo(c1, SHOP, photo, "my order")
    c1.close()  # worker restart: new connection, same file
    c2 = sqlite3.connect(db)
    c2.row_factory = sqlite3.Row
    init_db(c2)
    msg, _b = agent.handle_text(c2, SHOP, "Sipho: 2 bread")
    assert "🧾 *Customer order*" in msg
    import hashlib
    plan = get_plan(c2, SHOP)
    assert plan["photo_digest"] == hashlib.sha256(photo).hexdigest()
    c2.close()


def test_clarify_context_is_per_shop_and_expires(conn, shop, monkeypatch):
    from app.orders import get_clarify_context
    _mock_vision(monkeypatch, {"doc_type": "other"})
    handle_photo(conn, SHOP, _photo_bytes(), "my order")
    assert get_clarify_context(conn, OTHER) is None
    conn.execute("UPDATE clarify_context SET expires_at = '2000-01-01T00:00:00Z'"
                 " WHERE shop_id = ?", (SHOP,))
    conn.commit()
    assert get_clarify_context(conn, SHOP) is None
    msg, _b = handle_text(conn, SHOP, "2 bread")
    assert "Customer order" not in msg  # expired ctx: plain text, no plan


def test_unrelated_text_ignores_clarify_context(conn, shop, monkeypatch):
    _mock_vision(monkeypatch, {"doc_type": "other"})
    handle_photo(conn, SHOP, _photo_bytes(), "my order")
    msg, _b = handle_text(conn, SHOP, "my reminders")
    assert "Customer order" not in msg
    from app.orders import get_clarify_context
    assert get_clarify_context(conn, SHOP) is not None  # still waiting


def test_partial_failure_retries_without_duplicates(conn, shop, monkeypatch):
    import app.tools as tools
    real = tools.statements_named
    calls = []

    def _flaky(c, s, names):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("render boom")
        return real(c, s, names)

    monkeypatch.setattr(tools, "statements_named", _flaky)
    handle_text(conn, SHOP, CAPTION + " Statement for Sipho.")
    msg, _b = handle_text(conn, SHOP, "Yes")
    assert "⚠️ *Partly saved*" in msg
    assert "reply Yes to retry" in msg
    assert get_plan(conn, SHOP) is not None  # still proposed, nothing lost
    msg, _b = handle_text(conn, SHOP, "Yes")
    assert "✅ *Order saved*" in msg
    assert conn.execute("SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) n FROM credit_entries WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == 1  # no phantom debt


def test_correction_keeps_other_tasks(conn, shop):
    handle_text(conn, SHOP, CAPTION)
    msg, _b = handle_text(conn, SHOP, "line 3 is 2 coke 2l for Bongani")
    assert "L3 Bongani" in msg
    assert "⏰ *Reminder*" in msg and "06:30" in msg
    assert "🧍 *Customer summaries*" in msg


def test_undo_after_batch_only_reverses_last_write(conn, shop):
    _confirm_example(conn)
    msg, _b = handle_text(conn, SHOP, "Undo")
    assert "Reminder" in msg or "reminder" in msg
    assert conn.execute("SELECT status FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchone()["status"] == "cancelled"
    assert get_order_draft(conn, SHOP) is not None  # earlier writes intact
