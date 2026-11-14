"""Client for Valio Aimo's mock price and availability APIs.

Batches product codes (the API takes 1-1000 per call), caches responses briefly
and counts calls so we can show how light the gateway is on Valio Aimo's systems.

Valio Aimo's rate limits are undisclosed and one API key is shared by every
deployment, so the client degrades gracefully instead of failing a quote:

- 429 / 5xx: retried with jittered backoff, honouring Retry-After, but never
  waiting more than MAX_WAIT seconds per request in total.
- Circuit breaker: after a 429 no call goes out for Retry-After (or 10 s), for
  every request in the process, instead of hammering the API.
- Coalescing: concurrent requests for the same ids share one in-flight call.
- Stale-while-error: when a live call fails, the last copy fetched (up to
  PRICE_STALE_MAX / AVAILABILITY_STALE_MAX old) is returned with
  item["_stale_seconds"] set. quote.build_offer turns that into a DATA_STALE
  warning, which the mandate engine always sends to a human ("ask").

With SOUS_VALKEY_URL set (Aiven for Valkey, sous/shared.py) the budget, the circuit
breaker and the cache are also shared by every deployment using the key: a global
per-minute budget with a prod-first cap per role, a 429 anywhere pauses everyone, and
offers fetched by one deployment serve the others. If Valkey is unreachable the client
falls back to the per-process behaviour above.
"""

from __future__ import annotations

import contextvars
import random
import threading
import time
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from . import config, shared

PRICE_TTL = 600  # prices change rarely within a session
AVAILABILITY_TTL = 60  # availability and cut-offs are time-sensitive
PRICE_STALE_MAX = 900  # oldest last-known copy served when Valio Aimo can't be reached
AVAILABILITY_STALE_MAX = 300
MAX_WAIT = 8.0  # most a request waits on retries before falling back / failing
MAX_ATTEMPTS = 3
DEFAULT_PAUSE = 30.0  # circuit-breaker pause after a 429 without Retry-After (Valio: a 429 blocks the client for 30 s)
MAX_PAUSE = 300.0

_lock = threading.Lock()
# (kind, day, productId) -> (fetched_at, item). Entries are kept past their TTL as the last-known copy.
_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
stats = {"price_calls": 0, "availability_calls": 0, "cache_hits": 0}
# Kept out of `stats`: callers diff `stats` per request to report the calls a quote cost.
_health = {"rate_limited": 0, "server_errors": 0, "stale_served": 0, "coalesced": 0, "last_429_at": None}
_circuit_until = 0.0
_inflight: dict[tuple[str, str, frozenset[str]], "_Call"] = {}
_sleep = time.sleep  # patched in tests


class AimoError(RuntimeError):
    def __init__(self, message: str, retry_after: int | None = None, rate_limited: bool = False):
        super().__init__(message)
        self.retry_after = retry_after  # seconds until it's worth trying again, when known
        self.rate_limited = rate_limited


class BudgetLimit(AimoError):
    """Our own per-minute call budget is used up (this process's cap, config.AIMO_CALLS_PER_MIN). A short
    self-imposed wait with a known Retry-After, not a Valio outage: never answered with a stale copy (that
    would turn a routine order into DATA_STALE -> ask, at random, depending on timing). Callers retry."""


class SharedBudgetLimit(BudgetLimit):
    """This deployment's share of the budget every deployment shares in Valkey (sous/shared.py) is used up."""


class SandboxLimit(AimoError):
    """A judge sandbox (sandbox.py) is out of its share of live calls; never served from a stale copy."""


class Meter:
    """Set (via `meter`) while a sandbox operation runs: `allowed` False = its quota is used up;
    `live` becomes True once the operation made a live call (cached answers cost nothing)."""

    def __init__(self, sandbox: str, allowed: bool = True):
        self.sandbox, self.allowed, self.live = sandbox, allowed, False


meter: contextvars.ContextVar[Meter | None] = contextvars.ContextVar("aimo_sandbox_meter", default=None)
_calls: list[float] = []
_sandbox_calls: list[float] = []  # live calls made for judge sandboxes, their own per-minute sub-budget


