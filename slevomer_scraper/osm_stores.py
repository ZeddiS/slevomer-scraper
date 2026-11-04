"""Import všech obchodů v ČR z OpenStreetMap (Overpass API) → Slevoměr API.

Spouští GitHub Actions jednou týdně (workflow stores.yml). Ručně:
    python -m slevomer_scraper.osm_stores                 # stáhne z Overpass a nahraje
    python -m slevomer_scraper.osm_stores --save cz.json  # jen stáhne a uloží
    python -m slevomer_scraper.osm_stores --file cz.json  # nahraje dříve stažená data

Normalizaci (typy obchodů, párování na řetězce, adresy) dělá server
(web/app/src/Stores.php) — tady se jen stahuje, doplní odhad obce a posílá po dávkách.
Data © přispěvatelé OpenStreetMap, ODbL.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .api import SlevomerApi

MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
UA = "SlevomerScraper/0.2 (+https://slevomer.zeddis.xyz)"

# Po krajích (ISO 3166-2) — jeden dotaz na celou ČR veřejné Overpass servery
# ve špičce odmítají („Dispatcher_Client … timeout“), menší dotazy projdou.
REGIONS = ["CZ-10", "CZ-20", "CZ-31", "CZ-32", "CZ-41", "CZ-42", "CZ-51", "CZ-52",
           "CZ-53", "CZ-63", "CZ-64", "CZ-71", "CZ-72", "CZ-80"]
SHOPS_QUERY = """[out:json][timeout:300];
area["ISO3166-2"="{region}"]->.r;
(
  nwr["shop"]["shop"!~"^(vacant|no|disused)$"](area.r);
  nwr["amenity"="pharmacy"](area.r);
  nwr["amenity"="marketplace"](area.r);
);
out center tags qt;"""

PLACES_QUERY = """[out:json][timeout:300];
area["ISO3166-1"="CZ"][admin_level=2]->.cz;
node["place"~"^(city|town|village)$"]["name"](area.cz);
out qt;"""

KEEP_TAGS = {
    "name", "brand", "brand:wikidata", "operator", "shop", "amenity", "opening_hours",
    "addr:street", "addr:place", "addr:housenumber", "addr:conscriptionnumber", "addr:streetnumber",
    "addr:city", "addr:postcode", "phone", "contact:phone", "website", "contact:website",
    "email", "contact:email", "wheelchair", "contact:facebook", "contact:instagram",
}
BATCH = 1500


def overpass(query: str, attempts: int = 6, min_elements: int = 1) -> dict:
    last: Exception | None = None
    for i in range(attempts):
        url = MIRRORS[i % len(MIRRORS)]
        try:
            print(f"Overpass: {url} (pokus {i + 1})", flush=True)
            data = urllib.parse.urlencode({"data": query}).encode()
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=1000) as resp:
                raw = resp.read()
            if not raw.lstrip().startswith(b"{"):
                raise RuntimeError("Overpass nevrátil JSON: " + raw[:200].decode(errors="replace"))
            out = json.loads(raw)
            if out.get("remark") and "error" in out["remark"].lower():
                raise RuntimeError(out["remark"])
            # Některá zrcadla mají zastaralé „area“ → prázdný výsledek bez chyby
            if len(out.get("elements", [])) < min_elements:
                raise RuntimeError(f"jen {len(out.get('elements', []))} prvků — zrcadlo nejspíš nemá aktuální oblasti")
            return out
        except (urllib.error.URLError, RuntimeError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            print(f"  selhalo: {e}", flush=True)
            time.sleep(min(120, 15 * (i + 1)))
    raise RuntimeError(f"Overpass selhal na všech zrcadlech: {last}")


class PlaceIndex:
    """Mřížka obcí ~0,1° pro odhad obce u prodejen bez addr:city."""

    def __init__(self, places: list[dict]):
        self.grid: dict[tuple[int, int], list[tuple[float, float, str, int]]] = {}
        rank = {"city": 3, "town": 2, "village": 1}
        for p in places:
            t = p.get("tags", {})
            key = (int(p["lat"] * 10), int(p["lon"] * 10))
            self.grid.setdefault(key, []).append((p["lat"], p["lon"], t["name"], rank.get(t.get("place"), 0)))

    def nearest(self, lat: float, lon: float, max_km: float = 6.0) -> str | None:
        gy, gx = int(lat * 10), int(lon * 10)
        best: tuple[float, str] | None = None
        kx = math.cos(math.radians(lat))
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for plat, plon, name, rank in self.grid.get((gy + dy, gx + dx), []):
                    d = math.hypot((plat - lat) * 111.32, (plon - lon) * 111.32 * kx)
                    # Města mají větší „dosah“ než vesnice
                    d_eff = d / (1 + 0.5 * rank)
                    if d <= max_km and (best is None or d_eff < best[0]):
                        best = (d_eff, name)
        return best[1] if best else None


def to_items(shops: dict, places: PlaceIndex | None) -> list[dict]:
    items = []
    for e in shops.get("elements", []):
        if e["type"] == "node":
            lat, lon = e.get("lat"), e.get("lon")
        else:
            c = e.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if lat is None or lon is None:
            continue
        tags = {k: v for k, v in e.get("tags", {}).items() if k in KEEP_TAGS}
        item = {"t": e["type"][0], "id": e["id"], "lat": lat, "lon": lon, "tags": tags}
        if "addr:city" not in tags and places is not None:
            guess = places.nearest(lat, lon)
            if guess:
                item["city"] = guess
        items.append(item)
    return items


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Import obchodů z OSM do Slevoměru")
    ap.add_argument("--file", help="použít dříve stažená data (JSON z --save)")
    ap.add_argument("--save", help="jen stáhnout a uložit do souboru")
    args = ap.parse_args(argv)

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            bundle = json.load(f)
    else:
        seen: dict[tuple[str, int], dict] = {}
        for region in REGIONS:
            part = overpass(SHOPS_QUERY.replace("{region}", region), min_elements=200)
            for e in part.get("elements", []):
                seen[(e["type"], e["id"])] = e   # prodejny na hranici krajů jen jednou
            print(f"  {region}: {len(part.get('elements', []))} (celkem {len(seen)})", flush=True)
            time.sleep(3)
        bundle = {"shops": {"elements": list(seen.values())}, "places": overpass(PLACES_QUERY, min_elements=3000)}
        if args.save:
            with open(args.save, "w", encoding="utf-8") as f:
                json.dump(bundle, f, ensure_ascii=False)
            print(f"Uloženo do {args.save}: {len(bundle['shops'].get('elements', []))} obchodů")
            return 0

    places = PlaceIndex(bundle.get("places", {}).get("elements", []))
    items = to_items(bundle["shops"], places)
    print(f"Prodejen k odeslání: {len(items)}", flush=True)
    if len(items) < 1000:
        print("Podezřele málo dat — končím bez nahrání.", file=sys.stderr)
        return 2

    api = SlevomerApi(timeout=120)
    run = api.post("/v1/ingest/run/start", {"source": "osm_stores", "run_ref": os.environ.get("GITHUB_RUN_ID", "manual")})
    run_id = run["run_id"]
    saved = skipped = 0
    try:
        for i in range(0, len(items), BATCH):
            res = api.post("/v1/ingest/stores", {"run_id": run_id, "items": items[i:i + BATCH]})
            saved += res["saved"]
            skipped += res["skipped"]
            print(f"  {min(i + BATCH, len(items))}/{len(items)} · uloženo {saved} · přeskočeno {skipped}", flush=True)
        fin = api.post("/v1/ingest/stores/finish", {"run_id": run_id, "sent": len(items)})
        print("Hotovo:", json.dumps(fin, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 — chybu chceme zapsat do běhu na serveru
        api.post("/v1/ingest/run/finish", {"run_id": run_id, "status": "failed", "message": str(e)[:1000]})
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
