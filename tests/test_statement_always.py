"""Statement images must always arrive: text/voice triggers, image-first."""

import pytest

from app import reports
from app.agent import MEDIA_OUTBOX, handle_text, handle_voice, take_media
from app.agent import try_statement_request
from app.tools import ensure_shop


SHOP = "27710000021"
VOICE_TRANSCRIPT = ("Sipho owes me money, give me a receipt"
                    " so I can send it to him")


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(reports, "LOGOS_DIR", tmp_path / "logos")
    monkeypatch.setenv("SISI_PUBLIC_URL", "https://x.example")
    MEDIA_OUTBOX.clear()


@pytest.fixture()
def ledger(conn):
    ensure_shop(conn, SHOP, "Always Spaza")
    bread = conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, cost_cents, stock_qty)"
        " VALUES (?, 'Bread', 2000, 1500, 50)", (SHOP,)).lastrowid
    sipho = conn.execute("INSERT INTO customers (shop_id, name) VALUES (?, 'Sipho')",
                         (SHOP,)).lastrowid
    conn.execute(
        "INSERT INTO sales (shop_id, product_id, product_name, qty, total_cents,"
        " customer_id, action_id, created_at) VALUES (?, ?, 'Bread', 2, 4000, ?, ?, ?)",
        (SHOP, bread, sipho, "alw-sale-0", "2026-10-05T10:00:00.000Z"))
    conn.execute(
        "INSERT INTO credit_entries (shop_id, customer_id, amount_cents,"
        " items_text, action_id, created_at) VALUES (?, ?, 4000, 'Bread', ?, ?)",
        (SHOP, sipho, "alw-ce-0", "2026-10-05T10:00:00.000Z"))
    conn.commit()
    return sipho


def _image_for(conn):
    items = take_media(SHOP)
    images = [i for i in items if i["kind"] == "image"]
    assert len(images) == 1
    return images[0]


@pytest.mark.parametrize("text", [
    "statement for Sipho",
    "receipt for Sipho",
    "Sipho's statement",
    "give me a receipt for Sipho",
    "what does Sipho owe, make a picture",
    "send it to Sipho",
    "something to send Sipho",
    "ngicela isitatimende sikaSipho",
    VOICE_TRANSCRIPT,
])
def test_text_triggers_produce_statement_image(conn, ledger, text):
    msg, buttons = handle_text(conn, SHOP, text)
    assert buttons == ["All statements", "Set reminder", "Menu"]
    assert "R40.00" in msg  # one short text with the total
    image = _image_for(conn)
    # The forward-caption is an instruction to the owner: same language as
    # the request that triggered it.
    expected = ("Mthumelele u-Sipho 👆" if "isitatimende" in text
                else "Forward this to Sipho 👆")
    assert image["caption"] == expected
    assert image["url"].startswith("https://x.example/media/")
    assert image["url"].endswith("?f=png")
    assert image.get("first") is True


def test_voice_regression_exact_transcript(conn, ledger, monkeypatch):
    import app.agent as agent
    monkeypatch.setattr(agent, "transcribe_audio",
                        lambda *a, **k: VOICE_TRANSCRIPT)
    transcript, reply, buttons = handle_voice(conn, SHOP, b"fake-audio")
    assert transcript == VOICE_TRANSCRIPT
    assert reply.startswith("Heard:")
    assert "R40.00" in reply
    image = _image_for(conn)
    assert image["caption"] == "Forward this to Sipho 👆"


def test_non_statement_guards(conn, ledger):
    assert try_statement_request(conn, SHOP, "show my receipts this month") is None
    assert try_statement_request(conn, SHOP, "Sipho paid R50") is None
    assert try_statement_request(conn, SHOP, "how much does sipho owe?") is None
    assert try_statement_request(conn, SHOP, "send it") is None


# --- sale + statement multi-intent -------------------------------------------
# Live bug (30 Oct 2026): "sipho also took one litre milk, one Marie
# biscuits and two Chappies, add that to his account and send his
# statement" rendered ONLY the statement — the fast path swallowed the
# three sale items. Sale verbs and quantities now force planner routing.


