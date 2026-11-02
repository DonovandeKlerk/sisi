"""Receipt↔intake linking: dedupe asks, cost-only links, separate payment.

Photo→receipt: a receipt matching a recent intake asks link/new instead
of double-adding stock. Receipt→photo: a photo matching a filed receipt
drops covered items. Payment is always explicit and separate.
"""

import json
from types import SimpleNamespace

import pytest
from test_format import assert_format

import app.interpreter as interp
from app import agent
from app.setup import skip_setup
from app.tools import (
    account_balance,
    add_stock,
    cash_balances,
    confirm_pending,
    get_intake_session,
    get_pending,
    photo_seen,
    propose_receipt,
    propose_stock_intake,
    record_purchase,
    set_float,
    start_guided_intake,
    supplier_balance,
    undo_last,
)
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID
OTHER = "27710000099"


@pytest.fixture()
def shop(conn):
    seed_demo(SHOP, conn)
    skip_setup(conn, SHOP)
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?",
                 (SHOP,))
    conn.commit()
    set_float(conn, SHOP, {"cash": 100000, "petty": 20000})
    return SHOP


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


def _stock(conn, name):
    return conn.execute("SELECT stock_qty FROM products"
                        " WHERE shop_id = ? AND name = ?",
                        (SHOP, name)).fetchone()["stock_qty"]


def _cost(conn, name):
    return conn.execute("SELECT cost_cents FROM products"
                        " WHERE shop_id = ? AND name = ?", (SHOP, name)
                        ).fetchone()["cost_cents"]


def _save_intake(conn, products, names, supplier="Jumbo", photo_hash="h"):
    propose_stock_intake(
        conn, SHOP, products, [{"product": n} for n in names],
        supplier, photo_hash)
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert "Stock saved" in msg


def _receipt_row(conn, no="R-1", shop=SHOP):
    return conn.execute("SELECT * FROM receipts WHERE shop_id = ?"
                        " AND receipt_no = ?", (shop, no)).fetchone()


# photo -> receipt ----------------------------------------------------------

def test_photo_receipt_link_costs_only_then_separate_payment(conn, shop):
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    assert _stock(conn, "Tastic Rice 2kg") == 8
    out = assert_format(propose_receipt(conn, SHOP, _receipt(),
                                       photo_hash="rh1"))
    assert "Reply *link*" in out and "or *new*" in out
    assert get_pending(conn, SHOP)["action_type"] == "link_choice"
    msg, _ = agent.handle_text(conn, SHOP, "link")
    assert_format(msg)
    assert "costs only" in msg and "Payment asked separately" in msg
    assert get_pending(conn, SHOP)["action_type"] == "receipt_link"
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert_format(msg)
    assert "Receipt linked" in msg and "Record stock payment?" in msg
    assert _stock(conn, "Tastic Rice 2kg") == 8  # no stock delta
    assert _cost(conn, "Tastic Rice 2kg") == 5500  # costs attached
    assert "• Till R1000.00" in cash_balances(conn, SHOP)  # not yet paid
    assert get_pending(conn, SHOP)["action_type"] == "record_purchase"
    data = json.loads(_receipt_row(conn)["data_json"])
    assert data["linked_action"]  # receipt row marked linked
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert "Stock payment saved" in msg
    assert "• Till R670.00" in cash_balances(conn, SHOP)
    assert undo_last(conn, SHOP) == "Undone: removed the stock purchase."
    assert "• Till R1000.00" in cash_balances(conn, SHOP)
    assert undo_last(conn, SHOP) == "Undone: unlinked the Jumbo receipt."
    assert _cost(conn, "Tastic Rice 2kg") is None  # stashed cost restored
    assert _stock(conn, "Tastic Rice 2kg") == 8
    assert _receipt_row(conn)["reversed"] == 1


def test_link_new_records_full_delivery(conn, shop):
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    propose_receipt(conn, SHOP, _receipt(), photo_hash="rh1")
    msg, _ = agent.handle_text(conn, SHOP, "new")
    assert msg.startswith("🧾 *Purchase: Jumbo*")
    assert "Paid from: Till" in msg
    confirm_pending(conn, SHOP)
    assert _stock(conn, "Tastic Rice 2kg") == 14  # 8 + 6 new
    assert "• Till R670.00" in cash_balances(conn, SHOP)
    data = json.loads(_receipt_row(conn)["data_json"])
    assert "linked_action" not in data


