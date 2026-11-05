"""HTTP pro zdroje letáků. Identifikujeme se poctivě jako robot s odkazem na stránku
pro obchody (žádost o odstranění). Ochrany proti robotům (CAPTCHA, Cloudflare/Akamai)
NIKDY neobcházíme — když zdroj odmítne, řetězec se přeskočí a zapíše do běhu."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; SlevomerBot/0.2; +https://slevy.zeddis.xyz/index.php/pro-obchody)"


class Blocked(RuntimeError):
    """Zdroj odmítl přístup (403/429/CAPTCHA) — neobcházet."""


def get(url: str, timeout: float = 60, headers: dict | None = None, data: bytes | None = None, attempts: int = 3) -> bytes:
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "cs-CZ,cs;q=0.9"}
    if headers:
        h.update(headers)
    last: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 429):
                raise Blocked(f"{e.code} {url}") from None
            if e.code < 500:
                raise
            last = e
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def get_json(url: str, **kw):
    return json.loads(get(url, headers={"Accept": "application/json"}, **kw))


def get_text(url: str, **kw) -> str:
    return get(url, **kw).decode("utf-8", errors="replace")


def post_json(url: str, payload: dict, **kw):
    body = json.dumps(payload).encode()
    return json.loads(get(url, data=body, headers={"Content-Type": "application/json", "Accept": "application/json"}, **kw))
