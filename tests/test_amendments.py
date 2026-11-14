"""Post-order exceptions as signed amendments (sous/amendments.py): short pick, delay, substitution at pick,
cancelled line; each signed, audited, re-checked against the mandate as it stands, with the agent unable to
accept what needs a human, the receipt re-signed with the new total and credit note lines, and one ERP change
message per amendment.

Offline: the catalog and Valio's price/availability (quote.offers) are replaced; every product costs EUR 1.00.
"""

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from jsonschema.validators import validator_for

from sous import amendments as AM, catalog, config, erp, quote as Q, service as S, signing, store
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
MON = date(2026, 11, 16)


def _product(pid, name, contains):
    return {
        "id": pid, "name": name, "names": {"fi": name, "en": name, "sv": ""}, "brand": "", "vendor": "",
        "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
        "allowed_lot_size": 1.0, "temperature": "chilled", "labels": [], "claims": [],
        "allergens": {"declared": True, "contains": contains, "may_contain": [],
                      "free_from": [] if contains else ["AM"]},
        "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
        "official_substitutes": [], "deleted": False,
    }


PRODUCTS = {"OAT": _product("OAT", "Oat drink 1 l", []), "OAT2": _product("OAT2", "Oat drink barista 1 l", []),
            "CREAM": _product("CREAM", "Cream 1 l", ["AM"])}


def fake_offers(product_ids, requested, need=None, need_unit=None):
    pr = {"catalogPrice": {"value": 1.0, "valueWithTax": 1.14, "validTo": None}, "comparisonPrice": {"value": 1.0},
          "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}
    av = {"logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": False,
          "scheduleLines": [{"salesBlock": False, "availableAmount": 99999, "availableDate": "2026-11-16",
                             "salesUnit": "ST"}]}
    return [Q.build_offer(PRODUCTS[p], pr, av, requested, need, need_unit, NOW) for p in product_ids], NOW


SENT: list[tuple[str, dict]] = []


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(config, "REQUIRE_AGENT_AUTH", False)
    monkeypatch.setattr(config, "APPROVER_CODE", "")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(erp, "_ready_for", None)
    monkeypatch.setattr(erp, "ERP_URL", "")
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(pid))
    monkeypatch.setattr(Q, "offers", fake_offers)
    monkeypatch.setattr(S, "_now", lambda: NOW)
    SENT.clear()
    monkeypatch.setattr(S.notify, "send", lambda ev, o, m: SENT.append((ev, o)))
    from sous import api
    api._hits.clear()
    yield
    signing._key.cache_clear()


def issue(**kw):
    return S.issue_mandate(Mandate(**{"id": "M", "buyer": "Cafe", "agent_id": "a", "issued_by": "owner",
                                      "auto_approve_limit_eur": 5000, "min_delivery_reliability": None, **kw}))


def order(amount=10, pid="OAT"):
    q = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=MON,
                                       items=[S.QuoteItem(product_ids=[pid], amount=amount, unit="SALES_UNIT")]))
    o = S.place_order("a", "M", q["quote"]["id"])
    assert o["status"] == "ordered", o["decision"]
    return o


def report(oid, **kw):
    return AM.report(oid, AM.AmendmentIn(**{"reason": "warehouse", **kw}), "global approver code")


def last(o):
    return o["amendments"][-1]


def events(name):
    return [e for e in store.audit_events(limit=1000) if e["event"] == name]


def receipt(o):
    r = signing.verify(o["receipt_jws"], "sous-order+jws")
    assert r is not None
    return r


def signed_ok(o):
    assert all(v["valid"] for v in AM.verify(store.get_order(o["id"]))), AM.verify(store.get_order(o["id"]))
    assert store.verify_chain()["ok"]


# --- each kind -------------------------------------------------------------------------------------------------

