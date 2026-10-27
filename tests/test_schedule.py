"""Scheduler: 19:00 nudge, Sunday snapshot, expiry warns (clock mocked)."""

from datetime import datetime

import pytest

from app import reports, schedule
from app.agent import handle_text
from app.db import today_str
from app.schedule import SAST, tick
from app.tools import confirm_pending, ensure_shop, record_count


SHOP = "27710000013"


def _at(day: str, hm: str) -> datetime:
    return datetime.strptime(f"{day} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=SAST)


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(reports, "LOGOS_DIR", tmp_path / "logos")
    monkeypatch.setenv("SISI_PUBLIC_URL", "https://x.example")


@pytest.fixture()
def shop(conn):
    from app.setup import skip_setup
    ensure_shop(conn, SHOP, "Nudge Spaza")
    conn.execute("UPDATE shops SET channel = 'vonage' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    skip_setup(conn, SHOP)
    return SHOP


def test_nudge_fires_once_when_quiet(conn, shop):
    jobs = tick(conn, _at("2026-10-27", "19:05"))  # a Tuesday
    assert len(jobs) == 1
    assert jobs[0]["key"] == "nudge-2026-10-27"
    assert "7pm books" in jobs[0]["text"]
    assert tick(conn, _at("2026-10-27", "19:20")) == []


def test_nudge_silent_before_time_and_when_busy(conn, shop):
    assert tick(conn, _at("2026-10-27", "18:59")) == []
    conn.execute(
        "INSERT INTO sales (shop_id, product_name, qty, total_cents, action_id,"
        " created_at) VALUES (?, 'Bread', 1, 2000, 'n-1', '2026-10-27T10:00:00.000Z')",
        (SHOP,))
    conn.commit()
    assert tick(conn, _at("2026-10-27", "19:05")) == []


def test_nudge_off_and_custom_time(conn, shop):
    msg, _ = handle_text(conn, SHOP, "nudge off")
    assert "off" in msg
    assert tick(conn, _at("2026-10-27", "19:05")) == []
    msg, _ = handle_text(conn, SHOP, "nudge at 20:00")
    assert "20:00" in msg
    msg, _ = handle_text(conn, SHOP, "nudge on")
    assert "on" in msg
    assert tick(conn, _at("2026-10-27", "19:05")) == []
    jobs = tick(conn, _at("2026-10-27", "20:05"))
    assert [j["key"] for j in jobs] == ["nudge-2026-10-27"]


def test_sunday_snapshot(conn, shop):
    jobs = tick(conn, _at("2026-11-01", "19:05"))  # a Sunday
    keys = [j["key"] for j in jobs]
    assert "snap-2026-11-01" in keys
    snap = next(j for j in jobs if j["key"].startswith("snap-"))
    assert len(snap["media"]) == 1
    assert snap["media"][0]["url"].startswith("https://x.example/media/")
    monday = [j for j in tick(conn, _at("2026-11-02", "19:05"))
              if j["key"].startswith("snap-")]
    assert monday == []


def test_day_summary_after_count(conn, shop, monkeypatch):
    monkeypatch.setattr("app.db.today_str", lambda: "2026-10-27")
    monkeypatch.setattr("app.schedule._day_summary",
                        lambda c, s, d: "L1\nL2\nL3")
    conn.execute("INSERT INTO flags (shop_id, key, value)"
                 " VALUES (?, 'want-books-2026-10-27', '1')", (SHOP,))
    conn.commit()
    record_count(conn, SHOP, 50000)
    out = confirm_pending(conn, SHOP)
    assert out.endswith("\n\nL1\nL2\nL3")
    assert confirm_pending(conn, SHOP) == "There is nothing waiting for Yes."


def test_expiry_warning(conn, shop):
    conn.execute(
        "INSERT INTO expiry_items (shop_id, product_name, qty, expiry_date)"
        " VALUES (?, 'Fresh Milk 1L', 4, '2026-10-29')", (SHOP,))
    conn.commit()
    jobs = tick(conn, _at("2026-10-27", "19:05"))
    warns = [j for j in jobs if j["key"].startswith("exp-")]
    assert len(warns) == 1
    assert "expires in 2 days" in warns[0]["text"]
    assert "front" in warns[0]["text"]
    assert [j for j in tick(conn, _at("2026-10-27", "19:20"))
            if j["key"].startswith("exp-")] == []


def test_expiry_input_parses(conn, shop):
    from app.tools import parse_expiry_date, set_expiry
    assert parse_expiry_date("2026-11-03") == "2026-11-03"
    assert parse_expiry_date("someday") is None
    msg, _ = handle_text(conn, SHOP, "bread expires Friday")
    assert "Expiry noted" in msg
    row = conn.execute("SELECT * FROM expiry_items WHERE shop_id = ?",
                       (SHOP,)).fetchone()
    assert row["product_name"] == "bread" or "Bread" in row["product_name"]
    assert set_expiry(conn, SHOP, "milk", "someday").startswith("When does it expire?")


def test_price_change_chains_update(conn):
    from app.tools import confirm_pending, ensure_shop, get_pending, propose_receipt
    ensure_shop(conn, SHOP)
    conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, cost_cents, stock_qty)"
        " VALUES (?, 'Bread', 1800, 1300, 10)", (SHOP,))
    pid = conn.execute("SELECT id FROM products WHERE shop_id = ?",
                       (SHOP,)).fetchone()["id"]
    propose_receipt(conn, SHOP, {
        "supplier": "Jumbo", "date": "2026-10-27", "receipt_no": "R1",
        "lines": [{"line_no": 1, "text": "BREAD x5", "item": "Bread",
                 "packs": None, "pack_size": None, "qty_units": 5,
                 "unit_cost_cents": 1500, "line_total_cents": 7500}],
        "vat_cents": None, "total_cents": 7500, "payment": "cash"})
    out = confirm_pending(conn, SHOP)
    assert "cost went up" in out and "Jumbo" in out and "margin is now" in out
    pend = get_pending(conn, SHOP)
    assert pend is not None and pend["action_type"] == "set_price"
    out2 = confirm_pending(conn, SHOP)
    assert "R" in out2
    price = conn.execute("SELECT price_cents FROM products WHERE id = ?",
                         (pid,)).fetchone()["price_cents"]
    assert price > 1800