def test_partial_overlap_mixed_dispositions_and_undo(conn, shop):
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    lines = [_receipt()["lines"][0], _simba_line()]
    propose_receipt(conn, SHOP, _receipt(no="R-2", lines=lines, total=53000),
                    photo_hash="rh2")
    assert get_pending(conn, SHOP)["action_type"] == "link_choice"
    msg, _ = agent.handle_text(conn, SHOP, "same")
    assert "Tastic Rice 2kg: costs only" in msg
    assert "• +10 Simba Chips 120g" in msg and "stock 3 → 13" in msg
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert "1 costs attached" in msg and "1 new stock" in msg
    assert _stock(conn, "Tastic Rice 2kg") == 8
    assert _stock(conn, "Simba Chips 120g") == 13
    assert _cost(conn, "Simba Chips 120g") == 2000
    msg, _ = agent.handle_text(conn, SHOP, "yes")  # separate payment
    assert "• Till R470.00" in cash_balances(conn, SHOP)
    undo_last(conn, SHOP)
    assert "• Till R1000.00" in cash_balances(conn, SHOP)
    undo_last(conn, SHOP)
    assert _stock(conn, "Tastic Rice 2kg") == 8
    assert _stock(conn, "Simba Chips 120g") == 3  # added stock removed
    assert _cost(conn, "Simba Chips 120g") is None
    assert _cost(conn, "Tastic Rice 2kg") is None
    assert _receipt_row(conn, "R-2")["reversed"] == 1


def test_genuine_repeat_below_threshold_goes_straight(conn, shop):
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    # Same product, but a different supplier and no intake costs to
    # compare: overlap alone is below threshold.
    out = propose_receipt(conn, SHOP, _receipt(supplier="Devland", no="R-9"),
                          photo_hash="rh9")
    assert out.startswith("🧾 *Purchase: Devland*")
    assert get_pending(conn, SHOP)["action_type"] == "receipt"
    confirm_pending(conn, SHOP)
    assert _stock(conn, "Tastic Rice 2kg") == 14  # full new stock
    assert "• Till R670.00" in cash_balances(conn, SHOP)  # full payment


def test_payment_before_intake_link_declines_second_payment(conn, shop):
    record_purchase(conn, SHOP, 33000, account="cash",
                    note="paid at the till")
    confirm_pending(conn, SHOP)
    assert "• Till R670.00" in cash_balances(conn, SHOP)
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    propose_receipt(conn, SHOP, _receipt(), photo_hash="rh1")
    assert get_pending(conn, SHOP)["action_type"] == "link_choice"
    agent.handle_text(conn, SHOP, "link")
    msg, _ = agent.handle_text(conn, SHOP, "yes")  # link, then payment asked
    assert "Record stock payment?" in msg
    msg, _ = agent.handle_text(conn, SHOP, "no")  # already paid: decline
    assert _stock(conn, "Tastic Rice 2kg") == 8
    assert _cost(conn, "Tastic Rice 2kg") == 5500
    assert "• Till R670.00" in cash_balances(conn, SHOP)  # one payment only
    assert _receipt_row(conn)["reversed"] == 0  # link stands


def test_link_on_supplier_account_proposes_supplier_payment(conn, shop):
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    propose_receipt(conn, SHOP, _receipt(payment="account"),
                    photo_hash="rh1")
    assert get_pending(conn, SHOP)["action_type"] == "link_choice"
    agent.handle_text(conn, SHOP, "link")
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert "Receipt linked" in msg
    assert get_pending(conn, SHOP)["action_type"] == "record_purchase"
    assert supplier_balance(conn, SHOP, "Jumbo") == 0  # not yet confirmed
    agent.handle_text(conn, SHOP, "yes")
    assert supplier_balance(conn, SHOP, "Jumbo") == 33000
    assert "• Till R1000.00" in cash_balances(conn, SHOP)
    undo_last(conn, SHOP)
    assert supplier_balance(conn, SHOP, "Jumbo") == 0
    undo_last(conn, SHOP)
    assert _receipt_row(conn)["reversed"] == 1


def test_receipt_image_resend_is_dup(conn, shop):
    propose_receipt(conn, SHOP, _receipt(), photo_hash="samepic")
    confirm_pending(conn, SHOP)
    out = propose_receipt(conn, SHOP, _receipt(no="R-other"),
                          photo_hash="samepic")
    assert out == ("🧾 Already filed Jumbo R-1 (R330.00)."
                   " Send a different slip.")
    assert _stock(conn, "Tastic Rice 2kg") == 8  # filed once
    assert get_pending(conn, SHOP) is None


