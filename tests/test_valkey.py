"""Shared Valio Aimo budget, circuit breaker and offer cache through Valkey (sous/shared.py).

Offline: Valkey is fakeredis, Valio Aimo is an httpx.MockTransport. Two "deployments" are
simulated by switching the role and clearing the in-process state between calls while the
Valkey server (one FakeServer) persists, the same as two processes sharing one Aiven service.
"""

import json
import logging
from datetime import date

import fakeredis
import httpx
import pytest
import redis

from sous import aimo, shared

DAY = date(2026, 11, 16)


def ok(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(200, json={"timeNow": "2026-11-13T21:00:00+0200",
                                     "products": [{"productId": p, "catalogPrice": {"value": 1.0}}
                                                  for p in body["products"]]})


class Api:
    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        step = self.script.pop(0) if self.script else ok
        return step(request) if callable(step) else step


@pytest.fixture
def server():
    return fakeredis.FakeServer()


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    aimo.reset()
    monkeypatch.setattr(aimo, "_sleep", lambda s: None)
    yield
    aimo.reset()
    shared.configure(url="", role="dev", limit=50, client=None)


def use(monkeypatch, api):
    monkeypatch.setattr(aimo, "_client", httpx.Client(base_url="https://aimo.test/api", transport=httpx.MockTransport(api)))


def deployment(server, role, limit=10):
    """Switch this process to act as another deployment sharing the same Valkey."""
    aimo.reset()
    shared.configure(url="rediss://default:secret@valkey.test:12345", role=role, limit=limit,
                     client=fakeredis.FakeRedis(server=server, decode_responses=True))


def test_unset_means_local_behaviour(monkeypatch):
    shared.configure(url="", client=None)
    use(monkeypatch, Api())
    assert set(aimo.prices(["1"], DAY)) == {"1"}
    b = aimo.health()["shared_budget"]
    assert b["backend"] == "local" and b["ok"] and b["used_this_minute"] == 1


def test_budget_is_shared_between_deployments(monkeypatch, server):
    use(monkeypatch, Api())
    deployment(server, "staging", limit=10)  # staging may use 5
    for i in range(5):
        aimo.prices([f"s{i}"], DAY)
    with pytest.raises(aimo.AimoError) as e:
        aimo.prices(["s9"], DAY)
    assert e.value.rate_limited and e.value.retry_after >= 1
    # A CI process (its own per-process budget is untouched) sees the same window: also capped.
    deployment(server, "ci", limit=10)
    with pytest.raises(aimo.AimoError):
        aimo.prices(["c1"], DAY)
    b = aimo.health()["shared_budget"]
    assert b["backend"] == "valkey" and b["ok"] and b["used_this_minute"] >= 5
    assert b["by_role"] == {"staging": 5} and b["role"] == "ci" and b["limit"] == 10 and b["role_limit"] == 5


def test_prod_keeps_its_reserved_share(monkeypatch, server):
    use(monkeypatch, Api())
    deployment(server, "ci", limit=10)
    for i in range(5):
        aimo.prices([f"c{i}"], DAY)
    deployment(server, "prod", limit=10)
    for i in range(5):  # prod still gets the other half
        aimo.prices([f"p{i}"], DAY)
    with pytest.raises(aimo.AimoError):  # and the global limit holds for prod too
        aimo.prices(["p9"], DAY)
    deployment(server, "staging", limit=10)
    with pytest.raises(aimo.AimoError):
        aimo.prices(["s1"], DAY)


def test_429_opens_the_circuit_for_every_deployment(monkeypatch, server):
    deployment(server, "ci")
    use(monkeypatch, Api(httpx.Response(429, headers={"Retry-After": "30"})))
    with pytest.raises(aimo.AimoError):
        aimo.prices(["1"], DAY)
    assert float(fakeredis.FakeRedis(server=server).get("sous:valio:circuit_until")) > 0
    deployment(server, "prod")
    api = Api()
    use(monkeypatch, api)
    with pytest.raises(aimo.AimoError) as e:
        aimo.prices(["2"], DAY)
    assert e.value.rate_limited and e.value.retry_after >= 29
    assert api.requests == []  # prod paused without ever calling Valio
    assert aimo.health()["circuit"] == "open"


def test_offer_cache_shared_between_deployments(monkeypatch, server):
    deployment(server, "staging")
    use(monkeypatch, Api())
    aimo.prices(["1", "2"], DAY)
    deployment(server, "prod")
    api = Api()
    use(monkeypatch, api)
    before = aimo.stats["cache_hits"]
    out = aimo.prices(["1", "2", "3"], DAY)
    assert set(out) == {"1", "2", "3"}
    assert [json.loads(r.content)["products"] for r in api.requests] == [["3"]]  # only the miss goes live
    assert aimo.stats["cache_hits"] - before == 2
    assert aimo.health()["shared_budget"]["shared_cache_hits"] == 2


def test_stale_copy_from_another_deployment(monkeypatch, server):
    deployment(server, "staging")
    use(monkeypatch, Api())
    aimo.availability(["1"], DAY)
    # Age the shared copy past the availability TTL but within its stale horizon.
    r = fakeredis.FakeRedis(server=server, decode_responses=True)
    key = f"sous:valio:offer:availability:{DAY.isoformat()}:1"
    keep = max(aimo.CACHED_MAX, aimo.AVAILABILITY_STALE_MAX)  # last-known copy, and the sandbox cached answer
    assert keep - 5 < r.ttl(key) <= keep
    d = json.loads(r.get(key))
    d["t"] -= aimo.AVAILABILITY_TTL + 30
    r.set(key, json.dumps(d), keepttl=True)
    deployment(server, "prod")
    use(monkeypatch, Api(httpx.Response(503, text="down"), httpx.Response(503), httpx.Response(503)))
    out = aimo.availability(["1"], DAY)
    assert out["1"]["_stale_seconds"] >= aimo.AVAILABILITY_TTL


class Broken(fakeredis.FakeRedis):
    def execute_command(self, *a, **k):
        raise redis.ConnectionError("Error connecting to valkey.test:12345")

    def pipeline(self, *a, **k):
        raise redis.ConnectionError("Error connecting to valkey.test:12345")


def test_fail_open_when_valkey_is_down(monkeypatch, caplog):
    shared.configure(url="rediss://default:secret@valkey.test:12345", role="prod", limit=1, client=Broken())
    use(monkeypatch, Api())
    with caplog.at_level(logging.WARNING, logger="sous.shared"):
        for i in range(3):  # global limit 1 doesn't apply: per-process budget instead
            assert set(aimo.prices([f"{i}"], DAY)) == {f"{i}"}
    warnings = [r for r in caplog.records if "unreachable" in r.getMessage()]
    assert len(warnings) == 1  # logged once
    b = aimo.health()["shared_budget"]
    assert b["backend"] == "local" and b["ok"] is False and b["used_this_minute"] == 3
    text = json.dumps(aimo.health()) + caplog.text
    assert "secret" not in text and "rediss://" not in text


def test_unreachable_url_fails_open_quickly(monkeypatch):
    # Real redis-py client against a closed port: no fakeredis involved.
    shared.configure(url="redis://default:secret@127.0.0.1:1", role="prod", limit=1, client=None)
    use(monkeypatch, Api())
    assert set(aimo.prices(["1", "2"], DAY)) == {"1", "2"}
    assert aimo.health()["shared_budget"]["backend"] == "local"


def test_shared_budget_refusal_is_a_wait_not_a_stale_copy(monkeypatch, server):
    # staging leaves a copy in Valkey; later ci is over its share: it gets a retryable error, not DATA_STALE.
    deployment(server, "staging", limit=2)
    use(monkeypatch, Api())
    aimo.availability(["1"], DAY)
    r = fakeredis.FakeRedis(server=server, decode_responses=True)
    key = f"sous:valio:offer:availability:{DAY.isoformat()}:1"
    d = json.loads(r.get(key))
    d["t"] -= aimo.AVAILABILITY_TTL + 30
    r.set(key, json.dumps(d), keepttl=True)
    deployment(server, "ci", limit=2)  # ci may use 1, staging already used it
    with pytest.raises(aimo.SharedBudgetLimit) as e:
        aimo.availability(["1"], DAY)
    assert e.value.rate_limited and e.value.retry_after >= 1


def test_local_budget_refusal_does_not_serve_other_deployments_old_copies(monkeypatch, server):
    deployment(server, "staging")
    use(monkeypatch, Api())
    aimo.availability(["1"], DAY)
    r = fakeredis.FakeRedis(server=server, decode_responses=True)
    key = f"sous:valio:offer:availability:{DAY.isoformat()}:1"
    d = json.loads(r.get(key))
    d["t"] -= aimo.AVAILABILITY_TTL + 30
    r.set(key, json.dumps(d), keepttl=True)
    deployment(server, "prod")
    monkeypatch.setattr(aimo.config, "AIMO_CALLS_PER_MIN", 1)
    aimo.prices(["9"], DAY)  # this process's own cap is now used up
    with pytest.raises(aimo.BudgetLimit):
        aimo.availability(["1"], DAY)
