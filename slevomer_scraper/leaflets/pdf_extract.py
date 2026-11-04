"""Vytěžení nabídek z textové vrstvy PDF letáku (vrstva 2 pipeline, docs/PLAN.md § 4).

Heuristika podle polohy slov (pdfplumber):
  1. sloučí zdvojené znaky (stínované písmo — Kaufland), slova → řádky,
  2. najde ceny (69,90 · 69,- · „69“ + horní index „90“) a rozliší hlavní / původní / jednotkovou,
  3. ke každé hlavní ceně hledá název nad ní nebo vlevo (tučné písmo, nejbližší blok),
     množství („400 g“, „1 kg“) a slevu („-50 %“) v okolí.
Každá nabídka má `confidence` 0–100; pod 60 jde do fronty kontroly v adminu.
Je to záměrně jednoduchý a vyměnitelný modul — AI čtení (Claude vision) půjde zapnout později.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

PRICE_RE = re.compile(r"^(\d{1,4})[,.](\d{2}|-{1,2}|–)(/|\*)?$")
INT_RE = re.compile(r"^\d{1,4}$")
CENTS_RE = re.compile(r"^\d{2}$")
DISC_RE = re.compile(r"^[-–]\s?(\d{1,2})\s?%$")
QTY_RE = re.compile(r"(\d+(?:[,.]\d+)?\s?(?:x\s?\d+(?:[,.]\d+)?\s?)?(?:kg|g|l|ml|dl|cl|ks|kusů|kusy|kus|m|pr\.|tablet|rolí|role))\b", re.I)
UNIT_PRICE_HINT = re.compile(r"=|kč\s*/|/\s*kg|/\s*l\b|cena za 1 kg|1 kg|100 g|1 l\b|100 ml|1 ks =", re.I)
ORIG_HINT = re.compile(r"běžná|původ|doporuč|dříve|bez kart|bez klub", re.I)
CARD_HINT = re.compile(r"s kartou|s klubem|s aplikací|clubcard|lidl plus|bonus|kaufland card|penny kart", re.I)
NOISE = re.compile(r"^(akce|cena|naše|nase|naše cena|super|super cena|novinka|top|hit|jen|pouze|od|do|nyní|platí|kč|cz|%|mega|sleva|výhodné|balení|více druhů|ks|kus|zdarma|\d\+\d|lze libovolně|kombinovat|lze libovolně kombinovat|cena za \d.*|při koupi.*|cena za 1 ks.*|kupte \d.*|zaplaťte \d.*|víkend.*|od (pondělí|úterý|středy|čtvrtka|pátku|soboty|neděle).*|\*.*)$", re.I)
QTY_START = re.compile(r"^(\d+(?:[,.]\d+)?\s?(?:x\s?)?\d*\s?(?:kg|g|l|ml|ks|kus|kusy|kusů|svazek|balení|m)\b|balení\b|průměr|cena za|více druhů|\d+ druh)", re.I)
SLOGAN = re.compile(r"\b(ceny|cena|každý den|vyrobeno|trade|s\.r\.o|a\.s\.|výhodn|ušetř|sleva|akce|nabídka|novinka|dárek|soutěž|platí|vyprodání)\b", re.I)
CHAINS = {"albert", "lidl", "kaufland", "billa", "tesco", "penny", "globus", "norma"}
QTY_ONLY = re.compile(r"\d+(?:[,.]\d+)?\s*(?:x\s*\d+\s*)?(?:ks|kus|kusy|kusů|bal|balení|rolí|role|páry|párů|pár|kg|g|l|ml|dl|tablet|tablety)\.?", re.I)
BOLD = re.compile(r"bold|black|heavy|semibold|demi|extrabold", re.I)


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    bold: bool

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Line:
    words: list[Word] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def x0(self): return min(w.x0 for w in self.words)
    @property
    def x1(self): return max(w.x1 for w in self.words)
    @property
    def top(self): return min(w.top for w in self.words)
    @property
    def bottom(self): return max(w.bottom for w in self.words)
    @property
    def size(self): return max(w.size for w in self.words)
    @property
    def bold(self): return sum(1 for w in self.words if w.bold) >= len(self.words) / 2


@dataclass
class Price:
    value: float
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    kind: str = "main"     # main / orig / unit
    used: bool = False

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


def _words(page) -> list[Word]:
    try:
        page = page.dedupe_chars(tolerance=1)
    except Exception:  # noqa: BLE001 — starší pdfplumber
        pass
    raw = page.extract_words(extra_attrs=["size", "fontname"], keep_blank_chars=False, x_tolerance=1.5, y_tolerance=2)
    out = []
    for w in raw:
        t = w["text"].strip()
        if not t:
            continue
        # zbytky zdvojení „332299,,9900“ → „32,90“
        if re.fullmatch(r"(\d)\1(\d)\2,,(\d)\3(\d)\4", t):
            t = t[0] + t[2] + "," + t[6] + t[8]
        out.append(Word(t, w["x0"], w["x1"], w["top"], w["bottom"], round(float(w.get("size") or 0), 1), bool(BOLD.search(w.get("fontname") or ""))))
    return out


def _lines(words: list[Word]) -> list[Line]:
    lines: list[Line] = []
    for w in sorted(words, key=lambda w: (round(w.top), w.x0)):
        for ln in lines:
            last = ln.words[-1]
            if abs(ln.top - w.top) < max(2.0, w.size * 0.35) and 0 <= w.x0 - last.x1 < max(6.0, w.size * 0.9) and abs(last.size - w.size) < 3:
                ln.words.append(w)
                break
        else:
            lines.append(Line([w]))
    return lines


def _prices(words: list[Word]) -> list[Price]:
    prices: list[Price] = []
    used = set()
    for i, w in enumerate(words):
        if i in used:
            continue
        m = PRICE_RE.match(w.text)
        if m:
            cents = m.group(2)
            val = float(m.group(1)) + (0 if not cents.isdigit() else int(cents) / 100)
            p = Price(val, w.x0, w.x1, w.top, w.bottom, w.size)
            if m.group(3) == "/":
                p.kind = "orig"
            prices.append(p)
            continue
        # „69“ velkým písmem + „90“ malým vpravo nahoře (horní index)
        if INT_RE.match(w.text) and w.size >= 14:
            for j, v in enumerate(words):
                if j == i or j in used or not CENTS_RE.match(v.text):
                    continue
                if 0 <= v.x0 - w.x1 < w.size * 0.6 and v.size < w.size * 0.8 and w.top - 2 <= v.top <= w.top + w.size * 0.5:
                    prices.append(Price(float(w.text) + int(v.text) / 100, w.x0, v.x1, w.top, w.bottom, w.size))
                    used.update({i, j})
                    break
    return prices


def _classify(prices: list[Price], lines: list[Line]) -> None:
    if not prices:
        return
    biggest = max(p.size for p in prices)
    for p in prices:
        ctx = " ".join(ln.text for ln in lines if abs(ln.top - p.top) < max(4, p.size * 0.6) and ln.x0 - 120 < p.x0 < ln.x1 + 10)
        if p.kind == "orig" or ORIG_HINT.search(ctx):
            p.kind = "orig"
        elif UNIT_PRICE_HINT.search(ctx) and p.size < biggest * 0.7:
            p.kind = "unit"
        elif p.size < biggest * 0.45:
            p.kind = "small"


def _near(a_x0, a_x1, b_x0, b_x1, slack=40.0) -> bool:
    return a_x0 - slack < b_x1 and b_x0 - slack < a_x1


def _is_brand(t: str) -> bool:
    """Řádek se značkou: VELKÁ PÍSMENA, max 4 slova (Kaufland: „KUNÍN“ nad „Pohár mléčný“)."""
    t = t.strip()
    letters = [c for c in t if c.isalpha()]
    return bool(letters) and len(t.split()) <= 4 and sum(1 for c in letters if c.isupper()) / len(letters) > 0.85


def _name_like(t: str) -> bool:
    if len(t) < 3 or PRICE_RE.match(t) or DISC_RE.match(t) or NOISE.match(t):
        return False
    # odrážky s parametry („• vybrané druhy“, „• 31,90 Kč“) a nálepky typu „MIMOŘÁDNĚ BEZ APLIKACE“
    if t[0] in "•·▪*▼◆-–" or re.search(r"kč", t, re.I) or re.search(r"(?i)bez aplikace|s aplikací|vybrané druhy|více druhů|běžná cena", t):
        return False
    if re.fullmatch(r"[\d\s,.%/=*+–-]+", t) or not re.search(r"[A-Za-zÁ-žá-ž]{2}", t):
        return False
    if UNIT_PRICE_HINT.search(t) and re.search(r"\d", t) and ("=" in t or "kč" in t.lower()):
        return False
    # Otočený text na okraji stránky se rozsype po písmenech („a k e m os K“)
    tokens = t.split()
    loose = [w for w in tokens if len(w) <= 2 and w.isalpha() and w.lower() not in ("ks", "kg", "g", "l", "ml", "dl", "cl")]
    if len(tokens) >= 3 and len(loose) / len(tokens) >= 0.6:
        return False
    # Samotné množství nebo zbytek věty („/1 ks“, „6 bal“, „24 rolí“, „a cena“, „Ušetřete“)
    if t[0] == "/" or QTY_ONLY.fullmatch(t) or re.fullmatch(r"(?i)(a |i |s )?(cena|ušetřete|ušetři|výhodně)", t):
        return False
    return True


def extract_page(page, page_no: int) -> list[dict]:
    words = _words(page)
    if len(words) < 5:
        return []
    lines = _lines(words)
    prices = _prices(words)
    _classify(prices, lines)
    mains = [p for p in prices if p.kind == "main"]
    W, H = float(page.width), float(page.height)
    offers = []
    for p in sorted(mains, key=lambda p: (p.top, p.x0)):
        # Kandidátní řádky: nad cenou (do ~28 % výšky stránky) nebo vlevo na stejné úrovni
        cands = []
        for ln in lines:
            t = ln.text.strip()
            if not _name_like(t):
                continue
            above = p.top - ln.bottom
            if 0 <= above < H * 0.28 and _near(ln.x0, ln.x1, p.x0, p.x1, 90):
                dist = above + abs(ln.x0 - p.x0) * 0.25
            elif abs(ln.top - p.top) < p.size * 1.6 and 0 < p.x0 - ln.x1 < 160:
                dist = (p.x0 - ln.x1) * 0.6 + 8
            else:
                continue
            cands.append((dist, ln))
        if not cands:
            continue
        cands.sort(key=lambda c: c[0])
        anchor = cands[0][1]
        # Blok textu: od nejbližšího řádku nahoru, dokud řádky navazují (zarovnání vlevo, malá mezera)
        block = [anchor]
        cur = anchor
        for _ in range(4):
            up = [ln for ln in lines if ln is not cur and 0 <= cur.top - ln.bottom < max(cur.size, ln.size) * 1.1
                  and (abs(ln.x0 - cur.x0) < 18 or abs((ln.x0 + ln.x1) / 2 - (cur.x0 + cur.x1) / 2) < 18) and _name_like(ln.text.strip())]
            if not up:
                break
            cur = min(up, key=lambda ln: cur.top - ln.bottom)
            block.insert(0, cur)
        # Název = horní řádky bloku s největším písmem (popisky a gramáž pod nimi vynechat)
        top_size = max(ln.size for ln in block[:2])
        name_parts = []
        for ln in block:
            first_is_brand = len(name_parts) == 1 and _is_brand(name_parts[0].text)
            if name_parts and QTY_START.match(ln.text.strip()):
                break
            if name_parts and ln.size < top_size * 0.82 and not first_is_brand:
                break
            if not QTY_START.match(ln.text.strip()):
                name_parts.append(ln)
            if len(name_parts) >= 3:
                break
        if not name_parts:
            continue
        name_ln = name_parts[0]
        brand = None
        if len(name_parts) > 1 and _is_brand(name_parts[0].text):
            brand = name_parts[0].text.strip().title() if name_parts[0].text.isupper() else name_parts[0].text.strip()
        name = " ".join(ln.text for ln in name_parts)
        name = re.sub(r"\s+", " ", name).strip(" ,.-")
        if len(name) < 3 or len(name) > 120:
            continue

        # Okolí karty: oblast mezi názvem a cenou ± okraje
        top = min(name_ln.top, p.top) - 5
        bottom = p.bottom + p.size * 2.5
        x0 = min(name_ln.x0, p.x0) - 30
        x1 = max(name_ln.x1, p.x1) + 60
        area = [ln for ln in lines if ln.top >= top and ln.bottom <= bottom and _near(ln.x0, ln.x1, x0, x1, 0)]
        area_text = " | ".join(ln.text for ln in area)

        qty = None
        for ln in area:
            m = QTY_RE.search(ln.text)
            if m and not UNIT_PRICE_HINT.search(ln.text):
                qty = m.group(1)
                break
        disc = None
        for i, w in enumerate(words):
            if not (top - 60 < w.top < bottom + 10 and x0 - 60 < w.x0 < x1 + 60):
                continue
            m = DISC_RE.match(w.text)
            if not m:
                m2 = re.match(r"^(\d{1,2})\s?%$", w.text)
                prev = words[i - 1] if i else None
                if m2 and prev and prev.text in ("-", "–") and abs(prev.top - w.top) < 6:
                    m = m2
            if m and 0 < int(m.group(1)) < 100:
                disc = int(m.group(1))
                break
        orig = None
        for q in prices:
            if q.kind == "orig" and not q.used and top - 40 < q.top < bottom + 20 and x0 - 60 < q.x0 < x1 + 60 and q.value > p.value:
                orig = q.value
                q.used = True
                break
        card = bool(CARD_HINT.search(area_text))
        # Původní cena musí dávat smysl: víc než 3× akční cena jen když to potvrzuje i sleva v %
        if orig is not None:
            implied = round((1 - p.value / orig) * 100)
            if (orig > p.value * 3 or implied > 70) and not (disc and abs(implied - disc) <= 2):
                orig = None

        conf = 45
        if name_ln.bold:
            conf += 12
        if len(name_parts) == 1 and _is_brand(name):
            conf -= 25   # jen značka bez názvu produktu → ke kontrole
        if name_ln.size >= 9:
            conf += 5
        if qty:
            conf += 12
        if orig and disc and abs(round((1 - p.value / orig) * 100) - disc) <= 2:
            conf += 18
        elif orig or disc:
            conf += 8
        if p.size < 12:
            conf -= 10
        # Ceny v obchodech končí skoro vždy na ,x0 / ,x9 / ,x5 — jiná koncovka bývá jednotková cena
        if round(p.value * 100) % 10 not in (0, 9, 5):
            conf -= 25
        if orig is not None and round(orig * 100) % 10 not in (0, 9, 5):
            orig = None
        low = name.lower()
        if low in CHAINS or (brand and brand.lower() in CHAINS and len(name_parts) == 1):
            conf -= 35   # jen název řetězce
        if SLOGAN.search(name) and (name.isupper() or len(name.split()) > 4):
            conf -= 30   # reklamní slogan, ne produkt
        if re.search(r"\d+\s?g\b|\d+\s?ml\b", low) and len(name.split()) <= 4 and name.isupper():
            conf -= 15
        if p.value < 1 or p.value > 50000:
            conf -= 40
        conf = max(0, min(95, conf))

        offers.append({
            "name": name,
            "brand": brand,
            "price": round(p.value, 2),
            "original_price": orig,
            "discount_pct": disc,
            "quantity_text": qty,
            "loyalty_required": card,
            "page_no": page_no,
            "bbox": [round(max(0, x0) / W, 4), round(max(0, top) / H, 4), round(min(W, x1 - x0) / W, 4), round(min(H, bottom - top) / H, 4)],
            "confidence": conf,
            "raw_text": area_text[:1500],
            "source": "pdf",
        })
    return offers


def extract_pdf(path: str, max_pages: int = 120) -> list[dict]:
    import pdfplumber

    out: list[dict] = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages[:max_pages], start=1):
            try:
                out.extend(extract_page(page, i))
            except Exception as e:  # noqa: BLE001 — jedna rozbitá stránka nesmí shodit celý leták
                print(f"    stránka {i}: {e}")
    # duplicity (stejný název a cena na stejné stránce)
    seen = set()
    uniq = []
    for o in out:
        k = (o["page_no"], o["name"].lower(), o["price"])
        if k not in seen:
            seen.add(k)
            uniq.append(o)
    return uniq
