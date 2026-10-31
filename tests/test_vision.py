"""Photo expiry pipeline: vision shape, agent + webhook wiring. Offline."""

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.agent as agent
import app.llm as llm
import app.main as main
import app.vision as vision
from app.main import app
from app.vision import CANT_READ, VisionError, read_expiry_photo
from app.voice import VoiceError
from app.whatsapp import download_media, reset_fake
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID

ffmpeg_available = shutil.which("ffmpeg") is not None


@pytest.fixture()
def photo_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SISI_DB", str(tmp_path / "photo.db"))
    monkeypatch.setenv("APP_SECRET", "test-app-secret")
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    monkeypatch.delenv("WHATSAPP_TOKEN", raising=False)
    monkeypatch.setattr(llm, "TOKEN_LOG", tmp_path / "usage.log")
    reset_fake()
    seed_demo(SHOP)
    import app.db as _db
    _c = _db.connect()
    _c.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?", (SHOP,))
    _c.commit()
    _c.close()
    return tmp_path


def _vresp(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2))


def _tiny_jpg() -> bytes:
    """A real 8x8 jpeg, generated with Pillow (ffmpeg-independent)."""
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


def _expiry_doc(items):
    return {"doc_type": "expiry",
            "expiry": {"stamp_count": len(items), "items": items},
            "book": {}, "receipt": {}}


# --- vision request shape (stubbed API) ------------------------------------


