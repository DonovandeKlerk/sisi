"""Crash fix + busy indicator regressions."""

import pytest

from app import reports
from app.agent import MEDIA_OUTBOX
from app.main import process_message
from app.whatsapp import FakeSender, IncomingMessage, get_sender


SHOP = "27710000020"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    monkeypatch.delenv("WHATSAPP_TOKEN", raising=False)
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "deep" / "reports")
    monkeypatch.setattr(reports, "LOGOS_DIR", tmp_path / "deep" / "logos")
    MEDIA_OUTBOX.clear()
    assert isinstance(get_sender(), FakeSender)


def test_switch_logo_creates_missing_dirs(conn, monkeypatch):
    import io
    from PIL import Image
    from app.tools import ensure_shop, save_shop_logo, switch_logo
    monkeypatch.setattr("app.logo.remove_bg", lambda b: None)
    ensure_shop(conn, SHOP)
    img = Image.new("RGB", (60, 40), "white")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    save_shop_logo(conn, SHOP, buf.getvalue())
    out = switch_logo(conn, SHOP, "original")
    assert "original" in out


def test_finish_card_creates_missing_dirs(conn):
    from app.setup import _finish
    from app.tools import ensure_shop
    ensure_shop(conn, SHOP, "Dir Spaza")
    out = _finish(conn, SHOP)
    assert "You're all set" in out


def test_process_message_never_goes_silent(conn, monkeypatch, tmp_path):
    import app.main as main
    monkeypatch.setenv("SISI_DB", str(tmp_path / "t.db"))

    def boom(conn, shop_id, text):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(main, "handle_text", boom)
    process_message(IncomingMessage("mid-1", SHOP, "text", "hi", ""))
    sent = get_sender().pop_shop(SHOP)
    assert len(sent) == 1
    assert sent[0]["body"].startswith("😅 Something went wrong")


def test_photo_gets_status_line_first(conn, monkeypatch, tmp_path):
    import app.main as main
    monkeypatch.setenv("SISI_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(main, "_download_msg_media", lambda m, l: b"img")
    monkeypatch.setattr(main, "handle_photo",
                        lambda c, s, b, cap="": (None, "photo reply", None))
    process_message(IncomingMessage("mid-2", SHOP, "media:image", None, "",
                                    media_id="m1"))
    sent = get_sender().pop_shop(SHOP)
    assert sent[0]["body"].startswith("⏳")
    assert sent[1]["body"].startswith("photo reply")


def test_voice_gets_status_line_first(conn, monkeypatch, tmp_path):
    import app.main as main
    monkeypatch.setenv("SISI_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(main, "_download_msg_media", lambda m, l: b"aud")
    monkeypatch.setattr(main, "handle_voice",
                        lambda c, s, b, suf: ("t", "voice reply", None))
    process_message(IncomingMessage("mid-3", SHOP, "media:audio", None, "",
                                    media_id="m1"))
    sent = get_sender().pop_shop(SHOP)
    assert sent[0]["body"].startswith("⏳")
    assert sent[1]["body"].startswith("voice reply")


def test_slow_text_gets_status_line_first(conn, monkeypatch, tmp_path):
    import app.main as main
    monkeypatch.setenv("SISI_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(main, "handle_text",
                        lambda c, s, t: ("month-end reply", None))
    process_message(IncomingMessage("mid-4", SHOP, "text", "month-end report", ""))
    sent = get_sender().pop_shop(SHOP)
    assert sent[0]["body"].startswith("⏳")
    assert sent[1]["body"].startswith("month-end reply")


def test_fast_text_has_no_status_line(conn, monkeypatch, tmp_path):
    import app.main as main
    monkeypatch.setenv("SISI_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(main, "handle_text", lambda c, s, t: ("hi there", None))
    process_message(IncomingMessage("mid-5", SHOP, "text", "hi", ""))
    sent = get_sender().pop_shop(SHOP)
    assert len(sent) == 1 and sent[0]["body"].startswith("hi there")
