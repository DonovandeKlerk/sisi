"""Photo stock intake: products + caption -> one editable preview.

A product photo with an intake caption must reach the stock-intake
workflow (never the legacy 'can't tell what this is' rejection):
vision extracts candidate products with uncertainty, the planner
emits stock_add, and code matches inventory, asks for missing prices,
and saves only on Yes. No payment is posted without a stated cost.
"""

import json
from types import SimpleNamespace

import pytest

import app.interpreter as interp
from app import agent
from app.db import init_db
from app.setup import skip_setup
from app.tools import (account_balance, get_pending, propose_stock_intake,
                       record_photo)
from seed.seed_demo import DEMO_SHOP_ID, seed_demo

SHOP = DEMO_SHOP_ID

CAPTION = ("i bought this... please add it to our stock and add relevant "
           "prices.. if you don't have a price for an item.. ask me")


def _prod(brand, variant="", size="", count=None, readable=True, notes=""):
    return {"brand": brand, "variant": variant, "size": size,
            "visible_count": count, "readable": readable, "notes": notes}


def _doc(products):
    return {"doc_type": "other",
            "expiry": {"stamp_count": 0, "items": []},
            "book": {"date": "", "page_total_cents": None, "crossed_out": 0,
                     "lines": []},
            "receipt": {"supplier": "", "date": "", "receipt_no": "",
                        "lines": [], "vat_cents": None, "total_cents": None,
                        "payment": ""},
            "products": products}


@pytest.fixture()
def shop(conn):
    seed_demo(SHOP, conn)
    skip_setup(conn, SHOP)
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?",
                 (SHOP,))
    conn.commit()
    return SHOP


def _stub(monkeypatch, tasks, question=None, understood="intake test"):
    def fake(messages):
        body = {"understood": understood, "tasks": tasks,
                "question": question, "options": []}
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  total_tokens=2))

    monkeypatch.setattr(interp, "propose_plan", fake)


def _stock_add(*names):
    return [{"kind": "stock_add",
             "args": {"items": [{"product": n} for n in names]},
             "quote": "add to stock"}]


def _photo(monkeypatch, products, caption=CAPTION):
    monkeypatch.setattr(agent, "read_shop_photo",
                        lambda image, shop="": _doc(products))
    return caption


# vision evidence ------------------------------------------------------------

def test_vision_schema_requests_products():
    import app.vision as vision
    assert "PRODUCTS" in vision.SHOP_PHOTO_PROMPT
    schema = vision.SHOP_PHOTO_SCHEMA["schema"]
    assert "products" in schema["required"]
    props = schema["properties"]["products"]["items"]["properties"]
    assert sorted(props) == ["brand", "notes", "readable", "size",
                             "variant", "visible_count"]


def test_vision_parses_products_with_uncertainty(monkeypatch):
    import app.vision as vision

    def fake(messages, response_format):
        assert "PRODUCTS" in messages[0]["content"][0]["text"]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(
                {"doc_type": "other",
                 "expiry": {"stamp_count": 0, "items": []},
                 "book": {"date": "", "page_total_cents": None,
                          "crossed_out": 0, "lines": []},
                 "receipt": {"supplier": "", "date": "", "receipt_no": "",
                             "lines": [], "vat_cents": None,
                             "total_cents": None, "payment": ""},
                 "products": [
                     {"brand": "Albany", "variant": "White Bread",
                      "size": "700g", "visible_count": 2, "readable": True,
                      "notes": ""},
                     {"brand": "", "variant": "", "size": "",
                      "visible_count": None, "readable": False,
                      "notes": "label turned away"},
                 ]})))],
            usage=None)

    monkeypatch.setattr(vision, "_complete_vision", fake)
    monkeypatch.setenv("MODEL_API_KEY", "k")
    doc = vision.read_shop_photo(b"fake-image-bytes", SHOP)
    assert doc["products"][0]["visible_count"] == 2
    assert doc["products"][1] == {"brand": "", "variant": "", "size": "",
                                  "visible_count": None, "readable": False,
                                  "notes": "label turned away"}


def test_brief_carries_products_and_caption():
    from app.interpreter import media_brief
    brief = media_brief(_doc([_prod("Albany", "White Bread", "700g", 2)]),
                        "add to stock", "abc")
    assert "Albany White Bread 700g" in brief
    assert "x2 seen" in brief
    assert "add to stock" in brief


# intake preview -------------------------------------------------------------

