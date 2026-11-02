"""Post-task WhatsApp overview card: render, trigger, fallback, no-spam."""

import json
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
    assert buttons == ["Yes", "No"]
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
