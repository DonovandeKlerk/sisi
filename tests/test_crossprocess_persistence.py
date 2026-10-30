"""Cross-process persistence: separate interpreters sharing one database.

Honest framing: locally the shared server is a SQLite *file* (POSIX locks
serialize writers correctly), which stands in for Turso Cloud in
production. These tests prove OUR code holds no process-local state, honors
transactions, and survives handoff/restart/kill — the properties the Modal
volume's file-replacement broke. The volume failure itself has no local
analog; Turso provides the shared server (plus a live smoke at cutover).
"""

import multiprocessing as mp
import os
import sqlite3

SHOP = "27780000001"


def _open(db):
    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _w_confirm_item(db, out):
    from app.setup import skip_setup
    from app.tools import (answer_intake_session, confirm_pending,
                           get_intake_session, start_guided_intake)
    conn = _open(db)
    try:
        skip_setup(conn, SHOP)
        start_guided_intake(
            conn, SHOP,
            [{"brand": "Albany", "variant": "Superior White Bread",
              "size": "700g", "visible_count": 1, "readable": True,
              "notes": ""}],
            [], "add to stock", "xp-photo-1")
        answer_intake_session(conn, SHOP, "R16")
        answer_intake_session(conn, SHOP, "sell R22")
        reply = confirm_pending(conn, SHOP)
        out.put({"reply_has_saved": "Saved Albany" in reply})
    finally:
        conn.close()


def _w_read_item(db, out):
    conn = _open(db)
    try:
        row = conn.execute("SELECT stock_qty, cost_cents, price_cents"
                           " FROM products WHERE shop_id = ? AND name = ?",
                           (SHOP, "Albany Superior White Bread 700g")).fetchone()
        sess = conn.execute("SELECT status FROM intake_sessions"
                            " WHERE shop_id = ?", (SHOP,)).fetchone()
        pend = conn.execute("SELECT status FROM pending_actions"
                            " WHERE shop_id = ? ORDER BY id DESC LIMIT 1",
                            (SHOP,)).fetchone()
        out.put({"stock": tuple(row) if row else None,
                 "session": sess["status"] if sess else None,
                 "pending": pend["status"] if pend else None})
    finally:
        conn.close()


def _run(fn, db, timeout=60):
    ctx = mp.get_context("spawn")
    out = ctx.Queue()
    proc = ctx.Process(target=fn, args=(db, out))
    proc.start()
    proc.join(timeout)
    assert proc.exitcode == 0, f"{fn.__name__} exit={proc.exitcode}"
    return out.get(timeout=10)


def test_confirm_in_worker_visible_in_next_worker(tmp_path):
    db = str(tmp_path / "xp.db")
    conn = _open(db)
    from app.db import init_db
    init_db(conn)
    conn.close()
    assert _run(_w_confirm_item, db)["reply_has_saved"] is True
    seen = _run(_w_read_item, db)  # fresh interpreter, same file
    assert seen["stock"] == (1.0, 1600, 2200)
    assert seen["pending"] == "confirmed"
    assert seen["session"] in ("done", None)  # single-item session finished


def _w_flag_writer(db, out, n):
    conn = _open(db)
    try:
        for i in range(25):
            conn.execute("INSERT INTO flags (shop_id, key, value)"
                         " VALUES (?, ?, 'v')", (SHOP, f"xp-{n}-{i}"))
            conn.commit()
        out.put({"wrote": 25})
    finally:
        conn.close()


def test_concurrent_writers_lose_nothing(tmp_path):
    db = str(tmp_path / "conc.db")
    conn = _open(db)
    from app.db import init_db
    from app.setup import skip_setup
    init_db(conn)
    skip_setup(conn, SHOP)
    conn.commit()
    conn.close()
    ctx = mp.get_context("spawn")
    procs = []
    for n in range(4):
        out = ctx.Queue()
        proc = ctx.Process(target=_w_flag_writer, args=(db, out, n))
        proc.start()
        procs.append((proc, out))
    for proc, out in procs:
        proc.join(120)
        assert proc.exitcode == 0
        assert out.get(timeout=10) == {"wrote": 25}
    conn = _open(db)
    try:
        total = conn.execute("SELECT COUNT(*) c FROM flags"
                             " WHERE shop_id = ? AND key LIKE 'xp-%'",
                             (SHOP,)).fetchone()["c"]
        assert total == 100
    finally:
        conn.close()


def _w_killed_mid_txn(db, _out):
    conn = _open(db)
    conn.execute("INSERT INTO flags (shop_id, key, value)"
                 " VALUES (?, 'xp-doomed', 'v')", (SHOP,))
    os._exit(1)  # die without commit: nothing may persist


def test_killed_writer_leaves_clean_db_and_redelivery_works(tmp_path):
    db = str(tmp_path / "kill.db")
    conn = _open(db)
    from app.db import init_db
    from app.setup import skip_setup
    init_db(conn)
    skip_setup(conn, SHOP)
    conn.commit()
    conn.close()
    ctx = mp.get_context("spawn")
    out = ctx.Queue()
    proc = ctx.Process(target=_w_killed_mid_txn, args=(db, out))
    proc.start()
    proc.join(60)
    assert proc.exitcode == 1
    conn = _open(db)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) c FROM flags"
                            " WHERE shop_id = ? AND key = 'xp-doomed'",
                            (SHOP,)).fetchone()["c"] == 0
        # redelivery of the same unit succeeds exactly once
        conn.execute("INSERT INTO flags (shop_id, key, value)"
                     " VALUES (?, 'xp-doomed', 'v')", (SHOP,))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) c FROM flags"
                            " WHERE shop_id = ? AND key = 'xp-doomed'",
                            (SHOP,)).fetchone()["c"] == 1
    finally:
        conn.close()


def _w_dedupe_mark(db, out):
    import app.main as main_mod
    out.put({"seen": main_mod._already_seen("xp-mid-1", SHOP)})


def test_dedupe_survives_worker_handoff(tmp_path, monkeypatch):
    db = str(tmp_path / "dd.db")
    monkeypatch.setenv("SISI_DB", db)
    conn = _open(db)
    from app.db import init_db
    init_db(conn)
    conn.close()
    assert _run(_w_dedupe_mark, db)["seen"] is False  # first delivery
    assert _run(_w_dedupe_mark, db)["seen"] is True  # fresh worker: dup
