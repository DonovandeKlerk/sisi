"""Infobip WhatsApp channel.

Same internal contract as the Vonage/Meta paths: inbound payloads normalize
to `IncomingMessage` (with channel="infobip") and flow through the same agent,
voice, and vision handlers. Replies go out via `InfobipSender`.

Trial notes (trial sender, e.g. 447860088970): verified recipients only,
free-form messages only inside the 24h user-initiated window — outside it,
only approved templates may be sent (see `send_template`).

Config (env, never logged): INFOBIP_API_KEY, INFOBIP_BASE_URL,
INFOBIP_WHATSAPP_FROM, INFOBIP_WEBHOOK_TOKEN (inbound webhook path token).

Sources (official Infobip docs, verified 2026-10-28):
- Auth + base URL: https://www.infobip.com/docs/api
  ("Authorization: App <key>"; per-account base URL, not secret)
- Trial limits: https://www.infobip.com/docs/whatsapp/get-started
  (shared sender, verified numbers, 100 free conversations)
- Window/templates: https://www.infobip.com/docs/whatsapp/message-types-and-templates
  and https://www.infobip.com/docs/whatsapp/manage-integration
- Template payload: https://www.infobip.com/docs/tutorials/authenticate-users-with-whatsapp-template-messages
- Interactive list payload + inbound reply shape + sender webhook config:
  https://www.infobip.com/docs/tutorials/send-an-interactive-list-with-whatsapp-api.md
- Endpoint paths + schemas: https://api.infobip.com/platform/1/openapi/whatsapp
  (OpenAPI v3.257.0: /message/text|template|image|document|audio,
  /message/interactive/buttons|list, /senders/{sender}/media/{mediaId},
  /senders/{sender}/message/{messageId}/read, inbound + DLR webhooks)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid

import httpx

from app.net import call_with_retries
from app.sendqueue import RateLimited
from app.voice import VoiceError
from app.whatsapp import IncomingMessage, WhatsAppSender, split_message

log = logging.getLogger("sisi.infobip")

MAX_BTNS = 3
MAX_BTN_TITLE = 20
MAX_ROWS = 10
MAX_ROW_TITLE = 24


def _digits(number: str) -> str:
    return re.sub(r"\D", "", number or "")


def _base_url() -> str:
    base = (os.environ.get("INFOBIP_BASE_URL", "") or "").strip().rstrip("/")
    if base and "://" not in base:
        base = "https://" + base
    return base


def _retry_after(response) -> float | None:
    try:
        return max(0.0, float(response.headers.get("retry-after", "")))
    except (ValueError, TypeError, AttributeError):
        return None


def _numbered(body: str, options: list[str]) -> str:
    """Numbered fallback ('1) .. 2) ..'); skipped when the body already
    carries numbers (ask_clarify renders its own)."""
    if "1)" in (body or ""):
        return body
    lines = [body or "", ""]
    for i, o in enumerate(options, 1):
        lines.append(f"{i}) {o}")
    return "\n".join(lines)


def _clean_label(text: str, limit: int) -> str:
    """Button/row label: no markdown (Infobip titles reject it), trimmed."""
    return re.sub(r"[*_~`]", "", text or "").strip()[:limit]


class InfobipSender(WhatsAppSender):
    """Send via the Infobip WhatsApp API. Same interface as VonageSender."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 whatsapp_from: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("INFOBIP_API_KEY", "")
        self.base_url = (base_url or _base_url()).rstrip("/")
        self.whatsapp_from = _digits(whatsapp_from
                                     or os.environ.get("INFOBIP_WHATSAPP_FROM", ""))

    def _headers(self) -> dict:
        return {"Authorization": f"App {self.api_key}",
                "Content-Type": "application/json", "Accept": "application/json"}

    def _check(self) -> None:
        if not self.api_key:
            raise RuntimeError("INFOBIP_API_KEY is not set")
        if not self.base_url:
            raise RuntimeError("INFOBIP_BASE_URL is not set")
        if not self.whatsapp_from:
            raise RuntimeError("INFOBIP_WHATSAPP_FROM is not set")

    def _post(self, path: str, payload: dict, label: str, to: str,
              timeout: float = 15) -> dict:
        self._check()

        def send():
            resp = httpx.post(self.base_url + path, headers=self._headers(),
                              json=payload, timeout=timeout)
            resp.raise_for_status()
            try:
                body = resp.json()
            except ValueError:
                body = {}
            log.info("infobip %s sent to=%s status=%s id=%s", label, to,
                     resp.status_code, body.get("messageId", "?"))
            return body if isinstance(body, dict) else {}

        try:
            return call_with_retries(send)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status == 429:
                log.warning("infobip 429 to=%s retry-after=%s", to,
                            _retry_after(e.response))
                raise RateLimited(_retry_after(e.response)) from e
            text = e.response.text[:300] if e.response is not None else ""
            log.error("infobip %s failed to=%s status=%s body=%s",
                      label, to, status, text)
            raise

    def _base_payload(self, to: str) -> dict:
        return {"from": self.whatsapp_from, "to": _digits(to)}

    def send_text(self, to: str, body: str) -> None:
        for chunk in split_message(body):
            self._post("/whatsapp/1/message/text",
                       {**self._base_payload(to),
                        "content": {"text": chunk}}, "text", _digits(to))

    def send_template(self, to: str, name: str, language: str = "en",
                      placeholders: list[str] | None = None) -> None:
        """Send an approved template (for outside the 24h window)."""
        self._post("/whatsapp/1/message/template",
                   {"messages": [{**self._base_payload(to),
                                  "messageId": uuid.uuid4().hex,
                                  "content": {
                                      "templateName": name,
                                      "templateData": {
                                          "body": {"placeholders":
                                                   list(placeholders or [])}},
                                      "language": language}}]},
                   f"template:{name}", _digits(to))

    def send_buttons(self, to: str, body: str, buttons: list[str]) -> None:
        """Native reply buttons; numbered text when unsupported.

        429 (RateLimited) is re-raised for the queue to retry; any other
        failure falls back to '1) .. 2) ..' text the numbered tap resolves.
        """
        options = [_clean_label(b, MAX_BTN_TITLE) for b in buttons[:MAX_BTNS]]
        options = [o for o in options if o]
        payload = {**self._base_payload(to),
                   "content": {"body": {"text": body[:1024]},
                               "action": {"buttons": [
                                   {"type": "REPLY", "id": o, "title": o}
                                   for o in options]}}}
        try:
            self._post("/whatsapp/1/message/interactive/buttons", payload,
                       "buttons", _digits(to))
        except RateLimited:
            raise
        except (httpx.HTTPError, RuntimeError) as e:
            log.warning("infobip buttons fallback to=%s err=%r",
                        _digits(to), e)
            self.send_text(to, _numbered(body, buttons[:MAX_BTNS]))

    def send_list(self, to: str, body: str, button_text: str,
                  rows: list[str]) -> None:
        """Native interactive list (up to 10 rows); numbered text fallback."""
        options = [_clean_label(r, MAX_ROW_TITLE) for r in rows[:MAX_ROWS]]
        options = [o for o in options if o]
        payload = {**self._base_payload(to),
                   "content": {"body": {"text": body[:1024]},
                               "action": {
                                   "title": _clean_label(
                                       button_text, MAX_BTN_TITLE) or "Choose",
                                   "sections": [{
                                       "title": "Options",
                                       "rows": [{"id": o, "title": o}
                                                for o in options]}]}}}
        try:
            self._post("/whatsapp/1/message/interactive/list", payload,
                       "list", _digits(to))
        except RateLimited:
            raise
        except (httpx.HTTPError, RuntimeError) as e:
            log.warning("infobip list fallback to=%s err=%r", _digits(to), e)
            self.send_text(to, _numbered(body, rows[:MAX_ROWS]))

    def send_image(self, to: str, url: str, caption: str = "") -> None:
        self._post("/whatsapp/1/message/image",
                   {**self._base_payload(to),
                    "content": {"mediaUrl": url, "caption": caption[:1000]}},
                   "image", _digits(to), timeout=20)

    def send_file(self, to: str, url: str, filename: str, caption: str = "") -> None:
        self._post("/whatsapp/1/message/document",
                   {**self._base_payload(to),
                    "content": {"mediaUrl": url, "caption": caption[:1000],
                                "filename": (filename or "sisi-file")[:255]}},
                   "file", _digits(to), timeout=20)

    def mark_read(self, message_id: str) -> bool:
        """Best-effort read receipt. Never raises; False when unconfigured."""
        if not message_id or not self.api_key or not self.base_url \
                or not self.whatsapp_from:
            return False
        url = (f"{self.base_url}/whatsapp/1/senders/{self.whatsapp_from}"
               f"/message/{message_id}/read")
        try:
            resp = httpx.post(url, headers=self._headers(), timeout=5)
            resp.raise_for_status()
            log.info("infobip mark-read %s status=%s", message_id,
                     resp.status_code)
            return True
        except Exception:
            log.warning("infobip mark-read failed", exc_info=True)
            return False


