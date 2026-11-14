"""Monday rush: many buyers' agents order the same short product at once; stock is never over-allocated.

Offline and self-contained: a fake Valio Aimo with a fixed stock of one product, a fresh state database, and
the real gateway (FastAPI app through TestClient, sous/service.py, sous/reservations.py). N agents, each for a
different buyer with its own mandate (like the judge sandboxes), start together on a barrier: quote, think for
a moment (an LLM agent takes far longer), order; if the order is refused with availability.allocated and its
options offer what's left, they re-quote that and order once more. Buyers have 4 weeks of order history of
different sizes, which only the pro_rata policy uses.

    uv run python -m scripts.rush                  # 60 agents, both policies
    uv run python -m scripts.rush --agents 20 --policy pro_rata --stock 150

Prints, per policy: allocated vs Valio's stock (never over), what every buyer asked and got, refusals by rule,
p50/p95 latency of quote and order calls. tests/test_rush.py runs a 20-agent version and asserts no over-allocation.
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import sys
import tempfile
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

PID = "RUSH-BUTTER"
PRICE = 3.2


def _product(pid: str) -> dict:
    return {
        "id": pid, "name": "Valio butter 500 g", "names": {"fi": "Valio voi 500 g", "en": "", "sv": ""},
        "brand": "Valio", "vendor": "", "category": "10400", "country_of_origin": "fi", "sales_unit": "ST",
        "base_unit": "ST", "units": {"ST": 1.0}, "allowed_lot_size": 1.0, "temperature": "chilled", "labels": [],
        "claims": [], "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": []},
        "size_from_name": (0.5, "KG"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
        "official_substitutes": [], "deleted": False,
    }


class FakeValio:
    """Valio Aimo's price and availability for one product: a fixed stock that never changes during the rush
    (Valio's own refresh would only lower it once orders arrive there; offline, reservations alone hold it)."""

    def __init__(self, stock: float, day: date):
        self.stock, self.day = stock, day
        self.calls = Counter()
        self._lock = threading.Lock()

    def prices(self, ids, requested):
        with self._lock:
            self.calls["price"] += 1
        return {i: {"productId": i, "catalogPrice": {"value": PRICE, "valueWithTax": round(PRICE * 1.135, 2),
                                                     "validTo": None},
                    "comparisonPrice": {"value": PRICE * 2}, "comparisonUnit": "KG", "tax": {"taxRate": 13.5}}
                for i in ids}

    def availability(self, ids, requested):
        with self._lock:
            self.calls["availability"] += 1
        close = datetime.combine(self.day - timedelta(days=1), datetime.min.time()).replace(hour=17)
        return {i: {"productId": i, "logisticModel": "Stock", "salesBlock": False,
                    "closingTime": close.strftime("%Y-%m-%dT%H:%M:%S") + "+0200",
                    "scheduleLines": [{"salesBlock": False, "availableAmount": self.stock,
                                       "availableDate": self.day.isoformat(), "salesUnit": "ST"}]}
                for i in ids}


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, round(p / 100 * len(xs) + 0.5) - 1))]


