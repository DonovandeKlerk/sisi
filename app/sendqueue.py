"""Reliable outbound sending: one ordered queue per number.

All Vonage sends go through `send_queue` rows and `drain()`: at least
PACE_S apart per number, in id order. A 429 (or any transport/5xx error
the sender surfaces) parks the head row with backoff — 2s, 4s, 8s, 16s
(30s total), honouring Retry-After — and the drain waits within budget
so the common case still delivers inside one worker; the every-minute
cron drains leftovers. Consecutive text rows merge into one message
(up to the split limit). Media that still fails after retries is
replaced by a fallback text offering 'resend', and the drain continues
so no flow ever stalls behind a failed picture.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time

import httpx

from app.providers import QUEUED_CHANNELS, WrongProviderError

log = logging.getLogger("sisi.sendqueue")

PACE_S = 1.5
BACKOFF_S = (2.0, 4.0, 8.0, 16.0)  # sums to 30s
DRAIN_BUDGET_S = 40.0  # covers the 30s backoff plus pacing drift
MEDIA_FALLBACK = "Couldn't send the PDF/card, reply 'resend' to try again"


def _fallback_text() -> str:
    from app.i18n import t
    return t("media_fallback")


class RateLimited(Exception):
    """The channel answered 429. retry_after overrides the next backoff."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("rate limited (429)")
        self.retry_after = retry_after


def _retry_after_s(response) -> float | None:
    try:
        value = response.headers.get("retry-after", "")
        return max(0.0, float(value))
    except (ValueError, TypeError, AttributeError):
        return None


def enqueue(conn: sqlite3.Connection, to: str, channel: str, kind: str,
            payload: dict, commit: bool = True) -> int:
    """Queue one outbound message. kind: text | buttons | image | file | template.

    Long texts split into ≤1500-char rows up front, so every HTTP send —
    not just every row — leaves the per-number gap (pieces that still fit
    together re-merge at drain time). Returns the last row id.
    """
    from app.whatsapp import split_message
    payload = dict(payload or {})
    bodies = split_message(payload.get("body", "")) if kind == "text" else []
    chunks = bodies or [None]
    row_id = -1
    for body in chunks:
        chunk = dict(payload, body=body) if body is not None else payload
        cur = conn.execute(
            "INSERT INTO send_queue (to_number, channel, kind, payload_json)"
            " VALUES (?, ?, ?, ?)",
            (to, channel or "", kind, json.dumps(chunk)),
        )
        row_id = cur.lastrowid
    if commit:
        conn.commit()
    return row_id


def _head(conn: sqlite3.Connection, to: str, now: float):
    return conn.execute(
        "SELECT * FROM send_queue WHERE to_number = ? AND status = 'queued'"
        " AND (next_at IS NULL OR next_at <= ?) ORDER BY id LIMIT 1",
        (to, now),
    ).fetchone()


def _waiting(conn: sqlite3.Connection, to: str):
    return conn.execute(
        "SELECT * FROM send_queue WHERE to_number = ? AND status = 'queued'"
        " AND next_at IS NOT NULL ORDER BY next_at LIMIT 1",
        (to,),
    ).fetchone()


STUCK_SEND_S = 600.0
"""A 'sending' claim older than this is a crashed drain: safe to requeue."""


def _claim(conn: sqlite3.Connection, row_id: int,
           now: float | None = None) -> bool:
    import time as _time
    cur = conn.execute(
        "UPDATE send_queue SET status = 'sending', claimed_at = ?"
        " WHERE id = ? AND status = 'queued'",
        (now if now is not None else _time.time(), row_id))
    conn.commit()
    return cur.rowcount == 1


def recover_stuck_claims(conn: sqlite3.Connection, now: float) -> int:
    """Requeue 'sending' rows whose drain died (never active ones).

    Only timestamped claims older than STUCK_SEND_S move; anything
    younger may still be sending, and untimestamped legacy rows stay
    parked rather than risk a duplicate.
    """
    cur = conn.execute("UPDATE send_queue SET status = 'queued',"
                       " claimed_at = NULL WHERE status = 'sending'"
                       " AND claimed_at IS NOT NULL AND claimed_at < ?",
                       (now - STUCK_SEND_S,))
    conn.commit()
    return cur.rowcount


