"""Time each slow capture step to find the stall (Modal run, test shop only)."""

import modal
import time

image = (
    modal.Image.debian_slim()
    .pip_install_from_requirements("requirements.txt")
    .add_local_dir("app", remote_path="/root/app",
                   ignore=["__pycache__", "*.pyc", "*.pyo"])
    .add_local_file("tests/fixtures/book_page.png",
                    remote_path="/root/book_page.png")
    .add_local_file("samples/voice_sale_cash.ogg",
                    remote_path="/root/voice_sale_cash.ogg")
)

app = modal.App(
    "sisi-probe",
    image=image,
    secrets=[modal.secret.Secret.from_name("sisi-env")],
    volumes={"/data": modal.volume.Volume.from_name("sisi-data")},
)


@app.function(timeout=300)
def probe(shop: str = "") -> str:
    import os
    from pathlib import Path

    shop_id = (shop or "").strip()
    os.environ["SISI_DB"] = "/data/sisi.db"
    from app.agent import handle_photo, handle_text, handle_voice
    from app.db import connect
    from app.tools import get_pending

    out = []
    conn = connect()
    try:
        t0 = time.time()
        msg, _ = handle_text(conn, shop_id, "sold 2 bread and send Thandi her statement")
        out.append(f"multi: {time.time() - t0:.1f}s pend={get_pending(conn, shop_id) is not None}")
        if get_pending(conn, shop_id) is not None:
            handle_text(conn, shop_id, "No")
        t0 = time.time()
        heard, reply, _ = handle_voice(
            conn, shop_id, Path("/root/voice_sale_cash.ogg").read_bytes())
        out.append(f"voice: {time.time() - t0:.1f}s heard={(heard or '')[:40]!r}")
        if get_pending(conn, shop_id) is not None:
            handle_text(conn, shop_id, "No")
        t0 = time.time()
        _d, reply, _ = handle_photo(
            conn, shop_id, Path("/root/book_page.png").read_bytes(), "")
        out.append(f"photo: {time.time() - t0:.1f}s pend={get_pending(conn, shop_id) is not None}")
        return "\n".join(out)
    finally:
        conn.close()


@app.local_entrypoint()
def main(shop: str = ""):
    print(probe.remote(shop))
