"""FastAPI: GET /health, GET/POST /webhook, dev-only GET /_sim/outbox.

POST /webhook verifies X-Hub-Signature-256, dedupes by WhatsApp message id
(the stable idempotency key: Meta retries reuse it, claimed atomically via
INSERT OR IGNORE), returns 200 fast, and processes each message in a
background task with its own DB connection.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
import traceback

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response

from app.agent import (DEFER_OVERVIEW, handle_photo, handle_text, handle_voice,
                       take_buttons, take_lists, take_media,
                       take_overview_pending)
from app.db import (READ_ONLY, connect, enforce_read_only, init_db,
                    is_transient_db_error, now_iso)
from app.i18n import CURRENT_LANG, STRINGS, detect, owner_lang
from app.i18n import t as _t
from app.vision import VisionError
from app.voice import VoiceError
from app.vonage import VonageSender, download_vonage_media, parse_vonage_webhook
from app.whapi import PENDING_TIPS, parse_whapi_status, parse_whapi_webhook
from app.infobip import (InfobipSender, download_infobip_media,
                         parse_infobip_webhook)
from app.providers import sender_for_channel
from app.whatsapp import (
    FakeSender,
    IncomingMessage,
    download_media,
    get_sender,
    parse_statuses,
    parse_webhook,
    verify_signature,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("sisi")

app = FastAPI(title="Sisi")


UNSUPPORTED_REPLY = STRINGS["en"]["unsupported"]
CONSENT_LINE = STRINGS["en"]["consent_ps"]
MAX_WEBHOOK_BYTES = 256 * 1024

# Per-shop inbound rate limit (sliding window). In-memory: correct for a single
# warm container; back with a shared store if serving from several instances.
RATE_MAX = 30
RATE_WINDOW = 60.0
_rate_hits: dict[str, list[float]] = {}


def _rate_limited(shop_id: str) -> bool:
    now = time.monotonic()
    hits = _rate_hits.setdefault(shop_id, [])
    while hits and hits[0] <= now - RATE_WINDOW:
        hits.pop(0)
    if len(hits) >= RATE_MAX:
        return True
    hits.append(now)
    return False


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/webhook")
def webhook_verify(request: Request):
    q = request.query_params
    token = os.environ.get("VERIFY_TOKEN", "")
    if q.get("hub.mode") == "subscribe" and token and q.get("hub.verify_token") == token:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="verify failed")


@app.post("/webhook")
async def webhook_receive(request: Request, background_tasks: BackgroundTasks):
    raw = await request.body()
    if len(raw) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    secret = os.environ.get("APP_SECRET", "")
    if secret:
        if not verify_signature(raw, request.headers.get("X-Hub-Signature-256"), secret):
            raise HTTPException(status_code=403, detail="bad signature")
    else:
        log.warning("APP_SECRET not set — skipping signature check (local dev only)")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return {"status": "ignored"}
    msgs = parse_webhook(payload)
    statuses = parse_statuses(payload)
    if statuses:
        background_tasks.add_task(_apply_dlrs, statuses)
    if not msgs and not statuses:
        log.warning("meta inbound dropped: no messages keys=%s",
                    sorted(payload.keys()) if isinstance(payload, dict) else "?")
    for msg in msgs:
        if _rate_limited(msg.shop_id):
            log.warning("rate-limited %s", msg.shop_id)
            continue
        background_tasks.add_task(_ack_inbound, msg.channel or "meta",
                                  msg.message_id, msg.shop_id)
        await _dispatch(background_tasks, msg)
    return {"status": "ok"}


def _apply_dlrs(reports: list[tuple[str, str, str]]) -> None:
    """Correlate delivery reports to reminders. Never raises."""
    if not reports:
        return
    try:
        from app.reminders import record_dlr
        conn = connect()
        try:
            init_db(conn)
            for provider_id, outcome, detail in reports:
                try:
                    record_dlr(conn, provider_id, outcome, detail)
                except Exception:
                    log.warning("dlr apply failed id=%s", provider_id,
                                exc_info=True)
        finally:
            conn.close()
    except Exception:
        log.warning("dlr batch failed", exc_info=True)


def _ack_inbound(channel: str, message_id: str, shop_id: str) -> None:
    """Read receipt + typing indicator. Best-effort, runs after the 200."""
    try:
        sender_for_channel(channel or "meta").mark_read(message_id)
    except Exception:
        log.warning("mark-read failed", exc_info=True)
    if (channel or "meta") == "whapi":
        try:
            from app.whapi import WhapiSender
            WhapiSender().send_presence(shop_id)
        except Exception:
            log.warning("presence failed", exc_info=True)



def _send_demo_tip(shop_id: str, message_id: str) -> None:
    """Once-a-day how-to-try tip for strangers. No LLM, no shop setup."""
    from app import demo as _demo
    from app.sendqueue import send_immediate
    conn = connect()
    try:
        init_db(conn)
        if _demo.tip_already_sent(conn, shop_id):
            log.info("demo tip suppressed (already today) %s", shop_id)
            return
        if _already_seen(message_id, shop_id):
            return
        send_immediate("whapi", shop_id, "text", {"body": _demo.tip_text()})
        _demo.mark_tip_sent(conn, shop_id)
        log.info("demo tip sent to %s", shop_id)
    except Exception:
        log.warning("demo tip failed", exc_info=True)
    finally:
        conn.close()


async def _dispatch(background_tasks: BackgroundTasks,
                    msg: IncomingMessage) -> None:
    """Run handle-now in a spawned Modal worker on Modal, else a background task.

    Either way the webhook returns 200 instantly; slow work (vision, voice,
    reports) never blocks the receipt. The spawn never blocks the event
    loop: async spawn when the SDK offers it, else a worker thread.
    """
    if os.environ.get("SISI_SPAWN") == "1":
        try:
            import asyncio
            import modal
            fn = modal.Function.from_name("sisi", "worker")
            spawn_aio = getattr(getattr(fn, "spawn", None), "aio", None)
            if callable(spawn_aio):
                await spawn_aio(msg.__dict__)
            else:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, fn.spawn, msg.__dict__)
            return
        except Exception:
            log.warning("modal spawn failed; using background task", exc_info=True)
    background_tasks.add_task(process_message, msg)


@app.post("/webhook/vonage")
async def webhook_vonage_receive(request: Request, background_tasks: BackgroundTasks):
    # The Vonage sandbox signs nothing inbound, so there is no signature to
    # check here; only the rate limit + body cap apply. Production Vonage
    # apps should verify the request signature or JWT.
    raw = await request.body()
    if len(raw) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return {"status": "ignored"}
    msgs = parse_vonage_webhook(payload)
    if not msgs:
        log.warning("vonage inbound dropped: no messages keys=%s",
                    sorted(payload.keys()) if isinstance(payload, dict) else "?")
    for msg in msgs:
        log.info("vonage inbound id=%s from=%s type=%s", msg.message_id,
                 msg.shop_id, msg.kind)
        if _rate_limited(msg.shop_id):
            log.warning("rate-limited %s", msg.shop_id)
            continue
        background_tasks.add_task(_ack_inbound, msg.channel or "vonage",
                                  msg.message_id, msg.shop_id)
        await _dispatch(background_tasks, msg)
    return {"status": "ok"}


@app.post("/webhook/vonage/status")
async def webhook_vonage_status(request: Request,
                                background_tasks: BackgroundTasks):
    """Vonage delivery receipts -> reminder states. Always 200."""
    try:
        payload = json.loads(await request.body() or b"{}")
    except ValueError:
        return {"status": "ignored"}
    try:
        reports = parse_vonage_status(payload)
        if reports:
            background_tasks.add_task(_apply_dlrs, reports)
    except Exception:
        log.warning("vonage status parse failed", exc_info=True)
    return {"status": "ok"}


def parse_vonage_status(payload: dict) -> list[tuple[str, str, str]]:
    """Pull [(provider_id, outcome, detail)] from a Vonage receipt."""
    if not isinstance(payload, dict):
        return []
    msg = payload.get("message")
    node = msg if isinstance(msg, dict) else payload
    mid = node.get("message_uuid") or payload.get("message_uuid") or ""
    status = str(node.get("status") or payload.get("status") or "").lower()
    if not mid or not status:
        return []
    if status in ("delivered", "read", "seen"):
        return [(mid, "delivered", "")]
    if status in ("failed", "undelivered", "rejected", "expired"):
        err = node.get("error") or {}
        detail = err.get("detail", "") if isinstance(err, dict) else ""
        return [(mid, "failed", detail or f"vonage {status}")]
    return [(mid, "submitted", "")]


@app.post("/webhook/infobip/dlr")
async def webhook_infobip_dlr(request: Request,
                              background_tasks: BackgroundTasks):
    """Delivery reports: logged for ops, always 200 (never retry-triggering).

    Declared before the token route below so /dlr is not swallowed by {token}.
    """
    try:
        payload = json.loads(await request.body() or b"{}")
    except ValueError:
        return {"status": "ignored"}
    try:
        results = payload.get("results", []) if isinstance(payload, dict) else []
        reports: list[tuple[str, str, str]] = []
        if isinstance(results, list):
            for r in results:
                if not isinstance(r, dict):
                    continue
                status = r.get("status") or {}
                name = status.get("name") if isinstance(status, dict) else status
                log.info("infobip dlr id=%s to=%s status=%s", r.get("messageId"),
                         r.get("to"), name)
                mid = r.get("messageId", "")
                upper = str(name or "").upper()
                if not mid:
                    continue
                if "DELIVER" in upper or upper in ("READ", "SEEN"):
                    reports.append((mid, "delivered", ""))
                elif "PENDING" in upper or "SENT" in upper or "PAID" in upper:
                    reports.append((mid, "submitted", ""))
                else:
                    desc = status.get("description", "") \
                        if isinstance(status, dict) else ""
                    reports.append((mid, "failed", desc or str(name or "failed")))
        if reports:
            background_tasks.add_task(_apply_dlrs, reports)
    except Exception:
        log.warning("infobip dlr parse failed", exc_info=True)
    return {"status": "ok"}


@app.post("/webhook/infobip/{token}")
async def webhook_infobip_receive(request: Request,
                                  background_tasks: BackgroundTasks,
                                  token: str):
    # Infobip signs nothing on sender "Forward to HTTP" posts, so auth is a
    # shared secret embedded in the configured URL path:
    #   https://<app>/webhook/infobip/<INFOBIP_WEBHOOK_TOKEN>
    # compared in constant time. Fails closed when unconfigured. The handler
    # returns 200 instantly (dispatch is async) so Infobip never retries a
    # receipt; redelivery is still safe via the messages_seen messageId key.
    expected = os.environ.get("INFOBIP_WEBHOOK_TOKEN", "").strip()
    if not expected or not hmac.compare_digest(token.strip(), expected):
        # Lengths only: distinguishes "secret empty" from "value mismatch"
        # (e.g. pasted whitespace or truncation) without logging secrets.
        log.warning("infobip webhook 403: secret %s len=%d got len=%d",
                    "set" if expected else "UNSET", len(expected), len(token))
        raise HTTPException(status_code=403, detail="bad webhook token")
    raw = await request.body()
    if len(raw) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return {"status": "ignored"}
    msgs = parse_infobip_webhook(payload)
    if not msgs:
        log.warning("infobip inbound dropped: no messages keys=%s",
                    sorted(payload.keys()) if isinstance(payload, dict) else "?")
    for msg in msgs:
        log.info("infobip inbound id=%s from=%s type=%s", msg.message_id,
                 msg.shop_id, msg.kind)
        if _rate_limited(msg.shop_id):
            log.warning("rate-limited %s", msg.shop_id)
            continue
        background_tasks.add_task(_ack_inbound, msg.channel or "infobip",
                                  msg.message_id, msg.shop_id)
        await _dispatch(background_tasks, msg)
    return {"status": "ok"}


@app.post("/webhook/whapi/{token}")
async def webhook_whapi_receive(request: Request,
                                background_tasks: BackgroundTasks,
                                token: str):
    # Whapi documents no signature scheme, so auth is a shared secret
    # embedded in the configured URL path (same pattern as Infobip):
    #   https://<app>/webhook/whapi/<WHAPI_WEBHOOK_TOKEN>
    # compared in constant time. Fails closed when unconfigured. One
    # route serves messages + statuses (Whapi posts all events to the
    # same channel webhook URL); channel, allowlist and loop guards
    # live in the parser. Always 200 instantly so Whapi never retries
    # a receipt; redelivery is still safe via the messages_seen key.
    expected = os.environ.get("WHAPI_WEBHOOK_TOKEN", "").strip()
    if not expected or not hmac.compare_digest(token.strip(), expected):
        log.warning("whapi webhook 403: secret %s len=%d got len=%d",
                    "set" if expected else "UNSET", len(expected), len(token))
        raise HTTPException(status_code=403, detail="bad webhook token")
    raw = await request.body()
    if len(raw) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return {"status": "ignored"}
    try:
        reports = parse_whapi_status(payload)
        if reports:
            background_tasks.add_task(_apply_dlrs, reports)
    except Exception:
        log.warning("whapi status parse failed", exc_info=True)
    msgs = parse_whapi_webhook(payload)
    tips = list(PENDING_TIPS)
    PENDING_TIPS.clear()
    if not msgs and not tips and not isinstance(payload.get("event"), dict):
        log.warning("whapi inbound dropped: no messages keys=%s",
                    sorted(payload.keys()) if isinstance(payload, dict) else "?")
    for shop_id, mid in tips:
        if _rate_limited(shop_id):
            continue
        background_tasks.add_task(_send_demo_tip, shop_id, mid)
    for msg in msgs:
        log.info("whapi inbound id=%s from=%s type=%s", msg.message_id,
                 msg.shop_id, msg.kind)
        if _rate_limited(msg.shop_id):
            log.warning("rate-limited %s", msg.shop_id)
            continue
        # Typing indicator goes out with the 200; the reply follows.
        background_tasks.add_task(_ack_inbound, msg.channel or "whapi",
                                  msg.message_id, msg.shop_id)
        await _dispatch(background_tasks, msg)
    return {"status": "ok"}


def _already_seen(message_id: str, shop_id: str) -> bool:
    conn = connect()
    try:
        init_db(conn)
        if READ_ONLY.get():
            enforce_read_only(conn)
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages_seen (message_id, shop_id) VALUES (?, ?)",
            (message_id, shop_id),
        )
        conn.commit()
        return cur.rowcount == 0
    finally:
        conn.close()


def _with_consent(conn, shop_id: str, reply: str) -> str:
    """Append the POPIA consent line on a shop's first-ever reply."""
    from app.db import read_only as _ro
    if _ro():
        return reply  # testing mode: consent stamp can wait for live
    row = conn.execute("SELECT consent_given_at FROM shops WHERE shop_id = ?",
                       (shop_id,)).fetchone()
    if row is not None and row["consent_given_at"] is not None:
        return reply
    conn.execute("INSERT OR IGNORE INTO shops (shop_id, name) VALUES (?, ?)",
                 (shop_id, f"Shop {shop_id}"))
    conn.execute("UPDATE shops SET consent_given_at = ? WHERE shop_id = ?",
                 (now_iso(), shop_id))
    conn.commit()
    return reply + "\n\n" + _t("consent_ps")


