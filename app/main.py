"""FastAPI: GET /health, GET/POST /webhook, dev-only GET /_sim/outbox.

POST /webhook verifies X-Hub-Signature-256, dedupes by WhatsApp message id
(the stable idempotency key: Meta retries reuse it, claimed atomically via
INSERT OR IGNORE), returns 200 fast, and processes each message in a
background task with its own DB connection.
"""

from __future__ import annotations

import json
import logging
import os
import traceback

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.agent import handle_text, handle_voice
from app.db import connect, init_db
from app.voice import VoiceError
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

UNSUPPORTED_REPLY = "I can only read text for now — photos and voice notes are coming soon."


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
    for msg in parse_webhook(payload):
        background_tasks.add_task(process_message, msg)
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


def process_message(msg: IncomingMessage) -> None:
    """Background worker: one message, own connection, never raises."""
    try:
        if _already_seen(msg.message_id, msg.shop_id):
            log.info("duplicate %s ignored", msg.message_id)
            return
        sender = get_sender()
        conn = connect()
        try:
            init_db(conn)
            if msg.kind == "media:audio":
                if not msg.media_id:
                    sender.send_text(msg.shop_id, "That voice note came out empty. Send it again?")
                    return
                try:
                    audio = download_media(msg.media_id)
                    _, reply, buttons = handle_voice(conn, msg.shop_id, audio, ".ogg")
                except VoiceError as e:
                    sender.send_text(msg.shop_id, str(e))
                    return
            elif msg.text is None:
                sender.send_text(msg.shop_id, UNSUPPORTED_REPLY)
                return
            else:
                reply, buttons = handle_text(conn, msg.shop_id, msg.text)
        finally:
            conn.close()
        if buttons:
            sender.send_buttons(msg.shop_id, reply, buttons)
        else:
            sender.send_text(msg.shop_id, reply)
        log.info("%s -> %s (%s)", msg.shop_id, msg.message_id,
                 "buttons" if buttons else "text")
    except Exception:
        log.error("process_message failed:\n%s", traceback.format_exc())


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
