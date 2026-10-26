#!/usr/bin/env python3
"""POST a signed fake WhatsApp message to a local Sisi server and print the reply.

No Meta account needed. The server must run with the fake sender (default when
WHATSAPP_PHONE_NUMBER_ID is unset):

    .venv/bin/python -m uvicorn app.main:app --port 8000
    APP_SECRET=dev python scripts/simulate.py "Sipho took 2 bread on the book"

Set APP_SECRET on both sides to exercise signature verification; omit it to
skip signing (local dev only).

Voice notes: pass --audio <file.ogg|file.wav> to run a local recording
through the voice pipeline (transcribe + reply), e.g.
samples/voice_sipho_credit.ogg.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import uuid

import httpx


def build_payload(shop: str, text: str, mid: str) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "sim-entry",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "27110000000",
                                 "phone_number_id": "sim-pnid"},
                    "messages": [{
                        "id": mid,
                        "from": shop,
                        "timestamp": str(int(time.time())),
                        "type": "text",
                        "text": {"body": text},
                    }],
                },
            }],
        }],
    }


def send_voice(server: str, shop: str, path: str, timeout: int) -> int:
    from pathlib import Path

    audio = Path(path).read_bytes()
    ctype = "audio/wav" if path.lower().endswith(".wav") else "audio/ogg"
    try:
        resp = httpx.post(f"{server}/_sim/voice", params={"shop": shop},
                          content=audio, headers={"Content-Type": ctype},
                          timeout=timeout + 30)
    except httpx.ConnectError:
        print(f"cannot reach {server} — is uvicorn running?")
        return 1
    if resp.status_code == 404:
        print("live sender active — dev voice endpoint disabled.")
        return 1
    if resp.status_code != 200:
        print(resp.text[:300])
        return 1
    data = resp.json()
    print(f"Heard: {data['transcript']!r}")
    print(f"Sisi: {data['reply']}")
    if data.get("buttons"):
        print("[" + "] [".join(data["buttons"]) + "]")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate one WhatsApp message to Sisi.")
    parser.add_argument("message", nargs="?", default="",
                        help="owner's text message (ignored with --audio)")
    parser.add_argument("--audio", default="",
                        help="local audio file to send as a voice note")
    parser.add_argument("--shop", default="27710000001")
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=int, default=60,
                        help="seconds to wait for the model reply")
    args = parser.parse_args()
    if args.audio:
        sys.exit(send_voice(args.server, args.shop, args.audio, args.timeout))
    if not args.message:
        parser.error("a message is required unless --audio is given")

    raw = json.dumps(build_payload(args.shop, args.message, f"wamid.sim{uuid.uuid4().hex[:12]}"),
                     separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json"}
    secret = os.environ.get("APP_SECRET", "")
    if secret:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(
            secret.encode(), raw, hashlib.sha256).hexdigest()

    try:
        resp = httpx.post(f"{args.server}/webhook", content=raw, headers=headers, timeout=10)
    except httpx.ConnectError:
        print(f"cannot reach {args.server} — is uvicorn running?")
        sys.exit(1)
    print(f"POST /webhook -> {resp.status_code}")
    if resp.status_code == 403:
        print("signature rejected — set the same APP_SECRET for server and simulator.")
        sys.exit(1)
    if resp.status_code != 200:
        print(resp.text[:300])
        sys.exit(1)

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        time.sleep(1)
        outbox = httpx.get(f"{args.server}/_sim/outbox", params={"shop": args.shop}, timeout=10)
        if outbox.status_code == 404:
            print("live sender active — the reply went to WhatsApp.")
            return
        for m in outbox.json().get("messages", []):
            print(f"Sisi: {m['body']}")
            if m.get("buttons"):
                print("[" + "] [".join(m["buttons"]) + "]")
            return
    print("timed out waiting for a reply — is the model reachable?")
    sys.exit(1)


if __name__ == "__main__":
    main()
