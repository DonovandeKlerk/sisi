"""Logo on every document: Dict-backed helper, fresh-instance renders."""

import io
import sys
import types

import pytest

from app import logo as logo_mod
from app import reports
from app.agent import handle_photo
from app.tools import ensure_shop

SHOP = "27710000046"


def _photo_bytes():
    from PIL import Image
    img = Image.new("RGB", (300, 200), "white")
    px = img.load()
    for x in range(100, 200):
        for y in range(60, 140):
            px[x, y] = (20, 80, 40)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _fake_dict_modal(monkeypatch, store):
    mod = types.ModuleType("modal")

    class Dict:
        @classmethod
        def from_name(cls, name, create_if_missing=False):
            box = types.SimpleNamespace()
            box.put = lambda k, v: store.__setitem__(k, v)
            box.get = lambda k, d=None: store.get(k, d)
            return box

    mod.Dict = Dict
    monkeypatch.setitem(sys.modules, "modal", mod)
    monkeypatch.setenv("SISI_MEDIA_DICT", "sisi-media")


@pytest.fixture(autouse=True)
def _no_setup(conn):
    from app.setup import skip_setup
    skip_setup(conn, SHOP)


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(reports, "LOGOS_DIR", tmp_path / "logos")
    monkeypatch.delenv("SISI_PUBLIC_URL", raising=False)


def _white_box_pixels(path):
    from PIL import Image
    img = Image.open(path).convert("RGB")
    px = img.load()
    n = 0
    for x in range(924, 1020):
        for y in range(56, 152):
            r, g, b = px[x, y]
            if r > 200 and g > 200 and b > 200:
                n += 1
    return n


def test_save_publishes_logo_dict(conn, monkeypatch):
    store: dict = {}
    _fake_dict_modal(monkeypatch, store)
    ensure_shop(conn, SHOP, "Doc Spaza")
    handle_photo(conn, SHOP, _photo_bytes(), "logo")
    assert store[f"logo:{SHOP}"]["tile"][:8] == b"\x89PNG\r\n\x1a\n"


def test_brand_materializes_tile_from_dict(conn, monkeypatch):
    from pathlib import Path
    store: dict = {}
    _fake_dict_modal(monkeypatch, store)
    ensure_shop(conn, SHOP, "Doc Spaza")
    handle_photo(conn, SHOP, _photo_bytes(), "logo")
    tile = Path(reports.shop_brand(conn, SHOP)["logo"])
    orig = Path(reports.shop_brand(conn, SHOP)["logo_orig"])
    want = tile.read_bytes()
    tile.unlink()
    orig.unlink()  # this container's snapshot lost the files
    brand = reports.shop_brand(conn, SHOP)
    assert Path(brand["logo"]).read_bytes() == want
    assert brand["logo"].endswith("-tile.png")


def test_brand_without_logo_is_name_only(conn):
    ensure_shop(conn, SHOP, "Doc Spaza")
    assert reports.shop_brand(conn, SHOP)["logo"] == ""


def _doc_data():
    return {
        "stmt": {"name": "Sipho", "date": "2026-10-28",
                 "items": [{"date": "2026-10-22", "item": "Bread",
                            "qty": "2", "cents": 4000}],
                 "payments": [], "total": 4000},
        "month": {"shop": "Doc Spaza", "label": "October 2026", "sales": 10000,
                  "gross": 4000, "exp_total": 1000, "net": 3000, "cash": 5000,
                  "petty": 500, "bank": 0, "debt_total": 2000,
                  "insights": ["hi"]},
        "snap": {"sales": 100, "gross": 40, "best": "Bread", "best_cents": 50,
                 "slowest": "Tea", "slow_cents": 5, "cash": 500, "owed": 4000,
                 "tip": "hi"},
        "order": {"lines": [{"item": "Bread", "qty": "2", "cents": 4000}]},
    }


