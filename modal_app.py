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
    os.environ["SISI_DB"] = "/data/sisi.db"
    os.environ.setdefault("SISI_REPORTS", "/data/reports")
    return fastapi_app


@app.function(schedule=modal.Period(minutes=15), timeout=300)
def nudges():
    """Quarter-hour tick: 19:00 SAST book nudge, Sunday snapshot, expiry warns."""
    import logging

    os.environ["SISI_DB"] = "/data/sisi.db"
    os.environ.setdefault("SISI_REPORTS", "/data/reports")
    from app.schedule import run_due
    for job in run_due():
        logging.getLogger("sisi").info("scheduled %s -> %s", job["key"], job["shop"])
