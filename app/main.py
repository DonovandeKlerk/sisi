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
from fastapi.responses import FileResponse, PlainTextResponse

from app.agent import handle_photo, handle_text, handle_voice, take_media
from app.db import connect, init_db, now_iso
from app.i18n import CURRENT_LANG, STRINGS, detect, owner_lang
from app.i18n import t as _t
from app.vision import VisionError
from app.voice import VoiceError
from app.vonage import VonageSender, download_vonage_media, parse_vonage_webhook
from app.whatsapp import (
    FakeSender,
    IncomingMessage,
    download_media,
    get_sender,
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
    if not msgs:
        log.warning("meta inbound dropped: no messages keys=%s",
                    sorted(payload.keys()) if isinstance(payload, dict) else "?")
    for msg in msgs:
        if _rate_limited(msg.shop_id):
            log.warning("rate-limited %s", msg.shop_id)
            continue
        try:
            get_sender().mark_read(msg.message_id)
        except Exception:
            log.warning("mark-read failed", exc_info=True)
        _dispatch(background_tasks, msg)
    return {"status": "ok"}


def _dispatch(background_tasks: BackgroundTasks, msg: IncomingMessage) -> None:
    """Run handle-now in a spawned Modal worker on Modal, else a background task.

    Either way the webhook returns 200 instantly; slow work (vision, voice,
    reports) never blocks the receipt.
    """
    if os.environ.get("SISI_SPAWN") == "1":
        try:
            import modal
            modal.Function.from_name("sisi", "worker").spawn(msg.__dict__)
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
        try:
            VonageSender().mark_read(msg.message_id)
        except Exception:
            log.warning("mark-read failed", exc_info=True)
        _dispatch(background_tasks, msg)
    return {"status": "ok"}


@app.post("/webhook/vonage/status")
async def webhook_vonage_status():
    return {"status": "ok"}


def _already_seen(message_id: str, shop_id: str) -> bool:
    conn = connect()
    try:
        init_db(conn)
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
    """Fetch message media for either channel. None when the message has no media ref."""
    if msg.media_url:
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


def process_message(msg: IncomingMessage) -> None:
    """Background worker: one message, own connection, never raises."""
    CURRENT_LANG.set("en")
    try:
        log.info("handling id=%s channel=%s kind=%s shop=%s", msg.message_id,
                 msg.channel, msg.kind, msg.shop_id)
        if _already_seen(msg.message_id, msg.shop_id):
            log.info("duplicate %s ignored", msg.message_id)
            return
        sender = VonageSender() if msg.channel == "vonage" else get_sender()
        try:
            _handle_message(sender, msg)
        except Exception:
            log.error("process_message failed:\n%s", traceback.format_exc())
            try:
                from app.sendqueue import send_immediate
                send_immediate(msg.channel or "meta", msg.shop_id, "text",
                               {"body": _t("friendly_error")})
            except Exception:
                log.error("friendly fallback failed:\n%s", traceback.format_exc())
    except Exception:
        log.error("process_message failed:\n%s", traceback.format_exc())


def _handle_message(sender, msg: IncomingMessage) -> None:
    """Handle one message; raises to process_message for the friendly fallback."""
    from app.reports import ensure_dirs
    from app.sendqueue import QueuedSender
    ensure_dirs()
    conn = connect()
    try:
        init_db(conn)
        from app.tools import ensure_shop
        ensure_shop(conn, msg.shop_id)
        channel = msg.channel or "meta"
        conn.execute("UPDATE shops SET channel = ? WHERE shop_id = ?",
                     (channel, msg.shop_id))
        conn.commit()
        CURRENT_LANG.set(detect(msg.text, default=owner_lang(conn, msg.shop_id)))
        out = QueuedSender.wrap(sender, conn, channel)
        try:
            line = _slow_line(conn, msg)
            if line:
                out.send_text(msg.shop_id, line)
                out.flush()
        except Exception:
            log.warning("status line failed", exc_info=True)
        if msg.kind == "media:audio":
            try:
                audio = _download_msg_media(msg, "voice note")
                if audio is None:
                    out.send_text(msg.shop_id, _t("empty_voice"))
                    out.flush()
                    return
                _, reply, buttons = handle_voice(conn, msg.shop_id, audio, ".ogg")
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
                                                 msg.caption or "")
            except (VisionError, VoiceError) as e:
                out.send_text(msg.shop_id, str(e))
                out.flush()
                return
        elif msg.text is None:
            reply, buttons = _t("unsupported"), None
        else:
            reply, buttons = handle_text(conn, msg.shop_id, msg.text)
        items = take_media(msg.shop_id)
        from app.mediasend import with_media_links

        reply = with_media_links(reply, items)
        reply = _with_consent(conn, msg.shop_id, reply)
        for item in [i for i in items if i.get("first")]:
            out.send_media_item(msg.shop_id, item)
        if buttons:
            out.send_buttons(msg.shop_id, reply, buttons)
        else:
            out.send_text(msg.shop_id, reply)
        for item in [i for i in items if not i.get("first")]:
            out.send_media_item(msg.shop_id, item)
        out.flush()
    finally:
        conn.close()
    log.info("%s -> %s (%s)", msg.shop_id, msg.message_id,
             "buttons" if buttons else "text")


@app.get("/media/{token}")
def serve_media(token: str, f: str = "png"):
    """Serve a report PNG/PDF by its unguessable token (?f=png|pdf or .suffix).

    Files live on the shared Volume (never expire, so links and old
    reports keep working); no auth so Vonage and owners can fetch them.
    """
    from app.reports import get_report_by_token, refresh_media

    refresh_media()  # pick up files committed by worker containers first
    if "." in token:
        token, _, suffix = token.rpartition(".")
        if suffix not in ("png", "pdf"):
            raise HTTPException(status_code=404, detail="not found")
        f = suffix
    if f not in ("png", "pdf"):
        raise HTTPException(status_code=404, detail="not found")
    conn = connect()
    try:
        init_db(conn)
        row = get_report_by_token(conn, token)
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    path = row["pdf_path"] if f == "pdf" else row["png_path"]
    if not path:
        raise HTTPException(status_code=404, detail="not found")
    media = "application/pdf" if f == "pdf" else "image/png"
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
