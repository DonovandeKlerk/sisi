"""Webhook, senders, and a simulated end-to-end conversation. Model stubbed, no network."""

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.llm as llm
import app.whatsapp as wa
from app.main import app
from app.whatsapp import (
    CloudApiSender,
    FakeSender,
    get_sender,
    parse_webhook,
    reset_fake,
    verify_signature,
)
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID
SECRET = "test-app-secret"


@pytest.fixture()
def sim_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SISI_DB", str(tmp_path / "sim.db"))
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.setenv("VERIFY_TOKEN", "test-verify")
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    monkeypatch.delenv("WHATSAPP_TOKEN", raising=False)
    monkeypatch.setattr(llm, "TOKEN_LOG", tmp_path / "usage.log")
    reset_fake()
    seed_demo(SHOP)
    return tmp_path


def _signed(body: bytes) -> dict:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}"}


def _wa_text(shop: str, text: str, mid: str = "wamid.1") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "e", "changes": [{
            "field": "messages",
            "value": {"messages": [{
                "id": mid, "from": shop, "timestamp": "1761500000",
                "type": "text", "text": {"body": text}}]}}]}],
    }


def _post_text(client: TestClient, text: str, mid: str = "wamid.1"):
    raw = json.dumps(_wa_text(SHOP, text, mid)).encode()
    return client.post("/webhook", content=raw, headers=_signed(raw))


# --- signatures ------------------------------------------------------------


def test_verify_signature():
    body = b'{"hello":"world"}'
    good = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(body, good, SECRET) is True
    assert verify_signature(body, good, "wrong-secret") is False
    assert verify_signature(body, "sha256=deadbeef", SECRET) is False
    assert verify_signature(body, None, SECRET) is False
    assert verify_signature(body, good, "") is False


def test_get_verify(sim_env):
    client = TestClient(app)
    ok = client.get("/webhook", params={"hub.mode": "subscribe",
                                        "hub.verify_token": "test-verify",
                                        "hub.challenge": "chall-123"})
    assert ok.status_code == 200 and ok.text == "chall-123"
    bad = client.get("/webhook", params={"hub.mode": "subscribe",
                                         "hub.verify_token": "nope",
                                         "hub.challenge": "chall-123"})
    assert bad.status_code == 403


def test_post_rejects_bad_signature(sim_env):
    client = TestClient(app)
    raw = json.dumps(_wa_text(SHOP, "hi")).encode()
    assert client.post("/webhook", content=raw).status_code == 403
    forged = client.post("/webhook", content=raw,
                         headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert forged.status_code == 403


# --- payload parsing -------------------------------------------------------


def test_parse_text_button_status():
    msgs = parse_webhook(_wa_text(SHOP, "hello", "m1"))
    assert len(msgs) == 1 and msgs[0].text == "hello" and msgs[0].kind == "text"

    btn = {"object": "whatsapp_business_account", "entry": [{"changes": [{
        "value": {"messages": [{"id": "m2", "from": SHOP, "type": "interactive",
                                "interactive": {"button_reply": {"id": "Yes",
                                                                "title": "Yes"}}}]}}]}]}
    msgs = parse_webhook(btn)
    assert len(msgs) == 1 and msgs[0].text == "Yes" and msgs[0].kind == "button"

    status = {"object": "whatsapp_business_account", "entry": [{"changes": [{
        "value": {"statuses": [{"id": "m1", "status": "delivered"}]}}]}]}
    assert parse_webhook(status) == []
    assert parse_webhook({"object": "other"}) == []
    assert parse_webhook({}) == []


def test_parse_unsupported_type_yields_graceful_none():
    img = {"object": "whatsapp_business_account", "entry": [{"changes": [{
        "value": {"messages": [{"id": "m9", "from": SHOP, "type": "image",
                                "image": {"id": "media-1"}}]}}]}]}
    msgs = parse_webhook(img)
    assert len(msgs) == 1 and msgs[0].text is None and msgs[0].kind == "media:image"


# --- senders ---------------------------------------------------------------


def test_fake_sender_is_default_and_records():
    sender = get_sender()
    assert isinstance(sender, FakeSender)
    sender.send_text(SHOP, "hi")
    sender.send_buttons(SHOP, "save?", ["Yes", "No"])
    mine = sender.pop_shop(SHOP)
    assert [m["type"] for m in mine] == ["text", "buttons"]
    assert mine[1]["buttons"] == ["Yes", "No"]
    assert sender.pop_shop(SHOP) == []


def test_factory_selects_cloud_only_when_both_vars_set(monkeypatch):
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    monkeypatch.delenv("WHATSAPP_TOKEN", raising=False)
    assert isinstance(get_sender(), FakeSender)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "pnid-1")
    assert isinstance(get_sender(), FakeSender)  # token still missing -> fake, no crash
    monkeypatch.setenv("WHATSAPP_TOKEN", "tok")
    sender = get_sender()
    assert isinstance(sender, CloudApiSender)
    assert sender.url == "https://graph.facebook.com/v26.0/pnid-1/messages"


