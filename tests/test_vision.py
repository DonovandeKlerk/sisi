"""Photo expiry pipeline: vision shape, agent + webhook wiring. Offline."""

import base64
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
    return tmp_path


def _vresp(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2))


def _tiny_jpg() -> bytes:
    """A real 8x8 jpeg (ffmpeg-generated when available, else a minimal stub)."""
    if ffmpeg_available:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=size=8x8:rate=1:duration=1",
             "-frames:v", "1", "-f", "mjpeg", "pipe:1"],
            capture_output=True, timeout=30,
        )
        if proc.returncode == 0 and proc.stdout[:2] == b"\xff\xd8":
            return proc.stdout
    return base64.b64decode(
        "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////"
        "//////////////////////////////////////////2wBDAf////////////////////////////////"
        "//////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAA"
        "AAAAAP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AK//Z")


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


def test_handle_photo_proposes_preview(photo_env, monkeypatch):
    import app.db as db

    monkeypatch.setenv("SISI_TODAY", "2026-11-01")
    monkeypatch.setattr(agent, "read_expiry_photo", lambda *a, **k: [
        {"product": "Fresh Milk 1L", "qty": 2, "expiry_date": "2026-10-30"}])
    conn = db.connect(str(photo_env / "photo.db"))
    try:
        findings, reply, buttons = agent.handle_photo(conn, SHOP, b"jpg")
    finally:
        conn.close()
    assert findings[0]["product"] == "Fresh Milk 1L"
    assert "• 2 × Fresh Milk 1L 30 Oct — expired" in reply
    assert buttons == ["Yes", "No"]


def test_handle_photo_nothing_readable(photo_env, monkeypatch):
    import app.db as db

    monkeypatch.setattr(agent, "read_expiry_photo", lambda *a, **k: [])
    conn = db.connect(str(photo_env / "photo.db"))
    try:
        findings, reply, buttons = agent.handle_photo(conn, SHOP, b"jpg")
    finally:
        conn.close()
    assert (findings, buttons) == ([], None)
    assert "could not read any dates" in reply

    def boom(*a, **k):
        raise VisionError(CANT_READ)

    monkeypatch.setattr(agent, "read_expiry_photo", boom)
    conn = db.connect(str(photo_env / "photo.db"))
    try:
        findings, reply, buttons = agent.handle_photo(conn, SHOP, b"jpg")
    finally:
        conn.close()
    assert (findings, reply, buttons) == (None, CANT_READ, None)


def test_sim_photo_endpoint(photo_env, monkeypatch):
    monkeypatch.setenv("SISI_TODAY", "2026-11-01")
    monkeypatch.setattr(agent, "read_expiry_photo", lambda *a, **k: [
        {"product": "Fresh Milk 1L", "qty": 2, "expiry_date": "2026-10-30"}])
    client = TestClient(app)
    r = client.post("/_sim/photo", params={"shop": SHOP}, content=b"jpg-bytes",
                    headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 200
    data = r.json()
    assert data["findings"][0]["qty"] == 2
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
    monkeypatch.setattr(agent, "read_expiry_photo", lambda *a, **k: [
        {"product": "Fresh Milk 1L", "qty": 2, "expiry_date": "2026-10-30"}])
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{
        "value": {"messages": [{"id": "wamid.p1", "from": SHOP, "type": "image",
                                "image": {"id": "media-1", "mime_type": "image/jpeg"}}]}}]}]}
    raw = json.dumps(payload).encode()
    sig = hmac.new(b"test-app-secret", raw, hashlib.sha256).hexdigest()
    client = TestClient(app)
    r = client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": f"sha256={sig}"})
    assert r.status_code == 200
    out = client.get("/_sim/outbox", params={"shop": SHOP}).json()["messages"]
    assert len(out) == 1 and out[0]["type"] == "buttons"
    assert "• 2 × Fresh Milk 1L 30 Oct — expired" in out[0]["body"]


def test_download_media_photo_label(photo_env):
    with pytest.raises(VoiceError, match="photo"):
        download_media("media-1", "photo")
