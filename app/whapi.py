"""Whapi.Cloud WhatsApp channel (testing provider).

Same internal contract as the other paths: inbound payloads normalize
to `IncomingMessage` (with channel="whapi") and flow through the same
agent, voice, and vision handlers. Replies go out via `WhapiSender`.
The live Infobip integration is untouched; provider selection stays
explicit via `sender_for_channel` / `SISI_PROVIDER`.

Safety model (this channel must never act on unrelated chats):
- `channel_id` must match WHAPI_CHANNEL_ID (verified channel DEADPL-S9KV8).
- `from_me` echoes are dropped (loop prevention: API-sent messages
  reappear on the webhook with source "api").
- Only 1:1 chats (`@s.whatsapp.net`) are accepted; groups (`@g.us`),
  broadcasts, channels and status updates are ignored.
- Messages from the linked sender number itself (WHAPI_NUMBER) are
  dropped; the sender number is never treated as a shop owner.
- WHAPI_ALLOWLIST (comma-separated digits) is REQUIRED for an open
  channel: with no allowlist configured, non-demo inbound is dropped
  (fail closed). Join-code demo users (see app/demo.py) and numbers
  already marked is_demo may pass without being on the allowlist.
- Messages older than WHAPI_MAX_AGE_S (default 600) are dropped so a
  history sync can never replay old chats into the shop.

Identity mapping: the shop is keyed by the TESTER's number (the `from`
digits, e.g. 27710000002), which resolves to that number's existing
shop row — its records are preserved, never reseeded or merged. The
linked sender (WHAPI_NUMBER, e.g. 27745492157) only sends; it owns no
shop.

Config (env, never logged): WHAPI_TOKEN, WHAPI_CHANNEL_ID,
WHAPI_NUMBER, WHAPI_WEBHOOK_TOKEN (inbound webhook path token),
WHAPI_ALLOWLIST, WHAPI_BASE_URL, WHAPI_MAX_AGE_S, WHAPI_MAX_MEDIA_BYTES.

Sources (official Whapi.Cloud help-desk docs, verified 2026-10-30):
- Send text: .../help-desk/sending/send-text-message
  (POST /messages/text {to, body}; digits only, no plus)
- Send buttons: .../help-desk/sending/send-message-with-buttons
  (POST /messages/interactive, quick_reply; max 3, title <= 25 chars;
  replies arrive as type "reply" with buttons_reply {id, title})
- Send lists: whapi-whatsapp-api-skill msg-interactive.md
  (POST /messages/interactive, type "list", action.list {sections[],
  label}; optional header/footer; replies as list_reply {id, title})
- Typing presence: PUT /presences/{chat} {presence} (OpenAPI PresencesApi)
- Send media: .../help-desk/sending/send-video-audio-image-document
  (media by URL/base64/media-id; 30-day cloud expiry)
- Media params (caption/filename/mime): whapi-cloud/whapi-whatsapp-api-skill
  references/msg-media.md ({to, media, caption[, mime_type, filename]})
- Incoming format: .../receiving/webhooks/incoming-webhooks-format/
  incoming-message (messages[] + event {type, event} + channel_id;
  image/document/voice nodes carry {id, mime_type, file_size, link?})
- Media download: .../receiving/http-api/get-a-full-size-picture-in-the-response
  (GET /media/{media-id} with Bearer auth returns the file bytes;
  `link` only exists when Auto Download is on)
- Sent echoes: .../incoming-webhooks-format/sent-message
  (from_me + source api/mobile/web; status "pending" then statuses.post)
- Tracked events: .../receiving/webhooks/our-webhooks-tracked-events
  (statuses may repeat per message)
- Webhook security: .../account-and-whapi-channels/customizable-webhook-headers
  (no signature scheme exists; auth is our own path token plus
  channel validation; custom headers are API-configurable only)
"""

from __future__ import annotations

import logging
import os
import re
import time

import httpx

from app.net import call_with_retries
from app.sendqueue import RateLimited
from app.voice import VoiceError
from app.whatsapp import IncomingMessage, WhatsAppSender, split_message

log = logging.getLogger("sisi.whapi")

# Strangers (not allowlisted, no join code): tip once/day, no LLM.
PENDING_TIPS: list[tuple[str, str]] = []

