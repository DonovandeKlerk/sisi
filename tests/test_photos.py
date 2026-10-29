"""Photo router: classify shop photos, skip duplicates, fixture images."""

import hashlib
from pathlib import Path

import app.agent as agent
import app.vision as vision
from app.agent import handle_photo
from app.vision import VisionError, read_shop_photo
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID
FIXTURES = Path(__file__).parent / "fixtures"


def _vresp(content):
    from types import SimpleNamespace

    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                           usage=None)


def _doc(doc_type, **kw):
    base = {"doc_type": doc_type,
            "expiry": {"stamp_count": 0, "items": []},
            "book": {"date": "", "page_total_cents": None, "crossed_out": 0, "lines": []},
            "receipt": {"supplier": "", "date": "", "receipt_no": "", "lines": [],
                        "vat_cents": None, "total_cents": None, "payment": ""}}
    base.update(kw)
    return base


def test_read_shop_photo_parses_doc_types(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "k")
    doc = _doc("receipt", receipt={"supplier": "Jumbo", "date": "2026-10-20",
                                   "receipt_no": "R1", "lines": [], "vat_cents": 1500,
                                   "total_cents": 11500, "payment": "cash"})
    monkeypatch.setattr(vision, "_complete_vision",
                        lambda m, r: _vresp(__import__("json").dumps(doc)))
    out = read_shop_photo(b"img", SHOP)
    assert out["doc_type"] == "receipt"
    assert out["receipt"]["supplier"] == "Jumbo"
    assert out["receipt"]["total_cents"] == 11500


def test_read_shop_photo_bad_json(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "k")
    monkeypatch.setattr(vision, "_complete_vision", lambda m, r: _vresp("nope"))
    try:
        read_shop_photo(b"img", SHOP)
    except VisionError:
        pass
    else:
        raise AssertionError("expected VisionError")


def _stub_plan(monkeypatch, body):
    import json
    from types import SimpleNamespace

    import app.interpreter as interp

    def fake(messages):
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2))

    monkeypatch.setattr(interp, "propose_plan", fake)


def test_handle_photo_other_helps(conn, monkeypatch):
    seed_demo(SHOP, conn)
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    monkeypatch.setattr(agent, "read_shop_photo", lambda image, shop="": _doc("other"))
    _stub_plan(monkeypatch, {"understood": "unrecognized photo", "tasks": [],
                             "question": "Is this a book page, till slip, shelf or stock photo?",
                             "options": []})
    _, reply, buttons = handle_photo(conn, SHOP, b"photo-1")
    assert buttons is None
    assert "book page" in reply and "till slip" in reply


def test_duplicate_photo_detected_after_save(conn, monkeypatch):
    from app.agent import handle_text

    seed_demo(SHOP, conn)
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    calls = []

    def fake_read(image, shop=""):
        calls.append(1)
        return _doc("expiry", expiry={"stamp_count": 1, "items": [
            {"product": "Milk 1L", "qty": 2, "expiry_date": "2020-01-01"}]})

    monkeypatch.setattr(agent, "read_shop_photo", fake_read)
    _stub_plan(monkeypatch, {"understood": "expiry photo",
                             "tasks": [{"kind": "expiry_photo", "args": {},
                                        "quote": ""}],
                             "question": None, "options": []})
    _, first, buttons = handle_photo(conn, SHOP, b"same-bytes")
    assert buttons == ["Yes", "No"]
    handle_text(conn, SHOP, "Yes")
    _, second, _ = handle_photo(conn, SHOP, b"same-bytes")
    assert "already saved" in second
    assert len(calls) == 1


def test_fixture_images_exist_and_hash():
    book = FIXTURES / "book_page.png"
    slip = FIXTURES / "till_slip.png"
    assert book.exists() and slip.exists()
    assert hashlib.sha256(book.read_bytes()).hexdigest() != \
        hashlib.sha256(slip.read_bytes()).hexdigest()
