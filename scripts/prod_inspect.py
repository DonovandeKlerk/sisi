"""Read-only production inspector (Modal run, never deployed).

Usage (from repo root):
    .venv/bin/modal run scripts/prod_inspect.py

Prints aggregate balances and recent moves per shop. SELECT-only: this
script never writes, proposes, or confirms anything.
"""

import modal

image = (
    modal.Image.debian_slim()
    .pip_install_from_requirements("requirements.txt")
    .add_local_dir("app", remote_path="/root/app",
                   ignore=["__pycache__", "*.pyc", "*.pyo"])
)

app = modal.App(
    "sisi-inspect",
    image=image,
    secrets=[modal.secret.Secret.from_name("sisi-env")],
    volumes={"/data": modal.volume.Volume.from_name("sisi-data")},
)


def _r(cents) -> str:
    return f"R{(cents or 0) / 100:,.2f}"


@app.function(timeout=180)
def inspect() -> str:
    import os

    os.environ["SISI_DB"] = "/data/sisi.db"
    from app.db import connect, get_db_path, using_turso

    conn = connect()
    try:
        out = [f"backend: {'turso' if using_turso() else 'sqlite:' + str(get_db_path())}"]
        shops = conn.execute(
            "SELECT shop_id, name, channel, created_at FROM shops ORDER BY created_at"
        ).fetchall()
        out.append(f"shops: {len(shops)}")
        for s in shops:
            sid = s["shop_id"]
            out.append(f"--- {sid} ({s['name']}) ch={s['channel'] or '-'}")
            for acct in ("cash", "bank"):
                bal = conn.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) s FROM cash_moves"
                    " WHERE shop_id=? AND account=? AND reversed=0",
                    (sid, acct)).fetchone()["s"]
                out.append(f"  {acct}: {_r(bal)}")
            kinds = conn.execute(
                "SELECT kind, COUNT(*) n, COALESCE(SUM(amount_cents),0) s FROM cash_moves"
                " WHERE shop_id=? AND reversed=0 GROUP BY kind ORDER BY s DESC",
                (sid,)).fetchall()
            out.append("  cash_moves by kind: " + ", ".join(
                f"{k['kind']} x{k['n']}={_r(k['s'])}" for k in kinds) or "none")
            moves = conn.execute(
                "SELECT created_at, account, kind, amount_cents, note FROM cash_moves"
                " WHERE shop_id=? AND reversed=0 ORDER BY id DESC LIMIT 15",
                (sid,)).fetchall()
            for m in moves:
                out.append(f"    {m['created_at'][:16]} {m['account']}/{m['kind']}"
                           f" {_r(m['amount_cents'])} {m['note'] or ''}"[:140])
            days = conn.execute(
                "SELECT substr(created_at,1,10) d, pay_method, COUNT(*) n,"
                " COALESCE(SUM(total_cents),0) s FROM sales"
                " WHERE shop_id=? AND reversed=0 GROUP BY d, pay_method"
                " ORDER BY d DESC LIMIT 12",
                (sid,)).fetchall()
            out.append("  sales/day: " + "; ".join(
                f"{d['d']} {d['pay_method']} x{d['n']}={_r(d['s'])}" for d in days) or "none")
            acts = conn.execute(
                "SELECT created_at, kind, summary, undone FROM actions"
                " WHERE shop_id=? ORDER BY created_at DESC LIMIT 8",
                (sid,)).fetchall()
            for a in acts:
                out.append(f"    act {a['created_at'][:16]} {a['kind']}"
                           f"{' UNDONE' if a['undone'] else ''} {a['summary']}"[:140])
            custs = conn.execute(
                "SELECT c.name, COALESCE((SELECT SUM(amount_cents) FROM credit_entries e"
                " WHERE e.shop_id=c.shop_id AND e.customer_id=c.id AND e.reversed=0),0)"
                " - COALESCE((SELECT SUM(amount_cents) FROM payments p"
                " WHERE p.shop_id=c.shop_id AND p.customer_id=c.id AND p.reversed=0),0) bal"
                " FROM customers c WHERE c.shop_id=? ORDER BY bal DESC",
                (sid,)).fetchall()
            out.append("  credit: " + ", ".join(
                f"{c['name']}={_r(c['bal'])}" for c in custs[:12]) or "none")
            pends = conn.execute(
                "SELECT action_type, status, created_at FROM pending_actions"
                " WHERE shop_id=? ORDER BY id DESC LIMIT 4",
                (sid,)).fetchall()
            for p in pends:
                out.append(f"    pend {p['created_at'][:16]} {p['action_type']} {p['status']}")
            ph = conn.execute(
                "SELECT kind, COUNT(*) n FROM photo_hashes WHERE shop_id=? GROUP BY kind",
                (sid,)).fetchall()
            out.append("  photos: " + ", ".join(
                f"{r['kind']} x{r['n']}" for r in ph) or "none")
            sq = conn.execute(
                "SELECT created_at, kind, status, attempts, substr(payload_json,1,120) p"
                " FROM send_queue WHERE to_number=? ORDER BY id DESC LIMIT 6",
                (sid,)).fetchall()
            for q in sq:
                out.append(f"    queue {q['created_at'][:16]} {q['kind']}"
                           f" {q['status']} try{q['attempts']} {q['p']}"[:160])
            rep = conn.execute(
                "SELECT month, png_path, token, created_at FROM reports"
                " WHERE shop_id=? ORDER BY created_at DESC LIMIT 6",
                (sid,)).fetchall()
            for r in rep:
                tok = r["token"] or ""
                out.append(f"    report {r['created_at'][:16]} {r['month']}"
                           f" {r['png_path']} tok={tok[:8]}")
                if r["month"] == "daily-2026-10-30":
                    out.append(f"    DAILY30_TOKEN={tok}")
        import os as _os
        out.append("base=" + _os.environ.get("SISI_PUBLIC_URL", "").rstrip("/"))
        for k in ("SISI_PROVIDER", "INFOBIP_WHATSAPP_FROM", "WHAPI_NUMBER",
                  "VONAGE_WHATSAPP_FROM", "SISI_MODEL"):
            out.append(f"{k}={_os.environ.get(k, '')}")
        return "\n".join(out)
    finally:
        conn.close()


@app.local_entrypoint()
def main():
    print(inspect.remote())
