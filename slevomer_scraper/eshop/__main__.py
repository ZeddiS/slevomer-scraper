"""Ceny z e-shopu Rossmann (GitHub Actions 1× denně, workflow eshop.yml).

    python -m slevomer_scraper.eshop                    # běžný běh (limit stránek i času)
    python -m slevomer_scraper.eshop --limit 50         # test
    python -m slevomer_scraper.eshop --dry-run --limit 5

Postup: mapa webu → server řekne, kdy kterou adresu viděl a které jsou spárované s produkty →
nejdřív spárované (starší než 2 dny), pak nikdy neviděné, pak nejdéle neviděné → stránky produktů
pomalu po jedné → dávky na /v1/ingest/eshop → /v1/ingest/eshop/finish (zmizelé, párování).
Když obchod začne odmítat (403/429/CAPTCHA), běh se zastaví — nic neobcházíme.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from datetime import date, timedelta

from .. import off_products
from ..api import ApiError, SlevomerApi
from ..leaflets import net
from . import rossmann


class Disabled(Exception):
    pass


def plan(urls: list[str], seen: dict[str, str], matched: set[str], limit: int) -> list[str]:
    stale = (date.today() - timedelta(days=2)).isoformat()
    first = [u for u in urls if u in matched and seen.get(u, "") < stale]
    never = [u for u in urls if u not in seen]
    rest = sorted((u for u in urls if u in seen and u not in matched), key=lambda u: seen[u])
    out: list[str] = []
    for u in first + never + rest:
        if u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return out


def off_lookup(api: SlevomerApi, names: dict[str, str], limit: int) -> tuple[list[str], int]:
    codes = list(names)[:limit]
    found = 0
    for i in range(0, len(codes), 50):
        chunk = codes[i:i + 50]
        res = off_products.search("code:(" + " OR ".join(chunk) + ")", 1, 100)
        hits = []
        for h in res.get("hits") or []:
            code = str(h.get("code") or "")
            if not (h.get("product_name_cs") or h.get("generic_name_cs")) and names.get(code):
                h["product_name_cs"] = names[code]
            hits.append(h)
        if hits:
            found += api.post("/v1/ingest/products", {"items": hits}).get("saved", 0)
        time.sleep(off_products.PAUSE)
    return codes, found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=int(os.environ.get("SLV_ESHOP_LIMIT", "3000")))
    ap.add_argument("--minutes", type=float, default=float(os.environ.get("SLV_ESHOP_MINUTES", "100")))
    ap.add_argument("--delay", type=float, default=1.2, help="pauza mezi stránkami (s)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-off", dest="off_lookup", action="store_false", help="nedohledávat EAN v Open Food Facts")
    ap.add_argument("--off-limit", type=int, default=3000)
    args = ap.parse_args()

    api = None if args.dry_run else SlevomerApi()
    run_id = None
    if api:
        run_id = api.post("/v1/ingest/run/start", {"source": "eshop_rossmann", "run_ref": os.environ.get("GITHUB_RUN_ID", "manual")})["run_id"]
    stats = {"sitemap": 0, "fetched": 0, "parsed": 0, "no_price": 0, "errors": 0, "saved": 0, "matched": 0, "price_changes": 0}
    status, message = "ok", None
    t0 = time.time()
    try:
        try:
            state = api.post("/v1/ingest/eshop/state", {"chain": rossmann.CHAIN}) if api else {"seen": {}, "matched": []}
        except ApiError as e:
            if e.status == 400:   # admin vypnul e-shop u řetězce (např. na žádost obchodu) → nic nečíst
                raise Disabled() from None
            raise
        urls = rossmann.product_urls()
        stats["sitemap"] = len(urls)
        todo = plan(urls, dict(state.get("seen") or {}), set(state.get("matched") or []), args.limit)
        print(f"Rossmann: {len(urls)} produktů v mapě webu, teď {len(todo)}", flush=True)
        batch: list[dict] = []

        def flush() -> None:
            if not batch:
                return
            if api:
                res = api.post("/v1/ingest/eshop", {"chain": rossmann.CHAIN, "items": batch})
                for k in ("saved", "matched", "price_changes"):
                    stats[k] += res.get(k, 0)
            else:
                for it in batch:
                    print("  ", it)
            batch.clear()

        for i, u in enumerate(todo):
            if time.time() - t0 > args.minutes * 60:
                message = f"Časový limit: zpracováno {i} z {len(todo)}."
                status = "partial"
                break
            try:
                item = rossmann.fetch(u)
                stats["fetched"] += 1
            except net.Blocked as e:
                message = f"Obchod odmítl přístup ({e}) — běh zastaven, nic neobcházíme."
                status = "partial"
                break
            except Exception as e:  # noqa: BLE001 — jedna rozbitá stránka nezastaví běh
                stats["errors"] += 1
                if stats["errors"] > 50 and stats["errors"] > stats["fetched"] // 5:
                    raise RuntimeError(f"Příliš mnoho chyb ({stats['errors']}), poslední: {e}") from e
                continue
            if item:
                stats["parsed"] += 1
                batch.append(item)
            else:
                stats["no_price"] += 1
            if len(batch) >= 200:
                flush()
                print(f"  {i + 1}/{len(todo)} · spárováno {stats['matched']}", flush=True)
            time.sleep(args.delay)
        flush()
        checked: list[str] = []
        if api and args.off_lookup:
            # Nespárované EAN dohledat v Open Food Facts (otevřená data, ODbL) → produkt vznikne z OFF,
            # od obchodu bereme jen cenu. Český název z e-shopu jen tam, kde ho OFF nemá.
            lookup = dict(api.post("/v1/ingest/eshop/state", {"chain": rossmann.CHAIN}).get("lookup") or {})
            checked, found = off_lookup(api, lookup, args.off_limit)
            stats.update({"off_checked": len(checked), "off_found": found})
        if api and stats["fetched"] > 0:
            fin = api.post("/v1/ingest/eshop/finish", {"chain": rossmann.CHAIN, "urls": urls, "checked": checked})
            stats.update({"gone": fin.get("gone", 0), "matched_later": fin.get("matched_now", 0)})
    except Disabled:
        message = "E-shop je u řetězce v adminu vypnutý — nic se nečetlo."
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        status, message = "failed", f"{type(e).__name__}: {e}"[:1500]
    stats["minutes"] = round((time.time() - t0) / 60, 1)
    print(stats, message or "", flush=True)
    if api and run_id:
        api.post("/v1/ingest/run/finish", {"run_id": run_id, "status": status, "stats": stats, "message": message})
    return 1 if status == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