def _download_msg_media(msg: IncomingMessage, label: str) -> bytes | None:
    """Fetch message media for any channel. None when the message has no media ref."""
    if (msg.channel or "meta") == "whapi":
        # Whapi media ids are NOT Meta graph ids: never fall through to
        # download_media. The Auto-Download link (when present) is only
        # a fallback inside the Whapi downloader.
        if not msg.media_id and not msg.media_url:
            return None
        from app.whapi import download_whapi_media
        return download_whapi_media(msg.media_id or "", msg.media_url)
    if msg.media_url:
        if (msg.channel or "meta") == "infobip":
            return download_infobip_media(msg.media_url)
        return download_vonage_media(msg.media_url)
    if msg.media_id:
        return download_media(msg.media_id, label)
    return None


FRIENDLY_ERROR = STRINGS["en"]["friendly_error"]


def _slow_line(conn, msg: IncomingMessage) -> str | None:
    """Busy indicator for slow work. None when the reply will be instant."""
    from app.agent import slow_status
    if msg.kind == "media:audio":
        return _t("busy_voice")
    if msg.kind == "media:image":
        if (msg.caption or "").strip().lower() == "logo":
            return _t("busy_logo")
        from app.setup import setup_step
        from app.tools import resolve_sender
        owner, staff = resolve_sender(conn, msg.shop_id)
        if staff is None and setup_step(conn, owner) == 4:
            return _t("busy_logo")
        return _t("busy_photo")
    if msg.kind == "text" and msg.text:
        return slow_status(msg.text)
    return None


