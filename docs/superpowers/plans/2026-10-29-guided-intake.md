# Guided One-Product-at-a-Time Intake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn photo stock intake into a guided conversation that handles one product at a time with per-item confirmation.

**Architecture:** A per-shop `intake_sessions` row holds the photo, item queue, current field, and answers; a completer answers one question per message in natural language; each item saves through its own `stock_intake_item` pending so Yes/Undo stay per-item and atomic. The existing one-shot `propose_stock_intake` stays for multi-item text and in-flight pendings.

**Tech Stack:** Python 3.11+, SQLite, pytest; existing `_propose`/`confirm_pending`/`_apply_add_stock` machinery.

**Spec:** User message 2026-10-29 "Improve Sisi's photo stock intake into a guided, one-product-at-a-time conversation" (this chat; no separate doc).

## Global Constraints

- Money stored as integer cents, shown as R0.00.
- Nothing saved until owner confirms; every write has undo via action_id.
- Never invent numbers, prices, customers, or pack contents.
- Replies follow the visual style guide (emoji headings, *bold*, one item/line, ≤120 chars/line).
- Every `_t("key")` literal must exist in en/zu/af/xh with matching placeholders (test_i18n enforces this).
- Data isolated per shop_id; no secrets in code/logs.
- No automatic cash deduction, payment, or supplier debt from intake alone.
- Preserve existing expiry/receipt/book/logo/order/media workflows and their tests.

## Review Focus

- A photo of non-products (person, landscape) with an intake caption: must ask for a real product photo or item list, never create lines. (Task 1 test.)
- Owner answers a question with another photo: must trigger the add-or-new choice, not silently replace. (Task 5 test.)
- Owner confirms an item, then the worker restarts before the next question renders: next message must resume at the next item, not re-save. (Task 6 test.)
- Two rapid "Yes" taps (double delivery): second must report nothing waiting, stock unchanged. (Task 6 test.)
- Cost stated as "R20 for both packs" (total, not per-pack): must ask per-pack vs total rather than assume. (Task 4 test: parser treats "for both/N packs" as needing basis clarification.)

---

### Task 1: Session store + item grounding (phantom fix)

**Files:**
- Modify: `app/db.py` (append `CREATE TABLE intake_sessions` to schema)
- Modify: `app/tools.py` (session CRUD + grounding, placed after `_undo_stock_intake`)
- Test: `tests/test_guided_intake.py` (new file)

**Interfaces:**
- Consumes: `ensure_shop(conn, shop_id)`, `now_iso()`, `_find_products` (existing).
- Produces:
  - `get_intake_session(conn, shop_id) -> dict | None` (None when absent/expired/done; lazy-expires after 24h)
  - `save_intake_session(conn, shop_id, session: dict) -> None` (upsert by shop_id)
  - `clear_intake_session(conn, shop_id) -> None`
  - `ground_intake_items(items: list, products: list, text: str) -> list` (drops planned items whose words overlap neither vision labels nor caption content words; never raises)
  - Session dict shape: `{photo_digest, caption, vision_json, items_json:[{name, qty, qty_source, unit, cost_cents, sell_cents, cost_basis, sell_unit, per_pack, packs, product_id, ambiguous[], new, state, seen_label}], current:int, field:str|None, status, stash:{kind, digest, caption, vision}}`

- [ ] **Step 1: Write the failing tests**

