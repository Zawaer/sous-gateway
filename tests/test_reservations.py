"""Stock reservations (sous/reservations.py), offline.

Quotes and orders go through the real service; Valio's price and availability are a fixed fake (as in
test_holds.py) with STOCK units of one product.
"""

import time as _time
from datetime import date, datetime, timedelta

import fakeredis
import pytest

from scripts import rush  # its fake product
from sous import catalog, config, holds, quote as Q, reservations as RES, rules, service as S, shared, signing, store
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
MON = date(2026, 11, 16)
STOCK = {"n": 100}
REAL_OFFERS = Q.offers


def product(pid):
    return {**rush._product(pid), "id": pid}


def fake_offers(product_ids, requested, need=None, need_unit=None):
    pr = {"catalogPrice": {"value": 1.0, "valueWithTax": 1.14, "validTo": None}, "comparisonPrice": {"value": 2.0},
          "comparisonUnit": "KG", "tax": {"taxRate": 13.5}, "error": False}
    av = {"logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": False,
          "scheduleLines": [{"salesBlock": False, "availableAmount": STOCK["n"], "availableDate": "2026-11-16",
                             "salesUnit": "ST"}]}
    return [Q.build_offer(product(pid), pr, av, requested, need, need_unit, NOW) for pid in product_ids], NOW


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    STOCK["n"] = 100
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(config, "REQUIRE_AGENT_AUTH", False)
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: product(pid) if pid.startswith("B") else None)
    monkeypatch.setattr(Q, "offers", fake_offers)
    monkeypatch.setattr(Q, "prefetch", lambda ids, requested: None)
    monkeypatch.setattr(S.substitutes, "suggest", lambda *a, **k: {"options": []})
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(holds, "_now", lambda: NOW)
    monkeypatch.setattr(S.notify, "send", lambda *a, **k: None)
    monkeypatch.delenv("SOUS_ALLOCATION", raising=False)
    RES._vol_cache.clear()
    for n in range(1, 4):
        S.issue_mandate(Mandate(id=f"M{n}", buyer=f"Hotel {n}", agent_id=f"a{n}", issued_by="chef",
                                notify_above_eur=None, auto_approve_limit_eur=500 if n < 3 else 1,
                                hard_limit_eur=20000, min_delivery_reliability=None, unusual_quantity_factor=None))
    yield
    shared.configure(url="", client=None)
    signing._key.cache_clear()


def quote(n, amount, pid="B1", day=MON):
    return S.request_quote(S.QuoteRequest(agent_id=f"a{n}", mandate_id=f"M{n}", delivery_date=day,
                                          items=[S.QuoteItem(product_ids=[pid], amount=amount)]))["quote"]


def order(n, q):
    return S.place_order(f"a{n}", f"M{n}", q["id"])


def test_quotes_made_before_any_order_cannot_together_over_allocate():
    q1, q2 = quote(1, 70), quote(2, 70)  # both sized to the 100 Valio has: nothing reserved yet
    assert q1["lines"][0]["quantity"] == q2["lines"][0]["quantity"] == 70
    assert order(1, q1)["status"] == "ordered"
    o2 = order(2, q2)
    assert o2["status"] == "refused"
    assert "availability.allocated" in {f["rule"] for f in o2["decision"]["findings"]}
    assert o2["allocation"]["lines"][0]["left"] == 30 and o2["allocation"]["policy"] == "first_come"
    (opt,) = o2["options"]
    assert opt["kind"] == "allocated_left" and opt["quantity"] == 30
    assert opt["requote"]["items"][0] == {"product_ids": ["B1"], "amount": 30, "unit": "SALES_UNIT"}
    # a new quote sees what's left, and ordering it fills the stock exactly
    q3 = quote(2, 70)
    assert q3["lines"][0]["quantity"] == 30 and q3["uncovered"]
    assert RES.summary("B1", "2026-11-16", NOW)["reserved"] == 70
    q4 = S.request_quote(S.QuoteRequest(agent_id="a2", mandate_id="M2", delivery_date=MON,
                                        items=[S.QuoteItem(product_ids=["B1"], amount=30)]))["quote"]
    assert order(2, q4)["status"] == "ordered"
    assert RES.summary("B1", "2026-11-16", NOW)["reserved"] == 100
    q5 = quote(1, 5)
    assert not q5["lines"][0]["orderable"]
    assert "ALLOCATED" in {i["code"] for i in q5["lines"][0]["issues"]}
    assert order(1, q5)["status"] == "refused"


