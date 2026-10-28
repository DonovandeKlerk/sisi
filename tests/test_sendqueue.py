"""Reliable sending: per-number queue, pacing, 429 backoff, fallback, resend."""

import httpx
import pytest

from app import sendqueue
from app.sendqueue import (
    MEDIA_FALLBACK,
    QueuedSender,
    RateLimited,
    drain,
    drain_all,
    enqueue,
)
from app.tools import ensure_shop, resend_last_media
from app.whatsapp import FakeSender


SHOP = "27710000018"


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
    monkeypatch.setattr(sendqueue, "check_media", lambda url, kind, timeout=5.0: (True, "image/png"))


def _queued(conn, to=SHOP):
    return conn.execute("SELECT * FROM send_queue WHERE to_number = ?"
                        " ORDER BY id", (to,)).fetchall()


def test_order_and_pacing(conn, clock):
    ensure_shop(conn, SHOP)
    sender = FakeSender()
    enqueue(conn, SHOP, "vonage", "buttons", {"body": "Save?", "buttons": ["Yes", "No"]})
    enqueue(conn, SHOP, "vonage", "text", {"body": "second"})
    enqueue(conn, SHOP, "vonage", "image", {"url": "https://x/y.png", "caption": "pic"})
    stats = drain(conn, SHOP, sender, clock=clock, sleep=clock.sleep)
    assert stats == {"sent": 3, "rate_limited": False, "failed": 0}
    kinds = [m["type"] for m in sender.pop_shop(SHOP)]
    assert kinds == ["buttons", "text", "image"]
    assert clock.sleeps == [1.5, 1.5]
    assert _queued(conn) == []


def test_consecutive_texts_merge(conn, clock):
    ensure_shop(conn, SHOP)
    sender = FakeSender()
    enqueue(conn, SHOP, "vonage", "text", {"body": "one"})
    enqueue(conn, SHOP, "vonage", "text", {"body": "two"})
    stats = drain(conn, SHOP, sender, clock=clock, sleep=clock.sleep)
    assert stats["sent"] == 1
    sent = sender.pop_shop(SHOP)
    assert len(sent) == 1 and sent[0]["body"] == "one\n\ntwo"
    assert clock.sleeps == []


def test_merge_respects_split_limit(conn, clock):
    ensure_shop(conn, SHOP)
    sender = FakeSender()
    enqueue(conn, SHOP, "vonage", "text", {"body": "a" * 1000})
    enqueue(conn, SHOP, "vonage", "text", {"body": "b" * 1000})
    stats = drain(conn, SHOP, sender, clock=clock, sleep=clock.sleep)
    assert stats["sent"] == 2
    assert len(sender.pop_shop(SHOP)) == 2


def test_429_retries_then_delivers(conn, clock):
    ensure_shop(conn, SHOP)
    calls = []

    class Flaky(FakeSender):
        def send_text(self, to, body):
            calls.append(body)
            if len(calls) < 3:
                raise RateLimited()
            super().send_text(to, body)

    enqueue(conn, SHOP, "vonage", "text", {"body": "hi"})
    stats = drain(conn, SHOP, Flaky(), clock=clock, sleep=clock.sleep)
    assert stats == {"sent": 1, "rate_limited": True, "failed": 0}
    assert clock.sleeps == [2.0, 4.0]
    assert _queued(conn) == []


def test_retry_after_honoured(conn, clock):
    ensure_shop(conn, SHOP)

    class Once(FakeSender):
        def __init__(self):
            super().__init__()
            self.n = 0

        def send_text(self, to, body):
            self.n += 1
            if self.n == 1:
                raise RateLimited(9.0)
            super().send_text(to, body)

    enqueue(conn, SHOP, "vonage", "text", {"body": "hi"})
    stats = drain(conn, SHOP, Once(), clock=clock, sleep=clock.sleep)
    assert stats["sent"] == 1
    assert clock.sleeps == [9.0]


def test_raw_http_429_with_header(conn, clock):
    ensure_shop(conn, SHOP)

    class Raw(FakeSender):
        def __init__(self):
            super().__init__()
            self.n = 0

        def send_text(self, to, body):
            self.n += 1
            if self.n == 1:
                req = httpx.Request("POST", "https://x")
                resp = httpx.Response(429, headers={"retry-after": "3"},
                                      request=req)
                raise httpx.HTTPStatusError("429", request=req, response=resp)
            super().send_text(to, body)

    enqueue(conn, SHOP, "vonage", "text", {"body": "hi"})
    stats = drain(conn, SHOP, Raw(), clock=clock, sleep=clock.sleep)
    assert stats["sent"] == 1 and stats["rate_limited"] is True
    assert clock.sleeps == [3.0]


def test_text_dropped_after_30s_budget(conn, clock):
    ensure_shop(conn, SHOP)

    class Down(FakeSender):
        def send_text(self, to, body):
            raise RateLimited()

    enqueue(conn, SHOP, "vonage", "text", {"body": "hi"})
    stats = drain(conn, SHOP, Down(), clock=clock, sleep=clock.sleep)
    assert stats == {"sent": 0, "rate_limited": True, "failed": 1}
    assert clock.sleeps == [2.0, 4.0, 8.0, 16.0]
    assert _queued(conn) == []


