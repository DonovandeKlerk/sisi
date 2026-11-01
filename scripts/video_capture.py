"""Capture REAL bot footage for the demo video (Modal run, never deployed).

Usage (from repo root), one part per run:
    .venv/bin/modal run scripts/video_capture.py --shop <live-shop> --only chat1
    ... --only multi | voice | book | chat3 | setup | draft | zulutext | zulvoice

Drives the real handlers on the test shop and dumps replies, buttons and
media to /data/videocap (cap-<part>.json + media/). Sends NO WhatsApp
traffic. Each part cleans its own chatter; saves are undone, pendings
cancelled, language restored. Intended lasting changes: Nomvula's R99.50
settlement (so 'all debtors' is 4) and statement/daily report artifacts.
"""

import modal

image = (
    modal.Image.debian_slim()
    .apt_install("ffmpeg", "fonts-dejavu-core")
    .pip_install_from_requirements("requirements.txt")
    .add_local_dir("app", remote_path="/root/app",
                   ignore=["__pycache__", "*.pyc", "*.pyo"])
    .add_local_dir("seed", remote_path="/root/seed",
                   ignore=["__pycache__", "*.pyc", "*.pyo"])
    .add_local_file("tests/fixtures/book_page.png",
                    remote_path="/root/book_page.png")
    .add_local_file("/Users/donovandeklerk/sisi-video/cap/book-real-30oct.png",
                    remote_path="/root/book_real.png")
    .add_local_file("samples/voice_sale_cash.ogg",
                    remote_path="/root/voice_sale_cash.ogg")
)

app = modal.App(
    "sisi-videocap",
    image=image,
    secrets=[modal.secret.Secret.from_name("sisi-env")],
    volumes={"/data": modal.volume.Volume.from_name("sisi-data")},
)

DEFAULT_SHOP = "27710000002"
TEST_NAME = "Bongi’s Spaza Shop"  # curly apostrophe, as stored
THROWAWAY = "27719998877"


