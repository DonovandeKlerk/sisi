"""End-to-end smoke against PROD, test shop only (Modal run, never deployed).

Usage (from repo root):
    .venv/bin/modal run scripts/smoke_demo.py

Drives the real handlers (text, voice, photo, planner, renders) against the
reseeded test shop 27710000002. Sends NO WhatsApp traffic (outboxes are
asserted in-process and popped). Every write is undone or cancelled, and
smoke chatter + the fixture photo hash are deleted afterwards; balances
must equal the pre-smoke snapshot at the end. Prints PASS/FAIL per step.
"""

import modal

image = (
    modal.Image.debian_slim()
    .apt_install("ffmpeg", "fonts-dejavu-core")
    .pip_install_from_requirements("requirements.txt")
    .add_local_dir("app", remote_path="/root/app",
                   ignore=["__pycache__", "*.pyc", "*.pyo"])
    .add_local_file("tests/fixtures/book_page.png",
                    remote_path="/root/book_page.png")
    .add_local_file("samples/voice_sale_cash.ogg",
                    remote_path="/root/voice_sale_cash.ogg")
)

app = modal.App(
    "sisi-smoke",
    image=image,
    secrets=[modal.secret.Secret.from_name("sisi-env")],
    volumes={"/data": modal.volume.Volume.from_name("sisi-data")},
)

TEST_SHOP = "27710000002"
TEST_NAME = "Bongi’s Spaza Shop"  # curly apostrophe, as stored