def test_short_pick_is_notify_reduces_total_and_receipt_carries_the_credit():
    issue()
    o = order(10)
    o = report(o["id"], kind="short_pick", line=0, delivered_quantity=6)
    a = last(o)
    assert (a["kind"], a["tier"], a["status"]) == ("short_pick", "notify", "applied")
    assert [f["rule"] for f in a["findings"]] == ["amendment.short_pick"]
    assert o["status"] == "ordered" and o["total_ex_vat"] == 6.0 and o["original_total_ex_vat"] == 10.0
    assert o["lines"][0]["quantity"] == 6 and o["lines"][0]["ordered_quantity"] == 10
    r = receipt(o)
    assert r["total_ex_vat"] == 6.0 and r["original_total_ex_vat"] == 10.0
    assert r["credit_notes"] == [{"amendment_id": a["id"], "line": 0, "product_id": "OAT", "name": "Oat drink 1 l",
                                  "quantity": -4, "amount_ex_vat": -4.0, "reason": "warehouse"}]
    assert r["amendments"][0]["id"] == a["id"] and r["amendments"][0]["status"] == "applied"
    assert signing.verify(a["jws"], "sous-amendment+jws")["status"] == "applied"
    assert store.spent_today("M", NOW.date().isoformat()) == 6.0  # the short-picked units aren't paid for
    assert [e for e, _ in SENT if e.startswith("order.amend")] == ["order.amended"]
    assert events("order.amended")[0]["data"]["amendment_id"] == a["id"]
    signed_ok(o)


def test_cancel_line_credits_the_whole_line():
    issue()
    o = order(4)
    o = report(o["id"], kind="cancel_line", line=0)
    assert last(o)["status"] == "applied" and o["total_ex_vat"] == 0.0 and o["lines"][0]["cancelled"]
    assert receipt(o)["credit_notes"][0]["amount_ex_vat"] == -4.0
    with pytest.raises(S.GatewayError) as e:
        report(o["id"], kind="short_pick", line=0, delivered_quantity=0)
    assert e.value.code == "line_cancelled"
    signed_ok(o)


def test_delay_within_mandate_is_applied_and_beyond_waits_for_the_buyer():
    issue(later_delivery="notify")
    o = report(order()["id"], kind="delay", new_delivery_date="2026-11-18")
    assert last(o)["status"] == "applied" and last(o)["findings"][0]["rule"] == "amendment.delay"
    assert o["delivery_date"] == "2026-11-18" and o["lines"][0]["delivery_date"] == "2026-11-18"
    issue(later_delivery="ask")
    o = report(order()["id"], kind="delay", new_delivery_date="2026-11-19")
    a = last(o)
    assert (a["tier"], a["status"]) == ("ask", "pending_buyer")
    assert a["findings"][0]["rule"] == "amendment.delay_beyond_mandate"
    assert o["delivery_date"] == "2026-11-16" and o["total_ex_vat"] == 10.0  # nothing changes until accepted
    assert SENT[-1][0] == "order.amendment_pending"
    o = AM.resolve(o["id"], a["id"], True, AM.Resolution(by="owner"), authority="global approver code", agent=False)
    assert last(o)["status"] == "accepted" and o["delivery_date"] == "2026-11-19"
    assert last(o)["resolution"]["by"] == "owner" and events("order.amendment_accepted")
    signed_ok(o)


def test_rejecting_a_delay_cancels_the_lines_with_a_credit():
    issue(later_delivery="ask")
    o = report(order(3)["id"], kind="delay", new_delivery_date="2026-11-20")
    o = AM.resolve(o["id"], last(o)["id"], False, AM.Resolution(by="owner", note="too late"),
                   authority="global approver code", agent=False)
    assert last(o)["status"] == "rejected" and o["total_ex_vat"] == 0.0 and o["delivery_date"] == "2026-11-16"
    assert receipt(o)["credit_notes"][0]["amount_ex_vat"] == -3.0
    signed_ok(o)


