"""Infobip channel: parsing, sender shape, endpoints, provider lock.

HTTP mocked. Fixture secrets are fake ("ib-key");nothing here touches live
credentials. Endpoint shapes follow the official Infobip OpenAPI spec
(https://api.infobip.com/platform/1/openapi/whatsapp).
"""

import httpx
import pytest
from fastapi.testclient import TestClient

import app.infobip as infobip
import app.llm as llm
import app.main as main
from app.infobip import (
    InfobipSender,
    download_infobip_media,
    parse_infobip_webhook,
)
from app.main import app
from app.providers import WrongProviderError, sender_for_channel
from app.sendqueue import RateLimited
from app.voice import VoiceError
from app.whatsapp import reset_fake
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID
SENDER = "447860088970"
TOKEN = "hook-token-1"


@pytest.fixture()
def ib_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SISI_DB", str(tmp_path / "ib.db"))
    monkeypatch.setenv("INFOBIP_API_KEY", "ib-key")
    monkeypatch.setenv("INFOBIP_BASE_URL", "https://test.api.infobip.com")
    monkeypatch.setenv("INFOBIP_WHATSAPP_FROM", SENDER)
    monkeypatch.setenv("INFOBIP_WEBHOOK_TOKEN", TOKEN)
    for var in ("WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_TOKEN",
                "VONAGE_API_KEY", "VONAGE_API_SECRET", "SISI_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(llm, "TOKEN_LOG", tmp_path / "usage.log")
    reset_fake()
    main._rate_hits.clear()
    seed_demo(SHOP)
    import app.db as _db
    _c = _db.connect()
    _c.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?", (SHOP,))
    _c.commit()
    _c.close()
    return tmp_path


def _item(message: dict, mid: str = "ib-mid-1", frm: str = SHOP,
          to: str = SENDER) -> dict:
    return {"from": frm, "to": to, "integrationType": "WHATSAPP",
            "receivedAt": "2026-10-28T10:00:00.000+0000", "messageId": mid,
            "message": message}


def _payload(*items: dict) -> dict:
    return {"results": list(items), "messageCount": len(items)}


# --- parsing ---------------------------------------------------------------

def test_parse_text_audio_voice_image(ib_env):
    msgs = parse_infobip_webhook(_payload(_item({"type": "TEXT", "text": "hi"})))
    assert len(msgs) == 1
    assert (msgs[0].shop_id, msgs[0].kind, msgs[0].text,
            msgs[0].channel) == (SHOP, "text", "hi", "infobip")

    for mtype in ("AUDIO", "VOICE"):
        msgs = parse_infobip_webhook(_payload(_item(
            {"type": mtype, "url": "https://test.api.infobip.com/m/1"})))
        assert len(msgs) == 1 and msgs[0].kind == "media:audio"
        assert msgs[0].media_url.endswith("/m/1") and msgs[0].text is None

    msgs = parse_infobip_webhook(_payload(_item(
        {"type": "IMAGE", "url": "https://test.api.infobip.com/m/2",
         "caption": "logo"})))
    assert len(msgs) == 1 and msgs[0].kind == "media:image"
    assert msgs[0].caption == "logo"


def test_parse_replies_prefer_title(ib_env):
    msgs = parse_infobip_webhook(_payload(_item(
        {"type": "INTERACTIVE_BUTTON_REPLY", "id": "Yes", "title": "Yes"})))
    assert len(msgs) == 1 and (msgs[0].kind, msgs[0].text) == ("button", "Yes")
    msgs = parse_infobip_webhook(_payload(_item(
        {"type": "INTERACTIVE_LIST_REPLY", "id": "3",
         "title": "Who owes me"})))
    assert (msgs[0].kind, msgs[0].text) == ("button", "Who owes me")
    msgs = parse_infobip_webhook(_payload(_item(
        {"type": "INTERACTIVE_BUTTON_REPLY", "id": "fallback-id"})))
    assert msgs[0].text == "fallback-id"


def test_parse_document_video_get_unsupported_kind(ib_env):
    msgs = parse_infobip_webhook(_payload(_item(
        {"type": "DOCUMENT", "url": "https://x.test/r.pdf"})))
    assert len(msgs) == 1
    assert (msgs[0].kind, msgs[0].text) == ("media:document", None)
    msgs = parse_infobip_webhook(_payload(_item({"type": "VIDEO"})))
    assert (msgs[0].kind, msgs[0].text) == ("media:video", None)


def test_parse_drops_unknown_and_garbage(ib_env):
    assert parse_infobip_webhook(_payload(
        _item({"type": "REACTION", "emoji": "👍"}))) == []
    assert parse_infobip_webhook(_payload(
        _item({"type": "LOCATION"}))) == []
    assert parse_infobip_webhook({}) == []
    assert parse_infobip_webhook({"results": "nope"}) == []
    assert parse_infobip_webhook(_payload("nope")) == []
    assert parse_infobip_webhook(_payload(_item({"type": "TEXT"},
                                                frm=""))) == []


def test_parse_to_mismatch_dropped(ib_env, monkeypatch):
    bad = _payload(_item({"type": "TEXT", "text": "hi"}, to="441111111111"))
    assert parse_infobip_webhook(bad) == []
    monkeypatch.delenv("INFOBIP_WHATSAPP_FROM")
    assert len(parse_infobip_webhook(bad)) == 1  # unenforced when unconfigured


def test_parse_missing_id_is_stable(ib_env):
    item = _item({"type": "TEXT", "text": "hi"}, mid="")
    del item["messageId"]
    a = parse_infobip_webhook(_payload(item))[0].message_id
    b = parse_infobip_webhook(_payload(dict(item)))[0].message_id
    assert a == b and a.startswith("infobip-")


# --- sender ----------------------------------------------------------------

class _FakePost:
    def __init__(self, script=None):
        self.calls = []
        self.script = list(script or [])

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return _ok()


class _FakeResp:
    status_code = 200
    text = '{"messageId":"ib-out-1"}'

    def raise_for_status(self):
        pass

    def json(self):
        return {"messageId": "ib-out-1"}


def _ok():
    return _FakeResp()


def _http_error(status):
    from types import SimpleNamespace
    req = SimpleNamespace()
    resp = httpx.Response(status, request=httpx.Request("POST", "https://x"),
                          text="err")
    return httpx.HTTPStatusError(f"http {status}", request=req, response=resp)


def test_send_text_shape_and_auth(ib_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(infobip.httpx, "post", fake)
    InfobipSender().send_text(SHOP, "hello there")
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == ("https://test.api.infobip.com"
                           "/whatsapp/1/message/text")
    assert call["headers"]["Authorization"] == "App ib-key"
    assert call["json"] == {"from": SENDER, "to": SHOP,
                            "content": {"text": "hello there"}}


def test_base_url_normalized(monkeypatch):
    monkeypatch.setenv("INFOBIP_API_KEY", "k")
    monkeypatch.setenv("INFOBIP_WHATSAPP_FROM", SENDER)
    monkeypatch.setenv("INFOBIP_BASE_URL", "test.api.infobip.com/")
    assert InfobipSender().base_url == "https://test.api.infobip.com"


def test_send_template_shape(ib_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(infobip.httpx, "post", fake)
    InfobipSender().send_template(SHOP, "test_whatsapp_template_en", "en",
                                  ["Donovan"])
    assert fake.calls[0]["url"].endswith("/whatsapp/1/message/template")
    msg = fake.calls[0]["json"]["messages"][0]
    assert (msg["from"], msg["to"]) == (SENDER, SHOP)
    assert msg["messageId"]
    assert msg["content"]["templateName"] == "test_whatsapp_template_en"
    assert msg["content"]["language"] == "en"
    assert msg["content"]["templateData"]["body"]["placeholders"] == ["Donovan"]


def test_send_image_and_file_shapes(ib_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(infobip.httpx, "post", fake)
    InfobipSender().send_image(SHOP, "https://app/media/a.png", "Card")
    assert fake.calls[0]["url"].endswith("/whatsapp/1/message/image")
    assert fake.calls[0]["json"]["content"] == {"mediaUrl": "https://app/media/a.png",
                                                "caption": "Card"}
    InfobipSender().send_file(SHOP, "https://app/media/a.pdf", "a.pdf", "Doc")
    assert fake.calls[1]["url"].endswith("/whatsapp/1/message/document")
    assert fake.calls[1]["json"]["content"] == {
        "mediaUrl": "https://app/media/a.pdf", "caption": "Doc",
        "filename": "a.pdf"}


def test_send_buttons_and_list_shapes(ib_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(infobip.httpx, "post", fake)
    InfobipSender().send_buttons(SHOP, "Save?", ["Yes", "No", "*Maybe*",
                                                 "Extra"])
    node = fake.calls[0]["json"]["content"]
    assert fake.calls[0]["url"].endswith("/whatsapp/1/message/interactive/buttons")
    assert node["body"] == {"text": "Save?"}
    assert node["action"]["buttons"] == [
        {"type": "REPLY", "id": "Yes", "title": "Yes"},
        {"type": "REPLY", "id": "No", "title": "No"},
        {"type": "REPLY", "id": "Maybe", "title": "Maybe"}]
    rows = [f"row {i}" for i in range(12)]
    InfobipSender().send_list(SHOP, "Pick", "Choose", rows)
    node = fake.calls[1]["json"]["content"]
    assert fake.calls[1]["url"].endswith("/whatsapp/1/message/interactive/list")
    assert node["action"]["title"] == "Choose"
    got = node["action"]["sections"][0]["rows"]
    assert [r["title"] for r in got] == [f"row {i}" for i in range(10)]
    assert all(set(r) == {"id", "title"} for r in got)


def test_sender_429_raises_rate_limited(ib_env, monkeypatch):
    err = _http_error(429)
    err.response.headers["retry-after"] = "7"
    fake = _FakePost(script=[err])
    monkeypatch.setattr(infobip.httpx, "post", fake)
    with pytest.raises(RateLimited) as ei:
        InfobipSender().send_text(SHOP, "hi")
    assert ei.value.retry_after == 7.0


def test_buttons_fall_back_to_numbered_on_400(ib_env, monkeypatch):
    fake = _FakePost(script=[_http_error(400), _ok()])
    monkeypatch.setattr(infobip.httpx, "post", fake)
    InfobipSender().send_buttons(SHOP, "Save?", ["✅ Yes", "❌ No"])
    assert len(fake.calls) == 2
    assert fake.calls[1]["url"].endswith("/whatsapp/1/message/text")
    assert "1) ✅ Yes" in fake.calls[1]["json"]["content"]["text"]


def test_missing_config_names_the_var(monkeypatch):
    monkeypatch.delenv("INFOBIP_API_KEY", raising=False)
    monkeypatch.setenv("INFOBIP_BASE_URL", "https://x.api.infobip.com")
    monkeypatch.setenv("INFOBIP_WHATSAPP_FROM", SENDER)
    with pytest.raises(RuntimeError, match="INFOBIP_API_KEY"):
        InfobipSender().send_text(SHOP, "hi")
    monkeypatch.setenv("INFOBIP_API_KEY", "k")
    monkeypatch.delenv("INFOBIP_BASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="INFOBIP_BASE_URL"):
        InfobipSender().send_text(SHOP, "hi")
    monkeypatch.setenv("INFOBIP_BASE_URL", "https://x.api.infobip.com")
    monkeypatch.delenv("INFOBIP_WHATSAPP_FROM", raising=False)
    with pytest.raises(RuntimeError, match="INFOBIP_WHATSAPP_FROM"):
        InfobipSender().send_text(SHOP, "hi")


def test_failure_logs_status_without_key(ib_env, monkeypatch, caplog):
    fake = _FakePost(script=[_http_error(400)])
    monkeypatch.setattr(infobip.httpx, "post", fake)
    with pytest.raises(httpx.HTTPStatusError):
        InfobipSender().send_text(SHOP, "hi")
    assert "ib-key" not in caplog.text
    assert "400" in caplog.text


def test_mark_read_shape_and_best_effort(ib_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(infobip.httpx, "post", fake)
    assert InfobipSender().mark_read("mid-9") is True
    assert fake.calls[0]["url"] == (
        f"https://test.api.infobip.com/whatsapp/1/senders/{SENDER}"
        "/message/mid-9/read")
    fake2 = _FakePost(script=[_http_error(500)])
    monkeypatch.setattr(infobip.httpx, "post", fake2)
    assert InfobipSender().mark_read("mid-9") is False
    monkeypatch.delenv("INFOBIP_API_KEY")
    assert InfobipSender().mark_read("mid-9") is False
    assert InfobipSender().mark_read("") is False


# --- media download --------------------------------------------------------

def test_download_media_uses_key_and_validates(ib_env, monkeypatch):
    seen = {}

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        @property
        def content(self):
            return b"bytes"

    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        seen.update(url=url, headers=headers)
        return Resp()

    monkeypatch.setattr(infobip.httpx, "get", fake_get)
    assert download_infobip_media("https://test.api.infobip.com/m/1") == b"bytes"
    assert seen["headers"]["Authorization"] == "App ib-key"
    with pytest.raises(VoiceError):
        download_infobip_media("http://plain/x")
    with pytest.raises(VoiceError):
        download_infobip_media("")


def test_download_media_failure_is_voice_error(ib_env, monkeypatch):
    def boom(url, headers=None, timeout=None, follow_redirects=None):
        raise _http_error(404)

    monkeypatch.setattr(infobip.httpx, "get", boom)
    with pytest.raises(VoiceError):
        download_infobip_media("https://test.api.infobip.com/m/1")


# --- endpoints --------------------------------------------------------------

def test_webhook_text_end_to_end_and_duplicate(ib_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(infobip.httpx, "post", fake)
    client = TestClient(app)
    body = _payload(_item({"type": "TEXT", "text": "resend"}, mid="ib-dedupe-1"))
    r = client.post(f"/webhook/infobip/{TOKEN}", json=body)
    assert r.status_code == 200
    texts = [c for c in fake.calls if c["url"].endswith("/message/text")]
    assert len(texts) == 1  # queued reply drained once
    r = client.post(f"/webhook/infobip/{TOKEN}", json=body)
    assert r.status_code == 200
    texts = [c for c in fake.calls if c["url"].endswith("/message/text")]
    assert len(texts) == 1  # duplicate messageId ignored


def test_webhook_auth_and_limits(ib_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(infobip.httpx, "post", fake)
    client = TestClient(app)
    body = _payload(_item({"type": "TEXT", "text": "hi"}))
    assert client.post("/webhook/infobip/wrong", json=body).status_code == 403
    big = "x" * (main.MAX_WEBHOOK_BYTES + 1)
    r = client.post(f"/webhook/infobip/{TOKEN}", content=big,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    r = client.post(f"/webhook/infobip/{TOKEN}", content=b"not json",
                    headers={"Content-Type": "application/json"})
    assert (r.status_code, r.json()) == (200, {"status": "ignored"})


def test_webhook_tolerates_pasted_whitespace(ib_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(infobip.httpx, "post", fake)
    monkeypatch.setenv("INFOBIP_WEBHOOK_TOKEN", f"  {TOKEN}\n")
    client = TestClient(app)
    body = _payload(_item({"type": "TEXT", "text": "resend"}, mid="ib-ws-1"))
    r = client.post(f"/webhook/infobip/{TOKEN}", json=body)
    assert r.status_code == 200
    texts = [c for c in fake.calls if c["url"].endswith("/message/text")]
    assert len(texts) == 1


def test_webhook_403_logs_lengths_not_values(ib_env, caplog):
    client = TestClient(app)
    body = _payload(_item({"type": "TEXT", "text": "hi"}))
    r = client.post("/webhook/infobip/wrong", json=body)
    assert r.status_code == 403
    assert "secret set len=12 got len=5" in caplog.text
    assert TOKEN not in caplog.text
    assert "ib-key" not in caplog.text


def test_webhook_fails_closed_without_token(ib_env, monkeypatch):
    monkeypatch.delenv("INFOBIP_WEBHOOK_TOKEN")
    client = TestClient(app)
    body = _payload(_item({"type": "TEXT", "text": "hi"}))
    assert client.post(f"/webhook/infobip/{TOKEN}", json=body).status_code == 403


def test_dlr_always_200(ib_env):
    client = TestClient(app)
    dlr = {"results": [{"messageId": "ib-out-1", "to": SHOP,
                        "status": {"groupName": "DELIVERED",
                                   "name": "DELIVERED_TO_HANDSET"}}],
           "messageCount": 1}
    assert client.post("/webhook/infobip/dlr", json=dlr).status_code == 200
    assert client.post("/webhook/infobip/dlr", json={"bogus": 1}
                       ).status_code == 200
    r = client.post("/webhook/infobip/dlr", content=b"zzz",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 200


# --- provider selection ------------------------------------------------------

def test_unset_provider_keeps_per_channel_routing(ib_env, monkeypatch):
    import app.vonage as _v
    from app.whatsapp import FakeSender
    monkeypatch.delenv("SISI_PROVIDER", raising=False)
    assert isinstance(sender_for_channel("vonage"), _v.VonageSender)
    assert isinstance(sender_for_channel("infobip"), InfobipSender)
    assert isinstance(sender_for_channel("meta"), FakeSender)
    assert isinstance(sender_for_channel(""), FakeSender)


def test_locked_provider_refuses_other_channels(ib_env, monkeypatch):
    monkeypatch.setenv("SISI_PROVIDER", "infobip")
    assert isinstance(sender_for_channel("infobip"), InfobipSender)
    with pytest.raises(WrongProviderError):
        sender_for_channel("vonage")
    with pytest.raises(WrongProviderError):
        sender_for_channel("meta")
    monkeypatch.setenv("SISI_PROVIDER", "bogus")
    with pytest.raises(WrongProviderError):
        sender_for_channel("infobip")
    with pytest.raises(ValueError):
        sender_for_channel("smoke-signal")


def test_queued_sender_tags_infobip_rows(ib_env, monkeypatch):
    import app.db as _db
    from app.sendqueue import QueuedSender
    monkeypatch.delenv("SISI_PROVIDER", raising=False)
    conn = _db.connect()
    try:
        out = QueuedSender.wrap(InfobipSender(), conn, "infobip")
        out.send_text(SHOP, "hello")
        row = conn.execute(
            "SELECT channel, kind FROM send_queue WHERE to_number = ?",
            (SHOP,)).fetchone()
        assert (row["channel"], row["kind"]) == ("infobip", "text")
        out2 = QueuedSender.wrap(InfobipSender(), conn, "vonage")
        out2.send_text(SHOP, "hi")
        rows = conn.execute(
            "SELECT DISTINCT channel FROM send_queue WHERE to_number = ?",
            (SHOP,)).fetchall()
        assert sorted(r["channel"] for r in rows) == ["infobip", "vonage"]
    finally:
        conn.close()


def test_send_immediate_infobip_drains(ib_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(infobip.httpx, "post", fake)
    monkeypatch.delenv("SISI_PROVIDER", raising=False)
    from app.sendqueue import send_immediate
    stats = send_immediate("infobip", SHOP, "text", {"body": "ping"})
    assert stats["sent"] == 1
    assert fake.calls[0]["url"].endswith("/whatsapp/1/message/text")
    monkeypatch.setenv("SISI_PROVIDER", "vonage")
    with pytest.raises(WrongProviderError):
        send_immediate("infobip", SHOP, "text", {"body": "ping"})


def test_drain_all_skips_locked_channel(ib_env, monkeypatch):
    import app.db as _db
    from app.sendqueue import drain_all, enqueue
    fake = _FakePost()
    monkeypatch.setattr(infobip.httpx, "post", fake)
    monkeypatch.setenv("SISI_PROVIDER", "infobip")
    conn = _db.connect()
    try:
        enqueue(conn, SHOP, "vonage", "text", {"body": "stale"})
        enqueue(conn, "27700000002", "infobip", "text", {"body": "fresh"})
        totals = drain_all(conn, sender_for_channel)
        assert totals["sent"] == 1
        left = conn.execute(
            "SELECT to_number FROM send_queue WHERE status = 'queued'"
            ).fetchall()
        assert [r["to_number"] for r in left] == [SHOP]
    finally:
        conn.close()


def test_resend_uses_queued_path_on_infobip(ib_env, monkeypatch):
    import json as _json
    import app.db as _db
    import app.tools as tools
    from app.agent import MEDIA_OUTBOX
    conn = _db.connect()
    try:
        conn.execute("UPDATE shops SET channel = 'infobip' WHERE shop_id = ?",
                     (SHOP,))
        conn.execute(
            "INSERT INTO flags (shop_id, key, value) VALUES (?, ?, ?)",
            (SHOP, "last-failed-media", _json.dumps(
                {"kind": "image", "channel": "infobip",
                 "url": "https://app/media/a.png", "caption": "Card"})))
        conn.commit()
        monkeypatch.delenv("SISI_SPAWN", raising=False)
        assert "Sending it again" in tools.resend_last_media(conn, SHOP)
        row = conn.execute(
            "SELECT channel, kind FROM send_queue WHERE to_number = ?",
            (SHOP,)).fetchone()
        assert (row["channel"], row["kind"]) == ("infobip", "image")
        assert MEDIA_OUTBOX.get(SHOP, []) == []
    finally:
        conn.close()
