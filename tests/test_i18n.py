"""Part G: six-language support (English, isiZulu, Afrikaans, isiXhosa, Sesotho, Setswana)."""

import ast
import string
from datetime import datetime
from pathlib import Path

import pytest

import app.agent as agent
from app import reports
from app.agent import MEDIA_OUTBOX, handle_text, handle_voice, take_media
from app.i18n import CURRENT_LANG, STRINGS, detect
from app.reminders import SAST, parse_request
from app.tools import (
    _find_products,
    compliance_guide,
    customer_statement,
    ensure_shop,
    parse_month_name,
    set_customer_language,
)

SHOP = "27710000099"
MON_10AM = datetime(2026, 10, 26, 10, 0)


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(reports, "LOGOS_DIR", tmp_path / "logos")
    monkeypatch.setenv("SISI_PUBLIC_URL", "https://x.example")
    MEDIA_OUTBOX.clear()


@pytest.fixture()
def shop(conn):
    from app.setup import skip_setup
    ensure_shop(conn, SHOP, "I18n Spaza")
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    skip_setup(conn, SHOP)
    return SHOP


@pytest.fixture()
def ledger(conn, shop):
    sipho = conn.execute(
        "INSERT INTO customers (shop_id, name) VALUES (?, 'Sipho')",
        (SHOP,)).lastrowid
    conn.execute(
        "INSERT INTO credit_entries (shop_id, customer_id, amount_cents,"
        " items_text, action_id, created_at) VALUES (?, ?, 4000, 'Bread',"
        " 'i18n-ce-0', '2026-10-12T10:00:00.000Z')",
        (SHOP, sipho))
    conn.commit()
    return sipho


def _pdf_bytes(conn):
    items = take_media(SHOP)
    url = next(i["url"] for i in items if i["kind"] == "file")
    token = url.split("/media/")[1].split("?")[0]
    row = reports.get_report_by_token(conn, token)
    return Path(row["png_path"] or row["pdf_path"]).read_bytes()


# --- translations file ------------------------------------------------------


def test_key_sets_match_across_languages():
    keys = sorted(STRINGS["en"])
    assert len(keys) > 500
    for lang in ("zu", "af", "xh", "st", "tn"):
        assert sorted(STRINGS[lang]) == keys, lang


def test_every_callsite_key_exists():
    root = Path("app")
    missing = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name not in ("t", "_t"):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            key = node.args[0].value
            for lang in ("en", "zu", "af", "xh", "st", "tn"):
                if key not in STRINGS[lang]:
                    missing.append((str(path), key, lang))
    assert missing == []


def test_placeholders_match_across_languages():
    for key in STRINGS["en"]:
        want = {v for _, v, _, _ in string.Formatter().parse(STRINGS["en"][key])
                if v}
        for lang in ("zu", "af", "xh", "st", "tn"):
            got = {v for _, v, _, _ in string.Formatter().parse(STRINGS[lang][key])
                   if v}
            assert got == want, (key, lang)


def test_translated_lines_fit_whatsapp_format():
    for lang in ("en", "zu", "af", "xh", "st", "tn"):
        for key, template in STRINGS[lang].items():
            for line in template.split("\n"):
                assert len(line) <= 120, (lang, key, line[:60])
            if len(template) > 300:
                assert "\n" in template, (lang, key)


# --- detection --------------------------------------------------------------


