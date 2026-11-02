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
    assert "Step 1 of 7" in msg and "business name" in msg
    assert setup_step(conn, SHOP) == 1


def test_full_flow_with_skips(conn):
    _run(conn, SHOP, ["hi", "Kwamama Spaza"])
    assert setup_step(conn, SHOP) == 2
    out = _run(conn, SHOP, ["skip", "yes"])
    assert "Step 4 of 7" in out[1]
    out = _run(conn, SHOP, ["skip"])
    assert "Step 5 of 7" in out[0]
    out = _run(conn, SHOP, ["1500 cash, 300 petty"])
    assert "Step 6 of 7" in out[0]
    assert account_balance(conn, SHOP, "cash") == 150000
    assert account_balance(conn, SHOP, "petty") == 30000
    out = _run(conn, SHOP, ["isiZulu"])
    assert "Isinyathelo 7 kwezi-7" in out[0] and "Icebo" in out[0]
    out = _run(conn, SHOP, ["yes"])
    assert out[0].startswith("✅ *Sewuhleliwe, Kwamama Spaza!*")
    assert "sold 2 bread" in out[0]
    assert setup_step(conn, SHOP) is None
    shop = ensure_shop(conn, SHOP)
    assert shop["phone"] == SHOP and shop["language"] == "zu"
    assert shop["address"] == "" and shop["logo_path"] == ""


def test_logo_photo_step(conn):
    _run(conn, SHOP, ["hi", "Shop", "12 Main Rd", "0820000001"])
    assert setup_step(conn, SHOP) == 4
    _, reply, _ = handle_photo(conn, SHOP, _photo())
    assert "Logo saved" in reply and "Step 5 of 7" in reply
    shop = ensure_shop(conn, SHOP)
    assert shop["logo_orig_path"] != ""  # original saved immediately
    assert shop["logo_path"].endswith("-tile.png")  # tile saved at once


def test_photo_before_logo_step_redirects(conn):
    handle_text(conn, SHOP, "hi")
    _, reply, _ = handle_photo(conn, SHOP, _photo())
    assert "finish setting up" in reply and "Step 1 of 7" in reply


def test_back_and_change(conn):
    _run(conn, SHOP, ["hi", "First Name", "Somewhere"])
    assert setup_step(conn, SHOP) == 3
    out = _run(conn, SHOP, ["back"])
    assert "Step 2 of 7" in out[0]
    _run(conn, SHOP, ["Elsewhere"])
    out = _run(conn, SHOP, ["change name"])
    assert "Step 1 of 7" in out[0]
    out = _run(conn, SHOP, ["Better Name"])
    assert "Step 3 of 7" in out[0]  # returned to where we were
    assert ensure_shop(conn, SHOP)["name"] == "Better Name"


def test_resume_after_help(conn):
    _run(conn, SHOP, ["hi", "Shop", "Addr"])
    msg, _ = handle_text(conn, SHOP, "help")
    assert "Demo help" in msg
    msg, _ = handle_text(conn, SHOP, "yes")
    assert msg.startswith("Let's finish setting up, step 4 of 7")


def test_setup_command_restarts(conn):
    _run(conn, SHOP, ["hi", "Shop", "Addr", "yes", "skip", "100 cash", "english",
                       "yes"])
    assert setup_step(conn, SHOP) is None
    msg, _ = handle_text(conn, SHOP, "setup")
    assert "Step 1 of 7" in msg


def test_shops_with_books_skip_setup(conn, monkeypatch):
    ensure_shop(conn, SHOP)
    conn.execute("INSERT INTO products (shop_id, name, stock_qty) VALUES (?, 'Bread', 5)",
                 (SHOP,))
    conn.commit()
    msg, _ = handle_text(conn, SHOP, "hello")
    assert "Step 1" not in msg and "Main menu" in msg


def test_reset_demo_runs_setup_then_reseeds(conn):
    from seed.seed_random import DEMO_NUMBER
    msg, _ = handle_text(conn, DEMO_NUMBER, "reset demo")
    assert "Step 1 of 7" in msg
    _run(conn, DEMO_NUMBER, ["Fresh Spaza", "skip", "yes", "skip",
                             "100 cash", "english", "yes"])
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
    assert "Step 1 of 7" in msg
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
    for step in range(1, 8):
        assert_format(step_prompt(conn, SHOP, step))


def _at_step(conn, shop, step):
    handle_text(conn, shop, "hi")
    answers = {1: "Kwamama", 2: "12 Main Rd", 3: "yes", 4: "skip",
               5: "100 cash", 6: "english"}
    for s in range(1, step):
        handle_text(conn, shop, answers[s])
    assert setup_step(conn, shop) == step


def _plan_flag(conn, shop, key):
    row = conn.execute("SELECT value FROM flags WHERE shop_id = ? AND key = ?",
                       (shop, key)).fetchone()
    return row["value"] if row else None


