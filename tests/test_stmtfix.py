"""Statement fix: header/logo-box layout, multi-creditor statements."""

import io

import pytest

from app import reports
from app.agent import handle_text, take_media
from app.tools import ensure_shop

SHOP = "27710000044"


@pytest.fixture(autouse=True)
def _no_setup(conn):
    from app.setup import skip_setup
    skip_setup(conn, SHOP)


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(reports, "LOGOS_DIR", tmp_path / "logos")
    monkeypatch.setenv("SISI_PUBLIC_URL", "https://x.example")


@pytest.fixture()
def debtors(conn):
    ensure_shop(conn, SHOP, "Stmt Spaza")
    plan = [("Bongi", 2000, 3, "2026-10-20"),   # oldest debt first
            ("Sipho", 4000, 2, "2026-10-22"),
            ("Jabu", 6000, 1, "2026-10-24"),
            ("Thandi", 9000, 12, "2026-10-25"),
            ("Zodwa", 0, 0, "2026-10-25")]
    for name, total, n, day in plan:
        cid = conn.execute("INSERT INTO customers (shop_id, name)"
                           " VALUES (?, ?)", (SHOP, name)).lastrowid
        if not n:
            continue
        each, rem = divmod(total, n)
        for i in range(n):
            amt = each + (rem if i == 0 else 0)
            conn.execute("INSERT INTO credit_entries (shop_id, customer_id,"
                         " amount_cents, items_text, action_id, created_at)"
                         " VALUES (?, ?, ?, 'Bread', ?, ?)",
                         (SHOP, cid, amt, f"stmt-{name}-{i}",
                          f"{day}T10:00:00.000Z"))
    conn.commit()


def _long_brand(tmp_path):
    logo = tmp_path / "logos" / "long-logo.png"
    logo.parent.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    Image.new("RGBA", (200, 100), (30, 140, 60, 255)).save(logo)
    return {"name": "Lylula Premium Bites and Catering Supplies (Pty) Ltd",
            "address": ("Shop 12, Main Road, Amanzimtoti, KwaZulu-Natal,"
                        " 4126, South Africa"),
            "phone": "0821234567",
            "logo": str(logo), "logo_orig": str(logo), "language": "en"}


def test_header_text_never_enters_logo_box(tmp_path):
    from PIL import Image, ImageDraw
    brand = _long_brand(tmp_path)
    img = Image.new("RGB", (1080, 1520), "white")
    dr = ImageDraw.Draw(img)
    lines, box_left = reports._header_text_lines(
        brand, "Statement · Sipho", "28 Oct 2026", 1080, dr)
    assert len(lines) > 3  # long name/address wrapped, not clipped away
    logo_box = (box_left, 60, box_left + reports.LOGO_BOX,
                60 + reports.LOGO_BOX)
    for line in lines:
        bbox = dr.textbbox((line["x"], line["y"]), line["text"],
                           font=line["font"])
        assert bbox[2] <= box_left - reports.LOGO_PAD, line["text"]
        assert (bbox[2] <= logo_box[0] or bbox[0] >= logo_box[2]
                or bbox[3] <= logo_box[1] or bbox[1] >= logo_box[3])
    y = reports._png_brand_header(img, brand, "Statement · Sipho",
                                  "28 Oct 2026")
    assert y >= 360


def test_pdf_header_text_stays_in_text_column(tmp_path):
    from reportlab.platypus import Paragraph
    brand = _long_brand(tmp_path)
    width = 400.0
    text_w, logo_w = reports._pdf_header_cols(width, True)
    assert text_w + reports.PDF_LOGO_GAP + logo_w == width
    assert reports._pdf_header_cols(width, False) == [width]
    st = reports._pdf_styles()
    contact = " · ".join(p for p in (brand["address"], brand["phone"]) if p)
    p = Paragraph(f"<b>{brand['name']}</b><br/>{contact}"
                  f"<br/>Statement · Sipho", st["body"])
    used_w, _ = p.wrap(text_w, 1000)
    logo_left = text_w + reports.PDF_LOGO_GAP
    assert used_w <= logo_left  # text bbox cannot reach the logo column
    data = {"name": "Sipho", "date": "2026-10-28",
            "items": [{"date": "2026-10-22", "item": "Bread",
                       "qty": "2", "cents": 4000}],
            "payments": [], "total": 4000}
    pdf = reports.render_statement_pdf(data, brand, str(tmp_path / "l.pdf"))
    assert open(pdf, "rb").read().count(b"/Image") >= 1


def test_statement_card_long_address_renders(tmp_path):
    brand = _long_brand(tmp_path)
    data = {"name": "Sipho", "date": "2026-10-28",
            "items": [{"date": "2026-10-22", "item": "Bread",
                       "qty": "2", "cents": 4000}],
            "payments": [], "total": 4000}
    pages = reports.render_statement_pngs(data, brand, str(tmp_path / "st"))
    assert len(pages) == 1
    pdf = reports.render_statement_pdf(data, brand, str(tmp_path / "st.pdf"))
    assert open(pdf, "rb").read().count(b"/Image") >= 1


