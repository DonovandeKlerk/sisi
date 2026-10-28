"""Logos show as-is in a header tile; replacement via pending state."""

import io

import pytest

from app import logo as logo_mod
from app import reports
from app.agent import LAST_PHOTO, MEDIA_OUTBOX, handle_photo, handle_text
from app.tools import ensure_shop


SHOP = "27710000015"


def _photo_bytes(size=(300, 200), color=(20, 80, 40)):
    from PIL import Image
    img = Image.new("RGB", size, "white")
    px = img.load()
    for x in range(100, 200):
        for y in range(60, 140):
            px[x, y] = color
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
    monkeypatch.delenv("SISI_PUBLIC_URL", raising=False)
    MEDIA_OUTBOX.clear()
    LAST_PHOTO.clear()


def test_tile_renders_rounded_with_border():
    from PIL import Image
    tile = Image.open(io.BytesIO(logo_mod.render_tile(_photo_bytes())))
    assert tile.size == (192, 192)
    assert tile.mode == "RGBA"
    assert tile.getpixel((0, 0))[3] == 0  # rounded corners transparent
    assert tile.getpixel((191, 191))[3] == 0
    assert tile.getpixel((96, 0))[:3] == (214, 211, 209)  # 1px border
    assert tile.getpixel((96, 96))[:3] == (20, 80, 40)  # art kept as-is


def test_tile_never_crops_or_stretches():
    from PIL import Image
    wide = Image.new("RGB", (400, 100), (200, 30, 30))
    buf = io.BytesIO()
    wide.save(buf, "PNG")
    tile = Image.open(io.BytesIO(logo_mod.render_tile(buf.getvalue())))
    assert tile.getpixel((4, 96))[:3] == (255, 255, 255)  # padded, not cropped
    assert tile.getpixel((96, 96))[:3] == (200, 30, 30)


def test_logo_caption_upload_stages_until_yes(conn):
    ensure_shop(conn, SHOP, "Logo Spaza")
    _, reply, buttons = handle_photo(conn, SHOP, _photo_bytes(), "logo")
    assert "New logo?" in reply
    assert buttons == ["Yes", "No"]
    assert reports.shop_brand(conn, SHOP)["logo"] == ""  # live logo untouched
    msg, _ = handle_text(conn, SHOP, "yes")
    assert "Logo updated" in msg
    brand = reports.shop_brand(conn, SHOP)
    assert brand["logo"].endswith("-tile.png")
    assert brand["logo_orig"].endswith("-orig.png")
    from PIL import Image
    assert Image.open(brand["logo"]).size == (192, 192)
    assert Image.open(brand["logo_orig"]).size == (300, 200)


def test_logo_no_keeps_old_logo(conn):
    ensure_shop(conn, SHOP, "Logo Spaza")
    handle_photo(conn, SHOP, _photo_bytes(), "logo")
    msg, _ = handle_text(conn, SHOP, "no")
    assert "Kept your old logo" in msg
    assert reports.shop_brand(conn, SHOP)["logo"] == ""


def test_new_logo_pending_next_image_is_logo(conn, monkeypatch):
    import app.agent as agent
    monkeypatch.setenv("SISI_PUBLIC_URL", "https://x.example")

    def _boom(*a, **k):
        raise AssertionError("vision must not read a pending logo")

    monkeypatch.setattr(agent, "read_shop_photo", _boom)
    ensure_shop(conn, SHOP, "Logo Spaza")
    msg, _ = handle_text(conn, SHOP, "new logo")
    assert msg.startswith("📷")
    assert conn.execute("SELECT value FROM flags WHERE shop_id = ?"
                        " AND key = 'logo-pending'", (SHOP,)).fetchone()
    _, reply, buttons = handle_photo(conn, SHOP, _photo_bytes())  # no caption
    assert "New logo?" in reply
    assert buttons == ["Yes", "No"]
    assert reports.shop_brand(conn, SHOP)["logo"] == ""  # staged, not live
    assert conn.execute("SELECT value FROM flags WHERE shop_id = ?"
                        " AND key = 'logo-pending'", (SHOP,)).fetchone() is None
    from app.agent import take_media
    items = take_media(SHOP)
    assert len(items) == 1 and items[0]["kind"] == "image"  # header preview
    msg, _ = handle_text(conn, SHOP, "yes")
    assert "Logo updated" in msg
    assert reports.shop_brand(conn, SHOP)["logo"].endswith("-tile.png")


@pytest.mark.parametrize("word", ["redo logo", "fix logo", "new logo",
                                  "change logo", "lungisa ilogo",
                                  "phinda wenze ilogo", "ilogo entsha",
                                  "shintsha ilogo", "maak logo reg",
                                  "doen logo oor", "nuwe logo",
                                  "verander logo", "yenzi ilogo kwakhona",
                                  "tshintsha ilogo"])