DEFAULT_BASE_URL = "https://gate.whapi.cloud"
DEFAULT_CHANNEL_ID = "DEADPL-S9KV8"
DEFAULT_NUMBER = "27745492157"

MAX_BTNS = 3
MAX_BTN_TITLE = 25
MAX_ROWS = 10
MAX_ROW_TITLE = 24
MAX_LIST_LABEL = 20
MAX_MEDIA_BYTES = 20_000_000
MAX_AGE_S = 600


def _digits(number: str) -> str:
    return re.sub(r"\D", "", number or "")


def _base_url() -> str:
    base = (os.environ.get("WHAPI_BASE_URL", "") or "").strip().rstrip("/")
    return base or DEFAULT_BASE_URL


def _channel_id() -> str:
    return ((os.environ.get("WHAPI_CHANNEL_ID", "") or "").strip()
            or DEFAULT_CHANNEL_ID)


def _own_number() -> str:
    return _digits(os.environ.get("WHAPI_NUMBER", "") or "") or DEFAULT_NUMBER


def _allowlist() -> set[str]:
    raw = os.environ.get("WHAPI_ALLOWLIST", "") or ""
    return {_digits(p) for p in raw.split(",") if _digits(p)}


def _max_age_s() -> float:
    try:
        return max(0.0, float(os.environ.get("WHAPI_MAX_AGE_S", "")
                              or MAX_AGE_S))
    except ValueError:
        return float(MAX_AGE_S)


def _max_media_bytes() -> int:
    try:
        return max(1, int(os.environ.get("WHAPI_MAX_MEDIA_BYTES", "")
                          or MAX_MEDIA_BYTES))
    except ValueError:
        return MAX_MEDIA_BYTES


def _retry_after(response) -> float | None:
    try:
        return max(0.0, float(response.headers.get("retry-after", "")))
    except (ValueError, TypeError, AttributeError):
        return None


def _numbered(body: str, options: list[str]) -> str:
    if "1)" in (body or ""):
        return body
    lines = [body or "", ""]
    for i, o in enumerate(options, 1):
        lines.append(f"{i}) {o}")
    return "\n".join(lines)


def _clean_label(text: str, limit: int) -> str:
    return re.sub(r"[*_~`]", "", text or "").strip()[:limit]


