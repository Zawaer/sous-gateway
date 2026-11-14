"""Stock reservations: many buyers' agents ordering the same short product never over-allocate Valio's stock.

A quote is sized to the stock Valio reports, but N quotes made before any order would each see all of it. So
every order that goes through (or is held for a human) reserves its quantity per (product, delivery date), and
every later quote and order sees

    left = Valio's last known available amount - active reservations

An order whose line asks for more than is left is refused with the blocking issue ALLOCATED (rule
`availability.allocated`, owner valio); the refused order carries `allocation` (what was asked, what is left)
and `options` with a ready re-quote for what's left. docs/PROTOCOL.md 5.3.

A reservation ends when
- Valio's own availability reflects it: a newer observation shows the stock fell by at least the reserved
  amount since the oldest open reservation was made (oldest first: our orders arriving at Valio);
- its TTL passes (SOUS_RESERVATION_TTL_MINUTES, default 30) for an order that went through; a held order's
  tentative reservation lasts until its approve_by (approval makes it firm, a rejection or expiry releases it);
- the order is refused at approval or never stored.

Fair share when short, set by Valio (not by a buyer's mandate): SOUS_ALLOCATION
  first_come (default) whoever orders first gets it, up to what's left;
  pro_rata             when the demand on a (product, date) exceeds the stock, each buyer is capped at a share of
                       it in proportion to their last 4 weeks' ordered volume of the product (a buyer with no
                       history counts as the smallest regular buyer); a buyer asking for less than their share
                       leaves the rest to the others (water filling). Demand = what buyers asked for in quotes
                       and orders of the last RESERVATION_TTL minutes.

Pools: a judge sandbox's orders reserve only within that sandbox (each sandbox is its own world, keyed by its
id), so one visitor's demo order never takes another's stock; real and curated mandates share the global pool.
An eval runner gives each case its own pool (set_scope), so cases never see each other's orders. Reflection by
Valio's refresh applies to the global pool only (sandbox orders never reach Valio). Valio's mock never lowers
its stock, so with SOUS_RESERVATION_RELEASE_UNCHANGED=1 a reservation also ends once a fresh availability
reading (older than aimo's 60 s cache) still shows the amount it was made against: protection against a burst,
not a 30-minute hold on data that never moves.

Storage: the `stock_reservation`, `stock_demand` and `stock_observation` tables (created lazily, like holds). With
SOUS_VALKEY_URL set, reservations are mirrored in one Valkey hash per (product, date) and the check-and-reserve
is one optimistic WATCH/MULTI transaction there, so several deployments share them; Valkey down falls back to
the database (fail-open, sous/shared.py). The callers hold service._order_lock, so one process never races itself.
"""

from __future__ import annotations

import json
import math
import secrets
import threading
import time
from datetime import datetime, timedelta

from . import catalog, config, shared, store

MODES = ("first_come", "pro_rata")
VOLUME_DAYS = 28
EPS = 1e-9