@pytest.mark.parametrize("text,default,want", [
    ("what does Sipho owe", "en", "en"),
    ("ngicela isitatimende sikaSipho", "en", "zu"),
    ("stuur vir my die strokie van Sipho", "en", "af"),
    ("ndicela istatimenti sikaSipho", "en", "xh"),
    ("ndikhumbuze ngomso ukuhlawula irenti", "en", "xh"),
    ("herinner my more 7vm om huur te betaal", "en", "af"),
    ("yebo, stuur asseblief", "en", "af"),  # mixed slang: most markers win
    ("dumela kea leboga, tjhelete e kae", "en", "st"),
    ("dumela rra, ke a leboga, madi a kae", "en", "tn"),
    ("dumela lotlang, bua le nna", "en", "en"),  # st/tn tie keeps default
    ("sawubona hello", "af", "af"),  # tie keeps the default
    ("undo", "zu", "zu"),  # neutral keeps the default
    ("cash up", "zu", "en"),  # 'cash' is an English marker
    ("", "zu", "zu"),
    ("xyzzy", "xx", "en"),  # unknown default falls back to English
])
def test_detect(text, default, want):
    assert detect(text, default) == want


# --- replies follow the message, defaulting to the owner's language ---------


@pytest.mark.parametrize("text,caption", [
    ("ngicela isitatimende sikaSipho", "Mthumelele u-Sipho 👆"),
    ("stuur vir my die strokie van Sipho", "Stuur dit aan Sipho 👆"),
    ("ndicela istatimenti sikaSipho", "Mthumelele ku-Sipho 👆"),
    ("kea kopa setatemente sa Sipho", "Fetisetsa ena ho Sipho 👆"),
    ("rra ke kopa setatemente sa Sipho madi", "Fetisetsa ena go Sipho 👆"),
])
def test_message_language_overrides_default(conn, shop, ledger, text, caption):
    msg, _ = handle_text(conn, SHOP, text)
    assert "R40.00" in msg
    image = next(i for i in take_media(SHOP) if i["kind"] == "image")
    assert image["caption"] == caption


