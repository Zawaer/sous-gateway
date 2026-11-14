"""State shared by every Sous deployment through Aiven for Valkey (Redis protocol).

Prod, staging, CI evals and demo scripts all use ONE Valio Aimo API key with undisclosed
rate limits. Each process only sees its own calls, so two busy deployments together got
HTTP 429. With SOUS_VALKEY_URL set, the Valio client (sous/aimo.py) uses Valkey for:

- a global call budget: a sliding one-minute window shared by all deployments, with a
  per-role cap (prod may use the whole window, other roles only a share of it, so CI and
  staging can never starve prod);
- a slice reserved for interactive traffic (SOUS_VALIO_INTERACTIVE_SHARE, default 0.3): background work
  (standing-order renewals, sweeps, the live demo buyers, CI evals) stops at the rest of the window, so a
  judge's sandbox quote always finds calls left; interactive (sandbox) calls may use the whole window;
- a shared circuit breaker: a 429 anywhere pauses every deployment until Retry-After;
- a shared offer cache: price/availability items per (kind, date, product id), so a
  quote on staging warms prod and vice versa (the in-process cache stays as L1).

Fail-open: any Valkey error is logged once, Valkey is skipped for RETRY_DOWN seconds and
the caller falls back to per-process behaviour. A quote never fails because Valkey is down.
The URL (it carries the password) is never logged or returned.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from urllib.parse import urlsplit

from . import config

log = logging.getLogger("sous.shared")

PREFIX = "sous:valio:"
RETRY_DOWN = 30.0  # after a Valkey error, skip it this long before trying again
WINDOW = 60  # seconds
# Share of the global budget each role may use (of everything used in the window, by anyone).
# prod can always use the remainder; the others stop at their share, leaving the rest to prod.
ROLE_SHARE = {"prod": 1.0, "staging": 0.5, "demo": 0.5, "ci": 0.5, "dev": 0.5}

URL = config.env("VALKEY_URL", "") or ""
ROLE = (config.env("DEPLOYMENT_ROLE", "dev") or "dev").strip().lower()
# 60 = the old per-process default (AIMO_CALLS_PER_MIN): prod alone never gets less than before Valkey.
GLOBAL_CALLS_PER_MIN = int(config.env("VALIO_GLOBAL_CALLS_PER_MIN", "60"))
_share_override = config.env("VALIO_ROLE_SHARE", "")


def _share(v: str | None, default: float) -> float:
    try:
        return min(0.9, max(0.0, float(v))) if v not in (None, "") else default
    except ValueError:
        return default


# Slice of every per-minute budget (shared and per-process) background work can never use: kept for interactive
# traffic (judge sandboxes, a human waiting on a quote). Capped at 0.9 so background work is never shut out.
INTERACTIVE_SHARE = _share(config.env("VALIO_INTERACTIVE_SHARE", "0.3"), 0.3)

_lock = threading.Lock()
_client = None  # redis.Redis, created lazily
_down_until = 0.0
_logged_down = False
_last_error: str | None = None
_stats = {"denied": 0, "cache_hits": 0, "errors": 0}


class OverBudget(Exception):
    def __init__(self, retry_after: int, used: float, limit: int):
        super().__init__(f"shared Valio Aimo budget used ({used:.0f}/{limit} this minute)")
        self.retry_after = retry_after
        self.used = used
        self.limit = limit


def configure(url: str | None = None, role: str | None = None, limit: int | None = None, client=None,
              interactive_share: float | None = None) -> None:
    """(Re)configure; tests pass a fakeredis client. url="" turns Valkey off."""
    global URL, ROLE, GLOBAL_CALLS_PER_MIN, INTERACTIVE_SHARE, _client, _down_until, _logged_down, _last_error
    with _lock:
        if interactive_share is not None:
            INTERACTIVE_SHARE = _share(str(interactive_share), INTERACTIVE_SHARE)
        if url is not None:
            URL = url
        if role is not None:
            ROLE = role
        if limit is not None:
            GLOBAL_CALLS_PER_MIN = limit
        _client = client
        _down_until = 0.0
        _logged_down = False
        _last_error = None
        _stats.update(denied=0, cache_hits=0, errors=0)


def enabled() -> bool:
    return bool(URL)


def role_limit(role: str | None = None) -> int:
    role = role or ROLE
    share = ROLE_SHARE.get(role, 0.5)
    if _share_override and role != "prod":
        try:
            share = min(1.0, max(0.0, float(_share_override)))
        except ValueError:
            pass
    return max(1, int(GLOBAL_CALLS_PER_MIN * share))


def background_cap(total: int) -> int:
    """The most of a `total` per-minute budget background work may use: the interactive slice stays free."""
    return max(1, int(total * (1.0 - INTERACTIVE_SHARE)))


def limit_for(priority: str = "normal") -> int:
    """This deployment's cap on the shared window for one class of traffic (aimo.priority()):
    interactive = the whole window (judges' sandbox quotes come first, whatever the role); normal = the role's cap;
    background = the role's cap, but never into the interactive slice."""
    if priority == "interactive":
        return GLOBAL_CALLS_PER_MIN
    lim = role_limit()
    return min(lim, background_cap(GLOBAL_CALLS_PER_MIN)) if priority == "background" else lim


def _redis():
    """The Valkey client, or None when unconfigured or recently down."""
    global _client
    if not URL or time.time() < _down_until:
        return None
    if _client is None:
        with _lock:
            if _client is None:
                try:
                    import redis  # optional at runtime: only needed when SOUS_VALKEY_URL is set
                    _client = redis.Redis.from_url(URL, socket_timeout=1.0, socket_connect_timeout=2.0,
                                                   health_check_interval=30, decode_responses=True)
                except Exception as e:  # ImportError, bad URL
                    _failed(e)
                    return None
    return _client


def _failed(e: Exception) -> None:
    """Fail open: log once per outage (never the URL), skip Valkey for a while."""
    global _down_until, _logged_down, _last_error
    with _lock:
        _down_until = time.time() + RETRY_DOWN
        _last_error = _describe(e)
        _stats["errors"] += 1
        first = not _logged_down
        _logged_down = True
    if first:
        log.warning("Valkey unreachable (%s): using per-process Valio budget and cache until it is back",
                    _last_error)


def _describe(e: Exception) -> str:
    """Exception type and message with the URL's host, port, user and password masked (safe for logs)."""
    msg = str(e)
    try:
        u = urlsplit(URL)
        for secret in (u.password, u.username, u.hostname, str(u.port) if u.port else None, URL):
            if secret:
                msg = msg.replace(secret, "***")
    except ValueError:
        msg = ""
    return f"{type(e).__name__}: {msg[:160]}" if msg else type(e).__name__


def _ok() -> None:
    global _logged_down, _last_error
    if _logged_down:
        log.warning("Valkey reachable again: shared Valio budget and cache restored")
    _logged_down = False
    _last_error = None


def host() -> str | None:
    """Host name only (no user, password or port) for /health."""
    try:
        return urlsplit(URL).hostname if URL else None
    except ValueError:
        return None


# --- global budget ---------------------------------------------------------------------------------

def _window(now: float) -> tuple[int, float]:
    minute = int(now // WINDOW)
    return minute, (now - minute * WINDOW) / WINDOW


def _used(cur: int, prev: int, elapsed: float) -> float:
    """Sliding-window estimate: the previous minute's count weighted by how much of it is still in the window."""
    return prev * (1.0 - elapsed) + cur


def take(priority: str = "normal") -> bool:
    """Take one call from the global budget.

    Returns True when Valkey accounted for it, False when Valkey is off/down (the caller
    falls back to its per-process budget). Raises OverBudget when this role's cap (for this
    class of traffic, limit_for) is reached."""
    r = _redis()
    if r is None:
        return False
    now = time.time()
    minute, elapsed = _window(now)
    cur_key, prev_key = f"{PREFIX}budget:{minute}", f"{PREFIX}budget:{minute - 1}"
    limit = limit_for(priority)
    try:
        p = r.pipeline(transaction=True)
        p.incr(cur_key)
        p.expire(cur_key, 2 * WINDOW + 5)
        p.get(prev_key)
        cur, _, prev = p.execute()
        used = _used(int(cur), int(prev or 0), elapsed)
        if used > limit:
            r.decr(cur_key)
            _ok()
            with _lock:
                _stats["denied"] += 1
            # When enough of the window will have slid by: at worst at the end of this minute.
            raise OverBudget(retry_after=max(1, int(WINDOW * (1 - elapsed)) + 1), used=used - 1, limit=limit)
        p = r.pipeline(transaction=False)
        role_key = f"{PREFIX}budget:{minute}:{ROLE}"
        p.incr(role_key)
        p.expire(role_key, 2 * WINDOW + 5)
        p.execute()
        _ok()
        return True
    except OverBudget:
        raise
    except Exception as e:
        _failed(e)
        return False


def usage() -> dict | None:
    """{used_this_minute, by_role} from Valkey, or None when off/down."""
    r = _redis()
    if r is None:
        return None
    now = time.time()
    minute, elapsed = _window(now)
    roles = sorted(ROLE_SHARE)
    try:
        vals = r.mget([f"{PREFIX}budget:{minute}", f"{PREFIX}budget:{minute - 1}"]
                      + [f"{PREFIX}budget:{minute}:{x}" for x in roles])
        _ok()
    except Exception as e:
        _failed(e)
        return None
    cur, prev = int(vals[0] or 0), int(vals[1] or 0)
    return {"used_this_minute": round(_used(cur, prev, elapsed), 1),
            "by_role": {x: int(v) for x, v in zip(roles, vals[2:]) if v}}


# --- shared circuit breaker ------------------------------------------------------------------------

def open_circuit(seconds: float) -> None:
    """Pause every deployment for `seconds` (never shortens a pause already set)."""
    r = _redis()
    if r is None:
        return
    key = f"{PREFIX}circuit_until"
    until = time.time() + seconds
    try:
        cur = r.get(key)
        if cur is None or float(cur) < until:
            r.set(key, f"{until:.3f}", px=max(1, int(seconds * 1000)))
        _ok()
    except Exception as e:
        _failed(e)


def circuit_until() -> float:
    """Epoch until which some deployment was told to back off (0 when closed, off or down)."""
    r = _redis()
    if r is None:
        return 0.0
    try:
        v = r.get(f"{PREFIX}circuit_until")
        _ok()
        return float(v) if v else 0.0
    except Exception as e:
        _failed(e)
        return 0.0


# --- shared offer cache ----------------------------------------------------------------------------

def _offer_key(kind: str, day: str, pid: str) -> str:
    return f"{PREFIX}offer:{kind}:{day}:{pid}"


def get_offers(kind: str, day: str, ids: list[str]) -> dict[str, tuple[float, dict]]:
    """pid -> (fetched_at, item) for every copy Valkey still holds (fresh or last-known)."""
    r = _redis()
    if r is None or not ids:
        return {}
    try:
        vals = r.mget([_offer_key(kind, day, pid) for pid in ids])
        _ok()
    except Exception as e:
        _failed(e)
        return {}
    out = {}
    for pid, v in zip(ids, vals):
        if not v:
            continue
        try:
            d = json.loads(v)
            out[pid] = (float(d["t"]), d["item"])
        except (ValueError, KeyError, TypeError):
            continue
    return out


def put_offers(kind: str, day: str, fetched_at: float, items: list[dict], keep: int) -> None:
    """Store items for `keep` seconds (their stale-copy horizon; freshness is judged from fetched_at)."""
    r = _redis()
    if r is None or not items:
        return
    try:
        p = r.pipeline(transaction=False)
        for item in items:
            p.set(_offer_key(kind, day, item["productId"]),
                  json.dumps({"t": fetched_at, "item": item}, separators=(",", ":")), ex=keep)
        p.execute()
        _ok()
    except Exception as e:
        _failed(e)


def count_hits(n: int) -> None:
    with _lock:
        _stats["cache_hits"] += n


def health(local_used: int, local_limit: int) -> dict:
    """For /health: valio_aimo.shared_budget. Never contains the URL or password."""
    base = {"role": ROLE, "configured": enabled()}
    u = usage() if enabled() else None
    if u is not None:
        return {**base, "backend": "valkey", "ok": True, "used_this_minute": u["used_this_minute"],
                "limit": GLOBAL_CALLS_PER_MIN, "role_limit": role_limit(),
                "background_limit": limit_for("background"), "interactive_share": INTERACTIVE_SHARE,
                "by_role": u["by_role"],
                "denied": _stats["denied"], "shared_cache_hits": _stats["cache_hits"]}
    return {**base, "backend": "local", "ok": not enabled(), "used_this_minute": local_used, "limit": local_limit,
            "error": _last_error if enabled() else None}