_SQLITE = [
    """CREATE TABLE IF NOT EXISTS stock_reservation (
  id TEXT PRIMARY KEY, order_id TEXT NOT NULL, product_id TEXT NOT NULL, delivery_date TEXT NOT NULL,
  buyer TEXT, quantity REAL NOT NULL, base REAL NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS stock_demand (
  buyer TEXT NOT NULL, product_id TEXT NOT NULL, delivery_date TEXT NOT NULL, quantity REAL NOT NULL,
  expires_at TEXT NOT NULL, PRIMARY KEY (buyer, product_id, delivery_date))""",
    """CREATE TABLE IF NOT EXISTS stock_observation (
  product_id TEXT NOT NULL, delivery_date TEXT NOT NULL, amount REAL NOT NULL, observed_at TEXT NOT NULL,
  PRIMARY KEY (product_id, delivery_date))""",
]
_MARIADB = [
    """CREATE TABLE IF NOT EXISTS stock_reservation (
  id VARCHAR(32) PRIMARY KEY, order_id VARCHAR(32) NOT NULL, product_id VARCHAR(64) NOT NULL,
  delivery_date VARCHAR(10) NOT NULL, buyer VARCHAR(191), quantity DOUBLE NOT NULL, base DOUBLE NOT NULL,
  kind VARCHAR(16) NOT NULL, status VARCHAR(16) NOT NULL, created_at VARCHAR(40) NOT NULL,
  expires_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL,
  INDEX (product_id, delivery_date), INDEX (order_id))""",
    """CREATE TABLE IF NOT EXISTS stock_demand (
  buyer VARCHAR(191) NOT NULL, product_id VARCHAR(64) NOT NULL, delivery_date VARCHAR(10) NOT NULL,
  quantity DOUBLE NOT NULL, expires_at VARCHAR(40) NOT NULL, PRIMARY KEY (buyer, product_id, delivery_date))""",
    """CREATE TABLE IF NOT EXISTS stock_observation (
  product_id VARCHAR(64) NOT NULL, delivery_date VARCHAR(10) NOT NULL, amount DOUBLE NOT NULL,
  observed_at VARCHAR(40) NOT NULL, PRIMARY KEY (product_id, delivery_date))""",
]
COLS = ["id", "order_id", "product_id", "delivery_date", "buyer", "quantity", "base", "kind", "status",
        "created_at", "expires_at", "updated_at"]

_ready_for = None
_override = ""  # pool for mandates outside a sandbox: "" (global) unless an eval runner set one (set_scope)
_seen: dict = {}  # (table, key) -> last value written: skip rewriting unchanged rows (each write is a commit)
_vol_cache: dict = {}
_vol_lock = threading.Lock()
VOLUME_CACHE_SECONDS = 60.0


def mode() -> str:
    m = (config.env("ALLOCATION", "first_come") or "").strip().lower()
    return m if m in MODES else "first_come"


def ttl() -> timedelta:
    try:
        return timedelta(minutes=float(config.env("RESERVATION_TTL_MINUTES", "30")))
    except ValueError:
        return timedelta(minutes=30)


EVAL_SCOPE_TTL = 900  # seconds a runner scope's Valkey hash may outlive its last write (crashed runner)
_scope_keys: set[tuple[str, str]] = set()  # (pool key, day) written under the current runner scope


def set_scope(name: str | None) -> None:
    """Eval runners: put the orders of mandates outside a sandbox in their own pool (one per case), or back in
    the global one (None). Leaving a scope ends its reservations (database and Valkey): nothing of one case
    survives into the next, nor into another run sharing the Valkey."""
    global _override
    new = (name or "").replace("/", "-")
    if _override and new != _override:
        end_scope(_override)
    _override = new


def end_scope(scope: str) -> None:
    """End every reservation of a runner scope and delete its Valkey hashes."""
    if not scope:
        return
    try:
        _ensure()
        store._exec("UPDATE stock_reservation SET status = ? WHERE product_id LIKE ? AND status = ?",
                    ("scope_ended", f"{scope}/%", "active"))
    except Exception:  # noqa: BLE001 - best effort: the scope's keys expire anyway
        pass
    keys = [_key(pk, day) for pk, day in _scope_keys if pk.startswith(scope + "/")]
    _scope_keys.difference_update({(pk, day) for pk, day in _scope_keys if pk.startswith(scope + "/")})
    r = shared._redis()
    if r is not None and keys:
        try:
            r.delete(*keys)
            shared._ok()
        except Exception as e:  # noqa: BLE001
            shared._failed(e)


def _hash_ttl(pk: str) -> int:
    """Valkey expiry of a pool's hash: the reservation TTL, and short for a runner scope's."""
    t = int(ttl().total_seconds())
    if _override and pk.startswith(_override + "/"):
        return min(t, EVAL_SCOPE_TTL)
    return t


def pool_of(mandate) -> str:
    """The reservation pool for a mandate: its sandbox, else the runner's scope, else global ("")."""
    sbx = getattr(mandate, "sandbox", None) if mandate is not None else None
    return (sbx or _override or "").replace("/", "-")