def test_double_yes_on_link_applies_once(conn, shop):
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    propose_receipt(conn, SHOP, _receipt(), photo_hash="rh1")
    agent.handle_text(conn, SHOP, "link")
    agent.handle_text(conn, SHOP, "yes")
    agent.handle_text(conn, SHOP, "no")  # decline the payment
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert "nothing" in msg.lower()
    assert _stock(conn, "Tastic Rice 2kg") == 8
    assert _cost(conn, "Tastic Rice 2kg") == 5500
    n = conn.execute("SELECT COUNT(*) n FROM receipts WHERE shop_id = ?",
                     (SHOP,)).fetchone()["n"]
    assert n == 1


def test_link_choice_reask_and_cancel(conn, shop):
    _save_intake(conn, [_prod("Tastic", "Rice", "2kg", 6)],
                 ["Tastic Rice 2kg"])
    propose_receipt(conn, SHOP, _receipt(), photo_hash="rh1")
    for word in ("maybe", "yes", "no"):
        msg, btns = agent.handle_text(conn, SHOP, word)
        assert "Reply *link*" in msg  # anything else re-asks
        assert btns == ["🔗 Link", "🆕 New"]
        assert get_pending(conn, SHOP)["action_type"] == "link_choice"
    msg, _ = agent.handle_text(conn, SHOP, "cancel")
    assert get_pending(conn, SHOP) is None  # cancel drops
    assert _stock(conn, "Tastic Rice 2kg") == 8
    out = propose_receipt(conn, SHOP, _receipt(), photo_hash="rh1")
    assert "Reply *link*" in out  # the slip can be proposed again


def test_shop_isolation_other_intake_never_matches(conn, shop):
    skip_setup(conn, OTHER)
    conn.execute("INSERT INTO products (shop_id, name, price_cents, stock_qty,"
                 " wholesaler) VALUES (?, 'Tastic Rice 2kg', 6500, 2, 'Jumbo')",
                 (OTHER,))
    conn.commit()
    propose_stock_intake(
        conn, OTHER, [_prod("Tastic", "Rice", "2kg", 6)],
        [{"product": "Tastic Rice 2kg"}], "Jumbo", "h-other")
    agent.handle_text(conn, OTHER, "yes")
    out = propose_receipt(conn, SHOP, _receipt(), photo_hash="rh1")
    assert out.startswith("🧾 *Purchase: Jumbo*")  # no cross-shop match
    assert get_pending(conn, SHOP)["action_type"] == "receipt"


# receipt -> photo ---------------------------------------------------------

def _save_receipt(conn, receipt=None, photo_hash="rh"):
    out = propose_receipt(conn, SHOP, receipt or _receipt(),
                          photo_hash=photo_hash)
    assert get_pending(conn, SHOP)["action_type"] == "receipt"
    confirm_pending(conn, SHOP)


def test_receipt_photo_guided_drops_matched_rest_onboarded(conn, shop):
    _save_receipt(conn)
    assert _stock(conn, "Tastic Rice 2kg") == 8
    out = assert_format(start_guided_intake(
        conn, SHOP, [_prod("Tastic", "Rice", "2kg", 6),
                     _prod("Whey", "Protein", "2kg", 1)],
        [{"product": "Tastic Rice 2kg"}, {"product": "Whey Protein 2kg"}],
        "add to stock", "ph1"))
    assert "same delivery? Reply *link* or *new*" in out
    assert get_pending(conn, SHOP)["action_type"] == "link_choice"
    msg, _ = agent.handle_text(conn, SHOP, "link")
    assert_format(msg)
    assert "already on the receipt" in msg and "Tastic Rice 2kg" in msg
    sess = get_intake_session(conn, SHOP)
    assert [i["name"] for i in sess["items_json"]] == ["Whey Protein 2kg"]
    data = json.loads(_receipt_row(conn)["data_json"])
    assert "linked_action" not in data  # mark rides on the save
    msg, _ = agent.handle_text(conn, SHOP, "R16")
    assert "selling price" in msg
    msg, _ = agent.handle_text(conn, SHOP, "sell R18")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert "Whey Protein 2kg" in msg
    assert _stock(conn, "Tastic Rice 2kg") == 8  # untouched
    data = json.loads(_receipt_row(conn)["data_json"])
    assert data["linked_action"].startswith("a")  # stamped by the save
    undo_last(conn, SHOP)
    data = json.loads(_receipt_row(conn)["data_json"])
    assert "linked_action" not in data  # undo clears the mark