def _prune(calls: list[float], now: float) -> None:
    while calls and now - calls[0] > 60:
        calls.pop(0)


def _take_budget() -> None:
    """Global cap on live Valio Aimo calls per minute (their rate limits are undisclosed); sandbox calls also
    count against a smaller shared sub-budget (config.SANDBOX_CALLS_PER_MIN). With SOUS_VALKEY_URL, also the
    budget shared by every deployment (sous/shared.py)."""
    now = time.time()
    m = meter.get()
    with _lock:
        _prune(_calls, now)
        if m is not None:
            if not m.allowed:
                raise SandboxLimit(f"Sandbox {m.sandbox} has used its {config.SANDBOX_QUOTES} live Valio Aimo "
                                   "lookups. Cached offers still work; create a new sandbox to keep going.",
                                   retry_after=3600, rate_limited=True)
            _prune(_sandbox_calls, now)
            if len(_sandbox_calls) >= config.SANDBOX_CALLS_PER_MIN:
                raise SandboxLimit("Sandboxes share a small budget of live Valio Aimo calls and it is used up "
                                   "for this minute; retry shortly.",
                                   retry_after=max(1, int(60 - (now - _sandbox_calls[0])) + 1), rate_limited=True)
        if len(_calls) >= config.AIMO_CALLS_PER_MIN:
            raise BudgetLimit("The gateway's budget of live Valio Aimo calls for this minute is used up; retry shortly.",
                            retry_after=max(1, int(60 - (now - _calls[0])) + 1), rate_limited=True)
        _calls.append(now)
        if m is not None:
            _sandbox_calls.append(now)
            was_live, m.live = m.live, True
    # Then the budget every deployment shares in Valkey (no-op when unset or unreachable). Outside the lock:
    # it is a network round trip. If it refuses, hand back the local slot this call took.
    try:
        shared.take()
    except shared.OverBudget as e:
        with _lock:
            if now in _calls:
                _calls.remove(now)
            if m is not None:
                if now in _sandbox_calls:
                    _sandbox_calls.remove(now)
                m.live = was_live
        raise SharedBudgetLimit(f"The shared budget of live Valio Aimo calls for this minute is used up "
                        f"({shared.ROLE} may use {e.limit} of {shared.GLOBAL_CALLS_PER_MIN}); retry shortly.",
                        retry_after=e.retry_after, rate_limited=True) from None


_client = httpx.Client(
    base_url=config.AIMO_API_BASE,
    headers={"X-API-Key": config.AIMO_API_KEY, "User-Agent": "sous-gateway/0.1"},
    timeout=20.0,
)


def _retry_after(r: httpx.Response) -> float | None:
    """Retry-After as seconds from now: either delta-seconds or an HTTP date."""
    v = (r.headers.get("retry-after") or "").strip()
    if not v:
        return None
    try:
        s = float(v)
    except ValueError:
        try:
            d = parsedate_to_datetime(v)
        except (TypeError, ValueError):
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        s = (d - datetime.now(timezone.utc)).total_seconds()
    return min(max(s, 0.0), MAX_PAUSE)


def _open_circuit(seconds: float, share: bool = True) -> None:
    global _circuit_until
    with _lock:
        _circuit_until = max(_circuit_until, time.time() + seconds)
    if share:
        shared.open_circuit(seconds)  # every other deployment pauses too


def _circuit_wait() -> float:
    """Seconds until calls may go out: this process's pause or one another deployment set in Valkey."""
    until = shared.circuit_until()
    if until > _circuit_until:
        _open_circuit(min(until - time.time(), MAX_PAUSE), share=False)
    return max(0.0, _circuit_until - time.time())


