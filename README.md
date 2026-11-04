# slevomer-scraper

Zdroj **veřejného** repozitáře `slevomer-scraper` (Python, jen standardní knihovna, GitHub Actions).
Stahuje veřejná data a posílá je podepsaně (HMAC) na API Slevoměru. V kódu nejsou žádná tajemství —
adresa API a sdílený klíč jsou jen v GitHub Secrets (`SLV_API_BASE`, `SLV_INGEST_SECRET`).

| Workflow | Kdy | Co dělá |
|---|---|---|
| `stores.yml` | pondělí 02:17 UTC | všechny obchody v ČR z OpenStreetMap (Overpass, po krajích, více zrcadel) |
| `products.yml` | středa 03:43 UTC | produkty prodávané v ČR z Open Food Facts (search-a-licious) |
| `leaflets.yml` | 00:00 a 12:00 Praha | letáky Lidl, Kaufland, Penny, Billa, Albert, Tesco, Teta: stránky (WebP), akce ze strukturovaných dat nebo z PDF, pak zpracování na serveru (párování, historie cen, falešné slevy) |
| `notify.yml` | každou hodinu | upozornění a push (sledované produkty, letáky, končící slevy, týdenní souhrn, naplánované hromadné zprávy) |
| `eshop.yml` | denně 02:11 UTC | ceny z e-shopu Rossmann (jen fakta: EAN, ceny; ~3 000 stránek pomalu po jedné), nespárované EAN dohledá v Open Food Facts |

Ručně: `pip install -r requirements.txt`, `python -m slevomer_scraper.leaflets --chains lidl --max-pages 3`, `python -m slevomer_scraper.osm_stores --save cz.json` (jen stáhnout),
`python -m slevomer_scraper.osm_stores --file cz.json` (nahrát), `python -m slevomer_scraper.off_products --limit 300`.

Zdroje dat: © přispěvatelé OpenStreetMap (ODbL), Open Food Facts (ODbL, obrázky CC BY-SA).
Nikdy se nestahují agregátory slev (Kupi.cz apod.) — jen oficiální a otevřené zdroje.