def _pace_wait(conn: sqlite3.Connection, to: str, now: float,
               pace_s: float) -> float:
    row = conn.execute("SELECT last_sent FROM send_pacing WHERE to_number = ?",
                       (to,)).fetchone()
    if row is None:
        return 0.0
    return max(0.0, pace_s - (now - row["last_sent"]))


def _touch_pacing(conn: sqlite3.Connection, to: str, now: float) -> None:
    conn.execute(
        "INSERT INTO send_pacing (to_number, last_sent) VALUES (?, ?)"
        " ON CONFLICT (to_number) DO UPDATE SET last_sent = excluded.last_sent",
        (to, now),
    )
    conn.commit()


def _tracked(payload_json: str) -> int | None:
    """A send_queue row's reminder id, or None when untracked."""
    try:
        rid = json.loads(payload_json or "{}").get("reminder_id")
    except ValueError:
        return None
    return rid if isinstance(rid, int) else None


def _merge_texts(conn: sqlite3.Connection, to: str, head) -> tuple[str, list[int]]:
    """Claim consecutive due text rows after head; join bodies (capped).

    Delivery-tracked rows (reminders) never merge: their payload must
    survive so the drain can record exactly what was submitted.
    """
    from app.whatsapp import SPLIT_AT
    bodies = [json.loads(head["payload_json"]).get("body", "")]
    ids = [head["id"]]
    if _tracked(head["payload_json"]) is not None:
        return "\n\n".join(bodies), ids
    rows = conn.execute(
        "SELECT * FROM send_queue WHERE to_number = ? AND status = 'queued'"
        " AND kind = 'text' AND id > ? ORDER BY id", (to, head["id"])).fetchall()
    for row in rows:
        if row["id"] != ids[-1] + 1:
            break  # only truly consecutive rows merge
        if _tracked(row["payload_json"]) is not None:
            break
        body = json.loads(row["payload_json"]).get("body", "")
        if len("\n\n".join(bodies + [body])) > SPLIT_AT:
            break
        if not _claim(conn, row["id"]):
            break
        bodies.append(body)
        ids.append(row["id"])
    return "\n\n".join(bodies), ids


def check_media(url: str, kind: str, timeout: float = 5.0) -> tuple[bool, str]:
    """HEAD-check a media URL. Returns (ok, detail); never raises."""
    if not (url or "").startswith("http"):
        return False, "not a public http(s) url"
    try:
        resp = httpx.head(url, timeout=timeout, follow_redirects=True)
        ctype = resp.headers.get("content-type", "")
        if resp.status_code != 200:
            return False, f"http {resp.status_code}"
        if kind == "image" and not ctype.startswith("image/"):
            return False, f"content-type {ctype or '?'}"
        return True, ctype or f"http {resp.status_code}"
    except httpx.HTTPError as e:
        return False, repr(e)[:120]


def _send_claimed(sender, kind: str, payload: dict, to: str):
    """Send one claimed row. Returns the provider message id, if any."""
    from app.reports import persist_media
    if kind == "text":
        return sender.send_text(to, payload.get("body", ""))
    if kind == "template":
        if not hasattr(sender, "send_template"):
            raise ValueError("template unsupported on channel")
        return sender.send_template(to, payload.get("template", ""),
                                    payload.get("lang", "en"),
                                    payload.get("params", []))
    if kind == "buttons":
        sender.send_buttons(to, payload.get("body", ""),
                            payload.get("buttons", []))
        return None
    if kind == "list":
        sender.send_list(to, payload.get("body", ""),
                         payload.get("button_text", "Choose"),
                         payload.get("rows", []))
        return None
    if kind == "image":
        persist_media()  # publish the file + DB row before Vonage fetches it
        ok, detail = check_media(payload.get("url", ""), "image")
        log.info("media check image %s -> %s (%s)", payload.get("url", ""),
                 "ok" if ok else "BAD", detail)
        if not ok and detail == "not a public http(s) url":
            raise ValueError(detail)
        log.info("media send image %s", payload.get("url", ""))
        sender.send_image(to, payload.get("url", ""),
                          payload.get("caption", ""))
        return None
    if kind == "file":
        persist_media()  # publish the file + DB row before Vonage fetches it
        ok, detail = check_media(payload.get("url", ""), "file")
        log.info("media check file %s -> %s (%s)", payload.get("url", ""),
                 "ok" if ok else "BAD", detail)
        if not ok and detail == "not a public http(s) url":
            raise ValueError(detail)
        log.info("media send file %s", payload.get("url", ""))
        sender.send_file(to, payload.get("url", ""),
                         payload.get("filename", "sisi-file"),
                         payload.get("caption", ""))
        return None
    raise ValueError(f"unknown send kind {kind!r}")