```python
def test_grounding_drops_phantom_item():
    items = [{"product": "Albany Bread"}, {"product": "Photo item 4"}]
    products = [{"brand": "Albany", "variant": "Bread", "size": "",
                 "visible_count": 1, "readable": True, "notes": ""}]
    kept = ground_intake_items(items, products, "please add it to our stock")
    assert [e["product"] for e in kept] == ["Albany Bread"]

def test_session_roundtrip_and_isolation(conn):
    save_intake_session(conn, "s1", {"current": 1, "items_json": []})
    assert get_intake_session(conn, "s1")["current"] == 1
    assert get_intake_session(conn, "s2") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `MODEL_API_KEY= .venv/bin/python -m pytest tests/test_guided_intake.py -q -p no:cacheprovider`
Expected: FAIL with "cannot import name".

- [ ] **Step 3: Implement table + 4 functions in `app/db.py` / `app/tools.py`**

Table columns: `shop_id TEXT PRIMARY KEY, photo_digest TEXT, caption TEXT, vision_json TEXT, items_json TEXT, current INTEGER, field TEXT, status TEXT, stash_json TEXT, created_at, updated_at, expires_at`. Expiry: 24h from update, checked in getter.

- [ ] **Step 4: Run tests to verify they pass**

Run: same command. Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/db.py app/tools.py tests/test_guided_intake.py
git commit -m "intake: session store and item grounding"
```

### Task 2: Guided new-product flow

**Files:**
- Modify: `app/tools.py` (`start_guided_intake`, `ask_intake_field`, `answer_intake_session`, `propose_intake_item`)
- Modify: `app/i18n.py` (keys: see Interfaces)
- Test: `tests/test_guided_intake.py` (append)

**Interfaces:**
- Consumes: Task 1 session CRUD + grounding; `_intake_match`, `_coerce_pos_int`, `_propose`, `_t`.
- Produces:
  - `start_guided_intake(conn, shop_id, products, items, caption, photo_hash, supplier=None) -> str` (overview + first prompt; replaces any active session)
  - `answer_intake_session(conn, shop_id, text: str) -> str | None` (None when no active session or text is unrelated business)
  - `propose_intake_item(conn, shop_id, idx: int) -> str` (per-item Yes/No preview via `_propose("stock_intake_item", ...)`)
  - Field order for new: name? → which? → qty? → cost → sell → sell_unit? (only with pack context) → preview. One question per call; skips supplied fields. "don't know"/"skip" skips cost/sell only.
- i18n keys (all 4 languages, placeholders in braces): `g_head` ("📦 *Stock intake: {done} of {total} done*"), `g_found` ("Found {n} items: {names}."), `g_item` ("Item {i} of {n}: {name}."), `g_ask_name`, `g_ask_which(names)`, `g_ask_qty(name, seen)`, `g_ask_cost(name)`, `g_ask_sell(name)`, `g_ask_unit(name)`, `g_new_tag` ("NEW"), `g_saved_next` (appended after save; Task 6 owns the save, this task owns the string), `g_invalid` ("I didn't catch that. {hint}").

- [ ] **Step 1: Write the failing tests**

```python
def test_new_product_full_guided_flow(conn, shop, monkeypatch):
    start_guided_intake(conn, SHOP, [vision_brie("Whey Protein 2kg", 1)], [], "add to stock", "h1")
    # asks cost first (identity+qty confident from readable label)
    msg = answer_intake_session(conn, SHOP, "16 rand")
    assert "selling price" in msg  # cost accepted, sell asked next
    msg = answer_intake_session(conn, SHOP, "sell for eighteen")
    assert "Yes" in msg and "No" in msg  # preview proposed (assert via pending type instead)
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"

def test_one_question_per_message_skips_supplied(conn, shop):
    start_guided_intake(conn, SHOP, [], [{"product": "Sugar 2kg", "qty": 3, "cost_cents": 4500}], "sugar 3 cost R45", "h2")
    # qty+cost supplied -> first question is selling price
    msg = get_intake_session(conn, SHOP)  # assert field == "sell" via session
    assert msg["field"] == "sell"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `MODEL_API_KEY= .venv/bin/python -m pytest tests/test_guided_intake.py -q -p no:cacheprovider -k "guided or supplied"`
Expected: FAIL with "cannot import name".

- [ ] **Step 3: Implement the 3 functions + i18n keys**

Amount parser accepts "16 rand", "R16", "sixteen", "sell for eighteen" (reuse `_NUMBER_WORDS`; numbers 1–100; money = R-prefix/decimal/bare-in-price-field). Cost/sell assign to current item only.

- [ ] **Step 4: Run tests to verify they pass**

Run: full file. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/tools.py app/i18n.py tests/test_guided_intake.py
git commit -m "intake: guided new-product questions and answers"
```

