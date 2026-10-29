"""Vonage channel: parsing, sender shape, endpoint wiring. HTTP mocked."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.agent as agent
import app.llm as llm
import app.main as main
import app.vonage as vonage
from app.main import app
from app.voice import VoiceError
from app.vonage import (
    VonageSender,
    download_vonage_media,
    parse_vonage_webhook,
)
from app.whatsapp import reset_fake
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID


PATCH_CALLS: list = []


@pytest.fixture()
def vg_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SISI_DB", str(tmp_path / "vg.db"))
    monkeypatch.setenv("VONAGE_API_KEY", "vg-key")
    monkeypatch.setenv("VONAGE_API_SECRET", "vg-secret")
    monkeypatch.delenv("VONAGE_WHATSAPP_FROM", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    monkeypatch.delenv("WHATSAPP_TOKEN", raising=False)
    monkeypatch.setattr(llm, "TOKEN_LOG", tmp_path / "usage.log")

    def _fake_patch(url, **kw):
        PATCH_CALLS.append({"url": url, **kw})
        return SimpleNamespace(status_code=200,
                               raise_for_status=lambda: None)

    monkeypatch.setattr(vonage.httpx, "patch", _fake_patch)
    PATCH_CALLS.clear()
    reset_fake()
    main._rate_hits.clear()
    seed_demo(SHOP)
    import app.db as _db
    _c = _db.connect()
    _c.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?", (SHOP,))
    _c.commit()
    _c.close()
    return tmp_path


def _vg_payload(message: dict, mid: str = "vg-uuid-1") -> dict:
    return {"from": {"number": SHOP, "type": "whatsapp"},
            "to": {"number": "14157386102", "type": "whatsapp"},
            "message_uuid": mid, "timestamp": "2026-10-27T00:00:00Z",
            "message": message}


# --- parsing ---------------------------------------------------------------


def test_parse_text_audio_image():
    msgs = parse_vonage_webhook(_vg_payload({"message_type": "text", "text": "hi"}))
    assert len(msgs) == 1
    assert (msgs[0].shop_id, msgs[0].kind, msgs[0].text, msgs[0].channel) == (SHOP, "text", "hi", "vonage")

    msgs = parse_vonage_webhook(_vg_payload({"message_type": "audio",
                                             "audio": {"url": "https://m.test/a"}}))
    assert len(msgs) == 1 and msgs[0].kind == "media:audio"
    assert msgs[0].media_url == "https://m.test/a" and msgs[0].text is None

    msgs = parse_vonage_webhook(_vg_payload({"message_type": "image",
                                             "image": {"url": "https://m.test/i"}}))
    assert len(msgs) == 1 and msgs[0].kind == "media:image"
    assert msgs[0].media_url == "https://m.test/i"


def test_parse_video_and_file_are_graceful_kinds():
    msgs = parse_vonage_webhook(_vg_payload({"message_type": "video",
                                             "video": {"url": "https://m.test/v"}}))
    assert len(msgs) == 1 and msgs[0].kind == "media:video"
    msgs = parse_vonage_webhook(_vg_payload({"message_type": "file",
                                             "file": {"url": "https://m.test/d"}},
                                            "vg-uuid-9"))
    assert len(msgs) == 1 and msgs[0].kind == "media:document"


def test_parse_reply_unknown_and_garbage():
    msgs = parse_vonage_webhook(_vg_payload({"message_type": "reply",
                                             "reply": {"id": "Yes", "title": "Yes"}}))
    assert len(msgs) == 1 and (msgs[0].kind, msgs[0].text) == ("button", "Yes")
    assert parse_vonage_webhook(_vg_payload({"message_type": "sticker"})) == []
    assert parse_vonage_webhook({}) == []
    assert parse_vonage_webhook({"message": "nope"}) == []
    assert parse_vonage_webhook(_vg_payload({"message_type": "text"}, mid="")) != []


# --- sender ----------------------------------------------------------------


class _FakePost:
    def __init__(self):
        self.calls = []

    def __call__(self, url, auth=None, json=None, timeout=None):
        self.calls.append({"url": url, "auth": auth, "json": json})

        class Resp:
            status_code = 202
            text = '{"message_uuid":"out-1"}'

            def raise_for_status(self):
                pass

            def json(self):
                return {"message_uuid": "out-1"}

        return Resp()


def test_sender_request_shape(vg_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(vonage.httpx, "post", fake)
    VonageSender().send_text("+27710000001", "hello there")
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == "https://messages-sandbox.nexmo.com/v1/messages"
    assert call["auth"] == ("vg-key", "vg-secret")
    assert call["json"] == {"from": "14157386102", "to": "27710000001",
                            "channel": "whatsapp", "message_type": "text",
                            "text": "hello there"}


def test_sender_splits_long_replies(vg_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(vonage.httpx, "post", fake)
    body = "word " * 1200  # 6000 chars
    VonageSender().send_text(SHOP, body.strip())
    assert len(fake.calls) >= 4
    assert all(len(c["json"]["text"]) <= 1500 for c in fake.calls)
    assert " ".join(c["json"]["text"] for c in fake.calls) == body.strip()


def test_sender_buttons_interactive_custom(vg_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(vonage.httpx, "post", fake)
    VonageSender().send_buttons(SHOP, "Save?", ["Yes", "No"])
    assert len(fake.calls) == 1
    payload = fake.calls[0]["json"]
    assert payload["message_type"] == "custom"
    node = payload["custom"]["interactive"]
    assert node["type"] == "button"
    assert node["body"]["text"] == "Save?"
    replies = [b["reply"] for b in node["action"]["buttons"]]
    assert [(r["id"], r["title"]) for r in replies] == [("Yes", "Yes"),
                                                         ("No", "No")]


def test_sender_logs_status_without_secrets(vg_env, monkeypatch, caplog):
    import logging

    fake = _FakePost()
    monkeypatch.setattr(vonage.httpx, "post", fake)
    with caplog.at_level(logging.INFO, logger="sisi.vonage"):
        VonageSender().send_text("27710000002", "hi")
    assert "vonage reply sent to=27710000002 status=202" in caplog.text
    assert "out-1" in caplog.text
    assert "vg-key" not in caplog.text and "vg-secret" not in caplog.text


def test_sender_missing_creds_sends_nothing(vg_env, monkeypatch):
    monkeypatch.delenv("VONAGE_API_KEY")
    fake = _FakePost()
    monkeypatch.setattr(vonage.httpx, "post", fake)
    with pytest.raises(RuntimeError, match="VONAGE_API_KEY"):
        VonageSender().send_text(SHOP, "hi")
    assert fake.calls == []


# --- media download --------------------------------------------------------


def test_download_vonage_media(vg_env, monkeypatch):
    seen = {}

    class Resp:
        content = b"media-bytes"

        def raise_for_status(self):
            pass

    def fake_get(url, auth=None, timeout=None, follow_redirects=None):
        seen["url"] = url
        seen["auth"] = auth
        return Resp()

    monkeypatch.setattr(vonage.httpx, "get", fake_get)
    assert download_vonage_media("https://m.test/a") == b"media-bytes"
    assert seen == {"url": "https://m.test/a", "auth": ("vg-key", "vg-secret")}


def test_download_vonage_media_missing_creds(vg_env, monkeypatch):
    monkeypatch.delenv("VONAGE_API_SECRET")
    with pytest.raises(VoiceError, match="couldn't fetch"):
        download_vonage_media("https://m.test/a")


# --- endpoint wiring (model + HTTP stubbed) --------------------------------


def _resp(content=None, calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content,
                                                         tool_calls=calls or []))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2))


def _plan(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                              total_tokens=2))


def _scripted_llm(monkeypatch):
    import app.interpreter as interp

    def fake(messages):
        user_text = next(m["content"] for m in reversed(messages)
                         if isinstance(m, dict) and m.get("role") == "user")
        line = user_text.split("\n")[1] if "\n" in user_text else user_text
        if "PHOTO EVIDENCE" in user_text and "expiry" in user_text:
            tasks = [{"kind": "expiry_photo", "args": {}, "quote": ""}]
        elif "owe" in line:
            tasks = [{"kind": "balance", "args": {"customer": "Sipho"},
                      "quote": line}]
        else:
            tasks = [{"kind": "chat", "args": {"reply": "huh?"}, "quote": ""}]
        return _plan(json.dumps({"understood": "vonage test", "tasks": tasks,
                                 "question": None, "options": []}))

    monkeypatch.setattr(interp, "propose_plan", fake)


def _post_vonage(client, message, mid="vg-uuid-1"):
    return client.post("/webhook/vonage", json=_vg_payload(message, mid))


def test_vonage_video_gets_graceful_reply(vg_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(vonage.httpx, "post", fake)
    client = TestClient(app)
    r = _post_vonage(client, {"message_type": "video",
                              "video": {"url": "https://m.test/v"}}, "vg-vid-1")
    assert r.status_code == 200
    assert len(fake.calls) == 1
    assert "can't watch videos" in fake.calls[0]["json"]["text"]


def test_vonage_file_gets_graceful_reply(vg_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(vonage.httpx, "post", fake)
    client = TestClient(app)
    r = _post_vonage(client, {"message_type": "file",
                              "file": {"url": "https://m.test/d"}}, "vg-doc-1")
    assert r.status_code == 200
    assert len(fake.calls) == 1
    assert "can't read files" in fake.calls[0]["json"]["text"]


def test_vonage_text_end_to_end(vg_env, monkeypatch):
    _scripted_llm(monkeypatch)
    fake = _FakePost()
    monkeypatch.setattr(vonage.httpx, "post", fake)
    client = TestClient(app)
    r = _post_vonage(client, {"message_type": "text", "text": "How much does Sipho owe?"})
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert len(fake.calls) == 1
    body = fake.calls[0]["json"]["text"]
    assert "R73.00" in body and "delete my data" in body  # shared handler + consent
    from app.whatsapp import get_sender

    assert get_sender().pop_shop(SHOP) == []  # vonage reply did not use the meta sender


def test_vonage_voice_note_end_to_end(vg_env, monkeypatch):
    _scripted_llm(monkeypatch)
    monkeypatch.setattr(main, "download_vonage_media", lambda _url: b"ogg-bytes")
    monkeypatch.setattr(agent, "transcribe_audio", lambda *a, **k: "How much does Sipho owe?")
    fake = _FakePost()
    monkeypatch.setattr(vonage.httpx, "post", fake)
    client = TestClient(app)
    r = _post_vonage(client, {"message_type": "audio", "audio": {"url": "https://m.test/a"}},
                     "vg-uuid-2")
    assert r.status_code == 200
    assert len(fake.calls) == 2
    assert fake.calls[0]["json"]["text"].startswith("⏳")
    body = fake.calls[1]["json"]["text"]
    assert body.startswith("Heard:") and "R73.00" in body


def test_vonage_image_end_to_end(vg_env, monkeypatch):
    _scripted_llm(monkeypatch)
    monkeypatch.setenv("SISI_TODAY", "2026-11-01")
    monkeypatch.setattr(main, "download_vonage_media", lambda _url: b"jpg-bytes")
    monkeypatch.setattr(agent, "read_shop_photo", lambda *a, **k: {
        "doc_type": "expiry", "book": {}, "receipt": {},
        "expiry": {"stamp_count": 1, "items": [
            {"product": "Fresh Milk 1L", "qty": 2,
             "expiry_date": "2026-10-30"}]}})
    fake = _FakePost()
    monkeypatch.setattr(vonage.httpx, "post", fake)
    client = TestClient(app)
    r = _post_vonage(client, {"message_type": "image", "image": {"url": "https://m.test/i"}},
                     "vg-uuid-3")
    assert r.status_code == 200
    assert len(fake.calls) == 2
    assert fake.calls[0]["json"]["text"].startswith("⏳")
    payload = fake.calls[1]["json"]
    assert payload["message_type"] == "custom"  # preview buttons interactive
    node = payload["custom"]["interactive"]
    assert "• 2 × Fresh Milk 1L 30 Oct — expired" in node["body"]["text"]
    replies = [b["reply"]["title"] for b in node["action"]["buttons"]]
    assert replies == ["Yes", "No"]


def _vg_flat_payload(message_type: str, mid: str = "vg-real-1", **extra) -> dict:
    """Real sandbox inbound shape: flat fields, no nested 'message' object."""
    payload = {
        "channel": "whatsapp",
        "message_uuid": mid,
        "from": {"number": "27710000002", "type": "whatsapp"},
        "to": {"number": "14157386102", "type": "whatsapp"},
        "timestamp": "2026-10-27T05:00:00Z",
        "message_type": message_type,
        "profile": {"name": "Donovan"},
    }
    payload.update(extra)
    return payload


def test_parse_flat_sandbox_shape():
    msgs = parse_vonage_webhook(_vg_flat_payload("text", text="hi"))
    assert len(msgs) == 1
    assert (msgs[0].shop_id, msgs[0].kind, msgs[0].text, msgs[0].channel) == (
        "27710000002", "text", "hi", "vonage")
    assert msgs[0].message_id == "vg-real-1"

    msgs = parse_vonage_webhook(_vg_flat_payload("audio", audio={"url": "https://m.test/a"}))
    assert len(msgs) == 1 and msgs[0].kind == "media:audio"
    assert msgs[0].media_url == "https://m.test/a"

    msgs = parse_vonage_webhook(_vg_flat_payload("image", image={"url": "https://m.test/i"}))
    assert len(msgs) == 1 and msgs[0].kind == "media:image"
    assert msgs[0].media_url == "https://m.test/i"


def test_vonage_flat_text_end_to_end(vg_env, monkeypatch):
    from seed.seed_demo import seed_demo

    seed_demo("27710000002")  # the reporter's shop, like the demo shop
    _scripted_llm(monkeypatch)
    fake = _FakePost()
    monkeypatch.setattr(vonage.httpx, "post", fake)
    client = TestClient(app)
    r = client.post("/webhook/vonage",
                    json=_vg_flat_payload("text", text="How much does Sipho owe?"))
    assert r.status_code == 200
    assert len(fake.calls) == 1
    sent = fake.calls[0]["json"]
    assert sent["from"] == "14157386102" and sent["to"] == "27710000002"
    assert "R73.00" in sent["text"]


def test_vonage_status_and_bad_json(vg_env):
    client = TestClient(app)
    assert client.post("/webhook/vonage/status").status_code == 200
    assert client.post("/webhook/vonage/status", json={"message": {"x": 1}}).status_code == 200
    bad = client.post("/webhook/vonage", content=b"not json",
                      headers={"Content-Type": "application/json"})
    assert bad.status_code == 200 and bad.json() == {"status": "ignored"}


def test_mark_read_patch_shape(vg_env, monkeypatch):
    monkeypatch.setenv("VONAGE_API_BASE", "https://messages.nexmo.com")
    assert VonageSender().mark_read("uuid-9") is True
    assert len(PATCH_CALLS) == 1
    call = PATCH_CALLS[0]
    assert call["url"] == "https://messages.nexmo.com/v1/messages/uuid-9"
    assert call["json"] == {"status": "read",
                            "replying_indicator": {"show": "true", "type": "text"}}


def test_mark_read_failure_is_silent(vg_env, monkeypatch):
    def _boom(url, **kw):
        raise RuntimeError("nope")

    monkeypatch.setenv("VONAGE_API_BASE", "https://messages.nexmo.com")
    monkeypatch.setattr(vonage.httpx, "patch", _boom)
    assert VonageSender().mark_read("uuid-9") is False


def test_mark_read_skipped_on_sandbox(vg_env):
    assert VonageSender().mark_read("uuid-9") is False
    assert PATCH_CALLS == []
