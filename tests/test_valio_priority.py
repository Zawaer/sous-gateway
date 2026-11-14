"""Judges first: the interactive slice of the Valio budget and the cached answer for a sandbox over our own budget.

Offline: a fake clock (aimo.time / shared.time), fakeredis as Valkey, Valio as an httpx.MockTransport.
"""

import json
from datetime import date, datetime

import fakeredis
import httpx
import pytest

from sous import aimo, catalog, config, quote as Q, shared
from sous.mandate import Mandate, evaluate

DAY = date(2026, 11, 16)
NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


def ok(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if request.url.path.endswith("/price"):
        items = [{"productId": p, "catalogPrice": {"value": 1.4, "valueWithTax": 1.59}, "comparisonPrice": {"value": 1.4},
                  "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False} for p in body["products"]]
    else:
        items = [{"productId": p, "logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200",
                  "salesBlock": False, "scheduleLines": [{"salesBlock": False, "availableAmount": 5000.0,
                                                          "availableDate": "2026-11-16", "salesUnit": "ST"}]}
                 for p in body["products"]]
    return httpx.Response(200, json={"timeNow": "2026-11-13T21:00:00+0200", "products": items})


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(aimo, "time", c)
    monkeypatch.setattr(shared, "time", c)
    monkeypatch.setattr(aimo, "_sleep", c.sleep)
    return c


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    aimo.reset()
    monkeypatch.setattr(aimo, "_client", httpx.Client(base_url="https://aimo.test/api", transport=httpx.MockTransport(ok)))
    monkeypatch.setattr(aimo, "_DEFAULT_TRAFFIC", "")
    yield
    aimo.reset()
    shared.configure(url="", role="dev", limit=60, client=None, interactive_share=0.3)


def valkey(server, role="prod", limit=10):
    aimo.reset()
    shared.configure(url="rediss://default:secret@valkey.test:6379", role=role, limit=limit, interactive_share=0.3,
                     client=fakeredis.FakeRedis(server=server, decode_responses=True))


def sandbox(allowed=True):
    m = aimo.Meter("SBX-TEST", allowed=allowed)
    return m, aimo.meter.set(m)


def test_priority_classes(monkeypatch):
    assert aimo.priority() == "normal"
    with aimo.background():
        assert aimo.priority() == "background"
        _, tok = sandbox()
        assert aimo.priority() == "interactive"  # a judge's sandbox lookup always counts as interactive
        aimo.meter.reset(tok)
    monkeypatch.setattr(shared, "ROLE", "ci")
    assert aimo.priority() == "background"  # evals in CI never take the judges' slice


def test_shared_budget_keeps_a_slice_background_work_cannot_use(clock):
    valkey(fakeredis.FakeServer(), "prod", limit=10)
    with aimo.background():
        for i in range(7):
            aimo.prices([f"b{i}"], DAY)
        with pytest.raises(aimo.SharedBudgetLimit) as e:
            aimo.prices(["b9"], DAY)
    assert "background" in str(e.value) and e.value.retry_after >= 1
    _, tok = sandbox()
    try:
        for i in range(3):  # the interactive slice is still there for a judge
            aimo.prices([f"j{i}"], DAY)
    finally:
        aimo.meter.reset(tok)
    assert shared.health(0, 60)["background_limit"] == 7


def test_interactive_traffic_may_pass_a_non_prod_roles_cap(clock):
    server = fakeredis.FakeServer()
    valkey(server, "staging", limit=10)  # staging's own cap: 5 of 10
    for i in range(5):
        aimo.prices([f"s{i}"], DAY)
    with pytest.raises(aimo.SharedBudgetLimit):
        aimo.prices(["s9"], DAY)
    _, tok = sandbox()
    try:
        aimo.prices(["judge"], DAY)  # a judge's sandbox quote on staging still gets through
    finally:
        aimo.meter.reset(tok)


def test_local_budget_keeps_the_slice_too(clock, monkeypatch):
    monkeypatch.setattr(config, "AIMO_CALLS_PER_MIN", 10)
    with aimo.background():
        for i in range(7):
            aimo.prices([f"b{i}"], DAY)
        with pytest.raises(aimo.BudgetLimit) as e:
            aimo.prices(["b9"], DAY)
    assert "budget" in str(e.value)  # scripts/eval.py retries on this word
    for i in range(3):
        aimo.prices([f"n{i}"], DAY)  # normal traffic uses the rest
    clock.t += 61
    with aimo.background():
        aimo.prices(["later"], DAY)


def _fill_local_budget(monkeypatch, n=2):
    monkeypatch.setattr(config, "AIMO_CALLS_PER_MIN", n)
    for i in range(n):
        aimo.prices([f"fill{i}"], DAY)


PRODUCT = {"id": "1", "name": "Milk 1 l", "brand": "", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
           "allowed_lot_size": 0.01, "size_from_name": (1.0, "L"), "delivery_reliability": 0.99,
           "co2_kg_per_kg_group_avg": None, "labels": [], "claims": [], "allergens": None, "temperature": None}


def test_sandbox_over_our_own_budget_gets_a_recent_copy_marked_cached(clock, monkeypatch):
    monkeypatch.setattr(catalog, "get", lambda pid: {})
    aimo.prices(["1"], DAY)
    aimo.availability(["1"], DAY)
    clock.t += 300  # availability TTL (60 s) long gone; within the 10 min cache horizon
    _fill_local_budget(monkeypatch)
    _, tok = sandbox()
    try:
        price = aimo.prices(["1"], DAY)["1"]
        av = aimo.availability(["1"], DAY)["1"]
    finally:
        aimo.meter.reset(tok)
    assert "_cached_seconds" not in price  # price still fresh (10 min TTL)
    assert av["_cached_seconds"] == 300 and av["_cached_at"] and "_stale_seconds" not in av
    assert aimo.health()["cached_items_served"] == 1

    line = Q.build_offer(PRODUCT, price, av, DAY, 10, "L", NOW)
    cached = [i for i in line["issues"] if i["code"] == "DATA_CACHED"]
    assert line["orderable"] and cached and cached[0]["severity"] == "info"
    assert "5 min old" in cached[0]["message"] and line["cached_at"] == av["_cached_at"]
    assert not any(i["code"] == "DATA_STALE" for i in line["issues"])
    m = Mandate(id="M", buyer="B", agent_id="a", issued_by="h", min_delivery_reliability=None)
    outcome, findings = evaluate(m, "a", [line], requested_date=DAY, now=NOW)
    assert outcome == "silent", findings  # information, not a reason to hold the order


def test_no_cached_copy_for_old_data_or_for_non_sandbox_traffic(clock, monkeypatch):
    aimo.availability(["1"], DAY)
    clock.t += 300
    _fill_local_budget(monkeypatch)
    with pytest.raises(aimo.BudgetLimit):  # not a sandbox: the retryable refusal stands
        aimo.availability(["1"], DAY)
    clock.t += aimo.CACHED_MAX  # now 15 min old; and the minute rolled over, so fill the budget again
    _fill_local_budget(monkeypatch)
    _, tok = sandbox()
    try:
        with pytest.raises(aimo.BudgetLimit):  # too old: never a quote on unknown data
            aimo.availability(["1"], DAY)
        with pytest.raises(aimo.BudgetLimit):  # never fetched at all
            aimo.availability(["unknown"], DAY)
    finally:
        aimo.meter.reset(tok)


def test_sandbox_quota_used_up_also_gets_the_cached_copy(clock):
    aimo.availability(["1"], DAY)
    clock.t += 120
    _, tok = sandbox(allowed=False)  # its 15 lookups are used up
    try:
        assert aimo.availability(["1"], DAY)["1"]["_cached_seconds"] == 120
    finally:
        aimo.meter.reset(tok)


def test_valio_429_still_means_stale_not_cached(clock, monkeypatch):
    aimo.availability(["1"], DAY)
    clock.t += 120
    monkeypatch.setattr(aimo, "_client", httpx.Client(base_url="https://aimo.test/api", transport=httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"Retry-After": "60"}))))
    _, tok = sandbox()
    try:
        av = aimo.availability(["1"], DAY)["1"]
    finally:
        aimo.meter.reset(tok)
    assert av["_stale_seconds"] == 120 and "_cached_seconds" not in av  # DATA_STALE -> ask, as before


def test_cached_copy_from_another_deployment_through_valkey(clock):
    server = fakeredis.FakeServer()
    valkey(server, "prod", limit=10)
    aimo.availability(["1"], DAY)  # prod fetched it
    clock.t += 200
    valkey(server, "staging", limit=10)  # staging: empty L1, its shared budget all used this minute
    for i in range(10):
        shared.take("interactive")
    _, tok = sandbox()
    try:
        av = aimo.availability(["1"], DAY)["1"]
    finally:
        aimo.meter.reset(tok)
    assert av["_cached_seconds"] == 200