### Task 3: Existing-product fast path + careful matching

**Files:**
- Modify: `app/tools.py` (match confidence + existing branch in field order)
- Test: `tests/test_guided_intake.py` (append)

**Interfaces:**
- Consumes: Task 2 field engine; `_intake_match`.
- Produces: behavior — matched product with confident qty goes straight to item preview (no cost/sell/unit questions); shoplist price shown as source; "sell at R25" on existing explicitly updates the preview price; uncertain match/count asks exactly one question.
- i18n keys: `g_ask_qty_confirm(name, seen)` ("I see {seen} — how many did you get? Reply Yes if that's all."), `g_reuse_price(amt)` ("keeps shop price {amt}").

- [ ] **Step 1: Write the failing tests**

```python
def test_existing_confident_goes_straight_to_preview(conn, shop):
    start_guided_intake(conn, SHOP, [brie("Sasko White Bread 700g", 2, readable=True)], [{"product": "Sasko White Bread 700g"}], "add to stock", "h")
    assert get_pending(conn, SHOP)["action_type"] == "stock_intake_item"

def test_uncertain_count_confirms_first(conn, shop):
    start_guided_intake(conn, SHOP, [brie("Sasko White Bread 700g", None)], [{"product": "Sasko White Bread 700g"}], "add to stock", "h")
    assert get_intake_session(conn, SHOP)["field"] == "qty"

def test_explicit_sell_update_allowed_on_existing(conn, shop):
    ...answer "sell at R25" sets sell_cents=2500 stated; preview shows it...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `MODEL_API_KEY= .venv/bin/python -m pytest tests/test_guided_intake.py -q -p no:cacheprovider -k "existing or uncertain or explicit"`
Expected: FAIL (wrong field/pending).

- [ ] **Step 3: Implement match-confidence + existing branch**

Confident = readable vision label with exact inventory match + (stated qty or clear seen count). Confirm-qty accepts "yes" as approval. Never overwrite price unless the answer carries an explicit sell amount.

- [ ] **Step 4: Run tests to verify they pass**

Run: full file. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/tools.py app/i18n.py tests/test_guided_intake.py
git commit -m "intake: existing-product fast path"
```

### Task 4: Multipacks, cost basis, both

**Files:**
- Modify: `app/tools.py` (sell_unit/per_pack/basis fields + conversion math)
- Modify: `app/i18n.py` (keys below)
- Test: `tests/test_guided_intake.py` (append)

**Interfaces:**
- Consumes: Task 2 field engine.
- Produces: fields `sell_unit` (pack/singles/both), `per_pack`, `packs`, `cost_basis` (pack/single). Math mirrors `_apply_add_stock`: per-single cost = pack cost / pack_size (float, rounded only in totals). "both" writes `price_cents` (single) + `pack_price_cents` + `pack_name/pack_size` on the product row.
- i18n keys: `g_ask_unit_pack(name)` ("Will you sell this as a whole pack or as individual packets?"), `g_ask_perpack(name)`, `g_ask_packs(name)`, `g_ask_costbasis(amt)` ("Is {amt} for each pack or for one packet?"), `g_both_prices` (asks pack price then single price; reuse `g_ask_sell` with qualifier param `per`).

- [ ] **Step 1: Write the failing tests**

