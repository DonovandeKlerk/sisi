"""Tools + confirm/undo engine, all offline."""

from seed.seed_demo import DEMO_SHOP_ID, seed_demo
from app.tools import (
    TOOL_DEFS,
    TOOL_FUNCS,
    add_credit,
    add_stock,
    balance_cents,
    cancel_pending,
    cash_up,
    check_expiry_photo,
    compliance_guide,
    confirm_pending,
    demo_reset,
    draft_reminder,
    get_balance,
    get_pending,
    list_debtors,
    low_stock,
    read_slip_photo,
    record_payment,
    record_sale,
    restock_list,
    set_price,
    undo_last,
)

SHOP = DEMO_SHOP_ID


def _seeded(conn):
    return seed_demo(SHOP, conn)


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) c FROM {table} WHERE shop_id = ?", (SHOP,)).fetchone()["c"]


def _customer_id(conn, name):
    return conn.execute(
        "SELECT id FROM customers WHERE shop_id = ? AND name = ?", (SHOP, name)).fetchone()["id"]


def _stock(conn, name):
    return conn.execute(
        "SELECT stock_qty FROM products WHERE shop_id = ? AND name = ?", (SHOP, name)).fetchone()["stock_qty"]


# --- credit book -----------------------------------------------------------


def test_credit_propose_confirm_undo(conn):
    _seeded(conn)
    before = balance_cents(conn, SHOP, _customer_id(conn, "Sipho"))
    msg = add_credit(conn, SHOP, "Sipho", [{"product": "Coke 2L", "qty": 1}])
    assert "R36.00" in msg and "New balance" in msg
    assert get_pending(conn, SHOP) is not None
    assert balance_cents(conn, SHOP, _customer_id(conn, "Sipho")) == before  # nothing saved yet
    out = confirm_pending(conn, SHOP)
    assert "Saved" in out
    assert balance_cents(conn, SHOP, _customer_id(conn, "Sipho")) == before + 3600
    assert get_pending(conn, SHOP) is None
    undone = undo_last(conn, SHOP)
    assert "Undone" in undone
    assert balance_cents(conn, SHOP, _customer_id(conn, "Sipho")) == before


def test_preview_numbers_match_reality(conn):
    _seeded(conn)
    msg = add_credit(conn, SHOP, "Lerato",
                     [{"product": "Sasko White Bread 700g", "qty": 2}, {"product": "Coke 2L", "qty": 1}])
    assert "R73.00" in msg  # 2x18.50 + 36.00, computed never guessed
    assert "New balance R73.00" in msg
    confirm_pending(conn, SHOP)
    assert get_balance(conn, SHOP, "Lerato") == "💳 *Lerato* owes R73.00."


def test_decline_discards(conn):
    _seeded(conn)
    add_credit(conn, SHOP, "Sipho", [{"product": "Coke 2L", "qty": 1}])
    assert cancel_pending(conn, SHOP) == "OK, I did not save it."
    assert _count(conn, "credit_entries") == 3  # seed rows only
    assert get_pending(conn, SHOP) is None


def test_new_proposal_supersedes_old(conn):
    _seeded(conn)
    add_credit(conn, SHOP, "Sipho", [{"product": "Coke 2L", "qty": 1}])
    add_credit(conn, SHOP, "Thandi", [{"product": "Coke 2L", "qty": 1}])
    pendings = conn.execute(
        "SELECT COUNT(*) c FROM pending_actions WHERE shop_id = ? AND status = 'pending'",
        (SHOP,)).fetchone()["c"]
    assert pendings == 1
    confirm_pending(conn, SHOP)
    assert "💳 *Thandi* owes R81.50" in get_balance(conn, SHOP, "Thandi")  # 45.50 + 36.00


def test_missing_price_is_a_question(conn):
    _seeded(conn)
    add_stock(conn, SHOP, "Amagwinya", 10)
    confirm_pending(conn, SHOP)
    out = add_credit(conn, SHOP, "Sipho", [{"product": "Amagwinya", "qty": 2}])
    assert out == "❓ What is the price of Amagwinya?"
    assert get_pending(conn, SHOP) is None


def test_unknown_and_ambiguous_products(conn):
    _seeded(conn)
    assert "isn't on your list yet" in add_credit(conn, SHOP, "Sipho", [{"product": "Unicorn Flakes", "qty": 1}])
    out = add_credit(conn, SHOP, "Sipho", [{"product": "bread", "qty": 1}])
    assert out.startswith("❓ *Which one?*")
    assert "Sasko" in out and "Albany" in out
    assert get_pending(conn, SHOP) is None


