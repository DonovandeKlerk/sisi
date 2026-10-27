"""Daily plan: content, schedule, opt-out, evening checklist, setup step."""

from datetime import datetime

import pytest

from app.agent import handle_text
from app.reminders import SAST, save_reminder
from app.schedule import daily_plan, tick
from app.setup import setup_step
from app.tools import build_order, ensure_shop, set_float


SHOP = "27710000023"
DAY = "2026-10-27"  # a Tuesday


def _at(hm: str) -> datetime:
    return datetime.strptime(f"{DAY} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=SAST)


@pytest.fixture()
def shop(conn):
    from app.setup import skip_setup
    ensure_shop(conn, SHOP, "Plan Spaza")
    conn.execute("UPDATE shops SET channel = 'vonage' WHERE shop_id = ?",
                 (SHOP,))
    conn.commit()
    skip_setup(conn, SHOP)
    return SHOP


@pytest.fixture()
def full(conn, shop):
    save_reminder(conn, SHOP, datetime(2026, 10, 27, 7, 0, tzinfo=SAST),
                  "take order to shop", {"kind": "order"})
    conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, cost_cents, stock_qty,"
        " reorder_level, wholesaler) VALUES (?, 'Bread', 2000, 1500, 2, 10, 'Jumbo')",
        (SHOP,))
    bread = conn.execute("SELECT id FROM products WHERE shop_id = ?",
                         (SHOP,)).fetchone()["id"]
    conn.execute("INSERT INTO expiry_items (shop_id, product_name, qty, expiry_date,"
                 " action_id) VALUES (?, 'Milk 1L', 2, '2026-10-28', 'pl-e0')",
                 (SHOP,))
    sipho = conn.execute("INSERT INTO customers (shop_id, name) VALUES (?, 'Sipho')",
                         (SHOP,)).lastrowid
    conn.execute("INSERT INTO credit_entries (shop_id, customer_id, amount_cents,"
                 " items_text, action_id, created_at) VALUES (?, ?, 4000, 'Bread', ?, ?)",
                 (SHOP, sipho, "pl-ce-0", "2026-10-10T10:00:00.000Z"))
    build_order(conn, SHOP)
    set_float(conn, SHOP, {"cash": 50000})
    conn.execute("INSERT INTO sales (shop_id, product_id, product_name, qty,"
                 " total_cents, action_id, created_at)"
                 " VALUES (?, ?, 'Bread', 5, 10000, 'pl-s0', '2026-10-26T10:00:00.000Z')",
                 (SHOP, bread))
    conn.commit()
    return SHOP


def test_plan_has_every_section(conn, full):
    plan = daily_plan(conn, SHOP, DAY)
    assert plan.startswith("☀️ *Today's plan")
    assert "⏰ *Due today*" in plan and "07:00 — take order to shop" in plan
    assert "⚠️ *Low stock to buy*" in plan and "Bread — 2 left" in plan
    assert "⏰ *Expiring*" in plan and "Milk 1L" in plan
    assert "💳 *Chase first*" in plan and "Sipho — R40.00 (17 days)" in plan
    assert "💰 *Cash vs order*" in plan and "R500.00 in hand" in plan
    assert "📊 *Yesterday*" in plan and "Sales R100.00" in plan
    assert "profit ≈ R25.00" in plan
    assert "💡 *Tip*" in plan and "Sipho" in plan
    assert "Reply 'daily plan off' to stop these." in plan
    plan2 = daily_plan(conn, SHOP, DAY)
    assert "daily plan off" not in plan2  # opt-out mentioned once


def test_plan_skips_empty_sections(conn, shop):
    plan = daily_plan(conn, SHOP, DAY)
    assert plan.startswith("☀️ *Today's plan")
    for section in ("Due today", "Low stock", "Expiring", "Chase first",
                    "Cash vs order", "Yesterday"):
        assert section not in plan
    assert "💡 *Tip*" in plan


def test_tick_fires_plan_once_in_window(conn, shop):
    assert tick(conn, _at("05:59")) == []
    jobs = tick(conn, _at("06:05"))
    assert [j["key"] for j in jobs] == ["plan-2026-10-27"]
    assert jobs[0]["text"].startswith("☀️ *Today's plan")
    assert tick(conn, _at("06:20")) == []
    # stale morning plan skipped at 19:05 (the evening nudge still fires)
    assert all(not j["key"].startswith("plan-")
               for j in tick(conn, _at("19:05")))


def test_plan_off_and_custom_time(conn, shop):
    msg, _ = handle_text(conn, SHOP, "daily plan off")
    assert "Daily plan off" in msg
    assert tick(conn, _at("06:05")) == []
    msg, _ = handle_text(conn, SHOP, "daily plan at 5:30")
    assert "Every morning at 05:30" in msg
    jobs = tick(conn, _at("05:35"))
    assert [j["key"] for j in jobs] == ["plan-2026-10-27"]
    msg, _ = handle_text(conn, SHOP, "daily plan")
    assert msg.startswith("☀️ *Today's plan")


def test_nudge_doubles_as_checklist(conn, full):
    conn.execute("UPDATE cash_moves SET created_at = '2026-10-26T10:00:00.000Z'"
                 " WHERE shop_id = ?", (SHOP,))  # a quiet mocked today
    conn.commit()
    jobs = tick(conn, _at("19:05"))
    nudge = [j for j in jobs if j["key"] == "nudge-2026-10-27"][0]["text"]
    assert "7pm books" in nudge
    assert "✅ *Today's checklist*" in nudge
    assert "☐ 07:00 — take order to shop" in nudge
    assert "☐ Buy 1 low item" in nudge
    assert "☐ Chase 1 debtor" in nudge


def test_nudge_checklist_all_clear(conn, shop):
    jobs = tick(conn, _at("19:05"))
    nudge = [j for j in jobs if j["key"] == "nudge-2026-10-27"][0]["text"]
    assert "✅ All clear." in nudge


def test_setup_step_seven(conn):
    shop = "27710000024"
    answers = ["hi", "Plan Shop", "skip", "yes", "skip", "100 cash", "english"]
    for a in answers:
        msg, _ = handle_text(conn, shop, a)
    assert setup_step(conn, shop) == 7
    assert "Step 7 of 7" in msg
    assert "Daily plan every morning at 06:00? Reply Yes, a time, or No" in msg
    msg, _ = handle_text(conn, shop, "5:30")
    assert msg.startswith("✅ *You're all set")
    row = conn.execute("SELECT value FROM flags WHERE shop_id = ? AND key = ?",
                       (shop, "plan_time")).fetchone()
    assert row["value"] == "05:30"


def test_setup_step_seven_no(conn):
    shop = "27710000025"
    answers = ["hi", "Noplan Shop", "skip", "yes", "skip", "100 cash", "english",
               "no"]
    for a in answers:
        msg, _ = handle_text(conn, shop, a)
    assert msg.startswith("✅ *You're all set")
    row = conn.execute("SELECT value FROM flags WHERE shop_id = ? AND key = ?",
                       (shop, "plan_off")).fetchone()
    assert row is not None
