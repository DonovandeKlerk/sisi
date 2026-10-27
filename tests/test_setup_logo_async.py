"""Setup never blocks on the logo: background polish, post-finish delivery."""

import io
import time

import pytest

from app import reports
from app.agent import MEDIA_OUTBOX, handle_photo, handle_text
from app.setup import LOGO_FAILED, LOGO_READY, setup_step
from app.setup import finish_polish as _real_finish
from app.tools import ensure_shop


SHOP = "27710000017"


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(reports, "LOGOS_DIR", tmp_path / "logos")
    monkeypatch.delenv("SISI_PUBLIC_URL", raising=False)
    import app.setup as setup
    monkeypatch.setattr(setup, "finish_polish",
                        lambda shop_id, timeout_s=60: "done")
    MEDIA_OUTBOX.clear()


def _png():
    from PIL import Image
    img = Image.new("RGB", (120, 80), "white")
    px = img.load()
    for x in range(40, 80):
        for y in range(20, 60):
            px[x, y] = (20, 80, 40)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _to_step4(conn):
    for a in ["hi", "Kwamama Spaza", "12 Main Rd", "yes"]:
        handle_text(conn, SHOP, a)
    assert setup_step(conn, SHOP) == 4


def _flag(conn, key):
    row = conn.execute("SELECT value FROM flags WHERE shop_id = ? AND key = ?",
                       (SHOP, key)).fetchone()
    return row["value"] if row else None


def _set_flag(conn, key, value):
    conn.execute("INSERT INTO flags (shop_id, key, value) VALUES (?, ?, ?)"
                 " ON CONFLICT (shop_id, key) DO UPDATE SET value = excluded.value",
                 (SHOP, key, value))
    conn.commit()


def test_step4_accepts_formats_and_sizes(conn):
    import os
    _to_step4(conn)
    for image in (_png(),
                  b"\xff\xd8\xff" + b"0" * 500,
                  b"RIFF" + b"0" * 4 + b"WEBP" + b"0" * 500,
                  b"raw-bytes-huge" * 200000):
        _, reply, _ = handle_photo(conn, SHOP, image)
        assert "Logo received" in reply and "Step 5 of 7" in reply
        assert _flag(conn, "logo-polish") == "pending"
        assert os.path.exists(ensure_shop(conn, SHOP)["logo_orig_path"])
        conn.execute("UPDATE flags SET value = '4' WHERE shop_id = ?"
                     " AND key = 'setup-step'", (SHOP,))
        conn.commit()


def test_nothing_sent_mid_setup(conn):
    _to_step4(conn)
    _, reply, _ = handle_photo(conn, SHOP, _png())
    assert "couldn't clean" not in reply and "logo is ready" not in reply
    msg, _ = handle_text(conn, SHOP, "1500 cash, 300 petty")
    assert "couldn't clean" not in msg and "logo is ready" not in msg


def test_failure_arrives_after_all_set(conn):
    _to_step4(conn)
    handle_photo(conn, SHOP, _png())
    _set_flag(conn, "logo-polish", "failed")
    handle_text(conn, SHOP, "1500 cash, 300 petty")
    handle_text(conn, SHOP, "english")
    msg, _ = handle_text(conn, SHOP, "yes")
    assert msg.startswith("✅ *You're all set")
    assert LOGO_FAILED in msg
    assert msg.index("You're all set") < msg.index("couldn't clean up")


