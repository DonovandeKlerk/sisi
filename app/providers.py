"""Explicit channel provider selection.

Replies normally go out on the channel a message arrived on. Setting
``SISI_PROVIDER`` to ``meta``, ``vonage`` or ``infobip`` locks ALL sending
to that one provider: any attempt to send via another channel raises
``WrongProviderError`` instead of silently crossing providers. Unset (the
default) preserves the legacy per-channel routing.

All sender-selection sites (webhook replies, scheduled sends, the media
worker, queue drains, immediate sends) resolve through
``sender_for_channel`` so the rule has exactly one home.
"""

from __future__ import annotations

import os

CHANNELS = ("meta", "vonage", "infobip")
QUEUED_CHANNELS = ("vonage", "infobip")
"""Channels whose sends pace through the SQLite queue (strict rate limits)."""


class WrongProviderError(ValueError):
    """A send was attempted via a non-selected provider, and refused."""


def selected_provider() -> str:
    """The locked provider from SISI_PROVIDER, or "" for per-channel."""
    value = (os.environ.get("SISI_PROVIDER", "") or "").strip().lower()
    if not value:
        return ""
    if value not in CHANNELS:
        raise WrongProviderError(
            f"SISI_PROVIDER={value!r} is invalid; use one of "
            f"{', '.join(CHANNELS)} (or leave unset for per-channel).")
    return value


def sender_for_channel(channel: str | None):
    """Return the sender for a channel, enforcing the provider lock.

    Unknown channels raise instead of silently falling back to Meta.
    """
    code = (channel or "meta").strip().lower() or "meta"
    if code not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}; refusing silent fallback")
    selected = selected_provider()
    if selected and code != selected:
        raise WrongProviderError(
            f"SISI_PROVIDER={selected}: refusing {code} send")
    if code == "vonage":
        from app.vonage import VonageSender

        return VonageSender()
    if code == "infobip":
        from app.infobip import InfobipSender

        return InfobipSender()
    from app.whatsapp import get_sender

    return get_sender()