def _fetch(kind: str, day: str, chunk: list[str]) -> list[dict]:
    """One live call (with retries) for up to 1000 ids. Raises AimoError."""
    body = {"customer": config.AIMO_CUSTOMER, "requestedDate": day, "products": chunk}
    deadline = time.time() + MAX_WAIT
    for attempt in range(MAX_ATTEMPTS):
        wait = _circuit_wait()
        if wait > 0:
            if time.time() + wait > deadline or attempt == 0 and wait > 1:
                raise AimoError(f"Valio Aimo is rate-limiting the gateway; pausing calls for {wait:.0f} s.",
                                retry_after=int(wait) + 1, rate_limited=True)
            _sleep(wait)
        _take_budget()
        try:
            r = _client.post(f"/{kind}", json=body)
        except httpx.HTTPError as e:  # timeouts, connection errors, protocol errors
            raise AimoError(f"Valio Aimo {kind} API is unreachable: {type(e).__name__}") from e
        finally:
            with _lock:
                stats[f"{kind}_calls"] += 1
        if r.status_code == 429 or r.status_code >= 500:
            ra = _retry_after(r)
            if r.status_code == 429:
                pause = ra if ra is not None else DEFAULT_PAUSE
                _open_circuit(pause)
                with _lock:
                    _health["rate_limited"] += 1
                    _health["last_429_at"] = time.time()
            else:
                with _lock:
                    _health["server_errors"] += 1
            delay = ra + random.uniform(0, 0.3) if ra is not None else 0.5 * 2**attempt * random.uniform(0.5, 1.5)
            if attempt + 1 < MAX_ATTEMPTS and time.time() + delay <= deadline:
                _sleep(delay)
                continue
            limited = r.status_code == 429
            raise AimoError(
                f"Valio Aimo {kind} API returned HTTP {r.status_code}"
                + (" (rate limit)" if limited else f": {r.text[:200]}"),
                retry_after=int(ra if ra is not None else (DEFAULT_PAUSE if limited else 5)) + 1,
                rate_limited=limited)
        if r.status_code != 200:
            raise AimoError(f"Valio Aimo {kind} API returned HTTP {r.status_code}: {r.text[:200]}")
        try:
            payload = r.json()
            items = payload.get("products", [])
            if not isinstance(items, list):
                raise ValueError("'products' is not a list")
        except (ValueError, AttributeError) as e:  # json.JSONDecodeError is a ValueError
            raise AimoError(f"Valio Aimo {kind} API returned a malformed response: {e}") from e
        return [{**item, "_timeNow": payload.get("timeNow")}
                for item in items if isinstance(item, dict) and item.get("productId")]
    raise AssertionError("unreachable")


class _Call:
    """One in-flight live call that concurrent identical requests wait on."""

    def __init__(self, ids: frozenset[str]):
        self.ids = ids
        self.done = threading.Event()
        self.items: list[dict] | None = None
        self.error: AimoError | None = None


def _fetch_shared(kind: str, day: str, chunk: list[str]) -> list[dict]:
    """Run _fetch, or join an in-flight call that already asks for all of these ids."""
    want = frozenset(chunk)
    with _lock:
        call = next((c for (k, d, ids), c in _inflight.items() if k == kind and d == day and want <= ids), None)
        leader = call is None
        if leader:
            call = _inflight[(kind, day, want)] = _Call(want)
        else:
            _health["coalesced"] += 1
    if not leader:
        call.done.wait()
        if isinstance(call.error, SandboxLimit):  # another sandbox's limit is not ours: make the call ourselves
            return _fetch_shared(kind, day, chunk)
        if call.error:
            raise call.error
        return [i for i in call.items or [] if i["productId"] in want]
    try:
        call.items = _fetch(kind, day, chunk)
        fetched = time.time()
        with _lock:
            for item in call.items:
                _cache[(kind, day, item["productId"])] = (fetched, item)
        shared.put_offers(kind, day, fetched, call.items,
                          keep=PRICE_STALE_MAX if kind == "price" else AVAILABILITY_STALE_MAX)
        return call.items
    except Exception as e:
        call.error = e if isinstance(e, AimoError) else AimoError(f"Valio Aimo call failed: {type(e).__name__}")
        raise
    finally:
        with _lock:
            _inflight.pop((kind, day, want), None)
        call.done.set()


def _stale(kind: str, day: str, chunk: list[str], max_age: int, err: AimoError) -> dict[str, dict]:
    """Last-known copies of `chunk` young enough to serve, marked with their age."""
    now = time.time()
    out = {}
    with _lock:
        for pid in chunk:
            hit = _cache.get((kind, day, pid))
            if hit and now - hit[0] <= max_age:
                out[pid] = {**hit[1], "_stale_seconds": int(now - hit[0]),
                            "_stale_reason": "rate_limit" if err.rate_limited else "unavailable"}
        _health["stale_served"] += len(out)
    return out


