"""Rossmann e-shop (rossmann.cz) — ověřeno 2026-11-04:

- robots.txt zakazuje jen filtry (`*filter=*`), obchodní podmínky automatizované čtení nezakazují.
- Mapa webu `sitemap-products_0_CZ_cs-CZ.xml` obsahuje ~17 000 produktů.
- Stránka produktu má v HTML EAN, cenu, přeškrtnutou původní cenu a běžnou cenu za jednotku.

Čteme jen fakta (EAN, název, značka, množství, ceny). Obrázky a popisy NEpřebíráme.
Tempo: 1 stránka za ~1,2 s, max. několik tisíc denně → celý katalog zhruba jednou za týden.
"""
from __future__ import annotations

import html
import re

from ..leaflets import net
from ..leaflets.base import category_slug

CHAIN = "rossmann"
SITEMAP = "https://www.rossmann.cz/binbox/pub/sitemap/sitemap-products_0_CZ_cs-CZ.xml"
BASE = "https://www.rossmann.cz"


def product_urls() -> list[str]:
    xml = net.get_text(SITEMAP, timeout=90)
    urls = [html.unescape(u).strip() for u in re.findall(r"<loc>([^<]+)</loc>", xml)]
    return [u for u in dict.fromkeys(urls) if u.startswith(BASE + "/")]


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _price(s: str) -> float | None:
    m = re.search(r"(\d[\d\s]*(?:[.,]\d{1,2})?)\s*Kč", s)
    if not m:
        return None
    try:
        return float(m.group(1).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def parse_product(url: str, page: str) -> dict | None:
    """HTML stránky produktu → položka pro /v1/ingest/eshop (None = stránka bez ceny / není produkt)."""
    m = re.search(r'<h1 class="typo--medium">(.*?)</h1>', page, re.S) or re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    name = _text(m.group(1)) if m else ""
    if not name:
        return None
    block = page[page.find('id="product-prices"'):]
    if not block:
        return None
    block = block[:6000]
    final = None
    m = re.search(r'typo--h2[^"]*"[^>]*>(.*?)</div>', block, re.S)
    if m:
        final = _price(_text(m.group(1)))
    if final is None:
        return None
    original = None
    m = re.search(r'decor-strike[^"]*"[^>]*>(.*?)</div>', block, re.S)
    if m:
        original = _price(_text(m.group(1)))
        if original is not None and original <= final:
            original = None
    # Cena s věrnostní kartou (ROSSMANN CLUB), pokud ji stránka uvádí zvlášť.
    loyalty = None
    m = re.search(r"CLUB[^<]{0,40}</[^>]+>\s*(?:<[^>]+>\s*)*([\d\s.,]+\s*Kč)", block, re.I)
    if m:
        loyalty = _price(m.group(1))
        if loyalty is not None and loyalty >= final:
            loyalty = None
    ean = None
    m = re.search(r"EAN\s*(?:<[^>]+>\s*)*(\d{8,14})", page)
    if m:
        ean = m.group(1)
    sku = None
    m = re.search(r"Obj\. č\.:\s*(?:<[^>]+>\s*)*(\d+)", page)
    if m:
        sku = m.group(1)
    brand = None
    m = re.search(r'<div class="stack display--none display--lg-block">\s*<a href="/[^"]+" class="typo--b1 typo--book">(.*?)</a>', page, re.S)
    if m:
        brand = _text(m.group(1)) or None
    qty = None
    m = re.search(r'</h1>\s*</div>\s*<div class="stack stack--top-4 display--none display--lg-block">\s*<div class="typo--footnote typo--book">(.*?)</div>', page, re.S)
    if m:
        qty = _text(m.group(1)) or None
    unit_text = None
    m = re.search(r"Běžná cena:\s*([^<]{3,40})", page)
    if m:
        unit_text = _text(m.group(1))
    crumbs = re.findall(r'"@type":\s*"ListItem",\s*"position":\s*\d+,\s*"name":\s*"([^"]+)"', page)
    category = crumbs[-2] if len(crumbs) >= 2 else None
    return {
        "url": url,
        "sku": sku,
        "ean": ean,
        "name": name,
        "brand": brand,
        "quantity_text": qty,
        "price": final,
        "original_price": original,
        "loyalty_price": loyalty,
        "unit_price_text": unit_text,
        "category": category_slug(" ".join(crumbs[1:])) or ("drogerie" if crumbs else None),
        "category_name": html.unescape(category) if category else None,
        "available": "Do košíku" in block,
    }


def fetch(url: str) -> dict | None:
    return parse_product(url, net.get_text(url, timeout=40, attempts=2))
