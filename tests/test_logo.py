"""Logo background removal, fallback badge, switching, doc headers."""

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


def _cutout_bytes(square=140, size=200, hole=40):
    from PIL import Image
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    off = (size - square) // 2
    hoff = (size - hole) // 2
    for x in range(off, off + square):
        for y in range(off, off + square):
            px[x, y] = (20, 80, 40, 255)
    for x in range(hoff, hoff + hole):
        for y in range(hoff, hoff + hole):
            px[x, y] = (0, 0, 0, 0)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(reports, "LOGOS_DIR", tmp_path / "logos")
    monkeypatch.delenv("SISI_PUBLIC_URL", raising=False)
    MEDIA_OUTBOX.clear()
    LAST_PHOTO.clear()


def _upload(conn, monkeypatch, cutout):
    monkeypatch.setattr(logo_mod, "remove_bg", lambda b: cutout)
    ensure_shop(conn, SHOP, "Logo Spaza")
    _, reply, _ = handle_photo(conn, SHOP, _photo_bytes(), "logo")
    return reply


def test_cutout_saved_with_transparency(conn, monkeypatch):
    from PIL import Image
    reply = _upload(conn, monkeypatch, _cutout_bytes())
    assert "removed the background" in reply
    brand = reports.shop_brand(conn, SHOP)
    assert brand["logo"].endswith("-cutout.png")
    assert brand["logo_orig"].endswith("-orig.png")
    cut = Image.open(brand["logo"])
    assert cut.mode == "RGBA"
    assert cut.size == (140, 140)  # trimmed to content
    orig = Image.open(brand["logo_orig"])
    assert orig.size == (300, 200)


def test_cutout_keeps_real_transparency(conn, monkeypatch):
    from PIL import Image
    _upload(conn, monkeypatch, _cutout_bytes())
    cut = Image.open(reports.shop_brand(conn, SHOP)["logo"])
    lo, hi = cut.getchannel("A").getextrema()
    assert (lo, hi) == (0, 255)


def test_fallback_badge_when_cutout_loses_too_much(conn, monkeypatch):
    from PIL import Image
    reply = _upload(conn, monkeypatch, _cutout_bytes(square=50))
    assert "white badge" in reply
    brand = reports.shop_brand(conn, SHOP)
    assert brand["logo"].endswith("-badge.png")
    badge = Image.open(brand["logo"])
    assert badge.size == (512, 512)
    assert badge.convert("RGB").getpixel((256, 40)) == (255, 255, 255)
    assert badge.getpixel((5, 5))[3] == 0  # rounded corners stay transparent


def test_fallback_when_rembg_missing(conn, monkeypatch):
    monkeypatch.setattr(logo_mod, "remove_bg", lambda b: None)
    ensure_shop(conn, SHOP)
    _, reply, _ = handle_photo(conn, SHOP, _photo_bytes(), "logo")
    assert "white badge" in reply


def test_switch_between_original_and_cutout(conn, monkeypatch):
    _upload(conn, monkeypatch, _cutout_bytes())
    brand = reports.shop_brand(conn, SHOP)
    assert brand["logo"].endswith("-cutout.png")
    msg, _ = handle_text(conn, SHOP, "use original logo")
    assert "original" in msg
    assert reports.shop_brand(conn, SHOP)["logo"].endswith("-orig.png")
    msg, _ = handle_text(conn, SHOP, "remove logo background")
    assert "background-free" in msg
    assert reports.shop_brand(conn, SHOP)["logo"].endswith("-cutout.png")


def test_yes_confirms_fresh_logo(conn, monkeypatch):
    _upload(conn, monkeypatch, _cutout_bytes())
    msg, _ = handle_text(conn, SHOP, "yes")
    assert "will head your documents" in msg
    msg, _ = handle_text(conn, SHOP, "yes")
    assert "nothing waiting" in msg


def test_logo_on_every_document(conn, monkeypatch, tmp_path):
    _upload(conn, monkeypatch, _cutout_bytes())
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