def test_success_arrives_after_all_set_with_card(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("SISI_PUBLIC_URL", "https://sisi.example")
    _to_step4(conn)
    handle_photo(conn, SHOP, _png())
    logo = tmp_path / "logos" / "cut.png"
    logo.parent.mkdir(parents=True, exist_ok=True)
    logo.write_bytes(_png())
    conn.execute("UPDATE shops SET logo_path = ? WHERE shop_id = ?",
                 (str(logo), SHOP))
    conn.commit()
    _set_flag(conn, "logo-polish", "done")
    handle_text(conn, SHOP, "1500 cash, 300 petty")
    handle_text(conn, SHOP, "english")
    msg, _ = handle_text(conn, SHOP, "yes")
    assert msg.index("You're all set") < msg.index(LOGO_READY)
    captions = [m["caption"] for m in MEDIA_OUTBOX.get(SHOP, [])]
    assert "Your polished logo" in captions


def test_pending_then_next_message_delivers(conn, tmp_path, monkeypatch):
    import app.agent as agent
    monkeypatch.setattr(agent, "llm_chat", lambda c, s, t: "noted")
    _to_step4(conn)
    handle_photo(conn, SHOP, _png())
    handle_text(conn, SHOP, "1500 cash, 300 petty")
    handle_text(conn, SHOP, "english")
    msg, _ = handle_text(conn, SHOP, "yes")
    assert "logo is ready" not in msg and "couldn't clean" not in msg
    logo = tmp_path / "logos" / "cut.png"
    logo.parent.mkdir(parents=True, exist_ok=True)
    logo.write_bytes(_png())
    conn.execute("UPDATE shops SET logo_path = ? WHERE shop_id = ?",
                 (str(logo), SHOP))
    conn.commit()
    _set_flag(conn, "logo-polish", "done")
    msg, _ = handle_text(conn, SHOP, "hi again")
    assert LOGO_READY in msg
    msg, _ = handle_text(conn, SHOP, "hi once more")
    assert LOGO_READY not in msg  # delivered exactly once


def test_step_errors_skip_and_still_finish(conn, monkeypatch):
    import app.tools as tools
    monkeypatch.setattr(tools, "set_shop_name",
                        lambda c, s, n: (_ for _ in ()).throw(IOError("boom")))
    msg, _ = handle_text(conn, SHOP, "hi")
    assert "Step 1 of 7" in msg
    msg, _ = handle_text(conn, SHOP, "Kwamama")
    assert "Skipped step 1" in msg and "Step 2 of 7" in msg
    monkeypatch.setattr(tools, "set_float",
                        lambda c, s, a: (_ for _ in ()).throw(IOError("boom")))
    handle_text(conn, SHOP, "12 Main Rd")
    handle_text(conn, SHOP, "yes")
    handle_text(conn, SHOP, "skip")
    msg, _ = handle_text(conn, SHOP, "1500 cash, 300 petty")
    assert "Skipped step 5" in msg and "Step 6 of 7" in msg
    handle_text(conn, SHOP, "english")
    msg, _ = handle_text(conn, SHOP, "yes")
    assert msg.startswith("✅ *You're all set")
    assert setup_step(conn, SHOP) is None


def test_stale_pending_counts_as_failed(conn):
    ensure_shop(conn, SHOP)
    _set_flag(conn, "logo-polish", "pending")
    _set_flag(conn, "logo-polish-started", str(int(time.time()) - 61))
    from app.setup import polish_state
    assert polish_state(conn, SHOP) == "failed"
    assert _flag(conn, "logo-polish") == "failed"


def test_finish_polish_failure_and_success(tmp_path, monkeypatch):
    import os
    from app.db import connect, init_db
    real_finish = _real_finish
    monkeypatch.setenv("SISI_DB", str(tmp_path / "polish.db"))
    c = connect()
    init_db(c)
    ensure_shop(c, SHOP)
    orig = tmp_path / "logos" / "orig.png"
    orig.parent.mkdir(parents=True, exist_ok=True)
    orig.write_bytes(_png())
    c.execute("UPDATE shops SET logo_orig_path = ? WHERE shop_id = ?",
              (str(orig), SHOP))
    c.commit()
    import app.logo as logo_mod
    monkeypatch.setattr(logo_mod, "process_upload",
                        lambda raw: (_ for _ in ()).throw(ValueError("bad")))
    assert real_finish(SHOP) == "failed"
    row = c.execute("SELECT value FROM flags WHERE shop_id = ? AND key = ?",
                    (SHOP, "logo-polish")).fetchone()
    assert row["value"] == "failed"
    monkeypatch.setattr(logo_mod, "process_upload",
                        lambda raw: {"cutout": _png(), "original": _png(),
                                     "badge": _png(), "used_cutout": True})
    assert real_finish(SHOP) == "done"
    shop = ensure_shop(c, SHOP)
    assert shop["logo_path"] != "" and os.path.exists(shop["logo_path"])
    c.close()


def test_tick_delivers_logo_backstop_once(conn):
    from datetime import datetime
    from app.schedule import tick
    ensure_shop(conn, SHOP)
    conn.execute("UPDATE shops SET channel = 'vonage' WHERE shop_id = ?",
                 (SHOP,))
    conn.execute("INSERT INTO flags (shop_id, key, value)"
                 " VALUES (?, 'setup-done', '1')", (SHOP,))
    conn.commit()
    _set_flag(conn, "logo-polish", "failed")
    morning = datetime(2026, 10, 26, 10, 0)  # Monday, before nudge time
    jobs = tick(conn, morning)
    logo_jobs = [j for j in jobs if j["key"] == "logo-ready"]
    assert len(logo_jobs) == 1
    assert logo_jobs[0]["text"] == LOGO_FAILED
    assert tick(conn, morning) == []