def _send_try_again_soon(msg: IncomingMessage) -> None:
    """Direct 'try again in a moment' reply that needs no database.

    send_immediate and QueuedSender both open a DB connection, which is
    exactly what is down — so send via the channel sender directly.
    Language comes from the message text when the owner's saved
    language was never loaded (connect died first); an already-set
    CURRENT_LANG (owner_lang won the race) is kept.
    """
    CURRENT_LANG.set(detect(msg.text, default=CURRENT_LANG.get()))
    sender_for_channel(msg.channel or "meta").send_text(
        msg.shop_id, _t("try_again_soon"))


def _transient_fallback(msg: IncomingMessage, err: Exception) -> None:
    """One warning + a try-again-soon reply. Never raises."""
    log.warning("process_message db unavailable (%s)", err)
    try:
        _send_try_again_soon(msg)
    except Exception:
        log.warning("try-again-soon send failed", exc_info=True)


def process_message(msg: IncomingMessage) -> None:
    """Background worker: one message, own connection, never raises."""
    CURRENT_LANG.set("en")
    # Read-only testing: the Whapi channel must not mutate business
    # records until database durability is proven. Explicit unlock only:
    # WHAPI_ALLOW_WRITES=1. Other channels are unaffected.
    ro = (msg.channel or "meta") == "whapi" \
        and os.environ.get("WHAPI_ALLOW_WRITES", "") != "1"
    # Demo join-code shops must mutate their own sandbox even when the
    # Whapi channel is otherwise read-only for real shops.
    if ro and (msg.channel or "meta") == "whapi":
        try:
            from app import demo as _demo
            matched, _ = _demo.extract_join(msg.text)
            if matched:
                ro = False
            else:
                _c = connect()
                try:
                    init_db(_c)
                    if _demo.is_demo_shop(_c, msg.shop_id):
                        ro = False
                finally:
                    _c.close()
        except Exception:
            log.warning("demo write-unlock check failed", exc_info=True)
    token = READ_ONLY.set(ro)
    try:
        log.info("handling id=%s channel=%s kind=%s shop=%s%s",
                 msg.message_id, msg.channel, msg.kind, msg.shop_id,
                 " READ-ONLY" if ro else "")
        if _already_seen(msg.message_id, msg.shop_id):
            log.info("duplicate %s ignored", msg.message_id)
            return
        sender = sender_for_channel(msg.channel or "meta")
        try:
            _handle_message(sender, msg)
        except Exception as e:
            if is_transient_db_error(e):
                # Short outage: connect() already retried, and the
                # DB-backed error report + queued fallback would fail
                # too — reply direct instead.
                _transient_fallback(msg, e)
                return
            detail = traceback.format_exc()
            log.error("process_message failed:\n%s", detail)
            try:
                from app.tools import record_error
                _dbg = connect()
                try:
                    init_db(_dbg)
                    record_error(
                        _dbg, msg.shop_id,
                        f"{msg.channel or 'meta'}/{msg.kind}/"
                        f"{msg.message_id}", detail)
                finally:
                    _dbg.close()
            except Exception:
                log.error("error report failed:\n%s", traceback.format_exc())
            try:
                from app.sendqueue import send_immediate
                send_immediate(msg.channel or "meta", msg.shop_id, "text",
                               {"body": _t("friendly_error")})
            except Exception:
                log.error("friendly fallback failed:\n%s", traceback.format_exc())
    except Exception as e:
        # Covers _already_seen's connect (outside the inner try): a dead
        # DB there must still tell the user to try again, not go silent.
        if is_transient_db_error(e):
            _transient_fallback(msg, e)
        else:
            log.error("process_message failed:\n%s", traceback.format_exc())
    finally:
        READ_ONLY.reset(token)


