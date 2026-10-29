"""Receipt-link conversation gaps A–E: deferral, split preview, dup pay, pay carry, prices.

A. A pending link_choice is paused (deferred), never silently replaced.
B. The LINK preview sections every line; over-intake quantities split.
C. The link's payment offer skips exact dupes and warns near-matches.
D. Multi-item/photo pay args are noted, then asked after saving.
E. Links move cost_cents only; every cost change shows old→new.
"""

import json
import sqlite3
from types import SimpleNamespace

from test_format import assert_format

import app.interpreter as interp
from app import agent
from app.db import init_db
from app.setup import skip_setup
from app.tools import (
    account_balance,
    add_stock,
    answer_link_choice,
    cancel_deferred_link,
    cash_balances,
    confirm_pending,
    correct_link_line,
    get_deferred_link,
    get_intake_session,
    get_pending,
    propose_receipt,
    propose_stock_intake,
    record_purchase,
    resume_deferred_link,
    set_float,
    start_guided_intake,
    supplier_balance,
    undo_last,
)
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID
OTHER = "27710000099"


def _shop(conn, shop_id=SHOP):
    seed_demo(shop_id, conn)
    skip_setup(conn, shop_id)
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?",
                 (shop_id,))
    conn.commit()
    set_float(conn, shop_id, {"cash": 100000, "petty": 20000})
    return shop_id


def _prod(brand, variant="", size="", count=None):
    return {"brand": brand, "variant": variant, "size": size,
            "visible_count": count, "readable": True, "notes": ""}


def _receipt(supplier="Jumbo", no="R-1", lines=None, total=33000,
             payment="cash"):
    return {"supplier": supplier, "date": "2026-10-29", "receipt_no": no,
            "lines": lines if lines is not None else [
                {"line_no": 1, "text": "TASTIC 2KG x6 330.00",
                 "item": "Tastic 2kg", "packs": None, "pack_size": None,
                 "qty_units": 6, "unit_cost_cents": 5500,
                 "line_total_cents": 33000}],
            "vat_cents": None, "total_cents": total, "payment": payment}


def _simba_line(no=2, qty=10, unit_cost=2000):
    return {"line_no": no, "text": f"SIMBA x{qty}", "item": "Simba Chips 120g",
            "packs": None, "pack_size": None, "qty_units": qty,
            "unit_cost_cents": unit_cost,
            "line_total_cents": qty * unit_cost}


def _stock(conn, name, shop=SHOP):
    return conn.execute("SELECT stock_qty FROM products"
                        " WHERE shop_id = ? AND name = ?",
                        (shop, name)).fetchone()["stock_qty"]


def _cost(conn, name, shop=SHOP):
    return conn.execute("SELECT cost_cents FROM products"
                        " WHERE shop_id = ? AND name = ?", (shop, name)
                        ).fetchone()["cost_cents"]


def _prices(conn, shop=SHOP):
    return {r["name"]: (r["price_cents"], r["pack_price_cents"])
            for r in conn.execute("SELECT name, price_cents, pack_price_cents"
                                  " FROM products WHERE shop_id = ?",
                                  (shop,)).fetchall()}


def _save_intake(conn, products, names, supplier="Jumbo", photo_hash="h"):
    propose_stock_intake(
        conn, SHOP, products, [{"product": n} for n in names],
        supplier, photo_hash)
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert "Stock saved" in msg


def _receipt_row(conn, no="R-1", shop=SHOP):
    return conn.execute("SELECT * FROM receipts WHERE shop_id = ?"
                        " AND receipt_no = ?", (shop, no)).fetchone()


def _deferred(conn, shop=SHOP):
    return get_deferred_link(conn, shop)


def _stub(monkeypatch, tasks):
    def fake(messages):
        body = {"understood": "t", "tasks": tasks, "question": None,
                "options": []}
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2))

    monkeypatch.setattr(interp, "propose_plan", fake)


def _defer_tastic(conn):
    """A pending Tastic link_choice paused by an unrelated stock add."""
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    propose_receipt(conn, SHOP, _receipt(), photo_hash="rh1")
    pend = get_pending(conn, SHOP)
    assert pend["action_type"] == "link_choice"
    saved = pend["payload_json"]
    out = assert_format(add_stock(conn, SHOP, "Coke 2L", 2))
    assert "reply 'resume'" in out
    assert get_pending(conn, SHOP)["action_type"] == "add_stock"
    row = _deferred(conn)
    assert row is not None and row["payload_json"] == saved
    return saved