def _pk(pid: str, scope: str) -> str:
    """Pool key: the product id itself in the global pool, '<scope>/<product id>' in another."""
    return f"{scope}/{pid}" if scope else pid


def _pid(pk: str) -> str:
    return pk.rsplit("/", 1)[-1]


def _release_unchanged() -> bool:
    return config.env("RESERVATION_RELEASE_UNCHANGED", "0") == "1"


def _ensure() -> None:
    global _ready_for
    with store._lock:
        conn = store.db()
        if _ready_for is conn:
            return
        for stmt in (_MARIADB if config.DB_URL else _SQLITE):
            store._exec(stmt)
        _seen.clear()  # another database (tests swap them): nothing in it was written by us yet
        _ready_for = conn


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="milliseconds")


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _key(pid: str, day: str) -> str:
    # per deployment role: prod, staging and CI share one Valkey for the Valio budget, but staging's demo orders
    # must not take prod's stock; replicas of one deployment (same role) do share their reservations
    return f"{shared.PREFIX}resv:{shared.ROLE}:{pid}:{day}"


def _obs_key(pid: str, day: str) -> str:
    return f"{shared.PREFIX}resv-obs:{pid}:{day}"


def _floor(qty: float, pk: str) -> float:
    p = catalog.get(_pid(pk)) or {}
    lot = p.get("allowed_lot_size")
    if p.get("sales_unit") == "KG":
        step = lot or 0.01
        return round(math.floor(qty / step + EPS) * step, 2)
    if lot and lot >= 1:
        return math.floor(qty / lot + EPS) * lot
    return float(math.floor(qty + EPS))


# --- observations: Valio's last known available amount ------------------------------------------

def observe(pid: str, day: str, amount: float, now: datetime, fetched_at: datetime | None = None) -> None:
    """Record what Valio reports and end the reservations it reflects. `fetched_at`: when Valio actually
    returned this reading (business time; a cache hit is older than `now`), None when unknown."""
    _ensure()
    if _seen.get(("obs", pid, day)) == float(amount):
        if _release_unchanged() and fetched_at is not None:  # only a reading with a known fetch time counts
            _settle_global(pid, day, float(amount), now, fetched_at)
        return  # otherwise nothing new: no rewrite
    _seen[("obs", pid, day)] = float(amount)
    store._upsert("stock_observation", ["product_id", "delivery_date", "amount", "observed_at"],
                  (pid, day, float(amount), _iso(now)))
    r = shared._redis()
    if r is not None:
        try:
            r.set(_obs_key(pid, day), json.dumps({"amount": float(amount), "at": _iso(now)}),
                  ex=int(ttl().total_seconds()) + 3600)
            shared._ok()
        except Exception as e:  # noqa: BLE001 - fail open
            shared._failed(e)
    _settle_global(pid, day, float(amount), now, fetched_at)  # the global pool's reflected reservations end now


def _settle_global(pid: str, day: str, amount: float, now: datetime, fetched_at: datetime | None) -> None:
    """A Valio reading came in, fetched from Valio at `fetched_at`: settle the global pool against it, in the
    database and Valkey. Only here may a reservation end as 'unchanged' (SOUS_RESERVATION_RELEASE_UNCHANGED), and
    only against the reading's real fetch time: never the stored observation alone or a cache hit's quote time."""
    _settle_db(pid, day, amount, now, fresh_at=fetched_at)
    r = shared._redis()
    if r is None or not _release_unchanged() or fetched_at is None:
        return

    def tx(pipe):
        raw = pipe.hgetall(_key(pid, day))
        active, ended = _settle([json.loads(v) for v in raw.values()], amount, now, fresh_at=fetched_at)
        pipe.multi()
        if ended:
            pipe.hdel(_key(pid, day), *ended)
        for y in active:
            if y.pop("_rebased", None):
                pipe.hset(_key(pid, day), y["id"], json.dumps(y))

    try:
        r.transaction(tx, _key(pid, day))
        shared._ok()
    except Exception as e:  # noqa: BLE001
        shared._failed(e)