def test_payment_flow_and_undo(conn):
    _seeded(conn)
    msg = record_payment(conn, SHOP, "Thandi", 5000)
    assert "paid R50.00" in msg and "New balance -R4.50" in msg
    confirm_pending(conn, SHOP)
    assert get_balance(conn, SHOP, "Thandi") == "✅ *Thandi* is paid up with R4.50 credit."
    undo_last(conn, SHOP)
    assert get_balance(conn, SHOP, "Thandi") == "💳 *Thandi* owes R45.50."


def test_payment_unknown_customer(conn):
    _seeded(conn)
    assert record_payment(conn, SHOP, "Nobody", 1000) == "No one named Nobody is on the book."
    assert get_pending(conn, SHOP) is None


def test_balances_and_debtors(conn):
    _seeded(conn)
    assert get_balance(conn, SHOP, "Sipho") == "💳 *Sipho* owes R73.00."
    assert get_balance(conn, SHOP, "Lerato") == "✅ *Lerato* owes nothing. The book is clean."
    out = list_debtors(conn, SHOP)
    assert "Jabu — R120.00" in out and "Total: R238.50." in out


def test_undo_is_lifo(conn):
    _seeded(conn)
    add_credit(conn, SHOP, "Lerato", [{"product": "Coke 2L", "qty": 1}])
    confirm_pending(conn, SHOP)
    record_payment(conn, SHOP, "Lerato", 1000)
    confirm_pending(conn, SHOP)
    undo_last(conn, SHOP)  # removes the payment first
    assert get_balance(conn, SHOP, "Lerato") == "💳 *Lerato* owes R36.00."
    undo_last(conn, SHOP)
    assert get_balance(conn, SHOP, "Lerato") == "✅ *Lerato* owes nothing. The book is clean."


def test_undo_nothing(conn):
    _seeded(conn)
    assert undo_last(conn, SHOP) == "There is nothing to undo."  # seed rows excluded


# --- sales and stock -------------------------------------------------------


def test_cash_sale_and_undo(conn):
    _seeded(conn)
    msg = record_sale(conn, SHOP, "Coke 2L", 2)
    assert "R72.00 cash" in msg and "Stock left 3" in msg
    confirm_pending(conn, SHOP)
    assert _stock(conn, "Coke 2L") == 3
    assert _count(conn, "sales") == 1
    undo_last(conn, SHOP)
    assert _stock(conn, "Coke 2L") == 5
    assert cash_up(conn, SHOP).endswith("Nothing recorded yet.")


def test_credit_sale_links_book_and_stock(conn):
    _seeded(conn)
    msg = record_sale(conn, SHOP, "Simba Chips 120g", 1, buyer="Bongani")
    assert "to Bongani" in msg
    confirm_pending(conn, SHOP)
    assert get_balance(conn, SHOP, "Bongani") == "💳 *Bongani* owes R24.00."
    assert _stock(conn, "Simba Chips 120g") == 2
    sale = conn.execute("SELECT * FROM sales WHERE shop_id = ?", (SHOP,)).fetchone()
    cred = conn.execute(
        "SELECT * FROM credit_entries WHERE shop_id = ? AND customer_id = ? ORDER BY id DESC LIMIT 1",
        (SHOP, _customer_id(conn, "Bongani"))).fetchone()
    assert sale["action_id"] == cred["action_id"]  # one action, reversed together
    undo_last(conn, SHOP)
    assert get_balance(conn, SHOP, "Bongani") == "✅ *Bongani* owes nothing. The book is clean."
    assert _stock(conn, "Simba Chips 120g") == 3


def test_short_stock_warns_but_saves(conn):
    _seeded(conn)
    msg = record_sale(conn, SHOP, "Simba Chips 120g", 5)
    assert "short" in msg
    confirm_pending(conn, SHOP)
    assert _stock(conn, "Simba Chips 120g") == -2


def test_add_stock_and_set_price(conn):
    _seeded(conn)
    msg = add_stock(conn, SHOP, "Airtime R10", 20, wholesaler="Flash")
    assert "New: Airtime R10" in msg and "Price still needed" in msg
    confirm_pending(conn, SHOP)
    assert "Airtime R10 → R10.00" in set_price(conn, SHOP, "Airtime R10", 1000)
    confirm_pending(conn, SHOP)
    add_credit(conn, SHOP, "Jabu", [{"product": "Airtime", "qty": 1}])
    confirm_pending(conn, SHOP)
    assert get_balance(conn, SHOP, "Jabu") == "💳 *Jabu* owes R130.00."
    undo_last(conn, SHOP)
    undo_last(conn, SHOP)  # price removed
    row = conn.execute("SELECT price_cents FROM products WHERE shop_id = ? AND name = ?",
                       (SHOP, "Airtime R10")).fetchone()
    assert row["price_cents"] is None