def test_other_products_and_dates_are_separate():
    assert order(1, quote(1, 100))["status"] == "ordered"
    assert quote(2, 50, pid="B2")["lines"][0]["quantity"] == 50
    assert quote(2, 50, day=MON + timedelta(days=1))["lines"][0]["quantity"] == 50


def test_held_order_reserves_tentatively_until_decided():
    q = quote(3, 60)  # M3 auto-approves only 1 EUR: held for a human
    o = order(3, q)
    assert o["status"] == "pending_approval"
    s = RES.summary("B1", "2026-11-16", NOW)
    assert s["reserved"] == 60 and s["tentative"] == 60
    assert quote(1, 70)["lines"][0]["quantity"] == 40
    S.decide(o["id"], "chef", approve=False)
    assert RES.summary("B1", "2026-11-16", NOW)["reserved"] == 0
    o = order(3, quote(3, 60))
    S.decide(o["id"], "chef", approve=True)
    s = RES.summary("B1", "2026-11-16", NOW)
    assert s["reserved"] == 60 and s["tentative"] == 0


def test_reservations_end_by_ttl_or_when_valio_reflects_them():
    order(1, quote(1, 40))
    order(2, quote(2, 30))
    assert RES.summary("B1", "2026-11-16", NOW)["reserved"] == 70
    STOCK["n"] = 60  # Valio's refresh shows the first order (40) arrived; not yet the second
    q = quote(1, 100)
    assert q["lines"][0]["quantity"] == 30  # 60 - the 30 still open
    assert RES.summary("B1", "2026-11-16", NOW)["reserved"] == 30
    later = NOW + RES.ttl() + timedelta(seconds=1)
    assert RES.summary("B1", "2026-11-16", later)["reserved"] == 0


def test_pro_rata_caps_each_buyer_by_four_week_volume(monkeypatch):
    monkeypatch.setenv("SOUS_ALLOCATION", "pro_rata")
    monkeypatch.setattr(RES, "volumes", lambda pid, now: {"Hotel 1": 300.0, "Hotel 2": 100.0})
    q1 = quote(1, 90)  # alone so far: demand 90 <= 100, no cap
    assert q1["lines"][0]["quantity"] == 90
    q2 = quote(2, 90)  # now demand 180 > 100: Hotel 2's share is 25
    assert q2["lines"][0]["quantity"] == 25
    o1 = order(1, q1)  # Hotel 1's share is 75
    assert o1["status"] == "refused" and o1["allocation"]["lines"][0] == {
        **o1["allocation"]["lines"][0], "left": 75, "why": "share"}
    assert "Valio shares it" in o1["decision"]["findings"][0]["message"] or any(
        "Valio shares it" in f["message"] for f in o1["decision"]["findings"])
    assert order(2, q2)["status"] == "pending_approval"  # partial need: a human confirms (need.partial)
    q = S.request_quote(S.QuoteRequest(agent_id="a1", mandate_id="M1", delivery_date=MON,
                                       items=[S.QuoteItem(product_ids=["B1"], amount=75)]))["quote"]
    assert order(1, q)["status"] == "ordered"
    assert RES.summary("B1", "2026-11-16", NOW)["reserved"] == 100


def test_shares_water_filling():
    caps = RES.shares(100, {"a": 10, "b": 90, "c": 90}, {"a": 1, "b": 1, "c": 2})
    assert caps["a"] == 10 and round(caps["b"], 3) == 30 and round(caps["c"], 3) == 60


def test_valkey_mirror_is_shared_between_deployments(tmp_path, monkeypatch):
    server = fakeredis.FakeServer()
    shared.configure(url="redis://fake", client=fakeredis.FakeRedis(server=server, decode_responses=True))
    assert order(1, quote(1, 80))["status"] == "ordered"
    fr = fakeredis.FakeRedis(server=server, decode_responses=True)
    assert len(fr.hgetall(f"{shared.PREFIX}resv:{shared.ROLE}:B1:2026-11-16")) == 1
    assert fr.get(f"{shared.PREFIX}resv-obs:B1:2026-11-16")
    # another deployment: its own (empty) database, the same Valkey
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "other.db")
    monkeypatch.setattr(store, "_conn", None)
    S.issue_mandate(Mandate(id="M2", buyer="Hotel 2", agent_id="a2", issued_by="chef", notify_above_eur=None,
                            hard_limit_eur=20000, min_delivery_reliability=None, unusual_quantity_factor=None))
    assert quote(2, 50)["lines"][0]["quantity"] == 20  # sized to what A left, read from Valkey
    # B quotes while Valkey is unreachable (sees all 100), then orders once it is back: Valkey refuses it
    client = shared._client
    shared.configure(url="", client=None)
    q = quote(2, 50)
    assert q["lines"][0]["quantity"] == 50
    shared.configure(url="redis://fake", client=client)
    o = order(2, q)
    assert o["status"] == "refused" and o["allocation"]["lines"][0]["left"] == 20


