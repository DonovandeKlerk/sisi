"""Shared pytest fixtures: isolated temp DB per test."""

import sqlite3

import pytest

from app.db import init_db


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
