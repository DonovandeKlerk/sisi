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

from app.net import call_with_retries

log = logging.getLogger("sisi.whatsapp")

GRAPH_BASE = "https://graph.facebook.com"
MAX_BUTTONS = 3
MAX_BUTTON_TITLE = 20
MAX_ROWS = 10
MAX_ROW_TITLE = 24
MAX_TEXT_LEN = 4096
SPLIT_AT = 1500


def split_message(body: str, limit: int = SPLIT_AT) -> list[str]:
    """Split a long reply at blank-line section boundaries (style guide: ~1500 chars).

    Sections that alone top the limit fall back to word splits; pathological
    words are hard-split so no chunk ever tops the limit.
    """
    body = body or ""
    if not body:
        return []
    if len(body) <= limit:
        return [body]
    chunks, current = [], ""
    for section in body.split("\n\n"):
        piece = section if not current else current + "\n\n" + section
        if len(piece) <= limit:
            current = piece
            continue
        if current:
            chunks.append(current)
        current = section
        while len(current) > limit:
            cut = current.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append(current[:cut])
            current = current[cut:].lstrip()
    if current:
        chunks.append(current)
    return chunks


@dataclass
class IncomingMessage:
    message_id: str
    shop_id: str  # sender's number is the shop key
    kind: str  # "text" | "button" | "media:<type>"
    text: str | None  # text body, button title, or None when unsupported
    timestamp: str = ""
    media_id: str | None = None  # WhatsApp media id for audio/image/video
    media_url: str | None = None  # provider-hosted media URL (vonage/infobip)
    channel: str = "meta"  # inbound channel: "meta", "vonage" or "infobip"
    caption: str = ""  # image caption, e.g. "logo"


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
                    caption = inner.get("caption", "") if isinstance(inner, dict) else ""
                    out.append(IncomingMessage(mid, frm, f"media:{mtype}", None, ts,
                                               media_id, None, "meta",
                                               caption if isinstance(caption, str) else ""))
    return out


def parse_statuses(payload: dict) -> list[tuple[str, str, str]]:
    """Pull delivery reports from a Cloud API webhook payload.

    Returns [(provider_id, outcome, detail)] with outcome delivered,
    failed, or submitted. Anything unparseable yields nothing.
    """
    out: list[tuple[str, str, str]] = []
    if not isinstance(payload, dict):
        return out
    if payload.get("object") != "whatsapp_business_account":
        return out
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            value = (change or {}).get("value") or {}
            for st in value.get("statuses") or []:
                if not isinstance(st, dict):
                    continue
                mid = st.get("id", "")
                status = str(st.get("status", "")).lower()
                if not mid or not status:
                    continue
                if status in ("delivered", "read"):
                    out.append((mid, "delivered", ""))
                elif status in ("failed", "deleted"):
                    errors = st.get("errors") or []
                    detail = ""
                    if errors and isinstance(errors[0], dict):
                        detail = str(errors[0].get("title", "")
                                     or errors[0].get("code", ""))
                    out.append((mid, "failed", detail or "delivery failed"))
                elif status == "sent":
                    out.append((mid, "submitted", ""))
    return out


def download_media(media_id: str, label: str = "voice note") -> bytes:
    """Fetch WhatsApp media bytes via the media-id endpoint. Raises VoiceError."""
    from app.voice import CANT_HEAR, VoiceError

    token = os.environ.get("WHATSAPP_TOKEN", "")
    version = os.environ.get("GRAPH_API_VERSION", "v26.0")
    if not token:
        raise VoiceError(f"I couldn't fetch that {label}. Please send it again?")
    headers = {"Authorization": f"Bearer {token}"}

    def fetch() -> bytes:
        meta = httpx.get(f"{GRAPH_BASE}/{version}/{media_id}", headers=headers, timeout=15)
        meta.raise_for_status()
        url = meta.json().get("url", "")
        data = httpx.get(url, headers=headers, timeout=30)
        data.raise_for_status()
        return data.content

    try:
        return call_with_retries(fetch)
    except httpx.HTTPError as e:
        log.warning("media download failed: %r", e)
        if label == "voice note":
            raise VoiceError(CANT_HEAR) from e
        raise VoiceError(f"I couldn't fetch that {label}. Please send it again?") from e


class WhatsAppSender:
    """Sending contract. Implementations must not raise for missing config."""

    def send_text(self, to: str, body: str) -> None:
        raise NotImplementedError

    def send_buttons(self, to: str, body: str, buttons: list[str]) -> None:
        raise NotImplementedError

    def send_list(self, to: str, body: str, button_text: str,
                  rows: list[str]) -> None:
        raise NotImplementedError

    def send_image(self, to: str, url: str, caption: str = "") -> None:
        raise NotImplementedError

    def send_file(self, to: str, url: str, filename: str, caption: str = "") -> None:
        raise NotImplementedError

    def mark_read(self, message_id: str) -> bool:
        """Best-effort read receipt (+typing where supported). Never raises."""
        return False


