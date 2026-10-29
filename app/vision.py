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
from app.i18n import STRINGS
from app.i18n import t as _t
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

CANT_READ = STRINGS["en"]["vis_cant_read"]

SHOP_PHOTO_SCHEMA = {
    "name": "shop_photo",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "doc_type": {"type": "string",
                         "enum": ["expiry", "book_page", "receipt", "other"]},
            "expiry": {
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
                    },
                },
                "required": ["stamp_count", "items"],
                "additionalProperties": False,
            },
            "book": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "page_total_cents": {"type": ["integer", "null"]},
                    "crossed_out": {"type": "integer"},
                    "lines": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "line_no": {"type": "integer"},
                                "text": {"type": "string"},
                                "item": {"type": "string"},
                                "qty": {"type": "number"},
                                "price_cents": {"type": ["integer", "null"]},
                                "kind": {"type": "string", "enum": ["cash", "credit",
                                                                    "payment", "unreadable"]},
                                "customer": {"type": "string"},
                            },
                            "required": ["line_no", "text", "item", "qty",
                                         "price_cents", "kind", "customer"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["date", "page_total_cents", "crossed_out", "lines"],
                "additionalProperties": False,
            },
            "receipt": {
                "type": "object",
                "properties": {
                    "supplier": {"type": "string"},
                    "date": {"type": "string"},
                    "receipt_no": {"type": "string"},
                    "lines": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "line_no": {"type": "integer"},
                                "text": {"type": "string"},
                                "item": {"type": "string"},
                                "packs": {"type": ["number", "null"]},
                                "pack_size": {"type": ["number", "null"]},
                                "qty_units": {"type": ["number", "null"]},
                                "unit_cost_cents": {"type": ["integer", "null"]},
                                "line_total_cents": {"type": ["integer", "null"]},
                            },
                            "required": ["line_no", "text", "item", "packs",
                                         "pack_size", "qty_units", "unit_cost_cents",
                                         "line_total_cents"],
                            "additionalProperties": False,
                        },
                    },
                    "vat_cents": {"type": ["integer", "null"]},
                    "total_cents": {"type": ["integer", "null"]},
                    "payment": {"type": "string"},
                },
                "required": ["supplier", "date", "receipt_no", "lines",
                             "vat_cents", "total_cents", "payment"],
                "additionalProperties": False,
            },
            "products": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "brand": {"type": "string"},
                        "variant": {"type": "string"},
                        "size": {"type": "string"},
                        "visible_count": {"type": ["integer", "null"]},
                        "readable": {"type": "boolean"},
                        "notes": {"type": "string"},
                    },
                    "required": ["brand", "variant", "size",
                                 "visible_count", "readable", "notes"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["doc_type", "expiry", "book", "receipt", "products"],
        "additionalProperties": False,
    },
}

SHOP_PHOTO_PROMPT = (
    "This photo is from a small South African shop. First decide what it is: "
    "'expiry' (shelf/fridge/counter with date stamps), 'book_page' (a handwritten "
    "sales or credit page from the shop book), 'receipt' (a printed till slip or "
    "invoice from a wholesaler), or 'other'. Fill only that section; leave the "
    "others empty. "
    "EXPIRY: count every readable best-before/use-by/expiry stamp, one entry per "
    "stamp (product as printed, identical-item count, date as YYYY-MM-DD; SA packs "
    "print DD/MM/YYYY so convert carefully). Never use PD/MAN dates. "
    "BOOK PAGE: transcribe every written line with its line number and the raw "
    "text. Skip crossed-out lines (count them in crossed_out) and skip the page's "
    "own total line (put it in page_total_cents). For each kept line: the item as "
    "written (keep abbreviations like chap, brd, stny and isiZulu spellings as-is), "
    "qty as written (default 1; quantities are loose singles), the written price in "
    "cents or null, kind (cash sale, credit sale with the customer's name, a credit "
    "payment with the customer, or unreadable), and the customer first name or ''. "
    "RECEIPT: supplier name, receipt date as YYYY-MM-DD, receipt/invoice number, "
    "every line (item as printed, packs and pack size when shown, total single "
    "units when you can tell, unit cost and line total in cents, null when not "
    "shown), VAT in cents or null, receipt total in cents or null, and the payment "
    "word as printed (cash, card, account, or ''). Use only values you can actually "
    "read; never invent a number. "
    "PRODUCTS: whenever packaged goods are visible (whatever the doc type), "
    "list each distinct product once: brand as printed ('' when the label is "
    "hidden), variant/flavour, package size as printed ('' when unreadable), "
    "how many identical packages you can actually count (null when unsure), "
    "readable true only when the brand and size are both legible, and a short "
    "note for anything uncertain ('label turned away', 'only part visible'). "
    "A multipack's contents are not a count: report the packs you see."
)


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
        raise VisionError(_t("vis_broken"))
    if not image or len(image) > MAX_IMAGE_BYTES:
        raise VisionError(_t("vis_cant_read"))
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
        raise VisionError(_t("vis_cant_read")) from e
    log_usage(shop_id, config.model_name(), getattr(resp, "usage", None))
    try:
        payload = json.loads(resp.choices[0].message.content or "{}")
    except (ValueError, AttributeError, IndexError):
        raise VisionError(_t("vis_cant_read")) from None
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


