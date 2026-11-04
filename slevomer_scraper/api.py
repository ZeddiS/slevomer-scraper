"""Podepsaný klient Slevoměr API (jen stdlib).

Podpis musí odpovídat web/app/src/Hmac.php:
  HMAC-SHA256(secret, ts + "\n" + nonce + "\n" + METHOD + "\n" + path + "\n" + body)
kde path je logická cesta API (např. "/v1/ingest/ping"), nezávislá na tom,
jestli server běží s hezkými URL nebo přes /api/index.php/...

Konfigurace z proměnných prostředí (v GitHub Actions = Secrets):
  SLV_API_BASE       např. https://slevomer.zeddis.xyz/api/index.php
  SLV_INGEST_SECRET  sdílené tajemství (min. 32 znaků)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.request


class ApiError(RuntimeError):
    def __init__(self, status: int, payload: object):
        super().__init__(f"HTTP {status}: {payload}")
        self.status = status
        self.payload = payload


class SlevomerApi:
    def __init__(self, base: str | None = None, secret: str | None = None, timeout: float = 60):
        self.base = (base or os.environ.get("SLV_API_BASE", "")).rstrip("/")
        self.secret = secret or os.environ.get("SLV_INGEST_SECRET", "")
        self.timeout = timeout
        if not self.base:
            raise ValueError("Chybí SLV_API_BASE")
        if len(self.secret) < 32:
            raise ValueError("SLV_INGEST_SECRET chybí nebo je kratší než 32 znaků")

    def _sign(self, method: str, path: str, body: bytes) -> dict[str, str]:
        ts = str(int(time.time()))
        nonce = secrets.token_hex(16)
        msg = "\n".join([ts, nonce, method.upper(), path]).encode() + b"\n" + body
        sig = hmac.new(self.secret.encode(), msg, hashlib.sha256).hexdigest()
        return {"X-Slv-Timestamp": ts, "X-Slv-Nonce": nonce, "X-Slv-Signature": sig}

    def post(self, path: str, payload: dict, attempts: int = 3) -> dict:
        """Výpadek hostingu (5xx, síť) zkusí znovu — příjem dat je idempotentní. Každý pokus má nový podpis."""
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        for attempt in range(attempts):
            headers = {"Content-Type": "application/json", "User-Agent": "SlevomerScraper/0.1"}
            headers.update(self._sign("POST", path, body))
            req = urllib.request.Request(self.base + path, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")
                try:
                    payload_out: object = json.loads(raw)
                except ValueError:
                    payload_out = raw[:300]
                if e.code < 500 or attempt == attempts - 1:
                    raise ApiError(e.code, payload_out) from None
            except (urllib.error.URLError, TimeoutError, ValueError):
                if attempt == attempts - 1:
                    raise
            time.sleep(5 * (attempt + 1))
        raise RuntimeError("nedosažitelné")

    def get(self, path: str) -> dict:
        req = urllib.request.Request(self.base + path, headers={"User-Agent": "SlevomerScraper/0.1"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode())

    def tick_until_done(self, max_rounds: int = 200) -> list[dict]:
        """Volá /v1/cron/tick, dokud server nehlásí done (dávkové zpracování na hostingu)."""
        results = []
        for _ in range(max_rounds):
            res = self.post("/v1/cron/tick", {})
            results.append(res)
            if res.get("done"):
                break
        return results


if __name__ == "__main__":
    api = SlevomerApi()
    print("health:", api.get("/v1/health"))
    print("ping:  ", api.post("/v1/ingest/ping", {"echo": "ahoj"}))
    print("tick:  ", api.tick_until_done())