def test_receipt_photo_all_matched_already_recorded(conn, shop):
    _save_receipt(conn)
    out = start_guided_intake(
        conn, SHOP, [_prod("Tastic", "Rice", "2kg", 6)],
        [{"product": "Tastic Rice 2kg"}], "add to stock", "ph1")
    assert "same delivery?" in out
    msg, _ = agent.handle_text(conn, SHOP, "link")
    assert_format(msg)
    assert "Already recorded" in msg and "Tastic Rice 2kg" in msg
    assert get_intake_session(conn, SHOP) is None
    assert get_pending(conn, SHOP) is None
    assert _stock(conn, "Tastic Rice 2kg") == 8
    data = json.loads(_receipt_row(conn)["data_json"])
    assert data["linked_action"] == "photo:ph1"
    assert photo_seen(conn, SHOP, "ph1") is not None  # resend hits dup guard
    # A repeated photo (new hash, same goods) no longer matches.
    out = start_guided_intake(
        conn, SHOP, [_prod("Tastic", "Rice", "2kg", 6)],
        [{"product": "Tastic Rice 2kg"}], "add to stock", "ph2")
    assert "same delivery?" not in out
    assert get_intake_session(conn, SHOP) is not None


def test_receipt_photo_oneshot_link_and_new(conn, shop):
    _save_receipt(conn)
    out = propose_stock_intake(
        conn, SHOP, [_prod("Tastic", "Rice", "2kg", 6),
                     _prod("Whey", "Protein", "2kg", 1)],
        [{"product": "Tastic Rice 2kg"}, {"product": "Whey Protein 2kg"}],
        "Jumbo", "ph1")
    assert "same delivery?" in out
    msg, _ = agent.handle_text(conn, SHOP, "link")
    assert "already on the receipt" in msg
    assert "• 1. Whey Protein 2kg" in msg and "• 2." not in msg
    data = json.loads(_receipt_row(conn)["data_json"])
    assert "linked_action" not in data
    msg, _ = agent.handle_text(conn, SHOP, "line 1: sell R18")
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert "Stock saved" in msg
    data = json.loads(_receipt_row(conn)["data_json"])
    assert data["linked_action"].startswith("a")
    undo_last(conn, SHOP)
    data = json.loads(_receipt_row(conn)["data_json"])
    assert "linked_action" not in data
    # new: the full photo set proceeds as a fresh delivery.
    out = propose_stock_intake(
        conn, SHOP, [_prod("Tastic", "Rice", "2kg", 6)],
        [{"product": "Tastic Rice 2kg"}], "Jumbo", "ph2")
    assert "same delivery?" in out  # mark was cleared, matches again
    msg, _ = agent.handle_text(conn, SHOP, "new")
    assert "📦 *Stock intake*" in msg and "Tastic Rice 2kg" in msg
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert _stock(conn, "Tastic Rice 2kg") == 14  # operator's double count
    data = json.loads(_receipt_row(conn)["data_json"])
    assert "linked_action" not in data


# explicit payment everywhere ------------------------------------------------

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


def test_combined_instruction_pays_only_when_stated(conn, shop, monkeypatch):
    _stub(monkeypatch, [{"kind": "stock_add", "quote": "",
                         "args": {"items": [{"product": "Coke 2L",
                                             "qty": 2}],
                                  "account": "petty", "pay_cents": 6000}}])
    msg, _ = agent.handle_text(conn, SHOP,
                               "receive 2 coke and pay R60 from petty cash")
    assert "Paid R60.00 from Petty cash" in msg
    agent.handle_text(conn, SHOP, "yes")
    assert "• Petty cash R140.00" in cash_balances(conn, SHOP)
    assert _stock(conn, "Coke 2L") == 7
    _stub(monkeypatch, [{"kind": "stock_add", "quote": "",
                         "args": {"items": [{"product": "Coke 2L",
                                             "qty": 2}]}}])
    msg, _ = agent.handle_text(conn, SHOP, "receive 2 coke")
    assert "Paid" not in msg
    agent.handle_text(conn, SHOP, "yes")
    assert "• Petty cash R140.00" in cash_balances(conn, SHOP)
    assert "• Till R1000.00" in cash_balances(conn, SHOP)
    assert _stock(conn, "Coke 2L") == 9


def test_record_purchase_unchanged(conn, shop):
    before = _stock(conn, "Coke 2L")
    msg = record_purchase(conn, SHOP, 5000, account="cash")
    assert "Record stock payment?" in msg
    confirm_pending(conn, SHOP)
    assert "• Till R950.00" in cash_balances(conn, SHOP)
    assert _stock(conn, "Coke 2L") == before  # stock never moves here
    record_purchase(conn, SHOP, 9000, account="supplier", supplier="Jumbo")
    confirm_pending(conn, SHOP)
    assert supplier_balance(conn, SHOP, "Jumbo") == 9000
    undo_last(conn, SHOP)
    assert supplier_balance(conn, SHOP, "Jumbo") == 0
