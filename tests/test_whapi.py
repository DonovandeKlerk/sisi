"""Whapi.Cloud channel: parsing, guards, sender, media, status, endpoints.

HTTP mocked. Fixture secrets are fake ("wh-key"); nothing here touches
live credentials. Payload shapes follow the official Whapi.Cloud
help-desk docs (verified 2026-10-30; see app/whapi.py docstring).
"""

import time

import httpx
import pytest
from fastapi.testclient import TestClient

import app.llm as llm
import app.main as main
import app.whapi as whapi
from app.main import app
from app.providers import WrongProviderError, sender_for_channel
from app.reminders import SAST, send_due, window_open
from app.sendqueue import RateLimited
from app.voice import VoiceError
from app.whapi import (
    WhapiSender,
    download_whapi_media,
    parse_whapi_status,
    parse_whapi_webhook,
)
from app.whatsapp import reset_fake
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID  # tester chat number (allowlisted below)
STRANGER = "27719998888"
TOKEN = "wh-hook-1"
CHANNEL = "DEADPL-S9KV8"
OWN = "27710000002"
NOW = 1790000000.0


@pytest.fixture()
def wh_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SISI_DB", str(tmp_path / "wh.db"))
    monkeypatch.setenv("WHAPI_TOKEN", "wh-key")
    monkeypatch.setenv("WHAPI_BASE_URL", "https://gate.whapi.test")
    monkeypatch.setenv("WHAPI_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("WHAPI_NUMBER", OWN)
    monkeypatch.setenv("WHAPI_WEBHOOK_TOKEN", TOKEN)
    monkeypatch.setenv("WHAPI_ALLOWLIST", SHOP)
    for var in ("WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_TOKEN",
                "VONAGE_API_KEY", "VONAGE_API_SECRET", "SISI_PROVIDER",
                "INFOBIP_API_KEY", "WHAPI_MAX_AGE_S", "WHAPI_MAX_MEDIA_BYTES"):
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


def _msg(mid="w-mid-1", frm=SHOP, mtype="text", node=None, **kw):
    m = {"id": mid, "from_me": False, "type": mtype,
         "chat_id": f"{frm}@s.whatsapp.net", "timestamp": int(NOW),
         "source": "mobile", "from": frm, "from_name": "Tester"}
    if node is not None:
        m[mtype if mtype != "reply" else "reply"] = node
    m.update(kw)
    return m


def _payload(*msgs, channel=CHANNEL, event="messages"):
    return {"messages": list(msgs),
            "event": {"type": event, "event": "post"},
            "channel_id": channel}


def _status_payload(*statuses, channel=CHANNEL):
    return {"statuses": list(statuses),
            "event": {"type": "statuses", "event": "post"},
            "channel_id": channel}


# --- parsing: happy paths --------------------------------------------------

def test_parse_text_image_voice_reply(wh_env):
    msgs = parse_whapi_webhook(
        _payload(_msg(node={"body": "hi sisi"})), now=NOW)
    assert len(msgs) == 1
    assert (msgs[0].shop_id, msgs[0].kind, msgs[0].text,
            msgs[0].channel) == (SHOP, "text", "hi sisi", "whapi")

    msgs = parse_whapi_webhook(_payload(_msg(
        mtype="image", mid="w-img-1",
        node={"id": "jpeg-abc", "mime_type": "image/jpeg",
              "caption": "shelf photo"})), now=NOW)
    assert len(msgs) == 1
    assert (msgs[0].kind, msgs[0].media_id, msgs[0].caption) == (
        "media:image", "jpeg-abc", "shelf photo")

    msgs = parse_whapi_webhook(_payload(_msg(
        mtype="voice", mid="w-v-1",
        node={"id": "oga-abc", "mime_type": "audio/ogg; codecs=opus"})), now=NOW)
    assert len(msgs) == 1
    assert (msgs[0].kind, msgs[0].media_id) == ("media:audio", "oga-abc")

    msgs = parse_whapi_webhook(_payload(_msg(
        mtype="reply", mid="w-r-1",
        node={"type": "buttons_reply",
              "buttons_reply": {"id": "ButtonsV3:Yes", "title": "Yes"}})),
        now=NOW)
    assert len(msgs) == 1
    assert (msgs[0].kind, msgs[0].text) == ("button", "Yes")


def test_parse_autodownload_link_kept_as_fallback(wh_env):
    msgs = parse_whapi_webhook(_payload(_msg(
        mtype="image", mid="w-img-2",
        node={"id": "jpeg-def", "mime_type": "image/jpeg",
              "link": "https://files.test/in.jpeg"})), now=NOW)
    assert len(msgs) == 1
    assert (msgs[0].media_id, msgs[0].media_url) == (
        "jpeg-def", "https://files.test/in.jpeg")


def test_parse_document_video_get_unsupported_kind(wh_env):
    msgs = parse_whapi_webhook(_payload(_msg(
        mtype="document", mid="w-d-1",
        node={"id": "pdf-1", "file_name": "slip.pdf"})), now=NOW)
    assert len(msgs) == 1
    assert (msgs[0].kind, msgs[0].text) == ("media:document", None)
    msgs = parse_whapi_webhook(_payload(
        _msg(mtype="video", mid="w-vd-1", node={"id": "mp4-1"})), now=NOW)
    assert (msgs[0].kind, msgs[0].text) == ("media:video", None)


# --- parsing: loop / privacy guards ----------------------------------------

def test_parse_drops_own_echoes(wh_env):
    api_echo = _msg(mid="w-echo-1", node={"body": "our own reply"})
    api_echo.update(from_me=True, source="api")
    assert parse_whapi_webhook(_payload(api_echo), now=NOW) == []
    mobile_echo = _msg(mid="w-echo-2", node={"body": "typed on phone"})
    mobile_echo.update(from_me=True, source="mobile")
    assert parse_whapi_webhook(_payload(mobile_echo), now=NOW) == []


def test_parse_drops_groups_broadcasts_and_status(wh_env):
    group = _msg(mid="w-g-1", node={"body": "group chat"})
    group["chat_id"] = "120363424979900095@g.us"
    assert parse_whapi_webhook(_payload(group), now=NOW) == []
    broadcast = _msg(mid="w-b-1", node={"body": "broadcast"})
    broadcast["chat_id"] = f"{SHOP}@broadcast"
    assert parse_whapi_webhook(_payload(broadcast), now=NOW) == []
    status = _msg(mid="w-s-1", node={"body": "status reply"})
    status["chat_id"] = "status@broadcast"
    assert parse_whapi_webhook(_payload(status), now=NOW) == []


def test_parse_drops_linked_number_itself(wh_env, monkeypatch):
    monkeypatch.setenv("WHAPI_ALLOWLIST", f"{SHOP},{OWN}")
    self_msg = _msg(mid="w-self-1", frm=OWN, node={"body": "note to self"})
    assert parse_whapi_webhook(_payload(self_msg), now=NOW) == []


def test_parse_allowlist_required_and_enforced(wh_env, monkeypatch):
    stranger = _msg(mid="w-x-1", frm=STRANGER, node={"body": "hi"})
    assert parse_whapi_webhook(_payload(stranger), now=NOW) == []
    monkeypatch.setenv("WHAPI_ALLOWLIST", "")
    mine = _msg(mid="w-x-2", node={"body": "hi"})
    assert parse_whapi_webhook(_payload(mine), now=NOW) == []  # fail closed
    monkeypatch.delenv("WHAPI_ALLOWLIST")
    assert parse_whapi_webhook(_payload(mine), now=NOW) == []


def test_parse_wrong_channel_dropped(wh_env):
    bad = _payload(_msg(node={"body": "hi"}), channel="OTHER-CH")
    assert parse_whapi_webhook(bad, now=NOW) == []


def test_parse_stale_history_dropped(wh_env, monkeypatch):
    old = _msg(mid="w-old-1", node={"body": "ancient"})
    old["timestamp"] = int(NOW - 3600)
    assert parse_whapi_webhook(_payload(old), now=NOW) == []
    fresh = _msg(mid="w-new-1", node={"body": "now"})
    assert len(parse_whapi_webhook(_payload(fresh), now=NOW)) == 1
    monkeypatch.setenv("WHAPI_MAX_AGE_S", "5")
    assert parse_whapi_webhook(_payload(fresh), now=NOW + 10) == []


def test_parse_drops_noise_and_garbage(wh_env):
    for mtype in ("reaction", "sticker", "location", "contact", "poll",
                  "order", "call", "action"):
        assert parse_whapi_webhook(_payload(
            _msg(mid=f"w-n-{mtype}", mtype=mtype, node={"x": 1})), now=NOW) == []
    assert parse_whapi_webhook({}, now=NOW) == []
    assert parse_whapi_webhook({"messages": "nope"}, now=NOW) == []
    assert parse_whapi_webhook(_payload("nope"), now=NOW) == []
    assert parse_whapi_webhook(_payload(_msg(mid="")), now=NOW) == []
    # Non-message events never yield messages.
    assert parse_whapi_webhook(
        {"messages": [_msg(node={"body": "hi"})],
         "event": {"type": "chats", "event": "post"},
         "channel_id": CHANNEL}, now=NOW) == []


# --- statuses ---------------------------------------------------------------

def test_status_mapping(wh_env):
    p = _status_payload(
        {"id": "w-o-1", "status": "pending", "recipient_id": SHOP,
         "timestamp": str(int(NOW))},
        {"id": "w-o-2", "status": "sent", "recipient_id": SHOP,
         "timestamp": str(int(NOW))},
        {"id": "w-o-3", "status": "delivered", "recipient_id": SHOP,
         "timestamp": str(int(NOW))},
        {"id": "w-o-4", "status": "read", "recipient_id": SHOP,
         "timestamp": str(int(NOW))},
        {"id": "w-o-5", "status": "failed", "recipient_id": SHOP,
         "description": "bad number", "timestamp": str(int(NOW))})
    got = dict((mid, (out, det)) for mid, out, det in parse_whapi_status(p))
    assert got == {"w-o-1": ("submitted", ""), "w-o-2": ("submitted", ""),
                   "w-o-3": ("delivered", ""), "w-o-4": ("delivered", ""),
                   "w-o-5": ("failed", "bad number")}


def test_status_acceptance_is_not_delivery(wh_env):
    # Reordered/duplicate reports must not confuse acceptance with delivery:
    # the parser reports each truthfully; record_dlr resolves precedence.
    p = _status_payload({"id": "w-o-6", "status": "pending",
                         "recipient_id": SHOP, "timestamp": "1"})
    assert parse_whapi_status(p) == [("w-o-6", "submitted", "")]


def test_status_drops_wrong_channel_and_garbage(wh_env):
    p = _status_payload({"id": "w-o-7", "status": "read",
                         "recipient_id": SHOP}, channel="OTHER-CH")
    assert parse_whapi_status(p) == []
    assert parse_whapi_status({}) == []
    assert parse_whapi_status(_payload(_msg(node={"body": "hi"}))) == []
    assert parse_whapi_status(_status_payload("nope")) == []
    assert parse_whapi_status(_status_payload({"id": "", "status": ""})) == []


# --- sender -----------------------------------------------------------------

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
    text = '{"id":"w-out-1"}'

    def __init__(self, payload=None):
        self._payload = {"id": "w-out-1"} if payload is None else payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _ok(payload=None):
    return _FakeResp(payload)


def _http_error(status):
    from types import SimpleNamespace
    req = SimpleNamespace()
    resp = httpx.Response(status, request=httpx.Request("POST", "https://x"),
                          text="err")
    return httpx.HTTPStatusError(f"http {status}", request=req, response=resp)


def test_send_text_shape_auth_and_id(wh_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(whapi.httpx, "post", fake)
    mid = WhapiSender().send_text(f"+{SHOP}", "hello there")
    assert mid == "w-out-1"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == "https://gate.whapi.test/messages/text"
    assert call["headers"]["Authorization"] == "Bearer wh-key"
    assert call["json"] == {"to": SHOP, "body": "hello there"}


def test_send_buttons_shape_and_limits(wh_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(whapi.httpx, "post", fake)
    WhapiSender().send_buttons(SHOP, "Save it?",
                               ["Yes", "No thanks", "Maybe", "Fourth"])
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"].endswith("/messages/interactive")
    body = call["json"]
    assert (body["to"], body["type"]) == (SHOP, "button")
    btns = body["action"]["buttons"]
    assert len(btns) == 3  # capped at 3
    assert [b["type"] for b in btns] == ["quick_reply"] * 3
    assert all(len(b["title"]) <= 25 for b in btns)


def test_send_buttons_fallback_and_429(wh_env, monkeypatch):
    fake = _FakePost(script=[_http_error(400), _ok()])
    monkeypatch.setattr(whapi.httpx, "post", fake)
    WhapiSender().send_buttons(SHOP, "Save it?", ["Yes", "No"])
    assert len(fake.calls) == 2
    assert fake.calls[1]["url"].endswith("/messages/text")
    assert "1) Yes" in fake.calls[1]["json"]["body"]

    fake = _FakePost(script=[_http_error(429)] * 4)
    monkeypatch.setattr(whapi.httpx, "post", fake)
    monkeypatch.setattr(whapi.time, "sleep", lambda s: None)
    with pytest.raises(RateLimited):
        WhapiSender().send_buttons(SHOP, "Save it?", ["Yes"])


def test_send_list_is_numbered_text(wh_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(whapi.httpx, "post", fake)
    WhapiSender().send_list(SHOP, "Pick one", "Choose", ["Sell", "Stock"])
    assert len(fake.calls) == 1
    assert fake.calls[0]["url"].endswith("/messages/text")
    assert "1) Sell" in fake.calls[0]["json"]["body"]


def test_send_image_and_file_shapes(wh_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(whapi.httpx, "post", fake)
    WhapiSender().send_image(SHOP, "https://x.test/c.png", "Forward this 👆")
    WhapiSender().send_file(SHOP, "https://x.test/r.pdf", "report.pdf", "PDF")
    assert fake.calls[0]["url"].endswith("/messages/image")
    assert fake.calls[0]["json"] == {"to": SHOP, "media": "https://x.test/c.png",
                                     "caption": "Forward this 👆"}
    assert fake.calls[1]["url"].endswith("/messages/document")
    assert fake.calls[1]["json"] == {"to": SHOP, "media": "https://x.test/r.pdf",
                                     "filename": "report.pdf", "caption": "PDF"}


def test_sender_requires_token(wh_env, monkeypatch):
    monkeypatch.delenv("WHAPI_TOKEN")
    with pytest.raises(RuntimeError):
        WhapiSender().send_text(SHOP, "hi")


def test_mark_read_is_safe_noop(wh_env):
    assert WhapiSender().mark_read("w-mid-1") is False


# --- media download ----------------------------------------------------------

class _FakeStream:
    def __init__(self, chunks=None, status=200):
        self._chunks = chunks if chunks is not None else [b"bytes-here"]
        self._status = status

    def __call__(self, method, url, headers=None, timeout=None,
                 follow_redirects=None):
        assert method == "GET"
        assert url == "https://gate.whapi.test/media/jpeg-abc"
        assert headers == {"Authorization": "Bearer wh-key"}
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self._status != 200:
            raise _http_error(self._status)

    def iter_bytes(self, size):
        yield from self._chunks


def test_download_media_by_id(wh_env, monkeypatch):
    monkeypatch.setattr(whapi.httpx, "stream", _FakeStream())
    assert download_whapi_media("jpeg-abc") == b"bytes-here"


def test_download_media_size_cap(wh_env, monkeypatch):
    monkeypatch.setenv("WHAPI_MAX_MEDIA_BYTES", "4")
    monkeypatch.setattr(whapi.httpx, "stream", _FakeStream())
    with pytest.raises(VoiceError, match="too big"):
        download_whapi_media("jpeg-abc")


def test_download_media_link_fallback(wh_env, monkeypatch):
    def boom(*a, **k):
        raise _http_error(404)

    class _Resp:
        content = b"link-bytes"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(whapi.httpx, "stream", boom)
    monkeypatch.setattr(whapi.httpx, "get", lambda *a, **k: _Resp())
    out = download_whapi_media("jpeg-gone", "https://files.test/in.jpeg")
    assert out == b"link-bytes"


def test_download_media_failure_is_voice_error(wh_env, monkeypatch):
    def boom(*a, **k):
        raise _http_error(404)

    monkeypatch.setattr(whapi.httpx, "stream", boom)
    with pytest.raises(VoiceError):
        download_whapi_media("jpeg-gone")
    monkeypatch.delenv("WHAPI_TOKEN")
    with pytest.raises(VoiceError):
        download_whapi_media("jpeg-abc")


def test_download_routing_never_hits_meta(wh_env, monkeypatch):
    seen = {}

    def fake_whapi(media_id, link=None):
        seen["id"] = media_id
        return b"wh-bytes"

    def boom_meta(*a, **k):
        raise AssertionError("must not call Meta download_media")

    monkeypatch.setattr("app.whapi.download_whapi_media", fake_whapi)
    monkeypatch.setattr(main, "download_media", boom_meta)
    from app.whatsapp import IncomingMessage
    msg = IncomingMessage("w-r-1", SHOP, "media:image", None, "", channel="whapi",
                          media_id="jpeg-abc",
                          media_url="https://files.test/in.jpeg")
    assert main._download_msg_media(msg, "photo") == b"wh-bytes"
    assert seen["id"] == "jpeg-abc"
    bare = IncomingMessage("w-r-2", SHOP, "text", "hi", "", channel="whapi")
    assert main._download_msg_media(bare, "text") is None


# --- endpoints ---------------------------------------------------------------

def test_webhook_text_end_to_end_and_duplicate(wh_env, monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr(whapi.httpx, "post", fake)
    client = TestClient(app)
    body = _payload(_msg(mid="w-dedupe-1", node={"body": "resend"}))
    # Fresh timestamps: the history guard uses real clock on the route.
    body["messages"][0]["timestamp"] = int(time.time())
    r = client.post(f"/webhook/whapi/{TOKEN}", json=body)
    assert r.status_code == 200
    texts = [c for c in fake.calls if c["url"].endswith("/messages/text")]
    assert len(texts) == 1  # queued reply drained once
    r = client.post(f"/webhook/whapi/{TOKEN}", json=body)
    assert r.status_code == 200
    texts = [c for c in fake.calls if c["url"].endswith("/messages/text")]
    assert len(texts) == 1  # duplicate message id ignored


def test_webhook_status_applies_dlrs(wh_env, monkeypatch):
    applied = []
    monkeypatch.setattr(main, "_apply_dlrs", applied.extend)
    monkeypatch.setattr(main, "_dispatch",
                        lambda *a: applied.append(("dispatch",)))
    client = TestClient(app)
    body = _status_payload({"id": "w-o-9", "status": "read",
                            "recipient_id": f"{SHOP}@s.whatsapp.net",
                            "timestamp": str(int(NOW))})
    r = client.post(f"/webhook/whapi/{TOKEN}", json=body)
    assert r.status_code == 200
    assert ("w-o-9", "delivered", "") in applied
    assert ("dispatch",) not in applied  # statuses never dispatch replies


def test_webhook_drops_unguarded_traffic(wh_env, monkeypatch):
    dispatched = []
    monkeypatch.setattr(main, "_dispatch",
                        lambda bg, m: dispatched.append(m.message_id))
    client = TestClient(app)
    stranger = _payload(_msg(mid="w-str-1", frm=STRANGER, node={"body": "hi"}))
    stranger["messages"][0]["timestamp"] = int(time.time())
    assert client.post(f"/webhook/whapi/{TOKEN}", json=stranger).status_code == 200
    echo = _payload(_msg(mid="w-ech-9", node={"body": "echo"}))
    echo["messages"][0].update(from_me=True, source="api",
                               timestamp=int(time.time()))
    assert client.post(f"/webhook/whapi/{TOKEN}", json=echo).status_code == 200
    assert dispatched == []


def test_webhook_auth_and_limits(wh_env):
    client = TestClient(app)
    body = _payload(_msg(node={"body": "hi"}))
    assert client.post("/webhook/whapi/wrong", json=body).status_code == 403
    big = "x" * (main.MAX_WEBHOOK_BYTES + 1)
    r = client.post(f"/webhook/whapi/{TOKEN}", content=big,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    r = client.post(f"/webhook/whapi/{TOKEN}", content=b"not json",
                    headers={"Content-Type": "application/json"})
    assert (r.status_code, r.json()) == (200, {"status": "ignored"})


def test_webhook_403_logs_lengths_not_values(wh_env, caplog):
    client = TestClient(app)
    body = _payload(_msg(node={"body": "hi"}))
    r = client.post("/webhook/whapi/wrong", json=body)
    assert r.status_code == 403
    assert "secret set len=9 got len=5" in caplog.text
    assert TOKEN not in caplog.text
    assert "wh-key" not in caplog.text


def test_webhook_fails_closed_without_token(wh_env, monkeypatch):
    monkeypatch.delenv("WHAPI_WEBHOOK_TOKEN")
    client = TestClient(app)
    body = _payload(_msg(node={"body": "hi"}))
    assert client.post(f"/webhook/whapi/{TOKEN}", json=body).status_code == 403


# --- provider selection + reminders ------------------------------------------

def test_provider_lock_covers_whapi(wh_env, monkeypatch):
    assert isinstance(sender_for_channel("whapi"), WhapiSender)
    monkeypatch.setenv("SISI_PROVIDER", "infobip")
    with pytest.raises(WrongProviderError):
        sender_for_channel("whapi")
    monkeypatch.setenv("SISI_PROVIDER", "whapi")
    with pytest.raises(WrongProviderError):
        sender_for_channel("infobip")
    with pytest.raises(WrongProviderError):
        sender_for_channel("meta")


def test_selected_provider_accepts_whapi(wh_env, monkeypatch):
    from app.providers import selected_provider
    monkeypatch.setenv("SISI_PROVIDER", "whapi")
    assert selected_provider() == "whapi"


def test_whapi_channel_is_window_exempt(wh_env):
    from datetime import datetime, timedelta
    from app.db import connect, init_db
    from app.reminders import save_reminder
    from app.tools import ensure_shop
    conn = connect()
    try:
        init_db(conn)
        ensure_shop(conn, SHOP)
        conn.execute("UPDATE shops SET channel = 'whapi', language = 'en'"
                     " WHERE shop_id = ?", (SHOP,))
        now = datetime(2026, 10, 29, 9, 0, tzinfo=SAST)
        rid = save_reminder(conn, SHOP, now - timedelta(minutes=1),
                            "test ping", None, "none", record_action=False)
        assert window_open(conn, SHOP, "whapi", now) is True
        assert send_due(conn, now) == [rid]  # fires, never window-blocked
    finally:
        conn.close()