def last_amount(pid: str, day: str) -> float | None:
    _ensure()
    rows = store._exec("SELECT amount FROM stock_observation WHERE product_id = ? AND delivery_date = ?",
                       (pid, day))[0]
    db_amount = float(rows[0]["amount"]) if rows else None
    r = shared._redis()
    if r is not None:
        try:
            v = r.get(_obs_key(pid, day))
            shared._ok()
            if v:
                return float(json.loads(v)["amount"])
        except Exception as e:  # noqa: BLE001
            shared._failed(e)
    return db_amount


def _settle(resv: list[dict], amount: float | None, now: datetime,
            fresh_at: datetime | None = None) -> tuple[list[dict], dict[str, str]]:
    """(still active, {id: 'expired'|'reflected'}). Reflected: Valio's stock fell since the oldest open
    reservation was made; that fall covers reservations oldest first. The ones still active get their `base`
    lowered by what was reflected (Valio's amount with those orders counted), so a fall is never counted twice;
    a changed base is marked x["_rebased"] for the caller to persist. `fresh_at`: when `amount` is a Valio
    reading that just came in; only then may SOUS_RESERVATION_RELEASE_UNCHANGED end a reservation, and only if
    the reading is from after it (past aimo's 60 s cache)."""
    ended: dict[str, str] = {}
    active = []
    unchanged_after = timedelta(seconds=60)  # aimo.AVAILABILITY_TTL: a reading after it is fresh from Valio
    for x in sorted(resv, key=lambda x: x["created_at"]):
        if _dt(x["expires_at"]) <= now:
            ended[x["id"]] = "expired"
        elif (fresh_at is not None and amount is not None and _release_unchanged()
              and abs(amount - x["base"]) < EPS and fresh_at - _dt(x["created_at"]) >= unchanged_after):
            ended[x["id"]] = "unchanged"
        else:
            active.append(x)
    if amount is not None and active:
        fall, cum, keep = active[0]["base"] - amount, 0.0, []
        for x in active:
            cum += x["quantity"]
            if not keep and fall > EPS and cum <= fall + EPS and _dt(x["created_at"]) <= now:
                ended[x["id"]] = "reflected"
            else:
                keep.append(x)
        if done := sum(x["quantity"] for x in active if x["id"] in ended):
            for x in keep:
                x["base"], x["_rebased"] = x["base"] - done, True
        active = keep
    return active, ended


def _rows(pid: str, day: str) -> list[dict]:
    return store._exec("SELECT * FROM stock_reservation WHERE product_id = ? AND delivery_date = ? AND status = ?",
                       (pid, day, "active"))[0]


def _valio(pk: str, amount: float | None) -> float | None:
    """The amount to settle a pool against: Valio's, for the global pool; none (TTL only) for a sandbox's or an
    eval case's pool, whose orders never reach Valio."""
    return None if "/" in pk else amount


def _settle_db(pid: str, day: str, amount: float | None, now: datetime,
               fresh_at: datetime | None = None) -> list[dict]:
    active, ended = _settle(_rows(pid, day), _valio(pid, amount), now, fresh_at if "/" not in pid else None)
    for rid, why in ended.items():
        store._exec("UPDATE stock_reservation SET status = ?, updated_at = ? WHERE id = ?", (why, _iso(now), rid))
    for x in active:
        if x.pop("_rebased", None):
            store._exec("UPDATE stock_reservation SET base = ? WHERE id = ?", (x["base"], x["id"]))
    return active


# --- demand and shares (pro_rata) -----------------------------------------------------------------

def note_demand(buyer: str | None, pid: str, day: str, quantity: float | None, now: datetime) -> None:
    """A buyer asked for `quantity` (sales units) of the product for the day (a quote or an order)."""
    if not buyer or not quantity:
        return
    _ensure()
    k, exp = ("demand", buyer, pid, day), now + ttl()
    if (last := _seen.get(k)) and last[0] == float(quantity) and last[1] > now + ttl() / 2:
        return
    _seen[k] = (float(quantity), exp)
    store._upsert("stock_demand", ["buyer", "product_id", "delivery_date", "quantity", "expires_at"],
                  (buyer, pid, day, float(quantity), _iso(exp)))


