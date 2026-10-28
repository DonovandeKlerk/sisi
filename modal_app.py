"""Modal deployment for Sisi.

Always-on HTTPS webhook (one warm container), ffmpeg in the image, config from
the Modal secret named "sisi-env", SQLite persisted on the "sisi-data" volume
(container disks are ephemeral).

Prereqs (once each, on your machine):
    modal setup                                    # browser login
    modal secret create sisi-env --from-dotenv .env   # never prints values

Deploy:
    modal deploy modal_app.py                      # prints the public URL

Rollback: check out the last good commit, `modal deploy modal_app.py` again.
"""

import os

import modal

image = (
    modal.Image.debian_slim()
    .apt_install("ffmpeg", "fonts-dejavu-core")
    .pip_install_from_requirements("requirements.txt")
    # Bake the app package into the image (deploy uploads only modal_app.py).
    .add_local_dir("app", remote_path="/root/app",
                   ignore=["__pycache__", "*.pyc", "*.pyo"])
    .add_local_dir("seed", remote_path="/root/seed",
                   ignore=["__pycache__", "*.pyc", "*.pyo"])
)

app = modal.App(
    "sisi",
    image=image,
    secrets=[modal.secret.Secret.from_name("sisi-env")],
    volumes={"/data": modal.volume.Volume.from_name("sisi-data", create_if_missing=True)},
)

# Local imports (app/) are packaged automatically via include_source.
from app.main import app as fastapi_app  # noqa: E402


@app.function(min_containers=1, timeout=300)
@modal.asgi_app()
def web():
    # Force the volume path: a local SISI_DB value from the secret would
    # silently write to the container's ephemeral disk and lose shop data.
    _use_volume()
    return fastapi_app


@app.function(schedule=modal.Period(minutes=15), timeout=300)
def nudges():
    """Quarter-hour tick: 19:00 SAST book nudge, Sunday snapshot, expiry warns."""
    import logging
    _use_volume()

    from app.schedule import run_due
    for job in run_due():
        logging.getLogger("sisi").info("scheduled %s -> %s", job["key"], job["shop"])


def _use_volume() -> None:
    """Point the DB and all generated files at the persistent volume."""
    os.environ["SISI_DB"] = "/data/sisi.db"
    os.environ["SISI_REPORTS"] = "/data/reports"
    os.environ["SISI_LOGOS"] = "/data/logos"
    os.environ["SISI_MEDIA_VOLUME"] = "sisi-data"
    os.environ["SISI_MEDIA_DICT"] = "sisi-media"
    os.environ["SISI_SEND_LOCK"] = "sisi-sendlock"
    os.environ["SISI_SPAWN"] = "1"
    from app.reports import ensure_dirs
    ensure_dirs()


@app.function(timeout=300)
def worker(msg: dict):
    """Spawned per inbound message so webhooks return 200 instantly."""
    _use_volume()
    from app.main import process_message
    from app.whatsapp import IncomingMessage
    process_message(IncomingMessage(**msg))


@app.function(timeout=300)
def send_media(payload: dict) -> dict:
    """Background media send: 3s after the text, spaced, long 429 backoff."""
    _use_volume()
    from app.mediasend import send_media_deferred
    return send_media_deferred(**payload)


@app.function(schedule=modal.Period(minutes=1), timeout=120)
def every_minute():
    """Fire due reminders exactly once + drain leftover queued sends."""
    import logging
    _use_volume()
    from app.db import connect, init_db
    from app.reminders import send_due
    from app.sendqueue import drain_all
    from app.vonage import VonageSender
    from app.whatsapp import get_sender
    conn = connect()
    try:
        init_db(conn)
        for rid in send_due(conn):
            logging.getLogger("sisi").info("reminder %s fired", rid)
        drain_all(conn, lambda channel: VonageSender()
                  if channel == "vonage" else get_sender())
    finally:
        conn.close()
