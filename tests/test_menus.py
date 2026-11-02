"""Menus, interactive lists/buttons, numbered fallback (offline)."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

import app.llm as llm
import app.vonage as vonage
from app import agent
from app.agent import LIST_OUTBOX, MENU_ROWS
from app.sendqueue import RateLimited
from app.vonage import VonageSender, _numbered
from app.whatsapp import CloudApiSender, FakeSender
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID
FRESH = "27710000099"


def _plan_body(tasks=(), question=None, options=(), understood="menu test"):
    return {"understood": understood, "tasks": list(tasks),
            "question": question, "options": list(options)}


def _chat_reply(text):
    return _plan_body([{"kind": "chat", "args": {"reply": text},
                        "quote": ""}])


def _stub(monkeypatch, tmp_path, script):
    import app.interpreter as interp
    monkeypatch.setattr(llm, "TOKEN_LOG", tmp_path / "usage.log")
    calls = {"n": 0, "messages": []}

    def fake(messages):
        calls["n"] += 1
        calls["messages"] = messages
        item = script[min(calls["n"] - 1, len(script) - 1)]
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps(item)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2),
        )

    monkeypatch.setattr(interp, "propose_plan", fake)
    return calls


def _user_texts(calls):
    return [m["content"] for m in calls["messages"]
            if isinstance(m, dict) and m.get("role") == "user"]


@pytest.fixture()
def vg(monkeypatch):
    monkeypatch.setenv("VONAGE_API_KEY", "k")
    monkeypatch.setenv("VONAGE_API_SECRET", "s")


class _ScriptPost:
    """httpx.post script: responses or exceptions in order."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, url, auth=None, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _ok():
    return SimpleNamespace(status_code=202, text="{}",
                           raise_for_status=lambda: None,
                           json=lambda: {"message_uuid": "out-1"})


def _http_error(status):
    resp = SimpleNamespace(status_code=status, headers={}, text="err")
    return httpx.HTTPStatusError(f"http {status}", request=None,
                                 response=resp)


# menu ----------------------------------------------------------------------

def _use_english(conn):
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?",
                 (SHOP,))
    conn.commit()


def test_menu_queues_list_with_seven_rows(conn):
    seed_demo(SHOP, conn)
    _use_english(conn)
    msg, buttons = agent.handle_text(conn, SHOP, "menu")
    assert msg == "📋 *Main menu*\nPick one below 👇"
    assert buttons is None
    assert LIST_OUTBOX[SHOP][0]["rows"] == MENU_ROWS
    assert LIST_OUTBOX[SHOP][0]["button_text"] == "Choose"
    assert len(MENU_ROWS) == 7
    assert all(len(r) <= 24 for r in MENU_ROWS)


def test_hi_opens_menu_for_known_shop(conn):
    seed_demo(SHOP, conn)
    _use_english(conn)
    msg, _ = agent.handle_text(conn, SHOP, "hi")
    assert "Main menu" in msg
    assert LIST_OUTBOX[SHOP]


def test_hi_starts_setup_for_new_number(conn):
    msg, _ = agent.handle_text(conn, FRESH, "hi")
    assert "Step 1" in msg
    assert FRESH not in LIST_OUTBOX


def test_help_opens_menu(conn):
    seed_demo(SHOP, conn)
    _use_english(conn)
    msg, _ = agent.handle_text(conn, SHOP, "help")
    assert msg == "📋 *Main menu*\nPick one below 👇"
    assert LIST_OUTBOX[SHOP][0]["rows"] == [
        "Sell", "Stock", "Who owes me", "Statements", "Reports",
        "Reminders", "Settings"]


def test_help_mid_setup_keeps_text_and_resumes(conn):
    agent.handle_text(conn, FRESH, "hi")
    agent.handle_text(conn, FRESH, "Shop")
    agent.handle_text(conn, FRESH, "Addr")
    msg, _ = agent.handle_text(conn, FRESH, "help")
    assert "Demo help" in msg
    assert FRESH not in LIST_OUTBOX
    msg, _ = agent.handle_text(conn, FRESH, "yes")
    assert msg.startswith("Let's finish setting up, step 4 of 7")


@pytest.mark.parametrize("word", ["help", "what can you do", "commands",
                                  "hulp", "usizo", "uncedo"])
def test_help_words_open_menu(conn, word):
    seed_demo(SHOP, conn)
    _use_english(conn)
    msg, _ = agent.handle_text(conn, SHOP, word)
    assert "📋" in msg
    assert LIST_OUTBOX[SHOP][0]["rows"] == agent.menu_display_rows()
    assert len(LIST_OUTBOX[SHOP][0]["rows"]) == 7