```python
def test_multipack_singles_math(conn, shop):
    # 2 outer packs x 5 packets, R20 per outer pack
    ...answer unit="singles", packs=2, per_pack=5, cost=2000 basis=pack, sell=600...
    line = session items[0]
    assert line["stock_units"] == 10 and line["cost_cents"] == 400

def test_multipack_whole_is_two_units(conn, shop):
    ...unit="whole", packs=2... assert stock_units == 2, cost stays 2000/pack...

def test_both_writes_pack_and_single_prices(conn, shop):
    ...after save: products row has price_cents == single AND pack_price_cents == pack...

def test_total_cost_asks_basis(conn, shop):
    ...answer "R20 for both packs" while cost field open -> asks costbasis, assigns nothing...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `-k "multipack or both or total_cost"`. Expected: FAIL.

- [ ] **Step 3: Implement multipack fields + conversions**

"sold individually" synonyms: singles/single/loose/each/individual(ly)/packets. Whole: whole/pack/entire/box. "both" only via explicit word. Natural "five in each pack" fills per_pack.

- [ ] **Step 4: Run tests to verify they pass**

Run: full file. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/tools.py app/i18n.py tests/test_guided_intake.py
git commit -m "intake: multipack units and cost basis"
```

### Task 5: Conversation commands + photo/dup choices

**Files:**
- Modify: `app/tools.py` (commands in `answer_intake_session`; stash handling)
- Modify: `app/agent.py` (`try_intake_reply` rewrite to session-first; YES/NO/CANCEL intake interception; `handle_photo` session/dup branches)
- Modify: `app/i18n.py` (keys below)
- Test: `tests/test_guided_intake.py` (append); update `tests/test_photos.py::test_duplicate_photo_detected_after_save`, `tests/test_intake.py::test_duplicate_photo_detected_after_intake`

**Interfaces:**
- Consumes: Tasks 1–4; `_interpret`, `confirm_newest`/`cancel_newest`/`cancel_pending` (existing).
- Produces: commands `back`, `skip`/`skip item`, `pause`/`later`, `resume`/`continue`, `cancel` (asks confirm) / `cancel all`, `add`/`new` (pending-photo choice), `restock` (dup-photo stash). Unrelated text → `_interpret` reply + resume line. New photo mid-session stashes instead of replacing. Seen digest stashes instead of instant-dup.
- i18n keys: `g_back_none`, `g_skipped(name)`, `g_paused`, `g_resumed`, `g_cancel_ask(n)`, `g_cancel_done(n)`, `g_photo_choice(done, total)`, `g_dup_ask(day)`, `g_resume_line(i, n, q)` ("📌 Still on item {i} of {n}: {q}"), `g_required` ("That one I really need — {hint}").

- [ ] **Step 1: Write the failing tests**

