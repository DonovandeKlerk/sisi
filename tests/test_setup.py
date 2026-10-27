"""Guided setup: 6 steps, skips, back/change, resume, resets."""

import io

import pytest

from app import reports
from app.agent import MEDIA_OUTBOX, handle_photo, handle_text
from app.setup import setup_step
from app.tools import account_balance, ensure_shop


SHOP = "27710000016"


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(reports, "LOGOS_DIR", tmp_path / "logos")
    monkeypatch.delenv("SISI_PUBLIC_URL", raising=False)
    MEDIA_OUTBOX.clear()


def _photo():
    from PIL import Image
    img = Image.new("RGB", (120, 80), "white")
    px = img.load()
    for x in range(40, 80):
        for y in range(20, 60):
            px[x, y] = (20, 80, 40)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _run(conn, shop, answers):
    out = []
    for a in answers:
        msg, _ = handle_text(conn, shop, a)
        out.append(msg)
    return out


def test_new_number_starts_setup(conn):
    msg, buttons = handle_text(conn, SHOP, "hello")
    assert buttons is None
    assert "Step 1 of 6" in msg and "business name" in msg
    assert setup_step(conn, SHOP) == 1


def test_full_flow_with_skips(conn):
    _run(conn, SHOP, ["hi", "Kwamama Spaza"])
    assert setup_step(conn, SHOP) == 2
    out = _run(conn, SHOP, ["skip", "yes"])
    assert "Step 4 of 6" in out[1]
    out = _run(conn, SHOP, ["skip"])
    assert "Step 5 of 6" in out[0]
    out = _run(conn, SHOP, ["1500 cash, 300 petty"])
    assert "Step 6 of 6" in out[0]
    assert account_balance(conn, SHOP, "cash") == 150000
    assert account_balance(conn, SHOP, "petty") == 30000
    out = _run(conn, SHOP, ["isiZulu"])
    assert out[0].startswith("✅ *You're all set, Kwamama Spaza!*")
    assert "sold 2 bread" in out[0]
    assert setup_step(conn, SHOP) is None
    shop = ensure_shop(conn, SHOP)
    assert shop["phone"] == SHOP and shop["language"] == "zu"
    assert shop["address"] == "" and shop["logo_path"] == ""


def test_logo_photo_step(conn):
    _run(conn, SHOP, ["hi", "Shop", "12 Main Rd", "0820000001"])
    assert setup_step(conn, SHOP) == 4
    _, reply, _ = handle_photo(conn, SHOP, _photo())
    assert "Logo saved" in reply and "Step 5 of 6" in reply
    assert ensure_shop(conn, SHOP)["logo_path"] != ""


def test_photo_before_logo_step_redirects(conn):
    handle_text(conn, SHOP, "hi")
    _, reply, _ = handle_photo(conn, SHOP, _photo())
    assert "finish setting up" in reply and "Step 1 of 6" in reply


def test_back_and_change(conn):
    _run(conn, SHOP, ["hi", "First Name", "Somewhere"])
    assert setup_step(conn, SHOP) == 3
    out = _run(conn, SHOP, ["back"])
    assert "Step 2 of 6" in out[0]
    _run(conn, SHOP, ["Elsewhere"])
    out = _run(conn, SHOP, ["change name"])
    assert "Step 1 of 6" in out[0]
    out = _run(conn, SHOP, ["Better Name"])
    assert "Step 3 of 6" in out[0]  # returned to where we were
    assert ensure_shop(conn, SHOP)["name"] == "Better Name"


def test_resume_after_help(conn):
    _run(conn, SHOP, ["hi", "Shop", "Addr"])
    msg, _ = handle_text(conn, SHOP, "help")
    assert "Demo help" in msg
    msg, _ = handle_text(conn, SHOP, "yes")
    assert msg.startswith("Let's finish setting up, step 4 of 6")


def test_setup_command_restarts(conn):
    _run(conn, SHOP, ["hi", "Shop", "Addr", "yes", "skip", "100 cash", "english"])
    assert setup_step(conn, SHOP) is None
    msg, _ = handle_text(conn, SHOP, "setup")
    assert "Step 1 of 6" in msg


def test_shops_with_books_skip_setup(conn, monkeypatch):
    import app.agent as agent
    ensure_shop(conn, SHOP)
    conn.execute("INSERT INTO products (shop_id, name, stock_qty) VALUES (?, 'Bread', 5)",
                 (SHOP,))
    conn.commit()
    monkeypatch.setattr(agent, "llm_chat", lambda c, s, t: "roger")
    msg, _ = handle_text(conn, SHOP, "hello")
    assert msg == "roger"


def test_reset_demo_runs_setup_then_reseeds(conn):
    from seed.seed_random import DEMO_NUMBER
    msg, _ = handle_text(conn, DEMO_NUMBER, "reset demo")
    assert "Step 1 of 6" in msg
    _run(conn, DEMO_NUMBER, ["Fresh Spaza", "skip", "yes", "skip",
                             "100 cash", "english"])
    n = conn.execute("SELECT COUNT(*) n FROM products WHERE shop_id = ?",
                     (DEMO_NUMBER,)).fetchone()["n"]
    assert 25 <= n <= 30
    assert ensure_shop(conn, DEMO_NUMBER)["name"] == "Fresh Spaza"


def test_reset_shop_needs_confirmation(conn):
    ensure_shop(conn, SHOP)
    conn.execute("INSERT INTO products (shop_id, name, stock_qty) VALUES (?, 'Bread', 5)",
                 (SHOP,))
    conn.commit()
    msg, _ = handle_text(conn, SHOP, "reset shop")
    assert "erases ALL" in msg and "yes erase everything" in msg
    n = conn.execute("SELECT COUNT(*) n FROM products WHERE shop_id = ?",
                     (SHOP,)).fetchone()["n"]
    assert n == 1  # nothing wiped yet
    msg, _ = handle_text(conn, SHOP, "yes erase everything")
    assert "Step 1 of 6" in msg
    n = conn.execute("SELECT COUNT(*) n FROM products WHERE shop_id = ?",
                     (SHOP,)).fetchone()["n"]
    assert n == 0


def test_staff_bypass_setup(conn):
    from app.tools import add_staff
    ensure_shop(conn, STAFF_OWNER := "27710000017")
    add_staff(conn, STAFF_OWNER, "27820000001", "Thandi")
    msg, _ = handle_text(conn, "27820000001", "setup")
    assert msg.startswith("⛔")


def test_setup_prompts_follow_style(conn):
    from test_format import assert_format
    from app.setup import start_setup, step_prompt
    ensure_shop(conn, SHOP)
    assert_format(start_setup(conn, SHOP))
    for step in range(1, 7):
        assert_format(step_prompt(conn, SHOP, step))