class FakeSender(WhatsAppSender):
    """In-memory sender for local dev and tests. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.sent: list[dict] = []

    def send_text(self, to: str, body: str) -> str | None:
        """In-memory send; no provider id exists in dev, so None."""
        with self._lock:
            self.sent.append({"to": to, "type": "text", "body": body})
        log.info("fake -> %s: %.80s", to, body)
        return None

    def send_buttons(self, to: str, body: str, buttons: list[str]) -> None:
        with self._lock:
            self.sent.append({"to": to, "type": "buttons", "body": body,
                              "buttons": list(buttons)})
        log.info("fake -> %s: %.80s %s", to, body, buttons)

    def send_list(self, to: str, body: str, button_text: str,
                  rows: list[str]) -> None:
        with self._lock:
            self.sent.append({"to": to, "type": "list", "body": body,
                              "button_text": button_text,
                              "rows": list(rows)})
        log.info("fake -> %s: %.80s %s", to, body, rows)

    def send_image(self, to: str, url: str, caption: str = "") -> None:
        with self._lock:
            self.sent.append({"to": to, "type": "image", "url": url, "body": caption})
        log.info("fake -> %s: image %s", to, url)

    def send_file(self, to: str, url: str, filename: str, caption: str = "") -> None:
        with self._lock:
            self.sent.append({"to": to, "type": "file", "url": url,
                              "filename": filename, "body": caption})
        log.info("fake -> %s: file %s", to, url)

    def mark_read(self, message_id: str) -> bool:
        log.info("fake mark read %s", message_id)
        return True

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
        def send():
            resp = httpx.post(
                self.url,
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
                json=data,
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()

        return call_with_retries(send)

    def send_text(self, to: str, body: str) -> str | None:
        """Send text; returns the provider message id of the first chunk."""
        first: str | None = None
        for chunk in split_message(body):
            resp = self._post({"messaging_product": "whatsapp", "to": to,
                               "type": "text",
                               "text": {"preview_url": False,
                                        "body": chunk[:MAX_TEXT_LEN]}})
            if first is None and isinstance(resp, dict):
                msgs = resp.get("messages") or []
                mid = msgs[0].get("id", "") if msgs else ""
                first = mid if isinstance(mid, str) and mid else None
        return first

    def send_buttons(self, to: str, body: str, buttons: list[str]) -> None:
        action = [{"type": "reply",
                   "reply": {"id": b[:MAX_BUTTON_TITLE], "title": b[:MAX_BUTTON_TITLE]}}
                  for b in buttons[:MAX_BUTTONS]]
        self._post({"messaging_product": "whatsapp", "to": to, "type": "interactive",
                    "interactive": {"type": "button", "body": {"text": body[:1024]},
                                    "action": {"buttons": action}}})

    def send_list(self, to: str, body: str, button_text: str,
                  rows: list[str]) -> None:
        items = [{"id": r[:MAX_ROW_TITLE], "title": r[:MAX_ROW_TITLE]}
                 for r in rows[:MAX_ROWS]]
        self._post({"messaging_product": "whatsapp", "to": to, "type": "interactive",
                    "interactive": {"type": "list", "body": {"text": body[:1024]},
                                    "action": {"button": button_text[:MAX_BUTTON_TITLE],
                                               "sections": [{"title": "Options",
                                                             "rows": items}]}}})

    def send_image(self, to: str, url: str, caption: str = "") -> None:
        self._post({"messaging_product": "whatsapp", "to": to, "type": "image",
                    "image": {"link": url, "caption": caption[:1024]}})

    def send_file(self, to: str, url: str, filename: str, caption: str = "") -> None:
        self._post({"messaging_product": "whatsapp", "to": to, "type": "document",
                    "document": {"link": url, "filename": filename[:255],
                                 "caption": caption[:1024]}})

    def mark_read(self, message_id: str) -> bool:
        if not message_id:
            return False
        try:
            self._post({"messaging_product": "whatsapp", "status": "read",
                        "message_id": message_id})
            return True
        except Exception:
            log.warning("meta mark-read failed", exc_info=True)
            return False


_fake = FakeSender()


def get_sender() -> WhatsAppSender:
    """Cloud sender only when both live vars are set, else the fake. Never raises."""
    if os.environ.get("WHATSAPP_PHONE_NUMBER_ID") and os.environ.get("WHATSAPP_TOKEN"):
        return CloudApiSender()
    return _fake


def reset_fake() -> None:
    _fake.clear()
