"""Short-DB-outage behavior: cron skips the tick, webhooks say try-again-soon.

Live bug (Nov 2026): every_minute crashed in connect() on a Turso 502
("HTTP status 502: connect to upstream failed"). One failed connect must
never crash a job or leave a user hanging.
"""

import logging
import warnings

import pytest
from turso_serverless.dbapi import OperationalError as TursoOperationalError

from app.i18n import STRINGS
from app.main import process_message
from app.whatsapp import FakeSender, IncomingMessage, get_sender

SHOP = "27710000099"
OUTAGE = "HTTP status 502: connect to upstream failed"


@pytest.fixture(autouse=True)
def _fake_sender(monkeypatch):
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    monkeypatch.delenv("WHATSAPP_TOKEN", raising=False)
    assert isinstance(get_sender(), FakeSender)


def _outage(*a, **k):
    raise TursoOperationalError(OUTAGE)


def _tick_local():
    """Run the real every_minute handler locally (Modal .local())."""
    import modal_app

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # local-run volume notice
        modal_app.every_minute.local()


def test_every_minute_skips_tick_on_502(monkeypatch, tmp_path, caplog):
    import modal_app

    monkeypatch.setattr(modal_app, "_use_volume", lambda: None)
    monkeypatch.setenv("SISI_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr("app.db.connect", _outage)
    with caplog.at_level(logging.WARNING, logger="sisi"):
        _tick_local()  # must not raise
    skips = [r for r in caplog.records if "skipping tick" in r.getMessage()]
    assert len(skips) == 1  # a single warning, then the next tick retries
    assert "502" in skips[0].getMessage()


def test_every_minute_reraises_permanent_errors(monkeypatch, tmp_path):
    import modal_app

    monkeypatch.setattr(modal_app, "_use_volume", lambda: None)
    monkeypatch.setenv("SISI_DB", str(tmp_path / "t.db"))

    def boom(*a, **k):
        raise RuntimeError("no such table: shops")

    monkeypatch.setattr("app.db.connect", boom)
    with pytest.raises(RuntimeError, match="no such table"):
        _tick_local()


def test_every_minute_fires_due_reminder(monkeypatch, tmp_path):
    import modal_app
    from app.db import connect, init_db
    from app.tools import ensure_shop

    monkeypatch.setattr(modal_app, "_use_volume", lambda: None)
    monkeypatch.setenv("SISI_DB", str(tmp_path / "t.db"))
    db = connect()
    try:
        init_db(db)
        ensure_shop(db, SHOP)
        db.execute("UPDATE shops SET channel = 'meta' WHERE shop_id = ?",
                   (SHOP,))
        db.execute("INSERT INTO reminders (shop_id, due_at, summary)"
                   " VALUES (?, ?, ?)",
                   (SHOP, "2000-01-01T00:00:00+02:00", "pay supplier"))
        # Fresh inbound keeps meta's 24h window open so the tick sends.
        db.execute("INSERT INTO messages_seen (message_id, shop_id)"
                   " VALUES ('seed-inbound', ?)", (SHOP,))
        db.commit()
    finally:
        db.close()
    _tick_local()
    db = connect()
    try:
        status = db.execute("SELECT status FROM reminders WHERE shop_id = ?",
                            (SHOP,)).fetchone()["status"]
    finally:
        db.close()
    assert status == "submitted"  # queued by send_due, sent by drain_all
    sent = get_sender().pop_shop(SHOP)
    assert sent and "pay supplier" in sent[0]["body"]


def test_webhook_connect_outage_sends_zulu_try_again(monkeypatch, tmp_path):
    import app.main as main

    monkeypatch.setenv("SISI_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(main, "connect", _outage)
    process_message(IncomingMessage("mid-502a", SHOP, "text", "sawubona", ""))
    sent = get_sender().pop_shop(SHOP)
    assert len(sent) == 1
    assert sent[0]["body"] == STRINGS["zu"]["try_again_soon"]


def test_webhook_midhandler_outage_sends_english_try_again(
        monkeypatch, tmp_path):
    import app.main as main

    monkeypatch.setenv("SISI_DB", str(tmp_path / "t.db"))

    def boom(sender, msg):
        raise TursoOperationalError(OUTAGE)

    monkeypatch.setattr(main, "_handle_message", boom)
    process_message(IncomingMessage("mid-502b", SHOP, "text", "hi", ""))
    sent = get_sender().pop_shop(SHOP)
    assert len(sent) == 1
    assert sent[0]["body"] == STRINGS["en"]["try_again_soon"]


def test_webhook_permanent_db_error_sends_nothing(monkeypatch, tmp_path):
    import app.main as main

    monkeypatch.setenv("SISI_DB", str(tmp_path / "t.db"))

    def boom(*a, **k):
        raise RuntimeError("no such table: shops")

    monkeypatch.setattr(main, "connect", boom)
    process_message(IncomingMessage("mid-perm", SHOP, "text", "hi", ""))
    assert get_sender().pop_shop(SHOP) == []
