"""Month-end management accounts: figures, balance, files, resend, offer, media."""

from datetime import date

import pytest

from app import reports
from app.agent import MEDIA_OUTBOX, _maybe_first_offer, handle_text, take_media
from app.db import connect, init_db
from app.tools import ensure_shop
from app.vonage import VonageSender
from app.whatsapp import CloudApiSender


SHOP = "27710000009"


def _prev_month() -> str:
    today = date.today()
    if today.month == 1:
        return f"{today.year - 1}-12"
    return f"{today.year}-{today.month - 1:02d}"


@pytest.fixture()
def books(conn, tmp_path, monkeypatch):
    """Controlled one-month books inside the shared in-memory DB."""
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.delenv("SISI_PUBLIC_URL", raising=False)
    MEDIA_OUTBOX.clear()
    m = _prev_month()
    ts = f"{m}-15T10:00:00.000Z"
    ensure_shop(conn, SHOP, "Sisi Spaza")
    conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, cost_cents, stock_qty,"
        " reorder_level, wholesaler, category)"
        " VALUES (?, 'Bread', 2000, 1500, 10, 20, 'Jumbo', 'Bread'),"
        " (?, 'Milk 1L', 2500, 1800, 5, 4, 'Jumbo', 'Dairy')",
        (SHOP, SHOP))
    bread = conn.execute("SELECT id FROM products WHERE shop_id = ? AND name = 'Bread'",
                         (SHOP,)).fetchone()["id"]
    milk = conn.execute("SELECT id FROM products WHERE shop_id = ? AND name = 'Milk 1L'",
                        (SHOP,)).fetchone()["id"]
    sipho = conn.execute("INSERT INTO customers (shop_id, name, created_at)"
                         " VALUES (?, 'Sipho', ?)", (SHOP, ts)).lastrowid
    conn.execute(
        "INSERT INTO sales (shop_id, product_id, product_name, qty, total_cents,"
        " customer_id, action_id, created_at) VALUES"
        " (?, ?, 'Bread', 2, 4000, NULL, 't-sale-1', ?),"
        " (?, ?, 'Milk 1L', 1, 2500, ?, 't-sale-2', ?)",
        (SHOP, bread, ts, SHOP, milk, sipho, ts))
    conn.execute(
        "INSERT INTO credit_entries (shop_id, customer_id, amount_cents, action_id,"
        " created_at) VALUES (?, ?, 2500, 't-ce-1', ?)", (SHOP, sipho, ts))
    conn.execute(
        "INSERT INTO payments (shop_id, customer_id, amount_cents, action_id, created_at)"
        " VALUES (?, ?, 1000, 't-pay-1', ?)", (SHOP, sipho, ts))
    for i, (acct, amt, kind) in enumerate([
            ("cash", 50000, "float"), ("cash", 4000, "sale"),
            ("cash", 1000, "credit_payment"), ("cash", -3000, "purchase"),
            ("petty", -500, "expense"), ("cash", -1000, "drawing")]):
        conn.execute(
            "INSERT INTO cash_moves (shop_id, account, amount_cents, kind, action_id,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (SHOP, acct, amt, kind, f"t-cm-{i}", ts))
    conn.execute(
        "INSERT INTO expenses (shop_id, category, amount_cents, account, action_id,"
        " created_at) VALUES (?, 'electricity', 500, 'petty', 't-ex-1', ?)",
        (SHOP, ts))
    conn.execute(
        "INSERT INTO supplier_moves (shop_id, supplier, amount_cents, action_id,"
        " created_at) VALUES (?, 'Jumbo', 2000, 't-sm-1', ?)", (SHOP, ts))
    conn.commit()
    return m


def test_pl_figures(conn, books):
    d = reports.month_data(conn, SHOP, books)
    assert (d["sales"], d["cash_sales"], d["credit_sales"]) == (6500, 4000, 2500)
    assert d["cogs"] == 2 * 1500 + 1 * 1800
    assert d["gross"] == 6500 - 4800
    assert d["exp_total"] == 500
    assert d["net"] == 1700 - 500
    assert d["purchases"] == 3000
    assert d["closing_value"] == 10 * 1500 + 5 * 1800
    assert d["opening_value"] == 24000 - 3000 + 4800


def test_balance_sheet_balances(conn, books):
    d = reports.month_data(conn, SHOP, books)
    assert d["debt_total"] == 1500
    assert d["cred_total"] == 2000
    assert d["closing_cash"] == 50000 + 4000 + 1000 - 3000 - 500 - 1000
    assert d["assets"] == d["closing_value"] + d["closing_cash"] + d["debt_total"]
    assert d["assets"] - d["cred_total"] - d["equity"] == 0


def test_cash_flow_closes(conn, books):
    d = reports.month_data(conn, SHOP, books)
    expect = (d["opening_cash"] + d["cash_sales"] + d["collected"] + d["count_adj"]
              - d["paid_cash"] - d["exp_cash"] - d["draws"])
    assert expect == d["closing_cash"]


def test_insights_and_reorder(conn, books):
    d = reports.month_data(conn, SHOP, books)
    assert d["best"][0]["name"] == "Bread"
    assert any("Bread" in i for i in d["insights"])
    assert any("Sipho" in i for i in d["insights"])
    assert [r["name"] for r in d["reorder"]] == ["Bread"]
    assert d["reorder"][0]["wholesaler"] == "Jumbo"


def test_pdf_and_png_generated(conn, books):
    files = reports.save_report_files(conn, SHOP, books)
    pdf = open(files["pdf"], "rb").read(5)
    png = open(files["png"], "rb").read(8)
    assert pdf == b"%PDF-"
    assert png == b"\x89PNG\r\n\x1a\n"
    from PIL import Image
    assert Image.open(files["png"]).size == (1080, 1350)
    row = reports.get_report(conn, SHOP, books)
    assert row["token"] == files["token"]
    assert len(files["token"]) == 32


def test_month_short_format(conn, books):
    out = reports.month_short(conn, SHOP, books)
    assert out.startswith("📊 *Month-end:")
    for line in out.split("\n"):
        assert len(line) <= 120, line
    assert "R65.00" in out and "R12.00" in out


def test_build_queues_media_with_base_url(conn, books, monkeypatch):
    monkeypatch.setenv("SISI_PUBLIC_URL", "https://x.example")
    text, media = reports.build_month_end(conn, SHOP, books, "https://x.example")
    assert "📎 Card + full PDF below." in text
    assert [m["kind"] for m in media] == ["image", "file"]
    assert all(m["url"].startswith("https://x.example/media/") for m in media)
    assert media[1]["filename"] == f"sisi-month-end-{books}.pdf"


def test_resend_reuses_stored_files(conn, books):
    first, _ = reports.build_month_end(conn, SHOP, books)
    assert "🔁" not in first
    token = reports.get_report(conn, SHOP, books)["token"]
    second, _ = reports.build_month_end(conn, SHOP, books)
    assert "🔁 Re-sending your saved files." in second
    assert reports.get_report(conn, SHOP, books)["token"] == token


def test_report_month_command(conn, books):
    msg, buttons = handle_text(conn, SHOP, "report september")
    assert buttons is None
    # September may differ from the fixture month; both paths must answer.
    assert msg.startswith("📊 *Month-end:")


def test_first_of_month_offer_once(conn, books, monkeypatch):
    monkeypatch.setenv("SISI_TODAY", f"{date.today().isoformat()[:7]}-01")
    first = _maybe_first_offer(conn, SHOP, "hi")
    assert "It's the 1st" in first
    assert _maybe_first_offer(conn, SHOP, "hi") == "hi"


def test_seeded_shop_books_balance(conn, tmp_path, monkeypatch):
    from seed.seed_random import DEMO_NUMBER, seed_random_shop

    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    seed_random_shop(DEMO_NUMBER, conn, seed=7)
    month = date.today().isoformat()[:7]
    n_moves = conn.execute("SELECT COUNT(*) c FROM cash_moves WHERE shop_id = ?",
                           (DEMO_NUMBER,)).fetchone()["c"]
    n_rcpt = conn.execute("SELECT COUNT(*) c FROM receipts WHERE shop_id = ?",
                          (DEMO_NUMBER,)).fetchone()["c"]
    n_exp = conn.execute("SELECT COUNT(*) c FROM expenses WHERE shop_id = ?",
                         (DEMO_NUMBER,)).fetchone()["c"]
    assert n_moves > 50 and n_rcpt >= 3 and n_exp >= 5
    d = reports.month_data(conn, DEMO_NUMBER, month)
    assert d["sales"] > 0
    assert d["assets"] - d["cred_total"] - d["equity"] == 0
    text, _ = reports.build_month_end(conn, DEMO_NUMBER, month)
    assert text.startswith("📊 *Month-end:")


def test_first_offer_skips_empty_month(conn, monkeypatch):
    monkeypatch.setenv("SISI_TODAY", f"{date.today().isoformat()[:7]}-01")
    ensure_shop(conn, SHOP)
    assert _maybe_first_offer(conn, SHOP, "hi") == "hi"


class _FakePost:
    def __init__(self):
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append({"url": url, **kw})

        class Resp:
            status_code = 200
            text = "ok"

            def raise_for_status(self):
                pass

            def json(self):
                return {"message_uuid": "m-1"}

        return Resp()


def test_vonage_media_shapes(monkeypatch):
    monkeypatch.setenv("VONAGE_API_KEY", "k")
    monkeypatch.setenv("VONAGE_API_SECRET", "s")
    from app import vonage
    fake = _FakePost()
    monkeypatch.setattr(vonage.httpx, "post", fake)
    VonageSender().send_image("27710000001", "https://x/u.png", "card")
    VonageSender().send_file("27710000001", "https://x/u.pdf", "r.pdf", "pdf")
    assert [c["json"]["message_type"] for c in fake.calls] == ["image", "file"]
    assert fake.calls[0]["json"]["image"] == {"url": "https://x/u.png", "caption": "card"}
    assert fake.calls[1]["json"]["file"]["url"] == "https://x/u.pdf"


def test_cloud_media_shapes(monkeypatch):
    monkeypatch.setenv("WHATSAPP_TOKEN", "t")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "pid")
    from app import whatsapp
    fake = _FakePost()
    monkeypatch.setattr(whatsapp.httpx, "post", fake)
    CloudApiSender().send_image("2771", "https://x/u.png", "card")
    CloudApiSender().send_file("2771", "https://x/u.pdf", "r.pdf", "pdf")
    assert [c["json"]["type"] for c in fake.calls] == ["image", "document"]
    assert fake.calls[0]["json"]["image"]["link"] == "https://x/u.png"


def test_media_route_serves_token(conn, books, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    files = reports.save_report_files(conn, SHOP, books)
    row = reports.get_report(conn, SHOP, books)
    db_path = tmp_path / "route.db"
    file_conn = connect(db_path)
    init_db(file_conn)
    file_conn.execute("INSERT INTO shops (shop_id, name) VALUES (?, 'S')", (SHOP,))
    file_conn.execute(
        "INSERT INTO reports (shop_id, month, pdf_path, png_path, token)"
        " VALUES (?, ?, ?, ?, ?)",
        (SHOP, books, files["pdf"], files["png"], row["token"]))
    file_conn.commit()
    file_conn.close()
    monkeypatch.setenv("SISI_DB", str(db_path))
    client = TestClient(app)
    r = client.get(f"/media/{row['token']}", params={"f": "png"})
    assert r.status_code == 200
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    r = client.get(f"/media/{row['token']}", params={"f": "pdf"})
    assert r.status_code == 200
    assert r.content[:5] == b"%PDF-"
    assert client.get("/media/deadbeef", params={"f": "png"}).status_code == 404
