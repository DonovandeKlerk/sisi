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
from app.sendqueue import RateLimited
from app.voice import VoiceError
from app.whatsapp import IncomingMessage, WhatsAppSender, split_message

log = logging.getLogger("sisi.vonage")

VONAGE_MESSAGES_URL = "https://messages-sandbox.nexmo.com/v1/messages"
DEFAULT_WHATSAPP_FROM = "14157386102"


def _digits(number: str) -> str:
    return re.sub(r"\D", "", number or "")


def _retry_after(response) -> float | None:
    try:
        return max(0.0, float(response.headers.get("retry-after", "")))
    except (ValueError, TypeError, AttributeError):
        return None


MAX_BTNS = 3
MAX_BTN_TITLE = 20
MAX_ROWS = 10
MAX_ROW_TITLE = 24


def _numbered(body: str, options: list[str]) -> str:
    """Numbered fallback ('1) .. 2) ..'); skipped when the body already
    carries numbers (ask_clarify renders its own)."""
    if "1)" in (body or ""):
        return body
    lines = [body or "", ""]
    for i, o in enumerate(options, 1):
        lines.append(f"{i}) {o}")
    return "\n".join(lines)


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
            log.info("vonage reply sent to=%s status=%s body=%.300s",
                     data["to"], resp.status_code, resp.text)
            return resp.json()

        try:
            call_with_retries(send)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status == 429:
                retry_after = _retry_after(e.response)
                log.warning("vonage 429 to=%s retry-after=%s", data["to"],
                            retry_after)
                raise RateLimited(retry_after) from e
            text = e.response.text[:300] if e.response is not None else ""
            log.error("vonage reply failed to=%s status=%s body=%s", data["to"], status, text)
            raise
        except httpx.HTTPError as e:
            log.error("vonage reply failed to=%s err=%r", data["to"], e)
            raise

    def send_text(self, to: str, body: str) -> None:
        for chunk in split_message(body):
            self._post_text(to, chunk)

    def _post_custom(self, to: str, custom: dict, label: str) -> None:
        """POST a message_type=custom interactive payload (buttons/list)."""
        if not self.api_key or not self.api_secret:
            raise RuntimeError("VONAGE_API_KEY/VONAGE_API_SECRET are not set")
        data = {"from": self.whatsapp_from, "to": _digits(to), "channel": "whatsapp",
                "message_type": "custom", "custom": custom}

        def send():
            resp = httpx.post(VONAGE_MESSAGES_URL, auth=(self.api_key, self.api_secret),
                              json=data, timeout=15)
            resp.raise_for_status()
            log.info("vonage custom %s sent to=%s status=%s", label,
                     data["to"], resp.status_code)
            return resp.json()

        try:
            call_with_retries(send)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status == 429:
                log.warning("vonage 429 to=%s retry-after=%s", data["to"],
                            _retry_after(e.response))
                raise RateLimited(_retry_after(e.response)) from e
            text = e.response.text[:300] if e.response is not None else ""
            log.warning("vonage custom %s failed to=%s status=%s body=%s",
                        label, data["to"], status, text)
            raise

    def send_buttons(self, to: str, body: str, buttons: list[str]) -> None:
        """Interactive reply buttons; numbered text when unsupported.

        429 (RateLimited) is re-raised for the queue to retry; any other
        failure falls back to '1) .. 2) ..' text the numbered tap resolves.
        """
        options = [b[:MAX_BTN_TITLE] for b in buttons[:MAX_BTNS]]
        action = [{"type": "reply", "reply": {"id": o, "title": o}}
                  for o in options]
        custom = {"type": "interactive",
                  "interactive": {"type": "button",
                                  "body": {"text": body[:1024]},
                                  "action": {"buttons": action}}}
        try:
            self._post_custom(to, custom, "buttons")
        except RateLimited:
            raise
        except (httpx.HTTPError, RuntimeError) as e:
            log.warning("vonage buttons fallback to=%s err=%r", _digits(to), e)
            self.send_text(to, _numbered(body, buttons[:MAX_BTNS]))

    def send_list(self, to: str, body: str, button_text: str,
                  rows: list[str]) -> None:
        """Interactive list (up to 10 rows); numbered text when unsupported."""
        options = [r[:MAX_ROW_TITLE] for r in rows[:MAX_ROWS]]
        custom = {"type": "interactive",
                  "interactive": {
                      "type": "list", "body": {"text": body[:1024]},
                      "action": {
                          "button": button_text[:MAX_BTN_TITLE],
                          "sections": [{
                              "title": "Options",
                              "rows": [{"id": o, "title": o} for o in options],
                          }],
                      }}}
        try:
            self._post_custom(to, custom, "list")
        except RateLimited:
            raise
        except (httpx.HTTPError, RuntimeError) as e:
            log.warning("vonage list fallback to=%s err=%r", _digits(to), e)
            self.send_text(to, _numbered(body, rows[:MAX_ROWS]))

    def _post_media(self, to: str, message_type: str, payload: dict) -> None:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("VONAGE_API_KEY/VONAGE_API_SECRET are not set")
        data = {"from": self.whatsapp_from, "to": _digits(to), "channel": "whatsapp",
                "message_type": message_type, message_type: payload}

        def send():
            resp = httpx.post(VONAGE_MESSAGES_URL, auth=(self.api_key, self.api_secret),
                              json=data, timeout=20)
            resp.raise_for_status()
            log.info("vonage %s sent to=%s status=%s url=%s", message_type,
                     data["to"], resp.status_code, payload.get("url", ""))
            return resp.json()

        try:
            call_with_retries(send)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status == 429:
                log.warning("vonage 429 to=%s url=%s", data["to"],
                            payload.get("url", ""))
                raise RateLimited(_retry_after(e.response)) from e
            raise

    def send_image(self, to: str, url: str, caption: str = "") -> None:
        self._post_media(to, "image", {"url": url, "caption": caption[:1000]})

    def send_file(self, to: str, url: str, filename: str, caption: str = "") -> None:
        self._post_media(to, "file", {"url": url, "name": filename[:255],
                                      "caption": caption[:1000]})

    def mark_read(self, message_id: str) -> bool:
        """PATCH the message read + show the WhatsApp typing indicator.

        Uses VONAGE_API_BASE (sandbox default). Best-effort: the sandbox may
        not support it, so failures only log.
        """
        if not message_id or not self.api_key or not self.api_secret:
            return False
        base = os.environ.get("VONAGE_API_BASE",
                              "https://messages-sandbox.nexmo.com").rstrip("/")
        if "sandbox" in base:
            return False  # sandbox has no mark-read endpoint (always 404s)
        try:
            resp = httpx.patch(f"{base}/v1/messages/{message_id}",
                               auth=(self.api_key, self.api_secret),
                               json={"status": "read",
                                     "replying_indicator": {"show": "true",
                                                            "type": "text"}},
                               timeout=5)
            resp.raise_for_status()
            log.info("vonage mark-read %s status=%s", message_id,
                     resp.status_code)
            return True
        except Exception:
            log.warning("vonage mark-read failed (sandbox may not support it)",
                        exc_info=True)
            return False


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