class WhapiSender(WhatsAppSender):
    """Send via the Whapi.Cloud API. Same interface as the other senders."""

    def __init__(self, token: str | None = None,
                 base_url: str | None = None) -> None:
        self.token = token or os.environ.get("WHAPI_TOKEN", "")
        self.base_url = (base_url or _base_url()).rstrip("/")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json", "Accept": "application/json"}

    def _check(self) -> None:
        if not self.token:
            raise RuntimeError("WHAPI_TOKEN is not set")
        if not self.base_url:
            raise RuntimeError("WHAPI_BASE_URL is not set")

    def _post(self, path: str, payload: dict, label: str, to: str,
              timeout: float = 20) -> dict:
        self._check()

        def send():
            resp = httpx.post(self.base_url + path, headers=self._headers(),
                              json=payload, timeout=timeout)
            resp.raise_for_status()
            try:
                body = resp.json()
            except ValueError:
                body = {}
            log.info("whapi %s sent to=%s status=%s id=%s", label, to,
                     resp.status_code,
                     body.get("id", "?") if isinstance(body, dict) else "?")
            return body if isinstance(body, dict) else {}

        try:
            return call_with_retries(send)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status == 429:
                log.warning("whapi 429 to=%s retry-after=%s", to,
                            _retry_after(e.response))
                raise RateLimited(_retry_after(e.response)) from e
            text = e.response.text[:300] if e.response is not None else ""
            log.error("whapi %s failed to=%s status=%s body=%s",
                      label, to, status, text)
            raise

    @staticmethod
    def _sent_id(body: dict) -> str | None:
        for key in ("id", "message_id", "messageId"):
            mid = body.get(key, "")
            if isinstance(mid, str) and mid:
                return mid
        return None

    def send_text(self, to: str, body: str) -> str | None:
        """Send text; returns the provider message id of the first chunk."""
        first: str | None = None
        dest = _digits(to)
        for chunk in split_message(body):
            resp = self._post("/messages/text",
                              {"to": dest, "body": chunk}, "text", dest)
            if first is None:
                first = self._sent_id(resp)
        return first

    def send_buttons(self, to: str, body: str, buttons: list[str],
                       header: str = "", footer: str = "") -> None:
        """Native quick-reply buttons; numbered text when unsupported.

        429 (RateLimited) is re-raised for the queue to retry; any other
        failure falls back to '1) .. 2) ..' text the numbered tap resolves.
        """
        dest = _digits(to)
        options = [_clean_label(b, MAX_BTN_TITLE) for b in buttons[:MAX_BTNS]]
        options = [o for o in options if o]
        payload: dict = {"to": dest, "type": "button",
                         "body": {"text": body[:1024]},
                         "action": {"buttons": [
                             {"type": "quick_reply", "title": o, "id": o}
                             for o in options]}}
        if header:
            payload["header"] = {"text": _clean_label(header, 60)}
        if footer:
            payload["footer"] = {"text": _clean_label(footer, 60)}
        try:
            self._post("/messages/interactive", payload, "buttons", dest)
        except RateLimited:
            raise
        except (httpx.HTTPError, RuntimeError) as e:
            log.warning("whapi buttons fallback to=%s err=%r", dest, e)
            self.send_text(to, _numbered(body, buttons[:MAX_BTNS]))

    def send_list(self, to: str, body: str, button_text: str,
                  rows: list[str], header: str = "",
                  footer: str = "") -> None:
        """Native list message; numbered text when unsupported.

        429 (RateLimited) is re-raised for the queue to retry; any other
        failure falls back to '1) .. 2) ..' text the numbered tap resolves.
        """
        dest = _digits(to)
        options = [_clean_label(r, MAX_ROW_TITLE) for r in rows[:MAX_ROWS]]
        options = [o for o in options if o]
        payload: dict = {
            "to": dest, "type": "list",
            "body": {"text": body[:1024]},
            "action": {"list": {
                "label": _clean_label(button_text,
                                      MAX_LIST_LABEL) or "Choose",
                "sections": [{"title": "Options", "rows": [
                    {"id": o, "title": o} for o in options]}]}},
        }
        if header:
            payload["header"] = {"text": _clean_label(header, 60)}
        if footer:
            payload["footer"] = {"text": _clean_label(footer, 60)}
        try:
            self._post("/messages/interactive", payload, "list", dest)
        except RateLimited:
            raise
        except (httpx.HTTPError, RuntimeError) as e:
            log.warning("whapi list fallback to=%s err=%r", dest, e)
            self.send_text(to, _numbered(body, rows[:MAX_ROWS]))

    def send_presence(self, to: str, presence: str = "composing") -> bool:
        """Best-effort typing indicator. Never raises; False on any failure."""
        if not self.token or not self.base_url:
            return False
        dest = _digits(to)
        if not dest:
            return False
        try:
            resp = httpx.put(
                f"{self.base_url}/presences/{dest}@s.whatsapp.net",
                headers=self._headers(),
                json={"presence": presence}, timeout=5)
            resp.raise_for_status()
            return True
        except Exception:
            log.warning("whapi presence failed to=%s", dest, exc_info=True)
            return False

    def send_image(self, to: str, url: str, caption: str = "") -> None:
        dest = _digits(to)
        self._post("/messages/image",
                   {"to": dest, "media": url,
                    "caption": caption[:1000] if caption else ""},
                   "image", dest, timeout=30)

    def send_file(self, to: str, url: str, filename: str,
                  caption: str = "") -> None:
        dest = _digits(to)
        payload = {"to": dest, "media": url,
                   "filename": (filename or "sisi-file")[:255]}
        if caption:
            payload["caption"] = caption[:1000]
        self._post("/messages/document", payload, "file", dest, timeout=30)

    def mark_read(self, message_id: str) -> bool:
        # No read-receipt endpoint verified in the Whapi docs; skip rather
        # than call an unconfirmed path on the owner's own account.
        return False


