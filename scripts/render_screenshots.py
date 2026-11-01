"""Render README screenshots from live demo data (Modal run, never deployed).

Usage (from repo root):
    .venv/bin/modal run scripts/render_screenshots.py --shop <live-shop>
    .venv/bin/modal volume get sisi-data screenshots screenshots

Renders real statement/month/snapshot/shop PNG cards for the test shop
into /data/screenshots. Prints the brand contact shown in headers so PII
can be checked before publishing.
"""

import modal

image = (
    modal.Image.debian_slim()
    .apt_install("ffmpeg", "fonts-dejavu-core")
    .pip_install_from_requirements("requirements.txt")
    .add_local_dir("app", remote_path="/root/app",
                   ignore=["__pycache__", "*.pyc", "*.pyo"])
)

app = modal.App(
    "sisi-shots",
    image=image,
    secrets=[modal.secret.Secret.from_name("sisi-env")],
    volumes={"/data": modal.volume.Volume.from_name("sisi-data")},
)

DEFAULT_SHOP = "27710000002"
TEST_NAME = "Bongi’s Spaza Shop"  # curly apostrophe, as stored


@app.function(timeout=300)
def render(shop: str = "") -> str:
    shop_id = (shop or DEFAULT_SHOP).strip()  # live id via --shop, never git
    import os
    from pathlib import Path

    os.environ["SISI_DB"] = "/data/sisi.db"
    os.environ["SISI_REPORTS"] = "/data/reports"
    os.environ["SISI_LOGOS"] = "/data/logos"
    from app import reports as R
    from app.db import connect
    from app.i18n import doc_lang as _doc_lang
    from app.schedule import _snapshot_data

    conn = connect()
    try:
        shop = conn.execute(
            "SELECT name FROM shops WHERE shop_id = ?", (shop_id,)).fetchone()
        if shop is None or shop["name"] != TEST_NAME:
            return "REFUSED: test shop missing or renamed"
        brand = R.shop_brand(conn, shop_id)
        out = [f"brand: {brand['name']} addr={brand.get('address') or '-'}"
               f" phone={brand.get('phone') or '-'}"
               f" logo={'yes' if brand.get('logo') else 'no'}"]
        # Screenshots ship in a public repo: blank the real-world contact.
        # Prod data is untouched; only these renders are redacted.
        brand = dict(brand, address="", phone="")
        out.append("contact redacted in renders")
        shots = Path("/data/screenshots")
        shots.mkdir(parents=True, exist_ok=True)

        with _doc_lang("en"):
            data = R.statement_data(conn, shop_id, "Sipho")
            pages = R.render_statement_pngs(
                data, brand, str(shots / "shot-statement-sipho-en"))
            out.append(f"statement-sipho-en: {len(pages)} page(s)")
        with _doc_lang("zu"):
            data = R.statement_data(conn, shop_id, "Thandi")
            pages = R.render_statement_pngs(
                data, brand, str(shots / "shot-statement-thandi-zu"))
            out.append(f"statement-thandi-zu: {len(pages)} page(s)")
        with _doc_lang("en"):
            d = R.month_data(conn, shop_id, "2026-10")
            R.render_card(d, str(shots / "shot-month-2026-10.png"), brand)
            out.append("month-2026-10.png")
            R.render_shop_card(brand, str(shots / "shot-shop-card.png"))
            out.append("shop-card.png")
            data = _snapshot_data(conn, shop_id, "2026-11-01")
            R.render_snapshot_png(data, brand,
                                  str(shots / "shot-snapshot-week.png"),
                                  "2026-11-01")
            out.append("snapshot-week.png")
        return "\n".join(out)
    finally:
        conn.close()


@app.local_entrypoint()
def main(shop: str = ""):
    print(render.remote(shop))