def test_photo_intake_preview_not_rejection(conn, shop, monkeypatch):
    caption = _photo(monkeypatch, [_prod("Sasko", "White Bread", "700g", 2)])
    _stub(monkeypatch, _stock_add("Sasko White Bread 700g"))
    _, reply, buttons = agent.handle_photo(conn, SHOP, b"pic", caption)
    assert "can't tell what this is" not in reply
    assert "📦 *Stock intake*" in reply
    assert "×2 seen" in reply
    assert "sells R" in reply and "(list)" in reply
    assert buttons == ["Yes", "No"]
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake"


def test_unseen_caption_reaches_intake(conn, shop, monkeypatch):
    caption = _photo(monkeypatch, [_prod("Sasko", "White Bread", "700g", 1)],
                     "we bought groceries, capture them for the shop")
    _stub(monkeypatch, _stock_add("Sasko White Bread 700g"))
    _, reply, _ = agent.handle_photo(conn, SHOP, b"pic", caption)
    assert "📦 *Stock intake*" in reply


def test_new_product_asks_price_not_guess(conn, shop, monkeypatch):
    caption = _photo(monkeypatch, [_prod("Whey", "Protein", "2kg", 1)])
    _stub(monkeypatch, _stock_add("Whey Protein 2kg"))
    _, reply, _ = agent.handle_photo(conn, SHOP, b"pic", caption)
    assert "NEW" in reply
    assert "sells ?" in reply
    assert "selling price" in reply


def test_size_mismatch_never_substitutes(conn, shop, monkeypatch):
    # Marie 200g stocked; a multipack must not merge into it.
    caption = _photo(monkeypatch, [_prod("Marie", "", "Multipack", 1)])
    _stub(monkeypatch, _stock_add("Marie Multipack"))
    _, reply, _ = agent.handle_photo(conn, SHOP, b"pic", caption)
    assert "NEW" in reply
    assert "singles per pack?" in reply


def test_obscured_label_asks_name(conn, shop, monkeypatch):
    caption = _photo(monkeypatch,
                     [_prod("", "", "", 1, False, "label turned away")])
    _stub(monkeypatch, _stock_add())
    _, reply, _ = agent.handle_photo(conn, SHOP, b"pic", caption)
    assert "line 1 name" in reply
    msg, _ = agent.handle_text(conn, SHOP, "line 1 is Albany White Bread 700g")
    assert "Albany White Bread 700g" in msg


def test_unclear_count_asks_qty(conn, shop, monkeypatch):
    caption = _photo(monkeypatch,
                     [_prod("Sasko", "White Bread", "700g", None, True,
                            "stacked, can't count")])
    _stub(monkeypatch, _stock_add("Sasko White Bread 700g"))
    _, reply, _ = agent.handle_photo(conn, SHOP, b"pic", caption)
    assert "line 1 qty" in reply
    msg, _ = agent.handle_text(conn, SHOP, "4")
    assert "×4" in msg


def test_ambiguous_match_asks_which(conn, shop, monkeypatch):
    conn.execute("INSERT INTO products (shop_id, name, price_cents, stock_qty)"
                 " VALUES (?, 'Sasko Brown Bread 700g', 2000, 5)", (SHOP,))
    conn.commit()
    caption = _photo(monkeypatch, [_prod("Sasko", "Bread", "", 2)])
    _stub(monkeypatch, _stock_add("Sasko Bread"))
    _, reply, _ = agent.handle_photo(conn, SHOP, b"pic", caption)
    assert "which:" in reply
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert "Still need" in msg  # never guesses between alternatives
    msg, _ = agent.handle_text(conn, SHOP, "line 1 is Sasko Brown Bread 700g")
    assert "which:" not in msg


def test_no_products_asks_text_alternative(conn, shop, monkeypatch):
    caption = _photo(monkeypatch, [])
    _stub(monkeypatch, _stock_add("Sasko White Bread 700g"))
    _, reply, buttons = agent.handle_photo(conn, SHOP, b"pic", caption)
    assert "can't tell what this is" not in reply
    # planner named an item but vision saw none: intake still proposes it
    assert "📦 *Stock intake*" in reply
    assert "line 1 qty" in reply  # nothing seen, nothing assumed


def test_empty_intake_offers_text_alternative(conn, shop):
    msg = propose_stock_intake(conn, SHOP, [], [], None, "h")
    assert "can't make out any products" in msg
    assert get_pending(conn, SHOP) is None