def test_add_stock_keeps_known_wholesaler(conn):
    _seeded(conn)
    add_stock(conn, SHOP, "Coke 2L", 5, wholesaler="depot")
    confirm_pending(conn, SHOP)
    row = conn.execute("SELECT wholesaler, stock_qty FROM products WHERE shop_id = ? AND name = ?",
                       (SHOP, "Coke 2L")).fetchone()
    assert (row["wholesaler"], row["stock_qty"]) == ("Coke Depot", 10)
    add_stock(conn, SHOP, "New Beans", 3, wholesaler="Jumbo")
    confirm_pending(conn, SHOP)
    row = conn.execute("SELECT wholesaler FROM products WHERE shop_id = ? AND name = ?",
                       (SHOP, "New Beans")).fetchone()
    assert row["wholesaler"] == "Jumbo"


def test_low_stock_and_restock(conn):
    _seeded(conn)
    low = low_stock(conn, SHOP)
    for name in ("Tastic Rice 2kg", "Coke 2L", "Simba Chips 120g", "Fresh Milk 1L"):
        assert name in low
    restock = restock_list(conn, SHOP)
    assert restock.startswith("🛒 *Buy tomorrow*")
    for wholesaler in ("Devland", "Jumbo", "Coke Depot"):
        assert wholesaler in restock
    assert "• Tastic Rice 2kg — have 2, order 6" in restock


def test_cash_up(conn):
    _seeded(conn)
    record_sale(conn, SHOP, "Coke 2L", 2)
    confirm_pending(conn, SHOP)
    record_sale(conn, SHOP, "Stoney 2L", 1, buyer="Sipho")
    confirm_pending(conn, SHOP)
    record_payment(conn, SHOP, "Thandi", 4550)
    confirm_pending(conn, SHOP)
    day = conn.execute(
        "SELECT substr(datetime(created_at, '+2 hours'), 1, 10) d FROM sales LIMIT 1"
    ).fetchone()["d"]
    out = cash_up(conn, SHOP, day)
    assert "• Cash sales R72.00 (1 sales)" in out
    assert "• Book sales R33.00" in out
    assert "• Payments in R45.50" in out
    assert "💵 *In the drawer:* R117.50" in out


def test_cash_up_uses_sast_day(conn):
    _seeded(conn)
    record_sale(conn, SHOP, "Coke 2L", 1)
    confirm_pending(conn, SHOP)
    conn.execute("UPDATE sales SET created_at = '2026-10-26T23:30:00.000Z' WHERE shop_id = ?",
                 (SHOP,))
    conn.commit()
    assert "R36.00" in cash_up(conn, SHOP, "2026-10-27")  # 01:30 SAST next day
    assert cash_up(conn, SHOP, "2026-10-26").endswith("Nothing recorded yet.")


# --- photo findings --------------------------------------------------------


def test_expiry_flow_and_undo(conn, monkeypatch):
    _seeded(conn)
    monkeypatch.setenv("SISI_TODAY", "2026-11-01")
    msg = check_expiry_photo(conn, SHOP, [
        {"product": "Fresh Milk 1L", "qty": 2, "expiry_date": "2026-10-30"},
        {"product": "Tastic Rice 2kg", "qty": 1, "expiry_date": "2027-06-01"},
    ])
    assert "• 2 × Fresh Milk 1L 30 Oct — expired. Take them off the shelf." in msg
    before = _count(conn, "expiry_items")
    confirm_pending(conn, SHOP)
    assert _count(conn, "expiry_items") == before + 2
    undo_last(conn, SHOP)
    assert _count(conn, "expiry_items") == before


def test_expiry_all_fine(conn, monkeypatch):
    _seeded(conn)
    monkeypatch.setenv("SISI_TODAY", "2026-11-01")
    out = check_expiry_photo(conn, SHOP, [
        {"product": "Tastic Rice 2kg", "qty": 1, "expiry_date": "2027-06-01"}])
    assert out == "✅ All dates look fine. Nothing expiring soon."
    assert get_pending(conn, SHOP) is None


