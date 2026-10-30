"""Shared pytest fixtures: isolated temp DB per test."""

import sqlite3

import pytest

from app.db import init_db


@pytest.fixture(autouse=True)
def _clean_global_state():
    from app.agent import LAST_PHOTO, LIST_OUTBOX, MEDIA_OUTBOX, _STAFF_NAME
    from app.i18n import CURRENT_LANG
    from app.whatsapp import reset_fake
    MEDIA_OUTBOX.clear()
    LIST_OUTBOX.clear()
    LAST_PHOTO.clear()
    reset_fake()
    CURRENT_LANG.set("en")
    _STAFF_NAME.set(None)
    yield
    MEDIA_OUTBOX.clear()
    LIST_OUTBOX.clear()
    LAST_PHOTO.clear()
    reset_fake()
    CURRENT_LANG.set("en")
    _STAFF_NAME.set(None)


@pytest.fixture(autouse=True)
def _no_turso(monkeypatch):
    """Keep the suite hermetic: default connect() must never reach Turso,
    even on a dev machine with credentials exported."""
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("SISI_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("SISI_TODAY", raising=False)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_db(c)
    yield c
    c.close()