@pytest.mark.parametrize("reply,hm", [
    ("yes", None), ("Yes!", None), ("YES.", None), ("y", None),
    ("yebo", None), ("ja", None), ("ewe", None), ("ok", None),
    ("sharp", None), ("sure", None), ("please", None), ("on", None),
    ("6", (6, 0)), ("06", (6, 0)), ("6:00", (6, 0)), ("06:00", (6, 0)),
    ("5:30", (5, 30)), ("6am", (6, 0)), ("6 am", (6, 0)),
    ("6pm", (18, 0)), ("18h00", (18, 0)), ("6h30", (6, 30)),
    ("yes 6", (6, 0)), ("yes 06:00", (6, 0)), ("yes, 06:00", (6, 0)),
    ("yes! 5:30?", (5, 30)), ("yebo 6am", (6, 0)), ("ja, 18h00", (18, 0)),
])
def test_plan_step_lenient_yes_and_times(conn, reply, hm):
    shop = f"2771000{9000 + abs(hash(reply)) % 900}"
    _at_step(conn, shop, 7)
    msg, _ = handle_text(conn, shop, reply)
    assert msg.startswith("✅"), reply
    assert setup_step(conn, shop) is None
    assert _plan_flag(conn, shop, "plan_off") is None
    want = f"{hm[0]:02d}:{hm[1]:02d}" if hm else None
    assert _plan_flag(conn, shop, "plan_time") == want, reply


@pytest.mark.parametrize("reply", ["no", "No.", "nope", "nah", "nee", "off",
                                   "hayi", "cha", "aikona", "never", "skip",
                                   "no thanks", "no, thanks!"])
def test_plan_step_lenient_no(conn, reply):
    shop = f"2771000{9100 + abs(hash(reply)) % 90}"
    _at_step(conn, shop, 7)
    msg, _ = handle_text(conn, shop, reply)
    assert msg.startswith("✅"), reply
    assert _plan_flag(conn, shop, "plan_off") == "1"


def test_plan_step_pick_a_time_button(conn):
    _at_step(conn, SHOP, 7)
    msg, buttons = handle_text(conn, SHOP, "Pick a time")
    assert "What time" in msg and setup_step(conn, SHOP) == 7
    assert buttons == ["Yes", "Pick a time", "No"]
    msg, _ = handle_text(conn, SHOP, "half past five")
    assert msg.startswith("✅")  # unclear twice: default 06:00, move on
    assert setup_step(conn, SHOP) is None


def test_plan_step_never_loops_twice(conn):
    _at_step(conn, SHOP, 7)
    msg, _ = handle_text(conn, SHOP, "maybe later-ish?")
    assert "Step 7 of 7" in msg and setup_step(conn, SHOP) == 7
    msg, _ = handle_text(conn, SHOP, "dunno really")
    assert msg.startswith("✅") and setup_step(conn, SHOP) is None


def test_step_buttons_and_language_list(conn):
    from app.agent import take_lists
    msg, buttons = handle_text(conn, SHOP, "hi")
    assert buttons is None  # step 1 is free text
    _, buttons = handle_text(conn, SHOP, "Kwamama")
    assert buttons == ["Skip"]  # step 2
    _, buttons = handle_text(conn, SHOP, "1")
    assert buttons == ["Yes, use this number"]  # step 3, tap resolved
    _, buttons = handle_text(conn, SHOP, "yes")
    assert buttons == ["Skip"]  # step 4
    _, buttons = handle_text(conn, SHOP, "skip")
    assert buttons == ["Skip"]  # step 5
    msg, buttons = handle_text(conn, SHOP, "skip")
    assert buttons is None  # step 6 arrives as a list
    lists = take_lists(SHOP)
    assert len(lists) == 1
    assert lists[0]["button_text"] == "Language"
    assert lists[0]["rows"] == ["English", "isiZulu", "Afrikaans", "isiXhosa", "Sesotho", "Setswana"]
    msg, buttons = handle_text(conn, SHOP, "2")
    assert "Isinyathelo 7 kwezi-7" in msg  # digit fallback -> isiZulu
    assert buttons == ["Yes", "Pick a time", "No"]


@pytest.mark.parametrize("step,bad", [
    (1, "?"),
    (3, "huh?"),
    (4, "a logo please"),
    (5, "lots"),
    (6, "spanish"),
])
def test_each_step_recovers_after_one_retry(conn, step, bad):
    shop = f"2771999{100 + step}"
    _at_step(conn, shop, step)
    msg, _ = handle_text(conn, shop, bad)
    assert setup_step(conn, shop) == step  # one re-ask, then …
    msg, _ = handle_text(conn, shop, "still unclear??")
    assert setup_step(conn, shop) != step or msg.startswith("✅")


def test_step3_yes_number_combo_and_skip_float(conn):
    _at_step(conn, SHOP, 3)
    handle_text(conn, SHOP, "yes, 082 123 4567")
    assert ensure_shop(conn, SHOP)["phone"] == "0821234567"
    handle_text(conn, SHOP, "skip")  # step 4
    msg, _ = handle_text(conn, SHOP, "none")  # step 5 zero float
    assert "Step 6 of 7" in msg
