"""FastAPI app: GET/POST /webhook, GET /health. (Full wiring lands in Step 4.)"""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="Sisi")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/webhook")
def webhook_verify() -> dict[str, str]:  # Step 4
    return {"status": "not wired yet"}


@app.post("/webhook")
def webhook_receive() -> dict[str, str]:  # Step 4
    return {"status": "not wired yet"}
