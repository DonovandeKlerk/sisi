"""Background media worker: 429 recovery off-webhook, links, resend."""

import logging
import sys
import types

import pytest

from app import reports, sendqueue
from app.mediasend import (request_media_send, send_media_deferred,
                           with_media_links)
from app.sendqueue import RateLimited
from app.tools import ensure_shop, resend_last_media
from app.whatsapp import FakeSender

SHOP = "27710000041"
CARD_URL = "https://x.example/media/abc123?f=png"


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.t += s


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture(autouse=True)
def _no_head(monkeypatch):
    monkeypatch.setattr(sendqueue, "check_media",
                        lambda url, kind, timeout=5.0: (True, "image/png"))


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(reports, "LOGOS_DIR", tmp_path / "logos")
    monkeypatch.setenv("SISI_PUBLIC_URL", "https://x.example")


class ScriptedSender(FakeSender):
    """Fails image sends N times (RateLimited), then delivers."""

    def __init__(self, fails, retry_after=None):
        super().__init__()
        self._fails = fails
        self._retry_after = retry_after
        self.image_tries = 0

    def send_image(self, to, url, caption=""):
        self.image_tries += 1
        if self.image_tries <= self._fails:
            raise RateLimited(self._retry_after)
        return super().send_image(to, url, caption)


def _fake_fn_modal(monkeypatch, calls, fail=False):
    mod = types.ModuleType("modal")

    class Function:
        @classmethod
        def from_name(cls, app, name):
            if fail:
                raise RuntimeError("no creds")
            calls.append((app, name))
            fn = types.SimpleNamespace()
            fn.spawn = lambda payload: calls.append(("spawn", payload))
            return fn

    mod.Function = Function
    monkeypatch.setitem(sys.modules, "modal", mod)


def test_429_x4_then_202_delivered_via_worker(conn, clock, caplog):
    ensure_shop(conn, SHOP)
    sender = ScriptedSender(4)
    caplog.set_level(logging.INFO, logger="sisi.mediasend")
    stats = send_media_deferred(SHOP, "vonage", "image", CARD_URL, "card",
                                sender=sender, conn=conn, clock=clock,
                                sleep=clock.sleep)
    assert stats == {"sent": 1, "attempts": 5, "failed": 0}
    assert clock.sleeps == [3.0, 5.0, 10.0, 20.0, 40.0]
    delivered = sender.pop_shop(SHOP)
    assert len(delivered) == 1 and delivered[0]["type"] == "image"
    assert "attempt=1 status=429 wait=5.0s" in caplog.text
    assert "attempt=5 status=sent" in caplog.text


def test_worker_honours_retry_after(conn, clock):
    ensure_shop(conn, SHOP)
    sender = ScriptedSender(1, retry_after=30.0)
    stats = send_media_deferred(SHOP, "vonage", "image", CARD_URL, "card",
                                sender=sender, conn=conn, clock=clock,
                                sleep=clock.sleep)
    assert stats["sent"] == 1
    assert clock.sleeps == [3.0, 30.0]


def test_worker_falls_back_after_6_failures(conn, clock):
    ensure_shop(conn, SHOP)
    sender = ScriptedSender(99)
    stats = send_media_deferred(SHOP, "vonage", "image", CARD_URL, "card",
                                sender=sender, conn=conn, clock=clock,
                                sleep=clock.sleep)
    assert stats == {"sent": 0, "attempts": 6, "failed": 1}
    assert clock.sleeps == [3.0, 5.0, 10.0, 20.0, 40.0, 60.0, 1.5]
    bodies = [m["body"] for m in sender.pop_shop(SHOP)]
    assert bodies == ["Couldn't attach the file. Use the link above"
                      " or reply 'resend'."]
    row = conn.execute("SELECT value FROM flags WHERE shop_id = ?"
                       " AND key = 'last-failed-media'", (SHOP,)).fetchone()
    assert row is not None and "abc123" in row["value"]


def test_worker_respects_dict_gap(conn, clock, monkeypatch):
    store = {f"sendlock:{SHOP}": 1002.5}
    mod = types.ModuleType("modal")

    class Dict:
        @classmethod
        def from_name(cls, name, create_if_missing=False):
            assert name == "sisi-sendlock"
            box = types.SimpleNamespace()
            box.get = lambda k, d=0.0: store.get(k, d)
            box.put = lambda k, v: store.__setitem__(k, v)
            return box

    mod.Dict = Dict
    monkeypatch.setitem(sys.modules, "modal", mod)
    monkeypatch.setenv("SISI_SEND_LOCK", "sisi-sendlock")
    ensure_shop(conn, SHOP)
    sender = ScriptedSender(0)
    stats = send_media_deferred(SHOP, "vonage", "image", CARD_URL, "card",
                                sender=sender, conn=conn, clock=clock,
                                sleep=clock.sleep)
    assert stats["sent"] == 1
    assert clock.sleeps == [3.0, 1.5]
    assert store[f"sendlock:{SHOP}"] == pytest.approx(1004.5)


