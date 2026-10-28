"""Shared pytest fixtures: isolated temp DB per test."""

import sqlite3

import pytest

from app.db import init_db


@pytest.fixture(autouse=True)
def _clean_global_state():
    from app.agent import LAST_PHOTO, MEDIA_OUTBOX, _STAFF_NAME
    from app.i18n import CURRENT_LANG
    from app.whatsapp import reset_fake
    MEDIA_OUTBOX.clear()
    LAST_PHOTO.clear()
    reset_fake()
    CURRENT_LANG.set("en")
    _STAFF_NAME.set(None)
    yield
    MEDIA_OUTBOX.clear()
    LAST_PHOTO.clear()
    reset_fake()
    CURRENT_LANG.set("en")
    _STAFF_NAME.set(None)


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