def _fail_tracked(conn, row, detail: str) -> None:
    """Mark a tracked row's reminder failed (visible, retryable)."""
    from app.reminders import mark_reminder_failed
    rid = _tracked(row["payload_json"])
    if rid is not None:
        mark_reminder_failed(conn, rid, detail)


def _park(conn: sqlite3.Connection, ids: list[int], attempts: int,
          delay: float, now: float) -> None:
    conn.execute(
        f"UPDATE send_queue SET status = 'queued', attempts = ?, next_at = ?"
        f" WHERE id IN ({','.join('?' for _ in ids)})",
        (attempts, now + delay, *ids),
    )
    conn.commit()


def _drop(conn: sqlite3.Connection, ids: list[int]) -> None:
    conn.execute(
        f"DELETE FROM send_queue WHERE id IN ({','.join('?' for _ in ids)})",
        ids,
    )
    conn.commit()


def _fail_media(conn: sqlite3.Connection, to: str, row, channel: str) -> None:
    """Record the failed media for 'resend' and queue the fallback text."""
    payload = json.loads(row["payload_json"])
    conn.execute(
        "INSERT INTO flags (shop_id, key, value) VALUES (?, 'last-failed-media', ?)"
        " ON CONFLICT (shop_id, key) DO UPDATE SET value = excluded.value",
        (to, json.dumps({"kind": row["kind"], "channel": channel, **payload})),
    )
    conn.commit()
    enqueue(conn, to, channel, "text", {"body": _fallback_text()})
    _drop(conn, [row["id"]])
    log.error("media to=%s failed after retries; fallback queued", to)


def _on_rate_limit(conn: sqlite3.Connection, to: str, row, ids: list[int],
                   kind: str, retry_after: float | None, clock,
                   stats: dict) -> None:
    """Park the head rows with backoff, or give up past the 30s budget."""
    attempts = row["attempts"] + 1
    if attempts > len(BACKOFF_S):
        stats["failed"] += 1
        if kind in ("image", "file"):
            _fail_media(conn, to, row, row["channel"])
        else:
            _fail_tracked(conn, row, "rate-limited after retries")
            log.error("text to=%s dropped after 429 retries", to)
            _drop(conn, ids)
        return
    delay = retry_after if retry_after is not None \
        else BACKOFF_S[attempts - 1]
    log.warning("429 to=%s attempt=%d delay=%.1fs", to, attempts, delay)
    _park(conn, ids, attempts, delay, clock())
    stats["rate_limited"] = True


def _on_error(conn: sqlite3.Connection, to: str, row, ids: list[int],
              kind: str, clock, stats: dict, reason: str = "send failed") -> None:
    """Park the head rows with backoff, or give up past the budget."""
    attempts = row["attempts"] + 1
    if attempts > len(BACKOFF_S):
        stats["failed"] += 1
        if kind in ("image", "file"):
            _fail_media(conn, to, row, row["channel"])
        else:
            _fail_tracked(conn, row, reason)
            log.error("text to=%s failed after retries", to, exc_info=True)
            _drop(conn, ids)
        return
    log.warning("send to=%s failed attempt=%d", to, attempts, exc_info=True)
    _park(conn, ids, attempts, BACKOFF_S[attempts - 1], clock())


