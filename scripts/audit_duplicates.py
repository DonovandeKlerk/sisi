#!/usr/bin/env python3
"""Audit a Sisi shop DB for duplicate stock/money from receipt-intake gaps.

Read-only. Three checks (all shop-scoped):

(A) stock/stock_intake actions (undone) with unreversed purchase
    cash_moves or supplier_moves on the same action_id: suspected
    pre-fix auto-posts. kind=stock_intake is certainly a photo/text
    intake that paid on its own; kind=stock may also be a deliberate
    post-fix explicit add_stock payment, so it is only possible.
(B) unreversed receipts whose stock lines overlap restock moves for
    the same product within ±3 days from a different action:
    suspected double-stock pairs (intake + receipt both added).
(C) two or more unreversed receipts sharing one image_hash:
    the same slip filed twice.

Usage:
    python scripts/audit_duplicates.py --db data/sisi.db [--shop 277...] [--json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _rand(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}R{cents // 100}.{cents % 100:02d}"


def _audit_autoposts(conn: sqlite3.Connection, shop: str | None) -> list[dict]:
    q = ("SELECT * FROM actions WHERE undone = 0"
         " AND kind IN ('stock', 'stock_intake')")
    params: list = []
    if shop:
        q += " AND shop_id = ?"
        params.append(shop)
    q += " ORDER BY shop_id, created_at"
    out = []
    for act in conn.execute(q, params).fetchall():
        cash = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) s FROM cash_moves"
            " WHERE shop_id = ? AND action_id = ? AND kind = 'purchase'"
            " AND reversed = 0",
            (act["shop_id"], act["action_id"])).fetchone()["s"]
        sup = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) s FROM supplier_moves"
            " WHERE shop_id = ? AND action_id = ? AND reversed = 0",
            (act["shop_id"], act["action_id"])).fetchone()["s"]
        if not cash and not sup:
            continue
        out.append({
            "shop": act["shop_id"],
            "action_id": act["action_id"],
            "kind": act["kind"],
            "summary": act["summary"] or "",
            "date": (act["created_at"] or "")[:10],
            "cash_cents": abs(cash),
            "supplier_cents": sup,
            "confidence": ("certain intake auto-post"
                           if act["kind"] == "stock_intake"
                           else "possible (intake or explicit add_stock)"),
        })
    return out


def _receipt_stock_lines(conn: sqlite3.Connection, shop: str,
                         data: dict) -> list[dict]:
    lines = []
    for e in (data or {}).get("lines") or []:
        if not isinstance(e, dict) or e.get("unresolved"):
            continue
        pid = e.get("product_id")
        units = e.get("units")
        if pid is None and (e.get("name") or "").strip():
            row = conn.execute(
                "SELECT id FROM products WHERE shop_id = ? AND name = ?",
                (shop, e["name"].strip())).fetchone()
            pid = row["id"] if row else None
        if pid is None or not units:
            continue
        row = conn.execute("SELECT name FROM products WHERE id = ?",
                           (pid,)).fetchone()
        lines.append({"product_id": pid,
                      "product": row["name"] if row else e.get("name", "?"),
                      "units": units})
    return lines


def _audit_double_stock(conn: sqlite3.Connection,
                        shop: str | None) -> list[dict]:
    q = ("SELECT * FROM receipts WHERE reversed = 0")
    params: list = []
    if shop:
        q += " AND shop_id = ?"
        params.append(shop)
    q += " ORDER BY shop_id, created_at"
    out = []
    for r in conn.execute(q, params).fetchall():
        try:
            data = json.loads(r["data_json"] or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        r_ts = _parse_ts(r["created_at"])
        for ln in _receipt_stock_lines(conn, r["shop_id"], data):
            moves = conn.execute(
                "SELECT * FROM stock_moves WHERE shop_id = ?"
                " AND product_id = ? AND reason = 'restock'"
                " AND action_id != ?",
                (r["shop_id"], ln["product_id"], r["action_id"])).fetchall()
            near = []
            for m in moves:
                m_ts = _parse_ts(m["created_at"])
                if r_ts is not None and m_ts is not None \
                        and abs((m_ts - r_ts).total_seconds()) > 3 * 86400:
                    continue
                near.append({"action_id": m["action_id"],
                             "qty": m["delta_qty"],
                             "date": (m["created_at"] or "")[:10]})
            if near:
                out.append({
                    "shop": r["shop_id"],
                    "receipt_id": r["id"],
                    "supplier": r["supplier"] or "",
                    "ref": r["receipt_no"] or "",
                    "date": (r["created_at"] or "")[:10],
                    "product": ln["product"],
                    "receipt_qty": ln["units"],
                    "receipt_total_cents": r["total_cents"],
                    "moves": near,
                })
    return out


def _audit_reused_photos(conn: sqlite3.Connection,
                         shop: str | None) -> list[dict]:
    q = ("SELECT shop_id, image_hash, COUNT(*) n FROM receipts"
         " WHERE reversed = 0 AND image_hash != ''")
    params: list = []
    if shop:
        q += " AND shop_id = ?"
        params.append(shop)
    q += " GROUP BY shop_id, image_hash HAVING COUNT(*) >= 2"
    out = []
    for g in conn.execute(q, params).fetchall():
        rows = conn.execute(
            "SELECT id, supplier, receipt_no FROM receipts"
            " WHERE shop_id = ? AND image_hash = ? AND reversed = 0"
            " ORDER BY id",
            (g["shop_id"], g["image_hash"])).fetchall()
        out.append({
            "shop": g["shop_id"],
            "image_hash": g["image_hash"],
            "receipt_ids": [r["id"] for r in rows],
            "suppliers": [r["supplier"] for r in rows],
            "refs": [r["receipt_no"] for r in rows],
        })
    return out


def audit(conn: sqlite3.Connection, shop: str | None = None) -> dict:
    """Run all three checks; returns {"autoposts", "double_stock", "reused_photos"}."""
    return {
        "autoposts": _audit_autoposts(conn, shop),
        "double_stock": _audit_double_stock(conn, shop),
        "reused_photos": _audit_reused_photos(conn, shop),
    }


def format_human(report: dict, db: str, shop: str | None) -> str:
    lines = [f"Sisi duplicate audit: {db}"
             + (f" (shop {shop})" if shop else "")]
    auto = report["autoposts"]
    lines.append(f"(A) suspected pre-fix auto-posts: {len(auto)}")
    for f in auto:
        bits = []
        if f["cash_cents"]:
            bits.append(f"cash {_rand(f['cash_cents'])}")
        if f["supplier_cents"]:
            bits.append(f"supplier {_rand(f['supplier_cents'])}")
        lines.append(f"  • {f['date']} {f['shop']} {f['kind']}"
                     f" {f['action_id']} “{f['summary']}” {' '.join(bits)}"
                     f" [{f['confidence']}]")
    dbl = report["double_stock"]
    lines.append(f"(B) suspected double-stock pairs: {len(dbl)}")
    for f in dbl:
        total = _rand(f["receipt_total_cents"] or 0)
        for m in f["moves"]:
            lines.append(f"  • receipt {f['ref'] or '?'} {f['supplier']}"
                         f" {f['date']} {total} × {f['product']}:"
                         f" slip {f['receipt_qty']:g} vs {m['action_id']}"
                         f" {m['qty']:g} on {m['date']}")
    reu = report["reused_photos"]
    lines.append(f"(C) reused receipt photos: {len(reu)}")
    for f in reu:
        refs = ", ".join(f"{s} {r or '?'}".strip()
                         for s, r in zip(f["suppliers"], f["refs"]))
        lines.append(f"  • {f['shop']} {f['image_hash'][:16]}…"
                     f" receipts {f['receipt_ids']} ({refs})")
    n = len(auto) + len(dbl) + len(reu)
    lines.append("No issues found." if n == 0 else f"Total {n} finding(s).")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit a Sisi DB for duplicates.")
    parser.add_argument("--db", required=True, help="Path to the shop SQLite DB.")
    parser.add_argument("--shop", default=None, help="Only audit one shop_id.")
    parser.add_argument("--json", action="store_true",
                        help="Emit the raw findings as JSON.")
    args = parser.parse_args(argv)
    try:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        print(f"cannot open {args.db} read-only: {e}", file=sys.stderr)
        return 2
    conn.row_factory = sqlite3.Row
    try:
        report = audit(conn, args.shop)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_human(report, args.db, args.shop))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