def download_infobip_media(url: str) -> bytes:
    """Fetch Infobip-hosted inbound media bytes with the API key.

    Raises VoiceError (the photo path catches it too).
    """
    key = os.environ.get("INFOBIP_API_KEY", "")
    if not key or not url or not str(url).startswith("https://"):
        raise VoiceError("I couldn't fetch that file. Please send it again?")

    def fetch() -> bytes:
        resp = httpx.get(url, headers={"Authorization": f"App {key}"},
                         timeout=30, follow_redirects=True)
        resp.raise_for_status()
        return resp.content

    try:
        return call_with_retries(fetch)
    except httpx.HTTPError as e:
        log.warning("infobip media download failed: %r", e)
        raise VoiceError("I couldn't fetch that file. Please send it again?") from e


def _synth_id(payload: dict) -> str:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()
                            ).hexdigest()[:16]
    return f"infobip-{digest}"


def parse_infobip_webhook(payload: dict) -> list[IncomingMessage]:
    """Normalize an Infobip inbound webhook to internal messages.

    Validates per result: a `from` number, our sender in `to` (when
    INFOBIP_WHATSAPP_FROM is configured), and a known message type.
    TEXT/IMAGE/AUDIO/VOICE/button+list replies flow to the handlers;
    VIDEO/DOCUMENT get the graceful "unsupported" reply; anything else is
    dropped with a log (reactions etc. must never trigger replies).
    """
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        log.warning("infobip inbound dropped: no results list keys=%s",
                    sorted(payload.keys()))
        return []
    expected_to = _digits(os.environ.get("INFOBIP_WHATSAPP_FROM", ""))
    out: list[IncomingMessage] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        shop_id = _digits(str(item.get("from", "")))
        if not shop_id:
            log.warning("infobip inbound dropped: no from number")
            continue
        if expected_to:
            to = _digits(str(item.get("to", "")))
            if to and to != expected_to:
                log.warning("infobip inbound dropped: to=%s is not our sender",
                            to)
                continue
        mid = str(item.get("messageId", "") or "")
        if not mid:
            mid = _synth_id(item)
        ts = str(item.get("receivedAt", ""))
        msg = item.get("message")
        if not isinstance(msg, dict):
            continue
        mtype = str(msg.get("type", "")).upper()
        if mtype == "TEXT":
            text = msg.get("text", "")
            out.append(IncomingMessage(mid, shop_id, "text",
                                       text if isinstance(text, str) else "",
                                       ts, channel="infobip"))
        elif mtype == "IMAGE":
            node = msg
            caption = node.get("caption", "")
            out.append(IncomingMessage(
                mid, shop_id, "media:image", None, ts, channel="infobip",
                media_url=node.get("url") or None,
                caption=caption if isinstance(caption, str) else ""))
        elif mtype in ("AUDIO", "VOICE"):
            out.append(IncomingMessage(
                mid, shop_id, "media:audio", None, ts, channel="infobip",
                media_url=msg.get("url") or None))
        elif mtype in ("DOCUMENT", "VIDEO"):
            # Real content we cannot process yet: the "unsupported" reply
            # tells the owner what to send instead (never silent, never a crash).
            out.append(IncomingMessage(mid, shop_id, f"media:{mtype.lower()}",
                                       None, ts, channel="infobip",
                                       media_url=msg.get("url") or None))
        elif mtype in ("INTERACTIVE_BUTTON_REPLY", "INTERACTIVE_LIST_REPLY"):
            title = msg.get("title") or msg.get("id") or ""
            out.append(IncomingMessage(mid, shop_id, "button", str(title), ts,
                                       channel="infobip"))
        else:
            log.info("infobip inbound dropped: type=%r id=%s",
                     msg.get("type"), mid)
    return out
