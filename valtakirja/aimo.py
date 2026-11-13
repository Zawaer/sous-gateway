"""Client for Valio Aimo's mock price and availability APIs.

Batches product codes (the API takes 1-1000 per call), caches responses briefly
and counts calls so we can show how light the gateway is on Valio Aimo's systems.
"""

from __future__ import annotations

import threading
import time
from datetime import date

import httpx

from . import config

PRICE_TTL = 600  # prices change rarely within a session
AVAILABILITY_TTL = 60  # availability and cut-offs are time-sensitive

_lock = threading.Lock()
_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
stats = {"price_calls": 0, "availability_calls": 0, "cache_hits": 0}


class AimoError(RuntimeError):
    pass


_client = httpx.Client(
    base_url=config.AIMO_API_BASE,
    headers={"X-API-Key": config.AIMO_API_KEY, "User-Agent": "valtakirja-gateway/0.1"},
    timeout=20.0,
)


def _post(kind: str, requested: date, ids: list[str], ttl: int) -> dict[str, dict]:
    day = requested.isoformat()
    out: dict[str, dict] = {}
    missing: list[str] = []
    now = time.time()
    with _lock:
        for pid in dict.fromkeys(ids):
            hit = _cache.get((kind, day, pid))
            if hit and now - hit[0] < ttl:
                out[pid] = hit[1]
                stats["cache_hits"] += 1
            else:
                missing.append(pid)
    for i in range(0, len(missing), 1000):
        chunk = missing[i : i + 1000]
        body = {"customer": config.AIMO_CUSTOMER, "requestedDate": day, "products": chunk}
        for attempt in range(3):
            r = _client.post(f"/{kind}", json=body)
            if r.status_code != 429 and r.status_code < 500:
                break
            time.sleep(0.5 * 2**attempt)
        stats[f"{kind}_calls"] += 1
        if r.status_code != 200:
            raise AimoError(f"Valio Aimo {kind} API returned HTTP {r.status_code}: {r.text[:200]}")
        payload = r.json()
        with _lock:
            for item in payload.get("products", []):
                item = {**item, "_timeNow": payload.get("timeNow")}
                _cache[(kind, day, item["productId"])] = (now, item)
                out[item["productId"]] = item
    return out


def prices(ids: list[str], requested: date) -> dict[str, dict]:
    return _post("price", requested, ids, PRICE_TTL)


def availability(ids: list[str], requested: date) -> dict[str, dict]:
    return _post("availability", requested, ids, AVAILABILITY_TTL)