def _from_shared(kind: str, day: str, missing: list[str], ttl: int, out: dict[str, dict]) -> list[str]:
    """L2: copies another deployment (or this one) put in Valkey. Fresh ones go to `out` and to L1. Older
    copies are only used when Valio Aimo itself fails (_shared_stale)."""
    found = shared.get_offers(kind, day, missing)
    if not found:
        return missing
    now = time.time()
    hits = 0
    with _lock:
        for pid, (t, item) in found.items():
            if now - t >= ttl:
                continue
            mine = _cache.get((kind, day, pid))
            if mine is None or mine[0] < t:
                _cache[(kind, day, pid)] = (t, item)
            out[pid] = item
            hits += 1
        stats["cache_hits"] += hits
    shared.count_hits(hits)
    return [pid for pid in missing if pid not in out]


def _shared_stale(kind: str, day: str, chunk: list[str]) -> None:
    """Valio Aimo failed (429, 5xx, unreachable, circuit open): adopt last-known copies from Valkey that are
    newer than ours, so _stale can serve a copy another deployment fetched."""
    found = shared.get_offers(kind, day, chunk)
    with _lock:
        for pid, (t, item) in found.items():
            mine = _cache.get((kind, day, pid))
            if mine is None or mine[0] < t:
                _cache[(kind, day, pid)] = (t, item)


def _post(kind: str, requested: date, ids: list[str], ttl: int, stale_max: int) -> dict[str, dict]:
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
    if missing:
        missing = _from_shared(kind, day, missing, ttl, out)
    for i in range(0, len(missing), 1000):
        chunk = missing[i : i + 1000]
        try:
            for item in _fetch_shared(kind, day, chunk):
                out[item["productId"]] = item
        except AimoError as e:
            if isinstance(e, (SandboxLimit, BudgetLimit)):
                raise
            _shared_stale(kind, day, chunk)
            stale = _stale(kind, day, chunk, stale_max, e)
            if not stale:
                raise
            # Ids without a recent copy are left out: the quote shows them as having no price/availability.
            out.update(stale)
    return out


def prices(ids: list[str], requested: date) -> dict[str, dict]:
    return _post("price", requested, ids, PRICE_TTL, PRICE_STALE_MAX)


def availability(ids: list[str], requested: date) -> dict[str, dict]:
    return _post("availability", requested, ids, AVAILABILITY_TTL, AVAILABILITY_STALE_MAX)


def health() -> dict:
    """For GET /health: how hard we're using the shared key and whether Valio Aimo is pushing back."""
    wait = _circuit_wait()  # may ask Valkey: outside the lock
    now = time.time()
    with _lock:
        local_used = sum(1 for t in _calls if now - t <= 60)
    budget = shared.health(local_used, config.AIMO_CALLS_PER_MIN)
    with _lock:
        last = _health["last_429_at"]
        return {
            "calls_last_minute": local_used,
            "budget_per_minute": config.AIMO_CALLS_PER_MIN,
            "sandbox_calls_last_minute": sum(1 for t in _sandbox_calls if now - t <= 60),
            "sandbox_budget_per_minute": config.SANDBOX_CALLS_PER_MIN,
            "circuit": "open" if wait > 0 else "closed",
            "circuit_retry_after_s": int(wait) + 1 if wait > 0 else 0,
            "last_429_at": datetime.fromtimestamp(last, config.TZ).isoformat(timespec="seconds") if last else None,
            "rate_limited_responses": _health["rate_limited"],
            "server_errors": _health["server_errors"],
            "stale_items_served": _health["stale_served"],
            "coalesced_requests": _health["coalesced"],
            "shared_budget": budget,
        }


def reset() -> None:
    """Forget cache, budget, circuit and counters (tests)."""
    global _circuit_until
    with _lock:
        _cache.clear()
        _calls.clear()
        _sandbox_calls.clear()
        _inflight.clear()
        _circuit_until = 0.0
        _health.update(rate_limited=0, server_errors=0, stale_served=0, coalesced=0, last_429_at=None)
