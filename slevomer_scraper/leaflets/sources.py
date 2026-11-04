"""Zdroje letáků 6 prioritních řetězců (průzkum 2026-11-02, docs/DATA_SOURCES.md):

| řetězec  | seznam letáků                         | stránky        | PDF | strukturované akce            |
|----------|---------------------------------------|----------------|-----|-------------------------------|
| Lidl     | Schwarz API (lidl/cs-CZ)              | imgproxy       | ano | ano — web lidl.cz (q/api)     |
| Kaufland | Schwarz API (kaufland/cs-CZ, store_id)| imgproxy       | ano | ne (web za Cloudflare)        |
| Penny    | penny.cz (Nuxt) → FlippingBook        | JPEG           | ne  | jen vybrané (~30) z penny.cz  |
| Billa    | billa.cz (Nuxt) → Publitas            | Publitas       | ano | ne                            |
| Albert   | albert.cz (Apollo) → Publitas         | Publitas       | ano | ne                            |
| Tesco    | Tesco leaflets GraphQL                | odkazem (CDN 403)| ne (CDN 403) | ne (web za Akamai)     |
| Teta     | tetadrogerie.cz/akce/letak → eCPaper  | JPEG (Azure)   | ano | ne                            |  (F6, 2026-11-04)
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, timedelta
from urllib.parse import urljoin

from . import net
from .base import LeafletRef, Source, category_slug, czdate, iso

SCHWARZ = "https://endpoints.leaflets.schwarz/v4"


def _today() -> str:
    return date.today().isoformat()


def _is_catalog(*texts: str | None) -> bool:
    t = " ".join(x or "" for x in texts).lower()
    return any(k in t for k in ("katalog", "magazín", "magazin", "spotřební", "nonfood", "brand"))


def _nuxt(html: str):
    """Rozbalí Nuxt payload (__NUXT_DATA__, formát devalue) na běžné objekty."""
    m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    arr = json.loads(m.group(1))
    memo: dict[int, object] = {}

    def rev(i, depth=0):
        if not isinstance(i, int) or depth > 80 or i >= len(arr):
            return i
        if i in memo:
            return memo[i]
        v = arr[i]
        if isinstance(v, list):
            if v and isinstance(v[0], str) and v[0] in ("Reactive", "ShallowReactive", "Ref", "ShallowRef", "EmptyRef", "Set", "Map", "Date", "NuxtError"):
                r = rev(v[1], depth + 1) if len(v) > 1 else None
            else:
                r = [rev(x, depth + 1) for x in v]
        elif isinstance(v, dict):
            r = {}
            memo[i] = r
            for k, x in v.items():
                r[k] = rev(x, depth + 1)
            return r
        else:
            r = v
        memo[i] = r
        return r

    return rev(0)


def _walk(o, pred, out):
    if isinstance(o, dict):
        if pred(o):
            out.append(o)
        for v in o.values():
            _walk(v, pred, out)
    elif isinstance(o, list):
        for v in o:
            _walk(v, pred, out)
    return out


# ── Schwarz (Lidl, Kaufland) ──────────────────────────────────────────
def schwarz_refs(chain: str, client_locale: str, store_id: str | None = None) -> list[LeafletRef]:
    q = f"{SCHWARZ}/overview?client_locale={client_locale}" + (f"&store_id={store_id}" if store_id else "")
    ov = net.get_json(q)
    refs = []
    for cat in ov.get("categories", []):
        for sub in cat.get("subcategories", []):
            for f in sub.get("flyers", []):
                fid = f["id"]
                det = net.get_json(f"{SCHWARZ}/flyer?flyer_identifier={fid}" + (f"&store_id={store_id}" if store_id else "&region_id=0&region_code=0"))
                fl = det.get("flyer", {})
                pages = [p.get("image") for p in sorted(fl.get("pages", []), key=lambda p: p.get("number", 0)) if p.get("image")]
                vf = iso(f.get("offerStartDate") or f.get("startDate"))
                vt = iso(f.get("offerEndDate") or f.get("endDate"))
                refs.append(LeafletRef(
                    chain=chain, external_id=fid, title=f"{f.get('name', '').strip()} {f.get('title', '').strip()}".strip(),
                    valid_from=vf, valid_to=vt, pages=pages, pdf_url=f.get("hiResPdfUrl") or f.get("pdfUrl"),
                    source_url=f.get("flyerUrlAbsolute"), kind="katalog" if _is_catalog(f.get("name"), sub.get("name")) else "letak",
                    variant=sub.get("name"),
                ))
    return refs


class Lidl(Source):
    chain = "lidl"
    # Kategorie webu lidl.cz s potravinami v akci (ověřeno 2026-11-02, ~400 položek)
    FOOD_CATEGORY = "10068374"

    def leaflets(self):
        refs = schwarz_refs("lidl", "lidl/cs-CZ")
        for r in refs:
            r.extract = False   # akce bereme strukturovaně z webu, PDF číst nemusíme
        return refs

    def structured_offers(self):
        out = []
        offset = 0
        while True:
            d = net.get_json("https://www.lidl.cz/q/api/search?assortment=CZ&locale=cs_CZ&version=v2.0.0"
                             f"&category.id={self.FOOD_CATEGORY}&fetchsize=60&offset={offset}")  # víc než 60 Lidl odmítne (401)
            items = d.get("items") or []
            for it in items:
                o = self._offer(it.get("gridbox", {}).get("data", {}))
                if o:
                    out.append(o)
            offset += len(items)
            if not items or offset >= int(d.get("numFound") or 0) or offset > 3000:
                break
            time.sleep(1.5)   # slušné tempo
        return out

    def _offer(self, g: dict) -> dict | None:
        title = (g.get("fullTitle") or "").strip(" -")
        if not title:
            return None
        price = g.get("price") or {}
        lp = (g.get("lidlPlus") or [{}])[0].get("price") if g.get("lidlPlus") else None
        regular = price.get("price")
        loyalty_price = lp.get("price") if lp else None
        if regular is None and loyalty_price is None:
            return None
        # okno platnosti, které právě běží nebo začne nejdřív
        windows = (g.get("stockAvailability") or {}).get("badgeInfoV2") or []
        today = _today()
        best = None
        for w in windows:
            if not w.get("validFrom") or not w.get("validUntil"):
                continue
            vf, vt = iso(w["validFrom"]), iso(w["validUntil"])
            if vt < today:
                continue
            if best is None or vf < best[0]:
                best = (vf, vt)
        if not best:
            if g.get("storeStartDate") and g.get("storeEndDate"):
                best = (iso(g["storeStartDate"]), iso(g["storeEndDate"]))
            else:
                return None
        disc = price.get("discount") or {}
        brand = ((g.get("brand") or {}).get("name") or "").strip() or None
        qty = (price.get("packaging") or {}).get("text") or ((lp or {}).get("packaging") or {}).get("text")
        cat = category_slug((g.get("keyfacts") or {}).get("wonCategoryPrimary") or g.get("category"))
        return {
            "key": f"{g.get('productId') or g.get('itemId')}:{best[0]}",
            "name": title,
            "brand": brand.title() if brand and brand.isupper() else brand,
            "image_url": g.get("image"),
            "price": regular if regular is not None else loyalty_price,
            "original_price": disc.get("deletedPrice") or price.get("oldPrice"),
            "discount_pct": disc.get("percentageDiscount"),
            "loyalty_required": regular is None,
            "loyalty_price": loyalty_price if regular is not None else None,
            "quantity_text": qty,
            "valid_from": best[0],
            "valid_to": best[1],
            "category": cat,
            "confidence": 100,
            "source": "structured",
        }


class Kaufland(Source):
    chain = "kaufland"
    STORE_ID = "1100"   # letáky jsou regionální podle prodejny; 1100 = typická prodejna (ověřeno 2026-11-02)

    def leaflets(self):
        return schwarz_refs("kaufland", "kaufland/cs-CZ", self.STORE_ID)


# ── Publitas (Billa, Albert) ──────────────────────────────────────────
def publitas_pages(view_url: str) -> list[str]:
    base = view_url.rstrip("/")
    for suffix in ("/page/1", ""):
        base = base[: -len(suffix)] if suffix and base.endswith(suffix) else base
    spreads = net.get_json(base + "/spreads.json")
    host = re.match(r"https://[^/]+", base).group(0)
    pages = []
    for sp in spreads:
        for pg in sp.get("pages", []):
            imgs = pg.get("images") or {}
            src = imgs.get("at1600") or imgs.get("at2000") or imgs.get("at1200")
            if src:
                pages.append((pg.get("number", len(pages) + 1), urljoin(host + "/", src.lstrip("/"))))
    return [u for _, u in sorted(pages)]


def _clean_pdf(u: str | None) -> str | None:
    return u.split("?")[0] if u else None


def _slug_dates(slug: str) -> tuple[str, str] | None:
    """„velky-letak-29-10-3-11-2026“ → (2026-10-29, 2026-11-03)."""
    m = re.search(r"(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{4})", slug)
    if not m:
        return None
    d1, m1, d2, m2, y = map(int, m.groups())
    y1 = y - 1 if m1 > m2 else y
    return f"{y1:04d}-{m1:02d}-{d1:02d}", f"{y:04d}-{m2:02d}-{d2:02d}"


class Billa(Source):
    chain = "billa"
    PAGE = "https://www.billa.cz/letaky-billa?tab=letaky-billa/velky-letak"

    def leaflets(self):
        root = _nuxt(net.get_text(self.PAGE)) or {}
        flat = json.dumps(root, ensure_ascii=False)
        # V datech stránky jde vždy PDF a hned za ním odkaz na prohlížeč téhož letáku.
        pairs: dict[str, str | None] = {}
        last_pdf = None
        for m in re.finditer(r"https://view\.publitas\.com/\d+/\d+/pdfs/[a-f0-9-]+\.pdf|https://view\.publitas\.com/billa-cz/([a-z0-9-]+)/page/1", flat):
            if m.group(1):
                pairs.setdefault(m.group(1), last_pdf)
                last_pdf = None
            else:
                last_pdf = m.group(0)
        refs = []
        for slug, pdf in sorted(pairs.items()):
            dates = _slug_dates(slug)
            if not dates or dates[1] < _today():
                continue
            view = f"https://view.publitas.com/billa-cz/{slug}"
            refs.append(LeafletRef(
                chain="billa", external_id=slug, title=slug.replace("-", " ").capitalize(), valid_from=dates[0], valid_to=dates[1],
                pages=publitas_pages(view), pdf_url=pdf, source_url=view + "/page/1",
                kind="katalog" if "katalog" in slug else "letak",
                variant="velký" if slug.startswith("velky") else ("malý" if slug.startswith("maly") else None),
            ))
        return refs


def publitas_pdf(view_url: str) -> str | None:
    """Odkaz na PDF ke stažení ze stránky prohlížeče Publitas."""
    try:
        html = net.get_text(view_url.rstrip("/") + "/")
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"https://view\.publitas\.com/\d+/\d+/pdfs/[a-f0-9-]+\.pdf", html)
    return m.group(0) if m else None


class Albert(Source):
    """albert.cz robotům vrací CAPTCHA → web nepoužíváme. Letáky jsou na Publitasu
    (letaky.albert.cz) pod názvy podle ISO týdne: {týden}hm_akcni_letak / {týden}sm_akcni_letak,
    platnost středa–úterý (ověřeno 2026-11-02: 44 = 28. 10.–3. 11., 45 = 4.–10. 11.)."""
    chain = "albert"
    BASE = "https://letaky.albert.cz"

    def leaflets(self):
        refs = []
        today = date.today()
        for delta in (-7, 0, 7):
            d = today + timedelta(days=delta)
            year, week, _ = d.isocalendar()
            monday = date.fromisocalendar(year, week, 1)
            vf, vt = monday + timedelta(days=2), monday + timedelta(days=8)
            if vt.isoformat() < _today():
                continue
            for code, variant in (("hm", "hypermarket"), ("sm", "supermarket")):
                slug = f"{week}{code}_akcni_letak"
                view = f"{self.BASE}/{slug}"
                try:
                    pages = publitas_pages(view)
                except Exception:  # noqa: BLE001 — leták pro daný týden ještě nevyšel
                    continue
                if not pages:
                    continue
                refs.append(LeafletRef(
                    chain="albert", external_id=f"{year}-{slug}", title=f"Albert {variant} — týden {week}",
                    valid_from=vf.isoformat(), valid_to=vt.isoformat(), pages=pages, pdf_url=publitas_pdf(view),
                    source_url=view + "/", variant=variant,
                ))
        return refs


# ── Tesco (GraphQL backend letáků) ────────────────────────────────────
class Tesco(Source):
    chain = "tesco"
    GQL = "https://api.prod.retail.tesco.com/marketing/leaflets-be/graphql"

    def leaflets(self):
        d = net.post_json(self.GQL, {"query": "query { leaflets { items { id type slug country validFrom validTo leafletUrl } } }"})
        items = [x for x in d["data"]["leaflets"]["items"] if x.get("country") == "cz" and iso(x["validTo"]) >= _today()]
        refs = []
        for x in items:
            q = ('query { leafletBySlug(country: cz, leafletType: %s, slug: "%s") { id pages { page pagePNG } } }'
                 % (x["type"], x["slug"].replace('"', "")))
            det = net.post_json(self.GQL, {"query": q})["data"]["leafletBySlug"] or {}
            pages = [p["pagePNG"] for p in sorted(det.get("pages") or [], key=lambda p: p["page"])]
            kind_name = {"HM": "hypermarkety", "SM": "supermarkety", "CAT": "katalog"}.get(x["type"], x["type"].lower())
            refs.append(LeafletRef(
                chain="tesco", external_id=str(x["id"]), title=f"Tesco {kind_name} od {iso(x['validFrom'])[8:10]}. {iso(x['validFrom'])[5:7]}.",
                valid_from=iso(x["validFrom"]), valid_to=iso(x["validTo"]), pages=pages, pdf_url=x.get("leafletUrl"),
                source_url=f"https://www.itesco.cz/akcni-nabidky/letaky-a-katalogy/{kind_name}/{x['slug']}/1",
                kind="katalog" if x["type"] == "CAT" else "letak", variant=x["type"],
                # CDN Tesca (digitalcontent.api.tesco.com) robotům vrací 403 → nestahujeme, jen odkazujeme
                extract=False, hotlink_pages=True,
            ))
        return refs


# ── Penny (penny.cz + FlippingBook) ───────────────────────────────────
class Penny(Source):
    chain = "penny"
    OFFERS = "https://www.penny.cz/nabidky?tab=akcni-polozky"
    LEAFLETS = "https://www.penny.cz/nabidky?tab=nabidky/letaky"

    def __init__(self):
        self._offers: list[dict] | None = None

    def structured_offers(self):
        if self._offers is not None:
            return self._offers
        root = _nuxt(net.get_text(self.OFFERS)) or {}
        prods = _walk(root, lambda o: "sku" in o and isinstance(o.get("price"), dict), [])
        out = {}
        for p in prods:
            pr = p["price"]
            reg = (pr.get("regular") or {}).get("value")
            loy = (pr.get("loyalty") or {}).get("value")
            if not reg or not pr.get("validityStart") or not pr.get("validityEnd"):
                continue
            unit = p.get("volumeLabelShort") or ""
            out[p["sku"]] = {
                "key": f"{p['sku']}:{pr['validityStart']}",
                "name": p.get("name"),
                "image_url": (p.get("images") or [None])[0],
                "price": reg / 100,
                "original_price": (pr.get("crossed") or 0) / 100 or None,
                "discount_pct": abs(pr["discountPercentage"]) if pr.get("discountPercentage") else None,
                "lowest_price_30d": (pr.get("lowestPrice") or 0) / 100 or None,
                "loyalty_price": loy / 100 if loy else None,
                "quantity_text": f"{p.get('amount')} {unit}".strip() if p.get("amount") else None,
                "valid_from": pr["validityStart"],
                "valid_to": pr["validityEnd"],
                "category": category_slug(p.get("category")),
                "confidence": 100,
                "source": "structured",
            }
        self._offers = list(out.values())
        return self._offers

    def leaflets(self):
        html = net.get_text(self.LEAFLETS)
        folders = sorted(set(re.findall(r"https://files\.rewe\.co\.at/PennyIntLeaflet/CZ/([0-9_a-z]+)/?", html)))
        ends = {}
        for o in self.structured_offers():
            ends[o["valid_from"]] = max(ends.get(o["valid_from"], ""), o["valid_to"])
        refs = []
        for f in folders:
            m = re.match(r"(\d{2})_(\d{2})_(\d{4})", f)
            if not m:
                continue
            vf = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            vt = ends.get(vf) or (date.fromisoformat(vf) + timedelta(days=6)).isoformat()
            if vt < _today():
                continue
            base = f"https://files.rewe.co.at/PennyIntLeaflet/CZ/{f}"
            idx = net.get_text(base + "/")
            n = max([int(x) for x in re.findall(r'href="\./(\d+)/"', idx)] or [1])
            pages = [f"{base}/files/assets/common/page-html5-substrates/page{i:04d}_4.jpg" for i in range(1, n + 1)]
            refs.append(LeafletRef(chain="penny", external_id=f, title=f"Penny leták od {m.group(1)}. {m.group(2)}.",
                                   valid_from=vf, valid_to=vt, pages=pages, pdf_url=None, source_url=base + "/", extract=False))
        return refs


# ── Teta drogerie (tetadrogerie.cz + eCPaper) ─────────────────────────
class Teta(Source):
    """Seznam letáků na tetadrogerie.cz/akce/letak („Teta leták č. 23 platnost 5. 11. 2026 - 17. 11. 2026“),
    prohlížeč letak.tetadrogerie.cz (robots.txt: Allow /) má stránky jako JPEG na Azure (…-H.jpg v pořadí
    stránek) a celé PDF. Klubový leták = ceny jen s kartou Teta klub. Ověřeno 2026-11-04."""
    chain = "teta"
    PAGE = "https://www.tetadrogerie.cz/akce/letak"

    def leaflets(self):
        html = net.get_text(self.PAGE)
        body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
        links = list(dict.fromkeys(re.findall(r'href="(https://letak\.tetadrogerie\.cz/[a-z0-9-]+/)"', body)))
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
        dated = re.findall(r"(Teta [^|]{2,60}?)\s+platnost\s+(\d{1,2}\.\s?\d{1,2}\.\s?\d{4})\s*[-–]\s*(\d{1,2}\.\s?\d{1,2}\.\s?\d{4})", text)
        refs = []
        for url in links:
            slug = url.rstrip("/").rsplit("/", 1)[1]
            # Název z odkazu („teta-klub-letak-23-2026“) ↔ položka seznamu se stejným číslem a typem
            num = re.search(r"(\d+)-(\d{4})$", slug)
            klub = "klub" in slug
            match = next((d for d in dated if ("klub" in d[0].lower()) == klub and (not num or f"č. {num.group(1)}" in d[0])), None)
            if not match:
                continue
            vf, vt = czdate(match[1]), czdate(match[2])
            if not vf or not vt or vt < _today():
                continue
            viewer = net.get_text(url)
            pdf = re.search(r"https://liveecpaperdmp[^\"' ]+\.pdf", viewer)
            pages = list(dict.fromkeys(re.findall(r"https://liveecpaperdmp[^\"' ]*-H\.jpg", viewer)))
            refs.append(LeafletRef(
                chain="teta", external_id=slug, title=match[0].strip(), valid_from=vf, valid_to=vt, pages=pages,
                pdf_url=pdf.group(0) if pdf else None, source_url=url, variant="klub" if klub else None, loyalty=klub,
            ))
        return refs


ALL = {s.chain: s for s in (Lidl, Kaufland, Penny, Billa, Albert, Tesco, Teta)}