def test_expiry_request_shape_and_parsing(photo_env, monkeypatch):
    seen = {}

    def fake_complete(messages, response_format):
        seen["messages"] = messages
        seen["response_format"] = response_format
        return _vresp(json.dumps({"items": [
            {"product": "Fresh Milk 1L", "qty": 2, "expiry_date": "2026-10-30"},
            {"product": "", "qty": 1, "expiry_date": "2026-10-30"},
            {"product": "Ghost", "qty": 0, "expiry_date": "2026-10-30"},
            {"product": "Bad Date", "qty": 1, "expiry_date": "soon"},
        ]}))

    monkeypatch.setattr(vision, "_complete_vision", fake_complete)
    out = read_expiry_photo(_tiny_jpg(), SHOP)
    assert out == [{"product": "Fresh Milk 1L", "qty": 2, "expiry_date": "2026-10-30"}]

    content = seen["messages"][0]["content"]
    assert content[0]["type"] == "text" and "expiry" in content[0]["text"].lower()
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    rf = seen["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "expiry_findings"
    assert rf["json_schema"]["strict"] is True
    schema = rf["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["stamp_count", "items"]
    assert schema["properties"]["items"]["items"]["additionalProperties"] is False


def test_expiry_bad_json_and_api_error(photo_env, monkeypatch, capsys):
    monkeypatch.setattr(vision, "_complete_vision", lambda m, r: _vresp("not json"))
    with pytest.raises(VisionError, match="couldn't read"):
        read_expiry_photo(_tiny_jpg(), SHOP)

    err = RuntimeError("down")
    err.body = {"error": {"message": "vision boom"}}
    monkeypatch.setattr(vision, "_complete_vision", lambda m, r: (_ for _ in ()).throw(err))
    with pytest.raises(VisionError, match="couldn't read"):
        read_expiry_photo(_tiny_jpg(), SHOP)
    assert "vision boom" in capsys.readouterr().err


def test_expiry_missing_key_and_bad_input(photo_env, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY")
    with pytest.raises(VisionError, match="can't read photos"):
        read_expiry_photo(_tiny_jpg(), SHOP)
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    with pytest.raises(VisionError, match="couldn't read"):
        read_expiry_photo(b"", SHOP)
    with pytest.raises(VisionError, match="couldn't read"):
        read_expiry_photo(b"x" * (vision.MAX_IMAGE_BYTES + 1), SHOP)


# --- image prep ------------------------------------------------------------


@pytest.mark.skipif(not ffmpeg_available, reason="ffmpeg not installed")
def test_prepare_downscales_big_photo():
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=3000x1500:rate=1:duration=1",
         "-frames:v", "1", "-f", "mjpeg", "pipe:1"],
        capture_output=True, timeout=30,
    )
    big = proc.stdout
    assert len(big) > 20000
    small, mime = vision._prepare_image(big)
    assert mime == "image/jpeg" and small[:2] == b"\xff\xd8"
    assert len(small) < len(big)


def test_prepare_without_ffmpeg_returns_original(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    raw = b"raw-bytes"
    assert vision._prepare_image(raw) == (raw, "image/jpeg")


# --- agent + endpoints -----------------------------------------------------


def _stub_plan(monkeypatch, body):
    import json as _json
    from types import SimpleNamespace as _NS

    import app.interpreter as interp

    def fake(messages):
        return _NS(choices=[_NS(message=_NS(content=_json.dumps(body)))],
                  usage=_NS(prompt_tokens=1, completion_tokens=1,
                            total_tokens=2))

    monkeypatch.setattr(interp, "propose_plan", fake)


def _expiry_plan():
    return {"understood": "expiry photo",
            "tasks": [{"kind": "expiry_photo", "args": {}, "quote": ""}],
            "question": None, "options": []}


def test_handle_photo_proposes_preview(photo_env, monkeypatch):
    import app.db as db

    monkeypatch.setenv("SISI_TODAY", "2026-11-01")
    monkeypatch.setattr(agent, "read_shop_photo", lambda *a, **k: _expiry_doc([
        {"product": "Fresh Milk 1L", "qty": 2, "expiry_date": "2026-10-30"}]))
    _stub_plan(monkeypatch, _expiry_plan())
    conn = db.connect(str(photo_env / "photo.db"))
    try:
        findings, reply, buttons = agent.handle_photo(conn, SHOP, b"jpg")
    finally:
        conn.close()
    assert findings["expiry"]["items"][0]["product"] == "Fresh Milk 1L"
    assert "• 2 × Fresh Milk 1L 30 Oct — expired" in reply
    assert buttons == ["Yes", "No"]


def test_handle_photo_nothing_readable(photo_env, monkeypatch):
    import app.db as db

    monkeypatch.setattr(agent, "read_shop_photo", lambda *a, **k: _expiry_doc([]))
    _stub_plan(monkeypatch, _expiry_plan())
    conn = db.connect(str(photo_env / "photo.db"))
    try:
        findings, reply, buttons = agent.handle_photo(conn, SHOP, b"jpg")
    finally:
        conn.close()
    assert findings["doc_type"] == "expiry" and buttons is None
    assert "could not read any dates" in reply

    def boom(*a, **k):
        raise VisionError(CANT_READ)

    monkeypatch.setattr(agent, "read_shop_photo", boom)
    _stub_plan(monkeypatch, {"understood": "unreadable photo", "tasks": [],
                             "question": CANT_READ, "options": []})
    conn = db.connect(str(photo_env / "photo.db"))
    try:
        findings, reply, buttons = agent.handle_photo(conn, SHOP, b"jpg")
    finally:
        conn.close()
    assert (findings, reply, buttons) == (None, CANT_READ, None)


def test_sim_photo_endpoint(photo_env, monkeypatch):
    monkeypatch.setenv("SISI_TODAY", "2026-11-01")
    monkeypatch.setattr(agent, "read_shop_photo", lambda *a, **k: _expiry_doc([
        {"product": "Fresh Milk 1L", "qty": 2, "expiry_date": "2026-10-30"}]))
    _stub_plan(monkeypatch, _expiry_plan())
    client = TestClient(app)
    r = client.post("/_sim/photo", params={"shop": SHOP}, content=b"jpg-bytes",
                    headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 200
    data = r.json()
    assert data["findings"]["expiry"]["items"][0]["qty"] == 2
    assert "expired" in data["reply"]
    assert data["buttons"] == ["Yes", "No"]


def test_sim_photo_404_when_live(photo_env, monkeypatch):
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "pnid")
    monkeypatch.setenv("WHATSAPP_TOKEN", "tok")
    client = TestClient(app)
    assert client.post("/_sim/photo", params={"shop": SHOP}, content=b"x").status_code == 404


def test_webhook_image_message_end_to_end(photo_env, monkeypatch):
    import hashlib
    import hmac

    monkeypatch.setenv("SISI_TODAY", "2026-11-01")
    monkeypatch.setattr(main, "download_media", lambda _mid, _label="voice note": b"jpg")
    monkeypatch.setattr(agent, "read_shop_photo", lambda *a, **k: _expiry_doc([
        {"product": "Fresh Milk 1L", "qty": 2, "expiry_date": "2026-10-30"}]))
    _stub_plan(monkeypatch, _expiry_plan())
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{
        "value": {"messages": [{"id": "wamid.p1", "from": SHOP, "type": "image",
                                "image": {"id": "media-1", "mime_type": "image/jpeg"}}]}}]}]}
    raw = json.dumps(payload).encode()
    sig = hmac.new(b"test-app-secret", raw, hashlib.sha256).hexdigest()
    client = TestClient(app)
    r = client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": f"sha256={sig}"})
    assert r.status_code == 200
    out = client.get("/_sim/outbox", params={"shop": SHOP}).json()["messages"]
    assert len(out) == 2 and out[0]["body"].startswith("⏳")
    assert out[1]["type"] == "buttons"
    assert "• 2 × Fresh Milk 1L 30 Oct — expired" in out[1]["body"]


