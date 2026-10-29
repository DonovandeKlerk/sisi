"""Live planner probes (manual, uses the configured dev model).

Runs representative messages through interpret() for real, printing
each plan plus latency and token usage. Fictional seed data only —
never run against a live shop DB.

Usage: SISI_MODEL=muse-spark-1.3-contributor .venv/bin/python scripts/eval_live.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config
from app.db import init_db
from app.interpreter import interpret
from app.tools import ensure_shop, set_float
from seed.seed_demo import seed_demo

SHOP = "27719999999"

CASES = [
    # (label, message) — paraphrases deliberately differ from suite wording
    ("milk-cash-reminder",
     "The milk delivery gets here in about fifteen minutes, what petty cash "
     "have we got to pay for it, and please remind me in five minutes"),
    ("customer-order",
     "Sipho asked for 2 loaves of bread and 5 chappies"),
    ("supplier-not-order",
     "I ordered milk from the supplier and it arrives soon"),
    ("cash-question",
     "how much cash do we have in the till right now?"),
    ("affordability",
     "can we afford to pay R200 for the delivery?"),
    ("statement",
     "send me a statement for Thandi so I can forward it"),
    ("restock",
     "what must I buy tomorrow?"),
    ("bare-reminder",
     "remind me in 10 minutes"),
    ("followup-shape",
     "to prepare for the delivery"),
    ("greeting", "good morning sisi"),
]


def main() -> int:
    print(f"model: {config.model_name()}")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    seed_demo(SHOP, conn)
    ensure_shop(conn, SHOP, "Eval Spaza")
    set_float(conn, SHOP, {"cash": 120000, "petty": 30000})
    from app.setup import skip_setup
    skip_setup(conn, SHOP)
    tot_s, tot_lat = 0, 0.0
    for label, text in CASES:
        start = time.time()
        try:
            plan = interpret(conn, SHOP, text)
        except Exception as e:  # noqa: BLE001 - probe harness
            print(f"\n## {label}\n  ERROR: {type(e).__name__}: {e}")
            continue
        lat = time.time() - start
        tot_lat += lat
        tasks = "; ".join(
            f"{t['kind']} {t['args']}" for t in plan.get("tasks") or [])
        print(f"\n## {label} ({lat:.1f}s)")
        print(f"  understood: {plan.get('understood')}")
        print(f"  tasks: {tasks or '-'}")
        print(f"  question: {plan.get('question')}")
    print(f"\n{len(CASES)} cases, total {tot_lat:.1f}s, "
          f"avg {tot_lat / len(CASES):.1f}s")
    print(f"(usage appended to {os.path.abspath('token_usage.log')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