def test_owner_default_for_neutral_message(conn, shop):
    conn.execute("UPDATE shops SET language = 'zu' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    msg, _ = handle_text(conn, SHOP, "undo")
    assert msg == "Akukho okuhlehliwa."


@pytest.mark.parametrize("lang,head", [
    ("en", "📋 *Main menu*"),
    ("zu", "📋 *Imenyu enkulu*"),
    ("af", "📋 *Hoofkieslys*"),
    ("xh", "📋 *Imenyu ephambili*"),
    ("st", "📋 *Lenane le ka sehloohong*"),
    ("tn", "📋 *Lenane le legolo*"),
])
def test_help_menu_per_language(conn, shop, lang, head):
    from app.agent import LIST_OUTBOX, MENU_ROWS, menu_display_rows, take_lists
    conn.execute("UPDATE shops SET language = ? WHERE shop_id = ?", (lang, SHOP))
    conn.commit()
    msg, _ = handle_text(conn, SHOP, "help")
    assert msg.startswith(head)
    queued = take_lists(SHOP)
    assert queued and queued[0]["rows"] == menu_display_rows()
    assert len(queued[0]["rows"]) == 7
    assert SHOP not in LIST_OUTBOX


@pytest.mark.parametrize("lang,head", [
    ("en", "❓ *Demo help — try me*"),
    ("zu", "❓ *Usizo — zama mina*"),
    ("af", "❓ *Hulp — probeer my*"),
    ("xh", "❓ *Uncedo — ndizame*"),
    ("st", "❓ *Thuso ya teko — leka nna*"),
    ("tn", "❓ *Thuso ya teko — leka nna*"),
])
def test_demo_help_text_per_language(conn, shop, lang, head):
    conn.execute("UPDATE shops SET language = ? WHERE shop_id = ?", (lang, SHOP))
    conn.commit()
    msg, _ = handle_text(conn, SHOP, "demo help")
    assert msg.startswith(head)


def test_speak_switches_owner_language(conn, shop):
    msg, _ = handle_text(conn, SHOP, "speak Afrikaans")
    assert msg == "🗣️ *Reg so, ek praat nou Afrikaans.*"
    row = conn.execute("SELECT language FROM shops WHERE shop_id = ?",
                       (SHOP,)).fetchone()
    assert row["language"] == "af"
    msg, _ = handle_text(conn, SHOP, "khuluma isiXhosa")
    assert msg == "🗣️ *Kulungile, ndizothetha isiXhosa.*"
    assert conn.execute("SELECT language FROM shops WHERE shop_id = ?",
                        (SHOP,)).fetchone()["language"] == "xh"
    msg, _ = handle_text(conn, SHOP, "bua Sesotho")
    assert msg == "🗣️ *Sharp, I speak Sesotho.*"
    assert conn.execute("SELECT language FROM shops WHERE shop_id = ?",
                        (SHOP,)).fetchone()["language"] == "st"
    msg, _ = handle_text(conn, SHOP, "bua Setswana")
    assert msg == "🗣️ *Sharp, I speak Setswana.*"
    assert conn.execute("SELECT language FROM shops WHERE shop_id = ?",
                        (SHOP,)).fetchone()["language"] == "tn"
    msg, _ = handle_text(conn, SHOP, "praat Engels")
    assert "English" in msg


def test_set_customer_language(conn, shop, ledger):
    # The command is written in English, so the confirmation is English;
    # only Sipho's future statements switch to isiXhosa.
    msg, _ = handle_text(conn, SHOP, "set Sipho language isiXhosa")
    assert msg == "✅ *Sipho's statements will be in isiXhosa.*"
    assert conn.execute("SELECT language FROM customers WHERE id = ?",
                        (ledger,)).fetchone()["language"] == "xh"
    assert set_customer_language(conn, SHOP, "Sipho", "Klingon").startswith("❓")
    assert "No one named" in set_customer_language(conn, SHOP, "Ghost", "af")


def test_scheduled_plan_uses_owner_language(conn, shop):
    from datetime import datetime as _dt
    from app.reminders import SAST as _SAST
    from app.schedule import tick
    conn.execute("UPDATE shops SET language = 'af' WHERE shop_id = ?", (SHOP,))
    conn.execute("UPDATE shops SET channel = 'vonage' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    jobs = tick(conn, _dt(2026, 10, 27, 6, 5, tzinfo=_SAST))
    assert jobs and jobs[0]["text"].startswith("☀️ *Vandag se plan")


def test_staff_cannot_change_language(conn):
    from app.agent import _STAFF_NAME
    from app.tools import ensure_shop
    owner, staff = "27710000098", "27820000099"
    ensure_shop(conn, owner, "Staff Lang Spaza")
    from app.setup import skip_setup
    skip_setup(conn, owner)
    handle_text(conn, owner, f"add staff {staff} Thandi")
    try:
        msg, _ = handle_text(conn, staff, "speak Afrikaans")
        assert msg.startswith("⛔")
        assert conn.execute("SELECT language FROM shops WHERE shop_id = ?",
                            (owner,)).fetchone()["language"] == "en"
    finally:
        _STAFF_NAME.set(None)


# --- setup in each language -------------------------------------------------


@pytest.mark.parametrize("answer,plan_mark,done_mark", [
    ("English", "Daily plan every morning", "You're all set"),
    ("isiZulu", "Icebo lansuku zonke", "Sewuhleliwe"),
    ("Afrikaans", "Dagplan elke oggend", "Jy is reg"),
    ("isiXhosa", "Icebo lemihla ngemihla", "Ulungile"),
    ("Sesotho", "Leano la tsatsi", "Leka tsena"),
    ("Setswana", "Leano la letsatsi", "Leka tse"),
])
def test_setup_in_each_language(conn, answer, plan_mark, done_mark):
    langs = ("English", "isiZulu", "Afrikaans", "isiXhosa", "Sesotho",
             "Setswana")
    shop = f"277100001{langs.index(answer):02d}"
    for text in ["hi", "Lang Spaza", "skip", "yes", "skip",
                 "1500 cash, 300 petty", answer]:
        msg, _ = handle_text(conn, shop, text)
    assert plan_mark in msg
    msg, _ = handle_text(conn, shop, "yes")
    assert done_mark in msg
    assert conn.execute("SELECT language FROM shops WHERE shop_id = ?",
                        (shop,)).fetchone()["language"] in (
                            "en", "zu", "af", "xh", "st", "tn")


# --- voice transcripts in Afrikaans and isiXhosa ----------------------------


def test_afrikaans_voice_statement(conn, shop, ledger, monkeypatch):
    monkeypatch.setattr(agent, "transcribe_audio",
                        lambda *a, **k: "stuur vir my die strokie van Sipho")
    transcript, reply, _ = handle_voice(conn, SHOP, b"fake-audio")
    assert reply.startswith("Gehoor: “stuur")
    assert "R40.00" in reply
    image = next(i for i in take_media(SHOP) if i["kind"] == "image")
    assert image["caption"] == "Stuur dit aan Sipho 👆"


def test_isixhosa_voice_statement(conn, shop, ledger, monkeypatch):
    monkeypatch.setattr(agent, "transcribe_audio",
                        lambda *a, **k: "ndicela istatimenti sikaSipho")
    _, reply, _ = handle_voice(conn, SHOP, b"fake-audio")
    assert reply.startswith("Ndivile: “ndicela")
    image = next(i for i in take_media(SHOP) if i["kind"] == "image")
    assert image["caption"] == "Mthumelele ku-Sipho 👆"


def test_afrikaans_voice_reminder(conn, shop, monkeypatch):
    import app.interpreter as interp

    def _no_model(messages):
        raise RuntimeError("no live model in tests")

    monkeypatch.setattr(interp, "propose_plan", _no_model)
    monkeypatch.setattr(agent, "transcribe_audio",
                        lambda *a, **k: "herinner my more 7vm om huur te betaal")
    _, reply, _ = handle_voice(conn, SHOP, b"fake-audio")
    assert reply.startswith("Gehoor:")
    assert "⏰ *Herinnering gestel*" in reply
    rows = conn.execute("SELECT * FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchall()
    assert len(rows) == 1 and "huur te betaal" in rows[0]["summary"]


def test_isixhosa_voice_reminder(conn, shop, monkeypatch):
    monkeypatch.setattr(agent, "transcribe_audio",
                        lambda *a, **k: "ndikhumbuze ngomso ukuhlawula irenti, enkosi")
    _, reply, _ = handle_voice(conn, SHOP, b"fake-audio")
    assert reply.startswith("Ndivile:")
    assert "⏰ *Isikhumbuzo simiselwe*" in reply
    rows = conn.execute("SELECT * FROM reminders WHERE shop_id = ?",
                        (SHOP,)).fetchall()
    assert len(rows) == 1


# --- reminder NLU in Afrikaans and isiXhosa ---------------------------------


def test_afrikaans_reminder_request():
    parsed = parse_request("herinner my more 7vm om huur te betaal", MON_10AM)
    assert parsed["due"] == datetime(2026, 10, 27, 7, 0, tzinfo=SAST)
    assert parsed["due"].utcoffset().total_seconds() == 7200
    parsed = parse_request("herinner my elke maandag 7vm om huur te betaal",
                           MON_10AM)
    assert parsed["recurrence"] == "weekly"
    assert parsed["due"] == datetime(2026, 11, 2, 7, 0, tzinfo=SAST)
    assert parse_request("herinner my elke maand om huur te betaal",
                         MON_10AM)["recurrence"] == "monthly"


def test_isixhosa_reminder_request():
    parsed = parse_request("ndikhumbuze ngomso ukuhlawula irenti", MON_10AM)
    assert parsed["due"] == datetime(2026, 10, 27, 7, 0, tzinfo=SAST)
    assert "ukuhlawula" in parsed["summary"]


def test_reminder_lists_in_owner_language(conn, shop):
    from app.reminders import save_reminder
    save_reminder(conn, SHOP, datetime(2026, 10, 27, 7, 0, tzinfo=SAST),
                  "betaal huur", None)
    conn.execute("UPDATE shops SET language = 'af' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    msg, _ = handle_text(conn, SHOP, "my herinneringe")
    assert msg.startswith("⏰ *My herinneringe*")


# --- translated documents ---------------------------------------------------


def test_statement_card_uses_customer_language(conn, shop, ledger):
    set_customer_language(conn, SHOP, "Sipho", "isiXhosa")
    CURRENT_LANG.set("en")
    msg = customer_statement(conn, SHOP, "Sipho")
    assert "Outstanding R40.00" in msg  # owner text stays in the message lang
    pdf = _pdf_bytes(conn)
    assert "Enkosi".encode() in pdf
    assert "Istatimenti".encode() in pdf


def test_statement_card_falls_back_to_owner_language(conn, shop, ledger):
    conn.execute("UPDATE shops SET language = 'af' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    CURRENT_LANG.set("en")
    customer_statement(conn, SHOP, "Sipho")
    assert "Dankie".encode() in _pdf_bytes(conn)


def test_month_end_pdf_uses_owner_language(conn, shop, monkeypatch):
    import calendar
    from app.db import today_str
    conn.execute("UPDATE shops SET language = 'af' WHERE shop_id = ?", (SHOP,))
    conn.commit()
    msg, _ = handle_text(conn, SHOP, "month-end report")
    assert msg.startswith("📊 *Month-end:")  # WhatsApp text follows the message
    row = reports.get_report(conn, SHOP, reports.default_month())
    pdf = open(row["pdf_path"], "rb").read()
    assert b"Wins" in pdf  # …but the filed PDF follows the owner
    assert "Oktober".encode() in pdf or "November".encode() in pdf
    filed = reports.default_month()
    assert calendar.month_name[int(filed[5:7])].encode() in msg.encode()


def test_compliance_per_language(conn, shop):
    CURRENT_LANG.set("af")
    assert compliance_guide(conn, SHOP).startswith("🏛️ *Spaza-nakoming*")
    assert "spazashopfund.co.za" in compliance_guide(conn, SHOP, "fund")
    CURRENT_LANG.set("xh")
    assert "GRATIS" not in compliance_guide(conn, SHOP, "fund")
    assert "ISIMAHLA" in compliance_guide(conn, SHOP, "fund")
    CURRENT_LANG.set("st")
    assert "spazashopfund.co.za" in compliance_guide(conn, SHOP, "fund")
    assert "MAHALA" in compliance_guide(conn, SHOP, "fund")
    CURRENT_LANG.set("tn")
    assert "spazashopfund.co.za" in compliance_guide(conn, SHOP, "fund")
    assert "MAHALA" in compliance_guide(conn, SHOP, "fund")


# --- cross-language matching ------------------------------------------------


def test_item_words_match_in_all_languages(conn, shop):
    conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, stock_qty)"
        " VALUES (?, 'Bread', 2000, 10), (?, 'Sugar 2.5kg', 5000, 4)",
        (SHOP, SHOP))
    conn.commit()
    for word, want in [("isonka", "Bread"), ("brood", "Bread"),
                       ("bohobe", "Bread"),
                       ("isinkwa", "Bread"), ("iswekile", "Sugar 2.5kg"),
                       ("tshwekere", "Sugar 2.5kg"),
                       ("suiker", "Sugar 2.5kg"), ("ushukela", "Sugar 2.5kg")]:
        prod, _ = _find_products(conn, SHOP, word)
        assert prod is not None and prod["name"] == want, word


def test_month_names_all_languages():
    assert parse_month_name("usepthemba") == parse_month_name("september")
    assert parse_month_name("septemba") == parse_month_name("sep")
    assert parse_month_name("oktober") == parse_month_name("october")
    assert parse_month_name("u-oktobha") == parse_month_name("oct")
    assert parse_month_name("motsheanong") == parse_month_name("may")
    assert parse_month_name("motsheganong") == parse_month_name("may")
    assert parse_month_name("pherekgong") == parse_month_name("jan")
    assert parse_month_name("sedimonthole") == parse_month_name("dec")