def test_download_media_photo_label(photo_env):
    with pytest.raises(VoiceError, match="photo"):
        download_media("media-1", "photo")


# --- adversarial vision JSON: normalization never crashes -------------------
# Live bug (30 Oct 2026): a daily-sales photo died with "Something went
# wrong" three times. Malformed model shapes (line_no as list/dict,
# lines as int, wrong-typed sections) escaped read_shop_photo as raw
# TypeError instead of degrading to VisionError.


def test_shop_photo_keeps_card_kind_and_page_date(photo_env, monkeypatch):
    seen = {}

    def fake(messages, response_format):
        seen["prompt"] = messages[0]["content"][0]["text"]
        return _vresp(json.dumps({
            "doc_type": "book_page",
            "book": {"date": "2026-10-30", "page_total_cents": None,
                     "crossed_out": 0,
                     "lines": [
                         {"line_no": 8, "text": "2 milk 56", "item": "milk",
                          "qty": 2, "price_cents": 5600, "kind": "card",
                          "customer": ""},
                         {"line_no": 15, "text": "Supplier: bread R80",
                          "item": "bread", "qty": None, "price_cents": 8000,
                          "kind": "supplier", "customer": ""},
                         {"line_no": 16, "text": "1 milk damaged",
                          "item": "milk", "qty": 1, "price_cents": None,
                          "kind": "wastage", "customer": ""},
                         {"line_no": 9, "text": "???", "item": "",
                          "qty": 1, "price_cents": None,
                          "kind": "teleport", "customer": ""}]}}))

    monkeypatch.setattr(vision, "_complete_vision", fake)
    doc = vision.read_shop_photo(_tiny_jpg(), SHOP)
    assert seen["prompt"].startswith("Today is ")
    assert doc["book"]["date"] == "2026-10-30"
    kinds = [ln["kind"] for ln in doc["book"]["lines"]]
    assert kinds == ["card", "supplier", "wastage", "unreadable"]
    qtys = [ln["qty"] for ln in doc["book"]["lines"]]
    assert qtys == [2, None, 1, 1]  # supplier keeps its unknown qty


@pytest.mark.parametrize("payload", [
    {"doc_type": "book_page",
     "book": {"lines": [{"line_no": [3], "text": "x"}]}},
    {"doc_type": "book_page",
     "book": {"lines": [{"line_no": {"n": 3}, "text": "x"}]}},
    {"doc_type": "book_page", "book": {"lines": 5}},
    {"doc_type": "book_page", "book": [{"lines": []}]},
    {"doc_type": "book_page", "book": "cash 2 bread"},
    {"doc_type": "book_page",
     "book": {"lines": [{"line_no": 1, "qty": {"n": 2}}]}},
    {"doc_type": "book_page",
     "book": {"crossed_out": {"n": 1}, "lines": []}},
    {"doc_type": "receipt",
     "receipt": {"lines": [{"line_no": 1, "packs": "two",
                             "qty_units": "many"}]}},
    {"doc_type": "other", "products": [{"visible_count": [2]}]},
    {"doc_type": "other", "products": 7},
])
def test_shop_photo_malformed_shapes_survive(photo_env, monkeypatch, payload):
    monkeypatch.setattr(vision, "_complete_vision",
                        lambda m, r: _vresp(json.dumps(payload)))
    doc = vision.read_shop_photo(_tiny_jpg(), SHOP)  # never raises
    assert doc["doc_type"] in ("expiry", "book_page", "receipt", "other")
    assert isinstance(doc["book"]["lines"], list)
    assert isinstance(doc["receipt"]["lines"], list)
    assert isinstance(doc["products"], list)


@pytest.mark.parametrize("payload", [[{"doc_type": "book_page"}],
                                     "just text", 42])
def test_shop_photo_non_object_degrades(photo_env, monkeypatch, payload):
    monkeypatch.setattr(vision, "_complete_vision",
                        lambda m, r: _vresp(json.dumps(payload)))
    with pytest.raises(VisionError):
        vision.read_shop_photo(_tiny_jpg(), SHOP)


def test_shop_photo_sanitizes_receipt_numbers(photo_env, monkeypatch):
    payload = {"doc_type": "receipt",
               "receipt": {"lines": [{"line_no": "1", "packs": "2",
                                       "pack_size": 6, "qty_units": "12"}]}}
    monkeypatch.setattr(vision, "_complete_vision",
                        lambda m, r: _vresp(json.dumps(payload)))
    doc = vision.read_shop_photo(_tiny_jpg(), SHOP)
    (line,) = doc["receipt"]["lines"]
    assert line["line_no"] == 1
    assert line["packs"] == 2.0
    assert line["pack_size"] == 6.0
    assert line["qty_units"] == 12.0