@app.function(timeout=900)
def capture(shop: str = "", only: str = "all") -> str:
    import json
    import os
    import shutil
    from pathlib import Path

    shop_id = (shop or DEFAULT_SHOP).strip()  # live id via --shop, never git
    os.environ["SISI_DB"] = "/data/sisi.db"
    os.environ["SISI_REPORTS"] = "/data/reports"
    os.environ["SISI_LOGOS"] = "/data/logos"
    from app import reports as R
    from app.agent import (handle_photo, handle_text, handle_voice,
                           take_buttons, take_lists, take_media)
    from app.db import connect
    from app.i18n import doc_lang as _doc_lang

    cap = Path("/data/videocap")
    cap.mkdir(parents=True, exist_ok=True)
    (cap / "red").mkdir(exist_ok=True)
    turns: list[dict] = []
    files: dict[str, str] = {}

    conn = connect()
    try:
        shop = conn.execute("SELECT name FROM shops WHERE shop_id = ?",
                            (shop_id,)).fetchone()
        if shop is None or shop["name"] != TEST_NAME:
            return "REFUSED: test shop missing or renamed"
        from app.tools import get_pending
        turn_max = conn.execute(
            "SELECT COALESCE(MAX(id),0) m FROM conversation_turns WHERE shop_id = ?",
            (shop_id,)).fetchone()["m"]

        def text(shot: str, owner_text: str) -> dict:
            take_media(shop_id)
            take_lists(shop_id)
            take_buttons(shop_id)
            msg, buttons = handle_text(conn, shop_id, owner_text)
            pend = get_pending(conn, shop_id)
            t = {"shot": shot, "owner": {"type": "text", "text": owner_text},
                 "sisi": msg, "buttons": buttons or [],
                 "lists": take_lists(shop_id),
                 "pending": (pend["action_type"] + ": " + (pend["preview_text"] or "")[:500])
                 if pend else None}
            turns.append(t)
            return t

        def snap_media(tag: str) -> list[dict]:
            got = []
            for m in take_media(shop_id):
                url = m.get("url") or ""
                token = url.split("/media/")[1].split("?")[0] if "/media/" in url else ""
                row = conn.execute("SELECT png_path FROM reports WHERE token = ?",
                                   (token,)).fetchone() if token else None
                src = (row["png_path"] or "") if row else ""
                dst = ""
                if src and Path(src).exists():
                    dst = str(cap / f"{tag}-{Path(src).name}")
                    shutil.copy(src, dst)
                got.append({"kind": m.get("kind"), "caption": m.get("caption"),
                            "filename": m.get("filename"), "file": dst})
            return got

        def brand_red():
            brand = R.shop_brand(conn, shop_id)
            return dict(brand, address="", phone="")

        if only in ("all", "chat1"):
            # Nomvula settles (idempotent; intended).
            row = conn.execute("SELECT id FROM customers WHERE shop_id = ? AND name = ?",
                               (shop_id, "Nomvula")).fetchone()
            if row is not None:
                from app.tools import balance_cents
                if balance_cents(conn, shop_id, row["id"]) > 0:
                    text("pay_nomvula", "Nomvula paid 99.50")
                    text("pay_nomvula_yes", "Yes")
            text("hi", "hi")
            text("sale1", "sold 2 bread to Sipho on credit")
            last = turns[-1]["sisi"] or ""
            if "Which one" in last or "which one" in last:
                text("sale1_which", "Albany")
            if get_pending(conn, shop_id) is None:
                text("sale1_retry", "sold 2 albany bread to Sipho on credit")
            text("sale1_yes", "Yes")
            text("sale1_undo", "undo")
            text("af_sale", "verkoop 1 melk kontant")
            if get_pending(conn, shop_id) is not None:
                text("af_sale_no", "No")

        if only in ("all", "multi"):
            t = text("multi", "sold 2 bread and send Thandi her statement")
            t["media"] = snap_media("multi")
            if get_pending(conn, shop_id) is not None:
                text("multi_no", "No")
            take_media(shop_id)

        if only in ("all", "voice"):
            audio = Path("/root/voice_sale_cash.ogg").read_bytes()
            heard, reply, _b = handle_voice(conn, shop_id, audio)
            pend = get_pending(conn, shop_id)
            turns.append({"shot": "voice",
                          "owner": {"type": "voice", "file": "voice_sale_cash.ogg"},
                          "heard": heard, "sisi": reply,
                          "pending": (pend["action_type"]) if pend else None})
            if get_pending(conn, shop_id) is not None:
                text("voice_no", "No")

        if only in ("all", "voiceyes"):
            audio = Path("/root/voice_sale_cash.ogg").read_bytes()
            heard, reply, _b = handle_voice(conn, shop_id, audio)
            pend = get_pending(conn, shop_id)
            turns.append({"shot": "voiceyes",
                          "owner": {"type": "voice", "file": "voice_sale_cash.ogg"},
                          "heard": heard, "sisi": reply,
                          "pending": (pend["action_type"]) if pend else None})
            if get_pending(conn, shop_id) is not None:
                text("voiceyes_yes", "Yes")
                text("voiceyes_undo", "undo")

        if only in ("all", "book"):
            import hashlib
            from app.tools import answer_book_line, first_book_question
            photo = Path("/root/book_page.png").read_bytes()
            digest = hashlib.sha256(photo).hexdigest()
            conn.execute("DELETE FROM photo_hashes WHERE shop_id = ? AND hash = ?",
                         (shop_id, digest))
            conn.commit()
            take_media(shop_id)
            _doc, reply, _b = handle_photo(conn, shop_id, photo, "")
            pend = get_pending(conn, shop_id)
            turns.append({"shot": "book",
                          "owner": {"type": "photo", "file": "book_page.png"},
                          "sisi": reply,
                          "pending": (pend["action_type"]) if pend else None})
            (cap / "book_page.png").write_bytes(photo)
            files["book_page"] = str(cap / "book_page.png")
            if pend is not None and pend["action_type"] == "book_page":
                for _ in range(6):
                    payload = json.loads(
                        get_pending(conn, shop_id)["payload_json"])
                    entry = first_book_question(payload)
                    if entry is None or not entry.get("options"):
                        break
                    answer_book_line(conn, shop_id, entry["line_no"],
                                     entry["options"][0])
                text("book_yes", "Yes")
                day = conn.execute(
                    "SELECT month FROM reports WHERE shop_id = ? AND month LIKE 'daily-%'"
                    " ORDER BY created_at DESC LIMIT 1", (shop_id,)).fetchone()
                if day is not None:
                    row = conn.execute(
                        "SELECT png_path FROM reports WHERE shop_id = ? AND month = ?",
                        (shop_id, day["month"])).fetchone()
                    if row is not None and Path(row["png_path"]).exists():
                        dst = str(cap / f"daily-{day['month'][6:]}.pdf")
                        shutil.copy(row["png_path"], dst)
                        files["daily_pdf"] = dst
                text("book_undo", "undo book")
            conn.execute("DELETE FROM photo_hashes WHERE shop_id = ? AND hash = ?",
                         (shop_id, digest))
            conn.commit()

        if only in ("all", "chat3"):
            from app.schedule import _nudge_text, _snapshot_data
            t = text("statements_all", "send all debtors their statements")
            t["media"] = snap_media("stmt")
            red = brand_red()
            for name in ("Sipho", "Thandi", "Mama Zodwa", "Bongani"):
                with _doc_lang("en"):
                    data = R.statement_data(conn, shop_id, name)
                    pages = R.render_statement_pngs(
                        data, red, str(cap / "red" / f"stmt-{name.lower().replace(' ', '-')}-p"))
                    pdf = str(cap / "red" / f"stmt-{name.lower().replace(' ', '-')}.pdf")
                    R.render_statement_pdf(data, red, pdf)
                    files[f"card_{name}"] = pages[0]
                    files[f"pdf_{name}"] = pdf
            before = conn.execute(
                "SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                (shop_id,)).fetchone()["n"]
            text("remind", "remind me to collect R68 from Sipho tomorrow at 9am")
            if get_pending(conn, shop_id) is not None:
                text("remind_no", "No")
            after = conn.execute(
                "SELECT COUNT(*) n FROM reminders WHERE shop_id = ?",
                (shop_id,)).fetchone()["n"]
            turns.append({"shot": "remind_clean", "before": before, "after": after})
            text("lang_xh", "language isiXhosa")
            lang = conn.execute("SELECT language FROM shops WHERE shop_id = ?",
                                (shop_id,)).fetchone()["language"]
            if lang != "xh":
                text("lang_xh_speak", "speak isiXhosa")
            text("lang_en", "speak English")
            turns.append({"shot": "nudge",
                          "sisi": _nudge_text(conn, shop_id, "2026-11-01")})
            with _doc_lang("en"):
                data = _snapshot_data(conn, shop_id, "2026-11-01")
                snap = str(cap / "red" / "snapshot-week.png")
                R.render_snapshot_png(data, red, snap, "2026-11-01")
                files["snapshot"] = snap

        if only in ("all", "setup"):
            from app.setup import setup_step
            from seed.seed_demo import wipe_shop
            wipe_shop(conn, THROWAWAY)
            conn.execute("DELETE FROM shops WHERE shop_id = ?", (THROWAWAY,))
            conn.commit()
            brand = R.shop_brand(conn, shop_id)
            logo_bytes = Path(brand["logo"]).read_bytes() if brand.get("logo") else b""
            answers = {1: "Bongi’s Spaza Shop", 2: "Main Road, Amanzimtoti",
                       3: THROWAWAY, 5: "1500 cash", 6: "English"}
            setup_turns = []
            msg, _ = handle_text(conn, THROWAWAY, "hi")
            setup_turns.append({"owner": "hi", "sisi": msg})
            for _ in range(12):
                step = setup_step(conn, THROWAWAY)
                if step is None:
                    break
                if step == 4 and logo_bytes:
                    _d, reply, _b = handle_photo(conn, THROWAWAY, logo_bytes, "logo")
                    setup_turns.append({"owner": "[logo photo]", "sisi": reply})
                    if setup_step(conn, THROWAWAY) == 4:
                        msg, _ = handle_text(conn, THROWAWAY, "Yes")
                        setup_turns.append({"owner": "Yes", "sisi": msg})
                else:
                    msg, _ = handle_text(conn, THROWAWAY, answers.get(step, "ok"))
                    setup_turns.append({"owner": answers.get(step, "ok"), "sisi": msg})
            turns.append({"shot": "setup", "turns": setup_turns})
            wipe_shop(conn, THROWAWAY)
            conn.execute("DELETE FROM shops WHERE shop_id = ?", (THROWAWAY,))
            conn.commit()

        if only in ("all", "bookreal1"):
            import hashlib
            photo = Path("/root/book_real.png").read_bytes()
            digest = hashlib.sha256(photo).hexdigest()
            conn.execute("DELETE FROM photo_hashes WHERE shop_id = ? AND hash = ?",
                         (shop_id, digest))
            conn.commit()
            take_media(shop_id)
            _doc, reply, _b = handle_photo(conn, shop_id, photo, "")
            pend = get_pending(conn, shop_id)
            entry = {"shot": "bookreal", "owner": {"type": "photo", "file": "book-real-30oct.png"},
                     "sisi": reply,
                     "pending": (pend["action_type"]) if pend else None}
            if pend is not None and pend["action_type"] == "book_page":
                import json as _j
                payload = _j.loads(pend["payload_json"])
                entry["date"] = payload.get("date")
                entry["bound"] = {k: len(payload.get(k) or [])
                                  for k in ("cash", "credit", "card", "payments",
                                            "supply", "waste")}
                entry["flagged"] = [
                    {"line_no": e.get("line_no"), "kind": e.get("kind"),
                     "text": (e.get("text") or "")[:80],
                     "options": e.get("options"), "need": e.get("need"),
                     "prod": (e.get("price_warn") or {}).get("prod")}
                    for e in payload.get("flagged", [])]
            turns.append(entry)
            # Leave the pending open for bookreal2; skip chatter cleanup.

        if only in ("all", "bookreal2"):
            import hashlib
            import json as _j
            from app.tools import (answer_book_line, answer_supply_number,
                                   first_book_question)
            photo = Path("/root/book_real.png").read_bytes()
            digest = hashlib.sha256(photo).hexdigest()
            plan = [(4, "Coke"), (4, "Coke"), (6, "Albany"), (7, "Albany"),
                    (9, "Coke"), (9, "Coke"), (11, "Albany")]
            for line_no, want in plan:
                pend = get_pending(conn, shop_id)
                if pend is None or pend["action_type"] != "book_page":
                    turns.append({"shot": "bookreal2", "sisi": "NO PENDING"})
                    break
                payload = _j.loads(pend["payload_json"])
                entry = first_book_question(payload)
                if entry is None:
                    break
                if entry.get("need"):
                    msg = answer_supply_number(conn, shop_id, entry["line_no"],
                                               entry["need"], 8)
                    turns.append({"shot": f"ans-{entry['line_no']}-{entry['need']}",
                                  "owner": {"type": "tap", "text": "8"},
                                  "sisi": msg[:400]})
                    continue
                opts = entry.get("options") or []
                pick = next((o for o in opts if want.lower() in str(o).lower()), None)
                if pick is None:
                    turns.append({"shot": f"ans-{entry['line_no']}",
                                  "sisi": f"NO MATCH for {want} in {opts}"})
                    break
                msg = answer_book_line(conn, shop_id, entry["line_no"], pick)
                turns.append({"shot": f"ans-{entry['line_no']}",
                              "owner": {"type": "tap", "text": str(pick)},
                              "sisi": msg[:500]})
            for _ in range(4):
                pend = get_pending(conn, shop_id)
                if pend is None:
                    break
                payload = _j.loads(pend["payload_json"])
                entry = first_book_question(payload)
                if entry is None:
                    break
                if entry.get("need"):
                    msg = answer_supply_number(conn, shop_id, entry["line_no"],
                                               entry["need"], 8)
                    turns.append({"shot": f"ans-{entry['line_no']}-{entry['need']}",
                                  "owner": {"type": "tap", "text": "8"},
                                  "sisi": msg[:400]})
                    continue
                turns.append({"shot": "leftover",
                              "sisi": str(entry)[:200]})
                break
            text("bookreal_yes", "Yes")
            text("bookreal_undo", "undo book")
            conn.execute("DELETE FROM photo_hashes WHERE shop_id = ? AND hash = ?",
                         (shop_id, digest))
            conn.commit()

        if only in ("all", "draft"):
            t = text("draft", "Draft a reminder for Thandi")
            t["media"] = snap_media("draft")
            if get_pending(conn, shop_id) is not None:
                text("draft_no", "No")
            take_media(shop_id)

        if only in ("all", "zulutext"):
            # Typed isiZulu sale (no honest Zulu speech available): real
            # understanding + confirmation, undone right after.
            from app.tools import get_pending as _gp
            text("zulu_sale",
                 "Ngithengise izinkwa ezimbili kuSipho ngesikweletu")
            last = turns[-1]["sisi"] or ""
            if "Which one" in last or "which one" in last:
                text("zulu_which", "Albany")
            if _gp(conn, shop_id) is None:
                text("zulu_retry",
                     "Ngithengise izinkwa ezimbili ze-Albany kuSipho "
                     "ngesikweletu")
            text("zulu_yes", "Yes")
            text("zulu_undo", "undo")

        if only in ("all", "zulvoice"):
            audio = Path("/root/zu_sale.ogg").read_bytes()
            heard, reply, _b = handle_voice(conn, shop_id, audio)
            pend = get_pending(conn, shop_id)
            turns.append({"shot": "zulvoice",
                          "owner": {"type": "voice", "file": "zu_sale.ogg"},
                          "heard": heard, "sisi": reply,
                          "pending": (pend["action_type"]) if pend else None})
            (cap / "zu_sale.ogg").write_bytes(audio)
            files["zu_audio"] = str(cap / "zu_sale.ogg")
            if get_pending(conn, shop_id) is not None:
                text("zulvoice_yes", "Yes")
                text("zulvoice_undo", "undo")

        if only in ("cleanup",):
            rows = conn.execute(
                "SELECT id, summary, due_at FROM reminders WHERE shop_id = ?"
                " AND substr(created_at, 1, 10) = '2026-11-01'", (shop_id,)).fetchall()
            for r in rows:
                turns.append({"shot": "cleanup",
                              "sisi": f"deleted reminder {r['id']}: {r['summary']} @ {r['due_at']}"})
                conn.execute("DELETE FROM reminders WHERE id = ?", (r["id"],))
            conn.commit()

        conn.execute("DELETE FROM conversation_turns WHERE shop_id = ? AND id > ?",
                     (shop_id, turn_max))
        conn.commit()
        (cap / f"cap-{only}.json").write_text(json.dumps(
            {"turns": turns, "files": files}, indent=1, ensure_ascii=False))
        pend = get_pending(conn, shop_id)
        return (f"[{only}] captured {len(turns)} turns, {len(files)} files;"
                f" pending={'none' if pend is None else pend['action_type']}")
    finally:
        conn.close()


@app.local_entrypoint()
def main(shop: str = "", only: str = "all"):
    print(capture.remote(shop, only))
