"""Media Dict: bytes shared across containers, volume fallback, miss log."""

import sys
import types

import pytest

from app import reports
from app.tools import ensure_shop

SHOP = "27710000045"


def _fake_dict_modal(monkeypatch, store, fail_put=False):
    mod = types.ModuleType("modal")

    class Dict:
        @classmethod
        def from_name(cls, name, create_if_missing=False):
            assert name == "sisi-media"
            box = types.SimpleNamespace()

            def _put(k, v):
                if fail_put:
                    raise RuntimeError("dict down")
                store[k] = v

            box.put = _put
            box.get = lambda k, d=None: store.get(k, d)
            return box

    mod.Dict = Dict
    monkeypatch.setitem(sys.modules, "modal", mod)
    monkeypatch.setenv("SISI_MEDIA_DICT", "sisi-media")


@pytest.fixture()
def card_file(tmp_path):
    p = tmp_path / "card.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\nCARD")
    return p


def test_dict_serves_after_file_deleted(conn, card_file, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    store: dict = {}
    _fake_dict_modal(monkeypatch, store)
    ensure_shop(conn, SHOP)
    token = reports.store_card(conn, SHOP, "profile", str(card_file))
    assert store[token]["png"] == b"\x89PNG\r\n\x1a\nCARD"
    card_file.unlink()  # serving container never sees the file
    client = TestClient(fastapi_app)
    r = client.get(f"/media/{token}.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == b"\x89PNG\r\n\x1a\nCARD"
    assert 'filename="card.png"' in r.headers["content-disposition"]


def test_pdf_shape_serves_from_dict(conn, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    store: dict = {}
    _fake_dict_modal(monkeypatch, store)
    ensure_shop(conn, SHOP)
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(b"%PDF-1.4 STMT")
    token = reports.store_card(conn, SHOP, "stmt-x-pdf", str(pdf))
    assert store[token]["pdf"] == b"%PDF-1.4 STMT"
    pdf.unlink()
    client = TestClient(fastapi_app)
    r = client.get(f"/media/{token}.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == b"%PDF-1.4 STMT"


def test_dict_miss_falls_back_to_volume(conn, card_file, monkeypatch):
    from fastapi.testclient import TestClient

    from app.db import connect as file_connect
    from app.db import init_db
    from app.main import app as fastapi_app

    store: dict = {}
    _fake_dict_modal(monkeypatch, store)
    db_path = card_file.parent / "fb.db"
    monkeypatch.setenv("SISI_DB", str(db_path))
    fc = file_connect()
    init_db(fc)
    ensure_shop(fc, SHOP)
    token = reports.store_card(fc, SHOP, "profile", str(card_file))
    fc.close()
    del store[token]  # legacy pre-Dict row: Dict miss, volume fallback
    client = TestClient(fastapi_app)
    r = client.get(f"/media/{token}.png", params={"f": "png"})
    assert r.status_code == 200
    assert r.content == b"\x89PNG\r\n\x1a\nCARD"


def test_put_failure_blocks_link(conn, card_file, monkeypatch):
    _fake_dict_modal(monkeypatch, {}, fail_put=True)
    ensure_shop(conn, SHOP)
    with pytest.raises(RuntimeError):
        reports.store_card(conn, SHOP, "profile", str(card_file))
    assert conn.execute("SELECT COUNT(*) c FROM reports").fetchone()["c"] == 0


def test_miss_404_logs_token(caplog, monkeypatch):
    import logging

    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    _fake_dict_modal(monkeypatch, {})
    monkeypatch.setenv("SISI_DB", ":memory:")
    caplog.set_level(logging.WARNING, logger="sisi")
    client = TestClient(fastapi_app)
    assert client.get("/media/deadbeef01.png").status_code == 404
    assert "media miss token=deadbeef01" in caplog.text


def test_save_report_files_puts_both_slots(conn, tmp_path, monkeypatch):
    store: dict = {}
    _fake_dict_modal(monkeypatch, store)
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(reports, "LOGOS_DIR", tmp_path / "logos")
    ensure_shop(conn, SHOP, "Dict Spaza")
    files = reports.save_report_files(conn, SHOP, "2026-10")
    entry = store[files["token"]]
    assert entry["png"][:8] == b"\x89PNG\r\n\x1a\n"
    assert entry["pdf"][:5] == b"%PDF-"
