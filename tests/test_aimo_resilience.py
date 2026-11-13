"""Valio Aimo client resilience: Retry-After, circuit breaker, stale-while-error, coalescing.

Offline: every HTTP call goes to an httpx.MockTransport.
"""

import threading
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from valtakirja import aimo, catalog, config, quote as Q, service as S
from valtakirja.mandate import Mandate, evaluate

DAY = date(2026, 11, 16)
NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)


def price_item(pid):
    return {"productId": pid, "catalogPrice": {"value": 1.4, "valueWithTax": 1.59, "validTo": "2026-12-31T00:00:00+0200"},
            "comparisonPrice": {"value": 1.4}, "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}


def avail_item(pid):
    return {"productId": pid, "logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": False,
            "scheduleLines": [{"salesBlock": False, "availableAmount": 5000.0, "availableDate": "2026-11-16",
                               "salesUnit": "ST"}]}


def ok(request: httpx.Request) -> httpx.Response:
    import json
    body = json.loads(request.content)
    make = price_item if request.url.path.endswith("/price") else avail_item
    return httpx.Response(200, json={"timeNow": "2026-11-13T21:00:00+0200",
                                     "products": [make(p) for p in body["products"]]})


class Api:
    """Scripted Valio Aimo: pops one response (or callable) per request; then answers 200."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        step = self.script.pop(0) if self.script else ok
        return step(request) if callable(step) else step


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    aimo.reset()
    sleeps: list[float] = []
    monkeypatch.setattr(aimo, "_sleep", sleeps.append)
    yield sleeps
    aimo.reset()


def use(monkeypatch, api: Api):
    monkeypatch.setattr(aimo, "_client", httpx.Client(base_url="https://aimo.test/api", transport=httpx.MockTransport(api)))


def age_cache(seconds):
    for k, (t, item) in list(aimo._cache.items()):
        aimo._cache[k] = (t - seconds, item)


# --- Retry-After and backoff -------------------------------------------------------------------

def test_429_with_retry_after_seconds_then_200(monkeypatch, fresh):
    api = Api(httpx.Response(429, headers={"Retry-After": "2"}, text="<html>Too many requests</html>"))
    use(monkeypatch, api)
    out = aimo.prices(["1", "2"], DAY)
    assert set(out) == {"1", "2"} and "_stale_seconds" not in out["1"]
    assert len(api.requests) == 2
    assert sum(fresh) >= 2  # waited out Retry-After before retrying
    h = aimo.health()
    assert h["rate_limited_responses"] == 1 and h["last_429_at"]


def test_retry_after_http_date():
    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    r = httpx.Response(429, headers={"Retry-After": format_datetime(when, usegmt=True)})
    assert 25 <= aimo._retry_after(r) <= 31
    assert aimo._retry_after(httpx.Response(429, headers={"Retry-After": "garbage"})) is None


def test_5xx_backs_off_then_succeeds(monkeypatch, fresh):
    api = Api(httpx.Response(502, text="bad gateway"), httpx.Response(503, text="busy"))
    use(monkeypatch, api)
    assert set(aimo.availability(["1"], DAY)) == {"1"}
    assert len(api.requests) == 3 and len(fresh) == 2 and sum(fresh) <= aimo.MAX_WAIT


def test_long_retry_after_fails_fast_and_opens_circuit(monkeypatch, fresh):
    api = Api(httpx.Response(429, headers={"Retry-After": "30"}))
    use(monkeypatch, api)
    with pytest.raises(aimo.AimoError) as e:
        aimo.prices(["1"], DAY)
    assert e.value.retry_after >= 30 and e.value.rate_limited
    assert fresh == []  # 30 s is over the per-request cap: no waiting
    # The circuit is open: the next request doesn't reach Valio Aimo at all.
    with pytest.raises(aimo.AimoError):
        aimo.prices(["2"], DAY)
    assert len(api.requests) == 1
    h = aimo.health()
    assert h["circuit"] == "open" and h["circuit_retry_after_s"] >= 29
    assert "key" not in str(h).lower()


def test_gateway_error_carries_retry_after(monkeypatch):
    use(monkeypatch, Api(httpx.Response(429, headers={"Retry-After": "20"})))
    monkeypatch.setattr(catalog, "get", lambda pid: {"id": pid})
    req = S.QuoteRequest(agent_id="a", delivery_date=DAY, items=[S.QuoteItem(product_ids=["1"], amount=1, unit="L")])
    with pytest.raises(S.GatewayError) as e:
        S.request_quote(req)
    assert (e.value.code, e.value.status) == ("valio_unavailable", 503) and e.value.retry_after >= 20


# --- stale-while-error -------------------------------------------------------------------------

def test_stale_copy_served_on_429_and_needs_a_human(monkeypatch):
    use(monkeypatch, Api())
    aimo.prices(["1"], DAY)
    aimo.availability(["1"], DAY)
    age_cache(130)  # availability TTL (60 s) passed; price still fresh
    use(monkeypatch, Api(httpx.Response(429, headers={"Retry-After": "60"})))
    price = aimo.prices(["1"], DAY)["1"]
    av = aimo.availability(["1"], DAY)["1"]
    assert "_stale_seconds" not in price
    assert 125 <= av["_stale_seconds"] <= 140
    assert aimo.health()["stale_items_served"] == 1

    product = {"id": "1", "name": "Milk 1 l", "brand": "", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
               "allowed_lot_size": 0.01, "size_from_name": (1.0, "L"), "delivery_reliability": 0.99,
               "co2_kg_per_kg_group_avg": None, "labels": [], "claims": [], "allergens": None, "temperature": None}
    line = Q.build_offer(product, price, av, DAY, 10, "L", NOW)
    stale = [i for i in line["issues"] if i["code"] == "DATA_STALE"]
    assert line["orderable"] and stale and stale[0]["severity"] == "warning"
    assert "availability" in stale[0]["message"] and "rate limit" in stale[0]["message"]
    assert "2 min ago" in stale[0]["message"]

    monkeypatch.setattr(catalog, "get", lambda pid: {})
    m = Mandate(id="M", buyer="B", agent_id="a", issued_by="h", min_delivery_reliability=None)
    outcome, findings = evaluate(m, "a", [line], requested_date=DAY, now=NOW)
    assert outcome == "ask"
    assert any(f.rule == "availability.data_stale" for f in findings)


def test_too_old_copy_is_not_served(monkeypatch):
    use(monkeypatch, Api())
    aimo.availability(["1"], DAY)
    age_cache(aimo.AVAILABILITY_STALE_MAX + 10)
    use(monkeypatch, Api(httpx.Response(429, headers={"Retry-After": "60"})))
    with pytest.raises(aimo.AimoError):
        aimo.availability(["1"], DAY)


def test_stale_on_network_error_and_during_open_circuit(monkeypatch):
    use(monkeypatch, Api())
    aimo.prices(["1", "2"], DAY)
    age_cache(aimo.PRICE_TTL + 5)

    def refuse(request):
        raise httpx.ConnectError("refused")

    use(monkeypatch, Api(refuse))
    out = aimo.prices(["1", "2", "3"], DAY)
    assert set(out) == {"1", "2"}  # "3" has no copy: left out, so its line shows NO_PRICE
    assert out["1"]["_stale_reason"] == "unavailable"

    aimo._open_circuit(60)
    api = Api()
    use(monkeypatch, api)
    assert out.keys() == aimo.prices(["1", "2"], DAY).keys()
    assert api.requests == []  # paused: served stale without calling


# --- coalescing --------------------------------------------------------------------------------

def test_concurrent_identical_requests_share_one_call(monkeypatch):
    release = threading.Event()

    def slow(request):
        release.wait(5)
        return ok(request)

    api = Api(slow)
    use(monkeypatch, api)
    results, errors = [], []

    def run(ids):
        try:
            results.append(aimo.availability(ids, DAY))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=run, args=(["1", "2", "3"],))]
    threads[0].start()
    deadline = time.time() + 5
    while not aimo._inflight and time.time() < deadline:
        time.sleep(0.01)
    threads += [threading.Thread(target=run, args=(ids,)) for ids in (["3", "1", "2"], ["2"], ["1", "2", "3"])]
    for t in threads[1:]:
        t.start()
    while aimo.health()["coalesced_requests"] < 3 and time.time() < deadline:
        time.sleep(0.01)
    release.set()
    for t in threads:
        t.join(5)
    assert not errors and len(results) == 4
    assert len(api.requests) == 1
    assert sorted(len(r) for r in results) == [1, 3, 3, 3]


def test_cached_ids_are_merged_not_refetched(monkeypatch):
    import json
    api = Api()
    use(monkeypatch, api)
    aimo.prices(["1", "2"], DAY)
    out = aimo.prices(["1", "2", "3"], DAY)
    assert set(out) == {"1", "2", "3"}
    assert json.loads(api.requests[1].content)["products"] == ["3"]
