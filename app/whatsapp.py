"""WhatsApp Cloud API boundary: signature check, payload parsing, sender abstraction.

Contract:
  - `verify_signature` / `parse_webhook` validate untrusted Meta input at the edge.
  - `WhatsAppSender` is the only way the app sends messages. `get_sender()`
    returns a `CloudApiSender` when both WHATSAPP_TOKEN and
    WHATSAPP_PHONE_NUMBER_ID are set, else the in-memory `FakeSender`.
    Nothing here ever crashes because WhatsApp config is missing.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
from dataclasses import dataclass

import httpx

log = logging.getLogger("sisi.whatsapp")

GRAPH_BASE = "https://graph.facebook.com"
MAX_BUTTONS = 3
MAX_BUTTON_TITLE = 20
MAX_TEXT_LEN = 4096


@dataclass
class IncomingMessage:
    message_id: str
    shop_id: str  # sender's number is the shop key
    kind: str  # "text" | "button" | "media:<type>"
    text: str | None  # text body, button title, or None when unsupported
    timestamp: str = ""
    media_id: str | None = None  # WhatsApp media id for audio/image/video


def verify_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """Check X-Hub-Signature-256. Returns False on any mismatch or missing part."""
    if not app_secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + expected, signature_header)


def parse_webhook(payload: dict) -> list[IncomingMessage]:
    """Pull user messages out of a Cloud API webhook payload.

    Status updates and non-WhatsApp objects yield nothing. Unsupported message
    types yield a message with text=None so the caller can reply gracefully.
    """
    out: list[IncomingMessage] = []
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return out
    entries = payload.get("entry") or []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            value = (change or {}).get("value") or {}
            for m in value.get("messages") or []:
                if not isinstance(m, dict):
                    continue
                mid, frm = m.get("id", ""), m.get("from", "")
                if not mid or not frm:
                    continue
                mtype = m.get("type", "")
                ts = m.get("timestamp", "")
                if mtype == "text":
                    out.append(IncomingMessage(mid, frm, "text",
                                               (m.get("text") or {}).get("body", ""), ts))
                elif mtype == "interactive":
                    br = (m.get("interactive") or {}).get("button_reply") or {}
                    out.append(IncomingMessage(mid, frm, "button",
                                               br.get("title") or br.get("id") or "", ts))
                else:
                    inner = m.get(mtype) or {}
                    media_id = inner.get("id") if isinstance(inner, dict) else None
                    out.append(IncomingMessage(mid, frm, f"media:{mtype}", None, ts, media_id))
    return out


def download_media(media_id: str) -> bytes:
    """Fetch WhatsApp media bytes via the media-id endpoint. Raises VoiceError."""
    from app.voice import CANT_HEAR, VoiceError

    token = os.environ.get("WHATSAPP_TOKEN", "")
    version = os.environ.get("GRAPH_API_VERSION", "v26.0")
    if not token:
        raise VoiceError("I couldn't fetch that voice note. Please send it again?")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        meta = httpx.get(f"{GRAPH_BASE}/{version}/{media_id}", headers=headers, timeout=15)
        meta.raise_for_status()
        url = meta.json().get("url", "")
        data = httpx.get(url, headers=headers, timeout=30)
        data.raise_for_status()
        return data.content
    except httpx.HTTPError as e:
        log.warning("media download failed: %r", e)
        raise VoiceError(CANT_HEAR) from e


class WhatsAppSender:
    """Sending contract. Implementations must not raise for missing config."""

    def send_text(self, to: str, body: str) -> None:
        raise NotImplementedError

    def send_buttons(self, to: str, body: str, buttons: list[str]) -> None:
        raise NotImplementedError


class FakeSender(WhatsAppSender):
    """In-memory sender for local dev and tests. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.sent: list[dict] = []

    def send_text(self, to: str, body: str) -> None:
        with self._lock:
            self.sent.append({"to": to, "type": "text", "body": body})
        log.info("fake -> %s: %.80s", to, body)

    def send_buttons(self, to: str, body: str, buttons: list[str]) -> None:
        with self._lock:
            self.sent.append({"to": to, "type": "buttons", "body": body,
                              "buttons": list(buttons)})
        log.info("fake -> %s: %.80s %s", to, body, buttons)

    def pop_shop(self, shop_id: str) -> list[dict]:
        """Take and clear queued messages for one shop (simulator polling)."""
        with self._lock:
            mine = [m for m in self.sent if m["to"] == shop_id]
            self.sent = [m for m in self.sent if m["to"] != shop_id]
        return mine

    def clear(self) -> None:
        with self._lock:
            self.sent = []


class CloudApiSender(WhatsAppSender):
    """Real Cloud API sender (Graph v26.0 messages endpoint)."""

    def __init__(self, token: str | None = None, phone_number_id: str | None = None,
                 version: str | None = None) -> None:
        self.token = token or os.environ.get("WHATSAPP_TOKEN", "")
        self.phone_number_id = phone_number_id or os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
        self.version = version or os.environ.get("GRAPH_API_VERSION", "v26.0")

    @property
    def url(self) -> str:
        return f"{GRAPH_BASE}/{self.version}/{self.phone_number_id}/messages"

    def _post(self, data: dict) -> dict:
        resp = httpx.post(
            self.url,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
            json=data,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def send_text(self, to: str, body: str) -> None:
        self._post({"messaging_product": "whatsapp", "to": to, "type": "text",
                    "text": {"preview_url": False, "body": body[:MAX_TEXT_LEN]}})

    def send_buttons(self, to: str, body: str, buttons: list[str]) -> None:
        action = [{"type": "reply",
                   "reply": {"id": b[:MAX_BUTTON_TITLE], "title": b[:MAX_BUTTON_TITLE]}}
                  for b in buttons[:MAX_BUTTONS]]
        self._post({"messaging_product": "whatsapp", "to": to, "type": "interactive",
                    "interactive": {"type": "button", "body": {"text": body[:1024]},
                                    "action": {"buttons": action}}})


_fake = FakeSender()


def get_sender() -> WhatsAppSender:
    """Cloud sender only when both live vars are set, else the fake. Never raises."""
    if os.environ.get("WHATSAPP_PHONE_NUMBER_ID") and os.environ.get("WHATSAPP_TOKEN"):
        return CloudApiSender()
    return _fake


def reset_fake() -> None:
    _fake.clear()