def test_demo_help_keeps_full_text(conn):
    seed_demo(SHOP, conn)
    _use_english(conn)
    msg, _ = agent.handle_text(conn, SHOP, "demo help")
    assert msg.startswith("❓ *Demo help")
    assert SHOP not in LIST_OUTBOX


def test_menu_intro_follows_owner_language(conn):
    seed_demo(SHOP, conn)
    conn.execute("UPDATE shops SET language = 'zu' WHERE shop_id = ?",
                 (SHOP,))
    conn.commit()
    msg, _ = agent.handle_text(conn, SHOP, "menu")
    assert "Imenyu enkulu" in msg
    assert LIST_OUTBOX[SHOP][0]["button_text"] == "Khetha"


# numbered taps --------------------------------------------------------------

def test_numbered_tap_resolves_to_row_text(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    agent.handle_text(conn, SHOP, "menu")
    calls = _stub(monkeypatch, tmp_path, [_chat_reply("stock here")])
    msg, _ = agent.handle_text(conn, SHOP, "2")
    assert _user_texts(calls)[-1].split("\n")[1] == "Stock"
    assert msg == "stock here"


def test_numbered_debtors_tap_answered_fast(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    _use_english(conn)
    agent.handle_text(conn, SHOP, "menu")
    calls = _stub(monkeypatch, tmp_path, [_chat_reply("must not be called")])
    msg, _ = agent.handle_text(conn, SHOP, "3")
    assert calls["n"] == 0  # "Who owes me" skips the planner
    assert "On the book" in msg


def test_numbered_yes_confirms_pending(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _stub(monkeypatch, tmp_path, [_plan_body(
        [{"kind": "sale",
          "args": {"items": [{"product": "Sasko White Bread 700g",
                               "qty": 2}]},
          "quote": "sell 2 bread"}])])
    agent.handle_text(conn, SHOP, "sell 2 bread then cash up")
    assert calls["n"] == 1
    from app.tools import get_pending
    assert get_pending(conn, SHOP) is not None
    msg, _ = agent.handle_text(conn, SHOP, "1")
    assert get_pending(conn, SHOP) is None
    assert msg.strip()


def test_numbered_no_cancels_pending(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    calls = _stub(monkeypatch, tmp_path, [_plan_body(
        [{"kind": "sale",
          "args": {"items": [{"product": "Sasko White Bread 700g",
                               "qty": 2}]},
          "quote": "sell 2 bread"}])])
    agent.handle_text(conn, SHOP, "sell 2 bread then cash up")
    assert calls["n"] == 1
    from app.tools import get_pending
    msg, _ = agent.handle_text(conn, SHOP, "2")
    assert get_pending(conn, SHOP) is None
    assert msg.strip()


def test_stale_options_leave_digit_alone(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    agent.handle_text(conn, SHOP, "menu")
    old = (datetime.now(timezone.utc) - timedelta(minutes=11)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")
    conn.execute("UPDATE flags SET value = ? WHERE shop_id = ? AND key = 'last-options'",
                 (json.dumps({"options": MENU_ROWS, "ts": old}), SHOP))
    conn.commit()
    calls = _stub(monkeypatch, tmp_path, [_chat_reply("huh")])
    agent.handle_text(conn, SHOP, "3")
    assert _user_texts(calls)[-1].split("\n")[1] == "3"


def test_digits_stay_data_during_setup(conn):
    agent.handle_text(conn, FRESH, "hi")
    agent._store_options(conn, FRESH, ["✅ Yes", "❌ No"])
    assert agent._resolve_numbered(conn, FRESH, "5") == "5"
    msg, _ = agent.handle_text(conn, FRESH, "5")
    assert "Step 1" in msg  # still answering step 1, not a tap


def test_digit_kept_for_reminder_offer(conn):
    seed_demo(SHOP, conn)
    from app.tools import propose_reminder_offer
    propose_reminder_offer(conn, SHOP, "2026-10-29T05:30:00",
                           "Take order to the shop")
    agent._store_options(conn, SHOP, ["✅ Yes", "❌ No"])
    assert agent._resolve_numbered(conn, SHOP, "6") == "6"


# vonage interactive + fallback -----------------------------------------------

def test_buttons_custom_payload_shape(vg, monkeypatch):
    fake = _ScriptPost([_ok()])
    monkeypatch.setattr(vonage.httpx, "post", fake)
    VonageSender().send_buttons(SHOP, "Save?", ["✅ Yes", "❌ No"])
    payload = fake.calls[0]["json"]
    assert payload["message_type"] == "custom"
    assert payload["channel"] == "whatsapp"
    node = payload["custom"]["interactive"]
    assert node["type"] == "button"
    assert [b["reply"]["title"] for b in node["action"]["buttons"]] == ["✅ Yes", "❌ No"]


def test_buttons_fall_back_to_numbered_on_400(vg, monkeypatch):
    fake = _ScriptPost([_http_error(400), _ok()])
    monkeypatch.setattr(vonage.httpx, "post", fake)
    VonageSender().send_buttons(SHOP, "Save?", ["✅ Yes", "❌ No"])
    assert len(fake.calls) == 2
    fallback = fake.calls[1]["json"]
    assert fallback["message_type"] == "text"
    assert "1) ✅ Yes" in fallback["text"] and "2) ❌ No" in fallback["text"]


def test_buttons_reraise_rate_limited_without_fallback(vg, monkeypatch):
    fake = _ScriptPost([_http_error(429)])
    monkeypatch.setattr(vonage.httpx, "post", fake)
    with pytest.raises(RateLimited):
        VonageSender().send_buttons(SHOP, "Save?", ["✅ Yes", "❌ No"])
    assert len(fake.calls) == 1  # queued for retry, no fallback text


def test_list_custom_payload_shape(vg, monkeypatch):
    fake = _ScriptPost([_ok()])
    monkeypatch.setattr(vonage.httpx, "post", fake)
    VonageSender().send_list(SHOP, "Pick one", "Choose", MENU_ROWS)
    payload = fake.calls[0]["json"]
    assert payload["message_type"] == "custom"
    node = payload["custom"]["interactive"]
    assert node["type"] == "list"
    assert node["action"]["button"] == "Choose"
    rows = node["action"]["sections"][0]["rows"]
    assert [r["title"] for r in rows] == MENU_ROWS


def test_list_falls_back_to_numbered(vg, monkeypatch):
    fake = _ScriptPost([_http_error(422), _ok()])
    monkeypatch.setattr(vonage.httpx, "post", fake)
    VonageSender().send_list(SHOP, "Pick one", "Choose", MENU_ROWS)
    fallback = fake.calls[1]["json"]
    assert fallback["message_type"] == "text"
    assert "1) Sell" in fallback["text"]
    assert "7) Settings" in fallback["text"]


def test_numbered_skips_body_with_numbers():
    assert _numbered("❓ Which?\n1) A\n2) B", ["A", "B"]).count("1)") == 1


# contextual buttons -----------------------------------------------------------

def test_statement_returns_next_step_buttons(conn):
    seed_demo(SHOP, conn)
    _use_english(conn)
    msg, buttons = agent.handle_text(conn, SHOP, "statement for Sipho")
    assert "Sipho" in msg
    assert buttons == ["📄 All statements", "⏰ Set reminder", "📋 Menu"]


def test_reminders_return_next_step_buttons(conn):
    seed_demo(SHOP, conn)
    _use_english(conn)
    _, buttons = agent.handle_text(conn, SHOP, "my reminders")
    assert buttons == ["⏰ Set reminder", "📋 Menu"]


def test_order_returns_next_step_buttons(conn):
    seed_demo(SHOP, conn)
    _use_english(conn)
    _, buttons = agent.handle_text(conn, SHOP, "order stock")
    assert buttons == ["📤 Send it", "📋 Menu"]


def test_button_tap_matches_typed_text(conn):
    seed_demo(SHOP, conn)
    payload = {"from": {"number": SHOP, "type": "whatsapp"},
               "to": {"number": "14157386102", "type": "whatsapp"},
               "message_uuid": "vg-btn-1", "timestamp": "2026-10-27T00:00:00Z",
               "message": {"message_type": "reply",
                           "reply": {"id": "Menu", "title": "Menu"}}}
    msgs = vonage.parse_vonage_webhook(payload)
    assert msgs[0].kind == "button" and msgs[0].text == "Menu"
    tapped, _ = agent.handle_text(conn, SHOP, msgs[0].text)
    typed, _ = agent.handle_text(conn, SHOP, "menu")
    assert tapped == typed


# senders, queue, drain ---------------------------------------------------------

def test_fake_sender_records_lists():
    sender = FakeSender()
    sender.send_list(SHOP, "Pick", "Choose", ["A", "B"])
    assert sender.sent[0]["type"] == "list"
    assert sender.sent[0]["rows"] == ["A", "B"]


def test_cloud_sender_list_shape(monkeypatch):
    seen = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["json"] = json
        return FakeResp()

    import app.whatsapp as wa
    monkeypatch.setattr(wa.httpx, "post", fake_post)
    CloudApiSender(token="tok", phone_number_id="pnid").send_list(
        SHOP, "Pick", "Choose", ["A", "B"])
    node = seen["json"]["interactive"]
    assert seen["json"]["type"] == "interactive"
    assert node["type"] == "list"
    assert node["action"]["button"] == "Choose"


def test_queued_list_drains_through_sender(conn):
    from app.sendqueue import QueuedSender, _send_claimed
    seed_demo(SHOP, conn)
    fake = FakeSender()
    out = QueuedSender.wrap(fake, conn, "vonage")
    out.send_list(SHOP, "Pick", "Choose", ["A"])
    row = conn.execute("SELECT kind, payload_json FROM send_queue").fetchone()
    assert row["kind"] == "list"
    payload = json.loads(row["payload_json"])
    assert payload["rows"] == ["A"]
    _send_claimed(fake, "list", payload, SHOP)
    assert fake.sent[0]["type"] == "list"


def test_process_message_delivers_menu_list(monkeypatch, tmp_path):
    import app.db as db
    import app.main as main
    from app.whatsapp import IncomingMessage, get_sender, reset_fake
    monkeypatch.setenv("SISI_DB", str(tmp_path / "m.db"))
    reset_fake()
    seed_demo(SHOP)
    conn = db.connect()
    try:
        db.init_db(conn)
        conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?",
                     (SHOP,))
        conn.commit()
    finally:
        conn.close()
    main.process_message(IncomingMessage("mid-menu", SHOP, "text", "menu",
                                         ""))
    sent = get_sender().pop_shop(SHOP)
    assert [m["type"] for m in sent] == ["text", "list"]
    assert sent[1]["rows"] == MENU_ROWS


def test_every_row_tap_reaches_router_as_typed(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    _use_english(conn)
    heard = []

    import app.interpreter as interp

    def fake(messages):
        user = [m["content"] for m in messages
                if isinstance(m, dict) and m.get("role") == "user"][-1]
        heard.append(user.split("\n")[1])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps(_chat_reply("ok"))))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2),
        )

    monkeypatch.setattr(llm, "TOKEN_LOG", tmp_path / "usage.log")
    monkeypatch.setattr(interp, "propose_plan", fake)
    agent.handle_text(conn, SHOP, "menu")
    for i, row in enumerate(MENU_ROWS, 1):
        agent.handle_text(conn, SHOP, "menu")
        agent.handle_text(conn, SHOP, str(i))
    # "Reminders" is a keyword and "Who owes me" is a fast lookup
    # (no planner calls); the rest arrive verbatim.
    assert heard == [r for r in MENU_ROWS
                     if r not in ("Reminders", "Who owes me")]


def test_unmatched_message_carries_shop_context(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    _use_english(conn)
    calls = _stub(monkeypatch, tmp_path, [_chat_reply("slow day")])
    agent.handle_text(conn, SHOP, "so how is business today")
    assert calls["n"] == 1
    user = _user_texts(calls)[-1]
    assert "Sasko White Bread 700g" in user
    assert "Sipho" in user


def test_unmatched_clarify_returns_buttons(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    _use_english(conn)
    calls = _stub(monkeypatch, tmp_path, [
        _plan_body([], question="Which bread?",
                   options=["White", "Brown"]),
    ])
    msg, buttons = agent.handle_text(conn, SHOP, "sell")
    assert calls["n"] == 1
    assert buttons == ["White", "Brown"]
    assert "Which bread?" in msg


def test_voice_composite_routes_to_planner(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    _use_english(conn)
    monkeypatch.setattr(agent, "transcribe_audio",
                        lambda *a, **k: "who owes me most and remind them")
    calls = _stub(monkeypatch, tmp_path, [_plan_body(
        [{"kind": "debtors", "args": {}, "quote": "who owes me most"},
         {"kind": "draft", "args": {"customer": "Jabu"},
          "quote": "remind them"}])])
    transcript, reply, _ = agent.handle_voice(conn, SHOP, b"ogg-bytes")
    assert transcript == "who owes me most and remind them"
    assert calls["n"] == 1
    assert reply.startswith("Heard:")
    assert "Jabu" in reply and "R120.00" in reply


# native interactive buttons/lists, localized ---------------------------------

def test_yes_no_buttons_have_emoji_and_fit_cap(conn):
    seed_demo(SHOP, conn)
    _use_english(conn)
    agent.handle_text(conn, SHOP, "my reminders")
    assert agent.yes_no_buttons() == ["✅ Yes", "❌ No"]
    assert all(len(b) <= 20 for b in agent.yes_no_buttons())


def test_yes_no_buttons_localized_all_langs(conn):
    from app.i18n import CURRENT_LANG, LANGS, STRINGS
    seed_demo(SHOP, conn)
    seen = set()
    for lang in LANGS:
        CURRENT_LANG.set(lang)
        pair = tuple(agent.yes_no_buttons())
        assert pair == (STRINGS[lang]["btn_yes"], STRINGS[lang]["btn_no"])
        assert all(len(b) <= 20 for b in pair)
        seen.add(pair)
    assert len(seen) == 6  # every language has its own titles


def test_menu_rows_localized_all_langs(conn):
    from app.i18n import CURRENT_LANG, LANGS, STRINGS
    seed_demo(SHOP, conn)
    for lang in LANGS:
        CURRENT_LANG.set(lang)
        rows = agent.menu_display_rows()
        assert len(rows) == 7
        assert all(len(r) <= 24 for r in rows)
        assert rows == [STRINGS[lang][f"menu_row_{k}"] for k in
                        ("sell", "stock", "owes", "statements", "reports",
                         "reminders", "settings")]
    CURRENT_LANG.set("en")


def test_emoji_yes_tap_confirms_pending(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    _use_english(conn)
    calls = _stub(monkeypatch, tmp_path, [_plan_body(
        [{"kind": "sale",
          "args": {"items": [{"product": "Sasko White Bread 700g",
                               "qty": 2}]},
          "quote": "sell 2 bread"}])])
    agent.handle_text(conn, SHOP, "sell 2 bread then cash up")
    assert calls["n"] == 1
    from app.tools import get_pending
    msg, _ = agent.handle_text(conn, SHOP, "✅ Yes")
    assert get_pending(conn, SHOP) is None
    assert msg.strip()


def test_localized_menu_tap_reaches_router(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    _use_english(conn)
    heard = []
    import app.interpreter as interp

    def fake(messages):
        user = [m["content"] for m in messages
                if isinstance(m, dict) and m.get("role") == "user"][-1]
        heard.append(user.split("\n")[1])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps(_chat_reply("ok"))))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2),
        )

    monkeypatch.setattr(llm, "TOKEN_LOG", tmp_path / "usage.log")
    monkeypatch.setattr(interp, "propose_plan", fake)
    agent.handle_text(conn, SHOP, "menu")
    assert agent.canonicalize_ui("Dayisa") == "Sell"
    # a localized row tap reaches the planner as the canonical English
    agent.handle_text(conn, SHOP, "Dayisa")
    assert heard[-1] == "Sell"


def test_localized_after_button_tap_maps_to_canonical():
    assert agent.canonicalize_ui("📤 Thumela") == "Send it"
    assert agent.canonicalize_ui("📋 Imenyu") == "menu"
    assert agent.canonicalize_ui("🔗 Xhumanisa") == "link"
    assert agent.canonicalize_ui("🆕 Entsha") == "new"


def test_many_options_become_list_not_buttons(conn):
    seed_demo(SHOP, conn)
    _use_english(conn)
    # the planner caps at 3 options; longer choice lists (book which-one,
    # clarify with many candidates) ride a queued list instead of buttons
    msg, buttons = agent.coerce_options(
        conn, SHOP, "Which one?", ["A", "B", "C", "D", "E"])
    assert buttons is None  # >3 options ride a queued list instead
    assert agent.LIST_OUTBOX[SHOP]
    assert agent.LIST_OUTBOX[SHOP][0]["rows"] == ["A", "B", "C", "D", "E"]
    assert "Which one?" in msg
    # numbered fallback still resolves against the stored list
    assert agent._resolve_numbered(conn, SHOP, "4") == "D"
    msg2, buttons2 = agent.coerce_options(conn, SHOP, "Which?", ["A", "B"])
    assert buttons2 == ["A", "B"]
    assert agent._resolve_numbered(conn, SHOP, "2") == "B"
    assert msg2 == "Which?"


def test_few_options_stay_buttons(conn, monkeypatch, tmp_path):
    seed_demo(SHOP, conn)
    _use_english(conn)
    calls = _stub(monkeypatch, tmp_path, [
        _plan_body([], question="Which bread?",
                   options=["White", "Brown"]),
    ])
    msg, buttons = agent.handle_text(conn, SHOP, "sell")
    assert calls["n"] == 1
    assert buttons == ["White", "Brown"]
    assert SHOP not in agent.LIST_OUTBOX
    assert "Which bread?" in msg