def _media_url(node) -> str | None:
    if isinstance(node, dict):
        return node.get("url")
    if isinstance(node, str):
        return node or None
    return None


def parse_vonage_webhook(payload: dict) -> list[IncomingMessage]:
    """Normalize a Vonage Messages inbound payload to internal messages.

    Handles both the nested shape (fields under "message") and the flat
    sandbox shape (message_type/text/audio/image at top level).
    """
    if not isinstance(payload, dict):
        return []
    nested = payload.get("message")
    msg = nested if isinstance(nested, dict) else payload
    shop_id = _digits(_sender_number(payload))
    mid = _message_id(payload)
    ts = str(payload.get("timestamp", ""))
    if not shop_id or not mid:
        return []
    mtype = str(msg.get("message_type", "")).lower()
    if mtype == "text":
        text = msg.get("text", "")
        text = text if isinstance(text, str) else ""
        return [IncomingMessage(mid, shop_id, "text", text, ts, channel="vonage")]
    if mtype == "audio":
        return [IncomingMessage(mid, shop_id, "media:audio", None, ts,
                                channel="vonage", media_url=_media_url(msg.get("audio")))]
    if mtype == "image":
        node = msg.get("image")
        caption = node.get("caption", "") if isinstance(node, dict) else ""
        return [IncomingMessage(mid, shop_id, "media:image", None, ts,
                                channel="vonage", media_url=_media_url(node),
                                caption=caption if isinstance(caption, str) else "")]
    if mtype == "reply":
        reply = msg.get("reply") or {}
        title = reply.get("title") or reply.get("id") or "" if isinstance(reply, dict) else ""
        return [IncomingMessage(mid, shop_id, "button", str(title), ts, channel="vonage")]
    log.warning("vonage inbound dropped: unknown message_type=%r keys=%s",
                msg.get("message_type"), sorted(payload.keys()))
    return []
