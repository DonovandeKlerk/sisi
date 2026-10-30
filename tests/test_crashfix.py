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


def test_save_logo_creates_missing_dirs(conn):
    import io
    from PIL import Image
    from app.tools import ensure_shop, save_shop_logo
    ensure_shop(conn, SHOP)
    img = Image.new("RGB", (60, 40), "white")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    out = save_shop_logo(conn, SHOP, buf.getvalue())
    assert "Logo saved" in out
    assert ensure_shop(conn, SHOP)["logo_path"].endswith("-tile.png")


def test_finish_card_creates_missing_dirs(conn):
    from app.setup import _finish
    from app.tools import ensure_shop
    ensure_shop(conn, SHOP, "Dir Spaza")
    out = _finish(conn, SHOP)
    assert "You're all set" in out


def test_process_message_never_goes_silent(conn, monkeypatch, tmp_path):
    import app.main as main
    monkeypatch.setenv("SISI_DB", str(tmp_path / "t.db"))

    def boom(conn, shop_id, text, sent_at=None):
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
                        lambda c, s, b, cap="", sent_at=None: (None, "photo reply", None))
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
                        lambda c, s, b, suf, sent_at=None: ("t", "voice reply", None))
    process_message(IncomingMessage("mid-3", SHOP, "media:audio", None, "",
                                    media_id="m1"))
    sent = get_sender().pop_shop(SHOP)
    assert sent[0]["body"].startswith("⏳")
    assert sent[1]["body"].startswith("voice reply")


def test_slow_text_gets_status_line_first(conn, monkeypatch, tmp_path):
    import app.main as main
    monkeypatch.setenv("SISI_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(main, "handle_text",
                        lambda c, s, t, sent_at=None: ("month-end reply", None))
    process_message(IncomingMessage("mid-4", SHOP, "text", "month-end report", ""))
    sent = get_sender().pop_shop(SHOP)
    assert sent[0]["body"].startswith("⏳")
    assert sent[1]["body"].startswith("month-end reply")


def test_fast_text_has_no_status_line(conn, monkeypatch, tmp_path):
    import app.main as main
    monkeypatch.setenv("SISI_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(main, "handle_text", lambda c, s, t, sent_at=None: ("hi there", None))
    process_message(IncomingMessage("mid-5", SHOP, "text", "hi", ""))
    sent = get_sender().pop_shop(SHOP)
    assert len(sent) == 1 and sent[0]["body"].startswith("hi there")


# --- crash diagnostics: record + owner-only retrieval ------------------------


def test_crash_records_error_report(conn, monkeypatch, tmp_path):
    import app.main as main
    from app.db import connect
    monkeypatch.setenv("SISI_DB", str(tmp_path / "t.db"))

    def boom(conn, shop_id, text, sent_at=None):
        raise RuntimeError("kaboom-book")

    monkeypatch.setattr(main, "handle_text", boom)
    process_message(IncomingMessage("mid-9", SHOP, "text", "hi", ""))
    sent = get_sender().pop_shop(SHOP)
    assert sent[0]["body"].startswith("😅 Something went wrong")
    db = connect()
    try:
        row = db.execute(
            "SELECT context, detail FROM error_reports WHERE shop_id = ?"
            " ORDER BY id DESC LIMIT 1", (SHOP,)).fetchone()
    finally:
        db.close()
    assert row is not None
    assert "mid-9" in row["context"]
    assert "RuntimeError: kaboom-book" in row["detail"]


def test_debug_last_error_command(conn):
    from app.agent import handle_text
    from app.setup import skip_setup
    from app.tools import ensure_shop, record_error
    skip_setup(conn, SHOP)
    ensure_shop(conn, SHOP)
    record_error(conn, SHOP, "whapi/media:image/mid-9",
                 'Traceback (most recent call last):\n  File "app/x.py",'
                 ' line 1, in f\nRuntimeError: kaboom-book')
    msg, buttons = handle_text(conn, SHOP, "debug last error")
    assert buttons is None
    assert "kaboom-book" in msg and "mid-9" in msg
    assert all(len(line) <= 120 for line in msg.splitlines())


def test_debug_no_errors_recorded(conn):
    from app.agent import handle_text
    from app.setup import skip_setup
    from app.tools import ensure_shop
    skip_setup(conn, SHOP)
    ensure_shop(conn, SHOP)
    msg, _ = handle_text(conn, SHOP, "debug last error")
    assert "No errors recorded" in msg


def test_debug_staff_blocked(conn):
    from app.agent import handle_text
    from app.setup import skip_setup
    from app.tools import ensure_shop
    skip_setup(conn, SHOP)
    ensure_shop(conn, SHOP)
    handle_text(conn, SHOP, "add staff 27821234567 Thandi")
    msg, _ = handle_text(conn, "27821234567", "debug last error")
    assert msg.startswith("⛔")


def test_error_reports_capped(conn):
    from app.tools import ERROR_REPORT_KEEP, ensure_shop, record_error
    ensure_shop(conn, SHOP)
    for i in range(ERROR_REPORT_KEEP + 5):
        record_error(conn, SHOP, f"ctx-{i}", "boom")
    assert conn.execute("SELECT COUNT(*) n FROM error_reports WHERE shop_id = ?",
                        (SHOP,)).fetchone()["n"] == ERROR_REPORT_KEEP