def _handle_message(sender, msg: IncomingMessage) -> None:
    """Handle one message; raises to process_message for the friendly fallback."""
    from app.reports import ensure_dirs
    from app.sendqueue import QueuedSender
    ensure_dirs()
    defer_token = DEFER_OVERVIEW.set(True)
    conn = connect()
    try:
        init_db(conn)
        if READ_ONLY.get():
            enforce_read_only(conn)
        from app.tools import ensure_shop
        ensure_shop(conn, msg.shop_id)
        channel = msg.channel or "meta"
        # Scheduled routing (reminders, daily plan) follows the last
        # inbound channel — except Whapi testing, which must never
        # silently flip it. Explicit switch only: WHAPI_FOLLOW_CHANNEL=1
        # (and only when writes are allowed at all).
        from app.db import read_only as _ro
        if not _ro() and (channel != "whapi"
                          or os.environ.get("WHAPI_FOLLOW_CHANNEL", "") == "1"):
            conn.execute("UPDATE shops SET channel = ? WHERE shop_id = ?",
                         (channel, msg.shop_id))
            conn.commit()
        else:
            log.info("whapi inbound: shops.channel left unchanged")
        CURRENT_LANG.set(detect(msg.text, default=owner_lang(conn, msg.shop_id)))
        out = QueuedSender.wrap(sender, conn, channel)

        # --- join-code demo gate (no LLM) ---------------------------------
        demo_handled = False
        if (msg.channel or "meta") == "whapi":
            from app import demo as _demo
            matched, join_lang = _demo.extract_join(msg.text)
            is_demo = _demo.is_demo_shop(conn, msg.shop_id)
            used_now = _demo.demo_msgs_used(conn, msg.shop_id) if is_demo else 0
            if matched and (not is_demo or used_now >= _demo.msg_limit()):
                if not is_demo and _demo.cap_reached(conn):
                    out.send_text(msg.shop_id, _demo.cap_full_text())
                    out.flush()
                    return
                _demo.provision_demo(conn, msg.shop_id, join_lang)
                _demo.increment_demo_msg(conn, msg.shop_id)
                CURRENT_LANG.set(join_lang)
                reply = _demo.start_demo_reply(conn, msg.shop_id, join_lang)
                buttons = None
                demo_handled = True
            elif matched and is_demo:
                # Already in an active demo: refresh welcome + menu, no LLM.
                _demo.increment_demo_msg(conn, msg.shop_id)
                lang = owner_lang(conn, msg.shop_id) or join_lang
                CURRENT_LANG.set(lang)
                reply = _demo.start_demo_reply(conn, msg.shop_id, lang)
                buttons = None
                demo_handled = True
            elif is_demo:
                used = _demo.increment_demo_msg(conn, msg.shop_id)
                limit = _demo.msg_limit()
                if used > limit:
                    # Past the limit: polite close at most once a day.
                    if _demo.exhausted_notice_sent_today(conn, msg.shop_id):
                        log.info("demo exhausted silent %s used=%s",
                                 msg.shop_id, used)
                        return
                    lang = owner_lang(conn, msg.shop_id)
                    out.send_text(msg.shop_id, _demo.exhausted_text(lang))
                    out.flush()
                    _demo.mark_exhausted_notice(conn, msg.shop_id)
                    return
                if used == limit:
                    # 10th message: handle normally, then append closing.
                    pass

        if not demo_handled:
            try:
                line = _slow_line(conn, msg)
                if line:
                    out.send_text(msg.shop_id, line)
                    out.flush()
            except Exception:
                log.warning("status line failed", exc_info=True)
        from app.reminders import parse_msg_ts
        sent_at = parse_msg_ts(msg.timestamp)
        if demo_handled:
            pass  # reply/buttons already set
        elif msg.kind == "media:audio":
            try:
                audio = _download_msg_media(msg, "voice note")
                if audio is None:
                    out.send_text(msg.shop_id, _t("empty_voice"))
                    out.flush()
                    return
                _, reply, buttons = handle_voice(conn, msg.shop_id, audio,
                                                 ".ogg", sent_at)
            except VoiceError as e:
                out.send_text(msg.shop_id, str(e))
                out.flush()
                return
        elif msg.kind == "media:image":
            try:
                image = _download_msg_media(msg, "photo")
                if image is None:
                    out.send_text(msg.shop_id, _t("empty_photo"))
                    out.flush()
                    return
                _, reply, buttons = handle_photo(conn, msg.shop_id, image,
                                                 msg.caption or "", sent_at)
            except (VisionError, VoiceError) as e:
                out.send_text(msg.shop_id, str(e))
                out.flush()
                return
        elif msg.kind == "media:video":
            reply, buttons = _t("unsupported_video"), None
        elif msg.kind == "media:document":
            reply, buttons = _t("unsupported_doc"), None
        elif msg.text is None:
            reply, buttons = _t("unsupported"), None
        else:
            reply, buttons = handle_text(conn, msg.shop_id, msg.text,
                                         sent_at)
        items = take_media(msg.shop_id)
        from app.mediasend import with_media_links

        reply = with_media_links(reply, items)
        reply = _with_consent(conn, msg.shop_id, reply)
        from app.agent import _normalize as _agent_word
        if _agent_word(msg.text or "") != "resend":
            # Resends return the stored reply verbatim; storing them
            # again would let a media-resend ack clobber text recovery.
            # Same normalization as the agent route, so filler-word
            # variants ("resend please") are excluded too.
            from app.tools import remember_last_reply
            remember_last_reply(conn, msg.shop_id, reply)
        for item in [i for i in items if i.get("first")]:
            out.send_media_item(msg.shop_id, item)
        if buttons:
            out.send_buttons(msg.shop_id, reply, buttons)
        else:
            out.send_text(msg.shop_id, reply)
        for btn in take_buttons(msg.shop_id):
            out.send_buttons(msg.shop_id, btn["body"], btn["buttons"])
        for lst in take_lists(msg.shop_id):
            out.send_list(msg.shop_id, lst["body"], lst["button_text"],
                          lst["rows"])
        for item in [i for i in items if not i.get("first")]:
            out.send_media_item(msg.shop_id, item)
        # Flush the task reply first so the owner sees it immediately.
        out.flush()

        # Deferred overview card + menu (post-task or demo welcome).
        if take_overview_pending(msg.shop_id):
            try:
                from app.overview import queue_overview_and_menu
                fallback = queue_overview_and_menu(conn, msg.shop_id)
                if fallback:
                    out.send_text(msg.shop_id, fallback)
                for item in take_media(msg.shop_id):
                    out.send_media_item(msg.shop_id, item)
                for lst in take_lists(msg.shop_id):
                    out.send_list(msg.shop_id, lst["body"], lst["button_text"],
                                  lst["rows"])
                out.flush()
            except Exception:
                log.warning("deferred overview failed", exc_info=True)

        # After the 10th demo message, append the closing note.
        try:
            from app import demo as _demo
            if _demo.is_demo_shop(conn, msg.shop_id):
                used = _demo.demo_msgs_used(conn, msg.shop_id)
                if used >= _demo.msg_limit() and not _demo.exhausted_notice_sent_today(
                        conn, msg.shop_id):
                    lang = owner_lang(conn, msg.shop_id)
                    out.send_text(msg.shop_id, _demo.exhausted_text(lang))
                    out.flush()
                    _demo.mark_exhausted_notice(conn, msg.shop_id)
        except Exception:
            log.warning("demo close note failed", exc_info=True)
    finally:
        conn.close()
        DEFER_OVERVIEW.reset(defer_token)
    log.info("%s -> %s (%s)", msg.shop_id, msg.message_id,
             "buttons" if buttons else "text")