def test_spawn_routing_spawned_on_modal(conn, monkeypatch):
    calls: list = []
    _fake_fn_modal(monkeypatch, calls)
    monkeypatch.setenv("SISI_SPAWN", "1")
    mode = request_media_send(conn, SHOP, "vonage", "image",
                              {"url": CARD_URL, "caption": "card"},
                              delay_s=3.0)
    assert mode == "spawned"
    assert calls[0] == ("sisi", "send_media")
    assert calls[1][0] == "spawn"
    assert calls[1][1] == {"shop_id": SHOP, "channel": "vonage",
                           "kind": "image", "url": CARD_URL, "caption": "card",
                           "filename": "", "delay_s": 3.0}
    assert conn.execute("SELECT COUNT(*) c FROM send_queue").fetchone()["c"] == 0


def test_spawn_routing_queues_when_spawn_fails(conn, monkeypatch):
    calls: list = []
    _fake_fn_modal(monkeypatch, calls, fail=True)
    monkeypatch.setenv("SISI_SPAWN", "1")
    mode = request_media_send(conn, SHOP, "vonage", "image",
                              {"url": CARD_URL, "caption": "card"})
    assert mode == "queued"
    row = conn.execute("SELECT kind FROM send_queue").fetchone()
    assert row["kind"] == "image"


def test_spawn_routing_queues_without_flag(conn):
    mode = request_media_send(conn, SHOP, "vonage", "image",
                              {"url": CARD_URL, "caption": "card"})
    assert mode == "queued"
    row = conn.execute("SELECT kind FROM send_queue").fetchone()
    assert row["kind"] == "image"


def test_with_media_links_rewrites_and_skips_local():
    assert with_media_links("hi", None) == "hi"
    assert with_media_links("hi", []) == "hi"
    out = with_media_links("hi", [
        {"kind": "image", "url": "https://h/media/abc?f=png"},
        {"kind": "file", "url": "https://h/media/abc?f=pdf"},
        {"kind": "image", "url": "/tmp/x.png"},
    ])
    assert out == ("hi\n\n📎 Card: https://h/media/abc.png"
                   "\n📄 PDF: https://h/media/abc.pdf")


def test_link_in_reply_text(tmp_path, monkeypatch):
    from app.db import connect as file_connect
    from app.db import init_db
    from app.main import _handle_message
    from app.whatsapp import IncomingMessage

    monkeypatch.setenv("SISI_DB", str(tmp_path / "link.db"))
    fc = file_connect()
    init_db(fc)
    ensure_shop(fc, SHOP, "Link Spaza")
    sipho = fc.execute("INSERT INTO customers (shop_id, name)"
                       " VALUES (?, 'Sipho')", (SHOP,)).lastrowid
    fc.execute("INSERT INTO credit_entries (shop_id, customer_id,"
               " amount_cents, items_text, action_id, created_at)"
               " VALUES (?, ?, 4000, 'Bread', ?, ?)",
               (SHOP, sipho, "lnk-ce-0", "2026-10-05T10:00:00.000Z"))
    fc.commit()
    fc.close()
    sender = FakeSender()
    _handle_message(sender, IncomingMessage("mid-link1", SHOP, "text",
                                            "statement for Sipho", ""))
    body = "\n".join(m.get("body", "") for m in sender.pop_shop(SHOP))
    assert "📎 Card: https://x.example/media/" in body
    assert "📄 PDF: https://x.example/media/" in body
    assert ".png" in body and ".pdf" in body


def test_statement_reminder_text_carries_link(conn):
    import json

    from app.reminders import send_due

    ensure_shop(conn, SHOP, "Link Spaza")
    sipho = conn.execute("INSERT INTO customers (shop_id, name)"
                         " VALUES (?, 'Sipho')", (SHOP,)).lastrowid
    conn.execute("INSERT INTO credit_entries (shop_id, customer_id,"
                 " amount_cents, items_text, action_id, created_at)"
                 " VALUES (?, ?, 4000, 'Bread', ?, ?)",
                 (SHOP, sipho, "lnk-ce-0", "2026-10-05T10:00:00.000Z"))
    conn.execute("INSERT INTO reminders (shop_id, summary, due_at,"
                 " context_json, recurrence, status)"
                 " VALUES (?, 'collect', '2026-10-26T05:00:00+02:00', ?, '',"
                 " 'pending')",
                 (SHOP, json.dumps({"kind": "statement", "name": "Sipho"})))
    conn.commit()
    assert send_due(conn) == [1]
    payload = conn.execute("SELECT payload_json FROM send_queue"
                           " WHERE kind = 'text'").fetchone()["payload_json"]
    text = json.loads(payload)["body"]
    assert "📎 Card: https://x.example/media/" in text
    assert ".png" in text


