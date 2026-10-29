"""Purchase receipts: preview, weighted cost, expenses, undo, list."""

from test_format import assert_format

from app.agent import handle_text
from app.db import today_str
from app.tools import (
    cash_balances,
    confirm_pending,
    list_receipts,
    propose_receipt,
    set_float,
    supplier_balance,
)
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID


def _receipt(**kw):
    base = {"supplier": "Jumbo", "date": "2026-10-20", "receipt_no": "R-2210",
            "lines": [
                {"line_no": 1, "text": "TASTIC 2KG x6 330.00", "item": "Tastic 2kg",
                 "packs": None, "pack_size": None, "qty_units": 6,
                 "unit_cost_cents": 5500, "line_total_cents": 33000},
                {"line_no": 2, "text": "COKE 2L CASE x2", "item": "Coke 2L",
                 "packs": 2, "pack_size": None, "qty_units": None,
                 "unit_cost_cents": None, "line_total_cents": None},
                {"line_no": 3, "text": "ELECTRICITY PREPAID 120.00",
                 "item": "Electricity prepaid", "packs": None, "pack_size": None,
                 "qty_units": None, "unit_cost_cents": None,
                 "line_total_cents": 12000},
            ],
            "vat_cents": 7793, "total_cents": 45000, "payment": "cash"}
    base.update(kw)
    return base


def test_receipt_preview(conn):
    seed_demo(SHOP, conn)
    conn.execute("UPDATE products SET cost_cents = 5000 WHERE shop_id = ? AND name LIKE 'Tastic%'",
                 (SHOP,))
    out = assert_format(propose_receipt(conn, SHOP, _receipt()))
    assert out.startswith("🧾 *Purchase: Jumbo*")
    assert "• +6 Tastic Rice 2kg @ R53.75 (was R50.00) → stock 2 → 8" in out
    assert "• Electricity R120.00 → expenses" in out
    assert "• line 2 'COKE 2L CASE x2' — pack size unknown" in out
    assert "• Total R450.00 ✅" in out
    assert "• Incl. VAT R77.93" in out
    assert "• Paid from: Till" in out


def test_receipt_confirm_applies_all(conn):
    seed_demo(SHOP, conn)
    conn.execute("UPDATE products SET cost_cents = 5000 WHERE shop_id = ? AND name LIKE 'Tastic%'",
                 (SHOP,))
    set_float(conn, SHOP, {"cash": 100000})
    propose_receipt(conn, SHOP, _receipt())
    saved = assert_format(confirm_pending(conn, SHOP))
    assert saved.startswith("✅ *Receipt saved*")
    row = conn.execute("SELECT stock_qty, cost_cents FROM products"
                       " WHERE shop_id = ? AND name LIKE 'Tastic%'", (SHOP,)).fetchone()
    assert (row["stock_qty"], row["cost_cents"]) == (8, 5375.0)
    assert "• Till R550.00" in cash_balances(conn, SHOP)
    n = conn.execute("SELECT COUNT(*) c FROM expenses WHERE shop_id = ? AND reversed = 0",
                     (SHOP,)).fetchone()["c"]
    assert n == 1


def test_receipt_supplier_credit_and_undo(conn):
    from app.tools import undo_last

    seed_demo(SHOP, conn)
    set_float(conn, SHOP, {"cash": 100000})
    propose_receipt(conn, SHOP, _receipt(payment="account"))
    confirm_pending(conn, SHOP)
    assert supplier_balance(conn, SHOP, "Jumbo") == 45000
    assert "• Till R1000.00" in cash_balances(conn, SHOP)
    assert undo_last(conn, SHOP) == "Undone: removed the Jumbo receipt."
    assert supplier_balance(conn, SHOP, "Jumbo") == 0
    assert "• Till R1000.00" in cash_balances(conn, SHOP)


def test_receipt_undo_created_product_cleans_moves(conn):
    from app.tools import undo_last

    seed_demo(SHOP, conn)
    set_float(conn, SHOP, {"cash": 100000})
    propose_receipt(conn, SHOP, _receipt(
        receipt_no="R-9NEW", total_cents=6000,
        lines=[{"line_no": 1, "text": "MYSTERY BEANS 500G x3 60.00",
                "item": "Mystery Beans 500g", "packs": None,
                "pack_size": None, "qty_units": 3,
                "unit_cost_cents": 2000, "line_total_cents": 6000}]))
    confirm_pending(conn, SHOP)
    row = conn.execute("SELECT id, stock_qty FROM products"
                       " WHERE shop_id = ? AND name = ?",
                       (SHOP, "Mystery Beans 500g")).fetchone()
    assert row is not None and row["stock_qty"] == 3
    undo_last(conn, SHOP)  # must not raise FOREIGN KEY
    left = conn.execute("SELECT COUNT(*) c FROM products"
                        " WHERE shop_id = ? AND name = ?",
                        (SHOP, "Mystery Beans 500g")).fetchone()["c"]
    assert left == 0
    moves = conn.execute("SELECT COUNT(*) c FROM stock_moves"
                         " WHERE shop_id = ? AND product_id = ?",
                         (SHOP, row["id"])).fetchone()["c"]
    assert moves == 0


def test_receipt_duplicate_skipped(conn):
    seed_demo(SHOP, conn)
    propose_receipt(conn, SHOP, _receipt())
    confirm_pending(conn, SHOP)
    out = propose_receipt(conn, SHOP, _receipt())
    assert out == "🧾 Already filed Jumbo R-2210 (R450.00). Send a different slip."


def test_receipt_correct_line_and_account(conn):
    seed_demo(SHOP, conn)
    set_float(conn, SHOP, {"cash": 100000, "petty": 50000})
    propose_receipt(conn, SHOP, _receipt())
    msg, _ = handle_text(conn, SHOP, "line 2 is 12 coke")
    assert "• +12 Coke 2L @ ? → stock 5 → 17" in msg
    msg, _ = handle_text(conn, SHOP, "from petty cash")
    assert "• Paid from: Petty cash" in msg
    confirm_pending(conn, SHOP)
    assert "• Petty cash R50.00" in cash_balances(conn, SHOP)


def test_list_receipts_month(conn):
    seed_demo(SHOP, conn)
    propose_receipt(conn, SHOP, _receipt())
    confirm_pending(conn, SHOP)
    propose_receipt(conn, SHOP, _receipt(receipt_no="R-2211", total_cents=33000,
                                         lines=[_receipt()["lines"][0]]))
    confirm_pending(conn, SHOP)
    out = assert_format(list_receipts(conn, SHOP, today_str()[:7]))
    assert "🧾 *Receipts:" in out
    assert "R-2210" in out and "R-2211" in out
    assert "Total R780.00" in out
