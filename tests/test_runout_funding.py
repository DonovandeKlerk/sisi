"""Stock run-out forecasting and the credit-readiness funding pack."""

from pathlib import Path

import pytest

from app import reports
from app.agent import MEDIA_OUTBOX, handle_text, take_media
from app.tools import (
    _runout_rows,
    _sales_velocity,
    ensure_shop,
    restock_list,
    stock_forecast,
)


SHOP = "27710000201"


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(reports, "LOGOS_DIR", tmp_path / "logos")
    monkeypatch.setenv("SISI_PUBLIC_URL", "https://x.example")
    MEDIA_OUTBOX.clear()


@pytest.fixture()
def shop(conn):
    from app.setup import skip_setup
    ensure_shop(conn, SHOP, "Runout Spaza")
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    skip_setup(conn, SHOP)
    return SHOP


def _add_product(conn, name, stock, reorder=6, wholesaler="Devland"):
    conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, cost_cents,"
        " stock_qty, reorder_level, wholesaler) VALUES (?, ?, 2000, 1500,"
        " ?, ?, ?)", (SHOP, name, stock, reorder, wholesaler))
    conn.commit()
    return conn.execute("SELECT id FROM products WHERE shop_id = ? AND name = ?",
                        (SHOP, name)).fetchone()["id"]


def _sell(conn, product_id, name, qty, day):
    conn.execute(
        "INSERT INTO sales (shop_id, product_id, product_name, qty, total_cents,"
        " action_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (SHOP, product_id, name, qty, qty * 2000, f"ro-{product_id}-{day}-{qty}",
         f"{day}T10:00:00.000Z"))
    conn.commit()


DAY = "2026-10-27"


def test_velocity_from_recent_sales(conn, shop):
    bid = _add_product(conn, "Bread", 10)
    for day in ("2026-10-23", "2026-10-24", "2026-10-25", "2026-10-26"):
        _sell(conn, bid, "Bread", 2, day)
    assert _sales_velocity(conn, SHOP, bid, today=DAY) == pytest.approx(8 / 14)
    assert _sales_velocity(conn, SHOP, 999999, today=DAY) == 0.0


def test_runout_rows_soonest_first_and_skips(conn, shop):
    low = _add_product(conn, "Bread", 2)
    high = _add_product(conn, "Milk", 40)
    nosales = _add_product(conn, "Sugar", 1)
    for day in ("2026-10-24", "2026-10-25", "2026-10-26"):
        _sell(conn, low, "Bread", 2, day)
        _sell(conn, high, "Milk", 2, day)
    rows = _runout_rows(conn, SHOP, today=DAY)
    assert [r["name"] for r in rows] == ["Bread"]
    assert rows[0]["days_left"] == 4
    assert rows[0]["when"].startswith("Sat 31 Oct")
    assert all(r["name"] != "Sugar" for r in rows)  # no velocity, no signal


def test_runout_skips_zero_stock(conn, shop):
    bid = _add_product(conn, "Bread", 0)
    _sell(conn, bid, "Bread", 5, "2026-10-26")
    assert _runout_rows(conn, SHOP, today=DAY) == []


def test_nudge_lists_soonest_runouts(conn, shop):
    from app.schedule import _nudge_text
    bid = _add_product(conn, "Bread", 2)
    for day in ("2026-10-24", "2026-10-25", "2026-10-26"):
        _sell(conn, bid, "Bread", 2, day)
    text = _nudge_text(conn, SHOP, DAY)
    assert "Bread runs out" in text


def test_restock_sized_by_velocity(conn, shop):
    bid = _add_product(conn, "Bread", 2)
    for n in range(21, 28):  # 2/day for 7 days -> velocity exactly 1.0
        _sell(conn, bid, "Bread", 2, f"2026-10-{n:02d}")
    out = restock_list(conn, SHOP)
    # velocity cover-14 need is 14-2=12, above the reorder*2 fallback of 10
    assert "order 12" in out


def test_snapshot_tip_flags_runout(conn, shop):
    from app.schedule import _snapshot_data
    bid = _add_product(conn, "Bread", 2)
    for day in ("2026-10-24", "2026-10-25", "2026-10-26"):
        _sell(conn, bid, "Bread", 2, day)
    data = _snapshot_data(conn, SHOP, DAY)
    assert "runs out" in data["tip"]


