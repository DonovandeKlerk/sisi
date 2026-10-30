"""App-level Turso rehearsal, server-side (cutover verification).

Proves REAL app code works on Turso before traffic flips (or right
after): driver Row name/index access, write+commit+readback, and a full
handle_text("menu") round-trip on a canary shop. Every canary row is
deleted afterwards; re-run the remote snapshot to prove zero residue.

    .venv/bin/modal run scripts/turso_app_rehearsal.py

Only shapes and counts are printed, never shop contents.
"""

import modal

image = (
    modal.Image.debian_slim()
    .apt_install("ffmpeg", "fonts-dejavu-core")
    .pip_install_from_requirements("requirements.txt")
    .add_local_dir("app", remote_path="/root/app",
                   ignore=["__pycache__", "*.pyc", "*.pyo"])
)
app = modal.App("sisi-turso-rehearsal")


@app.function(image=image, secrets=[modal.Secret.from_name("sisi-env")],
              timeout=300)
def rehearse() -> dict:
    import os

    os.environ.setdefault("SISI_DB", "/tmp/rehearsal-ignored.db")
    from app.agent import handle_text
    from app.db import connect, init_db, using_turso

    out: dict = {"using_turso": using_turso()}
    conn = connect()
    try:
        init_db(conn)
        row = conn.execute(
            "SELECT shop_id, name FROM shops LIMIT 1").fetchone()
        out["row_by_name_ok"] = isinstance(row["name"], str)
        out["row_by_index_ok"] = row[0] == row["shop_id"]
        out["row_keys"] = sorted(row.keys())
        conn.execute("INSERT OR IGNORE INTO shops (shop_id, name)"
                     " VALUES ('smoke-shop', 'smoke')")
        conn.execute("INSERT INTO flags (shop_id, key, value)"
                     " VALUES ('smoke-shop', 'rehearsal', '1')")
        conn.commit()
        back = conn.execute("SELECT value FROM flags WHERE shop_id = ?"
                            " AND key = 'rehearsal'",
                            ("smoke-shop",)).fetchone()
        out["write_readback_ok"] = back is not None and back["value"] == "1"
        conn.execute("DELETE FROM flags WHERE shop_id = 'smoke-shop'")
        conn.commit()
        msg, buttons = handle_text(conn, "smoke-shop", "menu")
        # Fresh shop → setup flow (proves the full read+write path ran).
        out["setup_routed_ok"] = "Step 1" in msg
        out["menu_buttons"] = bool(buttons)
        # Strict cleanup of every canary row this test created.
        for table in ("conversation_turns", "flags", "reminders",
                      "pending_actions", "task_plans", "clarify_context",
                      "await_price", "await_reminder", "intake_sessions",
                      "photo_hashes", "order_drafts", "shops"):
            conn.execute(f"DELETE FROM {table} WHERE shop_id = ?",
                         ("smoke-shop",))
        conn.commit()
        left = conn.execute("SELECT COUNT(*) FROM shops WHERE shop_id = ?",
                            ("smoke-shop",)).fetchone()[0]
        out["cleanup_ok"] = (left == 0)
        return out
    finally:
        conn.close()


@app.local_entrypoint()
def main():
    print(rehearse.remote())