```python
def test_back_skip_pause_resume_cancel(conn, shop): ...
def test_new_photo_asks_add_or_new(conn, shop, monkeypatch): ...  # via handle_photo, asserts choice text; "add" merges
def test_dup_photo_asks_restock(conn, shop, monkeypatch): ...  # resend -> question; "restock" starts session
def test_unrelated_question_keeps_progress(conn, shop, monkeypatch): ...  # planner stub + resume line, session intact
def test_invalid_reply_clarifies_no_products(conn, shop): ...  # "yesterday maybe" -> g_invalid; no lines added
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `-k "back_skip or add_or_new or dup_photo_asks or unrelated or invalid_reply"`. Expected: FAIL.

- [ ] **Step 3: Implement commands + agent wiring**

YES/NO/CANCEL blocks: only when no pending/plan/staged/await AND session active → delegate to `answer_intake_session`. Attempted-answer detection for invalid path: contains digit/amount/unit-word/cost/sell word. Photo stash lives in `session["stash"]`; dup stash in flags table under `intake-dup-photo`.

- [ ] **Step 4: Run tests + update the two legacy dup tests to the ask-first flow**

Run: full file + `tests/test_photos.py tests/test_intake.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/tools.py app/agent.py app/i18n.py tests/
git commit -m "intake: conversation commands and photo choices"
```

### Task 6: Per-item atomic save, undo, summary

**Files:**
- Modify: `app/tools.py` (`_apply_stock_intake_item`, register in `_APPLIERS`, session advance + summary inside apply)
- Modify: `app/agent.py` (NO on item preview = skip + continue)
- Modify: `app/prompts.py` (update stock_add guidance: name only seen/said products, never "photo item N" placeholders)
- Test: `tests/test_guided_intake.py` (append)

**Interfaces:**
- Consumes: Tasks 2–5.
- Produces: `_APPLIERS["stock_intake_item"]`; apply converts the line to an `add_stock` payload, calls `_apply_add_stock` (kind `"stock"`, existing undo messages name the product), marks the item done, advances, appends next prompt or final summary. Final summary: saved (name × qty unit), skipped, still-pending counts.
- i18n keys: `g_item_saved(name, qty)` (prefix before next prompt), `g_summary(saved, skipped, pending)` lines, `g_no_preview` (safety when pending vanished).

- [ ] **Step 1: Write the failing tests**

```python
def test_item_save_advances_and_summary_ends(conn, shop): ...  # 2 items: yes,yes -> second save appends summary; session done
def test_undo_names_exact_item(conn, shop): ...  # undo after item 2 -> names item 2, item 1 stock intact
def test_no_double_save_on_repeated_yes(conn, shop): ...  # stock unchanged, "nothing waiting"
def test_restart_mid_item_resumes(conn_file_db): ...  # close+reopen between items -> next prompt intact, no re-save
def test_no_cash_or_supplier_writes(conn, shop): ...  # cash_moves/supplier_moves empty after full intake with costs... (cost posts on confirmed cost like add_stock: assert ONLY the confirmed cost posts, nothing else)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `-k "advances or undo_names or double_save or restart_mid or no_cash"`. Expected: FAIL.

- [ ] **Step 3: Implement apply + advance + summary + NO-skip + prompt update**

NO-word block: if pending action is `stock_intake_item` → mark skipped, advance, return next prompt (do not call `cancel_pending`).

- [ ] **Step 4: Run tests to verify they pass**

Run: full file. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/tools.py app/agent.py app/prompts.py tests/test_guided_intake.py
git commit -m "intake: per-item save, undo, summary"
```

### Task 7: Full verification

**Files:**
- Test: `tests/test_guided_intake.py` (review-focus tests), full suite, clean clone.

- [ ] **Step 1: Add the five Review Focus tests to the owning areas** (non-product photo; photo-as-answer; restart-before-render; double Yes; "for both packs" total).
- [ ] **Step 2: Run the full suite**

Run: `MODEL_API_KEY= .venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass except `test_media_served_from_fresh_process` (sandbox blocks socket bind; CI runs it).
- [ ] **Step 3: Clean-clone check**

Run: `rm -rf /tmp/sisi-ci && git clone -q . /tmp/sisi-ci && cd /tmp/sisi-ci && MODEL_API_KEY= /Users/donovandeklerk/sisi/.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: same result.
- [ ] **Step 4: Fix any failures; do not push (user deploys separately)**

## Self-Review

1. **Spec coverage:** overview+one-by-one (T2/T6), new flow (T2), existing fast path (T3), multipacks incl. both+math (T4), natural/combined answers (T2/T4), back/skip/pause/resume/cancel (T5), persistence+isolation (T1), unrelated questions (T5), new-photo choice (T5), invalid replies (T5), phantom fix (T1+T6-prompt), duplicates/idempotency (T5+T6), atomic saves (T6), explicit undo (T6), no auto-payment (T6), summary (T6). All covered.
2. **Step scan:** each step names one test/code/verification action; bodies only where signatures don't determine them (parsers, math).
3. **Type consistency:** session dict shape defined once in Task 1 and reused; `answer_intake_session` signature stable across tasks.
4. **Review Focus:** five lines above, each mapped to Task 7 step 1 with owning-task context.
5. **Proportion:** plan is shorter than the spec; code blocks are tests/assertions only.
