"""Window-aware reminder delivery: states, DLR correlation, templates.

Enqueueing (or API acceptance) must never mean delivered. Reminders move
pending -> queued -> submitted -> delivered, or wait as blocked with a
visible reason, or fail visibly. Nothing is silently discarded.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.reminders import (
    SAST,
    list_reminders,
    mark_reminder_failed,
    now_sast,
    record_dlr,
    retry_stuck,
    save_reminder,
    send_due,
    window_open,
)
from app.sendqueue import drain, enqueue
from app.tools import ensure_shop

SHOP = "27710000111"
NOW = datetime(2026, 10, 29, 9, 0, tzinfo=SAST)  # Thursday 09:00 SAST


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SISI_REMINDER_TEMPLATE", raising=False)
    monkeypatch.delenv("SISI_REMINDER_TEMPLATE_LANG", raising=False)


def _shop(conn, channel="infobip"):
    ensure_shop(conn, SHOP, "Window Spaza")
    conn.execute("UPDATE shops SET channel = ? WHERE shop_id = ?",
                 (channel, SHOP))
    conn.commit()


def _seen(conn, when: datetime):
    conn.execute("INSERT INTO messages_seen (message_id, shop_id, created_at)"
                 " VALUES (?, ?, ?)",
                 (f"mid-{when.isoformat()}", SHOP, when.isoformat()))
    conn.commit()


def _due(conn, **kw):
    kw.setdefault("due", NOW - timedelta(minutes=1))
    kw.setdefault("summary", "pack the order")
    return save_reminder(conn, SHOP, kw["due"], kw["summary"],
                         kw.get("context"), kw.get("recurrence", "none"),
                         record_action=False)


class _StubSender:
    """Sender stub returning a fixed provider id."""

    def __init__(self, mid="pv-1"):
        self.mid = mid
        self.calls = []

    def send_text(self, to, body):
        self.calls.append(("text", to, body))
        return self.mid


def test_enqueue_is_not_delivered(conn):
    _shop(conn)
    _seen(conn, NOW - timedelta(hours=1))
    rid = _due(conn)
    assert send_due(conn, NOW) == [rid]
    row = conn.execute("SELECT status FROM reminders WHERE id = ?",
                       (rid,)).fetchone()
    assert row["status"] == "queued"
    assert conn.execute("SELECT COUNT(*) n FROM send_queue"
                        " WHERE to_number = ?", (SHOP,)).fetchone()["n"] == 1


def test_drain_marks_submitted_with_provider_id(conn):
    _shop(conn)
    _seen(conn, NOW - timedelta(hours=1))
    rid = _due(conn)
    send_due(conn, NOW)
    stats = drain(conn, SHOP, _StubSender("pv-9"), sleep=lambda s: None)
    assert stats["sent"] == 1
    row = conn.execute("SELECT status, provider_msg_id FROM reminders"
                       " WHERE id = ?", (rid,)).fetchone()
    assert (row["status"], row["provider_msg_id"]) == ("submitted", "pv-9")
    assert conn.execute("SELECT COUNT(*) n FROM send_queue"
                        " WHERE to_number = ?", (SHOP,)).fetchone()["n"] == 0


def test_dlr_delivered_and_duplicate_is_idempotent(conn):
    _shop(conn)
    rid = _due(conn)
    conn.execute("UPDATE reminders SET status = 'submitted',"
                 " provider_msg_id = 'pv-d' WHERE id = ?", (rid,))
    conn.commit()
    assert record_dlr(conn, "pv-d", "delivered") is True
    assert record_dlr(conn, "pv-d", "delivered") is True
    row = conn.execute("SELECT status FROM reminders WHERE id = ?",
                       (rid,)).fetchone()
    assert row["status"] == "delivered"


def test_dlr_out_of_order_delivery_wins(conn):
    _shop(conn)
    rid = _due(conn)
    conn.execute("UPDATE reminders SET status = 'submitted',"
                 " provider_msg_id = 'pv-o' WHERE id = ?", (rid,))
    conn.commit()
    assert record_dlr(conn, "pv-o", "failed", "first report") is True
    assert record_dlr(conn, "pv-o", "delivered") is True
    assert record_dlr(conn, "pv-o", "failed", "stale report") is True
    row = conn.execute("SELECT status, dlr_detail FROM reminders WHERE id = ?",
                       (rid,)).fetchone()
    assert (row["status"], row["dlr_detail"]) == ("delivered", "")


def test_dlr_unknown_id_ignored(conn):
    _shop(conn)
    assert record_dlr(conn, "no-such-id", "delivered") is False


def test_window_closed_blocks_with_reason(conn):
    _shop(conn)
    rid = _due(conn)
    assert window_open(conn, SHOP, "infobip", NOW) is False
    assert send_due(conn, NOW) == []
    row = conn.execute("SELECT status, blocked_reason FROM reminders"
                       " WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "blocked"
    assert "24h window" in row["blocked_reason"]
    assert conn.execute("SELECT COUNT(*) n FROM send_queue"
                        " WHERE to_number = ?", (SHOP,)).fetchone()["n"] == 0
    assert "⛔ blocked" in list_reminders(conn, SHOP)


def test_window_reopen_recovers_without_duplicates(conn):
    _shop(conn)
    rid = _due(conn)
    send_due(conn, NOW)
    _seen(conn, NOW)
    assert send_due(conn, NOW) == [rid]
    assert conn.execute("SELECT COUNT(*) n FROM send_queue"
                        " WHERE to_number = ?", (SHOP,)).fetchone()["n"] == 1
    assert send_due(conn, NOW) == []  # already queued: no second row


def test_template_used_when_configured(conn, monkeypatch):
    monkeypatch.setenv("SISI_REMINDER_TEMPLATE", "reminder_poke")
    _shop(conn)
    rid = _due(conn)
    assert send_due(conn, NOW) == [rid]
    row = conn.execute("SELECT kind, payload_json FROM send_queue"
                       " WHERE to_number = ?", (SHOP,)).fetchone()
    assert row["kind"] == "template"
    import json
    payload = json.loads(row["payload_json"])
    assert payload["template"] == "reminder_poke"
    assert payload["reminder_id"] == rid


def test_template_drain_marks_submitted(conn, monkeypatch):
    class _TplSender(_StubSender):
        def send_template(self, to, name, lang="en", placeholders=None):
            self.calls.append(("template", name, placeholders))
            return "pv-tpl"

    monkeypatch.setenv("SISI_REMINDER_TEMPLATE", "reminder_poke")
    _shop(conn)
    rid = _due(conn)
    send_due(conn, NOW)
    drain(conn, SHOP, _TplSender(), sleep=lambda s: None)
    row = conn.execute("SELECT status, provider_msg_id FROM reminders"
                       " WHERE id = ?", (rid,)).fetchone()
    assert (row["status"], row["provider_msg_id"]) == ("submitted", "pv-tpl")


def test_meta_without_template_impl_stays_blocked(conn, monkeypatch):
    monkeypatch.setenv("SISI_REMINDER_TEMPLATE", "reminder_poke")
    _shop(conn, "meta")
    rid = _due(conn)
    assert send_due(conn, NOW) == []
    row = conn.execute("SELECT status FROM reminders WHERE id = ?",
                       (rid,)).fetchone()
    assert row["status"] == "blocked"


def test_vonage_channel_is_window_exempt(conn):
    _shop(conn, "vonage")
    rid = _due(conn)
    assert window_open(conn, SHOP, "vonage", NOW) is True
    assert send_due(conn, NOW) == [rid]


def test_final_failure_marks_failed_visibly(conn):
    _shop(conn)
    _seen(conn, NOW - timedelta(hours=1))
    rid = _due(conn)
    send_due(conn, NOW)
    conn.execute("UPDATE send_queue SET attempts = 99 WHERE to_number = ?",
                 (SHOP,))
    conn.commit()

    class _Boom:
        def send_text(self, to, body):
            raise RuntimeError("simulated outage")

    drain(conn, SHOP, _Boom(), sleep=lambda s: None)
    row = conn.execute("SELECT status, dlr_detail FROM reminders WHERE id = ?",
                       (rid,)).fetchone()
    assert row["status"] == "failed"
    assert "RuntimeError" in row["dlr_detail"]
    assert "❌ failed" in list_reminders(conn, SHOP)


def test_retry_command_recovers_and_reports(conn):
    _shop(conn)
    _due(conn)
    send_due(conn, NOW)
    assert "0 re-queued, 1 still blocked" in retry_stuck(conn, SHOP, NOW)
    _seen(conn, NOW)
    assert "1 re-queued, 0 still blocked" in retry_stuck(conn, SHOP, NOW)
    assert "No stuck reminders" in retry_stuck(conn, SHOP, NOW)


def test_retry_command_routes(conn):
    from app.agent import handle_text
    from app.setup import skip_setup
    skip_setup(conn, SHOP)
    _shop(conn)
    msg, _b = handle_text(conn, SHOP, "retry reminders")
    assert "No stuck reminders" in msg


def test_recurring_keeps_fired_instance(conn):
    _shop(conn)
    _seen(conn, NOW - timedelta(hours=1))
    rid = _due(conn, recurrence="daily")
    send_due(conn, NOW)
    rows = conn.execute("SELECT id, status FROM reminders WHERE shop_id = ?"
                        " ORDER BY id", (SHOP,)).fetchall()
    assert [(r["id"], r["status"]) for r in rows] == [(rid, "queued"),
                                                     (rid + 1, "pending")]
    nxt = conn.execute("SELECT due_at FROM reminders WHERE id = ?",
                       (rid + 1,)).fetchone()["due_at"]
    assert nxt.startswith("2026-10-30")


def test_stuck_claims_recovered(conn):
    _shop(conn)
    _seen(conn, NOW - timedelta(hours=1))
    rid = _due(conn)
    conn.execute("UPDATE reminders SET status = 'sending',"
                 " sent_at = '2000-01-01T00:00:00+02:00' WHERE id = ?", (rid,))
    conn.commit()
    assert send_due(conn, NOW) == [rid]
    conn.execute("UPDATE send_queue SET status = 'sending', claimed_at = 1"
                 " WHERE to_number = ?", (SHOP,))
    conn.commit()
    stats = drain(conn, SHOP, _StubSender(), sleep=lambda s: None)
    assert stats["sent"] == 1


def test_tracked_rows_never_merge(conn):
    _shop(conn)
    enqueue(conn, SHOP, "meta", "text", {"body": "plain", "reminder_id": None})
    conn.execute("UPDATE send_queue SET payload_json = '{\"body\": \"plain\"}'"
                 " WHERE to_number = ?", (SHOP,))
    enqueue(conn, SHOP, "meta", "text", {"body": "tracked", "reminder_id": 7})
    conn.commit()
    sender = _StubSender()
    drain(conn, SHOP, sender, sleep=lambda s: None)
    assert [c[2] for c in sender.calls] == ["plain", "tracked"]


def test_cancel_and_move_cover_blocked(conn):
    from app.reminders import cancel_numbered, move_numbered
    _shop(conn)
    rid = _due(conn)
    send_due(conn, NOW)
    assert "cancelled" in cancel_numbered(conn, SHOP, 1).lower()
    assert conn.execute("SELECT status FROM reminders WHERE id = ?",
                       (rid,)).fetchone()["status"] == "cancelled"
    rid2 = _due(conn)
    send_due(conn, NOW)
    assert "06:00" in move_numbered(conn, SHOP, 1, "6am", NOW)


def test_snooze_recovers_fired_reminder(conn):
    from app.reminders import snooze_last
    _shop(conn)
    _seen(conn, NOW - timedelta(hours=1))
    rid = _due(conn)
    send_due(conn, NOW)
    out = snooze_last(conn, SHOP, 30, NOW)
    assert "09:30" in out
    row = conn.execute("SELECT status FROM reminders WHERE id = ?",
                       (rid,)).fetchone()
    assert row["status"] == "pending"


def test_undo_reminder_cancels_blocked(conn):
    from app.tools import _action_id, undo_last
    _shop(conn)
    rid = _due(conn)
    send_due(conn, NOW)
    conn.execute("INSERT INTO actions (action_id, shop_id, kind, summary,"
                 " data_json) VALUES (?, ?, 'reminder', ?, ?)",
                 (_action_id(), SHOP, "reminder: x",
                  f'{{"reminder_id": {rid}}}'))
    conn.commit()
    undo_last(conn, SHOP)
    assert conn.execute("SELECT status FROM reminders WHERE id = ?",
                       (rid,)).fetchone()["status"] == "cancelled"


def test_mark_failed_only_from_live_states(conn):
    _shop(conn)
    rid = _due(conn)
    mark_reminder_failed(conn, rid, "boom")
    assert conn.execute("SELECT status FROM reminders WHERE id = ?",
                       (rid,)).fetchone()["status"] == "pending"
    conn.execute("UPDATE reminders SET status = 'queued' WHERE id = ?", (rid,))
    conn.commit()
    mark_reminder_failed(conn, rid, "boom")
    assert conn.execute("SELECT status FROM reminders WHERE id = ?",
                       (rid,)).fetchone()["status"] == "failed"


def test_daily_plan_shows_stuck_reminders(conn):
    from app.schedule import daily_plan
    _shop(conn)
    _due(conn)
    send_due(conn, NOW)
    assert "reminders stuck" in daily_plan(conn, SHOP, "2026-10-29")


# --- webhook wiring ------------------------------------------------------------


def _wired_db(tmp_path, monkeypatch):
    import sqlite3

    import app.db as _db
    db = str(tmp_path / "dlr.db")
    monkeypatch.setenv("SISI_DB", db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    init_db(c)
    return c, db


def test_infobip_dlr_webhook_correlates(tmp_path, monkeypatch):
    c, _db = _wired_db(tmp_path, monkeypatch)
    ensure_shop(c, SHOP, "DLR Spaza")
    c.execute("UPDATE shops SET channel = 'infobip' WHERE shop_id = ?",
              (SHOP,))
    rid = save_reminder(c, SHOP, NOW, "x", None, "none",
                        record_action=False)
    c.execute("UPDATE reminders SET status = 'submitted',"
              " provider_msg_id = 'ib-1' WHERE id = ?", (rid,))
    c.commit()
    c.close()
    client = TestClient(app)
    dlr = {"results": [{"messageId": "ib-1", "to": SHOP,
                        "status": {"name": "DELIVERED_TO_HANDSET"}}]}
    assert client.post("/webhook/infobip/dlr", json=dlr).status_code == 200
    assert client.post("/webhook/infobip/dlr", json=dlr).status_code == 200
    import sqlite3
    c = sqlite3.connect(_db)
    row = c.execute("SELECT status FROM reminders WHERE id = ?",
                    (rid,)).fetchone()
    c.close()
    assert row[0] == "delivered"


def test_vonage_status_webhook_correlates(tmp_path, monkeypatch):
    c, _db = _wired_db(tmp_path, monkeypatch)
    ensure_shop(c, SHOP, "DLR Spaza")
    rid = save_reminder(c, SHOP, NOW, "x", None, "none",
                        record_action=False)
    c.execute("UPDATE reminders SET status = 'submitted',"
              " provider_msg_id = 'vg-1' WHERE id = ?", (rid,))
    c.commit()
    c.close()
    client = TestClient(app)
    body = {"message_uuid": "vg-1", "status": "delivered"}
    assert client.post("/webhook/vonage/status", json=body).status_code == 200
    import sqlite3
    c = sqlite3.connect(_db)
    row = c.execute("SELECT status FROM reminders WHERE id = ?",
                    (rid,)).fetchone()
    c.close()
    assert row[0] == "delivered"


def test_meta_statuses_correlate(tmp_path, monkeypatch):
    c, _db = _wired_db(tmp_path, monkeypatch)
    monkeypatch.delenv("APP_SECRET", raising=False)
    ensure_shop(c, SHOP, "DLR Spaza")
    rid = save_reminder(c, SHOP, NOW, "x", None, "none",
                        record_action=False)
    c.execute("UPDATE reminders SET status = 'submitted',"
              " provider_msg_id = 'wamid-1' WHERE id = ?", (rid,))
    c.commit()
    c.close()
    client = TestClient(app)
    payload = {"object": "whatsapp_business_account", "entry": [{
        "changes": [{"value": {"statuses": [
            {"id": "wamid-1", "status": "failed",
             "errors": [{"code": 131047, "title": "window closed"}]}]}}]}]}
    assert client.post("/webhook", json=payload).status_code == 200
    import sqlite3
    c = sqlite3.connect(_db)
    row = c.execute("SELECT status, dlr_detail FROM reminders WHERE id = ?",
                    (rid,)).fetchone()
    c.close()
    assert row[0] == "failed" and "window closed" in row[1]


def test_window_open_parses_legacy_seen_format(conn):
    _shop(conn)
    conn.execute("INSERT INTO messages_seen (message_id, shop_id, created_at)"
                 " VALUES ('m1', ?, '2026-10-29T08:59:123456Z')", (SHOP,))
    conn.commit()
    assert window_open(conn, SHOP, "meta", NOW) is True
    conn.execute("UPDATE messages_seen SET created_at ="
                 " '2026-10-27T08:59:123456Z' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    assert window_open(conn, SHOP, "meta", NOW) is False