def test_failed_media_queues_fallback_and_continues(conn, clock):
    ensure_shop(conn, SHOP)

    class NoPics(FakeSender):
        def send_image(self, to, url, caption=""):
            raise RuntimeError("boom")

    enqueue(conn, SHOP, "vonage", "image",
            {"url": "https://x/y.png", "caption": "card"})
    enqueue(conn, SHOP, "vonage", "text", {"body": "the total is R10.00"})
    sender = NoPics()
    stats = drain(conn, SHOP, sender, clock=clock, sleep=clock.sleep)
    assert stats["failed"] == 1
    row = conn.execute("SELECT value FROM flags WHERE shop_id = ?"
                       " AND key = 'last-failed-media'", (SHOP,)).fetchone()
    assert row is not None and "y.png" in row["value"]
    # the flow continues: text delivered, then the fallback
    bodies = [m["body"] for m in sender.pop_shop(SHOP)]
    assert bodies == ["the total is R10.00", MEDIA_FALLBACK]
    assert _queued(conn) == []


def test_failed_media_fallback_text_sent(conn, clock):
    ensure_shop(conn, SHOP)
    seen = []

    class NoPics(FakeSender):
        def send_image(self, to, url, caption=""):
            raise RuntimeError("boom")

        def send_text(self, to, body):
            seen.append(body)
            super().send_text(to, body)

    enqueue(conn, SHOP, "vonage", "text", {"body": "statement total R10.00"})
    enqueue(conn, SHOP, "vonage", "image",
            {"url": "https://x/y.png", "caption": "card"})
    drain(conn, SHOP, NoPics(), clock=clock, sleep=clock.sleep)
    assert seen[0] == "statement total R10.00"
    assert MEDIA_FALLBACK in seen[1]


def test_resend_requeues_failed_media(conn):
    ensure_shop(conn, SHOP)
    conn.execute("UPDATE shops SET channel = 'vonage' WHERE shop_id = ?",
                 (SHOP,))
    conn.execute("INSERT INTO flags (shop_id, key, value)"
                 " VALUES (?, 'last-failed-media', ?)",
                 (SHOP, '{"kind": "image", "url": "https://x/y.png",'
                        ' "caption": "card", "filename": ""}'))
    conn.commit()
    assert resend_last_media(conn, SHOP) == "📤 *Sending it again…*"
    rows = _queued(conn)
    assert len(rows) == 1 and rows[0]["kind"] == "image"
    assert conn.execute("SELECT value FROM flags WHERE shop_id = ?"
                        " AND key = 'last-failed-media'",
                        (SHOP,)).fetchone() is None


def test_resend_with_nothing(conn):
    ensure_shop(conn, SHOP)
    assert resend_last_media(conn, SHOP).startswith("Nothing to resend")


def test_vonage_sender_raises_rate_limited(monkeypatch):
    import app.vonage as vonage
    monkeypatch.setenv("VONAGE_API_KEY", "k")
    monkeypatch.setenv("VONAGE_API_SECRET", "s")

    class Resp:
        status_code = 429
        headers = {"retry-after": "5"}
        text = "too many"

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "429", request=httpx.Request("POST", "https://x"),
                response=httpx.Response(429, headers=self.headers,
                                        request=httpx.Request("POST",
                                                              "https://x")))

        def json(self):
            return {}

    monkeypatch.setattr(vonage.httpx, "post", lambda *a, **k: Resp())
    with pytest.raises(RateLimited) as exc:
        vonage.VonageSender().send_text(SHOP, "hi")
    assert exc.value.retry_after == 5.0


def test_queued_sender_passthrough_for_other_channels(conn):
    ensure_shop(conn, SHOP)
    sender = FakeSender()
    out = QueuedSender.wrap(sender, conn, "meta")
    out.send_text(SHOP, "hi")
    assert len(sender.pop_shop(SHOP)) == 1
    assert _queued(conn) == []
    assert out.flush() == {"sent": 0, "rate_limited": False, "failed": 0}


def test_queued_sender_queues_vonage(conn, clock):
    ensure_shop(conn, SHOP)
    sender = FakeSender()
    out = QueuedSender.wrap(sender, conn, "vonage", clock=clock,
                            sleep=clock.sleep)
    out.send_text(SHOP, "hi")
    assert sender.pop_shop(SHOP) == []
    assert len(_queued(conn)) == 1
    out.flush()
    assert len(sender.pop_shop(SHOP)) == 1
    assert _queued(conn) == []


def test_drain_all_covers_every_number(conn, clock):
    ensure_shop(conn, SHOP)
    ensure_shop(conn, "27710000019")
    sender = FakeSender()
    enqueue(conn, SHOP, "vonage", "text", {"body": "a"})
    enqueue(conn, "27710000019", "vonage", "text", {"body": "b"})
    stats = drain_all(conn, lambda channel: sender, clock=clock,
                      sleep=clock.sleep)
    assert stats["sent"] == 2
    assert len(sender.pop_shop(SHOP)) == 1
    assert len(sender.pop_shop("27710000019")) == 1


def test_handle_message_vonage_goes_through_queue(conn, monkeypatch):
    import app.main as main
    from app.main import _handle_message
    from app.whatsapp import IncomingMessage
    monkeypatch.setattr(main, "handle_text", lambda c, s, t: ("cash reply", None))
    sender = FakeSender()
    _handle_message(sender, IncomingMessage("mid-q1", SHOP, "text",
                                            "cash up", "", channel="vonage"))
    sent = sender.pop_shop(SHOP)
    assert len(sent) == 1 and sent[0]["body"].startswith("cash reply")