def drain(conn: sqlite3.Connection, to: str, sender, *,
          clock=time.time, sleep=time.sleep,
          pace_s: float = PACE_S) -> dict:
    """Send every due row for one number, in order. Returns stats."""
    stats = {"sent": 0, "rate_limited": False, "failed": 0}
    start = clock()
    recover_stuck_claims(conn, start)
    from app.reminders import mark_reminder_submitted
    while True:
        now = clock()
        row = _head(conn, to, now)
        if row is None:
            waiting = _waiting(conn, to)
            if waiting is None or clock() - start >= DRAIN_BUDGET_S:
                break
            pause = max(0.0, min(waiting["next_at"] - clock(),
                                 start + DRAIN_BUDGET_S - clock()))
            if pause <= 0:
                continue
            sleep(pause)
            continue
        if not _claim(conn, row["id"], now):
            continue
        wait = _pace_wait(conn, to, clock(), pace_s)
        if wait > 0:
            sleep(wait)
        kind = row["kind"]
        payload = json.loads(row["payload_json"])
        ids = [row["id"]]
        if kind == "text":
            body, ids = _merge_texts(conn, to, row)
            payload = {"body": body}
            rid = _tracked(row["payload_json"])
            if rid is not None:
                payload["reminder_id"] = rid
        if kind == "template" and not hasattr(sender, "send_template"):
            _fail_tracked(conn, row, "template unsupported on channel")
            log.error("template to=%s unsupported on sender; marked failed",
                      to)
            stats["failed"] += 1
            _drop(conn, ids)
            continue
        try:
            provider_id = _send_claimed(sender, kind, payload, to)
        except RateLimited as e:
            _on_rate_limit(conn, to, row, ids, kind, e.retry_after, clock,
                           stats)
            continue  # loop waits for next_at inside budget, cron covers rest
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else 0
            if status == 429:
                _on_rate_limit(conn, to, row, ids, kind,
                               _retry_after_s(e.response), clock, stats)
            else:
                _on_error(conn, to, row, ids, kind, clock, stats,
                          f"HTTP {status}")
            continue
        except Exception as e:
            _on_error(conn, to, row, ids, kind, clock, stats,
                      type(e).__name__)
            continue
        rid = payload.get("reminder_id")
        if isinstance(rid, int):
            mark_reminder_submitted(conn, rid, provider_id or "")
        _drop(conn, ids)
        _touch_pacing(conn, to, clock())
        stats["sent"] += 1
    return stats


def drain_all(conn, sender_for_channel, *, clock=time.time, sleep=time.sleep,
              pace_s: float = PACE_S) -> dict:
    """Drain every number with queued rows (minute-cron backstop)."""
    totals = {"sent": 0, "rate_limited": False, "failed": 0}
    numbers = conn.execute(
        "SELECT DISTINCT to_number, channel FROM send_queue"
        " WHERE status = 'queued'").fetchall()
    for row in numbers:
        try:
            sender = sender_for_channel(row["channel"] or "meta")
        except WrongProviderError as e:
            log.error("drain_all: %s; rows for %s stay parked", e,
                      row["to_number"])
            continue
        stats = drain(conn, row["to_number"], sender,
                      clock=clock, sleep=sleep, pace_s=pace_s)
        totals["sent"] += stats["sent"]
        totals["failed"] += stats["failed"]
        totals["rate_limited"] = totals["rate_limited"] or stats["rate_limited"]
    return totals


def send_immediate(channel: str, to: str, kind: str, payload: dict) -> dict:
    """Enqueue one message and drain it (for paths without an open conn)."""
    from app.db import connect, init_db
    from app.providers import sender_for_channel
    conn = connect()
    try:
        init_db(conn)
        sender = sender_for_channel(channel or "meta")
        if (channel or "meta") in QUEUED_CHANNELS:
            enqueue(conn, to, channel, kind, payload)
            return drain(conn, to, sender)
        _send_claimed(sender, kind, payload, to)
        return {"sent": 1, "rate_limited": False, "failed": 0}
    finally:
        conn.close()