def test_slip_flow_and_undo(conn):
    _seeded(conn)
    msg = read_slip_photo(conn, SHOP, [
        {"product": "Coke 2L", "qty": 12, "cost_cents": 3000},
        {"product": "Oros 2L", "qty": 6},
    ], wholesaler="Coke Depot")
    assert "Save slip to stock?" in msg and "[new]" in msg
    confirm_pending(conn, SHOP)
    assert _stock(conn, "Coke 2L") == 17
    cost = conn.execute("SELECT cost_cents FROM products WHERE shop_id = ? AND name = ?",
                        (SHOP, "Coke 2L")).fetchone()["cost_cents"]
    assert cost == 3000
    undo_last(conn, SHOP)
    assert _stock(conn, "Coke 2L") == 5
    assert conn.execute("SELECT COUNT(*) c FROM products WHERE shop_id = ? AND name = ?",
                        (SHOP, "Oros 2L")).fetchone()["c"] == 0


# --- reads, reminders, compliance ------------------------------------------


def test_draft_reminder(conn):
    _seeded(conn)
    out = draft_reminder(conn, SHOP, "Sipho")
    assert "Sipho" in out and "R73.00" in out and "Forward this yourself" in out
    assert draft_reminder(conn, SHOP, "Lerato") == "Lerato owes nothing, no reminder needed."


def test_compliance_guide(conn):
    _seeded(conn)
    assert "spazashopfund.co.za" in compliance_guide(conn, SHOP)
    assert "FREE" in compliance_guide(conn, SHOP, "fund")
    assert "R100,000" in compliance_guide(conn, SHOP, "fund")
    assert "agent" in compliance_guide(conn, SHOP, "scam")


def test_demo_reset(conn):
    _seeded(conn)
    add_credit(conn, SHOP, "Stranger", [{"product": "Coke 2L", "qty": 1}])
    confirm_pending(conn, SHOP)
    assert demo_reset(conn, SHOP).startswith("🎉 Fresh start.")
    assert _count(conn, "customers") == 6
    assert get_balance(conn, SHOP, "Sipho") == "💳 *Sipho* owes R73.00."


def test_delete_my_data_erases_shop(conn):
    from app.tools import delete_my_data

    _seeded(conn)
    add_credit(conn, SHOP, "Sipho", [{"product": "Coke 2L", "qty": 1}])
    confirm_pending(conn, SHOP)
    assert delete_my_data(conn, SHOP).startswith("✅ *Done.*")
    for table in ("customers", "products", "credit_entries", "payments", "sales",
                  "stock_moves", "expiry_items", "pending_actions", "actions"):
        assert _count(conn, table) == 0, table
    shop = conn.execute("SELECT * FROM shops WHERE shop_id = ?", (SHOP,)).fetchone()
    assert shop is not None and shop["consent_given_at"] is None
    assert get_balance(conn, SHOP, "Sipho") == "No one named Sipho is on the book."


def test_tool_registry_complete():
    names = {t["function"]["name"] for t in TOOL_DEFS}
    assert names == set(TOOL_FUNCS)
    for expected in ["add_credit", "record_payment", "get_balance", "list_debtors",
                     "draft_reminder", "record_sale", "add_stock", "set_price", "low_stock",
                     "list_stock", "best_sellers",
                     "restock_list", "cash_up", "check_expiry_photo", "read_slip_photo",
                     "compliance_guide", "undo_last", "record_expense", "record_drawing",
                     "record_count", "record_supplier_payment", "cash_balances",
                     "sales_summary", "profit_month", "list_receipts",
                     "month_end_report"]:
        assert expected in names


def test_confirm_cancel_without_pending(conn):
    assert confirm_pending(conn, SHOP) == "There is nothing waiting for Yes."
    assert cancel_pending(conn, SHOP) == "There is nothing waiting."


def test_fuzzy_product_matching(conn):
    from app.tools import _find_products

    _seeded(conn)
    prod, _ = _find_products(conn, SHOP, "Sasko bread")
    assert prod["name"] == "Sasko White Bread 700g"
    prod, _ = _find_products(conn, SHOP, "2l coke")
    assert prod["name"] == "Coke 2L"
    prod, _ = _find_products(conn, SHOP, "a bottle of coke 2l please")
    assert prod["name"] == "Coke 2L"
    prod, _ = _find_products(conn, SHOP, "two litre Coke")
    assert prod["name"] == "Coke 2L"
    prod, _ = _find_products(conn, SHOP, "2 liter stoney")
    assert prod["name"] == "Stoney 2L"
    assert _find_products(conn, SHOP, "Unicorn Flakes")[0] is None
