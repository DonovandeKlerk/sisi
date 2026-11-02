"""End-to-end confirm-flow pins for guided intake (live-stall regression).

Drives the real webhook path (process_message + FakeSender outbox) over a
file DB: typed/button Yes, duplicate redelivery, restart, send failure,
full four-item flow with Undo, and resend recovery. Asserts stock rows and
ledger state, not just reply wording.
"""

import json
import sqlite3

import pytest

from app.db import init_db
from app.main import process_message
from app.setup import skip_setup
from app.whatsapp import IncomingMessage, get_sender

SHOP = "27790000001"


@pytest.fixture
def shop_db(tmp_path, monkeypatch):
    db = str(tmp_path / "e2e.db")
    monkeypatch.setenv("SISI_DB", db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    skip_setup(conn, SHOP)
    conn.commit()
    conn.close()
    get_sender().pop_shop(SHOP)
    return db


def _db(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _vprod(label, count=None):
    words = label.split()
    return {"brand": words[0], "variant": " ".join(words[1:]),
            "size": "", "visible_count": count, "readable": True,
            "notes": ""}


_MID = [0]


def _send(text, kind="text"):
    _MID[0] += 1
    process_message(IncomingMessage(f"e2e-{_MID[0]}", SHOP, kind, text, ""))
    return [m["body"] if isinstance(m, dict) else m
            for m in get_sender().pop_shop(SHOP)]


def _start(conn):
    from app.tools import start_guided_intake
    start_guided_intake(
        conn, SHOP,
        [_vprod("Albany Superior White Bread 700g", 1),
         _vprod("NPL Anabolic Whey 908g", 1),
         _vprod("Sunny Popcorn 500g", 2),
         _vprod("Risi Marie Multipack 3x150g", 1)],
        [], "add to stock", "e2e-photo-1")


def _stock(conn, name):
    row = conn.execute("SELECT stock_qty, cost_cents, price_cents FROM products"
                       " WHERE shop_id = ? AND name = ?", (SHOP, name)).fetchone()
    return None if row is None else tuple(row)


def _moves(conn):
    cash = conn.execute("SELECT COUNT(*) c FROM cash_moves"
                        " WHERE shop_id = ? AND reversed = 0", (SHOP,)).fetchone()["c"]
    sup = conn.execute("SELECT COUNT(*) c FROM supplier_moves"
                       " WHERE shop_id = ? AND reversed = 0", (SHOP,)).fetchone()["c"]
    return cash, sup


def _answer_item(cost, sell):
    out = _send(cost)[-1]
    assert "selling price" in out
    out = _send(sell)[-1]
    assert "Reply Yes to save" in out


def test_button_yes_saves_item1_once_and_asks_item2(shop_db):
    conn = _db(shop_db)
    _start(conn)
    conn.close()
    _answer_item("R16", "sell R22")
    out = _send("Yes", kind="button")[-1]
    assert "Saved Albany Superior White Bread 700g" in out
    assert "Item 2 of 4" in out and "NPL Anabolic Whey 908g" in out
    conn = _db(shop_db)
    try:
        assert _stock(conn, "Albany Superior White Bread 700g") == (1.0, 1600, 2200)
        n = conn.execute("SELECT COUNT(*) c FROM stock_moves"
                         " WHERE shop_id = ? AND reason = 'restock'",
                         (SHOP,)).fetchone()["c"]
        assert n == 1  # exactly one stock addition
        pend = conn.execute("SELECT status FROM pending_actions"
                            " WHERE shop_id = ? ORDER BY id DESC LIMIT 1",
                            (SHOP,)).fetchone()
        assert pend["status"] == "confirmed"
        sess = conn.execute("SELECT current, field FROM intake_sessions"
                            " WHERE shop_id = ?", (SHOP,)).fetchone()
        assert (sess["current"], sess["field"]) == (1, "cost")
        assert _moves(conn) == (0, 0)
    finally:
        conn.close()


def test_typed_yes_matches_button(shop_db):
    conn = _db(shop_db)
    _start(conn)
    conn.close()
    _answer_item("R16", "sell R22")
    out = _send("Yes")[-1]
    assert "Saved Albany Superior White Bread 700g" in out
    assert "Item 2 of 4" in out
    conn = _db(shop_db)
    try:
        assert _stock(conn, "Albany Superior White Bread 700g") == (1.0, 1600, 2200)
    finally:
        conn.close()


def test_infobip_button_payload_confirms(shop_db, monkeypatch):
    import app.main as main_mod
    from app.infobip import parse_infobip_webhook
    monkeypatch.setattr(main_mod, "sender_for_channel",
                        lambda channel: get_sender())
    conn = _db(shop_db)
    _start(conn)
    conn.close()
    _answer_item("R16", "sell R22")

    def _btn(text, mid):
        return {"results": [{
            "from": SHOP, "to": "447860088970", "messageId": mid,
            "receivedAt": "2026-10-30T03:39:33.000Z",
            "message": {"type": "INTERACTIVE_BUTTON_REPLY",
                        "id": text, "title": text}}]}

    msgs = parse_infobip_webhook(_btn("Yes", "e2e-btn-1"))
    assert len(msgs) == 1 and msgs[0].kind == "button" and msgs[0].text == "Yes"
    for m in msgs:
        process_message(m)
    out = get_sender().pop_shop(SHOP)[-1]["body"]
    assert "Saved Albany Superior White Bread 700g" in out
    conn = _db(shop_db)
    try:
        assert _stock(conn, "Albany Superior White Bread 700g")[0] == 1.0
    finally:
        conn.close()


def test_duplicate_redelivery_sends_nothing_and_saves_once(shop_db):
    conn = _db(shop_db)
    _start(conn)
    conn.close()
    _answer_item("R16", "sell R22")
    process_message(IncomingMessage("e2e-dup-1", SHOP, "button", "Yes", ""))
    first = [m["body"] if isinstance(m, dict) else m
             for m in get_sender().pop_shop(SHOP)]
    assert any("Saved Albany" in m for m in first)
    process_message(IncomingMessage("e2e-dup-1", SHOP, "button", "Yes", ""))
    assert get_sender().pop_shop(SHOP) == []  # redelivery: silent, safe
    conn = _db(shop_db)
    try:
        assert _stock(conn, "Albany Superior White Bread 700g")[0] == 1.0
        n = conn.execute("SELECT COUNT(*) c FROM stock_moves"
                         " WHERE shop_id = ?", (SHOP,)).fetchone()["c"]
        assert n == 1
    finally:
        conn.close()


def test_second_distinct_yes_never_confirms_item2(shop_db):
    conn = _db(shop_db)
    _start(conn)
    conn.close()
    _answer_item("R16", "sell R22")
    _send("Yes", kind="button")
    out = _send("Yes")[-1]  # fresh delivery, nothing pending
    assert "Saved" not in out
    conn = _db(shop_db)
    try:
        assert _stock(conn, "Albany Superior White Bread 700g")[0] == 1.0
        assert _stock(conn, "NPL Anabolic Whey 908g") is None  # item 2 untouched
        sess = conn.execute("SELECT current FROM intake_sessions"
                            " WHERE shop_id = ?", (SHOP,)).fetchone()
        assert sess["current"] == 1
    finally:
        conn.close()


def test_restart_mid_intake_resumes(shop_db):
    conn = _db(shop_db)
    _start(conn)
    conn.close()
    _answer_item("R16", "sell R22")
    _send("Yes")  # worker "dies" here: close everything, reopen below
    conn = _db(shop_db)  # fresh process, same file
    try:
        sess = conn.execute("SELECT current, field FROM intake_sessions"
                            " WHERE shop_id = ?", (SHOP,)).fetchone()
        assert (sess["current"], sess["field"]) == (1, "cost")
    finally:
        conn.close()
    out = _send("R450")[-1]  # item 2 cost answered after restart
    assert "selling price" in out
    conn = _db(shop_db)
    try:
        assert _stock(conn, "Albany Superior White Bread 700g")[0] == 1.0
    finally:
        conn.close()


def test_send_failure_falls_back_and_retry_stays_safe(shop_db, monkeypatch):
    import app.sendqueue as sq

    calls = []
    orig_wrap = sq.QueuedSender.wrap

    def boom_wrap(sender, conn, channel):
        out = orig_wrap(sender, conn, channel)

        def boom(*a, **k):
            raise RuntimeError("send down")

        out.flush = boom
        return out

    def fake_immediate(channel, to, kind, payload):
        calls.append(payload.get("body", ""))
        return {}

    monkeypatch.setattr(sq.QueuedSender, "wrap", staticmethod(boom_wrap))
    monkeypatch.setattr(sq, "send_immediate", fake_immediate)
    conn = _db(shop_db)
    _start(conn)
    conn.close()
    _answer_item("R16", "sell R22")  # sends fail, fallbacks fire, no crash
    assert calls, "friendly fallback never attempted"
    n_before = len(calls)
    process_message(IncomingMessage("e2e-dup-1", SHOP, "button", "Yes", ""))
    process_message(IncomingMessage("e2e-dup-1", SHOP, "button", "Yes", ""))
    conn = _db(shop_db)
    try:
        assert _stock(conn, "Albany Superior White Bread 700g")[0] == 1.0
    finally:
        conn.close()
    assert len(calls) == n_before + 1  # redelivery adds no new fallback


def test_full_four_items_summary_undo_and_no_money(shop_db):
    conn = _db(shop_db)
    _start(conn)
    conn.close()
    _answer_item("R16", "sell R22")
    _send("Yes")
    _answer_item("R450", "sell R520")
    _send("Yes")
    _answer_item("R9", "sell R12")
    _send("Yes")
    out = _send("R30")[-1]
    assert "selling price" in out
    out = _send("sell R8")[-1]
    out = _send("singles")[-1]
    out = _send("3 in each pack")[-1]
    out = _send("2")[-1]
    assert "for each pack or for one packet" in out
    out = _send("each pack")[-1]
    assert "Reply Yes to save" in out
    out = _send("2 packs")[-1]  # preview correction: 1 → 2 packs
    assert "Reply Yes to save" in out
    msgs = _send("Yes")
    assert any("saved 4" in m.lower() or "4 saved" in m.lower()
               or ("Albany" in m and "Risi" in m) for m in msgs)
    # Completed intake queues the post-task overview image + main menu
    # after the summary.
    assert "Main menu" in msgs[-1]
    conn = _db(shop_db)
    try:
        assert _stock(conn, "Albany Superior White Bread 700g")[0] == 1.0
        assert _stock(conn, "NPL Anabolic Whey 908g")[0] == 1.0
        assert _stock(conn, "Sunny Popcorn 500g")[0] == 2.0
        assert _stock(conn, "Risi Marie Multipack 3x150g")[0] == 6.0
        assert _moves(conn) == (0, 0)
        assert conn.execute("SELECT COUNT(*) c FROM intake_sessions"
                            " WHERE shop_id = ?", (SHOP,)).fetchone()["c"] == 0
    finally:
        conn.close()
    msgs = _send("undo")  # undo reply, then overview image + main menu
    assert any("Risi" in m for m in msgs)
    assert "Main menu" in msgs[-1]
    conn = _db(shop_db)
    try:
        assert _stock(conn, "Risi Marie Multipack 3x150g") is None
        assert _stock(conn, "Sunny Popcorn 500g")[0] == 2.0  # others intact
        assert _moves(conn) == (0, 0)
    finally:
        conn.close()


def test_existing_item2_previews_immediately(shop_db):
    from seed.seed_demo import seed_demo
    conn = _db(shop_db)
    seed_demo(SHOP, conn)
    from app.setup import skip_setup
    skip_setup(conn, SHOP)
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?",
                 (SHOP,))
    conn.commit()
    from app.tools import start_guided_intake
    start_guided_intake(
        conn, SHOP,
        [_vprod("Albany Superior White Bread 700g", 1),
         _vprod("Tastic Rice 2kg", 2)],
        [], "add to stock", "e2e-photo-2")
    conn.close()
    _answer_item("R16", "sell R22")
    out = _send("Yes")[-1]  # item 2 already complete → straight to preview
    assert "Item 2 of 2" in out and "Reply Yes to save" in out
    out = _send("Yes")[-1]
    conn = _db(shop_db)
    try:
        row = conn.execute("SELECT stock_qty, price_cents FROM products"
                           " WHERE shop_id = ? AND name = ?",
                           (SHOP, "Tastic Rice 2kg")).fetchone()
        assert row["stock_qty"] == 4.0  # 2 + 2, price untouched below
        assert row["price_cents"] == 6500
    finally:
        conn.close()


def test_resend_recovers_current_question_without_resaving(shop_db):
    conn = _db(shop_db)
    _start(conn)
    conn.close()
    _answer_item("R16", "sell R22")
    saved = _send("Yes", kind="button")[-1]
    assert "Saved Albany" in saved
    # Session advanced: resend recovers the CURRENT question, never the
    # stale ack and never obsolete Yes instructions.
    out = _send("resend")[-1]
    assert "Item 2 of 4" in out and "NPL Anabolic Whey 908g" in out
    assert "cost" in out.lower()
    assert "Reply Yes to save" not in out
    assert "Saved Albany" not in out
    again = _send("resend")[-1]
    assert again == out  # repeatable, never accumulates
    conn = _db(shop_db)
    try:
        assert _stock(conn, "Albany Superior White Bread 700g")[0] == 1.0
        assert _moves(conn) == (0, 0)
        pend = conn.execute("SELECT COUNT(*) c FROM pending_actions"
                            " WHERE shop_id = ? AND status = 'pending'",
                            (SHOP,)).fetchone()["c"]
        assert pend == 0  # nothing auto-confirmed, nothing new pending
    finally:
        conn.close()


def test_resend_live_preview_returns_buttons(shop_db):
    conn = _db(shop_db)
    _start(conn)
    conn.close()
    _answer_item("R16", "sell R22")  # item-1 preview now pending
    process_message(IncomingMessage("e2e-rs-1", SHOP, "text", "resend", ""))
    got = get_sender().pop_shop(SHOP)[-1]
    assert got["type"] == "buttons"  # live preview keeps working buttons
    assert got["buttons"] == ["Yes", "No"]
    assert "Reply Yes to save" in got["body"]
    assert "Albany Superior White Bread 700g" in got["body"]
    out = _send("Yes")[-1]  # the resent preview confirms normally
    assert "Saved Albany" in out
    conn = _db(shop_db)
    try:
        assert _stock(conn, "Albany Superior White Bread 700g")[0] == 1.0
    finally:
        conn.close()


def test_resend_filler_variant_does_not_clobber(shop_db):
    conn = _db(shop_db)
    _start(conn)
    conn.close()
    _answer_item("R16", "sell R22")
    saved = _send("Yes", kind="button")[-1]
    assert "Saved Albany" in saved
    conn = _db(shop_db)  # failed-media record, as the media worker leaves it
    conn.execute("INSERT INTO flags (shop_id, key, value) VALUES (?, ?, ?)",
                 (SHOP, "last-failed-media", json.dumps(
                     {"kind": "image", "url": "https://x/y.png",
                      "caption": "c", "filename": "y.png"})))
    conn.commit()
    conn.close()
    got = _send("resend please")  # media path taken (+queued media item)
    assert any("again" in m for m in got)
    out = _send("resend")[-1]  # session open: current question, not the ack
    assert "Item 2 of 4" in out
    conn = _db(shop_db)  # and the stored last reply is intact underneath
    try:
        val = conn.execute("SELECT value FROM flags WHERE shop_id = ?"
                           " AND key = 'last-reply'", (SHOP,)).fetchone()["value"]
        assert val == saved
    finally:
        conn.close()


def test_resend_without_session_returns_last_reply(shop_db):
    _send("menu")
    conn = _db(shop_db)
    try:
        stored = conn.execute("SELECT value FROM flags WHERE shop_id = ?"
                              " AND key = 'last-reply'", (SHOP,)).fetchone()["value"]
    finally:
        conn.close()
    assert stored and "Main menu" in stored
    assert _send("resend")[-1] == stored  # verbatim, nothing pending


def test_resend_with_nothing_recorded(shop_db):
    out = _send("resend")[-1]
    assert out  # short "nothing to resend" text, never a crash
    conn = _db(shop_db)
    try:
        assert _moves(conn) == (0, 0)
    finally:
        conn.close()
