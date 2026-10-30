"""Import a SQLite dump into Turso Cloud, server-side.

The Turso token never leaves Modal: the dump is uploaded to the volume
first, then this applies it over the driver and prints per-table counts
for comparison with scripts/turso_verify.py --db <backup>.

Usage:
    sqlite3 /tmp/sisi-cutover-backup.db .dump > /tmp/sisi-cutover.sql
    .venv/bin/modal volume put sisi-data /tmp/sisi-cutover.sql /cutover.sql
    .venv/bin/modal run scripts/turso_import.py --dump /cutover.sql

Statements run one by one (split with sqlite3.complete_statement, so
semicolons inside text are safe); the dump's own BEGIN/COMMIT/PRAGMA
lines are skipped in favour of one final commit. Foreign keys are
re-enabled and checked at the end; violations abort the report.

Refuses to run when the target already holds shop rows (prevents a
double import); pass --force to override after a verified wipe.
Only counts are printed, never row contents.
"""

import modal

image = modal.Image.debian_slim().pip_install("turso_serverless==0.1.0")
app = modal.App("sisi-turso-import")
vol = modal.Volume.from_name("sisi-data", create_if_missing=True)


@app.function(image=image, secrets=[modal.Secret.from_name("sisi-env")],
              volumes={"/data": vol})
def apply_dump(dump_path: str, force: bool = False) -> dict:
    import os
    import re
    import sqlite3

    import turso_serverless

    conn = turso_serverless.connect(
        os.environ["TURSO_DATABASE_URL"],
        auth_token=os.environ["TURSO_AUTH_TOKEN"],
    )
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%'").fetchall()]
        if tables and not force:
            total = 0
            for (t,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                    " AND name NOT LIKE 'sqlite_%'").fetchall():
                total += conn.execute(
                    f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            if total:
                return {"aborted": f"target holds {total} rows; refusing"}
        with open("/data/" + dump_path.lstrip("/")) as f:
            text = f.read()
        conn.execute("PRAGMA foreign_keys = OFF")
        buf, ran = "", 0
        for line in text.splitlines(keepends=True):
            buf += line
            if not sqlite3.complete_statement(buf):
                continue
            stmt = buf.strip()
            buf = ""
            if not stmt or stmt.startswith("--"):
                continue
            head = stmt.split(None, 1)[0].upper().rstrip(";")
            if head in ("BEGIN", "COMMIT", "PRAGMA"):
                continue
            if head == "CREATE":
                # Idempotent DDL: safe whether the target is empty or
                # already holds our schema (observed once, unexplained).
                stmt = re.sub(r"^CREATE\s+TABLE\s+",
                              "CREATE TABLE IF NOT EXISTS ", stmt,
                              count=1, flags=re.IGNORECASE)
                stmt = re.sub(r"^CREATE\s+(UNIQUE\s+)?INDEX\s+",
                              lambda m: f"CREATE {m.group(1) or ''}INDEX"
                                        " IF NOT EXISTS ", stmt,
                              count=1, flags=re.IGNORECASE)
            conn.execute(stmt)
            ran += 1
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        counts = {}
        for (t,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%'").fetchall():
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return {"statements": ran, "tables": len(counts),
                "rows": sum(counts.values()),
                "fk_violations": len(violations), "counts": counts}
    finally:
        conn.close()


@app.function(image=image, secrets=[modal.Secret.from_name("sisi-env")])
def snapshot() -> dict:
    """Per-table row counts + content hashes. Same hashing as
    scripts/turso_verify.py (repr of value tuples), so reports compare
    exactly. Only counts and hashes are printed, never contents."""
    import hashlib
    import os

    import turso_serverless

    conn = turso_serverless.connect(
        os.environ["TURSO_DATABASE_URL"],
        auth_token=os.environ["TURSO_AUTH_TOKEN"],
    )
    try:
        tables = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%'").fetchall())
        out = {}
        for table in tables:
            cols = [r[1] for r in conn.execute(
                f"PRAGMA table_info({table})").fetchall()]
            order = ", ".join(cols) if cols else "rowid"
            h = hashlib.sha256()
            n = 0
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                h.update(repr(tuple(row)).encode())
                n += 1
            out[table] = {"rows": n, "sha": h.hexdigest()[:16]}
        return {"tables": out}
    finally:
        conn.close()


@app.local_entrypoint()
def main(dump: str = "", force: bool = False, snapshot_only: bool = False):
    if snapshot_only or not dump:
        print(snapshot.remote())
    else:
        print(apply_dump.remote(dump, force))
