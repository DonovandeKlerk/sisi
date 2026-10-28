"""Logo fix: solid-cutout first, isnet rembg, repolish of old shops."""

import io
import logging

import pytest

from app import logo as logo_mod
from app import reports
from app.agent import handle_text, take_media
from app.tools import ensure_shop

SHOP = "27710000042"


def _dark_square_logo():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (200, 200), (12, 12, 14))
    d = ImageDraw.Draw(img)
    d.ellipse([40, 40, 160, 160], fill=(30, 140, 60))
    d.rectangle([85, 85, 115, 115], fill=(240, 240, 240))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _white_logo():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (200, 200), "white")
    ImageDraw.Draw(img).rectangle([40, 40, 160, 160], fill=(200, 30, 30))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _busy_photo():
    from PIL import Image
    img = Image.new("RGB", (200, 200))
    px = img.load()
    for x in range(200):
        for y in range(200):
            px[x, y] = (200, 30, 30) if (x // 10 + y // 10) % 2 \
                else (30, 60, 200)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _centred_cutout():
    from PIL import Image
    img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    px = img.load()
    for x in range(30, 170):
        for y in range(30, 170):
            px[x, y] = (20, 80, 40, 255)
    for x in range(80, 120):
        for y in range(80, 120):
            px[x, y] = (0, 0, 0, 0)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _corner_fragment():
    from PIL import Image
    img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    px = img.load()
    for x in range(80):
        for y in range(80):
            px[x, y] = (20, 80, 40, 255)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _no_setup(conn):
    from app.setup import skip_setup
    skip_setup(conn, SHOP)


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(reports, "LOGOS_DIR", tmp_path / "logos")
    monkeypatch.setenv("SISI_PUBLIC_URL", "https://x.example")


def test_dark_square_logo_loses_the_square(monkeypatch):
    from PIL import Image
    monkeypatch.setattr(logo_mod, "remove_bg", lambda b: None)
    done = logo_mod.process_upload(_dark_square_logo())
    assert done["method"] == "solid"
    assert done["used_cutout"]
    assert 0.15 <= done["kept"] <= 0.95
    cut = Image.open(io.BytesIO(done["cutout"]))
    assert cut.size[0] < 200  # trimmed to content
    assert cut.getpixel((1, 1))[3] == 0  # square gone at the corners
    cx, cy = cut.size[0] // 2, cut.size[1] // 2
    assert cut.getpixel((cx, cy))[3] == 255  # inner design kept


def test_white_bg_logo_is_cut(monkeypatch):
    from PIL import Image
    monkeypatch.setattr(logo_mod, "remove_bg", lambda b: None)
    done = logo_mod.process_upload(_white_logo())
    assert done["method"] == "solid"
    assert done["used_cutout"]
    cut = Image.open(io.BytesIO(done["cutout"]))
    assert cut.getpixel((1, 1))[3] == 0


def test_photo_falls_back_to_badge(monkeypatch):
    from PIL import Image
    monkeypatch.setattr(logo_mod, "remove_bg", lambda b: None)
    done = logo_mod.process_upload(_busy_photo())
    assert done["method"] == "badge"
    assert not done["used_cutout"]
    assert "rembg unavailable" in done["fallback_reason"]
    badge = Image.open(io.BytesIO(done["badge"]))
    assert badge.size == (512, 512)


def test_rembg_used_when_no_solid_bg(monkeypatch):
    from PIL import Image
    monkeypatch.setattr(logo_mod, "remove_bg", lambda b: _centred_cutout())
    done = logo_mod.process_upload(_busy_photo())
    assert done["method"] == "rembg"
    assert done["used_cutout"]
    cut = Image.open(io.BytesIO(done["cutout"]))
    assert cut.size == (140, 140)


def test_rembg_corner_fragment_rejected(monkeypatch):
    monkeypatch.setattr(logo_mod, "remove_bg", lambda b: _corner_fragment())
    done = logo_mod.process_upload(_busy_photo())
    assert done["method"] == "badge"
    assert "rembg failed quality" in done["fallback_reason"]


def _seed_old_shop(fc, orig_bytes, mode=None):
    from pathlib import Path
    ensure_shop(fc, SHOP, "Old Spaza")
    logos = Path(reports.LOGOS_DIR)
    logos.mkdir(parents=True, exist_ok=True)
    orig = logos / f"{SHOP}-orig.png"
    orig.write_bytes(orig_bytes)
    old = logos / f"{SHOP}-badge.png"
    old.write_bytes(orig_bytes)
    fc.execute("UPDATE shops SET logo_orig_path = ?, logo_path = ?"
               " WHERE shop_id = ?", (str(orig), str(old), SHOP))
    if mode:
        fc.execute("INSERT INTO flags (shop_id, key, value)"
                   " VALUES (?, 'logo-mode', ?)", (SHOP, mode))
    fc.commit()
    return orig


def test_startup_repolish_old_shop(tmp_path, monkeypatch):
    from PIL import Image

    from app.db import connect as file_connect
    from app.db import init_db
    from app.setup import repolish_scan

    monkeypatch.setenv("SISI_DB", str(tmp_path / "old.db"))
    monkeypatch.setattr(logo_mod, "remove_bg", lambda b: None)
    fc = file_connect()
    init_db(fc)
    _seed_old_shop(fc, _dark_square_logo())
    jobs = repolish_scan(fc)
    assert list(jobs) == [SHOP]
    jobs[SHOP].join(timeout=30)
    assert not jobs[SHOP].is_alive()
    flags = {r["key"]: r["value"] for r in fc.execute(
        "SELECT key, value FROM flags WHERE shop_id = ?", (SHOP,))}
    assert flags["logo-method"] == "solid"
    assert flags["logo-polish"] == "done"
    shop = fc.execute("SELECT logo_path FROM shops WHERE shop_id = ?",
                      (SHOP,)).fetchone()
    assert shop["logo_path"].endswith("-cutout.png")
    cut = Image.open(shop["logo_path"])
    assert cut.getpixel((1, 1))[3] == 0
    assert repolish_scan(fc) == {}
    fc.close()


def test_repolish_preserves_original_mode(tmp_path, monkeypatch):
    from app.db import connect as file_connect
    from app.db import init_db
    from app.setup import repolish_scan

    monkeypatch.setenv("SISI_DB", str(tmp_path / "oldmode.db"))
    monkeypatch.setattr(logo_mod, "remove_bg", lambda b: None)
    fc = file_connect()
    init_db(fc)
    _seed_old_shop(fc, _dark_square_logo(), mode="original")
    jobs = repolish_scan(fc)
    jobs[SHOP].join(timeout=30)
    flags = {r["key"]: r["value"] for r in fc.execute(
        "SELECT key, value FROM flags WHERE shop_id = ?", (SHOP,))}
    assert flags["logo-mode"] == "original"
    assert flags["logo-method"] == "solid"
    assert reports.shop_brand(fc, SHOP)["logo"].endswith("-orig.png")
    fc.close()


@pytest.mark.parametrize("word", ["fix logo", "redo logo", "lungisa ilogo",
                                  "phinda wenze ilogo", "maak logo reg",
                                  "doen logo oor", "yenzi ilogo kwakhona"])
def test_redo_logo_command(conn, monkeypatch, caplog, word):
    monkeypatch.setattr(logo_mod, "remove_bg", lambda b: None)
    caplog.set_level(logging.INFO, logger="sisi.logo")
    _seed_old_shop(conn, _dark_square_logo())
    msg, _ = handle_text(conn, SHOP, word)
    assert msg.startswith("🔄")  # redo reply in the message's language
    assert "method=solid" in caplog.text
    brand = reports.shop_brand(conn, SHOP)
    assert brand["logo"].endswith("-cutout.png")
    items = take_media(SHOP)
    assert len(items) == 1 and items[0]["kind"] == "image"
    from pathlib import Path
    assert Path(reports.REPORTS_DIR,
                f"{SHOP}-logofix.png").exists()
    msg, _ = handle_text(conn, SHOP, "use original logo")
    assert "original" in msg
    assert reports.shop_brand(conn, SHOP)["logo"].endswith("-orig.png")


def test_redo_logo_without_logo(conn):
    from app.setup import skip_setup
    skip_setup(conn, "27710000043")
    msg, _ = handle_text(conn, "27710000043", "fix logo")
    assert "No logo yet" in msg
