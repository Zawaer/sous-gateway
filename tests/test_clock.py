"""Demo clock (sous/clock.py): frozen and advancing modes, cut-offs judged against it, and disclosure in
signed quotes, orders, audit events, /health and response headers. Offline: no Valio calls, no data files."""

from datetime import date, datetime, timedelta

import pytest

from sous import aimo, catalog, clock, config, quote as Q, service as S, signing, store
from sous.mandate import Mandate

TZ = config.TZ
CUTOFF = "2026-11-15T17:00:00+0200"  # what Valio's mock returns for Stock products
AFTER_CUTOFF = datetime(2026, 11, 16, 10, 0, tzinfo=TZ)  # judges reviewing on Monday
DEMO = "2026-11-15T09:00:00+02:00"


def product(pid="1"):
    return {
        "id": pid, "name": "Lactose-free milk 1 l", "names": {"fi": "Laktoositon maito 1 l", "en": "", "sv": ""},
        "brand": "", "vendor": "", "category": "10400", "country_of_origin": "fi", "sales_unit": "ST",
        "base_unit": "ST", "units": {"ST": 1.0}, "allowed_lot_size": 0.01, "temperature": "chilled", "labels": [],
        "claims": ["FREE_FROM_LACTOSE"],
        "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": []},
        "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": 1.4,
        "official_substitutes": [], "deleted": False,
    }


PRICE = {"catalogPrice": {"value": 1.4, "valueWithTax": 1.59, "validTo": "2026-12-31T00:00:00+0200"},
         "comparisonPrice": {"value": 1.4}, "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}
AVAIL = {"logisticModel": "Stock", "closingTime": CUTOFF, "salesBlock": False,
         "scheduleLines": [{"salesBlock": False, "availableAmount": 5000.0, "availableDate": "2026-11-16",
                            "salesUnit": "ST"}]}


class RealClock:
    """Stands in for the wall clock so 'advancing' can be tested without sleeping."""
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: product(pid))
    monkeypatch.setattr(aimo, "prices", lambda ids, d: {i: PRICE for i in ids})
    monkeypatch.setattr(aimo, "availability", lambda ids, d: {i: AVAIL for i in ids})
    monkeypatch.setattr(S.notify, "send", lambda *a, **k: None)
    # a troubled line (cut-off passed) makes the quote look up substitutes, which needs the catalog index
    monkeypatch.setattr(S.substitutes, "suggest", lambda *a, **k: {"options": []})
    real = RealClock(AFTER_CUTOFF)
    monkeypatch.setattr(clock, "real_now", real)
    clock.configure(None)
    yield real
    clock.configure(None)
    signing._key.cache_clear()


def quote_req(**kw):
    return S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=date(2026, 11, 16),
                          items=[S.QuoteItem(product_ids=["1"], amount=100, unit="L")], **kw)


def issue_mandate():
    S.issue_mandate(Mandate(id="M", buyer="Hotel", agent_id="a", issued_by="chef"))


# --- the clock itself --------------------------------------------------------

def test_real_mode_by_default(env):
    assert clock.mode() == "real" and not clock.is_demo()
    assert clock.now() == AFTER_CUTOFF
    assert clock.header() is None
    assert clock.info() == {"mode": "real", "now": AFTER_CUTOFF.isoformat(timespec="seconds")}
    assert clock.audit_tag() == {"mode": "real"}


def test_frozen_mode_stays_put(env):
    clock.configure(DEMO, "frozen")
    assert clock.now() == datetime.fromisoformat(DEMO)
    env.t += timedelta(hours=3)
    assert clock.now() == datetime.fromisoformat(DEMO)
    info = clock.info()
    assert info["mode"] == "demo" and info["advancing"] is False
    assert "Demo clock" in info["note"] and "2026-11-15T09:00:00+02:00" in info["note"]
    assert clock.header() == "demo 2026-11-15T09:00:00+02:00"