def _script(monkeypatch, tmp_path, plans):
    import json
    from types import SimpleNamespace

    import app.interpreter as interp
    import app.llm as llm
    monkeypatch.setattr(llm, "TOKEN_LOG", tmp_path / "usage.log")
    calls = {"n": 0}

    def fake(messages):
        calls["n"] += 1
        body = plans[min(calls["n"] - 1, len(plans) - 1)]
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2))

    monkeypatch.setattr(interp, "propose_plan", fake)
    return calls


@pytest.mark.parametrize("text", [
    "sipho also took one litre milk one marie biscuits and two chappies"
    " please also add that to his account and send me his statement",
    "Sipho took 2 bread, send his statement",
    "add 2 bread to Sipho's account and send his statement",
    "2 bread on Sipho's account, send his statement",
    "Sipho bought milk, and his statement please",
    "put one bread on the book and send Sipho's statement",
    "uSipho uthathe ubisi, ngicela isitatimende sakhe",
])
def test_sale_plus_statement_is_multi(text):
    from app.agent import _looks_multi
    assert _looks_multi(text) is True, text


@pytest.mark.parametrize("text", [
    "statement for Sipho",
    "receipt for Sipho",
    "Sipho's statement",
    "give me a receipt for Sipho",
    "what does Sipho owe, make a picture",
    "send it to Sipho",
    "something to send Sipho",
    "ngicela isitatimende sikaSipho",
    VOICE_TRANSCRIPT,
    "statements for 4 creditors",
    "state vir 4 debiteure",
    "statements for 2 creditors",
    "statements for 9 creditors",
    "izitatimende zabantu abayi-4 abakweletayo",
    "statements for everyone who owes",
    "all statements",
    "receipts for Sipho and Thandi",
    "add up Sipho's statement",
    "statement for Sipho, 2 pages",
    "Sipho owes R200, send the receipt",
])
def test_pure_statements_stay_single(text):
    from app.agent import _looks_multi
    assert _looks_multi(text) is False, text


def test_sale_plus_statement_reaches_planner(conn, ledger, monkeypatch,
                                            tmp_path):
    conn.execute(
        "INSERT INTO products (shop_id, name, price_cents, cost_cents, stock_qty)"
        " VALUES (?, 'Milk', 2800, 2200, 30)", (SHOP,))
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?",
                 (SHOP,))
    conn.commit()
    calls = _script(monkeypatch, tmp_path, [{
        "understood": "sale plus statement", "question": None, "options": [],
        "tasks": [
            {"kind": "sale", "args": {
                "buyer": "Sipho",
                "items": [{"product": "Bread", "qty": 2},
                          {"product": "Milk", "qty": 1}]}, "quote": ""},
            {"kind": "statement", "args": {"customer": "Sipho"},
             "quote": ""}]}])
    msg, buttons = handle_text(
        conn, SHOP, "sipho took two bread and one milk, add that to his"
        " account and send me his statement")
    assert calls["n"] == 1  # planner ran; fast path did not hijack
    assert "Save sale? (2 items)" in msg  # every item proposed ...
    assert "I'll send that once you confirm" in msg  # ... statement held
    assert "Outstanding" not in msg
    assert buttons == ["Yes", "No"]
    assert take_media(SHOP) == []
    msg, _ = handle_text(conn, SHOP, "yes")
    assert "Sale saved" in msg and "Outstanding R108.00" in msg
    assert take_media(SHOP) != []


def test_image_sent_before_text(conn, monkeypatch):
    import app.main as main
    from app.main import _handle_message
    from app.whatsapp import FakeSender, IncomingMessage
    monkeypatch.setattr(main, "handle_text",
                        lambda c, s, t, sent_at=None: ("the total is R50.00", None))
    MEDIA_OUTBOX[SHOP] = [
        {"kind": "image", "url": "https://x.example/media/a?f=png",
         "caption": "Forward this to Sipho 👆", "filename": "",
         "first": True},
        {"kind": "file", "url": "https://x.example/media/b?f=pdf",
         "caption": "Statement PDF", "filename": "statement-sipho.pdf"},
    ]
    sender = FakeSender()
    _handle_message(sender, IncomingMessage("mid-st1", SHOP, "text",
                                            "statement for Sipho", ""))
    sent = sender.pop_shop(SHOP)
    assert [m["type"] for m in sent] == ["text", "image", "text", "file"]
    assert sent[0]["body"].startswith("⏳")  # status line first
    assert sent[1]["body"] == "Forward this to Sipho 👆"  # then the image
    assert sent[2]["body"].startswith("the total is R50.00")