@app.function(timeout=600)
def smoke() -> str:
    import hashlib
    import json
    import os
    from pathlib import Path

    os.environ["SISI_DB"] = "/data/sisi.db"
    os.environ["SISI_REPORTS"] = "/data/reports"
    os.environ["SISI_LOGOS"] = "/data/logos"
    from app import reports as R
    from app.agent import (MENU_ROWS, handle_photo, handle_text, handle_voice,
                           take_buttons, take_lists, take_media)
    from app.db import connect
    from app.tools import (account_balance, answer_book_line, balance_cents,
                           first_book_question, get_pending)

    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = ""):
        results.append((name, bool(ok), str(detail or "")[:300]))

    conn = connect()
    try:
        shop = conn.execute(
            "SELECT name, language FROM shops WHERE shop_id = ?",
            (TEST_SHOP,)).fetchone()
        if shop is None or shop["name"] != TEST_NAME:
            return "REFUSED: test shop missing or renamed"
        lang = shop["language"]
        snap_cash = account_balance(conn, TEST_SHOP, "cash")
        snap_bank = account_balance(conn, TEST_SHOP, "bank")
        custs = conn.execute(
            "SELECT id, name FROM customers WHERE shop_id = ?",
            (TEST_SHOP,)).fetchall()
        snap_bal = {r["name"]: balance_cents(conn, TEST_SHOP, r["id"])
                    for r in custs}
        turn_max = conn.execute(
            "SELECT COALESCE(MAX(id),0) m FROM conversation_turns"
            " WHERE shop_id = ?", (TEST_SHOP,)).fetchone()["m"]
        # Restart-proofing: a preempted attempt leaves the fixture hash behind
        # (its book save was undone, but the hash stays), which would trip the
        # dup guard. Fixture bytes never occur naturally, so deleting it at
        # the start only ever removes a previous smoke attempt's row.
        fix_digest = hashlib.sha256(
            Path("/root/book_page.png").read_bytes()).hexdigest()
        had_hash = conn.execute(
            "SELECT 1 FROM photo_hashes WHERE shop_id = ? AND hash = ?",
            (TEST_SHOP, fix_digest)).fetchone() is not None
        conn.execute("DELETE FROM photo_hashes WHERE shop_id = ? AND hash = ?",
                     (TEST_SHOP, fix_digest))
        if had_hash:  # a first attempt ran: drop its chatter too (LLM context only)
            conn.execute(
                "DELETE FROM conversation_turns WHERE shop_id = ?"
                " AND created_at > strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-2 hours')",
                (TEST_SHOP,))
            turn_max = conn.execute(
                "SELECT COALESCE(MAX(id),0) m FROM conversation_turns"
                " WHERE shop_id = ?", (TEST_SHOP,)).fetchone()["m"]
        conn.commit()
        clar_max = conn.execute(
            "SELECT COALESCE(MAX(rowid),0) m FROM clarify_context"
            " WHERE shop_id = ?", (TEST_SHOP,)).fetchone()["m"]
        plan_ids = {r["plan_id"] for r in conn.execute(
            "SELECT plan_id FROM task_plans WHERE shop_id = ?",
            (TEST_SHOP,)).fetchall()}

        # 1. hi / menu list.
        take_lists(TEST_SHOP)
        take_media(TEST_SHOP)
        take_buttons(TEST_SHOP)
        msg, _ = handle_text(conn, TEST_SHOP, "hi")
        lists = take_lists(TEST_SHOP)
        check("hi_menu", len(lists) == 1 and lists[0]["rows"] == MENU_ROWS
              and bool(msg.strip()),
              f"reply={msg[:60]!r} rows={lists[0]['rows'] if lists else []}")

        # 2. Sale by text (planner path), then No.
        msg, _ = handle_text(conn, TEST_SHOP, "sold 2 coke")
        pend = get_pending(conn, TEST_SHOP)
        preview = (pend["preview_text"] or "") if pend else ""
        ok = pend is not None and "Coke" in preview and "R72.00" in preview
        check("sale_text", ok, f"pend={pend['action_type'] if pend else None}"
              f" prev={preview[:100]!r} reply={msg[:100]!r}")
        if get_pending(conn, TEST_SHOP) is not None:
            handle_text(conn, TEST_SHOP, "No")
        check("sale_text_cancelled", get_pending(conn, TEST_SHOP) is None)

        # 3. Sale by voice (real Meta ASR), then No.
        audio = Path("/root/voice_sale_cash.ogg").read_bytes()
        try:
            heard, reply, _ = handle_voice(conn, TEST_SHOP, audio)
        except Exception as e:  # noqa: BLE001 - smoke must report, not crash
            heard, reply = "", f"VOICE ERROR: {e}"
        pend = get_pending(conn, TEST_SHOP)
        preview = (pend["preview_text"] or "") if pend else ""
        check("sale_voice", bool((heard or "").strip()) and pend is not None,
              f"heard={heard[:80]!r} pend={pend['action_type'] if pend else None}"
              f" prev={preview[:80]!r} reply={reply[:80]!r}")
        if get_pending(conn, TEST_SHOP) is not None:
            handle_text(conn, TEST_SHOP, "No")

        # 4. Book-page photo -> answer -> Yes -> daily PDF -> undo.
        photo = Path("/root/book_page.png").read_bytes()
        digest = hashlib.sha256(photo).hexdigest()
        _doc, reply, _ = handle_photo(conn, TEST_SHOP, photo, "")
        pend = get_pending(conn, TEST_SHOP)
        check("book_photo_proposes",
              pend is not None and pend["action_type"] == "book_page",
              f"pend={pend['action_type'] if pend else None} reply={reply[:120]!r}")
        bound_detail = ""
        if pend is not None and pend["action_type"] == "book_page":
            for _ in range(6):
                payload = json.loads(get_pending(conn, TEST_SHOP)["payload_json"])
                entry = first_book_question(payload)
                if entry is None:
                    break
                if entry.get("options"):
                    answer_book_line(conn, TEST_SHOP, entry["line_no"],
                                     entry["options"][0])
                else:  # supplier qty/cost or unanswerable: stop and report
                    bound_detail = f"open q line {entry['line_no']} need={entry.get('need')}"
                    break
            payload = json.loads(get_pending(conn, TEST_SHOP)["payload_json"])
            n_bound = sum(len(payload.get(k) or [])
                          for k in ("cash", "credit", "card", "payments",
                                    "supply", "waste"))
            check("book_lines_bind", n_bound >= 1 and
                  first_book_question(payload) is None,
                  f"bound={n_bound} {bound_detail} date={payload.get('date')}")
            msg, _ = handle_text(conn, TEST_SHOP, "Yes")
            check("book_yes_saves", msg.startswith("✅ *Book saved*"),
                  msg[:120])
        else:
            check("book_lines_bind", False, "no book pending")
            check("book_yes_saves", False, "no book pending")
            payload = {}

        # 5. Daily sales PDF from that save.
        day = (payload or {}).get("date", "")
        row = conn.execute(
            "SELECT png_path, token FROM reports WHERE shop_id = ? AND month = ?",
            (TEST_SHOP, f"daily-{day}")).fetchone() if day else None
        pdf_ok, pdf_detail = False, f"day={day or '-'}"
        if row is not None:
            pdf_path = row["png_path"] or ""  # store_card keeps the pdf path here
            if pdf_path.endswith(".pdf") and Path(pdf_path).exists():
                raw = Path(pdf_path).read_bytes()
                pdf_ok = raw.startswith(b"%PDF") and b"Daily sales" in raw
                pdf_detail = f"{pdf_path} {len(raw)}B"
            else:
                pdf_detail = f"missing file {pdf_path}"
        check("daily_pdf", pdf_ok, pdf_detail)

        # 6. Undo the book save; balances must restore.
        if day:
            msg, _ = handle_text(conn, TEST_SHOP, "undo book")
            restored = account_balance(conn, TEST_SHOP, "cash") == snap_cash
            bals = {r["name"]: balance_cents(conn, TEST_SHOP, r["id"])
                    for r in conn.execute(
                        "SELECT id, name FROM customers WHERE shop_id = ?",
                        (TEST_SHOP,)).fetchall()}
            check("book_undo_restores",
                  "undone" in msg.lower() or "Undone" in msg
                  and restored and bals == snap_bal,
                  f"{msg[:80]!r} cash={restored} bals={bals == snap_bal}")
        else:
            check("book_undo_restores", False, "no save to undo")

        # 7. All debtors' statements: one card + PDF per debtor, logo checks.
        take_media(TEST_SHOP)
        msg, _ = handle_text(conn, TEST_SHOP, "send all debtors' statements")
        media = take_media(TEST_SHOP)
        debtors = [n for n, b in snap_bal.items() if b > 0]
        kinds = {}
        months = set()
        for m in media:
            kinds[m.get("kind")] = kinds.get(m.get("kind"), 0) + 1
            if "/media/" in (m.get("url") or ""):
                token = m["url"].split("/media/")[1].split("?")[0]
                r = conn.execute("SELECT month FROM reports WHERE token = ?",
                                 (token,)).fetchone()
                if r is not None:
                    months.add(r["month"])
        per_debtor = all(
            any(x == f"stmt-{_slug(n)}-pdf" for x in months)
            and sum(1 for x in months
                    if x.startswith(f"stmt-{_slug(n)}-p")) >= 1
            for n in debtors)
        check("statements_all_queued",
              len(debtors) == 5 and kinds.get("image", 0) >= 5
              and kinds.get("file", 0) >= 5 and "Statements for everyone" in msg
              and per_debtor,
              f"debtors={debtors} kinds={kinds} per_debtor={per_debtor}"
              f" reply={msg[:100]!r}")
        logo_ok, logo_detail = _logo_pixel_check(conn, R, TEST_SHOP, media)
        check("statement_logo", logo_ok, logo_detail)

        # 8. Multi-intent: sale + statement in one message.
        take_media(TEST_SHOP)
        msg, _ = handle_text(conn, TEST_SHOP,
                             "sold 2 bread and send Thandi her statement")
        media = take_media(TEST_SHOP)
        pend = get_pending(conn, TEST_SHOP)
        sale_side = (pend is not None and "bread" in
                     ((pend["preview_text"] or "") + msg).lower()) \
            or "bread" in msg.lower()
        stmt_side = any("thandi" in (m.get("caption") or "").lower()
                        or "thandi" in (m.get("filename") or "").lower()
                        for m in media)
        check("multi_intent", sale_side and stmt_side,
              f"sale={sale_side} stmt={stmt_side} pend={pend['action_type'] if pend else None}"
              f" media={len(media)} reply={msg[:150]!r}")
        if get_pending(conn, TEST_SHOP) is not None:
            handle_text(conn, TEST_SHOP, "No")
        take_media(TEST_SHOP)

        # 9. isiZulu / Afrikaans / isiXhosa statement requests.
        for name, text, marker in [
            ("lang_zu", "ngicela isitatimende sikaSipho", "Mthumelele u-Sipho"),
            ("lang_af", "stuur vir my die strokie van Thandi", "Stuur dit aan Thandi"),
            ("lang_xh", "ndicela istatimenti sikaMama Zodwa",
             "Mthumelele ku-Mama Zodwa"),
        ]:
            take_media(TEST_SHOP)
            msg, _ = handle_text(conn, TEST_SHOP, text)
            media = take_media(TEST_SHOP)
            caps = " ".join((m.get("caption") or "") for m in media)
            check(name, marker in caps and len(media) >= 2,
                  f"marker={marker in caps} media={len(media)} reply={msg[:80]!r}")

        # Cleanup: smoke chatter + fixture hash + any question/plan state
        # the multi-intent step parked; final balance check.
        conn.execute("DELETE FROM conversation_turns WHERE shop_id = ? AND id > ?",
                     (TEST_SHOP, turn_max))
        conn.execute("DELETE FROM photo_hashes WHERE shop_id = ? AND hash = ?",
                     (TEST_SHOP, digest))
        conn.execute("DELETE FROM clarify_context WHERE shop_id = ? AND rowid > ?",
                     (TEST_SHOP, clar_max))
        for r in conn.execute("SELECT plan_id FROM task_plans WHERE shop_id = ?",
                              (TEST_SHOP,)).fetchall():
            if r["plan_id"] not in plan_ids:
                conn.execute("DELETE FROM task_plans WHERE shop_id = ? AND plan_id = ?",
                             (TEST_SHOP, r["plan_id"]))
        conn.commit()
        leaked = get_pending(conn, TEST_SHOP) is not None
        bals = {r["name"]: balance_cents(conn, TEST_SHOP, r["id"])
                for r in conn.execute(
                    "SELECT id, name FROM customers WHERE shop_id = ?",
                    (TEST_SHOP,)).fetchall()}
        check("books_balanced",
              not leaked
              and account_balance(conn, TEST_SHOP, "cash") == snap_cash
              and account_balance(conn, TEST_SHOP, "bank") == snap_bank
              and bals == snap_bal and
              conn.execute("SELECT language FROM shops WHERE shop_id = ?",
                           (TEST_SHOP,)).fetchone()["language"] == lang,
              f"pending={leaked}")
        lines = [f"{n}: {'PASS' if ok else 'FAIL'}" + (f" — {d}" if d else "")
                 for n, ok, d in results]
        passed = sum(1 for _, ok, _ in results if ok)
        lines.append(f"SMOKE {passed}/{len(results)} passed")
        return "\n".join(lines)
    finally:
        conn.close()


