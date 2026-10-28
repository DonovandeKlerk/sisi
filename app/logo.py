"""Shop logo processing: background removal, trim, badge fallback.

Cut-out methods, tried in order:
  1. solid: near-solid background (any colour, incl. dark squares) sampled
     from the border pixels, flood-fill removed from the edges.
  2. rembg: isnet-general-use with alpha matting (best for graphic logos).
  3. badge: the original centred on a rounded white badge.

rembg is baked into the Modal image; locally it may be absent, in which
case removal gracefully reports unavailable and callers fall back.
"""

from __future__ import annotations

import io
import logging

log = logging.getLogger("sisi.logo")

SOLID_TOL = 36.0  # max RGB distance from the background colour
SOLID_TOL_LOOSE = 60.0  # second attempt: textured backdrops (dark slate…)
SOLID_BORDER_FRAC = 0.85  # border pixels that must match it
KEPT_MIN = 0.15  # quality gate: fraction of pixels kept
KEPT_MAX = 0.95
CORE_FRAC = 1 / 3  # centre box used by the not-cut check
CORE_MIN = 0.15  # min opaque fraction inside that box
EDGE_TOUCH_PX = 2  # opaque this close to the image edge counts as touching
KEPT_BLEED_OK = 0.60  # full-bleed designs may legitimately touch the edge


def remove_bg(image_bytes: bytes) -> bytes | None:
    """Cut out the subject with rembg (isnet, matted). None if unavailable."""
    try:
        from rembg import new_session, remove
    except Exception:
        return None
    try:
        session = new_session("isnet-general-use")
    except Exception:
        return None
    try:
        out = remove(image_bytes, session=session, alpha_matting=True)
    except Exception:
        return None
    if not out:
        return None
    return bytes(out)


def _alpha_coverage(img) -> float:
    """Fraction of pixels that are substantially opaque (alpha >= 128)."""
    alpha = img.getchannel("A")
    hist = alpha.histogram()
    opaque = sum(hist[128:])
    total = img.width * img.height
    return opaque / total if total else 0.0


def _core_intact(img) -> bool:
    """True when the logo core survived: the centre box stays mostly solid.

    Guards against removals that slice through the subject and keep only
    edge fragments.
    """
    w, h = img.size
    x0, y0 = int(w * (1 - CORE_FRAC) / 2), int(h * (1 - CORE_FRAC) / 2)
    box = img.getchannel("A").crop((x0, y0, w - x0, h - y0))
    hist = box.histogram()
    total = box.width * box.height
    return (sum(hist[128:]) / total) >= CORE_MIN if total else False


def _touches_edge(img) -> bool:
    """True when solid pixels reach the image edge (leftover background)."""
    solid = img.getchannel("A").point(lambda v: 255 if v >= 128 else 0)
    bbox = solid.getbbox()
    if not bbox:
        return False
    w, h = img.size
    return (bbox[0] <= EDGE_TOUCH_PX or bbox[1] <= EDGE_TOUCH_PX
            or bbox[2] >= w - 1 - EDGE_TOUCH_PX
            or bbox[3] >= h - 1 - EDGE_TOUCH_PX)


def _passes(img) -> tuple[bool, float, str]:
    """Quality gate: keeps 15-95%, core not cut, no edge-touching leftovers.

    Full-bleed designs (kept >= 60%) may touch the edge legitimately.
    Returns (ok, kept, reason-when-rejected).
    """
    kept = _alpha_coverage(img)
    if not KEPT_MIN <= kept <= KEPT_MAX:
        return False, kept, f"kept {kept:.0%} outside 15-95%"
    if not _core_intact(img):
        return False, kept, "logo core cut through"
    if kept < KEPT_BLEED_OK and _touches_edge(img):
        return False, kept, "cut touches image edge (leftover background)"
    return True, kept, ""


def trim_transparent(img):
    """Crop to content using the alpha channel. Returns the same image if empty."""
    bbox = img.getchannel("A").getbbox()
    if not bbox:
        return img
    return img.crop(bbox)