def test_restock_falls_back_without_sales(conn, shop):
    _add_product(conn, "Bread", 2)
    out = restock_list(conn, SHOP)
    # no sales: reorder_level*2 - stock = 12 - 2 = 10
    assert "order 10" in out


def test_forecast_fast_path(conn, shop):
    msg, _ = handle_text(conn, SHOP, "stock forecast")
    assert msg.startswith("✅ No run-outs")
    msg, _ = handle_text(conn, SHOP, "what runs out")
    assert msg.startswith("✅ No run-outs")
    bid = _add_product(conn, "Bread", 2)
    for day in ("2026-10-24", "2026-10-25", "2026-10-26"):
        _sell(conn, bid, "Bread", 2, day)
    msg, _ = handle_text(conn, SHOP, "stock forecast")
    assert msg.startswith("🔮 *Stock forecast*")
    assert "Bread" in msg


def _seed_quarter(conn):
    bid = _add_product(conn, "Bread", 10)
    for month, day in (("2026-08", "2026-08-15"), ("2026-09", "2026-09-15"),
                       ("2026-10", "2026-10-15")):
        _sell(conn, bid, "Bread", 5, day)
    sid = conn.execute(
        "INSERT INTO customers (shop_id, name) VALUES (?, 'Sipho')",
        (SHOP,)).lastrowid
    conn.execute(
        "INSERT INTO credit_entries (shop_id, customer_id, amount_cents,"
        " items_text, action_id, created_at) VALUES (?, ?, 4000, 'Bread',"
        " 'ce1', '2026-09-10T10:00:00.000Z')", (SHOP, sid))
    conn.commit()


def _pdf_bytes(conn):
    items = take_media(SHOP)
    url = next(i["url"] for i in items if i["kind"] == "file")
    token = url.split("/media/")[1].split("?")[0]
    row = reports.get_report_by_token(conn, token)
    return Path(reports.row_file_for(row, "pdf")).read_bytes()


def test_funding_pack_pdf_for_seeded_shop(conn, shop):
    _seed_quarter(conn)
    text, media = reports.build_funding_pack(conn, SHOP, "https://x.example")
    assert "funding pack ready" in text
    assert "spazashopfund.co.za" in text
    assert "FREE" in text
    assert media and media[0]["filename"] == "sisi-funding-pack.pdf"
    from app.agent import queue_media
    queue_media(SHOP, media)
    pdf = _pdf_bytes(conn)
    assert len(pdf) > 1000
    assert b"Funding Readiness Pack" in pdf
    assert b"spazashopfund.co.za" in pdf


def test_funding_pack_fast_path_phrases(conn, shop):
    _seed_quarter(conn)
    from app.agent import MEDIA_OUTBOX
    for phrase in ("funding pack", "credit pack", "spaza fund pack",
                   "fund pack", "readiness pack"):
        MEDIA_OUTBOX.clear()
        msg, _ = handle_text(conn, SHOP, phrase)
        assert "funding pack ready" in msg, phrase
        assert take_media(SHOP), phrase


def test_funding_pack_empty_shop_graceful(conn, shop):
    text, media = reports.build_funding_pack(conn, SHOP, "https://x.example")
    assert "No books yet" in text
    assert media == []


def test_funding_pack_pdf_follows_owner_language(conn, shop):
    _seed_quarter(conn)
    conn.execute("UPDATE shops SET language = 'st' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    text, media = reports.build_funding_pack(conn, SHOP, "https://x.example")
    assert "spazashopfund.co.za" in text
    from app.agent import queue_media
    queue_media(SHOP, media)
    pdf = _pdf_bytes(conn)
    assert "Boitokisetso".encode() in pdf


def test_funding_pack_staff_blocked(conn, shop):
    from app.agent import _STAFF_NAME
    from app.setup import skip_setup
    from app.tools import ensure_shop, funding_pack
    ensure_shop(conn, SHOP, "Runout Spaza")
    skip_setup(conn, SHOP)
    handle_text(conn, SHOP, "add staff 27820000099 Thandi")
    try:
        msg, _ = handle_text(conn, "27820000099", "funding pack")
        assert msg.startswith("⛔")
        _STAFF_NAME.set("Thandi")
        assert funding_pack(conn, SHOP).startswith("⛔")
    finally:
        _STAFF_NAME.set(None)