# answers, corrections, confirmation -----------------------------------------

def _seed_intake(conn, monkeypatch):
    caption = _photo(monkeypatch, [_prod("Sasko", "White Bread", "700g", 2),
                                   _prod("Marie", "", "Multipack", 1)])
    _stub(monkeypatch, _stock_add("Sasko White Bread 700g",
                                  "Marie Multipack"))
    agent.handle_photo(conn, SHOP, b"pic", caption)


def test_yes_blocked_until_qty_resolved(conn, shop, monkeypatch):
    _seed_intake(conn, monkeypatch)
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert "Still need" in msg
    assert "singles-per-pack" in msg
    assert get_pending(conn, SHOP) is not None  # nothing saved


def test_per_pack_answer_resolves_multipack(conn, shop, monkeypatch):
    _seed_intake(conn, monkeypatch)
    msg, _ = agent.handle_text(conn, SHOP, "line 2: 12 per pack")
    assert "singles-per-pack" not in msg
    assert "×12" in msg


def test_price_answers_and_cost_sell_clarify(conn, shop, monkeypatch):
    _seed_intake(conn, monkeypatch)
    msg, _ = agent.handle_text(conn, SHOP, "line 1: cost R18, sell R21")
    assert "cost R18.00" in msg and "sells R21.00 (you said)" in msg
    msg, _ = agent.handle_text(conn, SHOP, "line 2: 12 per pack, cost R30")
    assert "cost R30.00" in msg
    msg, _ = agent.handle_text(conn, SHOP, "R45")
    assert "sells R45.00 (you said)" in msg  # only sell was missing


def test_bare_amount_asks_cost_vs_sell(conn, shop, monkeypatch):
    caption = _photo(monkeypatch, [_prod("Whey", "Protein", "2kg", 1)])
    _stub(monkeypatch, _stock_add("Whey Protein 2kg"))
    agent.handle_photo(conn, SHOP, b"pic", caption)
    msg, _ = agent.handle_text(conn, SHOP, "R450")
    assert "cost" in msg and "selling price" in msg
    msg, _ = agent.handle_text(conn, SHOP, "cost")
    assert "cost R450.00" in msg


def test_bulk_answer_by_name(conn, shop, monkeypatch):
    _seed_intake(conn, monkeypatch)
    msg, _ = agent.handle_text(conn, SHOP, "marie 12 per pack")
    assert "×12" in msg


def test_correction_and_unknown_line(conn, shop, monkeypatch):
    _seed_intake(conn, monkeypatch)
    msg, _ = agent.handle_text(conn, SHOP, "line 1 is 3 loaves")
    assert "×3" in msg
    msg, _ = agent.handle_text(conn, SHOP, "line 9 is 2")
    assert "no line 9" in msg


def test_save_posts_no_payment_without_cost(conn, shop, monkeypatch):
    cash_before = account_balance(conn, SHOP, "cash")
    caption = _photo(monkeypatch, [_prod("Sasko", "White Bread", "700g", 2)])
    _stub(monkeypatch, _stock_add("Sasko White Bread 700g"))
    agent.handle_photo(conn, SHOP, b"pic", caption)
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    assert "Stock saved" in msg
    assert account_balance(conn, SHOP, "cash") == cash_before
    assert conn.execute("SELECT COUNT(*) n FROM supplier_moves"
                        " WHERE shop_id = ?", (SHOP,)).fetchone()["n"] == 0


def test_repeated_yes_is_idempotent(conn, shop, monkeypatch):
    caption = _photo(monkeypatch, [_prod("Sasko", "White Bread", "700g", 2)])
    _stub(monkeypatch, _stock_add("Sasko White Bread 700g"))
    agent.handle_photo(conn, SHOP, b"pic", caption)
    agent.handle_text(conn, SHOP, "yes")
    before = conn.execute("SELECT stock_qty FROM products"
                          " WHERE shop_id = ? AND name = ?",
                          (SHOP, "Sasko White Bread 700g")).fetchone()[0]
    msg, _ = agent.handle_text(conn, SHOP, "yes")
    after = conn.execute("SELECT stock_qty FROM products"
                         " WHERE shop_id = ? AND name = ?",
                         (SHOP, "Sasko White Bread 700g")).fetchone()[0]
    assert "nothing" in msg.lower()
    assert after == before


