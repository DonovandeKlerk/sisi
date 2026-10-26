"""Environment config. Import once; never print secrets."""

from __future__ import annotations

import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - optional in tests
    pass


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def model_name() -> str:
    """Live read so SISI_MODEL can switch standard/contributor per run."""
    return os.environ.get("SISI_MODEL") or "muse-spark-1.3"


MODEL_API_KEY = env("MODEL_API_KEY")
SISI_MODEL = env("SISI_MODEL", "muse-spark-1.3")
SISI_LANGUAGE_BIAS = env("SISI_LANGUAGE_BIAS", "English")
WHATSAPP_TOKEN = env("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = env("WHATSAPP_PHONE_NUMBER_ID")
GRAPH_API_VERSION = env("GRAPH_API_VERSION", "v26.0")
VERIFY_TOKEN = env("VERIFY_TOKEN")
APP_SECRET = env("APP_SECRET")
LOG_LEVEL = env("LOG_LEVEL", "INFO")

META_BASE_URL = "https://api.meta.ai/v1"
GRAPH_BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