def test_rule_is_registered():
    r = rules.get("availability.allocated")
    assert r["owner"] == "valio" and r["tiers"] == ["block"]


def sandbox_mandate(n, sbx):
    S.issue_mandate(Mandate(id=f"M{n}", buyer=f"Visitor {n}", agent_id=f"a{n}", issued_by="judge", sandbox=sbx,
                            notify_above_eur=None, auto_approve_limit_eur=500, hard_limit_eur=20000,
                            min_delivery_reliability=None, unusual_quantity_factor=None))


def test_sandbox_orders_reserve_only_within_their_sandbox(monkeypatch):
    import contextlib
    monkeypatch.setattr(S.sandbox, "metered", lambda m: contextlib.nullcontext())  # no sandbox rows here
    sandbox_mandate(4, "SBX-AAAA")
    sandbox_mandate(5, "SBX-BBBB")
    sandbox_mandate(6, "SBX-AAAA")
    assert order(4, quote(4, 90))["status"] == "ordered"
    assert quote(5, 80)["lines"][0]["quantity"] == 80  # another sandbox: its own world, full stock
    assert quote(1, 80)["lines"][0]["quantity"] == 80  # the global pool doesn't see sandbox orders either
    assert quote(6, 80)["lines"][0]["quantity"] == 10  # the same sandbox does
    assert RES.summary("B1", "2026-11-16", NOW, "SBX-AAAA")["reserved"] == 90
    assert RES.summary("B1", "2026-11-16", NOW)["reserved"] == 0


def test_eval_style_cases_each_see_full_stock():
    try:
        for case in ("H01", "H02", "H03"):
            RES.set_scope(f"eval-test-{case}")
            q = quote(1, 80)
            assert q["lines"][0]["quantity"] == 80, case
            assert order(1, q)["status"] == "ordered"
    finally:
        RES.set_scope(None)
    assert quote(1, 80)["lines"][0]["quantity"] == 80  # nothing reached the global pool


def at(monkeypatch, t):
    """Move the gateway's clock (and the fake Valio's reading time) to t."""
    monkeypatch.setattr(S, "_now", lambda: t)
    monkeypatch.setattr(Q, "offers", lambda *a, **k: (fake_offers(*a, **k)[0], t))


def valkey():
    server = fakeredis.FakeServer()
    shared.configure(url="redis://fake", client=fakeredis.FakeRedis(server=server, decode_responses=True))
    return fakeredis.FakeRedis(server=server, decode_responses=True)


@pytest.mark.parametrize("with_valkey", [False, True])
def test_release_unchanged_needs_a_fresh_reading(monkeypatch, with_valkey):
    """rene-codex-qa 11:01Z: with the flag on, time passing alone must not release a reservation."""
    if with_valkey:
        valkey()
    monkeypatch.setenv("SOUS_RESERVATION_RELEASE_UNCHANGED", "1")
    q1, q2 = quote(1, 70), quote(2, 70)
    assert order(1, q1)["status"] == "ordered"
    at(monkeypatch, NOW + timedelta(seconds=61))  # no new Valio reading
    o2 = order(2, q2)
    assert o2["status"] == "refused" and o2["allocation"]["lines"][0]["left"] == 30
    assert RES.summary("B1", "2026-11-16", NOW + timedelta(seconds=61))["reserved"] == 70


class FakeTime:
    """time.time() for aimo's cache and reservations' reading ages; everything else is the real time module."""

    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def __getattr__(self, name):
        return getattr(_time, name)