def download_whapi_media(media_id: str, link: str | None = None) -> bytes:
    """Fetch inbound Whapi media bytes, honouring the size cap.

    Primary path is GET /media/{media-id} (works whether or not Auto
    Download is on); the Auto-Download `link` is the fallback. Raises
    VoiceError (the photo path catches it too).
    """
    token = os.environ.get("WHAPI_TOKEN", "")
    cap = _max_media_bytes()
    if not token or not media_id:
        raise VoiceError("I couldn't fetch that file. Please send it again?")

    def fetch() -> bytes:
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{_base_url()}/media/{media_id}"
        try:
            with httpx.stream("GET", url, headers=headers, timeout=30,
                              follow_redirects=True) as resp:
                resp.raise_for_status()
                total = 0
                chunks: list[bytes] = []
                for chunk in resp.iter_bytes(65536):
                    total += len(chunk)
                    if total > cap:
                        raise VoiceError(
                            "That file is too big. Please send a smaller one?")
                    chunks.append(chunk)
                return b"".join(chunks)
        except VoiceError:
            raise
        except httpx.HTTPError as e:
            if not link or not str(link).startswith("https://"):
                raise
            log.info("whapi media id fetch failed, trying link: %r", e)
            resp = httpx.get(link, timeout=30, follow_redirects=True)
            resp.raise_for_status()
            if len(resp.content) > cap:
                raise VoiceError(
                    "That file is too big. Please send a smaller one?")
            return resp.content

    try:
        return call_with_retries(fetch)
    except VoiceError:
        raise
    except httpx.HTTPError as e:
        log.warning("whapi media download failed: %r", e)
        raise VoiceError("I couldn't fetch that file. Please send it again?") from e


def _event_type(payload: dict) -> str:
    event = payload.get("event")
    if isinstance(event, dict):
        return str(event.get("type", ""))
    return ""


def _fresh(ts: object, now: float) -> bool:
    """History-sync guard: drop messages older than WHAPI_MAX_AGE_S."""
    try:
        age = now - float(ts)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True  # missing/unparseable stamp: dedupe still protects
    return age <= _max_age_s()