def _demand(pid: str, day: str, now: datetime) -> dict[str, float]:
    rows = store._exec("SELECT buyer, quantity, expires_at FROM stock_demand WHERE product_id = ? AND delivery_date = ?",
                       (pid, day))[0]
    return {r["buyer"]: float(r["quantity"]) for r in rows if _dt(r["expires_at"]) > now}


def volumes(pid: str, now: datetime) -> dict[str, float]:
    """{buyer: sales units of the product in orders that went through in the last 4 weeks}."""
    k = (pid, id(store._conn))
    with _vol_lock:
        hit = _vol_cache.get(k)
        if hit and time.monotonic() - hit[0] < VOLUME_CACHE_SECONDS:
            return hit[1]
    since = now - timedelta(days=VOLUME_DAYS)
    out: dict[str, float] = {}
    for o in store.list_orders(status="ordered"):
        if _dt(store.ordered_at(o)) < since:
            continue
        for l in o.get("lines") or []:
            if l.get("product_id") == pid and l.get("quantity"):
                out[o.get("buyer") or "?"] = out.get(o.get("buyer") or "?", 0.0) + float(l["quantity"])
    with _vol_lock:
        _vol_cache[k] = (time.monotonic(), out)
    return out


def shares(pool: float, demand: dict[str, float], weights: dict[str, float]) -> dict[str, float]:
    """Weighted water filling: each buyer's cap of `pool`. A buyer needing less than their weighted share gets
    what they need and the rest is shared among the others, again by weight. Unrounded."""
    caps: dict[str, float] = {}
    left, todo = pool, {b: d for b, d in demand.items() if d > 0}
    while todo:
        total_w = sum(weights[b] for b in todo)
        fair = {b: left * weights[b] / total_w for b in todo}
        small = {b for b in todo if todo[b] <= fair[b] + EPS}
        if not small:
            caps.update(fair)
            break
        for b in small:
            caps[b] = todo.pop(b)
            left -= caps[b]
    return caps


def _cap(buyer: str | None, pid: str, day: str, pool: float, asking: float, active: list[dict],
         now: datetime) -> float | None:
    """pro_rata: the most this buyer may hold in total for (pid, day), or None when there is no cap (first_come,
    or demand fits the stock)."""
    if mode() != "pro_rata" or not buyer:
        return None
    demand = _demand(pid, day, now)
    held: dict[str, float] = {}
    for x in active:
        held[x["buyer"] or "?"] = held.get(x["buyer"] or "?", 0.0) + x["quantity"]
    for b, q in held.items():
        demand[b] = max(demand.get(b, 0.0), q)
    demand[buyer] = max(demand.get(buyer, 0.0), held.get(buyer, 0.0) + asking)
    if sum(demand.values()) <= pool + EPS:
        return None
    vol = volumes(_pid(pid), now)
    known = [v for b, v in vol.items() if v > 0 and b in demand]
    floor_w = min(known) if known else 1.0
    weights = {b: vol.get(b) or floor_w for b in demand}
    return _floor(shares(pool, demand, weights).get(buyer, 0.0), pid)


# --- what's left ------------------------------------------------------------------------------------

def _left(buyer: str | None, pid: str, day: str, amount: float, asking: float, active: list[dict],
          now: datetime) -> tuple[float, str]:
    """(how much this buyer can still get, why it is limited: 'allocated' | 'share')."""
    reserved = sum(x["quantity"] for x in active)
    left = max(0.0, amount - reserved)
    cap = _cap(buyer, pid, day, amount, asking, active, now)
    if cap is not None:
        mine = sum(x["quantity"] for x in active if x["buyer"] == buyer)
        if cap - mine < left - EPS:
            return max(0.0, _floor(cap - mine, pid)), "share"
    return _floor(left, pid), "allocated"


def available(pid: str, day: str, amount: float, buyer: str | None, now: datetime,
              asking: float = 0.0) -> tuple[float, str]:
    """What a quote for this buyer may still be sized to, from Valio's `amount` (just observed)."""
    return _left(buyer, pid, day, amount, asking, _active(pid, day, amount, now), now)


