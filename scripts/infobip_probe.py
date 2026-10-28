#!/usr/bin/env python3
"""Send ONE Infobip WhatsApp test template to a confirmed number. Live API.

Reads INFOBIP_API_KEY / INFOBIP_BASE_URL / INFOBIP_WHATSAPP_FROM from the
environment (Modal secret locally: never paste keys into chat; export them
or keep them in .env). Sends nothing unless --to and --confirm match
exactly. Trial accounts can only reach verified (signup) numbers.

Example:
    python scripts/infobip_probe.py --to 27710000002 --confirm 27710000002 \\
        --template test_whatsapp_template_en --language en --placeholder Donovan
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--to", required=True, help="recipient digits, e.g. 27710000002")
    ap.add_argument("--confirm", required=True,
                    help="repeat the recipient; must match --to exactly")
    ap.add_argument("--template", default=os.environ.get(
        "INFOBIP_TEMPLATE_TEST_NAME", ""),
        help="approved template name (or INFOBIP_TEMPLATE_TEST_NAME)")
    ap.add_argument("--language", default=os.environ.get(
        "INFOBIP_TEMPLATE_TEST_LANGUAGE", "en"))
    ap.add_argument("--placeholder", default="Donovan",
                    help="single body placeholder value")
    args = ap.parse_args(argv)
    if args.to != args.confirm:
        print("refusing: --to and --confirm differ", file=sys.stderr)
        return 2
    if not args.template:
        print("refusing: no --template (or INFOBIP_TEMPLATE_TEST_NAME)",
              file=sys.stderr)
        return 2
    from app.infobip import InfobipSender

    try:
        InfobipSender().send_template(args.to, args.template, args.language,
                                      [args.placeholder])
    except Exception as e:  # noqa: BLE001 - probe reports, never leaks keys
        print(f"send failed: {e}", file=sys.stderr)
        return 1
    print(f"template {args.template!r} accepted for {args.to}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