def test_undo_reverses_whole_intake(conn, shop, monkeypatch):
    caption = _photo(monkeypatch, [_prod("Sasko", "White Bread", "700g", 2),
                                   _prod("Whey", "Protein", "2kg", 1,
                                         notes="new")])
    _stub(monkeypatch, _stock_add("Sasko White Bread 700g", "Whey Protein 2kg"))
    agent.handle_photo(conn, SHOP, b"pic", caption)
    agent.handle_text(conn, SHOP, "line 2: sell R500")
    agent.handle_text(conn, SHOP, "yes")
    assert conn.execute("SELECT COUNT(*) n FROM products WHERE shop_id = ?"
                        " AND name = ?", (SHOP, "Whey Protein 2kg")
                        ).fetchone()["n"] == 1
    msg, _ = agent.handle_text(conn, SHOP, "undo")
    assert "Undone" in msg
    assert conn.execute("SELECT COUNT(*) n FROM products WHERE shop_id = ?"
                        " AND name = ?", (SHOP, "Whey Protein 2kg")
                        ).fetchone()["n"] == 0


def test_duplicate_photo_detected_after_intake(conn, shop, monkeypatch):
    caption = _photo(monkeypatch, [_prod("Sasko", "White Bread", "700g", 2)])
    _stub(monkeypatch, _stock_add("Sasko White Bread 700g"))
    agent.handle_photo(conn, SHOP, b"same-pic", caption)
    agent.handle_text(conn, SHOP, "yes")
    _, reply, _ = agent.handle_photo(conn, SHOP, b"same-pic", caption)
    assert "already saved" in reply


# persistence + isolation ----------------------------------------------------

def test_restart_keeps_intake_answerable(tmp_path, monkeypatch):
    import sqlite3

    from app.db import connect
    db_path = tmp_path / "shop.db"
    monkeypatch.setenv("SISI_DB", str(db_path))
    conn = connect(str(db_path))
    init_db(conn)
    seed_demo(SHOP, conn)
    skip_setup(conn, SHOP)
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?",
                 (SHOP,))
    conn.commit()
    monkeypatch.setattr(agent, "read_shop_photo",
                        lambda image, shop="": _doc(
                            [_prod("Sasko", "White Bread", "700g", 2)]))
    _stub(monkeypatch, _stock_add("Sasko White Bread 700g"))
    agent.handle_photo(conn, SHOP, b"pic", CAPTION)
    conn.close()  # worker restart: new connection, same shop DB
    conn2 = connect(str(db_path))
    try:
        msg, _ = agent.handle_text(conn2, SHOP, "line 1: cost R18")
        assert "cost R18.00" in msg
    finally:
        conn2.close()


def test_shop_isolation(conn, shop, monkeypatch):
    other = "27710000099"
    skip_setup(conn, other)
    caption = _photo(monkeypatch, [_prod("Sasko", "White Bread", "700g", 2)])
    _stub(monkeypatch, _stock_add("Sasko White Bread 700g"))
    agent.handle_photo(conn, SHOP, b"pic", caption)
    msg, _ = agent.handle_text(conn, other, "line 1: cost R18")
    assert "cost R18.00" not in msg  # other's shop has no intake
    assert get_pending(conn, SHOP) is not None  # ours waits untouched


def test_unrelated_question_passes_planner(conn, shop, monkeypatch):
    _seed_intake(conn, monkeypatch)
    _stub(monkeypatch, [{"kind": "cash", "args": {}, "quote": ""}])
    msg, _ = agent.handle_text(conn, SHOP, "how much is in the till?")
    assert "Cash" in msg
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake"
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake"


# legacy workflows preserved -------------------------------------------------

def test_expiry_receipt_logo_paths_unaffected(conn, shop, monkeypatch):
    from app.interpreter import media_brief
    exp = {"doc_type": "expiry", "expiry": {"stamp_count": 1, "items": []},
           "book": {}, "receipt": {}, "products": []}
    assert "expiry check" in media_brief(exp, "", "d")
    rec = {"doc_type": "receipt", "expiry": {}, "book": {},
           "receipt": {"supplier": "Jumbo", "total_cents": 100},
           "products": []}
    assert "Jumbo" in media_brief(rec, "", "d")
    caption = _photo(monkeypatch, [_prod("Sasko", "White Bread", "700g", 1)],
                     "logo")
    _stub(monkeypatch, [{"kind": "logo", "args": {}, "quote": ""}])
    _, reply, buttons = agent.handle_photo(conn, SHOP, b"pic", caption)
    assert "Stock intake" not in reply  # logo caption keeps logo routing