@pytest.fixture
def live_valio(monkeypatch):
    """The real quote path (Q.offers -> aimo cache, L1 and shared Valkey) over a fake Valio at aimo._fetch,
    with one clock: set(t) moves Valio's wall time and the gateway's business time (NOW + t) together."""
    from sous import aimo, clock

    ft, fetches = FakeTime(), []
    start = ft.t

    def fetch(kind, day, chunk):
        fetches.append((kind, round(ft.t - start, 1)))
        if kind == "price":
            return [{"productId": p, "catalogPrice": {"value": 1.0, "valueWithTax": 1.14, "validTo": None},
                     "comparisonPrice": {"value": 2.0}, "comparisonUnit": "KG", "tax": {"taxRate": 13.5}}
                    for p in chunk]
        return [{"productId": p, "logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200",
                 "salesBlock": False, "scheduleLines": [{"salesBlock": False, "availableAmount": STOCK["n"],
                                                         "availableDate": "2026-11-16", "salesUnit": "ST"}]}
                for p in chunk]

    def set_t(t):
        ft.t = start + t
        monkeypatch.setattr(S, "_now", lambda: NOW + timedelta(seconds=t))
        monkeypatch.setattr(clock, "now", lambda: NOW + timedelta(seconds=t))

    aimo.reset()
    monkeypatch.setattr(aimo, "time", ft)
    monkeypatch.setattr(RES, "time", ft)
    monkeypatch.setattr(aimo, "_fetch", fetch)
    monkeypatch.setattr(Q, "offers", REAL_OFFERS)
    set_t(0)
    yield set_t, fetches
    aimo.reset()


@pytest.mark.parametrize("with_valkey", [False, True])
def test_release_unchanged_ignores_a_cache_hit(monkeypatch, live_valio, with_valkey):
    """rene-codex-qa: a cache hit 61 s after the order, on a reading Valio returned 1.1 s earlier (59.9 s after
    the order), must not count as a reading fetched at quote time."""
    set_t, fetches = live_valio
    if with_valkey:
        valkey()
    monkeypatch.setenv("SOUS_RESERVATION_RELEASE_UNCHANGED", "1")
    set_t(-0.2)
    q1, q2 = quote(1, 70), quote(2, 70)  # one Valio reading at -0.2 s, then a cache hit
    set_t(0)
    assert order(1, q1)["status"] == "ordered"
    set_t(59.9)
    quote(3, 1)  # the cache is 60.1 s old: Valio is asked again at 59.9 s (still < 60 s after the order)
    set_t(61)
    quote(3, 1)  # a cache hit: the reading is from 59.9 s, not from now
    assert [t for k, t in fetches if k == "availability"] == [-0.2, 59.9]
    o2 = order(2, q2)
    assert o2["status"] == "refused" and o2["allocation"]["lines"][0]["left"] == 30
    assert RES.summary("B1", "2026-11-16", NOW + timedelta(seconds=61))["reserved"] == 70


@pytest.mark.parametrize("with_valkey", [False, True])
def test_release_unchanged_on_a_fresh_unchanged_reading(monkeypatch, live_valio, with_valkey):
    set_t, fetches = live_valio
    if with_valkey:
        valkey()
    monkeypatch.setenv("SOUS_RESERVATION_RELEASE_UNCHANGED", "1")
    order(1, quote(1, 80))
    assert quote(2, 80)["lines"][0]["quantity"] == 20  # a cache hit: still reserved
    set_t(61)
    assert quote(2, 80)["lines"][0]["quantity"] == 80  # Valio asked again at 61 s, still 100: released
    assert [t for k, t in fetches if k == "availability"] == [0, 61]
    assert RES.summary("B1", "2026-11-16", NOW + timedelta(seconds=61))["reserved"] == 0


def test_release_unchanged_is_off_by_default(monkeypatch):
    order(1, quote(1, 80))
    at(monkeypatch, NOW + timedelta(seconds=61))
    assert quote(2, 80)["lines"][0]["quantity"] == 20


def test_eval_scopes_with_valkey_never_bleed():
    """rene-claude-evals 11:24Z: with a shared Valkey, one case's orders must not reach the next case or run."""
    fr = valkey()
    try:
        RES.set_scope("eval-run1-H01")
        assert order(1, quote(1, 90))["status"] == "ordered"
        keys = [k for k in fr.keys("*resv:*") if "eval-run1-H01" in k]
        assert len(keys) == 1 and f":{shared.ROLE}:" in keys[0]
        assert 0 < fr.ttl(keys[0]) <= RES.EVAL_SCOPE_TTL
        RES.set_scope("eval-run1-H02")  # leaving H01 ends its pool
        assert not fr.exists(keys[0])
        assert quote(1, 90)["lines"][0]["quantity"] == 90
        assert order(1, quote(1, 90))["status"] == "ordered"
    finally:
        RES.set_scope(None)
    assert not [k for k in fr.keys("*resv:*") if "eval-run1" in k]
    assert quote(1, 100)["lines"][0]["quantity"] == 100  # nothing in the global pool either
