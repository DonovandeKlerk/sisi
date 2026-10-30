"""Live Turso smoke: two concurrent Modal workers share one database.

Run AFTER the Turso secrets are in sisi-env and the migration is imported,
BEFORE switching traffic (it only touches a canary row it deletes):

    .venv/bin/modal run scripts/turso_smoke.py

Proves: cross-container visibility, concurrent writes without loss, and
the sqlite3-compatible dialect our code relies on. Secrets stay server-side;
only row counts are printed.
"""

import modal

image = modal.Image.debian_slim().pip_install("turso_serverless==0.1.0")
app = modal.App("sisi-turso-smoke")


@app.function(image=image, secrets=[modal.Secret.from_name("sisi-env")])
def write_canary(worker: int, n: int) -> int:
    import os

    import turso_serverless

    conn = turso_serverless.connect(
        os.environ["TURSO_DATABASE_URL"],
        auth_token=os.environ["TURSO_AUTH_TOKEN"],
    )
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT OR IGNORE INTO shops (shop_id, name)"
            " VALUES ('smoke-shop', 'smoke')"
        )
        conn.commit()
        for i in range(n):
            conn.execute(
                "INSERT INTO flags (shop_id, key, value) VALUES (?, ?, 'smoke')",
                ("smoke-shop", f"smoke-{worker}-{i}"),
            )
            conn.commit()
        return n
    finally:
        conn.close()


@app.function(image=image, secrets=[modal.Secret.from_name("sisi-env")])
def read_canaries() -> dict:
    import os

    import turso_serverless

    conn = turso_serverless.connect(
        os.environ["TURSO_DATABASE_URL"],
        auth_token=os.environ["TURSO_AUTH_TOKEN"],
    )
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM flags WHERE shop_id = 'smoke-shop'"
        ).fetchone()
        conn.execute("DELETE FROM flags WHERE shop_id = 'smoke-shop'")
        conn.execute("DELETE FROM shops WHERE shop_id = 'smoke-shop'")
        conn.commit()
        return {"canaries": rows[0]}
    finally:
        conn.close()


@app.function(image=image, secrets=[modal.Secret.from_name("sisi-env")])
def cleanup() -> dict:
    """Delete leftover canaries (e.g. from an interrupted run). Runs first,
    sequentially, so concurrent writers never wipe each other."""
    import os

    import turso_serverless

    conn = turso_serverless.connect(
        os.environ["TURSO_DATABASE_URL"],
        auth_token=os.environ["TURSO_AUTH_TOKEN"],
    )
    try:
        conn.execute("DELETE FROM flags WHERE shop_id = 'smoke-shop'")
        conn.execute("DELETE FROM shops WHERE shop_id = 'smoke-shop'")
        conn.commit()
        return {"cleaned": True}
    finally:
        conn.close()


@app.local_entrypoint()
def main():
    print("cleanup:", cleanup.remote())
    a = write_canary.spawn(1, 25)
    b = write_canary.spawn(2, 25)
    print("wrote:", a.get(), b.get())
    print("read back:", read_canaries.remote())
