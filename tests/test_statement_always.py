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
