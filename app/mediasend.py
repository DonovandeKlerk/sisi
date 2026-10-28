"""Background media sender: delayed, spaced, long 429 backoff.

On Modal (SISI_SPAWN=1) media leaves the webhook request entirely: the
message flow spawns the ``send_media`` Modal function and returns 200
at once. The worker waits 3s after the text reply, keeps at least 2s
between sends to the same number (shared modal.Dict lock plus the
SQLite pacing row so local drains stay in step), and backs off
5/10/20/40/60s on 429 honouring Retry-After.

Without Modal (local dev, tests, spawn failure) media falls back to
the per-recipient SQLite queue drained in-process, exactly as before.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time

from app.db import connect, init_db
from app.i18n import CURRENT_LANG, owner_lang, t
from app.sendqueue import RateLimited, drain, enqueue

log = logging.getLogger("sisi.mediasend")

MEDIA_TEXT_DELAY_S = 3.0
MEDIA_GAP_S = 2.0
MEDIA_BACKOFF_S = (5.0, 10.0, 20.0, 40.0, 60.0)
MEDIA_ATTEMPTS = 1 + len(MEDIA_BACKOFF_S)


def short_link(url: str) -> str:
    """Rewrite ``/media/<id>?f=png`` to the prettier ``/media/<id>.png``."""
    base, sep, query = (url or "").partition("?")
    if sep and query == "f=png":
        return base + ".png"
    if sep and query == "f=pdf":
        return base + ".pdf"
    return url or ""


def media_link_lines(items: list[dict] | None) -> list[str]:
    """Public-link lines for every card/PDF in a media list."""
    lines = []
    for item in items or []:
        url = short_link(item.get("url", ""))
        if not url.startswith("http"):
            continue
        if item.get("kind") == "file":
            lines.append(t("link_pdf", url=url))
        else:
            lines.append(t("link_card", url=url))
    return lines


def with_media_links(reply: str, items: list[dict] | None) -> str:
    """Append the always-works media links to a reply (no media: unchanged)."""
    lines = media_link_lines(items)
    if not lines:
        return reply
    return f"{reply}\n\n" + "\n".join(lines)


def spawn_media(payload: dict) -> bool:
    """Fire-and-forget the media worker on Modal. False when unavailable."""
    if os.environ.get("SISI_SPAWN") != "1":
        return False
    try:
        import modal

        modal.Function.from_name("sisi", "send_media").spawn(payload)
    except Exception:
        log.warning("media spawn failed; using the local queue", exc_info=True)
        return False
    return True


def request_media_send(conn: sqlite3.Connection, to: str, channel: str,
                       kind: str, payload: dict, delay_s: float = 3.0) -> str:
    """Send media via the background worker, or queue it when Modal is out.

    Returns "spawned" or "queued".
    """
    job = {"shop_id": to, "channel": channel, "kind": kind,
           "url": payload.get("url", ""),
           "caption": payload.get("caption", ""),
           "filename": payload.get("filename", ""), "delay_s": delay_s}
    if spawn_media(job):
        log.info("media to=%s kind=%s spawned (delay %.0fs)", to, kind,
                 delay_s)
        return "spawned"
    enqueue(conn, to, channel, kind,
            {k: payload[k] for k in ("url", "caption", "filename")
             if k in payload})
    return "queued"


def _sqlite_gap(conn: sqlite3.Connection, to: str, now: float) -> float:
    row = conn.execute("SELECT last_sent FROM send_pacing WHERE to_number = ?",
                       (to,)).fetchone()
    last = float(row["last_sent"]) if row else 0.0
    return max(0.0, MEDIA_GAP_S - (now - last))


def _sqlite_touch(conn: sqlite3.Connection, to: str, now: float) -> None:
    conn.execute("INSERT INTO send_pacing (to_number, last_sent)"
                 " VALUES (?, ?) ON CONFLICT (to_number)"
                 " DO UPDATE SET last_sent = excluded.last_sent",
                 (to, now))
    conn.commit()


def _send_lock() -> object | None:
    name = os.environ.get("SISI_SEND_LOCK", "")
    if not name:
        return None
    try:
        import modal

        return modal.Dict.from_name(name, create_if_missing=True)
    except Exception:
        log.warning("send lock unavailable; SQLite pacing only", exc_info=True)
        return None


def _dict_gap(lock: object, to: str, now: float) -> float:
    if lock is None:
        return 0.0
    try:
        last = float(lock.get(f"sendlock:{to}", 0.0) or 0.0)  # type: ignore[union-attr]
    except Exception:
        log.warning("send lock read failed", exc_info=True)
        return 0.0
    return max(0.0, MEDIA_GAP_S - (now - last))


def _dict_touch(lock: object, to: str, now: float) -> None:
    if lock is None:
        return
    try:
        lock.put(f"sendlock:{to}", now)  # type: ignore[union-attr]
    except Exception:
        log.warning("send lock write failed", exc_info=True)


def _gap_wait(conn: sqlite3.Connection, to: str, now: float) -> float:
    return max(_sqlite_gap(conn, to, now),
               _dict_gap(_send_lock(), to, now))


def _gap_touch(conn: sqlite3.Connection, to: str, now: float) -> None:
    _sqlite_touch(conn, to, now)
    _dict_touch(_send_lock(), to, now)


def _parse_retry_after(response: object) -> float | None:
    try:
        raw = response.headers.get("retry-after")  # type: ignore[union-attr]
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return max(0.0, float(str(raw).strip().rstrip("s")))
    except ValueError:
        return None


def _record_failed_media(conn: sqlite3.Connection, shop_id: str, channel: str,
                         kind: str, url: str, caption: str,
                         filename: str) -> None:
    conn.execute("INSERT INTO flags (shop_id, key, value) VALUES (?, ?, ?)"
                 " ON CONFLICT (shop_id, key) DO UPDATE"
                 " SET value = excluded.value",
                 (shop_id, "last-failed-media",
                  json.dumps({"kind": kind, "channel": channel, "url": url,
                              "caption": caption, "filename": filename})))
    conn.commit()


def send_media_deferred(shop_id: str, channel: str, kind: str, url: str,
                        caption: str = "", filename: str = "",
                        delay_s: float = MEDIA_TEXT_DELAY_S,
                        *, sender: object = None,
                        conn: sqlite3.Connection | None = None,
                        clock=time.time, sleep=time.sleep) -> dict:
    """Background worker body: delay, gap lock, long backoff, then fallback.

    Returns {"sent": 0/1, "attempts": n, "failed": 0/1}. Runs inside the
    ``send_media`` Modal function in production; tests call it directly.
    """
    from app.sendqueue import _send_claimed

    own_conn = conn is None
    if own_conn:
        conn = connect()
        init_db(conn)
    assert conn is not None
    try:
        CURRENT_LANG.set(owner_lang(conn, shop_id))
        if sender is None:
            if (channel or "meta") == "vonage":
                from app.vonage import VonageSender

                sender = VonageSender()
            else:
                from app.whatsapp import get_sender

                sender = get_sender()
        log.info("media worker to=%s kind=%s delay=%.0fs url=%s", shop_id,
                 kind, delay_s, url)
        if delay_s > 0:
            sleep(delay_s)
        payload = {"url": url, "caption": caption, "filename": filename}
        for attempt in range(1, MEDIA_ATTEMPTS + 1):
            wait = _gap_wait(conn, shop_id, clock())
            if wait > 0:
                log.info("media to=%s gap wait %.1fs", shop_id, wait)
                sleep(wait)
            _gap_touch(conn, shop_id, clock())
            try:
                _send_claimed(sender, kind, payload, shop_id)
            except RateLimited as e:
                outcome, retry_after = "429", e.retry_after
            except Exception as e:  # noqa: BLE001 - every failure retries
                response = getattr(e, "response", None)
                status = getattr(response, "status_code", 0) or 0
                if status == 429:
                    outcome = "429"
                    retry_after = _parse_retry_after(response)
                elif response is not None:
                    outcome, retry_after = f"http-{status}", None
                else:
                    outcome, retry_after = type(e).__name__, None
            else:
                _gap_touch(conn, shop_id, clock())
                log.info("media to=%s kind=%s attempt=%d status=sent",
                         shop_id, kind, attempt)
                return {"sent": 1, "attempts": attempt, "failed": 0}
            if attempt >= MEDIA_ATTEMPTS:
                break
            pause = retry_after if retry_after is not None \
                else MEDIA_BACKOFF_S[attempt - 1]
            log.info("media to=%s kind=%s attempt=%d status=%s wait=%.1fs",
                     shop_id, kind, attempt, outcome, pause)
            sleep(pause)
        log.warning("media to=%s kind=%s failed after %d attempts", shop_id,
                    kind, MEDIA_ATTEMPTS)
        _record_failed_media(conn, shop_id, channel, kind, url, caption,
                             filename)
        enqueue(conn, shop_id, channel, "text",
                {"body": t("media_fallback_link")}, commit=False)
        conn.commit()
        drain(conn, shop_id, sender, clock=clock, sleep=sleep)
        return {"sent": 0, "attempts": MEDIA_ATTEMPTS, "failed": 1}
    finally:
        if own_conn:
            conn.close()