# A. link_choice preservation ----------------------------------------------


def test_defer_resume_nudge_roundtrip(conn):
    _shop(conn)
    _defer_tastic(conn)
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert "reply 'resume'" in msg  # nudge after the interrupt
    assert get_pending(conn, SHOP) is None
    assert _deferred(conn) is not None  # the question waited
    msg, btns = agent.handle_text(conn, SHOP, "resume")
    assert_format(msg)
    assert "Reply *link*" in msg and btns == ["link", "new"]
    assert get_pending(conn, SHOP)["action_type"] == "link_choice"
    assert _deferred(conn) is None
    msg, _ = agent.handle_text(conn, SHOP, "link")
    assert "costs only" in msg  # the restored question answers normally


def test_cancel_drops_deferred(conn):
    _shop(conn)
    _defer_tastic(conn)
    agent.handle_text(conn, SHOP, "yes")  # finish the interrupt
    msg, _ = agent.handle_text(conn, SHOP, "cancel")
    assert msg == "OK, dropped the paused receipt question."
    assert _deferred(conn) is None
    assert get_pending(conn, SHOP) is None
    assert resume_deferred_link(conn, SHOP) is None
    assert cancel_deferred_link(conn, SHOP) is None


def test_newer_link_choice_replaces_deferred_aloud(conn):
    _shop(conn)
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"], photo_hash="h1")
    _save_intake(conn, [_prod("Simba", "Chips", "120g", 10)],
                 ["Simba Chips 120g"], photo_hash="h2")
    propose_receipt(conn, SHOP, _receipt(), photo_hash="rh1")
    assert get_pending(conn, SHOP)["action_type"] == "link_choice"
    add_stock(conn, SHOP, "Coke 2L", 2)
    old = _deferred(conn)
    assert old is not None
    confirm_pending(conn, SHOP)
    out = assert_format(propose_receipt(
        conn, SHOP, _receipt(no="R-2", lines=[_simba_line(no=1)],
                             total=20000),
        photo_hash="rh2"))
    assert "Reply *link*" in out
    assert "Jumbo R-1" in out  # the dropped receipt is named
    assert conn.execute("SELECT status FROM pending_actions WHERE id = ?",
                        (old["id"],)).fetchone()["status"] == "superseded"
    msg, _ = agent.handle_text(conn, SHOP, "link")
    assert "Simba Chips 120g" in msg  # the newer question is live


def test_resume_beats_guided_resume(conn):
    _shop(conn)
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    propose_receipt(conn, SHOP, _receipt(), photo_hash="rh1")
    out = assert_format(start_guided_intake(
        conn, SHOP, [_prod("Whey", "Protein", "2kg", 1)],
        [{"product": "Whey Protein 2kg"}], "add to stock", "phx"))
    assert "reply 'resume'" in out  # the session paused the question
    assert get_intake_session(conn, SHOP) is not None
    msg, _ = agent.handle_text(conn, SHOP, "pause")
    assert "Paused" in msg
    msg, btns = agent.handle_text(conn, SHOP, "resume")
    assert "Reply *link*" in msg and btns == ["link", "new"]
    assert get_pending(conn, SHOP)["action_type"] == "link_choice"
    msg, _ = agent.handle_text(conn, SHOP, "cancel")
    assert get_pending(conn, SHOP) is None
    msg, _ = agent.handle_text(conn, SHOP, "resume")
    assert "Carrying on" in msg  # the session resumes once asked again


def test_deferred_invisible_to_confirm_deny(conn):
    _shop(conn)
    _defer_tastic(conn)
    agent.handle_text(conn, SHOP, "yes")
    assert get_pending(conn, SHOP) is None
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert "nothing" in msg.lower()
    assert _deferred(conn) is not None  # Yes never touches it
    msg, _ = agent.handle_text(conn, SHOP, "no")
    assert "nothing" in msg.lower()
    assert _deferred(conn) is not None  # No never touches it
    add_stock(conn, SHOP, "Coke 2L", 1)
    confirm_pending(conn, SHOP)  # confirms the live preview only
    assert _deferred(conn) is not None