def test_cloud_sender_request_shapes(monkeypatch):
    seen = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"messages": [{"id": "wamid.out"}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers
        seen["json"] = json
        return FakeResp()

    monkeypatch.setattr(wa.httpx, "post", fake_post)
    sender = CloudApiSender(token="tok", phone_number_id="pnid-1")
    sender.send_text("2771", "hello")
    assert seen["url"].endswith("/pnid-1/messages")
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["json"]["type"] == "text"

    long_btn = "x" * 30
    sender.send_buttons("2771", "save?", ["Yes", "No", "Maybe", "Extra", long_btn])
    action = seen["json"]["interactive"]["action"]["buttons"]
    assert len(action) == 3  # capped at 3
    assert all(len(b["reply"]["title"]) <= 20 for b in action)


# --- end-to-end simulated conversation (model stubbed) ---------------------


def _call(call_id, name, args):
    return SimpleNamespace(id=call_id,
                           function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _resp(content=None, calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content,
                                                         tool_calls=calls or []))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2))


def _scripted_model(monkeypatch):
    def fake(messages):
        if any(isinstance(m, dict) and m.get("role") == "tool" for m in messages):
            tool = next(m["content"] for m in reversed(messages)
                        if isinstance(m, dict) and m.get("role") == "tool")
            return _resp(f"OK: {tool}")
        user_text = next(m["content"] for m in reversed(messages)
                         if isinstance(m, dict) and m.get("role") == "user")
        if "took" in user_text:
            return _resp(calls=[_call("c1", "add_credit", {
                "customer": "Sipho",
                "items": [{"product": "Sasko White Bread 700g", "qty": 2},
                          {"product": "Coke 2L", "qty": 1}]})])
        if "owe" in user_text:
            return _resp(calls=[_call("c2", "get_balance", {"customer": "Sipho"})])
        return _resp("huh?")

    monkeypatch.setattr(llm, "_complete", fake)


def test_simulated_credit_conversation(sim_env, monkeypatch):
    _scripted_model(monkeypatch)
    client = TestClient(app)

    r1 = _post_text(client, "Sipho took two bread and a Coke", "wamid.1")
    assert r1.status_code == 200 and r1.json() == {"status": "ok"}
    out1 = client.get("/_sim/outbox", params={"shop": SHOP}).json()["messages"]
    assert len(out1) == 1 and out1[0]["type"] == "buttons"
    assert out1[0]["buttons"] == ["Yes", "No"]
    assert "R73.00" in out1[0]["body"]

    r2 = _post_text(client, "Yes", "wamid.2")
    assert r2.status_code == 200
    out2 = client.get("/_sim/outbox", params={"shop": SHOP}).json()["messages"]
    assert "R146.00" in out2[0]["body"]

    r3 = _post_text(client, "How much does Sipho owe?", "wamid.3")
    assert r3.status_code == 200
    out3 = client.get("/_sim/outbox", params={"shop": SHOP}).json()["messages"]
    assert "R146.00" in out3[0]["body"]


def test_webhook_dedupes_message_id(sim_env, monkeypatch):
    _scripted_model(monkeypatch)
    client = TestClient(app)
    assert _post_text(client, "Sipho took two bread and a Coke", "wamid.9").status_code == 200
    assert _post_text(client, "Sipho took two bread and a Coke", "wamid.9").status_code == 200
    out = client.get("/_sim/outbox", params={"shop": SHOP}).json()["messages"]
    assert len(out) == 1  # retry delivered once


def test_unsupported_type_gets_graceful_reply(sim_env):
    client = TestClient(app)
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{
        "value": {"messages": [{"id": "wamid.7", "from": SHOP, "type": "audio",
                                "audio": {"id": "media-1"}}]}}]}]}
    raw = json.dumps(payload).encode()
    assert client.post("/webhook", content=raw, headers=_signed(raw)).status_code == 200
    out = client.get("/_sim/outbox", params={"shop": SHOP}).json()["messages"]
    assert len(out) == 1 and "text" in out[0]["body"]
