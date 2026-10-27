"""Cash book: accounts, postings, float/count, expenses, drawings."""

from test_format import assert_format

from app.agent import handle_text
from app.tools import (
    add_stock,
    cash_balances,
    confirm_pending,
    profit_month,
    record_count,
    record_drawing,
    record_expense,
    record_payment,
    record_sale,
    record_supplier_payment,
    sales_summary,
    set_float,
    supplier_balance,
    undo_last,
)
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID


def test_float_sets_opening_once(conn):
    seed_demo(SHOP, conn)
    out = assert_format(set_float(conn, SHOP, {"cash": 50000, "petty": 20000}))
    assert "• Till R500.00" in out and "• Petty cash R200.00" in out
    again = set_float(conn, SHOP, {"cash": 99900})
    assert "already" in again
    out = assert_format(cash_balances(conn, SHOP))
    assert out.startswith("💰 *Cash*") and "• Till R500.00" in out


def test_float_command_parses_accounts(conn):
    seed_demo(SHOP, conn)
    msg, _ = handle_text(conn, SHOP, "float R1200 petty R300")
    assert "• Till R1200.00" in msg and "• Petty cash R300.00" in msg


def test_cash_sale_posts_payment_posts_credit_waits(conn):
    seed_demo(SHOP, conn)
    set_float(conn, SHOP, {"cash": 100000})
    record_sale(conn, SHOP, "Coke 2L", 2)
    confirm_pending(conn, SHOP)
    record_sale(conn, SHOP, "Coke 2L", 1, buyer="Sipho")
    confirm_pending(conn, SHOP)
    record_payment(conn, SHOP, "Sipho", 3600)
    confirm_pending(conn, SHOP)
    out = cash_balances(conn, SHOP)
    assert "• Till R1108.00" in out  # 1000 + 72 + 36, credit sale excluded
    undo_last(conn, SHOP)
    assert "• Till R1072.00" in cash_balances(conn, SHOP)


def test_expense_with_category_and_undo(conn):
    seed_demo(SHOP, conn)
    set_float(conn, SHOP, {"petty": 30000})
    msg = assert_format(record_expense(conn, SHOP, 20000, "transport",
                                       account="petty", note="taxi"))
    assert "📝 *Save expense?*" in msg and "Transport R200.00" in msg
    saved = assert_format(confirm_pending(conn, SHOP))
    assert saved.startswith("✅ *Expense saved*")
    assert "• Petty cash R100.00" in cash_balances(conn, SHOP)
    undo_last(conn, SHOP)
    assert "• Petty cash R300.00" in cash_balances(conn, SHOP)


def test_drawing_posts_and_undoes(conn):
    seed_demo(SHOP, conn)
    set_float(conn, SHOP, {"cash": 100000})
    record_drawing(conn, SHOP, 50000, note="groceries")
    confirm_pending(conn, SHOP)
    assert "• Till R500.00" in cash_balances(conn, SHOP)
    undo_last(conn, SHOP)
    assert "• Till R1000.00" in cash_balances(conn, SHOP)


def test_count_shows_over_and_short(conn):
    seed_demo(SHOP, conn)
    set_float(conn, SHOP, {"cash": 100000})
    msg = assert_format(record_count(conn, SHOP, 101000))
    assert "R10.00 over" in msg
    confirm_pending(conn, SHOP)
    assert "• Till R1010.00" in cash_balances(conn, SHOP)
    record_count(conn, SHOP, 99000)
    saved = confirm_pending(conn, SHOP)
    assert "R20.00 short" in saved
    assert "• Till R990.00" in cash_balances(conn, SHOP)


def test_purchase_posts_to_account_or_supplier(conn):
    seed_demo(SHOP, conn)
    set_float(conn, SHOP, {"cash": 100000})
    add_stock(conn, SHOP, "Coke 2L", 6, cost_cents=3000)
    confirm_pending(conn, SHOP)
    assert "• Till R820.00" in cash_balances(conn, SHOP)  # 6 x R30
    add_stock(conn, SHOP, "Stoney 2L", 6, cost_cents=3000,
              wholesaler="Jumbo", account="supplier")
    confirm_pending(conn, SHOP)
    assert supplier_balance(conn, SHOP, "Jumbo") == 18000
    assert "• Till R820.00" in cash_balances(conn, SHOP)  # untouched
    record_supplier_payment(conn, SHOP, "Jumbo", 18000)
    confirm_pending(conn, SHOP)
    assert supplier_balance(conn, SHOP, "Jumbo") == 0
    assert "• Till R640.00" in cash_balances(conn, SHOP)


def test_sales_summary_today_and_week(conn):
    seed_demo(SHOP, conn)
    record_sale(conn, SHOP, "Coke 2L", 2)
    confirm_pending(conn, SHOP)
    record_sale(conn, SHOP, "Stoney 2L", 1, buyer="Sipho")
    confirm_pending(conn, SHOP)
    out = assert_format(sales_summary(conn, SHOP, "today"))
    assert "📊 *Sales today*" in out
    assert "• Cash R72.00 (1 sale)" in out
    assert "• Credit R33.00" in out
    assert "• Total R105.00" in out
    out = sales_summary(conn, SHOP, "week")
    assert "📊 *Sales last 7 days*" in out and "• Total R105.00" in out
    assert "📊 *Sales today*" in sales_summary(conn, SHOP, "year")


def test_profit_month_math(conn):
    seed_demo(SHOP, conn)
    add_stock(conn, SHOP, "Coke 2L", 10, cost_cents=3000)
    confirm_pending(conn, SHOP)
    record_sale(conn, SHOP, "Coke 2L", 2)
    confirm_pending(conn, SHOP)
    record_expense(conn, SHOP, 1200, "transport")
    confirm_pending(conn, SHOP)
    out = assert_format(profit_month(conn, SHOP))
    assert "📈 *Profit for" in out
    assert "• Sales R72.00" in out
    assert "• Cost of sales R60.00" in out
    assert "• Gross R12.00 (17%)" in out
    assert "• Expenses R12.00" in out
    assert "• Net R0.00" in out