def test_deferred_survives_restart(tmp_path):
    path = tmp_path / "file.db"
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    init_db(c)
    _shop(c)
    _save_intake(c, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    propose_receipt(c, SHOP, _receipt(), photo_hash="rh1")
    add_stock(c, SHOP, "Coke 2L", 2)
    assert _deferred(c) is not None
    c.close()
    c2 = sqlite3.connect(str(path))
    c2.row_factory = sqlite3.Row
    out = resume_deferred_link(c2, SHOP)
    assert "Reply *link*" in out
    assert get_pending(c2, SHOP)["action_type"] == "link_choice"
    msg, _ = answer_link_choice(c2, SHOP, "link")
    assert "costs only" in msg
    c2.close()


def test_deferred_isolation(conn):
    _shop(conn)
    _defer_tastic(conn)
    _shop(conn, OTHER)
    record_purchase(conn, OTHER, 5000, account="cash")
    msg = confirm_pending(conn, OTHER)
    assert "resume" not in msg  # shop B never sees shop A's pause
    assert resume_deferred_link(conn, OTHER) is None
    assert cancel_deferred_link(conn, OTHER) is None
    assert _deferred(conn, SHOP) is not None
    assert _deferred(conn, OTHER) is None
    assert "Reply *link*" in resume_deferred_link(conn, SHOP)


# B. preview exactness + correction ----------------------------------------


def test_link_preview_sections(conn):
    _shop(conn)
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    lines = [_receipt()["lines"][0], _simba_line(),
             {"line_no": 3, "text": "ELECTRICITY PREPAID 150.00",
              "item": "Electricity prepaid", "packs": None, "pack_size": None,
              "qty_units": None, "unit_cost_cents": None,
              "line_total_cents": 15000},
             {"line_no": 4, "text": "???", "item": "",
              "packs": None, "pack_size": None, "qty_units": None,
              "unit_cost_cents": None, "line_total_cents": None}]
    propose_receipt(conn, SHOP, _receipt(no="R-4", lines=lines, total=68000),
                    photo_hash="rh4")
    assert get_pending(conn, SHOP)["action_type"] == "link_choice"
    msg, _ = agent.handle_text(conn, SHOP, "link")
    assert_format(msg)
    assert "Costs only (already in stock)" in msg
    assert "New stock" in msg
    assert "Expenses" in msg
    assert "Couldn't read" in msg
    assert "? → R55.00" in msg  # matched cost change, explicit
    assert "• +10 Simba Chips 120g" in msg and "stock 3 → 13" in msg
    assert "line 2 is link" in msg  # correction hint on the preview


def test_split_surplus_adds_stock(conn):
    _shop(conn)
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    assert _stock(conn, "Tastic Rice 2kg") == 8
    big = {"line_no": 1, "text": "TASTIC 2KG x10 550.00",
           "item": "Tastic 2kg", "packs": None, "pack_size": None,
           "qty_units": 10, "unit_cost_cents": 5500,
           "line_total_cents": 55000}
    propose_receipt(conn, SHOP, _receipt(lines=[big], total=55000),
                    photo_hash="rhb")
    assert get_pending(conn, SHOP)["action_type"] == "link_choice"
    msg, _ = agent.handle_text(conn, SHOP, "link")
    assert_format(msg)
    assert "6 of slip 10 already in stock" in msg
    assert "• +4 Tastic Rice 2kg" in msg
    assert "stock 8 → 12" in msg
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert "1 costs attached" in msg and "1 new stock" in msg
    assert _stock(conn, "Tastic Rice 2kg") == 12  # surplus added
    assert _cost(conn, "Tastic Rice 2kg") == 5500
    assert get_pending(conn, SHOP)["action_type"] == "record_purchase"
    agent.handle_text(conn, SHOP, "no")  # decline the payment
    assert _receipt_row(conn)["reversed"] == 0  # link stands
    assert undo_last(conn, SHOP) == "Undone: unlinked the Jumbo receipt."
    assert _stock(conn, "Tastic Rice 2kg") == 8  # surplus removed
    assert _cost(conn, "Tastic Rice 2kg") is None
    assert _receipt_row(conn)["reversed"] == 1


def test_full_cover_costs_only_note(conn):
    _shop(conn)
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    propose_receipt(conn, SHOP, _receipt(), photo_hash="rh1")
    msg, _ = agent.handle_text(conn, SHOP, "link")
    assert "(slip 6 covered by intake 6)" in msg
    assert "New stock" not in msg  # nothing surplus, no section
    agent.handle_text(conn, SHOP, "yes")
    assert _stock(conn, "Tastic Rice 2kg") == 8  # no stock delta
    assert _cost(conn, "Tastic Rice 2kg") == 5500


def test_correct_link_line(conn):
    _shop(conn)
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    lines = [_receipt()["lines"][0], _simba_line()]
    propose_receipt(conn, SHOP, _receipt(no="R-2", lines=lines, total=53000),
                    photo_hash="rh2")
    agent.handle_text(conn, SHOP, "link")
    msg, _ = agent.handle_text(conn, SHOP, "line 1 is new")
    assert_format(msg)
    assert "• +6 Tastic Rice 2kg" in msg
    assert "Costs only" not in msg  # flipped to new-stock
    msg, _ = agent.handle_text(conn, SHOP, "line 1 is link")
    assert "costs only" in msg and "• +6 Tastic Rice 2kg" not in msg
    msg, _ = agent.handle_text(conn, SHOP, "line 2 is link")
    assert "• +10 Simba Chips 120g" in msg  # no intake overlap: all surplus
    msg, _ = agent.handle_text(conn, SHOP, "line 2 is new")
    assert "• +10 Simba Chips 120g" in msg
    before = get_pending(conn, SHOP)["payload_json"]
    msg, _ = agent.handle_text(conn, SHOP, "line 1 is maybe")
    assert "line 2 is link" in msg  # hint, no writes
    assert get_pending(conn, SHOP)["payload_json"] == before
    msg, _ = agent.handle_text(conn, SHOP, "line 9 is new")
    assert "isn't on this slip" in msg
    assert correct_link_line(conn, OTHER, 1, "link") == \
        "No link preview to correct."
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert _stock(conn, "Tastic Rice 2kg") == 8  # linked: costs only
    assert _stock(conn, "Simba Chips 120g") == 13  # new: stock added
    assert _cost(conn, "Tastic Rice 2kg") == 5500
    assert _cost(conn, "Simba Chips 120g") == 2000


# C. payment duplicate check ------------------------------------------------


def test_exact_payment_skips_offer(conn):
    _shop(conn)
    record_purchase(conn, SHOP, 33000, account="cash",
                    note="receipt Jumbo R-1")
    confirm_pending(conn, SHOP)
    assert "• Till R670.00" in cash_balances(conn, SHOP)
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    propose_receipt(conn, SHOP, _receipt(), photo_hash="rh1")
    agent.handle_text(conn, SHOP, "link")
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert_format(msg)
    assert "Receipt linked" in msg
    assert "already on the books" in msg and "receipt Jumbo R-1" in msg
    assert get_pending(conn, SHOP) is None  # nothing proposed
    assert "• Till R670.00" in cash_balances(conn, SHOP)  # paid once


def test_exact_supplier_move_skips_offer(conn):
    _shop(conn)
    record_purchase(conn, SHOP, 33000, account="supplier", supplier="Jumbo",
                    note="receipt Jumbo R-1")
    confirm_pending(conn, SHOP)
    assert supplier_balance(conn, SHOP, "Jumbo") == 33000
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    propose_receipt(conn, SHOP, _receipt(payment="account"),
                    photo_hash="rh1")
    agent.handle_text(conn, SHOP, "link")
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert "already on the books" in msg
    assert get_pending(conn, SHOP) is None
    assert supplier_balance(conn, SHOP, "Jumbo") == 33000  # booked once


def test_different_amount_proceeds_clean(conn):
    _shop(conn)
    record_purchase(conn, SHOP, 32000, account="cash",
                    note="paid Jumbo part")
    confirm_pending(conn, SHOP)
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    propose_receipt(conn, SHOP, _receipt(), photo_hash="rh1")
    agent.handle_text(conn, SHOP, "link")
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert "Record stock payment?" in msg  # proceeds
    assert "Possible duplicate" not in msg  # no false warning
    assert get_pending(conn, SHOP)["action_type"] == "record_purchase"


def test_heuristic_warns_and_decline_stands(conn):
    _shop(conn)
    record_purchase(conn, SHOP, 33000, account="cash",
                    note="paid Jumbo at the till")
    confirm_pending(conn, SHOP)
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    propose_receipt(conn, SHOP, _receipt(), photo_hash="rh1")
    agent.handle_text(conn, SHOP, "link")
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert_format(msg)
    assert "Possible duplicate" in msg
    assert "paid Jumbo at the till" in msg
    assert "Record stock payment?" in msg  # still a question
    assert get_pending(conn, SHOP)["action_type"] == "record_purchase"
    msg, _ = agent.handle_text(conn, SHOP, "no")
    assert _stock(conn, "Tastic Rice 2kg") == 8
    assert _cost(conn, "Tastic Rice 2kg") == 5500
    assert "• Till R670.00" in cash_balances(conn, SHOP)  # one payment
    assert _receipt_row(conn)["reversed"] == 0  # link stands


def test_supplier_heuristic_warns(conn):
    _shop(conn)
    record_purchase(conn, SHOP, 33000, account="supplier", supplier="Jumbo",
                    note="Jumbo on account")
    confirm_pending(conn, SHOP)
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    propose_receipt(conn, SHOP, _receipt(payment="account"),
                    photo_hash="rh1")
    agent.handle_text(conn, SHOP, "link")
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert_format(msg)
    assert "Possible duplicate" in msg
    agent.handle_text(conn, SHOP, "yes")  # eyes-open double
    assert supplier_balance(conn, SHOP, "Jumbo") == 66000
    undo_last(conn, SHOP)
    assert supplier_balance(conn, SHOP, "Jumbo") == 33000
    undo_last(conn, SHOP)
    assert _receipt_row(conn)["reversed"] == 1


# D. multi-item + pay carry-forward -----------------------------------------


def _pay_tasks(items, **kw):
    args = {"items": items, "supplier": "Jumbo", "account": "petty",
            "pay_cents": 6000}
    args.update(kw)
    return [{"kind": "stock_add", "quote": "", "args": args}]


def _two_items():
    return [{"product": "Coke 2L", "qty": 2, "cost_cents": 2000,
             "sell_cents": 2500},
            {"product": "Simba Chips 120g", "qty": 3, "cost_cents": 1500,
             "sell_cents": 2000}]


def test_multi_item_pay_noted_then_asked(conn, monkeypatch):
    _shop(conn)
    _stub(monkeypatch, _pay_tasks(_two_items()))
    msg, _ = agent.handle_text(
        conn, SHOP, "receive 2 coke and 3 simba, pay R60 from petty")
    assert_format(msg)
    assert "Payment noted" in msg and "R60.00" in msg
    assert "Petty cash" in msg
    assert "• Petty cash R200.00" in cash_balances(conn, SHOP)  # not yet paid
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert "Stock saved" in msg and "Record stock payment?" in msg
    assert get_pending(conn, SHOP)["action_type"] == "record_purchase"
    agent.handle_text(conn, SHOP, "yes")
    assert "• Petty cash R140.00" in cash_balances(conn, SHOP)
    assert _stock(conn, "Coke 2L") == 7
    assert _stock(conn, "Simba Chips 120g") == 6


def test_multi_item_pay_decline(conn, monkeypatch):
    _shop(conn)
    _stub(monkeypatch, _pay_tasks(_two_items()))
    agent.handle_text(conn, SHOP, "receive 2 coke and 3 simba, pay R60")
    agent.handle_text(conn, SHOP, "yes")
    assert get_pending(conn, SHOP)["action_type"] == "record_purchase"
    agent.handle_text(conn, SHOP, "no")
    assert "• Petty cash R200.00" in cash_balances(conn, SHOP)  # unpaid
    assert _stock(conn, "Coke 2L") == 7  # intake stands
    assert _stock(conn, "Simba Chips 120g") == 6
    assert get_pending(conn, SHOP) is None


def test_single_item_pay_stays_combined(conn, monkeypatch):
    _shop(conn)
    _stub(monkeypatch, _pay_tasks([_two_items()[0]]))
    msg, _ = agent.handle_text(conn, SHOP, "receive 2 coke and pay R60")
    assert "Paid R60.00 from Petty cash" in msg
    assert "Payment noted" not in msg
    pend = get_pending(conn, SHOP)
    assert pend["action_type"] == "add_stock"
    assert "pending_pay" not in json.loads(pend["payload_json"])
    agent.handle_text(conn, SHOP, "yes")
    assert get_pending(conn, SHOP) is None  # one Yes, no second offer
    assert "• Petty cash R140.00" in cash_balances(conn, SHOP)


def _guided_pay_session(conn, photo_hash="phpay"):
    return start_guided_intake(
        conn, SHOP, [_prod("Whey", "Protein", "2kg", 1),
                     _prod("Oreo", "Cookies", "200g", 1)],
        [{"product": "Whey Protein 2kg"}, {"product": "Oreo Cookies 200g"}],
        "receive and pay R60 from petty", photo_hash, "Jumbo",
        pending_pay={"amount": 6000, "account": "petty",
                     "supplier": "Jumbo"})


def test_guided_pay_final_only(conn):
    _shop(conn)
    out = assert_format(_guided_pay_session(conn))
    assert "Payment noted" in out and "R60.00" in out
    sess = get_intake_session(conn, SHOP)
    assert sess["pending_pay"] == {"amount": 6000, "account": "petty",
                                   "supplier": "Jumbo"}
    agent.handle_text(conn, SHOP, "R16")
    agent.handle_text(conn, SHOP, "sell R18")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert "Record stock payment?" not in msg  # mid-flow: quiet
    assert get_pending(conn, SHOP) is None
    assert get_intake_session(conn, SHOP) is not None  # still going
    agent.handle_text(conn, SHOP, "R10")
    agent.handle_text(conn, SHOP, "sell R12")
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert_format(msg)
    assert "Intake done" in msg and "Record stock payment?" in msg
    assert get_pending(conn, SHOP)["action_type"] == "record_purchase"
    agent.handle_text(conn, SHOP, "yes")
    assert "• Petty cash R140.00" in cash_balances(conn, SHOP)
    assert _stock(conn, "Whey Protein 2kg") == 1
    assert _stock(conn, "Oreo Cookies 200g") == 1
    undo_last(conn, SHOP)
    assert "• Petty cash R200.00" in cash_balances(conn, SHOP)


def test_guided_cancel_all_proposes_pay(conn):
    _shop(conn)
    start_guided_intake(
        conn, SHOP, [_prod("Whey", "Protein", "2kg", 1)],
        [{"product": "Whey Protein 2kg"}], "receive and pay R60", "phc",
        "Jumbo",
        pending_pay={"amount": 6000, "account": "petty",
                     "supplier": "Jumbo"})
    msg, _ = agent.handle_text(conn, SHOP, "cancel")
    assert "Reply 'cancel' again" in msg  # armed
    msg, _ = agent.handle_text(conn, SHOP, "cancel")
    assert_format(msg)
    assert "cancelled" in msg and "Record stock payment?" in msg
    assert get_pending(conn, SHOP)["action_type"] == "record_purchase"
    agent.handle_text(conn, SHOP, "no")
    assert "• Petty cash R200.00" in cash_balances(conn, SHOP)


def test_guided_skip_last_proposes_pay(conn):
    _shop(conn)
    start_guided_intake(
        conn, SHOP, [_prod("Whey", "Protein", "2kg", 1)],
        [{"product": "Whey Protein 2kg"}], "receive and pay R60", "phs",
        "Jumbo",
        pending_pay={"amount": 6000, "account": "petty",
                     "supplier": "Jumbo"})
    msg, _ = agent.handle_text(conn, SHOP, "skip item")
    assert "Intake done" in msg and "Record stock payment?" in msg
    assert get_pending(conn, SHOP)["action_type"] == "record_purchase"
    agent.handle_text(conn, SHOP, "yes")
    assert "• Petty cash R140.00" in cash_balances(conn, SHOP)


def test_photo_executor_stashes_pay(conn):
    _shop(conn)
    out, _ = interp._exec_stock_add(
        conn, SHOP,
        {"items": [{"product": "Whey Protein 2kg"}], "pay_cents": 6000,
         "account": "petty", "supplier": "Jumbo"},
        {"digest": "dx1", "text": "paid R60 from petty",
         "doc": {"products": [_prod("Whey", "Protein", "2kg", 1)]}})
    assert "Payment noted" in out
    sess = get_intake_session(conn, SHOP)
    assert sess is not None
    assert sess["pending_pay"] == {"amount": 6000, "account": "petty",
                                   "supplier": "Jumbo"}


def test_partial_pay_dropped(conn):
    _shop(conn)
    out, _ = interp._exec_stock_add(
        conn, SHOP,
        {"items": [{"product": "Coke 2L", "qty": 2},
                   {"product": "Simba Chips 120g", "qty": 3}],
         "account": "petty"},  # no pay_cents: nothing to ask later
        {"text": "receive 2 coke and 3 simba"})
    assert "Payment noted" not in out
    assert "pending_pay" not in json.loads(
        get_pending(conn, SHOP)["payload_json"])


# E. price preview on link ----------------------------------------------------


def test_link_cost_arrows(conn):
    _shop(conn)
    conn.execute("UPDATE products SET cost_cents = 1500"
                 " WHERE shop_id = ? AND name = 'Simba Chips 120g'", (SHOP,))
    conn.commit()
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    tea = {"line_no": 3, "text": "ZULU TEA 100G x5 50.00",
           "item": "Zulu Tea 100g", "packs": None, "pack_size": None,
           "qty_units": 5, "unit_cost_cents": 1000,
           "line_total_cents": 5000}
    lines = [_receipt()["lines"][0], _simba_line(), tea]
    propose_receipt(conn, SHOP, _receipt(no="R-5", lines=lines, total=58000),
                    photo_hash="rh5")
    msg, _ = agent.handle_text(conn, SHOP, "link")
    assert_format(msg)
    assert "? → R55.00" in msg  # matched
    assert "R15.00 → R18.85" in msg  # unmatched existing, changed
    assert "— → R10.00" in msg  # created


def test_sell_prices_untouched_by_link_and_undo(conn):
    _shop(conn)
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    before = _prices(conn)
    tea = {"line_no": 2, "text": "ZULU TEA 100G x5 50.00",
           "item": "Zulu Tea 100g", "packs": None, "pack_size": None,
           "qty_units": 5, "unit_cost_cents": 1000,
           "line_total_cents": 5000}
    propose_receipt(conn, SHOP, _receipt(no="R-6", lines=[
        _receipt()["lines"][0], tea], total=38000), photo_hash="rh6")
    agent.handle_text(conn, SHOP, "link")
    agent.handle_text(conn, SHOP, "yes")
    agent.handle_text(conn, SHOP, "no")  # decline the payment
    after = _prices(conn)
    for name, pair in before.items():
        assert after[name] == pair  # byte-identical sell prices
    assert after["Zulu Tea 100g"] == (None, None)  # created priceless
    tea_row = conn.execute("SELECT id, stock_qty FROM products"
                           " WHERE shop_id = ? AND name = ?",
                           (SHOP, "Zulu Tea 100g")).fetchone()
    assert tea_row["stock_qty"] == 5  # added exactly once, never doubled
    assert undo_last(conn, SHOP) == "Undone: unlinked the Jumbo receipt."
    assert _prices(conn) == before  # and identical after undo
    assert conn.execute("SELECT COUNT(*) c FROM products WHERE id = ?",
                        (tea_row["id"],)).fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM stock_moves"
                        " WHERE product_id = ?",
                        (tea_row["id"],)).fetchone()["c"] == 0