def _active(pid: str, day: str, amount: float | None, now: datetime) -> list[dict]:
    """Active reservations: from Valkey when configured (every deployment's), else the database."""
    r = shared._redis()
    if r is not None:
        try:
            raw = r.hgetall(_key(pid, day))
            shared._ok()
            active, _ = _settle([json.loads(v) for v in raw.values()], _valio(pid, amount), now)
            return active
        except Exception as e:  # noqa: BLE001
            shared._failed(e)
    return _settle_db(pid, day, amount, now)


def stock_lines(lines: list[dict]) -> list[tuple[int, dict, str]]:
    """(index, line, delivery date) of every orderable line with known Valio stock."""
    out = []
    for i, l in enumerate(lines):
        day = l.get("delivery_date")
        if (l.get("orderable", True) and l.get("logistic_model") == "Stock" and l.get("quantity") and day
                and last_amount(l["product_id"], day) is not None):
            out.append((i, l, day))
    return out


def check(lines: list[dict], buyer: str | None, now: datetime, scope: str = "") -> list[dict]:
    """Shortfalls if these lines were ordered now in pool `scope`: [{line, product_id, delivery_date, asked,
    left, why}]."""
    out = []
    asked: dict[tuple[str, str], float] = {}
    for i, l, day in stock_lines(lines):
        pid = l["product_id"]
        pk = _pk(pid, scope)
        prior = asked.get((pid, day), 0.0)  # two lines of the same product in one order count together
        asked[(pid, day)] = prior + float(l["quantity"])
        note_demand(buyer, pk, day, asked[(pid, day)], now)
        left, why = available(pk, day, last_amount(pid, day), buyer, now, asked[(pid, day)])
        if asked[(pid, day)] > left + EPS:
            out.append({"line": i, "product_id": pid, "name": l.get("name"), "sales_unit": l.get("sales_unit"),
                        "delivery_date": day, "asked": float(l["quantity"]),
                        "left": max(0.0, _floor(left - prior, pk)), "why": why})
    return out


def reserve(order_id: str, lines: list[dict], buyer: str | None, now: datetime, until: datetime | None = None,
            scope: str = "") -> list[dict]:
    """Reserve every stock line of an order that goes through (until=None: firm, TTL) or is held (until =
    approve_by: tentative). All or nothing: returns the shortfalls and reserves nothing if any line no longer
    fits (another deployment took it since check())."""
    todo = stock_lines(lines)
    if not todo:
        return []
    kind = "tentative" if until is not None else "firm"
    expires = until if until is not None else now + ttl()
    made: list[tuple[str, str, dict]] = []
    for i, l, day in todo:
        pid = l["product_id"]
        amount = last_amount(pid, day)
        x = {"id": "R-" + secrets.token_hex(5).upper(), "order_id": order_id, "product_id": _pk(pid, scope),
             "delivery_date": day, "buyer": buyer, "quantity": float(l["quantity"]), "base": float(amount),
             "kind": kind, "status": "active", "created_at": _iso(now), "expires_at": _iso(expires),
             "updated_at": _iso(now)}
        short = _put(x, amount, now)
        if short is not None:
            for p, d, y in made:
                _drop(y, "released", now)
            return [{"line": i, "product_id": pid, "name": l.get("name"), "sales_unit": l.get("sales_unit"),
                     "delivery_date": day, "asked": x["quantity"], "left": short[0], "why": short[1]}]
        made.append((pid, day, x))
    return []


