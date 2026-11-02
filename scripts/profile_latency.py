#!/usr/bin/env python3
"""Profile end-to-end reply latency (handle start → reply queued).

Runs three shapes against a seeded shop with the model stubbed:
  1) plain text that hits the planner (slow path, stubbed)
  2) button/menu tap (deterministic, no LLM)
  3) voice note (transcribe stubbed)

Reports median ms for each. Overview deferral is measured separately.
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="sisi-lat-")
    os.environ["SISI_DB"] = str(Path(tmp) / "lat.db")
    os.environ["SISI_REPORTS"] = str(Path(tmp) / "reports")
    os.environ["SISI_LOGOS"] = str(Path(tmp) / "logos")
    os.environ["SISI_PUBLIC_URL"] = "https://sisi.example"
    os.environ["WHAPI_ALLOW_WRITES"] = "1"

    import app.llm as llm
    from app.agent import DEFER_OVERVIEW, handle_text, take_media, take_lists
    from app.db import connect, init_db
    from app.setup import skip_setup
    from seed.seed_demo import DEMO_SHOP_ID, seed_demo

    SHOP = DEMO_SHOP_ID
    seed_demo(SHOP)
    conn = connect()
    skip_setup(conn, SHOP)
    conn.execute("UPDATE shops SET language = 'en' WHERE shop_id = ?", (SHOP,))
    conn.commit()

    # Stub planner so "text" path doesn't hit the network.
    llm.chat = lambda *a, **k: {
        "understood": "stub", "question": "", "tasks": [],
        "raw": "{}",
    }

    def timed(fn, n=7):
        samples = []
        for _ in range(n):
            take_media(SHOP)
            take_lists(SHOP)
            t0 = time.perf_counter()
            fn()
            samples.append((time.perf_counter() - t0) * 1000)
        return samples

    def text_path():
        handle_text(conn, SHOP, "sold 1 coke")

    def button_path():
        handle_text(conn, SHOP, "menu")

    def deferred_overview_path():
        token = DEFER_OVERVIEW.set(True)
        try:
            handle_text(conn, SHOP, "float R500")
        finally:
            DEFER_OVERVIEW.reset(token)

    def sync_overview_path():
        handle_text(conn, SHOP, "float R501")

    results = {
        "text (stubbed planner)": timed(text_path),
        "button/menu tap": timed(button_path),
        "mutating + deferred overview mark": timed(deferred_overview_path),
        "mutating + sync overview render": timed(sync_overview_path),
    }
    print(f"Latency profile (ms) — DB={os.environ['SISI_DB']}")
    for name, samples in results.items():
        print(f"  {name}: median={statistics.median(samples):6.1f}  "
              f"mean={statistics.mean(samples):6.1f}  "
              f"min={min(samples):6.1f}  max={max(samples):6.1f}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