def test_new_strings_render_in_zulu(conn):
    from app.i18n import CURRENT_LANG
    _shop(conn)
    CURRENT_LANG.set("zu")
    out = propose_stock_intake(
        conn, SHOP, [], [{"product": "Coke 2L", "qty": 2},
                         {"product": "Simba Chips 120g", "qty": 3}],
        "Jumbo", "",
        pending_pay={"amount": 6000, "account": "petty",
                     "supplier": "Jumbo"})
    assert "Inkokhelo iqoshiwe" in out
    confirm_pending(conn, SHOP)  # one-shot intake saved (Zulu replies)
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    CURRENT_LANG.set("zu")  # agent turns reset the message language
    propose_receipt(conn, SHOP, _receipt(), photo_hash="rh1")
    out = add_stock(conn, SHOP, "Coke 2L", 2)
    assert "umisiwe" in out  # deferred hint
    out = resume_deferred_link(conn, SHOP)
    assert "embuzweni we-risiti" in out
    msg, _ = answer_link_choice(conn, SHOP, "link")
    assert "Izindleko kuphela (sesesitokweni)" in msg  # linked section
    assert "Yithi 'line 2 is link'" in msg  # correction hint
    assert correct_link_line(conn, SHOP, 1, "maybe").startswith("Yithi")