def test_resend_spawns_on_modal(conn, monkeypatch):
    import json

    calls: list = []
    _fake_fn_modal(monkeypatch, calls)
    monkeypatch.setenv("SISI_SPAWN", "1")
    ensure_shop(conn, SHOP)
    conn.execute("UPDATE shops SET channel = 'vonage' WHERE shop_id = ?",
                 (SHOP,))
    conn.execute("INSERT INTO flags (shop_id, key, value) VALUES (?, ?, ?)",
                 (SHOP, "last-failed-media",
                  json.dumps({"kind": "image", "url": CARD_URL,
                              "caption": "card", "filename": ""})))
    conn.commit()
    assert resend_last_media(conn, SHOP) == "📤 *Sending it again…*"
    assert calls[1][1]["url"] == CARD_URL
    assert conn.execute("SELECT COUNT(*) c FROM flags WHERE shop_id = ?"
                        " AND key = 'last-failed-media'",
                        (SHOP,)).fetchone()["c"] == 0


def test_media_suffix_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.db import connect as file_connect
    from app.db import init_db
    from app.main import app as fastapi_app

    png = tmp_path / "c.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nPAYLOAD")
    pdf = tmp_path / "c.pdf"
    pdf.write_bytes(b"%PDF-1.4 PAYLOAD")
    db_path = tmp_path / "suffix.db"
    monkeypatch.setenv("SISI_DB", str(db_path))
    fc = file_connect()
    init_db(fc)
    fc.execute("INSERT INTO shops (shop_id, name) VALUES (?, 'S')", (SHOP,))
    fc.execute("INSERT INTO reports (shop_id, month, pdf_path, png_path, token)"
               " VALUES (?, '2026-10', ?, ?, ?)",
               (SHOP, str(pdf), str(png), "cafef00d"))
    fc.commit()
    fc.close()
    client = TestClient(fastapi_app)
    r = client.get("/media/cafef00d.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert 'filename="c.png"' in r.headers["content-disposition"]
    assert r.content == b"\x89PNG\r\n\x1a\nPAYLOAD"
    r = client.get("/media/cafef00d.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == b"%PDF-1.4 PAYLOAD"
    assert client.get("/media/cafef00d.txt").status_code == 404
    assert client.get("/media/cafef00d",
                      params={"f": "bogus"}).status_code == 404


def test_media_served_from_fresh_process(tmp_path, monkeypatch):
    """A brand-new uvicorn process serves /media from the shared files."""
    import os
    import socket
    import subprocess
    import time
    from pathlib import Path

    import httpx

    from app.db import connect as file_connect
    from app.db import init_db

    png = tmp_path / "fresh.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nFRESH")
    pdf = tmp_path / "fresh.pdf"
    pdf.write_bytes(b"%PDF-1.4 FRESH")
    db_path = tmp_path / "fresh.db"
    fc = file_connect(db_path)
    init_db(fc)
    fc.execute("INSERT INTO shops (shop_id, name) VALUES (?, 'S')", (SHOP,))
    fc.execute("INSERT INTO reports (shop_id, month, pdf_path, png_path, token)"
               " VALUES (?, '2026-10', ?, ?, ?)",
               (SHOP, str(pdf), str(png), "fresh01"))
    fc.commit()
    fc.close()
    env = {k: v for k, v in os.environ.items()
           if k != "SISI_MEDIA_VOLUME"}
    env["SISI_DB"] = str(db_path)
    env["SISI_REPORTS"] = str(tmp_path)
    env["LOG_LEVEL"] = "ERROR"
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    root = Path(__file__).resolve().parent.parent
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host",
         "127.0.0.1", "--port", str(port)],
        cwd=root, env=env, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 25
        while True:
            try:
                if httpx.get(base + "/health", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.time() < deadline, "uvicorn did not start"
            time.sleep(0.2)
        r = httpx.get(base + "/media/fresh01.png", timeout=5.0)
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content == b"\x89PNG\r\n\x1a\nFRESH"
        r = httpx.get(base + "/media/fresh01.pdf", timeout=5.0)
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        assert r.content == b"%PDF-1.4 FRESH"
        assert httpx.get(base + "/media/fresh01.txt",
                         timeout=5.0).status_code == 404
    finally:
        proc.terminate()
        proc.wait(timeout=10)
