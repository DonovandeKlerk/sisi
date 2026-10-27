"""Order drafts: build from low stock + sales rate, edit, send-it format."""

from test_format import assert_format

from app.agent import handle_text
from app.tools import build_order, edit_order_add, edit_order_remove, send_order
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID


def _costs(conn):
    conn.execute("UPDATE products SET cost_cents = 5500 WHERE shop_id = ? AND name LIKE 'Tastic%'",
                 (SHOP,))
    conn.execute("UPDATE products SET cost_cents = 3000 WHERE shop_id = ? AND name = 'Coke 2L'",
                 (SHOP,))
    conn.commit()


def _daily_sales(conn, name, per_day, days=7):
    from datetime import date, timedelta

    from app.db import today_str

    pid = conn.execute("SELECT id FROM products WHERE shop_id = ? AND name = ?",
                       (SHOP, name)).fetchone()["id"]
    price = conn.execute("SELECT price_cents FROM products WHERE id = ?", (pid,)).fetchone()[0]
    today = date.fromisoformat(today_str())
    for back in range(days):
        day = (today - timedelta(days=back)).isoformat()
        conn.execute(
            """INSERT INTO sales (shop_id, product_id, product_name, qty, total_cents,
               action_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (SHOP, pid, name, per_day, price * per_day, f"hist-{name}-{back}",
             f"{day}T10:00:00.000Z"),
        )
    conn.commit()


def test_build_order_groups_with_packs_and_total(conn):
    seed_demo(SHOP, conn)
    _costs(conn)
    out = assert_format(build_order(conn, SHOP))
    assert out.startswith("📋 *Order draft*")
    assert "*Jumbo*" in out and "*Coke Depot*" in out
    assert "Tastic Rice 2kg — 6 units ≈ R330.00" in out
    assert "🛒 *Order total*" in out


def test_build_uses_average_daily_sales(conn):
    seed_demo(SHOP, conn)
    _costs(conn)
    _daily_sales(conn, "Tastic Rice 2kg", 7)
    out = build_order(conn, SHOP)
    assert "Tastic Rice 2kg — 47 units ≈ R2585.00" in out  # 7/day covers a week


def test_build_empty_when_stocked(conn):
    seed_demo(SHOP, conn)
    conn.execute("UPDATE products SET stock_qty = 99 WHERE shop_id = ?", (SHOP,))
    assert build_order(conn, SHOP) == "✅ Nothing to order. Stock is fine."


def test_send_it_formats_forwardable_message(conn):
    seed_demo(SHOP, conn)
    _costs(conn)
    build_order(conn, SHOP)
    assert send_order(conn, SHOP) == "Which wholesaler? Say 'send it Jumbo'."
    out = assert_format(send_order(conn, SHOP, "Jumbo"))
    assert out.startswith("Hello Jumbo! Order from MaZulu's Spaza:")
    assert "Tastic Rice 2kg x 6" in out
    assert send_order(conn, SHOP, "Jumbo") == \
        "That order was already sent. Say 'order stock' to build a new one."
    assert send_order(conn, SHOP, "Coke Depot").startswith("Hello Coke Depot!")


def test_add_and_remove_lines(conn):
    seed_demo(SHOP, conn)
    _costs(conn)
    build_order(conn, SHOP)
    out = assert_format(edit_order_add(conn, SHOP, "Stoney 2L", 12, None))
    assert "Stoney 2L — 12 units" in out
    out = assert_format(edit_order_remove(conn, SHOP, "Tastic Rice"))
    assert "Tastic Rice 2kg" not in out
    assert edit_order_add(conn, SHOP, "Coke 2L", 2, "packs") == \
        "❓ Coke 2L has no pack size. Add singles? e.g. 'add coke 12'."
    assert edit_order_remove(conn, SHOP, "Unicorn") == \
        "❓ Unicorn isn't on the order. Say 'order stock' to see it."


def test_order_commands_through_agent(conn):
    seed_demo(SHOP, conn)
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    _costs(conn)
    msg, _ = handle_text(conn, SHOP, "order stock")
    assert msg.startswith("📋 *Order draft*")
    msg, _ = handle_text(conn, SHOP, "add stoney 12")
    assert "Stoney 2L — 12 units" in msg
    msg, _ = handle_text(conn, SHOP, "remove stoney")
    assert "Stoney 2L" not in msg
    msg, _ = handle_text(conn, SHOP, "send it Jumbo")
    assert msg.startswith("Hello Jumbo!")