NAMES = ["Sipho", "Thandi", "Bongi", "Jabu", "Zodwa"]


def _caption_name(caption):
    return next(n for n in NAMES if n in caption)


def _media_parts(shop_id):
    images, files = [], []
    for m in take_media(shop_id):
        (images if m["kind"] == "image" else files).append(m)
    return images, files


TOP_PHRASES = ["statements for 4 creditors",
               "izitatimende zabantu abayi-4 abakweletayo",
               "state vir 4 debiteure",
               "iistatimenti zabantu abayi-4 abatyali"]


@pytest.mark.parametrize("phrase", TOP_PHRASES)
def test_statements_top_n_all_languages(conn, debtors, phrase):
    msg, _ = handle_text(conn, SHOP, phrase)
    assert "Zodwa" not in msg  # zero balance excluded
    images, files = _media_parts(SHOP)
    assert [_caption_name(m["caption"]) for m in images] == [
        "Thandi", "Thandi", "Jabu", "Sipho", "Bongi"]
    assert "(1/2)" in images[0]["caption"]
    assert "(2/2)" in images[1]["caption"]
    assert len(files) == 4  # one PDF per debtor
    assert all(m["url"].startswith("https://x.example/media/")
               for m in images + files)
    assert msg.index("Thandi") < msg.index("Jabu") < msg.index("Sipho")


ALL_PHRASES = ["send all debtors' statements",
               "thumela izitatimende zabo bonke abakweletayo",
               "stuur alle debiteure se state",
               "thumela iistatimenti zabo bonke abatyali"]


@pytest.mark.parametrize("phrase", ALL_PHRASES)
def test_statements_all_all_languages(conn, debtors, phrase):
    msg, _ = handle_text(conn, SHOP, phrase)
    assert "Zodwa" not in msg
    images, files = _media_parts(SHOP)
    assert [_caption_name(m["caption"]) for m in images] == [
        "Bongi", "Sipho", "Jabu", "Thandi", "Thandi"]  # oldest debt first
    assert len(files) == 4
    assert msg.index("Bongi") < msg.index("Sipho") < msg.index("Jabu")


NAMED_PHRASES = ["statements for Sipho, Thandi and Bongi",
                 "izitatimende zikaSipho, uThandi noBongi",
                 "state vir Sipho, Thandi en Bongi",
                 "iistatimenti zikaSipho, uThandi noBongi"]


@pytest.mark.parametrize("phrase", NAMED_PHRASES)
def test_statements_named_all_languages(conn, debtors, phrase):
    msg, _ = handle_text(conn, SHOP, phrase)
    assert "Jabu" not in msg and "Zodwa" not in msg
    images, files = _media_parts(SHOP)
    assert [_caption_name(m["caption"]) for m in images] == [
        "Sipho", "Thandi", "Thandi", "Bongi"]  # in named order
    assert len(files) == 3


def test_statements_top_n_fewer_than_n(conn, debtors):
    msg, _ = handle_text(conn, SHOP, "statements for 9 creditors")
    assert "Thandi" in msg and "Zodwa" not in msg
    images, _ = _media_parts(SHOP)
    assert len(images) == 5


def test_statements_named_unknown_ignored(conn, debtors):
    msg, _ = handle_text(conn, SHOP, "statements for Sipho, Thandi and Nobody")
    assert "Sipho" in msg and "Thandi" in msg
    images, _ = _media_parts(SHOP)
    assert [_caption_name(m["caption"]) for m in images] == [
        "Sipho", "Thandi", "Thandi"]


@pytest.mark.parametrize("phrase", ["all creditors",
                                    "statements for everyone who owes",
                                    "all statements",
                                    "statements for 2 creditors",
                                    "receipts for Sipho and Thandi"])
def test_every_statement_phrasing_delivers_media(conn, debtors, phrase):
    handle_text(conn, SHOP, phrase)
    images, files = _media_parts(SHOP)
    assert images, phrase  # never text-only
    assert files, phrase
    assert all("Forward this to" in m["caption"] or "👆" in m["caption"]
               for m in images), phrase


def test_month_end_report_delivers_media(conn):
    ensure_shop(conn, SHOP, "Stmt Spaza")
    handle_text(conn, SHOP, "month-end report")
    images, files = _media_parts(SHOP)
    assert len(images) == 1 and len(files) == 1
    assert all(m["url"].startswith("https://x.example/media/")
               for m in images + files)


def test_old_month_report_delivers_media(conn):
    ensure_shop(conn, SHOP, "Stmt Spaza")
    handle_text(conn, SHOP, "report september")
    images, files = _media_parts(SHOP)
    assert len(images) == 1 and len(files) == 1