def _dist(a: tuple, b: tuple) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def solid_bg_cutout(img, tol: float = SOLID_TOL):
    """Remove a near-solid background, or None when the border is mixed.

    Samples every border pixel; when one colour dominates (white, black,
    or a coloured square), flood-fills it away from the edges with a 1px
    feather. Interior regions of the same colour are left alone.
    """
    from PIL import Image, ImageDraw, ImageFilter

    rgb = img.convert("RGB")
    w, h = rgb.size
    if w < 3 or h < 3:
        return None
    px = rgb.load()
    border = ([px[x, 0] for x in range(w)] + [px[x, h - 1] for x in range(w)]
              + [px[0, y] for y in range(1, h - 1)]
              + [px[w - 1, y] for y in range(1, h - 1)])
    mid = len(border) // 2
    bg = (sorted(c[0] for c in border)[mid],
          sorted(c[1] for c in border)[mid],
          sorted(c[2] for c in border)[mid])
    near = sum(1 for c in border if _dist(c, bg) <= tol)
    if near / len(border) < SOLID_BORDER_FRAC:
        return None
    base_alpha = img.getchannel("A") if img.mode == "RGBA" else None
    work = Image.new("L", (w, h), 255)
    wpix = work.load()
    apix = base_alpha.load() if base_alpha else None
    for y in range(h):
        for x in range(w):
            if apix is not None and apix[x, y] <= 128:
                continue  # already transparent; output keeps source alpha
            if _dist(px[x, y], bg) <= tol:
                wpix[x, y] = 0
    work = work.filter(ImageFilter.MedianFilter(3))  # kill speckle noise
    seeds = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
             (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]
    for s in seeds:
        if work.getpixel(s) == 0:
            ImageDraw.floodfill(work, s, 128, thresh=0)
    alpha = Image.new("L", (w, h), 255)
    apix_out = alpha.load()
    for y in range(h):
        for x in range(w):
            v = work.getpixel((x, y))
            if v == 128:
                apix_out[x, y] = 0
            elif base_alpha is not None:
                apix_out[x, y] = base_alpha.getpixel((x, y))
    alpha = alpha.filter(ImageFilter.GaussianBlur(1))
    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    return out


def process_upload(image_bytes: bytes, max_side: int = 512) -> dict:
    """Process an owner logo upload.

    Returns {"cutout": RGBA bytes or None, "original": RGBA bytes,
    "badge": RGBA bytes, "used_cutout": bool, "method": "solid"|"rembg"|
    "badge", "kept": float, "fallback_reason": str}.
    """
    from PIL import Image
    img = Image.open(io.BytesIO(image_bytes or b"")).convert("RGBA")
    img.thumbnail((max_side * 2, max_side * 2))
    original = img.copy()
    cutout = None
    method = "badge"
    kept = 1.0
    reason = ""
    for tol in (SOLID_TOL, SOLID_TOL_LOOSE):
        try:
            solid = solid_bg_cutout(img, tol)
        except Exception:
            solid = None
        if solid is None:
            reason = "no solid background"
            continue
        ok, kept, why = _passes(solid)
        if ok:
            cutout, method = trim_transparent(solid), "solid"
            reason = ""
            break
        reason = f"solid cut failed quality ({why})"
    if cutout is None:
        rem = remove_bg(image_bytes)
        if rem:
            try:
                candidate = Image.open(io.BytesIO(rem)).convert("RGBA")
                candidate.thumbnail((max_side * 2, max_side * 2))
                ok, kept, why = _passes(candidate)
                if ok:
                    cutout, method = trim_transparent(candidate), "rembg"
                    reason = ""
                else:
                    bit = f"rembg failed quality ({why})"
                    reason = f"{reason}; {bit}" if reason else bit
            except Exception:
                reason = f"{reason}; rembg output unreadable" if reason \
                    else "rembg output unreadable"
        else:
            reason = f"{reason}; rembg unavailable" if reason \
                else "rembg unavailable"
    if cutout is None:
        method = "badge"
    badge = rounded_badge(original, max_side)
    out: dict = {"cutout": None, "original": None, "badge": None,
                 "used_cutout": cutout is not None, "method": method,
                 "kept": kept if cutout is not None else 1.0,
                 "fallback_reason": "" if cutout is not None else reason}
    for key, im in (("cutout", cutout), ("original", original),
                    ("badge", badge)):
        if im is None:
            continue
        buf = io.BytesIO()
        im.save(buf, "PNG")
        out[key] = buf.getvalue()
    return out


def log_polish(shop_id: str, done: dict) -> None:
    """Log one polish: shop, method, kept %, fallback reason."""
    log.info("logo polish shop=%s method=%s kept=%.1f%% reason=%s", shop_id,
             done.get("method", "?"), 100 * done.get("kept", 0.0),
             done.get("fallback_reason", "") or "-")


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