def test_pending_words_all_languages(conn, word):
    ensure_shop(conn, SHOP)
    msg, _ = handle_text(conn, SHOP, word)
    assert msg.startswith("📷")
    assert conn.execute("SELECT value FROM flags WHERE shop_id = ?"
                        " AND key = 'logo-pending'", (SHOP,)).fetchone()


def test_invalid_pending_image_keeps_pending(conn):
    ensure_shop(conn, SHOP)
    handle_text(conn, SHOP, "change logo")
    _, reply, _ = handle_photo(conn, SHOP, b"junk-bytes")
    assert "not a photo" in reply
    assert conn.execute("SELECT value FROM flags WHERE shop_id = ?"
                        " AND key = 'logo-pending'", (SHOP,)).fetchone()
    _, reply, _ = handle_photo(conn, SHOP, _photo_bytes())
    assert "New logo?" in reply
    msg, _ = handle_text(conn, SHOP, "yes")
    assert "Logo updated" in msg


def test_use_original_now_as_is(conn):
    ensure_shop(conn, SHOP)
    msg, _ = handle_text(conn, SHOP, "use original logo")
    assert "already shows as-is" in msg
    msg, _ = handle_text(conn, SHOP, "remove logo background")
    assert msg.startswith("📷")


def test_yes_applies_staged_logo(conn):
    ensure_shop(conn, SHOP, "Logo Spaza")
    handle_photo(conn, SHOP, _photo_bytes(), "logo")
    msg, _ = handle_text(conn, SHOP, "yes")
    assert "Logo updated" in msg
    msg, _ = handle_text(conn, SHOP, "yes")
    assert "nothing waiting" in msg


def test_legacy_logo_migrates_to_tile(conn, tmp_path):
    from pathlib import Path
    ensure_shop(conn, SHOP, "Old Spaza")
    logos = Path(reports.LOGOS_DIR)
    logos.mkdir(parents=True, exist_ok=True)
    old = logos / f"{SHOP}-badge.png"
    old.write_bytes(_photo_bytes())
    conn.execute("UPDATE shops SET logo_path = ?, logo_orig_path = ?"
                 " WHERE shop_id = ?", (str(old), str(old), SHOP))
    conn.commit()
    brand = reports.shop_brand(conn, SHOP)
    assert brand["logo"].endswith("-tile.png")
    assert Path(brand["logo"]).exists()
    assert reports.shop_brand(conn, SHOP)["logo"] == brand["logo"]


def test_logo_on_every_document(conn, tmp_path):
    ensure_shop(conn, SHOP, "Logo Spaza")
    handle_photo(conn, SHOP, _photo_bytes(), "logo")
    handle_text(conn, SHOP, "yes")
    brand = reports.shop_brand(conn, SHOP)
    plain = {"name": "Logo Spaza", "address": "", "phone": "", "logo": "",
             "logo_orig": ""}
    month = reports.month_data(conn, SHOP, "2026-10")
    pdf = reports.render_pdf(month, str(tmp_path / "m.pdf"), brand)
    assert open(pdf, "rb").read().count(b"/Image") >= 1
    card = reports.render_card(month, str(tmp_path / "m.png"), brand)
    naked = reports.render_card(month, str(tmp_path / "m0.png"), plain)
    assert open(card, "rb").read() != open(naked, "rb").read()
    data = {"name": "Sipho", "date": "2026-10-27", "items": [], "payments": [],
            "total": 0}
    spdf = reports.render_statement_pdf(data, brand, str(tmp_path / "s.pdf"))
    assert open(spdf, "rb").read().count(b"/Image") >= 1
    spages = reports.render_statement_pngs(data, brand, str(tmp_path / "s"))
    assert len(spages) == 1
    opath = reports.render_order_png({"lines": []}, brand,
                                     str(tmp_path / "o.png"), "2026-10-27")
    assert open(opath, "rb").read(8) == b"\x89PNG\r\n\x1a\n"
    snap = reports.render_snapshot_png(
        {"sales": 0, "gross": 0, "best": "—", "best_cents": 0, "slowest": "—",
         "slow_cents": 0, "cash": 0, "owed": 0, "tip": "hi"}, brand,
        str(tmp_path / "snap.png"), "2026-11-01")
    assert open(snap, "rb").read(8) == b"\x89PNG\r\n\x1a\n"
    prof = reports.render_shop_card(brand, str(tmp_path / "p.png"))
    from PIL import Image
    assert Image.open(prof).size == (1080, 640)
