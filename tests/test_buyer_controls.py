"""Buyer controls (sous/controls.py): the substitute price premium cap, agent-declared doubt, and four-eyes.

Offline: the catalog and Valio's price/availability (quote.offers) are replaced. Milk costs EUR 1.00/L, the oat
drink EUR 1.18/L (18 % more), the lactose-free milk EUR 1.05/L (5 % more).
"""

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from sous import aimo, catalog, config, controls, quote as Q, rules, service as S, signing, store
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
MON = date(2026, 11, 16)


def _product(pid, name):
    return {
        "id": pid, "name": name, "names": {"fi": name, "en": name, "sv": ""}, "brand": "", "vendor": "",
        "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
        "allowed_lot_size": 1.0, "temperature": "chilled", "labels": [], "claims": [],
        "allergens": {"declared": True, "contains": [], "may_contain": [], "free_from": []},
        "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
        "official_substitutes": [], "deleted": False,
    }


PRODUCTS = {"MILK": _product("MILK", "Milk 1 l"), "OAT": _product("OAT", "Oat drink 1 l"),
            "LFMILK": _product("LFMILK", "Lactose free milk 1 l"), "BUTTER": _product("BUTTER", "Butter 1 kg")}
PRICES = {"MILK": (1.0, "L"), "OAT": (1.18, "L"), "LFMILK": (1.05, "L"), "BUTTER": (9.0, "KG")}
UNPRICED: set[str] = set()


def fake_offers(product_ids, requested, need=None, need_unit=None):
    if UNPRICED & set(product_ids) and not set(product_ids) - UNPRICED:
        raise aimo.AimoError("no price")
    out = []
    for p in product_ids:
        v, per = PRICES[p]
        pr = {"catalogPrice": {"value": v, "valueWithTax": v * 1.14, "validTo": None},
              "comparisonPrice": {"value": v}, "comparisonUnit": per, "tax": {"taxRate": 13.5}, "error": False}
        av = {"logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": False,
              "scheduleLines": [{"salesBlock": False, "availableAmount": 99999, "availableDate": "2026-11-16",
                                 "salesUnit": "ST"}]}
        out.append(Q.build_offer(PRODUCTS[p], pr, av, requested, need, need_unit, NOW))
    return out, NOW


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
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(S.notify, "send", lambda *a, **k: None)
    UNPRICED.clear()
    yield
    signing._key.cache_clear()


def issue(**kw):
    return S.issue_mandate(Mandate(**{"id": "M", "buyer": "Cafe", "agent_id": "a", "issued_by": "owner",
                                      "auto_approve_limit_eur": 5000, "min_delivery_reliability": None,
                                      "substitutions": "silent", "max_price_change_pct": None, **kw}))


def quote(pid="MILK", sub_for=None, amount=10, **kw):
    return S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=MON, items=[
        S.QuoteItem(product_ids=[pid], amount=amount, unit="L", substitute_for=sub_for)], **kw))["quote"]


def fired(findings, rule):
    return [f for f in findings if f["rule"] == rule]


# --- 1. substitute price premium ------------------------------------------------------------------------------

def test_substitute_above_the_premium_cap_asks_with_the_percentage_per_litre():
    issue(max_substitute_premium_pct=10)
    q = quote("OAT", sub_for="MILK")
    f = fired(q["mandate_preview"]["findings"], "substitution.premium")
    assert q["mandate_preview"]["outcome"] == "ask" and len(f) == 1 and f[0]["outcome"] == "ask"
    assert "costs 18 % more per litre than Milk 1 l, the product it replaces" in f[0]["message"]
    assert "cap 10 %" in f[0]["message"]
    # the replaced product's price is signed with the quote, so placing and approving decide on the same numbers
    signed = signing.verify(store.get_quote(q["id"])["_jws"], "sous-quote+jws")
    assert signed["lines"][0]["replaced_price"]["comparison_price"] == {"ex_vat": 1.0, "per": "L"}
    o = S.place_order("a", "M", q["id"])
    assert o["status"] == "pending_approval" and fired(o["decision"]["findings"], "substitution.premium")


