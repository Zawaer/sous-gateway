"""Valio Aimo's proactive offers (sous/offers.py): signed offers, guardrails, delivery, quoting on an offer.

Offline with test_sandbox.py's fixtures: catalog of OAT (+ CREAM here, which contains milk), Valio Aimo mocked at
€1.00 per unit, fixed clock.
"""

import asyncio
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from jsonschema.validators import validator_for
from mcp import Client

from test_sandbox import CODE, NOW, OAT, env, new, seed_curated  # noqa: F401  (pytest fixture)

from sous import api, catalog, offers, rules, service as S, signing, store
from sous.mcp_server import mcp

CREAM = {**OAT, "id": "CREAM", "name": "Whipping cream 1 l", "category": "10401",
         "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": []}}
HOTEL, HOTEL_AGENT = "M-HOTEL-AURORA", "aurora-purchasing-agent"
HOSPITAL = "M-HOSPITAL-KITCHEN"  # avoids milk (AM)
SCHOOL, SCHOOL_AGENT = "M-SCHOOL-ESPOO", "espoo-school-agent"
VALIO = {"X-Approver-Code": CODE}
UNTIL = (NOW + timedelta(days=2)).isoformat()


@pytest.fixture(autouse=True)
def products(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(catalog, "get", lambda pid: {"OAT": OAT, "CREAM": CREAM}.get(pid))
    seed_curated()


@pytest.fixture
def c():
    return TestClient(api.app)


def offer(c, **kw):
    body = {"target": {"mandate_ids": [HOTEL, HOSPITAL]}, "product_ids": ["OAT", "CREAM"], "price_ex_vat": 0.8,
            "quantity_cap": 30, "valid_until": UNTIL, "reason": "near_best_before",
            "reason_text": "best before 20.11.", **kw}
    return c.post("/offers", json=body, headers=VALIO)


def quote(c, offer_id, amount=10, mandate=HOTEL, agent=HOTEL_AGENT, pid="OAT"):
    return c.post("/quotes", json={"agent_id": agent, "mandate_id": mandate, "delivery_date": "2026-11-16",
                                   "offer_id": offer_id,
                                   "items": [{"product_ids": [pid], "amount": amount, "unit": "SALES_UNIT"}]},
                  headers=VALIO)


def events(kind):
    return [e for e in store.audit_events(limit=500) if e["event"] == kind]


def test_create_signed_offer_with_allergen_exclusion_and_delivery(c):
    r = offer(c)
    assert r.status_code == 200, r.text
    o = r.json()
    core = signing.verify(o["jws"], "sous-offer+jws")
    assert core["id"] == o["id"] and core["products"][0]["price_ex_vat"] == 0.8
    assert core["products"][0]["reference_price_ex_vat"] == 1.0 and core["products"][0]["discount_pct"] == 20.0
    # the hospital avoids milk: it gets the oat drink but never the cream, with the reason on Valio's side
    assert o["mandates"][HOTEL]["product_ids"] == ["OAT", "CREAM"]
    assert o["mandates"][HOSPITAL]["product_ids"] == ["OAT"]
    assert "allergen" in o["mandates"][HOSPITAL]["excluded_products"][0]["why"]
    hosp = c.get(f"/mandates/{HOSPITAL}/offers", headers=VALIO).json()
    assert [p["product_id"] for p in hosp["offers"][0]["products"]] == ["OAT"]
    assert "mandates" not in hosp["offers"][0] and "excluded" not in hosp["offers"][0]  # no other buyers shown
    assert hosp["offers"][0]["disclosure"] and hosp["offers"][0]["sponsored_by"] == "Valio Aimo"
    # audit: created once, offered per mandate; a notification in each buyer's feed
    assert len(events("offer.created")) == 1 and len(events("offer.offered")) == 2
    feed = c.get("/notifications", headers=VALIO).json()
    feed = feed.get("notifications", feed) if isinstance(feed, dict) else feed
    assert sum(n["event"] == "offer.received" for n in feed) == 2
    # a mandate the target doesn't name sees nothing
    assert c.get(f"/mandates/{SCHOOL}/offers", headers=VALIO).json()["offers"] == []


def test_only_milk_products_for_a_milk_avoider_means_no_target(c):
    r = offer(c, target={"mandate_ids": [HOSPITAL]}, product_ids=["CREAM"])
    assert r.status_code == 422 and r.json()["error"] == "offer_no_targets"


def test_price_above_list_and_auth_are_refused(c):
    r = offer(c, price_ex_vat=1.2)
    assert r.status_code == 422 and r.json()["error"] == "offer_above_list"
    assert offer(c, price_ex_vat=None, discount_pct=15).json()["products"][0]["price_ex_vat"] == 0.85
    body = {"product_ids": ["OAT"], "price_ex_vat": 0.5, "quantity_cap": 1, "valid_until": UNTIL,
            "reason": "seasonal", "reason_text": "x"}
    assert c.post("/offers", json=body).status_code == 401
    assert c.post("/offers", json=body, headers={"X-Approver-Code": "nope"}).status_code == 401


def test_rate_limit_per_buyer(c):
    for _ in range(offers.MAX_ACTIVE_PER_BUYER):
        assert offer(c, target={"mandate_ids": [HOTEL]}).status_code == 200
    r = offer(c, target={"mandate_ids": [HOTEL]})
    assert r.status_code == 422 and r.json()["error"] == "offer_no_targets" and "rate_limited" in r.json()["message"]
    # another buyer still gets offers
    assert offer(c, target={"mandate_ids": [SCHOOL]}).status_code == 200


def test_quote_and_order_on_an_offer_mandate_still_decides(c):
    o = offer(c).json()
    r = quote(c, o["id"])
    assert r.status_code == 200, r.text
    q = r.json()["quote"]
    line = q["lines"][0]
    assert line["unit_price_ex_vat"] == 0.8 and line["line_total_ex_vat"] == 8.0 and line["offer_id"] == o["id"]
    assert line["offer"]["live_price_ex_vat"] == 1.0 and q["offer"]["id"] == o["id"] and q["total_ex_vat"] == 8.0
    assert signing.verify(r.json()["jws"], "sous-quote+jws")["offer"]["id"] == o["id"]  # signed into the quote
    assert q["mandate_preview"]["outcome"] == "notify"
    assert "offer.applied" in [f["rule"] for f in q["mandate_preview"]["findings"]]
    od = c.post("/orders", json={"agent_id": HOTEL_AGENT, "mandate_id": HOTEL, "quote_id": q["id"]}).json()
    assert od["status"] == "ordered" and od["lines"][0]["offer_id"] == o["id"]
    assert [e["data"]["order_id"] for e in events("offer.accepted")] == [od["id"]]
    assert c.get(f"/mandates/{HOTEL}/offers", headers=VALIO).json()["offers"][0]["remaining"] == 20
    # over the cap: 20 left
    r = quote(c, o["id"], amount=25)
    assert r.status_code == 409 and r.json()["error"] == "offer_over_cap"
    # the hospital can't buy the cream (never offered to it); another buyer can't use the hotel's offer at all
    r = quote(c, o["id"], mandate=HOSPITAL, agent="meilahti-kitchen-agent", pid="CREAM")
    assert r.status_code == 422 and r.json()["error"] == "offer_not_applicable"
    r = quote(c, o["id"], mandate=SCHOOL, agent=SCHOOL_AGENT)
    assert r.status_code == 404 and r.json()["error"] == "offer_not_found"
    r = quote(c, "OF-NOPE")
    assert r.status_code == 404 and r.json()["error"] == "offer_not_found"


def test_an_offer_never_bypasses_limits(c, monkeypatch):
    o = offer(c, quantity_cap=10000).json()
    q = quote(c, o["id"], amount=5000).json()["quote"]  # €4,000 > the hotel's €2,500 auto limit
    rules_fired = [f["rule"] for f in q["mandate_preview"]["findings"]]
    assert q["mandate_preview"]["outcome"] == "ask" and "spend.auto_limit" in rules_fired and "offer.applied" in rules_fired


def test_expired_and_withdrawn_offers(c, monkeypatch):
    o = offer(c).json()
    q = quote(c, o["id"]).json()["quote"]
    assert c.delete(f"/offers/{o['id']}", headers=VALIO).json()["status"] == "withdrawn"
    od = c.post("/orders", json={"agent_id": HOTEL_AGENT, "mandate_id": HOTEL, "quote_id": q["id"]}).json()
    assert od["status"] == "refused" and "offer.expired" in [f["rule"] for f in od["decision"]["findings"]]
    r = quote(c, o["id"])
    assert r.status_code == 409 and r.json()["error"] == "offer_expired"
    o2 = offer(c).json()
    monkeypatch.setattr(S, "_now", lambda: NOW + timedelta(days=3))
    assert c.get(f"/mandates/{HOTEL}/offers", headers=VALIO).json()["offers"] == []
    assert [e["subject"] for e in events("offer.expired")] == [o2["id"]]
    r = quote(c, o2["id"])
    assert r.status_code == 409 and r.json()["error"] == "offer_expired"


def test_rules_are_registered():
    ids = {r["id"] for r in rules.register()}
    assert {"offer.applied", "offer.expired", "offer.over_cap", "offer_not_found", "offer_expired",
            "offer_over_cap", "offer_not_applicable", "offer_above_list", "offer_no_targets"} <= ids


def test_sandbox_seed_offer_and_sandbox_scope(c):
    s = new(c)
    sid, hotel = s["sandbox"]["id"], next(m for m in s["mandates"] if m["scenario"] == "hotel")
    seeded = s["seeded"]["offer"]
    assert seeded["mandate_id"] == hotel["mandate_id"] and seeded["product_id"] == "464194"
    sb = {"X-Approver-Code": s["approver_token"]}
    got = c.get(f"/mandates/{hotel['mandate_id']}/offers",
                headers={"Authorization": f"Bearer {s['agent_token']}"}).json()["offers"]
    assert [x["id"] for x in got] == [seeded["offer_id"]] and got[0]["seeded"]
    assert got[0]["products"][0]["reference_price_ex_vat"] == 4.2
    # its sbxa_ token makes offers for its own buyers only
    body = {"product_ids": ["OAT"], "price_ex_vat": 0.5, "quantity_cap": 5, "valid_until": UNTIL,
            "reason": "new_product", "reason_text": "new lactose-free oat drink"}
    r = c.post("/offers", json=body, headers=sb)
    assert r.status_code == 200, r.text
    assert set(r.json()["mandates"]) == {m["mandate_id"] for m in s["mandates"]} and r.json()["sandbox"] == sid
    r = c.post("/offers", json={**body, "target": {"mandate_ids": [HOTEL]}}, headers=sb)
    assert r.status_code == 422 and r.json()["error"] == "offer_no_targets"
    assert {o["sandbox"] for o in c.get("/offers", headers=sb).json()["offers"]} == {sid}
    # curated views never list sandbox offers
    assert c.get(f"/mandates/{HOTEL}/offers", headers=VALIO).json()["offers"] == []
    assert c.post("/offers", json=body, headers={"Authorization": f"Bearer {s['agent_token']}"}).status_code == 401


def test_mcp_list_offers_structured(c):
    o = offer(c).json()

    async def go():
        async with Client(mcp) as cl:
            tools = {t.name: t for t in (await cl.list_tools()).tools}
            res = await cl.call_tool("list_offers", {"mandate_id": HOTEL})
            return tools["list_offers"], res

    tool, res = asyncio.run(go())
    assert tool.annotations.read_only_hint is True
    assert not res.is_error, res.content[0].text
    validator_for(tool.output_schema)(tool.output_schema).validate(res.structured_content)
    assert json.loads(res.content[0].text) == res.structured_content
    assert res.structured_content["offers"][0]["id"] == o["id"]


@pytest.mark.parametrize("inc", [1.14, 0.5])  # 0.5 < ex: no usable tax-inclusive price
def test_missing_vat_rate_is_derived_or_the_offer_line_refused_never_zero(c, monkeypatch, inc):
    """QA blocker (rene-codex-qa 11:49Z): no taxRate from Valio must not mean 0 % VAT on the offer price."""
    from sous import quote as Q
    live = Q.offers

    def no_rate(*a, **k):
        offs, now = live(*a, **k)
        for x in offs:
            x["vat_rate"], x["unit_price_inc_vat"] = None, inc
        return offs, now

    monkeypatch.setattr(Q, "offers", no_rate)
    o = offer(c).json()
    q = quote(c, o["id"]).json()["quote"]
    line = q["lines"][0]
    if inc > 1:  # derived from Valio's €1.00 ex / €1.14 inc: 14 %
        assert line["vat_rate"] == 14.0 and "derived" in line["vat_rate_source"]
        assert line["unit_price_ex_vat"] == 0.8 and line["line_total_inc_vat"] == 9.12 > line["line_total_ex_vat"]
    else:  # nothing to derive it from: the offer line is refused with the reason
        assert not line["orderable"] and "OFFER_VAT_UNKNOWN" in [i["code"] for i in line["issues"]]
        assert line.get("offer_id") is None and q["mandate_preview"]["outcome"] == "block"
        assert "availability.offer_vat_unknown" in [f["rule"] for f in q["mandate_preview"]["findings"]]