def _put(x: dict, amount: float, now: datetime) -> tuple[float, str] | None:
    """Store one reservation if it fits; else (left, why). Valkey: atomic check-and-set across deployments."""
    pid, day, buyer = x["product_id"], x["delivery_date"], x["buyer"]
    if _override and pid.startswith(_override + "/"):
        _scope_keys.add((pid, day))
    r = shared._redis()
    if r is not None:
        out: dict = {}

        def tx(pipe):
            raw = pipe.hgetall(_key(pid, day))
            active, ended = _settle([json.loads(v) for v in raw.values()], _valio(pid, amount), now)
            left, why = _left(buyer, pid, day, amount, x["quantity"], active, now)
            pipe.multi()
            if ended:
                pipe.hdel(_key(pid, day), *ended)
            for y in active:
                if y.pop("_rebased", None):
                    pipe.hset(_key(pid, day), y["id"], json.dumps(y))
            if x["quantity"] > left + EPS:
                out["short"] = (left, why)
                return
            pipe.hset(_key(pid, day), x["id"], json.dumps(x))
            pipe.expire(_key(pid, day), _hash_ttl(pid))

        try:
            r.transaction(tx, _key(pid, day))
            shared._ok()
            if "short" in out:
                return out["short"]
            _ensure()
            _settle_db(pid, day, amount, now)
            store._upsert("stock_reservation", COLS, tuple(x[c] for c in COLS))
            return None
        except Exception as e:  # noqa: BLE001 - Valkey down: the database (this deployment) decides
            shared._failed(e)
    _ensure()
    active = _settle_db(pid, day, amount, now)
    left, why = _left(buyer, pid, day, amount, x["quantity"], active, now)
    if x["quantity"] > left + EPS:
        return left, why
    store._upsert("stock_reservation", COLS, tuple(x[c] for c in COLS))
    return None


def _drop(x: dict, status: str, now: datetime) -> None:
    _ensure()
    store._exec("UPDATE stock_reservation SET status = ?, updated_at = ? WHERE id = ?", (status, _iso(now), x["id"]))
    r = shared._redis()
    if r is not None:
        try:
            r.hdel(_key(x["product_id"], x["delivery_date"]), x["id"])
            shared._ok()
        except Exception as e:  # noqa: BLE001
            shared._failed(e)


def for_order(order_id: str) -> list[dict]:
    _ensure()
    return store._exec("SELECT * FROM stock_reservation WHERE order_id = ? AND status = ?", (order_id, "active"))[0]


def release(order_id: str, now: datetime, status: str = "released") -> int:
    """End an order's reservations (rejected, expired, refused at approval)."""
    rows = for_order(order_id)
    for x in rows:
        _drop(x, status, now)
    return len(rows)


def confirm(order: dict, now: datetime, scope: str = "") -> list[dict]:
    """An approved held order: its tentative reservations become firm (TTL from now). An order held before
    reservations existed reserves now; returns the shortfalls if the stock was allocated meanwhile."""
    rows = for_order(order["id"])
    if not rows:
        return reserve(order["id"], order.get("lines") or [], order.get("buyer"), now, scope=scope)
    exp = _iso(now + ttl())
    for x in rows:
        store._exec("UPDATE stock_reservation SET kind = ?, expires_at = ?, updated_at = ? WHERE id = ?",
                    ("firm", exp, _iso(now), x["id"]))
        r = shared._redis()
        if r is not None:
            try:
                r.hset(_key(x["product_id"], x["delivery_date"]), x["id"],
                       json.dumps({**x, "kind": "firm", "expires_at": exp}))
                shared._ok()
            except Exception as e:  # noqa: BLE001
                shared._failed(e)
    return []


def total_reserved(pid: str, day: str, now: datetime, scope: str = "") -> float:
    return sum(x["quantity"] for x in _active(_pk(pid, scope), day, None, now))


# --- what the gateway says ------------------------------------------------------------------------

def message(s: dict) -> str:
    unit = s.get("sales_unit") or ""
    left = f"{s['left']:g} {unit}".strip()
    if s["why"] == "share":
        return (f"Stock for {s['delivery_date']} is short and Valio shares it in proportion to each buyer's last "
                f"4 weeks of orders (SOUS_ALLOCATION=pro_rata): your share leaves {left}"
                + (", see the order's options." if s["left"] > 0 else "."))
    if s["left"] > 0:
        return (f"Valio's stock for {s['delivery_date']} is already partly allocated to orders placed through "
                f"Sous: only {left} can still be ordered, see the order's options.")
    return (f"Valio's stock for {s['delivery_date']} is fully allocated to orders already placed through Sous; "
            "nothing is left for this date.")