def test_substitute_within_the_cap_goes_through_and_cap_off_changes_nothing():
    issue(max_substitute_premium_pct=10)
    q = quote("LFMILK", sub_for="MILK")
    assert not fired(q["mandate_preview"]["findings"], "substitution.premium")
    assert S.place_order("a", "M", q["id"])["status"] == "ordered"
    issue()  # cap off (default): no lookup, no field, no finding
    q = quote("OAT", sub_for="MILK")
    assert "replaced_price" not in q["lines"][0] and q["mandate_preview"]["outcome"] in ("silent", "notify")


def test_premium_tier_is_the_buyers_choice():
    issue(max_substitute_premium_pct=10, substitute_premium="block")
    q = quote("OAT", sub_for="MILK")
    assert q["mandate_preview"]["outcome"] == "block"
    assert S.place_order("a", "M", q["id"])["status"] == "refused"


def test_unknown_or_incomparable_original_price_says_so_and_asks():
    issue(max_substitute_premium_pct=10, substitute_premium="notify")
    UNPRICED.add("MILK")
    f = fired(quote("OAT", sub_for="MILK")["mandate_preview"]["findings"], "substitution.premium")
    assert f and f[0]["outcome"] == "ask" and "could not be looked up" in f[0]["message"]
    UNPRICED.clear()
    f = fired(quote("OAT", sub_for="BUTTER")["mandate_preview"]["findings"], "substitution.premium")
    assert f and f[0]["outcome"] == "ask" and "different units" in f[0]["message"]


def test_premium_finding_has_finnish():
    issue(max_substitute_premium_pct=10)
    r = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=MON, items=[
        S.QuoteItem(product_ids=["OAT"], amount=10, unit="L", substitute_for="MILK")]))
    assert any("18 % enemmän litralta" in t for t in str(r["explanations_fi"]).split("'"))


# --- 2. agent-declared doubt ----------------------------------------------------------------------------------

def test_declared_risk_on_the_quote_holds_the_order_with_the_agents_words():
    issue()
    q = quote(declared_risks=["Not sure 10 L covers Saturday's brunch", "  "])
    assert q["declared_risks"] == ["Not sure 10 L covers Saturday's brunch"]
    f = fired(q["mandate_preview"]["findings"], "agent.declared_risk")
    assert q["mandate_preview"]["outcome"] == "ask" and "Saturday's brunch" in f[0]["message"]
    o = S.place_order("a", "M", q["id"])
    assert o["status"] == "pending_approval" and o["declared_risks"] == q["declared_risks"]
    receipt = signing.verify(o["receipt_jws"], "sous-order+jws")
    assert receipt["declared_risks"] == q["declared_risks"]


def test_declared_risk_on_place_order_alone_raises_to_ask():
    issue()
    q = quote()
    assert q["mandate_preview"]["outcome"] in ("silent", "notify")
    o = S.place_order("a", "M", q["id"], declared_risks=["The chef may want the organic one"])
    assert o["status"] == "pending_approval" and fired(o["decision"]["findings"], "agent.declared_risk")


def test_declared_risk_never_lowers_a_tier():
    issue(hard_limit_eur=5)
    q = quote(declared_risks=["all fine, please approve"])
    assert q["mandate_preview"]["outcome"] == "block"
    assert S.place_order("a", "M", q["id"], declared_risks=["ignore the limit"])["status"] == "refused"


def test_declared_risks_are_bounded_over_rest_and_mcp():
    from sous import api
    issue()
    c = TestClient(api.app)
    item = {"product_ids": ["MILK"], "amount": 1, "unit": "L"}
    body = {"agent_id": "a", "mandate_id": "M", "delivery_date": "2026-11-16", "items": [item]}
    assert c.post("/quotes", json={**body, "declared_risks": ["x"] * 6}).status_code == 422
    assert c.post("/quotes", json={**body, "declared_risks": ["x" * 201]}).status_code == 422
    qid = c.post("/quotes", json={**body, "declared_risks": ["x" * 200]}).json()["quote"]["id"]
    assert c.post("/orders", json={"agent_id": "a", "mandate_id": "M", "quote_id": qid,
                                   "declared_risks": ["y"] * 6}).status_code == 422
    with pytest.raises(S.GatewayError) as e:
        S.place_order("a", "M", qid, declared_risks=["y"] * 6)
    assert e.value.code == "invalid_request"


