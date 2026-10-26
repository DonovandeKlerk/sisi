"""Terminal chat with Sisi using the real model: python -m sisi.chat [--shop ID]."""

from __future__ import annotations

import argparse
import sys

from app.agent import handle_text
from app.db import connect, init_db


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with Sisi in the terminal.")
    parser.add_argument("--shop", default="27710000001", help="shop_id (owner phone number)")
    args = parser.parse_args()
    conn = connect()
    init_db(conn)
    print(f"Sisi chat — shop {args.shop}. Type, Ctrl-D to quit.")
    try:
        for line in sys.stdin:
            text = line.strip()
            if not text:
                continue
            try:
                reply, buttons = handle_text(conn, args.shop, text)
            except RuntimeError as e:
                print(f"[config] {e}")
                continue
            print(f"Sisi: {reply}")
            if buttons:
                print("[" + "] [".join(buttons) + "]")
    except KeyboardInterrupt:
        pass
    finally:
        print("Sharp sharp. Bye!")


if __name__ == "__main__":
    main()