def issue(s: dict) -> dict:
    return {"code": "ALLOCATED", "severity": "blocking", "message": message(s)}


def options(order_id: str, shortfalls: list[dict]) -> list[dict]:
    """What a refused order can still get: a ready quote request for what's left of each short line."""
    return [{"id": f"{order_id}.A{n}", "kind": "allocated_left", "product_id": s["product_id"],
             "quantity": s["left"], "sales_unit": s.get("sales_unit"), "delivery_date": s["delivery_date"],
             "requote": {"delivery_date": s["delivery_date"],
                         "items": [{"product_ids": [s["product_id"]], "amount": s["left"], "unit": "SALES_UNIT"}]}}
            for n, s in enumerate((s for s in shortfalls if s["left"] > 0), 1)]


def fetched_at(pid: str, requested, amount: float, now: datetime) -> datetime | None:
    """When Valio actually returned the availability reading behind an offer (business time), from aimo's cache:
    a cache hit (L1, or a copy from the shared Valkey cache) keeps its original fetch time, so it is never taken
    for a reading fetched at quote time. None when unknown (replay, no cached copy) or when the cached copy no
    longer shows the offer's amount (refreshed meanwhile)."""
    if requested is None:
        return None
    from . import aimo
    hit = aimo.peek("availability", requested, pid)
    if not hit:
        return None
    t, item = hit
    lines = item.get("scheduleLines") or []
    if not lines or lines[0].get("availableAmount") is None or abs(float(lines[0]["availableAmount"]) - amount) > EPS:
        return None
    return now - timedelta(seconds=max(0.0, time.time() - t))  # the reading's age, on the business clock


def adjust_offers(offers: list[dict], buyer: str | None, now: datetime, scope: str = "", requested=None) -> None:
    """Quote hook: record Valio's amount and size each offer to what is left for this buyer in pool `scope`.
    `requested`: the date the offers were fetched for (aimo's cache key), to find each reading's fetch time."""
    for o in offers:
        day, amount = o.get("delivery_date"), o.get("available_amount")
        if o.get("logistic_model") != "Stock" or amount is None or not day:
            continue
        if not any(i["code"] == "DATA_STALE" for i in o.get("issues") or []):
            observe(o["product_id"], day, amount, now, fetched_at(o["product_id"], requested, amount, now))
        pk = _pk(o["product_id"], scope)
        note_demand(buyer, pk, day, o.get("quantity"), now)
        left, why = available(pk, day, amount, buyer, now)
        if left >= amount - EPS:
            continue
        from . import exposure
        o["available_amount"] = left
        o["stock_level"] = exposure.level("Stock", left, o.get("quantity"), True)
        o["issues"] = [i for i in o["issues"] if i["code"] != "PARTIAL_STOCK"]
        s = {"product_id": o["product_id"], "sales_unit": o.get("sales_unit"), "delivery_date": day, "left": left,
             "why": why}
        if left <= EPS and o.get("orderable"):
            o["issues"].append(issue(s))
            o["orderable"] = False
        elif o.get("quantity") and left < o["quantity"]:
            o["issues"].append({"code": "PARTIAL_STOCK", "severity": "warning",
                                "message": f"Only {left:g} {o.get('sales_unit')} in stock; {o['quantity']:g} needed."})


def summary(pid: str, day: str, now: datetime, scope: str = "") -> dict:
    amount = last_amount(pid, day)
    active = _active(_pk(pid, scope), day, amount, now)
    return {"product_id": pid, "delivery_date": day, "pool": scope or "global", "valio_amount": amount,
            "policy": mode(),
            "reserved": round(sum(x["quantity"] for x in active), 3),
            "tentative": round(sum(x["quantity"] for x in active if x["kind"] == "tentative"), 3),
            "by_buyer": {b: round(sum(x["quantity"] for x in active if x["buyer"] == b), 3)
                         for b in sorted({x["buyer"] or "?" for x in active})}}
