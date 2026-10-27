"""Vonage Messages API (sandbox) channel for WhatsApp.

Same internal contract as the Meta path: inbound payloads normalize to
`IncomingMessage` (with channel="vonage") and flow through the same agent,
voice, and vision handlers. Replies go out via `VonageSender`.

Config (env, never logged): VONAGE_API_KEY, VONAGE_API_SECRET,
VONAGE_WHATSAPP_FROM (default 14157386102, the sandbox number).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re

import httpx

from app.net import call_with_retries
from app.voice import VoiceError
from app.whatsapp import MAX_TEXT_LEN, IncomingMessage, WhatsAppSender

log = logging.getLogger("sisi.vonage")

VONAGE_MESSAGES_URL = "https://messages-sandbox.nexmo.com/v1/messages"
DEFAULT_WHATSAPP_FROM = "14157386102"


def _digits(number: str) -> str:
    return re.sub(r"\D", "", number or "")


def _split_text(body: str, limit: int = MAX_TEXT_LEN) -> list[str]:
    """Split a long reply on word boundaries; hard-split pathological words."""
    chunks, current = [], ""
    for word in (body or "").split():
        if len(word) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks += [word[i:i + limit] for i in range(0, len(word), limit)]
        elif len(current) + len(word) + 1 > limit:
            chunks.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        chunks.append(current)
    return chunks


class VonageSender(WhatsAppSender):
    """Send via the Vonage Messages sandbox. Same interface as CloudApiSender."""

    def __init__(self, api_key: str | None = None, api_secret: str | None = None,
                 whatsapp_from: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("VONAGE_API_KEY", "")
        self.api_secret = api_secret or os.environ.get("VONAGE_API_SECRET", "")
        self.whatsapp_from = (
            whatsapp_from or os.environ.get("VONAGE_WHATSAPP_FROM", "") or DEFAULT_WHATSAPP_FROM
        )

    def _post_text(self, to: str, body: str) -> None:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("VONAGE_API_KEY/VONAGE_API_SECRET are not set")
        data = {"from": self.whatsapp_from, "to": _digits(to), "channel": "whatsapp",
                "message_type": "text", "text": body}

        def send():
            resp = httpx.post(VONAGE_MESSAGES_URL, auth=(self.api_key, self.api_secret),
                              json=data, timeout=15)
            resp.raise_for_status()
            return resp.json()

        call_with_retries(send)

    def send_text(self, to: str, body: str) -> None:
        for chunk in _split_text(body):
            self._post_text(to, chunk)

    def send_buttons(self, to: str, body: str, buttons: list[str]) -> None:
        options = " / ".join(buttons[:3])
        self.send_text(to, f"{body}\n\nReply: {options}")


def download_vonage_media(url: str) -> bytes:
    """Fetch Vonage-hosted media bytes with basic auth. Raises VoiceError."""
    key = os.environ.get("VONAGE_API_KEY", "")
    secret = os.environ.get("VONAGE_API_SECRET", "")
    if not key or not secret:
        raise VoiceError("I couldn't fetch that file. Please send it again?")

    def fetch() -> bytes:
        resp = httpx.get(url, auth=(key, secret), timeout=30, follow_redirects=True)
        resp.raise_for_status()
        return resp.content

    try:
        return call_with_retries(fetch)
    except httpx.HTTPError as e:
        log.warning("vonage media download failed: %r", e)
        raise VoiceError("I couldn't fetch that file. Please send it again?") from e


def _sender_number(payload: dict) -> str:
    frm = payload.get("from") or {}
    if isinstance(frm, dict):
        return str(frm.get("number", ""))
    return str(frm)


def _message_id(payload: dict) -> str:
    mid = payload.get("message_uuid", "")
    if mid:
        return str(mid)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    return f"vonage-{digest}"


def parse_vonage_webhook(payload: dict) -> list[IncomingMessage]:
    """Normalize a Vonage Messages inbound payload to internal messages."""
    if not isinstance(payload, dict):
        return []
    msg = payload.get("message") or {}
    if not isinstance(msg, dict):
        return []
    shop_id = _digits(_sender_number(payload))
    mid = _message_id(payload)
    ts = str(payload.get("timestamp", ""))
    if not shop_id or not mid:
        return []
    mtype = str(msg.get("message_type", "")).lower()
    if mtype == "text":
        return [IncomingMessage(mid, shop_id, "text", str(msg.get("text", "")), ts,
                                channel="vonage")]
    if mtype == "audio":
        url = (msg.get("audio") or {}).get("url") if isinstance(msg.get("audio"), dict) else None
        return [IncomingMessage(mid, shop_id, "media:audio", None, ts,
                                channel="vonage", media_url=url)]
    if mtype == "image":
        url = (msg.get("image") or {}).get("url") if isinstance(msg.get("image"), dict) else None
        return [IncomingMessage(mid, shop_id, "media:image", None, ts,
                                channel="vonage", media_url=url)]
    if mtype == "reply":
        reply = msg.get("reply") or {}
        title = reply.get("title") or reply.get("id") or "" if isinstance(reply, dict) else ""
        return [IncomingMessage(mid, shop_id, "button", str(title), ts, channel="vonage")]
    return []