def parse_whapi_webhook(payload: dict, *, now: float | None = None) -> list[IncomingMessage]:
    """Normalize a Whapi messages webhook to internal messages.

    Drops (with a log): wrong channel, `from_me` echoes, non-1:1 chats,
    the linked number itself, non-allowlisted senders (allowlist is
    required — empty means drop all), stale history replays, and
    reaction/sticker/location/etc. noise. TEXT/IMAGE/VOICE-AUDIO/button
    replies flow to the handlers; VIDEO/DOCUMENT get the graceful
    "unsupported" reply.
    """
    if not isinstance(payload, dict):
        return []
    if _event_type(payload) not in ("", "messages"):
        return []
    if str(payload.get("channel_id", "")) != _channel_id():
        log.warning("whapi inbound dropped: channel_id=%r is not ours",
                    payload.get("channel_id"))
        return []
    messages = payload.get("messages")
    if not isinstance(messages, list):
        log.warning("whapi inbound dropped: no messages list keys=%s",
                    sorted(payload.keys()))
        return []
    allowed = _allowlist()
    if not allowed:
        log.warning("whapi inbound: WHAPI_ALLOWLIST is not set "
                    "(demo join-codes still accepted)")
    own = _own_number()
    stamp = time.time() if now is None else now
    out: list[IncomingMessage] = []
    tips: list[tuple[str, str]] = []  # (shop_id, mid) for once-a-day tip
    for m in messages:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id", "") or "")
        if not mid:
            continue
        if m.get("from_me"):
            continue  # own/API echo: never reply, prevents loops
        chat_id = str(m.get("chat_id", "") or "")
        if not chat_id.endswith("@s.whatsapp.net"):
            log.info("whapi inbound dropped: non-1:1 chat_id=%r id=%s",
                     chat_id, mid)
            continue
        shop_id = _digits(str(m.get("from", "") or chat_id.split("@")[0]))
        if not shop_id:
            log.warning("whapi inbound dropped: no sender id=%s", mid)
            continue
        if shop_id == own:
            continue  # the linked account's own messages: never process
        if shop_id not in allowed:
            # Demo path: join code in text, or an existing demo shop.
            # Everything else gets a tip (no LLM) via the webhook handler.
            body_guess = ""
            if str(m.get("type", "") or "") == "text":
                node = m.get("text") or {}
                if isinstance(node, dict):
                    body_guess = node.get("body", "") or ""
            from app import demo as _demo
            matched, _lang = _demo.extract_join(
                body_guess if isinstance(body_guess, str) else "")
            is_demo = False
            if not matched:
                try:
                    from app.db import connect, init_db
                    _c = connect()
                    try:
                        init_db(_c)
                        is_demo = _demo.is_demo_shop(_c, shop_id)
                    finally:
                        _c.close()
                except Exception:
                    log.warning("demo lookup failed", exc_info=True)
            if not matched and not is_demo:
                if str(m.get("type", "") or "") == "text":
                    log.info("whapi inbound tip-candidate %s id=%s",
                             shop_id, mid)
                    tips.append((shop_id, mid))
                else:
                    log.warning("whapi inbound dropped: %s not in allowlist "
                                "id=%s", shop_id, mid)
                continue
            log.info("whapi inbound demo path %s id=%s join=%s",
                     shop_id, mid, matched)
        if not _fresh(m.get("timestamp"), stamp):
            log.info("whapi inbound dropped: stale ts=%r id=%s",
                     m.get("timestamp"), mid)
            continue
        ts = str(m.get("timestamp", "") or "")
        mtype = str(m.get("type", "") or "")
        if mtype == "text":
            node = m.get("text") or {}
            body = node.get("body", "") if isinstance(node, dict) else ""
            out.append(IncomingMessage(mid, shop_id, "text",
                                       body if isinstance(body, str) else "",
                                       ts, channel="whapi"))
        elif mtype == "image":
            node = m.get("image") or {}
            if not isinstance(node, dict):
                node = {}
            caption = node.get("caption", "")
            out.append(IncomingMessage(
                mid, shop_id, "media:image", None, ts, channel="whapi",
                media_id=str(node.get("id", "") or "") or None,
                media_url=node.get("link") or None,
                caption=caption if isinstance(caption, str) else ""))
        elif mtype in ("voice", "audio", "ptt"):
            node = m.get(mtype) or {}
            if not isinstance(node, dict):
                node = {}
            out.append(IncomingMessage(
                mid, shop_id, "media:audio", None, ts, channel="whapi",
                media_id=str(node.get("id", "") or "") or None,
                media_url=node.get("link") or None))
        elif mtype in ("document", "video"):
            node = m.get(mtype) or {}
            if not isinstance(node, dict):
                node = {}
            out.append(IncomingMessage(
                mid, shop_id, f"media:{mtype}", None, ts, channel="whapi",
                media_id=str(node.get("id", "") or "") or None,
                media_url=node.get("link") or None))
        elif mtype == "reply":
            node = m.get("reply") or {}
            title = ""
            if isinstance(node, dict):
                inner = node.get("buttons_reply") or node.get("list_reply") or {}
                if isinstance(inner, dict):
                    title = str(inner.get("title", "") or "")
            out.append(IncomingMessage(mid, shop_id, "button", title, ts,
                                       channel="whapi"))
        else:
            log.info("whapi inbound dropped: type=%r id=%s", m.get("type"),
                     mid)
    PENDING_TIPS.clear()
    PENDING_TIPS.extend(tips)
    return out


def parse_whapi_status(payload: dict) -> list[tuple[str, str, str]]:
    """Pull [(provider_id, outcome, detail)] from a Whapi statuses webhook.

    Acceptance ("pending"/"sent") stays "submitted"; only delivered/read
    count as delivered. Unknown shapes yield nothing (never raise).
    """
    if not isinstance(payload, dict):
        return []
    if _event_type(payload) != "statuses":
        return []
    if str(payload.get("channel_id", "")) != _channel_id():
        log.warning("whapi status dropped: channel_id=%r is not ours",
                    payload.get("channel_id"))
        return []
    statuses = payload.get("statuses")
    if not isinstance(statuses, list):
        return []
    out: list[tuple[str, str, str]] = []
    for s in statuses:
        if not isinstance(s, dict):
            continue
        mid = str(s.get("id", "") or "")
        status = str(s.get("status", "") or "").lower()
        if not mid or not status:
            continue
        log.info("whapi status id=%s to=%s status=%s", mid,
                 s.get("recipient_id"), status)
        if status in ("delivered", "read", "played", "seen"):
            out.append((mid, "delivered", ""))
        elif status in ("failed", "undelivered", "rejected", "expired",
                        "error"):
            detail = s.get("description", "") or s.get("error", "")
            out.append((mid, "failed", str(detail) or f"whapi {status}"))
        else:
            out.append((mid, "submitted", ""))
    return out
