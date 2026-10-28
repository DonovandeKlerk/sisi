"""Media-send fix: 429 recovery, volume-backed media, resend path."""

import sys
import types

import httpx
import pytest

from app import reports, sendqueue
from app.sendqueue import MEDIA_FALLBACK, drain, enqueue
from app.tools import ensure_shop, resend_last_media
from app.whatsapp import FakeSender

SHOP = "27710000030"


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


def _fake_modal(monkeypatch, calls):
    """Stub the modal SDK: records Volume commit/reload calls."""
    mod = types.ModuleType("modal")

    class Volume:
        @classmethod
        def from_name(cls, name):
            calls.append(("volume", name))
            vol = types.SimpleNamespace()
            vol.commit = lambda: calls.append("commit")
            vol.reload = lambda: calls.append("reload")
            return vol

    mod.Volume = Volume
    monkeypatch.setitem(sys.modules, "modal", mod)
    monkeypatch.setenv("SISI_MEDIA_VOLUME", "sisi-data")


def test_429_then_202_retries_and_delivers(conn, clock, monkeypatch):
    """A live-shaped 429 (then 202) through VonageSender + drain: retried once."""
    import app.vonage as vonage
    monkeypatch.setenv("VONAGE_API_KEY", "k")
    monkeypatch.setenv("VONAGE_API_SECRET", "s")
    ensure_shop(conn, SHOP)
    posts = []

    def _post(url, **kw):
        posts.append(kw.get("json", {}))
        req = httpx.Request("POST", "https://messages-sandbox.nexmo.com/x")
        if len(posts) == 1:
            return httpx.Response(429, headers={"retry-after": "1"},
                                  request=req)
        return httpx.Response(202, json={"message_uuid": "u1"}, request=req)

    monkeypatch.setattr(vonage.httpx, "post", _post)
    enqueue(conn, SHOP, "vonage", "text", {"body": "hi"})
    stats = drain(conn, SHOP, vonage.VonageSender(), clock=clock,
                  sleep=clock.sleep)
    assert len(posts) == 2
    assert stats == {"sent": 1, "rate_limited": True, "failed": 0}
    assert clock.sleeps == [1.0]
    assert conn.execute("SELECT COUNT(*) n FROM send_queue").fetchone()["n"] == 0


def test_enqueue_splits_long_text_into_paced_sends(conn, clock):
    ensure_shop(conn, SHOP)
    body = "x" * 3000
    enqueue(conn, SHOP, "vonage", "text", {"body": body})
    rows = conn.execute("SELECT * FROM send_queue ORDER BY id").fetchall()
    assert len(rows) == 2
    sender = FakeSender()
    stats = drain(conn, SHOP, sender, clock=clock, sleep=clock.sleep)
    assert stats["sent"] == 2
    assert clock.sleeps == [1.5]
    assert "".join(m["body"] for m in sender.pop_shop(SHOP)) == body


def test_media_send_commits_volume(conn, clock, monkeypatch):
    calls = []
    _fake_modal(monkeypatch, calls)
    ensure_shop(conn, SHOP)
    enqueue(conn, SHOP, "vonage", "image",
            {"url": "https://x/y.png", "caption": "card"})
    stats = drain(conn, SHOP, FakeSender(), clock=clock, sleep=clock.sleep)
    assert stats["sent"] == 1
    assert ("volume", "sisi-data") in calls
    assert "commit" in calls


def test_media_send_without_modal_env_never_touches_modal(conn, clock,
                                                          monkeypatch):
    monkeypatch.delenv("SISI_MEDIA_VOLUME", raising=False)
    boom = types.ModuleType("modal")

    def _boom(name):
        raise AssertionError("modal must not be touched")

    boom.Volume = types.SimpleNamespace(from_name=_boom)
    monkeypatch.setitem(sys.modules, "modal", boom)
    ensure_shop(conn, SHOP)
    enqueue(conn, SHOP, "vonage", "image",
            {"url": "https://x/y.png", "caption": "card"})
    stats = drain(conn, SHOP, FakeSender(), clock=clock, sleep=clock.sleep)
    assert stats["sent"] == 1


def test_media_url_served_after_volume_reload(tmp_path, monkeypatch):
    """A file + row filed elsewhere is served here: reload, bytes, headers."""
    from fastapi.testclient import TestClient

    from app.db import connect, init_db
    from app.main import app
    calls = []
    _fake_modal(monkeypatch, calls)
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    png = tmp_path / "reports" / "card.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    payload = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    png.write_bytes(payload)
    db_path = tmp_path / "route.db"
    c = connect(db_path)
    init_db(c)
    c.execute("INSERT INTO shops (shop_id, name) VALUES (?, 'S')", (SHOP,))
    c.execute("INSERT INTO reports (shop_id, month, png_path, token)"
              " VALUES (?, 'stmt-x', ?, 'tok1')", (SHOP, str(png)))
    c.commit()
    c.close()
    monkeypatch.setenv("SISI_DB", str(db_path))
    r = TestClient(app).get("/media/tok1", params={"f": "png"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert "card.png" in r.headers["content-disposition"]
    assert r.content == payload
    assert "reload" in calls


def test_resend_path_end_to_end(conn, clock):
    ensure_shop(conn, SHOP)
    conn.execute("UPDATE shops SET channel = 'vonage' WHERE shop_id = ?",
                 (SHOP,))
    conn.commit()

    class NoPics(FakeSender):
        def send_image(self, to, url, caption=""):
            raise RuntimeError("boom")

    broken = NoPics()
    enqueue(conn, SHOP, "vonage", "image",
            {"url": "https://x/y.png", "caption": "card"})
    stats = drain(conn, SHOP, broken, clock=clock, sleep=clock.sleep)
    assert stats["failed"] == 1
    assert MEDIA_FALLBACK in [m["body"] for m in broken.pop_shop(SHOP)]
    assert conn.execute("SELECT value FROM flags WHERE shop_id = ?"
                        " AND key = 'last-failed-media'",
                        (SHOP,)).fetchone() is not None

    assert resend_last_media(conn, SHOP) == "📤 *Sending it again…*"
    sender = FakeSender()
    stats = drain(conn, SHOP, sender, clock=clock, sleep=clock.sleep)
    assert stats["sent"] == 1
    delivered = sender.pop_shop(SHOP)
    assert len(delivered) == 1 and delivered[0]["type"] == "image"
    assert delivered[0]["url"] == "https://x/y.png"
    assert delivered[0]["body"] == "card"
