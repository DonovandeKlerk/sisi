"""Post-task WhatsApp overview card: render, trigger, fallback, no-spam."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app import overview, reports
from app.agent import LIST_OUTBOX, MENU_ROWS, handle_text, take_lists, take_media
from app.i18n import CURRENT_LANG, doc_lang
from app.tools import ensure_shop
from seed.seed_demo import seed_demo

SHOP = "27710000077"


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(reports, "LOGOS_DIR", tmp_path / "logos")
    monkeypatch.setenv("SISI_PUBLIC_URL", "https://x.example")


@pytest.fixture()
def shop(conn):
    seed_demo(SHOP, conn)
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    return SHOP


def _stub_plan(monkeypatch, tasks, understood="overview test"):
    import app.interpreter as interp

    def fake(messages):
        body = {"understood": understood, "tasks": tasks,
                "question": None, "options": []}
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2),
        )

    monkeypatch.setattr(interp, "propose_plan", fake)


def _credit_task():
    return [{"kind": "credit",
             "args": {"customer": "Sipho",
                      "items": [{"product": "Coke 2L", "qty": 1}]},
             "quote": "Sipho took a Coke"}]


# --- rendering -------------------------------------------------------------


def test_render_png_with_and_without_logo(shop, conn):
    brand = reports.shop_brand(conn, SHOP)
    assert not brand["logo"]
    badge = overview.initials_badge_path(SHOP, brand["name"])
    assert badge.endswith(".png")
    with doc_lang("en"):
        data = overview.overview_data(conn, SHOP)
        plain = str(reports._reports_dir() / "plain.png")
        overview.render_overview_png(data, dict(brand, logo=""), plain)
        badged = str(reports._reports_dir() / "badged.png")
        overview.render_overview_png(data, dict(brand, logo=badge), badged)
    for path in (plain, badged):
        img = Image.open(path)
        assert img.size == (1080, 1600)
        assert img.format == "PNG"
    assert len(open(badged, "rb").read()) > 20_000


def test_card_font_scales_for_african_languages():
    assert reports._card_font(44).size == 44
    assert reports._card_font(72, bold=True).size == 72


@pytest.mark.parametrize("lang", ["zu", "xh", "st", "tn"])
def test_render_across_languages(shop, conn, lang):
    with doc_lang(lang):
        data = overview.overview_data(conn, SHOP)
        brand = reports.shop_brand(conn, SHOP)
        path = str(reports._reports_dir() / f"ov-{lang}.png")
        overview.render_overview_png(
            data, dict(brand,
                       logo=overview.initials_badge_path(SHOP, brand["name"])),
            path)
    img = Image.open(path)
    assert img.size == (1080, 1600)
    assert len(open(path, "rb").read()) > 20_000


# --- trigger ----------------------------------------------------------------


def test_confirm_queues_one_media_and_menu(shop, conn, monkeypatch):
    _stub_plan(monkeypatch, _credit_task())
    msg, buttons = handle_text(conn, SHOP, "Sipho took a Coke")
    assert buttons == ["✅ Yes", "❌ No"]
    assert take_media(SHOP) == []  # pending proposal: no card yet
    assert SHOP not in LIST_OUTBOX
    msg, buttons = handle_text(conn, SHOP, "yes")
    assert buttons is None and "Saved" in msg
    media = take_media(SHOP)
    assert len(media) == 1
    assert media[0]["kind"] == "image"
    assert media[0]["url"].startswith("https://x.example/media/")
    assert media[0].get("first") is True
    lists = take_lists(SHOP)
    assert len(lists) == 1
    assert lists[0]["rows"] == MENU_ROWS


def test_read_only_query_sends_no_overview(shop, conn):
    msg, _ = handle_text(conn, SHOP, "who owes me")
    assert "Sipho" in msg or "R" in msg
    assert take_media(SHOP) == []
    assert SHOP not in LIST_OUTBOX
    msg, _ = handle_text(conn, SHOP, "cash up")
    assert take_media(SHOP) == []
    assert SHOP not in LIST_OUTBOX


def test_cancel_sends_no_overview(shop, conn, monkeypatch):
    _stub_plan(monkeypatch, _credit_task())
    handle_text(conn, SHOP, "Sipho took a Coke")
    msg, buttons = handle_text(conn, SHOP, "no")
    assert buttons is None
    assert take_media(SHOP) == []
    assert SHOP not in LIST_OUTBOX


def test_undo_triggers_overview(shop, conn, monkeypatch):
    _stub_plan(monkeypatch, _credit_task())
    handle_text(conn, SHOP, "Sipho took a Coke")
    handle_text(conn, SHOP, "yes")
    take_media(SHOP)
    take_lists(SHOP)
    msg, buttons = handle_text(conn, SHOP, "undo")
    assert buttons is None and "Undone" in msg
    assert len(take_media(SHOP)) == 1
    assert len(take_lists(SHOP)) == 1


def test_direct_float_write_triggers_overview(conn, monkeypatch):
    ensure_shop(conn, SHOP, "Float Shop")
    from app.setup import skip_setup
    skip_setup(conn, SHOP)
    msg, buttons = handle_text(conn, SHOP, "float R500")
    assert buttons is None
    assert len(take_media(SHOP)) == 1
    assert len(take_lists(SHOP)) == 1


# --- failure fallback --------------------------------------------------------


def test_image_failure_falls_back_to_text_and_menu(shop, conn, monkeypatch):
    _stub_plan(monkeypatch, _credit_task())
    handle_text(conn, SHOP, "Sipho took a Coke")

    def boom(*a, **k):
        raise RuntimeError("render down")

    monkeypatch.setattr(overview, "render_overview_png", boom)
    msg, buttons = handle_text(conn, SHOP, "yes")
    assert buttons is None and "Saved" in msg
    assert take_media(SHOP) == []
    assert len(take_lists(SHOP)) == 1
    assert "📊" in msg  # text standing appended to the task reply


def test_gate_never_breaks_the_reply(shop, conn, monkeypatch):
    _stub_plan(monkeypatch, _credit_task())
    handle_text(conn, SHOP, "Sipho took a Coke")
    monkeypatch.setattr(overview, "books_fingerprint",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("fp")))
    msg, buttons = handle_text(conn, SHOP, "yes")
    assert buttons is None and "Saved" in msg


def test_overview_uses_owner_language(shop, conn):
    conn.execute("UPDATE shops SET language = 'zu' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    CURRENT_LANG.set("en")
    caption, media = overview.build_overview_card(conn, SHOP)
    assert len(media) == 1
    assert CURRENT_LANG.get() == "en"  # caller language restored
    assert media[0]["caption"] != ""  # no logo: one-time logo nudge rides along


def test_cash_in_hand_excludes_card(shop, conn, tmp_path):
    """Cash in hand = cash sales + payments; card is shown but not summed in."""
    from app.i18n import doc_lang, t
    from app.overview import (overview_data, render_overview_png,
                              initials_badge_path)

    conn.execute("UPDATE sales SET reversed = 1 WHERE shop_id = ?", (SHOP,))
    conn.execute("UPDATE payments SET reversed = 1 WHERE shop_id = ?", (SHOP,))
    conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, stock_qty)"
        " VALUES (?, 'OvBread', 1800, 10)", (SHOP,))
    pid = conn.execute(
        "SELECT id FROM products WHERE shop_id=? AND name='OvBread'",
        (SHOP,)).fetchone()["id"]
    conn.execute(
        "INSERT INTO sales (shop_id, product_id, product_name, qty, total_cents,"
        " customer_id, pay_method, action_id)"
        " VALUES (?, ?, 'OvBread', 1, 3700, NULL, 'cash', 'ov-cash-1')",
        (SHOP, pid))
    conn.execute(
        "INSERT INTO sales (shop_id, product_id, product_name, qty, total_cents,"
        " customer_id, pay_method, action_id)"
        " VALUES (?, ?, 'OvBread', 1, 3600, NULL, 'card', 'ov-card-1')",
        (SHOP, pid))
    cust = conn.execute(
        "SELECT id FROM customers WHERE shop_id=? LIMIT 1", (SHOP,)).fetchone()
    if cust is None:
        conn.execute(
            "INSERT INTO customers (shop_id, name) VALUES (?, 'Thandi')",
            (SHOP,))
        cust = conn.execute(
            "SELECT id FROM customers WHERE shop_id=? AND name='Thandi'",
            (SHOP,)).fetchone()
    conn.execute(
        "INSERT INTO payments (shop_id, customer_id, amount_cents, action_id)"
        " VALUES (?, ?, 4000, 'ov-pay-1')", (SHOP, cust["id"]))
    conn.commit()

    with doc_lang("en"):
        data = overview_data(conn, SHOP)
        assert data["cash_cents"] == 3700
        assert data["card_cents"] == 3600
        assert data["pay_cents"] == 4000
        assert data["takings_cents"] == 7700  # cash + payments; not card
        assert t("ov_takings") == "Cash in hand"
        brand = {"name": "Test Spaza", "address": "", "phone": "",
                 "logo": initials_badge_path(SHOP, "Test Spaza"),
                 "logo_orig": "", "language": "en"}
        path = str(tmp_path / "cash_hand.png")
        render_overview_png(data, brand, path)
        assert Path(path).stat().st_size > 1000


def test_runout_phrase_plain_words_localized():
    from app.i18n import doc_lang
    from app.overview import _runout_phrase

    with doc_lang("en"):
        assert _runout_phrase(0) == "runs out today"
        assert _runout_phrase(-2) == "runs out today"
        assert _runout_phrase(1) == "runs out tomorrow"
        assert _runout_phrase(3) == "runs out in 3 days"
        assert "d left" not in _runout_phrase(5)
    with doc_lang("zu"):
        assert "namuhla" in _runout_phrase(0)
        assert "kusasa" in _runout_phrase(1)
        assert "3" in _runout_phrase(3)


def test_footer_never_overlaps_content(shop, conn, tmp_path):
    """Crowded card (4 low items + debtor) keeps footer clear of content."""
    from PIL import Image, ImageChops
    from app.i18n import doc_lang
    from app.overview import render_overview_png, initials_badge_path

    data = {
        "day": "2026-11-02",
        "hm": "16:20",
        "cash_cents": 3700,
        "cash_n": 1,
        "card_cents": 3600,
        "card_n": 1,
        "book_cents": 0,
        "pay_cents": 4000,
        "takings_cents": 7700,
        "sales_cents": 7300,
        "debt_total": 19850,
        "debtors_n": 3,
        "top_debtor": {"name": "Jabu", "cents": 12000},
        "low": [
            {"name": "Bread", "days_left": 0},
            {"name": "Milk", "days_left": 1},
            {"name": "Coke", "days_left": 2},
            {"name": "Sugar", "days_left": 3},
        ],
    }
    brand = {"name": "Bongi Spaza", "address": "Amanzimtoti",
             "phone": "082 000 0000",
             "logo": initials_badge_path(SHOP, "Bongi Spaza"),
             "logo_orig": "", "language": "en"}
    path = tmp_path / "crowded.png"
    with doc_lang("en"):
        render_overview_png(data, brand, str(path))
    img = Image.open(path)
    assert img.height >= 1600
    # Footer band (bottom 140px) must differ from a blank cream strip —
    # more useful: crop content just above footer and ensure the debtor
    # line region is NOT inside the footer band by checking that wiping
    # the footer band does not remove mid-card ink... Simpler: the
    # Updated line lives in the footer band; measure non-cream pixels in
    # the last 140px and also in a band 200-340px from bottom — both
    # should have ink, and there should be a cream gap between content
    # and footer (approx y=height-170 to height-120 mostly cream).
    paper = (255, 251, 235)  # SISI paper approx
    footer = img.crop((0, img.height - 140, img.width, img.height))
    gap = img.crop((60, img.height - 170, img.width - 60, img.height - 120))
    content = img.crop((60, img.height - 340, img.width - 60, img.height - 200))

    def ink_ratio(im):
        rgb = im.convert("RGB")
        w, h = rgb.size
        pix = rgb.load()
        total = w * h
        ink = sum(1 for y in range(h) for x in range(w) if pix[x, y] != paper)
        return ink / max(total, 1)

    assert ink_ratio(footer) > 0.001  # Updated / Generated text
    assert ink_ratio(content) > 0.001  # debtor / owed content above
    # Gap between content and footer should be mostly paper (no overlap)
    assert ink_ratio(gap) < 0.02