def test_advancing_mode_moves_with_real_time(env):
    clock.configure(DEMO)
    assert clock.now() == datetime.fromisoformat(DEMO)
    env.t += timedelta(minutes=10)
    assert clock.now() == datetime.fromisoformat(DEMO) + timedelta(minutes=10)
    assert clock.info()["advancing"] is True
    assert clock.info()["real_now"] == env.t.isoformat(timespec="seconds")


def test_naive_demo_time_is_helsinki(env):
    clock.configure("2026-11-15T09:00:00", "frozen")
    assert clock.now().utcoffset() == timedelta(hours=2)
    assert clock.now() == datetime(2026, 11, 15, 9, 0, tzinfo=TZ)


def test_service_now_uses_the_clock(env):
    clock.configure(DEMO, "frozen")
    assert S._now() == datetime.fromisoformat(DEMO)


# --- cut-offs and signed artifacts ---------------------------------------------

def test_cutoff_passed_on_real_clock(env):
    offers, now = Q.offers(["1"], date(2026, 11, 16), 100, "L")
    assert now == AFTER_CUTOFF
    assert not offers[0]["orderable"]
    assert any(i["code"] == "CUTOFF_PASSED" for i in offers[0]["issues"])


def test_cutoff_evaluated_against_demo_clock(env):
    clock.configure(DEMO, "frozen")
    offers, now = Q.offers(["1"], date(2026, 11, 16), 100, "L")
    assert now == datetime.fromisoformat(DEMO)
    assert offers[0]["orderable"], offers[0]["issues"]


def test_signed_quote_and_order_carry_the_clock(env):
    clock.configure(DEMO, "frozen")
    issue_mandate()
    r = S.request_quote(quote_req())
    q = signing.verify(r["jws"], "sous-quote+jws")
    assert q["clock"]["mode"] == "demo" and q["clock"]["now"] == "2026-11-15T09:00:00+02:00"
    assert "Valio's mock returns fixed dates" in q["clock"]["note"]
    assert q["issued_at"].startswith("2026-11-15T09:00")
    assert q["lines"][0]["orderable"]
    # the quote expires by the (demo-clock) cut-off, not already in the past
    assert datetime.fromisoformat(q["expires_at"]) > datetime.fromisoformat(DEMO)

    o = S.place_order("a", "M", q["id"], idempotency_key="k1")
    assert o["status"] == "ordered"
    assert o["clock"]["mode"] == "demo"
    receipt = signing.verify(o["receipt_jws"], "sous-order+jws")
    assert receipt["clock"]["mode"] == "demo"

    events = store.audit_events(limit=50)
    assert events and all(e["data"]["clock"]["mode"] == "demo" for e in events)


def test_real_mode_quote_says_real(env):
    issue_mandate()
    q = S.request_quote(quote_req())["quote"]
    assert q["clock"] == {"mode": "real", "now": AFTER_CUTOFF.isoformat(timespec="seconds")}
    assert not q["lines"][0]["orderable"]
    assert store.audit_events(limit=5)[0]["data"]["clock"] == {"mode": "real"}


def test_audit_chain_still_verifies_with_clock_tag(env):
    clock.configure(DEMO, "frozen")
    issue_mandate()
    S.request_quote(quote_req())
    assert store.verify_chain()["ok"]


# --- disclosure on the wire ------------------------------------------------------

def test_health_and_header(env):
    from fastapi.testclient import TestClient
    from sous import api
    c = TestClient(api.app)  # no lifespan: the catalog index isn't needed
    r = c.get("/health")
    assert r.json()["clock"]["mode"] == "real" and "x-sous-clock" not in r.headers
    clock.configure(DEMO, "frozen")
    r = c.get("/health")
    assert r.json()["clock"]["mode"] == "demo"
    assert r.headers["x-sous-clock"] == "demo 2026-11-15T09:00:00+02:00"
    assert c.get("/mandates/nope").headers["x-sous-clock"].startswith("demo ")  # errors too


def test_credential_windows_stay_on_real_time(env):
    clock.configure(DEMO, "frozen")
    assert S._now() == datetime.fromisoformat(DEMO)
    assert S._real_now() == AFTER_CUTOFF  # a buyer's VC signed "now" is never "not yet valid" under the demo clock