class QueuedSender:
    """Sender interface that queues (strict-limit channels) or passes through."""

    def __init__(self, sender, conn: sqlite3.Connection | None, queued: bool,
                 clock=time.time, sleep=time.sleep,
                 pace_s: float = PACE_S, channel: str = "") -> None:
        self._sender = sender
        self._conn = conn
        self._queued = queued
        self._channel = channel or ("vonage" if queued else "meta")
        self._touched: list[str] = []
        self._clock = clock
        self._sleep = sleep
        self._pace = pace_s
        self._media_n: dict[str, int] = {}

    @classmethod
    def wrap(cls, sender, conn, channel: str, **kw) -> "QueuedSender":
        code = (channel or "meta").strip().lower() or "meta"
        return cls(sender, conn, code in QUEUED_CHANNELS, channel=code, **kw)

    def _remember(self, to: str) -> None:
        if to not in self._touched:
            self._touched.append(to)

    def send_text(self, to: str, body: str) -> None:
        if not self._queued:
            self._sender.send_text(to, body)
            return
        enqueue(self._conn, to, self._channel, "text", {"body": body})
        self._remember(to)

    def send_buttons(self, to: str, body: str, buttons: list[str]) -> None:
        if not self._queued:
            self._sender.send_buttons(to, body, buttons)
            return
        enqueue(self._conn, to, self._channel, "buttons",
                {"body": body, "buttons": buttons})
        self._remember(to)

    def send_list(self, to: str, body: str, button_text: str,
                  rows: list[str]) -> None:
        if not self._queued:
            self._sender.send_list(to, body, button_text, rows)
            return
        enqueue(self._conn, to, self._channel, "list",
                {"body": body, "button_text": button_text, "rows": rows})
        self._remember(to)

    def _media_delay(self, to: str) -> float:
        from app.mediasend import MEDIA_GAP_S, MEDIA_TEXT_DELAY_S

        n = self._media_n.get(to, 0)
        self._media_n[to] = n + 1
        return MEDIA_TEXT_DELAY_S + MEDIA_GAP_S * n

    def _request_media(self, to: str, kind: str, payload: dict) -> None:
        from app.mediasend import request_media_send

        request_media_send(self._conn, to, self._channel, kind, payload,
                           delay_s=self._media_delay(to))
        self._remember(to)

    def send_image(self, to: str, url: str, caption: str = "") -> None:
        if not self._queued:
            self._sender.send_image(to, url, caption)
            return
        self._request_media(to, "image", {"url": url, "caption": caption})

    def send_file(self, to: str, url: str, filename: str,
                  caption: str = "") -> None:
        if not self._queued:
            self._sender.send_file(to, url, filename, caption)
            return
        self._request_media(to, "file",
                            {"url": url, "filename": filename,
                             "caption": caption})

    def mark_read(self, message_id: str) -> bool:
        return self._sender.mark_read(message_id)

    def send_media_item(self, to: str, item: dict) -> None:
        """Enqueue or send media. Never raises — text/menu must keep flowing."""
        try:
            if item.get("kind") == "file":
                self.send_file(to, item.get("url", ""),
                               item.get("filename") or "sisi-report.pdf",
                               item.get("caption") or "")
            else:
                self.send_image(to, item.get("url", ""),
                                item.get("caption") or "")
        except Exception:
            log.warning("send_media_item failed to=%s kind=%s; continuing",
                        to, item.get("kind"), exc_info=True)
            try:
                if self._conn is not None:
                    from app.mediasend import _record_failed_media
                    _record_failed_media(
                        self._conn, to, self._channel,
                        item.get("kind") or "image",
                        item.get("url", ""), item.get("caption") or "",
                        item.get("filename") or "")
            except Exception:
                log.warning("last-failed-media record failed", exc_info=True)

    def flush(self) -> dict:
        totals = {"sent": 0, "rate_limited": False, "failed": 0}
        if not self._queued:
            return totals
        for to in self._touched:
            stats = drain(self._conn, to, self._sender, clock=self._clock,
                          sleep=self._sleep, pace_s=self._pace)
            totals["sent"] += stats["sent"]
            totals["failed"] += stats["failed"]
            totals["rate_limited"] = (totals["rate_limited"]
                                      or stats["rate_limited"])
        return totals
