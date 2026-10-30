"""Turso backend contract: the REAL turso_serverless driver classes driven
by a fake transport (no network). Pins the exact semantics our code relies
on: implicit transactions, row_factory protocol, lastrowid, executescript,
error classification, and the db.py backend switch.
"""

import sqlite3

import pytest

from turso_serverless.connection import Connection
from turso_serverless.dbapi import IntegrityError as TursoIntegrityError
from turso_serverless.dbapi import OperationalError as TursoOperationalError
from turso_serverless.session import StmtResult


class FakeSession:
    """In-memory stand-in for the driver's HTTP session."""

    def __init__(self, script=None):
        self.script = dict(script or {})  # sql-prefix -> (columns, rows)
        self.statements = []
        self._autocommit = True
        self.closed = False
        self.failures = {}  # sql-prefix -> RuntimeError to raise
        self.next_rowid = 41

    @property
    def autocommit(self):
        return self._autocommit

    def execute_pipeline(self, requests, track_autocommit=True):
        for req in requests:
            self.statements.append(req.get("sql", req.get("type", "?")))
        return [{} for _ in requests]

    def close(self):
        self.closed = True
        self._autocommit = True  # server rolls back on stream close

    def execute_stmt(self, sql, args=None, named_args=None, want_rows=True):
        self.statements.append(sql)
        for prefix, err in self.failures.items():
            if sql.startswith(prefix):
                raise err
        upper = sql.strip().upper()
        if upper.startswith("BEGIN"):
            self._autocommit = False
        elif upper in ("COMMIT", "ROLLBACK"):
            self._autocommit = True
        columns, rows, affected, rowid = [], [], 0, None
        for prefix, (columns, rows) in self.script.items():
            if sql.startswith(prefix):
                columns, rows = list(columns), [tuple(r) for r in rows]
        if upper.startswith("INSERT"):
            affected = 1
            rowid = self.next_rowid
            self.next_rowid += 1
            if "OR IGNORE" in upper and "DUP" in sql:
                affected, rowid = 0, None  # server ignored the insert
        return StmtResult(columns=columns, rows=rows,
                          affected_rows=affected, last_insert_rowid=rowid)


def _conn(session):
    return Connection(session)


def test_implicit_begin_commit_pairing():
    sess = FakeSession()
    conn = _conn(sess)
    assert conn.in_transaction is False
    conn.execute("SELECT 1")
    assert [s for s in sess.statements if s.startswith("BEGIN")] == []
    conn.execute("INSERT INTO t (a) VALUES (?)", (1,))
    assert sess.statements[1].startswith("BEGIN")
    assert conn.in_transaction is True
    conn.execute("INSERT INTO t (a) VALUES (?)", (2,))  # same txn, no 2nd BEGIN
    assert len([s for s in sess.statements if s.startswith("BEGIN")]) == 1
    conn.commit()
    assert sess.statements[-1] == "COMMIT"
    assert conn.in_transaction is False


def test_rollback_and_close_rolls_back():
    sess = FakeSession()
    conn = _conn(sess)
    conn.execute("INSERT INTO t (a) VALUES (1)")
    conn.rollback()
    assert sess.statements[-1] == "ROLLBACK"
    conn.execute("INSERT INTO t (a) VALUES (2)")
    conn.close()
    assert sess.closed is True  # uncommitted work lost, like sqlite3


def test_row_factory_name_and_index_access():
    sess = FakeSession({"SELECT a, b": (["a", "b"], [(1, "x")])})
    conn = _conn(sess)
    conn.row_factory = lambda _cur, row: row  # exactly what db.py sets
    row = conn.execute("SELECT a, b FROM t").fetchone()
    assert row["a"] == 1 and row[1] == "x"
    assert row.keys() == ["a", "b"] and len(row) == 2
    assert tuple(row) == (1, "x")


def test_executescript_and_lastrowid():
    sess = FakeSession()
    conn = _conn(sess)
    conn.executescript("CREATE TABLE t (a); INSERT INTO t VALUES (1);")
    assert any(s.startswith("CREATE TABLE") for s in sess.statements)
    cur = conn.execute("INSERT INTO t (a) VALUES (1)")
    assert cur.lastrowid == 41


def test_error_classification():
    sess = FakeSession()
    conn = _conn(sess)
    sess.failures["INSERT"] = RuntimeError("UNIQUE constraint failed: t.a")
    with pytest.raises(TursoIntegrityError):
        conn.execute("INSERT INTO t (a) VALUES (1)")
    sess.failures["INSERT"] = RuntimeError("database is locked")
    with pytest.raises(TursoOperationalError):
        conn.execute("INSERT INTO t (a) VALUES (1)")


def test_error_code_path_classification():
    class Coded(RuntimeError):
        def __init__(self, code, msg):
            super().__init__(msg)
            self.code = code

    sess = FakeSession()
    conn = _conn(sess)
    sess.failures["INSERT"] = Coded("SQLITE_CONSTRAINT_UNIQUE", "uniq")
    with pytest.raises(TursoIntegrityError):
        conn.execute("INSERT INTO t (a) VALUES (1)")
    sess.failures["INSERT"] = Coded("SQLITE_BUSY", "busy")
    with pytest.raises(TursoOperationalError):
        conn.execute("INSERT INTO t (a) VALUES (1)")


def test_insert_or_ignore_rowcount():
    sess = FakeSession()
    conn = _conn(sess)
    assert conn.execute("INSERT OR IGNORE INTO t (a) VALUES (1)").rowcount == 1
    cur = conn.execute("INSERT OR IGNORE INTO t (a) VALUES ('DUP')")
    assert cur.rowcount == 0  # ignored insert surfaces 0, like sqlite3


def test_db_connect_turso_branch(monkeypatch):
    import app.db as dbmod
    import turso_serverless

    made = {}

    def fake_connect(url, auth_token=None):
        made["url"] = url
        assert auth_token == "tok-123"
        return _conn(FakeSession())

    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://sisi-test.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok-123")
    monkeypatch.setattr(turso_serverless, "connect", fake_connect)
    assert dbmod.using_turso() is True
    assert dbmod.using_turso("/tmp/x.db") is False  # explicit path: sqlite
    conn = dbmod.connect()
    assert made["url"] == "libsql://sisi-test.turso.io"
    assert conn.row_factory is not None
    row = conn.execute("SELECT 1").fetchone()  # PRAGMA + SELECT flow
    assert row is None  # fake has no scripted rows; must not crash
    monkeypatch.delenv("TURSO_DATABASE_URL")
    assert dbmod.using_turso() is False


def test_error_tuples_cover_both_backends():
    import app.db as dbmod

    assert issubclass(sqlite3.OperationalError, dbmod.DB_OPERATIONAL_ERROR)
    assert issubclass(TursoOperationalError, dbmod.DB_OPERATIONAL_ERROR)
    assert issubclass(sqlite3.Error, dbmod.DB_ERROR)
    with pytest.raises(dbmod.DB_ERROR):
        raise TursoOperationalError("boom")
    with pytest.raises(dbmod.DB_OPERATIONAL_ERROR):
        raise sqlite3.OperationalError("boom")


def test_schema_executescript_over_driver():
    import app.db as dbmod

    sess = FakeSession()
    conn = _conn(sess)
    dbmod.init_db(conn)  # full SCHEMA + ALTER sweep, recorded only
    assert any("CREATE TABLE IF NOT EXISTS shops" in s for s in sess.statements)
    assert any("ALTER TABLE" in s for s in sess.statements)