def test_substitution_allowed_by_the_mandate_replaces_the_line():
    issue(substitutions="notify")
    o = report(order(5)["id"], kind="substitution_at_pick", line=0, product_id="OAT2", quantity=5)
    a, l = last(o), o["lines"][0]
    assert (a["tier"], a["status"]) == ("notify", "applied")
    assert l["product_id"] == "OAT2" and l["substituted_for"]["product_id"] == "OAT" and o["total_ex_vat"] == 5.0
    signed_ok(o)


def test_substitution_needing_a_human_waits_and_a_dearer_one_asks():
    issue()  # substitutions: ask (default)
    o = report(order(5)["id"], kind="substitution_at_pick", line=0, product_id="OAT2", quantity=5)
    a = last(o)
    assert (a["tier"], a["status"]) == ("ask", "pending_buyer")
    assert {"amendment.substitution_not_allowed", "substitution"} <= {f["rule"] for f in a["findings"]}
    issue(substitutions="notify")
    o2 = report(order(5)["id"], kind="substitution_at_pick", line=0, product_id="OAT2", quantity=5,
                unit_price_ex_vat=1.5)
    assert (last(o2)["tier"], last(o2)["status"]) == ("ask", "pending_buyer")
    assert [f["rule"] for f in last(o2)["findings"] if f["outcome"] == "ask"] == ["amendment.price_increase"]


def test_blocked_substitution_cannot_be_accepted_only_rejected():
    issue(avoid_allergens=["AM"], substitutions="notify")
    o = report(order(5)["id"], kind="substitution_at_pick", line=0, product_id="CREAM", quantity=5)
    a = last(o)
    assert (a["tier"], a["status"]) == ("block", "pending_buyer")
    assert "allergen.contains" in {f["rule"] for f in a["findings"]}
    with pytest.raises(S.GatewayError) as e:
        AM.resolve(o["id"], a["id"], True, AM.Resolution(by="owner"), authority="global approver code", agent=False)
    assert e.value.code == "amendment_blocked"
    o = AM.resolve(o["id"], a["id"], False, AM.Resolution(by="owner"), authority="global approver code", agent=False)
    assert o["lines"][0]["product_id"] == "OAT" and o["lines"][0]["cancelled"] and o["total_ex_vat"] == 0.0
    signed_ok(o)


# --- the agent and the mandate as it stands --------------------------------------------------------------------

