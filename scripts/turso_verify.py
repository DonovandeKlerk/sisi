"""Compare two Sisi databases table-by-table (row counts + content hashes).

Used to verify a Turso migration: run against the SQLite file and the
Turso database (via TURSO_DATABASE_URL/AUTH_TOKEN) and diff the reports.

Usage:
    PYTHONPATH=. python scripts/turso_verify.py --db data/sisi.db
    TURSO_DATABASE_URL=... TURSO_AUTH_TOKEN=... PYTHONPATH=. python scripts/turso_verify.py --turso
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def snapshot(conn):
    tables = sorted({t for (t,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%'").fetchall()})
    out = {}
    for table in tables:
        cols = [c[1] for c in conn.execute(
            f"PRAGMA table_info({table})").fetchall()]
        order = ", ".join(cols) if cols else "rowid"
        h = hashlib.sha256()
        n = 0
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}"):
            h.update(repr(tuple(row)).encode())
            n += 1
        out[table] = {"rows": n, "sha": h.hexdigest()[:16]}
    return out


def compat_probe(conn):
    """Exercise the SQL features Sisi relies on, then roll back."""
    conn.execute("SAVEPOINT sisi_probe")
    try:
        conn.execute("CREATE TEMP TABLE probe_t (a TEXT COLLATE NOCASE, b INTEGER)")
        conn.execute("INSERT INTO probe_t VALUES ('X', 1)")
        conn.execute("SELECT strftime('%Y-%m-%d', 'now')")
        conn.execute("SELECT * FROM probe_t WHERE a = 'x'")
        conn.execute("PRAGMA foreign_keys")
        return "ok"
    finally:
        conn.execute("ROLLBACK TO sisi_probe")
        conn.execute("RELEASE sisi_probe")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--turso", action="store_true")
    args = ap.parse_args()
    if args.turso:
        from app.db import connect
        conn = connect()
    else:
        path = args.db or "data/sisi.db"
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        print(json.dumps({"probe": compat_probe(conn),
                          "tables": snapshot(conn)}, indent=1))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