def _sane_cents(val) -> int | None:
    try:
        val = int(val)
    except (TypeError, ValueError):
        return None
    return val if val >= 0 else None


def _sane_date(val) -> str:
    exp = str(val or "").strip()
    try:
        date.fromisoformat(exp)
    except ValueError:
        return ""
    return exp


def read_shop_photo(image: bytes, shop_id: str = "") -> dict:
    """Classify a shop photo and extract its lines (one strict-schema call)."""
    if not os.environ.get("MODEL_API_KEY", ""):
        raise VisionError(_t("vis_broken"))
    if not image or len(image) > MAX_IMAGE_BYTES:
        raise VisionError(_t("vis_cant_read"))
    prepared, mime = _prepare_image(image)
    data_url = f"data:{mime};base64," + base64.b64encode(prepared).decode("ascii")
    messages = [{"role": "user", "content": [
        {"type": "text", "text": SHOP_PHOTO_PROMPT},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]}]
    try:
        resp = _complete_vision(
            messages, {"type": "json_schema", "json_schema": SHOP_PHOTO_SCHEMA})
    except Exception as e:  # noqa: BLE001 - API outage must not crash the chat
        print_api_error(e)
        raise VisionError(_t("vis_cant_read")) from e
    log_usage(shop_id, config.model_name(), getattr(resp, "usage", None))
    try:
        doc = json.loads(resp.choices[0].message.content or "{}")
    except (ValueError, AttributeError, IndexError):
        raise VisionError(_t("vis_cant_read")) from None
    try:
        kinds = {"cash", "credit", "payment", "unreadable"}
        book = doc.get("book") or {}
        book_lines = []
        for ln in book.get("lines", []) or []:
            if not isinstance(ln, dict):
                continue
            try:
                qty = float(ln.get("qty", 0))
            except (TypeError, ValueError):
                qty = 0
            book_lines.append({
                "line_no": int(ln.get("line_no") or 0),
                "text": str(ln.get("text") or "").strip(),
                "item": str(ln.get("item") or "").strip(),
                "qty": qty if qty > 0 else 1,
                "price_cents": _sane_cents(ln.get("price_cents")),
                "kind": ln.get("kind") if ln.get("kind") in kinds else "unreadable",
                "customer": str(ln.get("customer") or "").strip(),
            })
        doc["book"] = {"date": _sane_date(book.get("date")),
                       "page_total_cents": _sane_cents(book.get("page_total_cents")),
                       "crossed_out": int(book.get("crossed_out") or 0),
                       "lines": book_lines}
        receipt = doc.get("receipt") or {}
        receipt_lines = []
        for ln in receipt.get("lines", []) or []:
            if not isinstance(ln, dict):
                continue
            receipt_lines.append({
                "line_no": int(ln.get("line_no") or 0),
                "text": str(ln.get("text") or "").strip(),
                "item": str(ln.get("item") or "").strip(),
                "packs": ln.get("packs"), "pack_size": ln.get("pack_size"),
                "qty_units": ln.get("qty_units"),
                "unit_cost_cents": _sane_cents(ln.get("unit_cost_cents")),
                "line_total_cents": _sane_cents(ln.get("line_total_cents")),
            })
        doc["receipt"] = {
            "supplier": str(receipt.get("supplier") or "").strip(),
            "date": _sane_date(receipt.get("date")),
            "receipt_no": str(receipt.get("receipt_no") or "").strip(),
            "lines": receipt_lines,
            "vat_cents": _sane_cents(receipt.get("vat_cents")),
            "total_cents": _sane_cents(receipt.get("total_cents")),
            "payment": str(receipt.get("payment") or "").strip().lower(),
        }
        products = []
        for p in doc.get("products", []) or []:
            if not isinstance(p, dict):
                continue
            try:
                count = int(p.get("visible_count") or 0)
            except (TypeError, ValueError):
                count = 0
            products.append({
                "brand": str(p.get("brand") or "").strip(),
                "variant": str(p.get("variant") or "").strip(),
                "size": str(p.get("size") or "").strip(),
                "visible_count": count if count > 0 else None,
                "readable": bool(p.get("readable")),
                "notes": str(p.get("notes") or "").strip()[:120],
            })
        doc["products"] = products
        if doc.get("doc_type") not in ("expiry", "book_page", "receipt", "other"):
            doc["doc_type"] = "other"
        return doc
    except (ValueError, AttributeError) as e:
        raise VisionError(f"{_t('vis_cant_read')} (bad vision JSON: {e})") from e
