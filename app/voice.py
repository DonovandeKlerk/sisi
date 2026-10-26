"""Voice notes: ffmpeg conversion + Muse Voice Transcribe.

Request shape follows the live docs at https://dev.meta.ai/docs (speech-to-text):
multipart POST to /v1/asr/transcribe with a `request` JSON part
(mode/model/audioEncoding/languageBias/keywords) plus an `audio` file part,
Authorization header auth. JSON answers carry a `turns` array whose items
hold `transcript` text.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

from app import llm as _llm
from app.db import now_iso

TRANSCRIBE_URL = "https://api.meta.ai/v1/asr/transcribe"
ASR_MODEL = "muse-voice-transcribe-1.0"
MAX_AUDIO_BYTES = 10 * 1024 * 1024

KEYWORDS = [
    "Sasko", "Albany", "Iwisa", "Ace", "Tastic", "Lucky Star", "Coke", "Stoney",
    "Simba", "airtime", "data", "amagwinya", "vetkoek", "ibhuku", "skoloto",
    "ishelf", "imali",
]

CANT_HEAR = "I couldn't hear that voice note. Please send it again?"


class VoiceError(Exception):
    """User-facing voice failure; message is safe to send to the owner."""


def _language_bias() -> list[str]:
    raw = os.environ.get("SISI_LANGUAGE_BIAS", "English")
    langs = [v.strip() for v in raw.split(",") if v.strip()]
    return langs or ["English"]


def to_wav_16k_mono(audio: bytes, suffix: str = ".ogg") -> bytes:
    """Convert any WhatsApp audio to 16 kHz mono PCM wav via ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise VoiceError("Voice notes are not working right now. Please type instead.")
    if not audio:
        raise VoiceError(CANT_HEAR)
    with tempfile.TemporaryDirectory(prefix="sisi-voice-") as tmp:
        src = Path(tmp) / f"in{suffix or '.ogg'}"
        dst = Path(tmp) / "out.wav"
        src.write_bytes(audio)
        try:
            proc = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(src), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired as e:
            raise VoiceError(CANT_HEAR) from e
        if proc.returncode != 0 or not dst.exists():
            print(f"ffmpeg failed: {proc.stderr[:500]}", file=sys.stderr)
            raise VoiceError(CANT_HEAR)
        return dst.read_bytes()


def _print_api_error(body: str) -> None:
    print("Sisi voice API error (full body):", file=sys.stderr)
    print(body[:4000], file=sys.stderr)


def _log_stt(shop_id: str, wav_bytes: int) -> None:
    try:
        with open(_llm.TOKEN_LOG, "a") as f:
            f.write(json.dumps({"ts": now_iso(), "shop": shop_id,
                                "model": ASR_MODEL, "kind": "stt",
                                "wav_bytes": wav_bytes}) + "\n")
    except OSError:
        pass


def transcribe_wav(wav: bytes, shop_id: str = "", extra_keywords: list[str] | None = None) -> str:
    """Send 16 kHz mono wav to Muse Voice Transcribe; return the transcript."""
    key = os.environ.get("MODEL_API_KEY", "")
    if not key:
        raise VoiceError("Voice notes are not working right now. Please type instead.")
    if len(wav) > MAX_AUDIO_BYTES:
        raise VoiceError("That voice note is too long. Send a shorter one?")
    keywords = list(KEYWORDS)
    for kw in extra_keywords or []:
        if kw and kw not in keywords:
            keywords.append(kw)
    request_part = json.dumps({
        "mode": "ENDPOINTING",
        "model": ASR_MODEL,
        "audioEncoding": "WAV",
        "languageBias": _language_bias(),
        "keywords": keywords[:50],
    })
    try:
        resp = httpx.post(
            TRANSCRIBE_URL,
            headers={"Authorization": f"Bearer {key}"},
            data={"request": request_part},
            files={"audio": ("voice.wav", wav, "audio/wav")},
            timeout=60,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        _print_api_error(e.response.text)
        raise VoiceError(CANT_HEAR) from e
    except httpx.HTTPError as e:
        print(f"Sisi voice request failed: {e!r}"[:500], file=sys.stderr)
        raise VoiceError(CANT_HEAR) from e
    _log_stt(shop_id, len(wav))
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        turns = payload.get("turns") or []
        text = " ".join(t.get("transcript", "") for t in turns
                        if isinstance(t, dict) and t.get("transcript")).strip()
        if not text:
            text = str(payload.get("transcript") or payload.get("text") or "").strip()
        return text
    return " ".join(ln.strip() for ln in resp.text.splitlines() if ln.strip())


def transcribe_audio(audio: bytes, suffix: str = ".ogg", shop_id: str = "",
                   extra_keywords: list[str] | None = None) -> str:
    """Convert WhatsApp audio and transcribe it. Returns '' when nothing heard."""
    return transcribe_wav(to_wav_16k_mono(audio, suffix), shop_id, extra_keywords)