def test_every_renderer_shows_dict_only_logo(conn, tmp_path, monkeypatch):
    from pathlib import Path
    store: dict = {}
    _fake_dict_modal(monkeypatch, store)
    ensure_shop(conn, SHOP, "Doc Spaza")
    handle_photo(conn, SHOP, _photo_bytes(), "logo")
    brand = reports.shop_brand(conn, SHOP)
    Path(brand["logo"]).unlink()
    Path(brand["logo_orig"]).unlink()
    brand = reports.shop_brand(conn, SHOP)  # materialized from Dict
    assert brand["logo"].endswith("-tile.png")
    d = _doc_data()
    out = tmp_path / "docs"
    out.mkdir()
    month = reports.month_data(conn, SHOP, "2026-10")
    pngs = []
    pngs += reports.render_statement_pngs(d["stmt"], brand, str(out / "st"))
    pngs.append(reports.render_card(month, str(out / "m.png"), brand))
    pngs.append(reports.render_snapshot_png(d["snap"], brand,
                                            str(out / "snap.png"),
                                            "2026-11-01"))
    pngs.append(reports.render_order_png({"lines": []}, brand,
                                        str(out / "o.png"), "2026-10-28"))
    pngs.append(reports.render_shop_card(brand, str(out / "p.png")))
    for path in pngs:
        assert _white_box_pixels(path) > 500, path
    pdfs = [reports.render_statement_pdf(d["stmt"], brand, str(out / "s.pdf"))]
    pdfs.append(reports.render_pdf(month, str(out / "m.pdf"), brand))
    for path in pdfs:
        assert open(path, "rb").read().count(b"/Image") >= 1, path


def test_fresh_process_renders_logo_on_every_doc(tmp_path):
    """A fresh interpreter renders logos from shared paths only."""
    import os
    import subprocess

    from app.db import connect as file_connect
    from app.db import init_db

    logos = tmp_path / "logos"
    logos.mkdir()
    tile = logo_mod.render_tile(_photo_bytes())
    (logos / f"{SHOP}-tile.png").write_bytes(tile)
    (logos / f"{SHOP}-orig.png").write_bytes(_photo_bytes())
    db_path = tmp_path / "docs.db"
    fc = file_connect(db_path)
    init_db(fc)
    ensure_shop(fc, SHOP, "Doc Spaza")
    fc.execute("UPDATE shops SET logo_path = ?, logo_orig_path = ?"
               " WHERE shop_id = ?",
               (str(logos / f"{SHOP}-tile.png"),
                str(logos / f"{SHOP}-orig.png"), SHOP))
    sipho = fc.execute("INSERT INTO customers (shop_id, name)"
                       " VALUES (?, 'Sipho')", (SHOP,)).lastrowid
    fc.execute("INSERT INTO credit_entries (shop_id, customer_id,"
               " amount_cents, items_text, action_id, created_at)"
               " VALUES (?, ?, 4000, 'Bread', 'doc-ce-0',"
               " '2026-10-05T10:00:00.000Z')", (SHOP, sipho))
    fc.commit()
    fc.close()
    out = tmp_path / "fresh"
    out.mkdir()
    env = dict(os.environ)
    env["SISI_DB"] = str(db_path)
    env["SISI_REPORTS"] = str(tmp_path / "reports")
    env["SISI_LOGOS"] = str(logos)
    env.pop("SISI_MEDIA_DICT", None)
    env.pop("SISI_MEDIA_VOLUME", None)
    script = (
        "from app.db import connect, init_db\n"
        "from app import reports\n"
        f"conn = connect(); init_db(conn)\n"
        f"brand = reports.shop_brand(conn, '{SHOP}')\n"
        f"assert brand['logo'].endswith('-tile.png'), brand\n"
        "stmt = {'name': 'Sipho', 'date': '2026-10-28', 'items': "
        "[{'date': '2026-10-22', 'item': 'Bread', 'qty': '2', 'cents': 4000}],"
        " 'payments': [], 'total': 4000}\n"
        f"print(reports.render_statement_pngs(stmt, brand, '{out}/st'))\n"
        f"print(reports.render_statement_pdf(stmt, brand, '{out}/s.pdf'))\n"
        f"month = reports.month_data(conn, '{SHOP}', '2026-10')\n"
        f"print(reports.render_card(month, '{out}/m.png', brand))\n"
        f"print(reports.render_pdf(month, '{out}/m.pdf', brand))\n"
        "snap = {'sales': 100, 'gross': 40, 'best': 'Bread', 'best_cents': 50,"
        " 'slowest': 'Tea', 'slow_cents': 5, 'cash': 500, 'owed': 4000,"
        " 'tip': 'hi'}\n"
        f"print(reports.render_snapshot_png(snap, brand, '{out}/snap.png', '2026-11-01'))\n"
        f"print(reports.render_order_png({{'lines': []}}, brand, '{out}/o.png', '2026-10-28'))\n"
        f"print(reports.render_shop_card(brand, '{out}/p.png'))\n"
    )
    from pathlib import Path as _Path
    root = _Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=root, env=env,
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    for name in ("st-p1.png", "m.png", "snap.png", "o.png", "p.png"):
        path = out / name
        assert path.exists(), name
        assert _white_box_pixels(str(path)) > 500, name
    for name in ("s.pdf", "m.pdf"):
        assert open(out / name, "rb").read().count(b"/Image") >= 1, name
