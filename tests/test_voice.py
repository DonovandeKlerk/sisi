"""Voice pipeline: conversion, transcription shape, agent + webhook wiring. Offline."""

import json
import shutil
import subprocess
import wave
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import app.agent as agent
import app.llm as llm
import app.main as main
import app.voice as voice
from app.main import app
from app.voice import CANT_HEAR, VoiceError, to_wav_16k_mono, transcribe_wav
from app.whatsapp import download_media, reset_fake
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID

ffmpeg_available = shutil.which("ffmpeg") is not None


@pytest.fixture()
def voice_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SISI_DB", str(tmp_path / "voice.db"))
    monkeypatch.setenv("APP_SECRET", "test-app-secret")
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    monkeypatch.delenv("WHATSAPP_TOKEN", raising=False)
    monkeypatch.delenv("SISI_LANGUAGE_BIAS", raising=False)
    monkeypatch.setattr(llm, "TOKEN_LOG", tmp_path / "usage.log")
    reset_fake()
    seed_demo(SHOP)
    return tmp_path


def _tone_ogg() -> bytes:
    """One second of 440 Hz tone as ogg/opus bytes (needs an opus encoder)."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "libopus", "-ar", "16000", "-ac", "1", "-f", "ogg", "pipe:1"],
        capture_output=True, timeout=30,
    )
    if proc.returncode != 0:
        pytest.skip("no opus encoder in this ffmpeg build")
    return proc.stdout


# --- conversion ------------------------------------------------------------


@pytest.mark.skipif(not ffmpeg_available, reason="ffmpeg not installed")
def test_ogg_to_wav_16k_mono():
    wav = to_wav_16k_mono(_tone_ogg(), ".ogg")
    assert wav[:4] == b"RIFF"
    with wave.open(__import__("io").BytesIO(wav), "rb") as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, 16000, 2)


@pytest.mark.skipif(not ffmpeg_available, reason="ffmpeg not installed")
def test_garbage_audio_raises_voice_error():
    with pytest.raises(VoiceError):
        to_wav_16k_mono(b"not audio at all", ".ogg")


def test_conversion_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    with pytest.raises(VoiceError, match="not working"):
        to_wav_16k_mono(b"whatever", ".ogg")


# --- transcription request shape (mocked HTTP) -----------------------------


class _FakeTResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_transcribe_request_shape_and_turns(voice_env, monkeypatch):
    seen = {}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers
        seen["data"] = data
        seen["files"] = files
        return _FakeTResp({"turns": [{"transcript": "Sipho took bread"},
                                     {"transcript": "on the book"}]})

    monkeypatch.setattr(voice.httpx, "post", fake_post)
    out = transcribe_wav(b"RIFF....wav-bytes", SHOP)
    assert out == "Sipho took bread on the book"
    assert seen["url"] == "https://api.meta.ai/v1/asr/transcribe"
    assert seen["headers"]["Authorization"] == "Bearer test-key"
    req = json.loads(seen["data"]["request"])
    assert req["model"] == "muse-voice-transcribe-1.0"
    assert req["audioEncoding"] == "WAV"
    assert req["languageBias"] == ["English"]
    assert "Sasko" in req["keywords"] and "ibhuku" in req["keywords"]
    name, content, ctype = seen["files"]["audio"]
    assert name == "voice.wav" and ctype == "audio/wav"


def test_transcribe_language_bias_override(voice_env, monkeypatch):
    monkeypatch.setenv("SISI_LANGUAGE_BIAS", "English,isiZulu")
    seen = {}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        seen["data"] = data
        return _FakeTResp({"turns": []})

    monkeypatch.setattr(voice.httpx, "post", fake_post)
    assert transcribe_wav(b"RIFF....", SHOP) == ""
    assert json.loads(seen["data"]["request"])["languageBias"] == ["English", "isiZulu"]


def test_transcribe_extra_keywords_merged(voice_env, monkeypatch):
    seen = {}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        seen["data"] = data
        return _FakeTResp({"turns": [{"transcript": "hi"}]})

    monkeypatch.setattr(voice.httpx, "post", fake_post)
    transcribe_wav(b"RIFF....", SHOP, ["Sipho", "Coke"])
    req = json.loads(seen["data"]["request"])
    assert "Sipho" in req["keywords"] and "Sasko" in req["keywords"]
    assert len(req["keywords"]) == len(set(req["keywords"]))  # deduped


def test_handle_voice_passes_shop_names_as_keywords(voice_env, monkeypatch):
    import app.db as db

    seen = {}

    def fake_transcribe(audio, suffix, shop_id, extra=None):
        seen["extra"] = extra
        return "undo"

    monkeypatch.setattr(agent, "transcribe_audio", fake_transcribe)
    conn = db.connect(str(voice_env / "voice.db"))
    try:
        agent.handle_voice(conn, SHOP, b"audio", ".ogg")
    finally:
        conn.close()
    assert "Sipho" in seen["extra"] and "Coke 2L" in seen["extra"]


def test_transcribe_api_error_prints_body(voice_env, monkeypatch, capsys):
    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        req = httpx.Request("POST", url)
        resp = httpx.Response(400, text='{"error":{"message":"bad audio"}}', request=req)
        raise httpx.HTTPStatusError("bad", request=req, response=resp)

    monkeypatch.setattr(voice.httpx, "post", fake_post)
    with pytest.raises(VoiceError, match="couldn't hear"):
        transcribe_wav(b"RIFF....", SHOP)
    assert "bad audio" in capsys.readouterr().err


def test_transcribe_missing_key(voice_env, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY")
    with pytest.raises(VoiceError, match="not working"):
        transcribe_wav(b"RIFF....", SHOP)


def test_transcribe_too_long(voice_env):
    with pytest.raises(VoiceError, match="too long"):
        transcribe_wav(b"x" * (voice.MAX_AUDIO_BYTES + 1), SHOP)


# --- media download (mocked HTTP) ------------------------------------------


def test_download_media_two_step(voice_env, monkeypatch):
    monkeypatch.setenv("WHATSAPP_TOKEN", "tok")
    calls = []

    class FakeGet:
        def __init__(self, payload=None, content=b""):
            self._payload = payload
            self.content = content

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers))
        if url.endswith("/media-1"):
            return FakeGet({"url": "https://media.example/x"})
        return FakeGet(content=b"ogg-bytes")

    monkeypatch.setattr(voice.httpx, "get", fake_get)
    assert download_media("media-1") == b"ogg-bytes"
    assert calls[0][0] == "https://graph.facebook.com/v26.0/media-1"
    assert calls[0][1]["Authorization"] == "Bearer tok"
    assert calls[1][0] == "https://media.example/x"


def test_download_media_without_token(voice_env):
    with pytest.raises(VoiceError, match="couldn't fetch"):
        download_media("media-1")


# --- agent + endpoints -----------------------------------------------------


def _resp(content=None, calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content,
                                                         tool_calls=calls or []))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2))


def test_handle_voice_prefixes_heard(voice_env, monkeypatch):
    import sqlite3

    monkeypatch.setattr(agent, "transcribe_audio", lambda *a, **k: "How much does Sipho owe?")

    def fake_complete(messages):
        if any(isinstance(m, dict) and m.get("role") == "tool" for m in messages):
            tool = next(m["content"] for m in reversed(messages)
                        if isinstance(m, dict) and m.get("role") == "tool")
            return _resp(f"OK: {tool}")
        return _resp(calls=[SimpleNamespace(
            id="c1", function=SimpleNamespace(name="get_balance",
                                              arguments='{"customer":"Sipho"}'))])

    monkeypatch.setattr(llm, "_complete", fake_complete)
    import app.db as db

    conn = db.connect(str(voice_env / "voice.db"))
    try:
        transcript, reply, buttons = agent.handle_voice(conn, SHOP, b"audio", ".ogg")
    finally:
        conn.close()
    assert transcript == "How much does Sipho owe?"
    assert reply.startswith("Heard: “How much does Sipho owe?”")
    assert "R73.00" in reply


def test_handle_voice_empty_and_error(voice_env, monkeypatch):
    import app.db as db

    monkeypatch.setattr(agent, "transcribe_audio", lambda *a, **k: "  ")
    conn = db.connect(str(voice_env / "voice.db"))
    try:
        t, reply, b = agent.handle_voice(conn, SHOP, b"audio", ".ogg")
    finally:
        conn.close()
    assert t is None and "couldn't hear anything" in reply and b is None

    def boom(*a, **k):
        raise VoiceError(CANT_HEAR)

    monkeypatch.setattr(agent, "transcribe_audio", boom)
    conn = db.connect(str(voice_env / "voice.db"))
    try:
        t, reply, b = agent.handle_voice(conn, SHOP, b"audio", ".ogg")
    finally:
        conn.close()
    assert (t, reply, b) == (None, CANT_HEAR, None)


def test_sim_voice_endpoint(voice_env, monkeypatch):
    monkeypatch.setattr(agent, "transcribe_audio", lambda *a, **k: "undo")
    client = TestClient(app)
    r = client.post("/_sim/voice", params={"shop": SHOP}, content=b"ogg-bytes",
                    headers={"Content-Type": "audio/ogg"})
    assert r.status_code == 200
    data = r.json()
    assert data["transcript"] == "undo"
    assert "nothing to undo" in data["reply"]
    assert data["buttons"] == []


def test_sim_voice_404_when_live(voice_env, monkeypatch):
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "pnid")
    monkeypatch.setenv("WHATSAPP_TOKEN", "tok")
    client = TestClient(app)
    r = client.post("/_sim/voice", params={"shop": SHOP}, content=b"x")
    assert r.status_code == 404


def test_webhook_audio_message_end_to_end(voice_env, monkeypatch):
    monkeypatch.setattr(main, "download_media", lambda _mid, _label="voice note": b"ogg-bytes")
    monkeypatch.setattr(agent, "transcribe_audio", lambda *a, **k: "undo")
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{
        "value": {"messages": [{"id": "wamid.v1", "from": SHOP, "type": "audio",
                                "audio": {"id": "media-1", "mime_type": "audio/ogg"}}]}}]}]}
    raw = json.dumps(payload).encode()
    import hashlib
    import hmac

    sig = hmac.new(b"test-app-secret", raw, hashlib.sha256).hexdigest()
    client = TestClient(app)
    r = client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": f"sha256={sig}"})
    assert r.status_code == 200
    out = client.get("/_sim/outbox", params={"shop": SHOP}).json()["messages"]
    assert len(out) == 1 and out[0]["body"].startswith("Heard: “undo”")


def test_webhook_audio_without_media_id(voice_env):
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{
        "value": {"messages": [{"id": "wamid.v2", "from": SHOP, "type": "audio",
                                "audio": {}}]}}]}]}
    raw = json.dumps(payload).encode()
    import hashlib
    import hmac

    sig = hmac.new(b"test-app-secret", raw, hashlib.sha256).hexdigest()
    client = TestClient(app)
    assert client.post("/webhook", content=raw,
                       headers={"X-Hub-Signature-256": f"sha256={sig}"}).status_code == 200
    out = client.get("/_sim/outbox", params={"shop": SHOP}).json()["messages"]
    assert len(out) == 1 and "empty" in out[0]["body"]
