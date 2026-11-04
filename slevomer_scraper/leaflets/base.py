"""Společné typy zdrojů letáků (docs/PLAN.md § 4)."""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class LeafletRef:
    chain: str
    external_id: str
    title: str
    valid_from: str                 # YYYY-MM-DD
    valid_to: str
    pages: list[str] = field(default_factory=list)   # URL obrázků stránek v pořadí
    pdf_url: str | None = None
    source_url: str | None = None
    kind: str = "letak"             # letak / katalog
    variant: str | None = None
    extract: bool = True            # číst nabídky z PDF (u zdrojů se strukturovanými daty ne)
    hotlink_pages: bool = False     # stránky nenahrávat k nám, jen odkázat (zdroj robotům obrázky nedá)
    loyalty: bool = False           # celý leták platí jen s věrnostní kartou (Teta klubový leták)

    @property
    def content_hash(self) -> str:
        key = "|".join([self.chain, self.external_id, self.valid_from, self.valid_to, str(len(self.pages))])
        return hashlib.sha256(key.encode()).hexdigest()


class Source:
    chain: str = ""

    def leaflets(self) -> list[LeafletRef]:
        return []

    def structured_offers(self) -> list[dict]:
        """Nabídky ze strukturovaných dat webu obchodu (vrstva 1 — nejpřesnější)."""
        return []


def iso(d) -> str:
    """Datum z ISO řetězce / unix času / date → YYYY-MM-DD (čas Praha)."""
    if isinstance(d, (int, float)):
        return datetime.fromtimestamp(d).strftime("%Y-%m-%d")
    if isinstance(d, (date, datetime)):
        return d.strftime("%Y-%m-%d")
    s = str(d)
    if "T" in s and (s.endswith("Z") or "+" in s[10:]):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s[:10]


def czdate(s: str, year_hint: int | None = None) -> str | None:
    """„29.10.2026“ / „29. 10.“ → 2026-10-29."""
    import re
    m = re.search(r"(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})?", s)
    if not m:
        return None
    y = int(m.group(3) or year_hint or date.today().year)
    return f"{y:04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"


def to_webp(data: bytes, max_side: int = 1600, quality: int = 72) -> tuple[bytes, int, int]:
    from PIL import Image

    im = Image.open(io.BytesIO(data))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    im.thumbnail((max_side, max_side))
    out = io.BytesIO()
    im.save(out, "WEBP", quality=quality, method=4)
    return out.getvalue(), im.width, im.height


# Kategorie obchodů → naše kategorie produktů (stejné slugy jako migrace 003)
CATEGORY_HINTS = [
    ("alkohol", ("pivo", "víno", "lihovin", "alkohol", "cider")),
    ("napoje", ("nápoj", "voda", "káva", "čaj", "džus", "limon")),
    ("mlecne", ("mléč", "sýr", "jogurt", "máslo", "vejce", "mléko")),
    ("maso", ("maso", "uzenin", "drůbež", "šunk", "salám")),
    ("ryby", ("ryb", "mořsk")),
    ("ovoce-zelenina", ("ovoce", "zelenin", "f+v")),
    ("pecivo", ("pečivo", "chléb", "pekárn")),
    ("mrazene", ("mražen",)),
    ("sladkosti", ("sladk", "čokol", "snack", "cukrovin", "sušenk")),
    ("drogerie", ("drogerie", "kosmetik", "hygien", "prací", "čisticí")),
    ("zvirata", ("zvíř", "chovatel", "krmiv")),
    ("trvanlive", ("trvanliv", "konzerv", "těstovin", "pečení", "olej", "ocet", "koření", "mouka", "potraviny")),
]


def category_slug(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower()
    for slug, keys in CATEGORY_HINTS:
        if any(k in t for k in keys):
            return slug
    return None
