"""Rules added after comparing with other gateways: weekly limit, split orders, first-time products, Valio's pause.

Offline: the catalog and Valio's price/availability (quote.offers) are replaced; the clock is S._now.
"""

from datetime import date, datetime, timedelta

import pytest

from sous import catalog, config, pause, quote as Q, service as S, signing, store
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)  # a Friday
MON = date(2026, 11, 26)  # deliverable in every test clock below


def product(pid):
    return {
        "id": pid, "name": f"Milk {pid} 1 l", "names": {"fi": "", "en": "", "sv": ""}, "brand": "", "vendor": "",
        "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
        "allowed_lot_size": 1.0, "temperature": "chilled", "labels": [], "claims": [],
        "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": []},
        "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
        "official_substitutes": [], "deleted": False,
    }


PRODUCTS = {pid: product(pid) for pid in ("MILK", "OATS")}
clock = {"now": NOW}


def fake_offers(product_ids, requested, need=None, need_unit=None):
    pr = {"catalogPrice": {"value": 1.0, "valueWithTax": 1.14, "validTo": None}, "comparisonPrice": {"value": 1.0},
          "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}
    av = {"logisticModel": "Stock", "closingTime": "2026-11-25T17:00:00+0200", "salesBlock": False,
          "scheduleLines": [{"salesBlock": False, "availableAmount": 99999, "availableDate": "2026-11-26",
                             "salesUnit": "ST"}]}
    return [Q.build_offer(PRODUCTS[p], pr, av, requested, need, need_unit, clock["now"]) for p in product_ids], clock["now"]


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(config, "REQUIRE_AGENT_AUTH", False)
    monkeypatch.setattr(config, "APPROVER_CODE", "")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(pid))
    monkeypatch.setattr(Q, "offers", fake_offers)
    clock["now"] = NOW
    monkeypatch.setattr(S, "_now", lambda: clock["now"])
    monkeypatch.setattr(S.notify, "send", lambda *a, **k: None)
    yield
    signing._key.cache_clear()


def issue(mid="M", buyer="Hotel", **kw):
    S.issue_mandate(Mandate(**{"id": mid, "buyer": buyer, "agent_id": "a", "issued_by": "chef",
                               "auto_approve_limit_eur": 5000, "hard_limit_eur": None,
                               "min_delivery_reliability": None, "unusual_quantity_factor": None, **kw}))


def order(eur, pid="MILK", mid="M"):
    """Quote and order `eur` litres of a €1/L product: the order total is `eur` euros."""
    q = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id=mid, delivery_date=MON, items=[
        S.QuoteItem(product_ids=[pid], amount=eur, unit="L")]))["quote"]
    return S.place_order("a", mid, q["id"])


def fired(o):
    return [f["rule"] for f in o["decision"]["findings"] if f["outcome"] != "silent"]


def test_new_rules_are_off_by_default():
    issue(auto_approve_limit_eur=50)
    for _ in range(4):
        o = order(15)
        assert o["status"] == "ordered" and fired(o) == [], o["decision"]


def test_split_orders_are_checked_together_within_the_window():
    issue(auto_approve_limit_eur=50, split_window_minutes=60)
    for i in range(3):  # €45 together: under the €50 the agent may spend alone
        clock["now"] = NOW + timedelta(minutes=10 * i)
        assert order(15)["status"] == "ordered"
    clock["now"] = NOW + timedelta(minutes=30)
    o = order(15)
    assert o["status"] == "pending_approval" and fired(o) == ["spend.split_orders"]
    msg = o["decision"]["findings"][0]["message"]
    assert "3 more in the last 60 min" in msg and "€60.00" in msg
    # an hour after the first three, they no longer count
    clock["now"] = NOW + timedelta(minutes=95)
    assert order(15)["status"] == "ordered"


def test_split_rule_leaves_big_orders_to_the_auto_limit():
    issue(auto_approve_limit_eur=50, split_window_minutes=60)
    assert order(40)["status"] == "ordered"
    o = order(60)  # over the limit on its own: spend.auto_limit, not reported twice
    assert fired(o) == ["spend.auto_limit"]


def test_weekly_limit_counts_monday_to_sunday():
    issue(weekly_limit_eur=100)
    assert order(90)["status"] == "ordered"
    o = order(20)
    assert o["status"] == "pending_approval" and fired(o) == ["spend.weekly_limit"]
    assert "€110.00" in o["decision"]["findings"][0]["message"]
    clock["now"] = NOW + timedelta(days=3)  # Monday: a new week
    assert order(20)["status"] == "ordered"


def test_first_time_product_gets_the_chosen_tier_until_it_has_been_ordered():
    issue(first_time_products="ask")
    o = order(10)
    assert o["status"] == "pending_approval" and fired(o) == ["product.first_time"]
    assert S.decide(o["id"], "chef", True)["status"] == "ordered"
    assert order(10)["status"] == "ordered"  # now a known product
    o = order(10, pid="OATS")
    assert fired(o) == ["product.first_time"] and o["decision"]["findings"][0]["product_id"] == "OATS"


def test_first_time_notify_lets_the_order_through_with_an_fyi():
    issue(first_time_products="notify")
    o = order(10)
    assert o["status"] == "ordered" and o["decision"]["outcome"] == "notify"


def test_pause_refuses_orders_and_approvals_until_resumed():
    issue(auto_approve_limit_eur=50)
    held = order(60)
    assert held["status"] == "pending_approval"
    pause.pause(pause.PauseIn(reason="Incident: agent orders on hold", by="valio ops"))
    o = order(10)
    assert o["status"] == "refused" and "ordering.paused" in fired(o)
    assert "Incident: agent orders on hold" in o["decision"]["findings"][0]["message"]
    assert S.decide(held["id"], "chef", True)["status"] == "refused"
    assert pause.status()["paused_for_everyone"] is True
    pause.resume(pause.ResumeIn(by="valio ops"))
    assert order(10)["status"] == "ordered"
    events = [e["event"] for e in store.audit_events(limit=200)]
    assert "ordering.paused" in events and "ordering.resumed" in events


def test_pause_for_one_buyer_leaves_the_others():
    issue("M", buyer="Hotel")
    issue("M2", buyer="School")
    pause.pause(pause.PauseIn(buyer="School", reason="Unpaid invoices"))
    assert order(10, mid="M")["status"] == "ordered"
    assert order(10, mid="M2")["status"] == "refused"
    assert pause.resume(pause.ResumeIn(buyer="Hotel")) is None  # not paused


def test_pause_over_rest_needs_the_global_approver_code(monkeypatch):
    from fastapi.testclient import TestClient
    from sous.api import app
    monkeypatch.setattr(config, "APPROVER_CODE", "secret")
    c = TestClient(app)
    assert c.get("/agent-ordering").json() == {"paused_for_everyone": False, "pauses": []}
    assert c.post("/agent-ordering/pause", json={"reason": "x"}).status_code == 401
    r = c.post("/agent-ordering/pause", json={"reason": "Incident"}, headers={"x-approver-code": "secret"})
    assert r.status_code == 200 and r.json()["paused_for_everyone"] is True
    assert c.get("/agent-ordering").json()["pauses"][0]["reason"] == "Incident"
    assert c.post("/agent-ordering/resume", json={}, headers={"x-approver-code": "secret"}).status_code == 200
    assert c.post("/agent-ordering/resume", json={}, headers={"x-approver-code": "secret"}).status_code == 404
    rules = {r["id"] for r in c.get("/rules").json()["rules"]}
    assert {"ordering.paused", "spend.weekly_limit", "spend.split_orders", "product.first_time"} <= rules
