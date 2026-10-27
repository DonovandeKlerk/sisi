"""Shop logo processing: background removal, trim, badge fallback.

rembg (u2net) is baked into the Modal image; locally it may be absent, in
which case removal gracefully reports unavailable and callers fall back.
"""

from __future__ import annotations

import io


def remove_bg(image_bytes: bytes) -> bytes | None:
    """Cut out the subject with rembg. None when unavailable or broken."""
    try:
        from rembg import remove
    except Exception:
        return None
    try:
        out = remove(image_bytes)
    except Exception:
        return None
    if not out:
        return None
    return bytes(out)


def _alpha_coverage(img) -> float:
    """Fraction of pixels that are not fully transparent (0..1)."""
    alpha = img.getchannel("A")
    hist = alpha.histogram()
    opaque = sum(hist[1:])
    total = img.width * img.height
    return opaque / total if total else 0.0


def trim_transparent(img):
    """Crop to content using the alpha channel. Returns the same image if empty."""
    bbox = img.getchannel("A").getbbox()
    if not bbox:
        return img
    return img.crop(bbox)


def process_upload(image_bytes: bytes, max_side: int = 512) -> dict:
    """Process an owner logo upload.

    Returns {"cutout": RGBA bytes or None, "original": RGBA bytes,
    "badge": RGBA bytes, "used_cutout": bool}. The badge is the original
    inside a rounded white badge; it is used when the cut-out loses more
    than 60% of pixels or removal fails.
    """
    from PIL import Image
    img = Image.open(io.BytesIO(image_bytes or b"")).convert("RGBA")
    img.thumbnail((max_side * 2, max_side * 2))
    original = img.copy()
    cut = remove_bg(image_bytes)
    cutout = None
    if cut:
        try:
            candidate = Image.open(io.BytesIO(cut)).convert("RGBA")
            candidate.thumbnail((max_side * 2, max_side * 2))
            if _alpha_coverage(candidate) >= 0.40:
                cutout = trim_transparent(candidate)
        except Exception:
            cutout = None
    badge = rounded_badge(original, max_side)
    out = {"cutout": None, "original": None, "badge": None,
           "used_cutout": cutout is not None}
    for key, im in (("cutout", cutout), ("original", original), ("badge", badge)):
        if im is None:
            continue
        buf = io.BytesIO()
        im.save(buf, "PNG")
        out[key] = buf.getvalue()
    return out


def rounded_badge(img, size: int = 512, radius: int = 90):
    """Original artwork centred on a rounded white badge (RGBA)."""
    from PIL import Image, ImageDraw
    art = img.copy().convert("RGBA")
    art.thumbnail((size - 120, size - 120))
    badge = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1],
                                           radius=radius, fill=255)
    white = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    badge.paste(white, (0, 0), mask)
    badge.paste(art, ((size - art.width) // 2, (size - art.height) // 2), art)
    return badge
