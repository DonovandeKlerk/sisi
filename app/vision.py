"""Photo reading via Muse Spark vision + strict structured output.

Request shape follows the live docs at https://dev.meta.ai/docs
(image-understanding, structured-output): Chat Completions with the image as
a base64 data URL in an `image_url` content block, plus
`response_format: {"type": "json_schema", ...}` with `additionalProperties:
false` on every object and full `required` arrays, so decoding itself is
constrained to the schema.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

from app import config
from app.llm import get_client, log_usage, print_api_error

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_SIDE_PX = 2048

EXPIRY_SCHEMA = {
    "name": "expiry_findings",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "stamp_count": {"type": "number"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "product": {"type": "string"},
                        "qty": {"type": "number"},
                        "expiry_date": {"type": "string"},
                    },
                    "required": ["product", "qty", "expiry_date"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["stamp_count", "items"],
        "additionalProperties": False,
    },
}

EXPIRY_PROMPT = (
    "This photo is from a small shop: a shelf, a fridge, or groceries on a counter. "
    "First count every readable best-before/BB, use-by, or expiry/EXP date stamp in "
    "the photo and put that number in stamp_count. Then report one entry per stamp: "
    "product name as printed on the pack, how many identical items show that same "
    "date, and the date as YYYY-MM-DD. South African packs print dates as DD/MM/YYYY "
    "— convert carefully (18/02/2027 means 2027-02-18). Use the best-before/expiry "
    "date, never the production (PD) or manufacture (MAN) date. Use only dates you "
    "can actually read; report every stamp you counted. If no dates are readable, "
    "set stamp_count to 0 with an empty items list."
)

CANT_READ = "I couldn't read that photo. Try a closer shot of the date stamp."


class VisionError(Exception):
    """User-facing vision failure; message is safe to send to the owner."""


def _prepare_image(image: bytes) -> tuple[bytes, str]:
    """Downscale to MAX_SIDE_PX jpeg via ffmpeg when available, else original."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return image, "image/jpeg"
    with tempfile.TemporaryDirectory(prefix="sisi-photo-") as tmp:
        src = Path(tmp) / "in.jpg"
        dst = Path(tmp) / "out.jpg"
        src.write_bytes(image)
        try:
            proc = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                 "-vf", f"scale='if(gt(iw,ih),{MAX_SIDE_PX},-2)':'if(gt(iw,ih),-2,{MAX_SIDE_PX})'",
                 "-q:v", "3", str(dst)],
                capture_output=True, text=True, timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError):
            return image, "image/jpeg"
        if proc.returncode != 0 or not dst.exists():
            return image, "image/jpeg"
        return dst.read_bytes(), "image/jpeg"


def _complete_vision(messages: list, response_format: dict) -> object:
    """Thin seam over the API call so tests can stub it."""
    return get_client().chat.completions.create(
        model=config.model_name(), messages=messages, response_format=response_format,
        temperature=0,  # extraction wants determinism, not creativity
    )


def read_expiry_photo(image: bytes, shop_id: str = "") -> list[dict]:
    """Read expiry findings from a photo. Returns [] when no dates are readable."""
    if not os.environ.get("MODEL_API_KEY", ""):
        raise VisionError("I can't read photos right now. Tell me the dates in words?")
    if not image or len(image) > MAX_IMAGE_BYTES:
        raise VisionError(CANT_READ)
    prepared, mime = _prepare_image(image)
    data_url = f"data:{mime};base64," + base64.b64encode(prepared).decode("ascii")
    messages = [{"role": "user", "content": [
        {"type": "text", "text": EXPIRY_PROMPT},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]}]
    try:
        resp = _complete_vision(
            messages, {"type": "json_schema", "json_schema": EXPIRY_SCHEMA})
    except Exception as e:  # noqa: BLE001 - API outage must not crash the chat
        print_api_error(e)
        raise VisionError(CANT_READ) from e
    log_usage(shop_id, config.model_name(), getattr(resp, "usage", None))
    try:
        payload = json.loads(resp.choices[0].message.content or "{}")
    except (ValueError, AttributeError, IndexError):
        raise VisionError(CANT_READ) from None
    findings = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("product", "")).strip()
        try:
            qty = float(item.get("qty", 0))
        except (TypeError, ValueError):
            qty = 0
        exp = str(item.get("expiry_date", "")).strip()
        try:
            date.fromisoformat(exp)
        except ValueError:
            continue
        if name and qty > 0:
            findings.append({"product": name, "qty": qty, "expiry_date": exp})
    return findings