def _slug(name: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")


def _logo_pixel_check(conn, R, shop_id: str, media: list[dict]):
    """Logo drawn on a statement card with no text overlapping its box."""
    import os

    from PIL import Image
    brand = R.shop_brand(conn, shop_id)
    if not brand.get("logo"):
        return False, "brand has no logo"
    logo_path = brand["logo"]
    if not os.path.exists(logo_path):
        return False, f"logo file missing: {logo_path}"
    png_path = None
    for m in media:
        if m.get("kind") != "image" or "/media/" not in (m.get("url") or ""):
            continue
        token = m["url"].split("/media/")[1].split("?")[0]
        row = conn.execute("SELECT png_path FROM reports WHERE token = ?",
                           (token,)).fetchone()
        if row is not None and row["png_path"] \
                and os.path.exists(row["png_path"]):
            png_path = row["png_path"]
            break
    if png_path is None:
        return False, "no statement png found via media tokens"
    img = Image.open(png_path).convert("RGB")
    W = img.width
    box = (W - 60 - R.LOGO_BOX, R.LOGO_TOP, R.LOGO_BOX)
    crop = img.crop((box[0], box[1], box[0] + box[2], box[1] + box[2]))
    green = R._hex(R.SISI["primary"])
    greens = sum(1 for px in crop.getdata()
                 if abs(px[0] - green[0]) + abs(px[1] - green[1])
                 + abs(px[2] - green[2]) < 24)
    total = box[2] * box[2]
    if greens / total > 0.9:
        return False, "logo box is blank green (logo not drawn?)"
    ctrl = Image.new("RGB", (img.width, img.height), green)
    R._paste_logo(ctrl, logo_path, box)
    exp = ctrl.crop((box[0], box[1], box[0] + box[2], box[1] + box[2]))
    diff = sum(1 for a, b in zip(crop.getdata(), exp.getdata())
               if abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2]) > 24)
    if diff / total > 0.01:
        return False, f"{100 * diff / total:.1f}% of logo box differs (text overlap?)"
    return True, f"{png_path.split('/')[-1]} logo ok, overlap {100 * diff / total:.2f}%"


@app.local_entrypoint()
def main():
    print(smoke.remote())