@app.get("/media/{token}")
def serve_media(token: str, f: str = "png"):
    """Serve a report PNG/PDF by its unguessable token (?f=png|pdf or .suffix).

    Bytes come from the shared Dict first (cross-container safe), then the
    shared Volume path; no auth so providers and owners can fetch them.
    """
    from app.reports import get_report_by_token, media_get, refresh_media
    from app.reports import row_file_for

    refresh_media()  # pick up files committed by worker containers first
    if "." in token:
        token, _, suffix = token.rpartition(".")
        if suffix not in ("png", "pdf"):
            raise HTTPException(status_code=404, detail="not found")
        f = suffix
    if f not in ("png", "pdf"):
        raise HTTPException(status_code=404, detail="not found")
    media = "application/pdf" if f == "pdf" else "image/png"
    entry = media_get(token)
    if entry and entry.get(f):
        log.info("media dict hit token=%s", token)
        return Response(entry[f], media_type=media, headers={
            "Content-Disposition":
                f"attachment; filename=\"{entry.get('name') or f'{token}.{f}'}\""})
    conn = connect()
    try:
        init_db(conn)
        row = get_report_by_token(conn, token)
    finally:
        conn.close()
    path = row_file_for(row, f) if row is not None else ""
    if not path or not os.path.exists(path):
        log.warning("media miss token=%s", token)
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type=media,
                        filename=path.rsplit("/", 1)[-1])


