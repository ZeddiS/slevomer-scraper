"""Hodinové upozornění: zavolá /v1/cron/notify (vyhodnotí sledování, nové letáky,
končící slevy, týdenní souhrn a pošle push). Hosting nemá cron, takže hodinu tiká
GitHub Actions. V noci (21–8 h) server push neposílá, jen plní schránku v appce.

    python -m slevomer_scraper.notify [--ignore-quiet] [--cleanup]
"""
from __future__ import annotations

import argparse
import json
import sys

from .api import ApiError, SlevomerApi


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ignore-quiet", action="store_true", help="poslat push i v noci (test)")
    ap.add_argument("--cleanup", action="store_true", help="vynutit úklid (jinak jen ve 4 h)")
    args = ap.parse_args()
    try:
        res = SlevomerApi().post("/v1/cron/notify", {"ignore_quiet": args.ignore_quiet, "cleanup": args.cleanup})
    except ApiError as e:
        print(f"Chyba API: {e}", file=sys.stderr)
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=2))
    push = res.get("push") or {}
    return 1 if push.get("failed") and not push.get("sent") else 0


if __name__ == "__main__":
    sys.exit(main())