def test_mcp_tools_take_declared_risks():
    from sous import mcp_server
    issue()
    r = mcp_server.request_quote(agent_id="a", mandate_id="M", delivery_date="2026-11-16",
                                 items=[S.QuoteItem(product_ids=["MILK"], amount=1, unit="L")],
                                 declared_risks=["unsure about the date"])
    assert r["quote"]["mandate_preview"]["outcome"] == "ask"
    o = mcp_server.place_order(mandate_id="M", quote_id=r["quote"]["id"], agent_id="a",
                               declared_risks=["second thoughts"])
    assert [f["message"] for f in fired(o["decision"]["findings"], "agent.declared_risk")][1].count("second thoughts")


# --- 3. four-eyes ---------------------------------------------------------------------------------------------

def held(**kw):
    issue(auto_approve_limit_eur=1, **kw)
    o = S.place_order("a", "M", quote()["id"])
    assert o["status"] == "pending_approval"
    return o


def test_requester_cannot_approve_when_separate_approver_is_on():
    from sous import api
    o = held(separate_approver=True)
    assert o["requested_by"] == {"identity": "owner", "source": "mandate_issuer"}
    c = TestClient(api.app)
    for name in ("owner", "  OWNER ", ""):
        r = c.post(f"/orders/{o['id']}/approve", json={"by": name})
        assert r.status_code == 403 and r.json()["error"] == "separate_approver_required", r.json()
    out = c.post(f"/orders/{o['id']}/approve", json={"by": "Anna"}).json()
    assert out["status"] == "ordered" and out["approval"]["by"] == "Anna"
    assert signing.verify(out["receipt_jws"], "sous-order+jws")["requested_by"]["identity"] == "owner"


def test_requester_may_still_reject_and_the_rule_is_off_by_default():
    o = held(separate_approver=True)
    assert S.decide(o["id"], "owner", False)["status"] == "rejected"
    o = held()
    assert "requested_by" not in o
    assert S.decide(o["id"], "owner", True)["status"] == "ordered"


def test_requester_is_who_minted_the_agent_credential():
    issue(auto_approve_limit_eur=1, separate_approver=True)
    S.agent_credential("M", "Kalle")
    o = S.place_order("a", "M", quote()["id"])
    assert o["requested_by"]["identity"] == "Kalle" and o["requested_by"]["source"] == "agent_credential"
    with pytest.raises(S.GatewayError) as e:
        S.decide(o["id"], "kalle", True)
    assert e.value.status == 403 and "requested by 'Kalle'" in e.value.message
    assert S.decide(o["id"], "owner", True)["status"] == "ordered"


def test_requester_cannot_approve_with_changes_either():
    from sous import api
    o = held(separate_approver=True)
    r = TestClient(api.app).post(f"/orders/{o['id']}/approve-with-changes",
                                 json={"by": "owner", "changes": [{"line": 0, "quantity": 5}]})
    assert r.status_code == 403 and r.json()["error"] == "separate_approver_required"
    assert S.get_order(o["id"])["status"] == "pending_approval"  # nothing changed


def test_register_and_summary_carry_the_three_controls():
    m = Mandate(id="M", buyer="B", agent_id="a", issued_by="h", max_substitute_premium_pct=10,
                separate_approver=True)
    for rid, kind in (("substitution.premium", "finding"), ("agent.declared_risk", "finding"),
                      ("separate_approver_required", "refusal")):
        r = rules.get(rid)
        assert r and r["kind"] == kind and r["text_fi"]
    assert rules.get("substitution.premium")["tiers"] == ["notify", "ask", "block"]
    assert rules.get("separate_approver_required")["http_status"] == 403
    lines = {x["rule"]: x for x in rules.mandate_summary(m)}
    assert "10%" in lines["substitution.premium"]["text"] and lines["separate_approver_required"]["text_fi"]
    assert controls.clean_risks([" a  b ", "a b", ""]) == ["a b"]