def test_agent_cannot_accept_an_ask_tier_amendment_but_can_once_the_mandate_allows_it():
    from sous import api
    issue(later_delivery="ask")
    token = S.agent_credential("M", "owner")["agent_credential"]["token"]
    o = order()
    c = TestClient(api.app)
    r = c.post(f"/orders/{o['id']}/amendments", json={"kind": "delay", "new_delivery_date": "2026-11-19",
                                                      "reason": "truck"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403 and r.json()["error"] == "agent_cannot_amend"
    r = c.post(f"/orders/{o['id']}/amendments", json={"kind": "delay", "new_delivery_date": "2026-11-19",
                                                      "reason": "truck"})
    assert r.status_code == 200, r.text
    aid = last(r.json())["id"]
    bearer = {"Authorization": f"Bearer {token}"}
    r = c.post(f"/orders/{o['id']}/amendments/{aid}/accept", json={"by": "agent"}, headers=bearer)
    assert r.status_code == 403 and r.json()["error"] == "agent_cannot_accept"
    assert events("agent.amendment_accept_refused")[0]["data"]["amendment_id"] == aid
    assert last(store.get_order(o["id"]))["status"] == "pending_buyer"
    r = c.post(f"/orders/{o['id']}/amendments/{aid}/accept", json={"by": "agent"},
               headers={"Authorization": "Bearer vka_wrong"})
    assert r.status_code == 403 and r.json()["error"] == "agent_auth_invalid"
    issue(later_delivery="notify")  # the human loosens the mandate: re-checked at accept time
    r = c.post(f"/orders/{o['id']}/amendments/{aid}/accept", json={"by": "agent"}, headers=bearer)
    assert r.status_code == 200, r.text
    a = last(r.json())
    assert a["status"] == "accepted" and a["resolution"]["actor_type"] == "agent"
    assert a["resolution"]["authority"].startswith("agent credential ...")
    signed_ok(r.json())


def test_rest_needs_the_approver_code_and_get_order_shows_amendments(monkeypatch):
    from sous import api, mcp_server
    monkeypatch.setattr(config, "APPROVER_CODE", "secret-code")
    issue()
    o = order()
    c = TestClient(api.app)
    body = {"kind": "short_pick", "line": 0, "delivered_quantity": 7, "reason": "short"}
    assert c.post(f"/orders/{o['id']}/amendments", json=body).status_code == 401
    r = c.post(f"/orders/{o['id']}/amendments", json=body, headers={"X-Approver-Code": "secret-code"})
    assert r.status_code == 200, r.text
    g = c.get(f"/orders/{o['id']}", headers={"X-Approver-Code": "secret-code"}).json()
    assert g["amendments"][0]["status"] == "applied" and g["total_ex_vat"] == 7.0
    assert g["amendments"][0]["erp"]["status"] == "confirmed"  # the change message went to the stub ERP
    schema = mcp_server.output_schema(mcp_server.OrderOut)
    validator_for(schema)(schema).validate(g)  # MCP get_order's structured output stays valid


def test_invalid_amendments_are_refused():
    issue(auto_approve_limit_eur=5)
    q = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=MON,
                                       items=[S.QuoteItem(product_ids=["OAT"], amount=10, unit="SALES_UNIT")]))
    held = S.place_order("a", "M", q["quote"]["id"])
    assert held["status"] == "pending_approval"
    for kw, code in [({"kind": "cancel_line", "line": 0}, "not_ordered")]:
        with pytest.raises(S.GatewayError) as e:
            report(held["id"], **kw)
        assert e.value.code == code
    o = order(3)
    for kw, code in [({"kind": "short_pick", "line": 0, "delivered_quantity": 3}, "invalid_argument"),
                     ({"kind": "short_pick", "line": 5, "delivered_quantity": 1}, "invalid_argument"),
                     ({"kind": "short_pick", "line": 0}, "invalid_argument"),
                     ({"kind": "delay", "new_delivery_date": "2026-11-16"}, "invalid_argument"),
                     ({"kind": "substitution_at_pick", "line": 0, "product_id": "NOPE", "quantity": 1},
                      "product_not_found")]:
        with pytest.raises(S.GatewayError) as e:
            report(o["id"], **kw)
        assert e.value.code == code, kw
    report(o["id"], kind="delay", new_delivery_date="2026-11-19")  # ask (default later_delivery)
    with pytest.raises(S.GatewayError) as e:
        report(o["id"], kind="cancel_line", line=0)
    assert e.value.code == "amendment_pending"


def test_erp_change_message_is_idempotent_and_orderchg_shaped():
    issue()
    o = report(order(10)["id"], kind="short_pick", line=0, delivered_quantity=8)
    a = last(o)
    key = erp.change_key(o["id"], a["id"])
    doc = erp.document(key)
    p = doc["payload"]
    assert p["EDI_DC40"]["MESTYP"] == "ORDCHG" and p["E1EDK01"]["ACTION"] == "002"
    assert p["E1EDK01"]["BELNR"] == o["id"]
    assert p["E1EDP01"][0]["ACTION"] == "002" and p["E1EDP01"][0]["MENGE"] == "8"
    assert erp.enqueue_change(store.get_order(o["id"]), a)["payload_hash"] == doc["erp"]["payload_hash"]
    assert erp.document(o["id"])["payload"]["E1EDP01"][0]["MENGE"] == "10"  # the original stays as it was sent
