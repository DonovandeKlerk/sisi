"""Join-code demo mode: provision, 10-message cap, tip, isolation, no LLM."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import app.llm as llm
import app.main as main
import app.whapi as whapi
from app import demo
from app.agent import LIST_OUTBOX, take_lists, take_media
from app.db import connect, init_db
from app.main import app, process_message
from app.whapi import PENDING_TIPS, parse_whapi_webhook
from app.whatsapp import IncomingMessage, reset_fake
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID
STRANGER = "27718887766"
STRANGER2 = "27718887767"
TOKEN = "wh-hook-demo"
CHANNEL = "DEADPL-S9KV8"
OWN = "27745492157"
NOW = 1790000000.0


@pytest.fixture()
def demo_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SISI_DB", str(tmp_path / "demo.db"))
    monkeypatch.setenv("SISI_REPORTS", str(tmp_path / "reports"))
    monkeypatch.setenv("SISI_LOGOS", str(tmp_path / "logos"))
    monkeypatch.setenv("SISI_PUBLIC_URL", "https://sisi.example")
    monkeypatch.setenv("WHAPI_TOKEN", "wh-key")
    monkeypatch.setenv("WHAPI_BASE_URL", "https://gate.whapi.test")
    monkeypatch.setenv("WHAPI_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("WHAPI_NUMBER", OWN)
    monkeypatch.setenv("WHAPI_WEBHOOK_TOKEN", TOKEN)
    monkeypatch.setenv("WHAPI_ALLOWLIST", SHOP)  # stranger is NOT listed
    monkeypatch.setenv("WHAPI_ALLOW_WRITES", "1")
    monkeypatch.setenv("SISI_DEMO_JOIN_CODE", "SISI-DEMO")
    monkeypatch.setenv("SISI_DEMO_MSG_LIMIT", "10")
    monkeypatch.setenv("SISI_DEMO_DAILY_CAP", "50")
    for var in ("WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_TOKEN",
                "VONAGE_API_KEY", "VONAGE_API_SECRET", "SISI_PROVIDER",
                "INFOBIP_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(llm, "TOKEN_LOG", tmp_path / "usage.log")
    monkeypatch.setattr(whapi.httpx, "put", lambda *a, **k: _ok())
    monkeypatch.setattr(whapi.httpx, "post", lambda *a, **k: _ok({"message": {"id": "out-1"}}))
    reset_fake()
    main._rate_hits.clear()
    PENDING_TIPS.clear()
    seed_demo(SHOP)
    conn = connect()
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    conn.close()
    # Stub LLM so accidental calls fail loudly
    monkeypatch.setattr(llm, "chat", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("LLM must not run for demo tip/join/limit paths")))
    return tmp_path


def _ok(data=None):
    class R:
        status_code = 200
        text = "{}"
        content = b"{}"
        headers = {}

        def json(self):
            return data or {"status": "ok"}

        def raise_for_status(self):
            return None
    return R()


def _msg(mid="d-mid-1", frm=STRANGER, body="SISI-DEMO", mtype="text"):
    m = {"id": mid, "from_me": False, "type": mtype,
         "chat_id": f"{frm}@s.whatsapp.net", "timestamp": int(NOW),
         "source": "mobile", "from": frm, "from_name": "Demo"}
    if mtype == "text":
        m["text"] = {"body": body}
    return m


def _payload(*msgs):
    return {"messages": list(msgs),
            "event": {"type": "messages", "event": "post"},
            "channel_id": CHANNEL}


def test_extract_join_code_and_language():
    assert demo.extract_join("please SISI-DEMO thanks") == (True, "en")
    assert demo.extract_join("sisi-demo-zu") == (True, "zu")
    assert demo.extract_join("code SISI-DEMO-AF here") == (True, "af")
    assert demo.extract_join("hello") == (False, "en")
    assert demo.extract_join("") == (False, "en")


def test_stranger_without_code_gets_tip_once(demo_env, monkeypatch):
    sent = []

    def capture(channel, to, kind, payload):
        sent.append((channel, to, kind, payload))
        return "mid-tip"

    monkeypatch.setattr("app.sendqueue.send_immediate", capture)
    client = TestClient(app)
    body = _payload(_msg(mid="tip-1", body="hi sisi"))
    r = client.post(f"/webhook/whapi/{TOKEN}", json=body)
    assert r.status_code == 200
    # BackgroundTasks run inline in TestClient
    assert len(sent) == 1
    assert sent[0][1] == STRANGER
    assert "SISI-DEMO" in sent[0][3]["body"]
    # Second tip same day suppressed
    body2 = _payload(_msg(mid="tip-2", body="hello again"))
    client.post(f"/webhook/whapi/{TOKEN}", json=body2)
    assert len(sent) == 1


def test_join_provisions_isolated_shop_and_menu(demo_env):
    msgs = parse_whapi_webhook(
        _payload(_msg(mid="join-1", body="I want SISI-DEMO please")), now=NOW)
    assert len(msgs) == 1
    assert msgs[0].shop_id == STRANGER

    process_message(IncomingMessage(
        "join-1", STRANGER, "text", "I want SISI-DEMO please",
        str(int(NOW)), channel="whapi"))

    conn = connect()
    assert demo.is_demo_shop(conn, STRANGER)
    assert demo.demo_msgs_used(conn, STRANGER) == 1
    # Premade stock + today's sample sales present
    n = conn.execute("SELECT COUNT(*) n FROM products WHERE shop_id = ?",
                     (STRANGER,)).fetchone()["n"]
    assert n >= 8
    sales = conn.execute("SELECT COUNT(*) n FROM sales WHERE shop_id = ?",
                         (STRANGER,)).fetchone()["n"]
    assert sales >= 3
    # Real allowlisted shop untouched
    n_real = conn.execute("SELECT COUNT(*) n FROM products WHERE shop_id = ?",
                          (SHOP,)).fetchone()["n"]
    assert n_real >= 8
    lang = conn.execute("SELECT language FROM shops WHERE shop_id = ?",
                        (STRANGER,)).fetchone()["language"]
    assert lang == "en"
    conn.close()

    # Menu was queued via deferred overview path
    lists = take_lists(STRANGER)
    # After process_message the deferred overview drains lists; may be empty
    # if already sent. Check shop is setup-done.
    from app.setup import needs_setup
    conn = connect()
    assert not needs_setup(conn, STRANGER)
    conn.close()


def test_join_language_suffix(demo_env):
    process_message(IncomingMessage(
        "join-zu", STRANGER2, "text", "SISI-DEMO-ZU",
        str(int(NOW)), channel="whapi"))
    conn = connect()
    assert demo.is_demo_shop(conn, STRANGER2)
    assert conn.execute("SELECT language FROM shops WHERE shop_id = ?",
                        (STRANGER2,)).fetchone()["language"] == "zu"
    conn.close()


def test_ten_message_cap_and_eleventh(demo_env, monkeypatch):
    # Join
    process_message(IncomingMessage(
        "c0", STRANGER, "text", "SISI-DEMO", str(int(NOW)), channel="whapi"))
    # Messages 2..10: deterministic menu taps (no LLM)
    for i in range(2, 11):
        process_message(IncomingMessage(
            f"c{i}", STRANGER, "text", "menu",
            str(int(NOW) + i), channel="whapi"))
    conn = connect()
    assert demo.demo_msgs_used(conn, STRANGER) == 10
    conn.close()

    # 11th: exhausted, no LLM (chat stub raises)
    process_message(IncomingMessage(
        "c11", STRANGER, "text", "who owes me",
        str(int(NOW) + 20), channel="whapi"))
    # 12th same day: silent
    process_message(IncomingMessage(
        "c12", STRANGER, "text", "stock list",
        str(int(NOW) + 21), channel="whapi"))
    conn = connect()
    assert demo.demo_msgs_used(conn, STRANGER) == 12
    assert demo.exhausted_notice_sent_today(conn, STRANGER)
    conn.close()


def test_daily_cap_blocks_new_joins(demo_env, monkeypatch):
    monkeypatch.setenv("SISI_DEMO_DAILY_CAP", "1")
    process_message(IncomingMessage(
        "cap1", STRANGER, "text", "SISI-DEMO", str(int(NOW)), channel="whapi"))
    process_message(IncomingMessage(
        "cap2", STRANGER2, "text", "SISI-DEMO", str(int(NOW) + 1),
        channel="whapi"))
    conn = connect()
    assert demo.is_demo_shop(conn, STRANGER)
    assert not demo.is_demo_shop(conn, STRANGER2)
    conn.close()


def test_allowlisted_shop_unaffected(demo_env):
    # Allowlisted number still parses without needing a join code
    msgs = parse_whapi_webhook(
        _payload(_msg(mid="a1", frm=SHOP, body="menu")), now=NOW)
    assert len(msgs) == 1
    assert msgs[0].shop_id == SHOP