@app.get("/_sim/outbox")
def sim_outbox(shop: str):
    """Dev-only: take queued FakeSender messages for one shop. 404 when live."""
    sender = get_sender()
    if not isinstance(sender, FakeSender):
        raise HTTPException(status_code=404, detail="live sender active")
    return {"messages": sender.pop_shop(shop)}


@app.post("/_sim/voice")
async def sim_voice(request: Request, shop: str):
    """Dev-only: run raw audio bytes through the voice pipeline. 404 when live.

    POST raw audio (ogg/opus or wav) with ?shop=<id>; answers synchronously
    with the transcript, reply, and buttons. No signature needed (dev only).
    """
    if not isinstance(get_sender(), FakeSender):
        raise HTTPException(status_code=404, detail="live sender active")
    raw = await request.body()
    ctype = (request.headers.get("Content-Type") or "").lower()
    suffix = ".wav" if "wav" in ctype else ".ogg"
    conn = connect()
    try:
        init_db(conn)
        transcript, reply, buttons = handle_voice(conn, shop, raw, suffix)
    finally:
        conn.close()
    return {"transcript": transcript, "reply": reply, "buttons": buttons or []}


@app.post("/_admin/seed")
def admin_seed(token: str, shop: str):
    """Reseed the fictional demo shop. Guarded by VERIFY_TOKEN; fails closed."""
    expected = os.environ.get("VERIFY_TOKEN", "")
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="forbidden")
    from app.setup import skip_setup
    from seed.seed_demo import seed_demo
    from seed.seed_random import DEMO_NUMBER, seed_random_shop

    conn = connect()
    try:
        init_db(conn)
        if shop == DEMO_NUMBER:
            seed_random_shop(shop, conn)
        else:
            seed_demo(shop, conn)
        skip_setup(conn, shop)
        message = "Demo shop reseeded."
    finally:
        conn.close()
    return {"status": "ok", "shop": shop, "message": message}


@app.post("/_sim/photo")
async def sim_photo(request: Request, shop: str):
    """Dev-only: run raw photo bytes through the expiry pipeline. 404 when live."""
    if not isinstance(get_sender(), FakeSender):
        raise HTTPException(status_code=404, detail="live sender active")
    raw = await request.body()
    conn = connect()
    try:
        init_db(conn)
        findings, reply, buttons = handle_photo(conn, shop, raw)
    finally:
        conn.close()
    return {"findings": findings, "reply": reply, "buttons": buttons or []}
