"""Shop logo tiles: the owner's original, shown as-is.

No background removal: the original artwork is contain-fitted (never
cropped or stretched) into a white rounded tile with a subtle border,
used by every document header.
"""

from __future__ import annotations

import io

TILE_BG = (255, 255, 255, 255)
TILE_BORDER_COLOR = (214, 211, 209, 255)


def render_tile(image_bytes: bytes, size: int = 192) -> bytes:
    """Contain-fit artwork into a rounded bordered tile. Returns PNG bytes.

    Corner radius is 1/8 of the tile (12px at the 96px display size) and
    the border scales the same way (1px at display size).
    """
    from PIL import Image, ImageDraw
    img = Image.open(io.BytesIO(image_bytes or b"")).convert("RGBA")
    radius = max(2, size // 8)
    border = max(1, round(size / 96))
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=radius, fill=TILE_BG,
        outline=TILE_BORDER_COLOR, width=border)
    pad = max(2, size // 12)
    art = img.copy()
    art.thumbnail((size - 2 * pad, size - 2 * pad), Image.LANCZOS)
    tile.alpha_composite(
        _centered(size, art), (0, 0))
    buf = io.BytesIO()
    tile.save(buf, "PNG")
    return buf.getvalue()


def _centered(size: int, art):
    from PIL import Image
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(art, ((size - art.width) // 2, (size - art.height) // 2),
                 art)
    return canvas
