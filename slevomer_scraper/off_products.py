"""Import produktů prodávaných v ČR z Open Food Facts → Slevoměr API.

Zdroj: vyhledávání search.openfoodfacts.org (Elasticsearch, max 10 000 výsledků na dotaz),
proto se dotaz dělí podle prefixu čárového kódu, dokud každá část nemá < 9 000 produktů.
Klasické /api/v2/search bývá přetížené („Page temporarily unavailable“), proto ho nepoužíváme.
Neznámé kódy navíc dohledává server živě při skenu (/v1/products/barcode/{ean}).

    python -m slevomer_scraper.off_products              # vše a nahrát
    python -m slevomer_scraper.off_products --limit 300  # jen prvních N (test)

Data Open Food Facts © přispěvatelé, ODbL; obrázky CC BY-SA.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .api import SlevomerApi

SEARCH = "https://search.openfoodfacts.org/search"
UA = "SlevomerScraper/0.2 (+https://slevomer.zeddis.xyz)"
FIELDS = ("code,product_name,product_name_cs,generic_name_cs,brands,quantity,product_quantity,product_quantity_unit,"
          "categories_tags,image_front_url,nutriscore_grade,nova_group,allergens_tags,labels_tags,"
          "ingredients_text_cs,ingredients_text,nutriments,countries_tags")
COUNTRY = 'countries_tags:"en:czech-republic"'
PAGE = 100
BATCH = 250
PAUSE = 1.0   # slušnost vůči OFF


def search(q: str, page: int, page_size: int = PAGE, fields: str = FIELDS) -> dict:
    url = SEARCH + "?" + urllib.parse.urlencode({"q": q, "page": page, "page_size": page_size, "fields": fields, "langs": "cs"})
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"  OFF chyba ({e}), zkouším znovu…", flush=True)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("Open Food Facts search nedostupné")


def partitions(prefix: str = "") -> list[str]:
    """Prefixy kódů tak, aby každý dotaz měl < 9 000 výsledků."""
    q = COUNTRY + (f" AND code:{prefix}*" if prefix else "")
    count = search(q, 1, 1, "code").get("count", 0)
    time.sleep(PAUSE)
    if count == 0:
        return []
    if count < 9000 or len(prefix) >= 6:
        return [prefix]
    out: list[str] = []
    for d in "0123456789":
        out += partitions(prefix + d)
    return out


def fetch_all(limit: int | None = None):
    for prefix in partitions():
        q = COUNTRY + (f" AND code:{prefix}*" if prefix else "")
        page = 1
        while True:
            res = search(q, page)
            hits = res.get("hits") or []
            print(f"  prefix {prefix or '*'} strana {page}/{res.get('page_count')}: {len(hits)}", flush=True)
            for h in hits:
                yield h
                if limit is not None:
                    limit -= 1
                    if limit <= 0:
                        return
            if not hits or page >= (res.get("page_count") or 0):
                break
            page += 1
            time.sleep(PAUSE)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Import produktů z Open Food Facts do Slevoměru")
    ap.add_argument("--limit", type=int, help="jen prvních N produktů (test)")
    args = ap.parse_args(argv)

    api = SlevomerApi(timeout=120)
    run = api.post("/v1/ingest/run/start", {"source": "off_products", "run_ref": os.environ.get("GITHUB_RUN_ID", "manual")})
    run_id = run["run_id"]
    stats = {"sent": 0, "saved": 0, "skipped": 0}
    buf: list[dict] = []

    def flush():
        if not buf:
            return
        res = api.post("/v1/ingest/products", {"run_id": run_id, "items": buf})
        stats["sent"] += len(buf)
        stats["saved"] += res["saved"]
        stats["skipped"] += res["skipped"]
        buf.clear()

    try:
        for p in fetch_all(args.limit):
            buf.append(p)
            if len(buf) >= BATCH:
                flush()
        flush()
        api.post("/v1/ingest/run/finish", {"run_id": run_id, "status": "ok", "stats": stats})
    except Exception as e:  # noqa: BLE001
        api.post("/v1/ingest/run/finish", {"run_id": run_id, "status": "failed", "stats": stats, "message": str(e)[:1000]})
        raise
    print("Hotovo:", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