def run(agents: int = 60, stock: float = 400, policy: str = "first_come", seed: int = 7,
        think_ms: tuple[int, int] = (20, 200), held_every: int = 10, data_dir: str | None = None) -> dict:
    """One rush in a fresh state database; returns the numbers (the module's own state is restored after)."""
    from fastapi.testclient import TestClient

    from sous import aimo, api, catalog, config, reservations, service as S, shared, signing, store
    from sous.mandate import Mandate

    rnd = random.Random(seed)
    tmp = Path(tempfile.mkdtemp(prefix="sous-rush-", dir=data_dir))
    day = date.today() + timedelta(days=3)
    valio = FakeValio(stock, day)
    saved = {(m, k): getattr(m, k) for m, k in [
        (config, "STATE_DB"), (config, "DB_URL"), (config, "REQUIRE_AGENT_AUTH"), (store, "_conn"),
        (signing, "KEY_PATH"), (catalog, "get"), (aimo, "prices"), (aimo, "availability"),
        (S.notify, "send"), (S.substitutes, "suggest"), (reservations, "_ready_for")]}
    env_saved = {k: os.environ.get(k) for k in ("SOUS_ALLOCATION", "SOUS_ALLERGEN_AI")}
    shared_url = shared.URL
    try:
        os.environ["SOUS_ALLOCATION"], os.environ["SOUS_ALLERGEN_AI"] = policy, "0"
        config.STATE_DB, config.DB_URL, config.REQUIRE_AGENT_AUTH = tmp / "state.db", "", False
        store._conn = None
        reservations._ready_for = None
        reservations._vol_cache.clear()
        signing.KEY_PATH = tmp / "key.pem"
        signing._key.cache_clear()
        catalog.get = lambda pid: _product(pid) if pid == PID else None
        aimo.prices, aimo.availability = valio.prices, valio.availability
        S.notify.send = lambda *a, **k: None
        S.substitutes.suggest = lambda *a, **k: {"options": []}  # one product in this catalog: no substitutes
        api._hits.clear()
        shared.URL = ""  # offline: no Valkey (tests/test_reservations.py covers the Valkey mirror with fakeredis)

        buyers = []
        now = datetime.now(config.TZ)
        for n in range(1, agents + 1):
            b = {"buyer": f"Kitchen {n:02d}", "mandate": f"M-RUSH-{n:02d}", "agent": f"agent-{n:02d}",
                 "need": rnd.choice([6, 8, 10, 12, 16, 20, 24, 30, 40]),
                 "history": rnd.choice([0, 10, 20, 40, 60, 80, 120, 200]),
                 "held": held_every and n % held_every == 0}
            S.issue_mandate(Mandate(id=b["mandate"], buyer=b["buyer"], agent_id=b["agent"], issued_by="chef",
                                    auto_approve_limit_eur=5.0 if b["held"] else 1000.0, hard_limit_eur=5000,
                                    notify_above_eur=None, min_delivery_reliability=None,
                                    unusual_quantity_factor=None))
            if b["history"]:  # last 4 weeks of this product, under an older mandate (pro_rata weights)
                for w in range(4):
                    at = (now - timedelta(days=7 * w + 3)).isoformat(timespec="milliseconds")
                    store.insert_order({
                        "id": f"PO-H{n:02d}{w}", "quote_id": f"Q-H{n:02d}{w}", "mandate_id": f"M-HIST-{n:02d}",
                        "agent_id": b["agent"], "buyer": b["buyer"], "status": "ordered",
                        "total_ex_vat": round(PRICE * b["history"] / 4, 2), "created_at": at, "updated_at": at,
                        "lines": [{"product_id": PID, "quantity": b["history"] / 4, "sales_unit": "ST"}]})
            buyers.append(b)

        # one client per agent, each from its own address: the per-IP write limit is per buyer, as in real life
        clients = {b["agent"]: TestClient(api.app, raise_server_exceptions=False, client=(f"10.0.{n // 250}.{n % 250 + 1}", 50000))
                   for n, b in enumerate(buyers)}
        lat: dict[str, list[float]] = {"quote": [], "order": [], "agent": []}
        lat_lock = threading.Lock()
        start = threading.Barrier(agents)

        def call(kind: str, path: str, body: dict) -> dict:
            t = time.perf_counter()
            r = clients[body["agent_id"]].post(path, json=body)
            with lat_lock:
                lat[kind].append((time.perf_counter() - t) * 1000)
            return {"status": r.status_code, **(r.json() if r.headers.get("content-type", "").startswith(
                "application/json") else {})}

        def agent(b: dict) -> None:
            r = random.Random(seed * 1000 + int(b["agent"][-2:]))
            b.update(got=0.0, steps=[], rules=[], status="none")
            start.wait()
            t0 = time.perf_counter()
            try:
                _agent(b, r)
            finally:
                with lat_lock:
                    lat["agent"].append((time.perf_counter() - t0) * 1000)

        def _agent(b: dict, r: random.Random) -> None:
            request = {"delivery_date": day.isoformat(),
                       "items": [{"product_ids": [PID], "amount": b["need"], "unit": "SALES_UNIT"}]}
            for attempt in range(2):
                q = call("quote", "/quotes", {"agent_id": b["agent"], "mandate_id": b["mandate"], **request})
                lines = [l for l in (q.get("quote") or {}).get("lines", []) if l.get("orderable")]
                if not lines:
                    b["steps"].append("no stock in quote")
                    break
                time.sleep(r.uniform(*think_ms) / 1000)
                o = call("order", "/orders", {"agent_id": b["agent"], "mandate_id": b["mandate"],
                                              "quote_id": q["quote"]["id"],
                                              "idempotency_key": f"{b['agent']}-{attempt}"})
                st = o.get("status")
                b["steps"].append(st if isinstance(st, str) else f"http {o['status']}")
                b["rules"] += [f["rule"] for f in (o.get("decision") or {}).get("findings", [])
                               if f["outcome"] == "block"]
                if st in ("ordered", "pending_approval"):
                    b["got"] += sum(l["quantity"] for l in o["lines"])
                    b["status"] = st
                    break
                opts = [x for x in o.get("options") or [] if x["kind"] == "allocated_left"]
                if not opts:
                    break
                request = opts[0]["requote"]

        threads = [threading.Thread(target=agent, args=(b,)) for b in buyers]
        t_all = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.perf_counter() - t_all

        orders = [o for o in store.list_orders() if not o["id"].startswith("PO-H")]
        placed = sum(l["quantity"] for o in orders if o["status"] in ("ordered", "pending_approval")
                     for l in o["lines"] if l["product_id"] == PID)
        summ = reservations.summary(PID, day.isoformat(), datetime.now(config.TZ))
        refusals = Counter(rule for b in buyers for rule in b["rules"])
        return {
            "policy": policy, "agents": agents, "stock": stock, "delivery_date": day.isoformat(),
            "demand": sum(b["need"] for b in buyers), "allocated": summ["reserved"], "tentative": summ["tentative"],
            "placed": placed, "over_allocated": max(0.0, placed - stock),
            "ordered": sum(1 for b in buyers if b["status"] == "ordered"),
            "held": sum(1 for b in buyers if b["status"] == "pending_approval"),
            "partial": sum(1 for b in buyers if 0 < b["got"] < b["need"]),
            "nothing": sum(1 for b in buyers if not b["got"]),
            "retried": sum(1 for b in buyers if len(b["steps"]) > 1),
            "refusals": dict(refusals), "orders": len(orders), "valio_calls": dict(valio.calls),
            "latency_ms": {k: {"p50": round(_pct(v, 50), 1), "p95": round(_pct(v, 95), 1), "n": len(v)}
                           for k, v in lat.items()},
            "wall_s": round(wall, 2),
            "buyers": [{k: b.get(k) for k in ("buyer", "need", "history", "got", "status", "steps", "held")}
                       for b in buyers],
        }
    finally:
        if store._conn is not None:
            store._conn.close()
        for (m, k), v in saved.items():
            setattr(m, k, v)
        signing._key.cache_clear()
        reservations._ready_for = None
        reservations._vol_cache.clear()
        shared.URL = shared_url
        for k, v in env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def report(r: dict, per_buyer: bool = True) -> str:
    out = [f"## policy={r['policy']}: {r['agents']} agents, stock {r['stock']:g} ST of {PID} for {r['delivery_date']}, "
           f"demand {r['demand']:g} ST",
           f"allocated {r['allocated']:g} / {r['stock']:g} ST (orders placed or held: {r['placed']:g}; "
           f"tentative for held orders: {r['tentative']:g}); over-allocated: {r['over_allocated']:g}",
           f"buyers: {r['ordered']} ordered, {r['held']} held for a human, {r['partial']} got part of their need, "
           f"{r['nothing']} got nothing; {r['retried']} re-quoted the options' remainder",
           f"blocking rules on orders: {r['refusals'] or 'none'}",
           "latency ms: " + "; ".join(f"{k} p50 {v['p50']} p95 {v['p95']} (n={v['n']})"
                                      for k, v in r["latency_ms"].items())
           + f"; wall {r['wall_s']} s; fake Valio calls {r['valio_calls']}"]
    if per_buyer:
        out.append("buyer       4w-vol  need   got  status                       steps")
        for b in r["buyers"]:
            out.append(f"{b['buyer']:<11} {b['history']:>6} {b['need']:>5} {b['got']:>5g}  "
                       f"{b['status'] + (' (5 EUR limit)' if b['held'] else ''):<28} {' -> '.join(b['steps'])}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--agents", type=int, default=60)
    ap.add_argument("--stock", type=float, default=400)
    ap.add_argument("--policy", choices=["first_come", "pro_rata", "both"], default="both")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--quiet", action="store_true", help="no per-buyer table")
    ap.add_argument("--data-dir", default="/dev/shm" if Path("/dev/shm").is_dir() else None,
                    help="where the throwaway SQLite state goes (default tmpfs, so the host disk's fsync per commit "
                         "doesn't dominate the latency; pass /tmp to include it)")
    a = ap.parse_args()
    bad = 0
    for p in (["first_come", "pro_rata"] if a.policy == "both" else [a.policy]):
        r = run(a.agents, a.stock, p, a.seed, data_dir=a.data_dir)
        print(f"(state database in {a.data_dir or tempfile.gettempdir()})")
        print(report(r, not a.quiet), "\n")
        bad += r["over_allocated"] > 0 or r["allocated"] > r["stock"]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
