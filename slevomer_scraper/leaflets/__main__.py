"""Běh scraperu letáků (GitHub Actions 00:00 a 12:00 Praha, workflow leaflets.yml).

    python -m slevomer_scraper.leaflets                      # všechny řetězce
    python -m slevomer_scraper.leaflets --chains lidl,penny  # jen některé
    python -m slevomer_scraper.leaflets --max-pages 3        # test: jen první stránky letáků
    python -m slevomer_scraper.leaflets --dry-run            # nic neposílat, jen vypsat

Postup: zdroj → seznam letáků → server řekne, které zná (content_hash) → chybějící stránky
stáhnout, převést na WebP a nahrát → nové letáky s PDF vytěžit → strukturované akce →
opakovaně /v1/cron/tick (párování, historie cen, falešné slevy, úklid).
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import sys
import tempfile
import time
import traceback

from ..api import SlevomerApi
from . import net
from .base import Source, to_webp
from .pdf_extract import extract_pdf
from .sources import ALL


def _fold(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", s.lower())
    return re.sub(r"[^a-z0-9]+", "-", "".join(c for c in s if unicodedata.category(c) != "Mn")).strip("-")[:60]


def post_offers(api: SlevomerApi | None, chain: str, leaflet_id: int | None, offers: list[dict]) -> int:
    saved = 0
    for i in range(0, len(offers), 500):
        chunk = offers[i:i + 500]
        if api is None:
            saved += len(chunk)
            continue
        res = api.post("/v1/ingest/offers", {"chain": chain, "leaflet_id": leaflet_id, "items": chunk})
        saved += res.get("saved", 0)
    return saved


def run_source(src: Source, api: SlevomerApi | None, args, stats: dict) -> None:
    chain = src.chain
    st = stats.setdefault(chain, {"leaflets": 0, "pages": 0, "offers_pdf": 0, "offers_structured": 0, "errors": []})
    # Strukturovaná data nejdřív: když je zdroj (dočasně) odmítne, akce se přečtou z PDF letáků.
    structured: list[dict] = []
    pdf_fallback = False
    for attempt in range(2):
        try:
            structured = src.structured_offers()
            break
        except net.Blocked as e:
            if attempt == 0:
                time.sleep(20)
                continue
            st["errors"].append(f"strukturovaná data odmítnuta ({e}) → čtu PDF"[:200])
            pdf_fallback = True
    refs = src.leaflets()
    st["leaflets"] = len(refs)
    known = {}
    if api and refs:
        known = api.post("/v1/ingest/leaflets/known", {"hashes": [r.content_hash for r in refs]}).get("known", {})
    for ref in refs:
        pages = ref.pages[: args.max_pages] if args.max_pages else ref.pages
        print(f"  {ref.valid_from}–{ref.valid_to} {ref.title[:60]} ({len(ref.pages)} str.)", flush=True)
        k = known.get(ref.content_hash) or {}
        if api is None:
            continue
        res = api.post("/v1/ingest/leaflets", {
            "chain": chain, "external_id": ref.external_id, "title": ref.title, "kind": ref.kind, "variant": ref.variant,
            "valid_from": ref.valid_from, "valid_to": ref.valid_to, "source_url": ref.source_url, "pdf_url": ref.pdf_url,
            "page_count": len(ref.pages), "content_hash": ref.content_hash,
        })
        lid = res["leaflet_id"]
        if res.get("hidden"):   # skrytý na žádost obchodu → stránky ani PDF už nestahovat
            continue
        have = set(res.get("pages_present") or [])
        for n, url in enumerate(pages, start=1):
            if n in have or args.no_pages:
                continue
            try:
                if ref.hotlink_pages:
                    # Zdroj robotům obrázky nedá → prohlížeč uživatele je načte přímo od obchodu
                    api.post(f"/v1/ingest/leaflets/{lid}/pages", {"page_no": n, "image_url": url})
                else:
                    webp, _, _ = to_webp(net.get(url, timeout=90))
                    api.post(f"/v1/ingest/leaflets/{lid}/pages", {"page_no": n, "image_b64": base64.b64encode(webp).decode()})
                st["pages"] += 1
            except Exception as e:  # noqa: BLE001
                st["errors"].append(f"{ref.external_id} str. {n}: {e}"[:200])
        # PDF číst jen u nového letáku (nebo když z něj zatím nic není) — 2 běhy denně nic nestojí
        if (ref.extract or pdf_fallback) and ref.pdf_url and not args.no_pdf and not k.get("offers"):
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, "leaflet.pdf")
                    with open(path, "wb") as f:
                        f.write(net.get(ref.pdf_url, timeout=300))
                    offers = extract_pdf(path, max_pages=150)
                for o in offers:
                    # bez id letáku: stejná akce ve dvou letácích (Billa velký + malý) = jedna nabídka
                    o["key"] = f"pdf:{ref.valid_from}:{_fold(o['name'])}:{o['price']}"
                    o["valid_from"], o["valid_to"] = ref.valid_from, ref.valid_to
                    if ref.loyalty:
                        o["loyalty_required"] = True
                        o["key"] += ":karta"
                n = post_offers(api, chain, lid, offers)
                st["offers_pdf"] += n
                print(f"    PDF → {len(offers)} nabídek ({sum(1 for o in offers if o['confidence'] < 60)} ke kontrole)", flush=True)
            except Exception as e:  # noqa: BLE001
                st["errors"].append(f"{ref.external_id} PDF: {e}"[:200])
    if structured:
        st["offers_structured"] = post_offers(api, chain, None, structured)
        print(f"  strukturované akce: {len(structured)}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Scraper letáků Slevoměru")
    ap.add_argument("--chains", help="čárkou, výchozí všechny: " + ",".join(ALL))
    ap.add_argument("--max-pages", type=int, default=0, help="nahrát jen prvních N obrázků stránek (test); PDF se čte celé")
    ap.add_argument("--no-pages", action="store_true", help="nenahrávat obrázky stránek")
    ap.add_argument("--no-pdf", action="store_true", help="nečíst PDF")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    chains = [c.strip() for c in (args.chains or ",".join(ALL)).split(",") if c.strip()]
    api = None if args.dry_run else SlevomerApi(timeout=180)
    run_id = None
    if api:
        run_id = api.post("/v1/ingest/run/start", {"source": "leaflets", "run_ref": os.environ.get("GITHUB_RUN_ID", "manual")})["run_id"]
    stats: dict = {}
    failed = []
    t0 = time.time()
    for chain in chains:
        cls = ALL.get(chain)
        if not cls:
            print(f"Neznámý řetězec {chain}", file=sys.stderr)
            continue
        print(f"== {chain}", flush=True)
        try:
            run_source(cls(), api, args, stats)
        except net.Blocked as e:
            # Zdroj nás odmítl — NEOBCHÁZET, jen zapsat (admin → Stav scraperu)
            failed.append(chain)
            stats.setdefault(chain, {})["errors"] = [f"odmítnuto: {e}"]
            print(f"  odmítnuto zdrojem: {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            failed.append(chain)
            stats.setdefault(chain, {})["errors"] = [f"{type(e).__name__}: {e}"[:300]]
            traceback.print_exc()
    tick = {}
    if api:
        print("== zpracování na serveru (tick)", flush=True)
        try:
            rounds = api.tick_until_done(max_rounds=100)
            tick = {"rounds": len(rounds), "last": rounds[-1] if rounds else None}
        except Exception as e:  # noqa: BLE001 — běh se musí uzavřít, dožene to příští tick
            tick = {"error": f"{type(e).__name__}: {e}"[:300]}
            traceback.print_exc()
        status = "ok" if not failed else ("partial" if len(failed) < len(chains) else "failed")
        api.post("/v1/ingest/run/finish", {"run_id": run_id, "status": status, "stats": {"chains": stats, "tick": tick, "seconds": round(time.time() - t0)},
                                            "message": ("Selhalo: " + ", ".join(failed)) if failed else None})
    print("Hotovo:", {c: {k: v for k, v in s.items() if k != "errors"} for c, s in stats.items()}, "chyby:", sum(len(s.get("errors", [])) for s in stats.values()))
    return 0 if len(failed) < len(chains) else 1


if __name__ == "__main__":
    sys.exit(main())
