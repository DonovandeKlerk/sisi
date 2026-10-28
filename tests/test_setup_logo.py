"""Setup step 4 saves the logo synchronously and never blocks."""

import io

import pytest

from app import reports
from app.agent import MEDIA_OUTBOX, handle_photo, handle_text
from app.setup import setup_step
from app.tools import ensure_shop


SHOP = "27710000017"


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(reports, "LOGOS_DIR", tmp_path / "logos")
    monkeypatch.delenv("SISI_PUBLIC_URL", raising=False)
    MEDIA_OUTBOX.clear()


def _png(fmt="PNG", size=(120, 80)):
    from PIL import Image
    img = Image.new("RGB", size, "white")
    px = img.load()
    for x in range(40, 80):
        for y in range(20, 60):
            px[x, y] = (20, 80, 40)
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def _to_step4(conn):
    for a in ["hi", "Kwamama Spaza", "12 Main Rd", "yes"]:
        handle_text(conn, SHOP, a)
    assert setup_step(conn, SHOP) == 4


def test_step4_saves_and_advances(conn):
    _to_step4(conn)
    _, reply, _ = handle_photo(conn, SHOP, _png())
    assert "Logo saved" in reply and "Step 5 of 7" in reply
    assert setup_step(conn, SHOP) == 5
    shop = ensure_shop(conn, SHOP)
    assert shop["logo_orig_path"] != ""
    assert shop["logo_path"].endswith("-tile.png")
    assert conn.execute("SELECT value FROM flags WHERE shop_id = ?"
                        " AND key = 'logo-pending'", (SHOP,)).fetchone() is None


def test_step4_accepts_jpg_png_webp(conn):
    for fmt in ("JPEG", "PNG", "WEBP"):
        _to_step4(conn)
        _, reply, _ = handle_photo(conn, SHOP, _png(fmt))
        assert "Step 5 of 7" in reply, fmt
        from app.setup import start_setup
        start_setup(conn, SHOP)


def test_step4_invalid_bytes_skip_ahead(conn):
    _to_step4(conn)
    _, reply, _ = handle_photo(conn, SHOP, b"junk-bytes")
    assert "Skipped the logo" in reply and "Step 5 of 7" in reply
    assert setup_step(conn, SHOP) == 5


def test_full_setup_with_logo_finishes(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(reports, "LOGOS_DIR", tmp_path / "logos")
    monkeypatch.setenv("SISI_PUBLIC_URL", "https://sisi.example")
    MEDIA_OUTBOX.clear()
    _to_step4(conn)
    handle_photo(conn, SHOP, _png())
    handle_text(conn, SHOP, "1500 cash, 300 petty")
    handle_text(conn, SHOP, "english")
    msg, _ = handle_text(conn, SHOP, "yes")
    assert msg.startswith("✅ *You're all set")
    assert reports.shop_brand(conn, SHOP)["logo"].endswith("-tile.png")
