"""Approve and remember (sous/remembered.py): a human answers a soft, repetitive hold once; hard rules never move."""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from sous import api, catalog, config, quote as Q, remembered, rules as R, service as S, signing, store
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
PRICE = {"catalogPrice": {"value": 1.4, "valueWithTax": 1.59, "validTo": "2026-12-31T00:00:00+0200"},
         "comparisonPrice": {"value": 1.4}, "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}
AVAIL = {"logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": False,
         "scheduleLines": [{"salesBlock": False, "availableAmount": 5000, "availableDate": "2026-11-16",
                            "salesUnit": "ST"}]}
PRODUCTS = {}
H = {"x-approver-code": "c0de"}


def product(pid, name):
    return {"id": pid, "name": name, "names": {"fi": "", "en": "", "sv": ""}, "brand": "", "vendor": "",
            "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
            "allowed_lot_size": 0.01, "temperature": "chilled", "labels": [], "claims": [],
            "allergens": {"declared": True, "contains": [], "may_contain": [], "free_from": []},
            "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": 1.4,
            "official_substitutes": [], "deleted": False}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(config, "APPROVER_CODE", "c0de")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    PRODUCTS.clear()
    PRODUCTS["1"] = product("1", "Oat drink 1 l")
    PRODUCTS["9"] = product("9", "Milk 1 l")
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(pid))
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(remembered, "_now", lambda: NOW)
    monkeypatch.setattr(S.allergen_ai, "assess_lines", lambda *a, **k: {})
    yield
    signing._key.cache_clear()


def quote(qid, need=10, issue=None):
    line = Q.build_offer(PRODUCTS["1"], PRICE, AVAIL, date(2026, 11, 16), need, "L", NOW)
    line["substitute_for"] = "9"
    if issue:
        line["issues"] = line["issues"] + [issue]
    q = {"id": qid, "agent_id": "a", "mandate_id": "M", "issued_at": NOW.isoformat(),
         "expires_at": "2026-11-13T23:00:00+02:00", "requested_delivery_date": "2026-11-16", "currency": "EUR",
         "lines": [line], "total_ex_vat": line["line_total_ex_vat"], "total_inc_vat": line["line_total_inc_vat"],
         "uncovered": [], "alternatives_considered": []}
    store.put_quote(q, signing.sign(q, "sous-quote+jws"))
    return qid


def mandate(**kw):
    S.issue_mandate(Mandate(**{"id": "M", "buyer": "Kitchen", "agent_id": "a", "issued_by": "chef", **kw}))


def approve(c, oid, remember, headers=H):
    return c.post(f"/orders/{oid}/approve", headers=headers, json={"by": "Mikko", "remember": remember})


def rules(o):
    return {f["rule"]: f["outcome"] for f in o["decision"]["findings"]}


SUB = [{"rule": "substitution", "product_id": "1"}]


def test_remembered_substitution_turns_ask_into_notify_and_is_signed():
    mandate()
    c = TestClient(api.app)
    o1 = S.place_order("a", "M", quote("Q-1"))
    assert o1["status"] == "pending_approval" and rules(o1)["substitution"] == "ask"
    r = approve(c, o1["id"], SUB).json()
    assert r["status"] == "ordered" and r["remembered"][0]["rule"] == "substitution"

    o2 = S.place_order("a", "M", quote("Q-2"))
    assert o2["status"] == "ordered", rules(o2)
    assert rules(o2).get("remembered.substitution") == "notify" and "substitution" not in rules(o2)
    msg = next(f for f in o2["decision"]["findings"] if f["rule"] == "remembered.substitution")["message"]
    assert "Previously approved by Mikko on 13.11." in msg and "until 13.12." in msg

    listed = c.get("/mandates/M/remembered", headers=H).json()["remembered"]
    assert len(listed) == 1 and listed[0]["authority"] == "global approver code"
    body = signing.verify(listed[0]["jws"], remembered.TYP)
    assert body and body["product_id"] == "1" and body["original_product_id"] == "9" and body["by"] == "Mikko"
    assert any(e["event"] == "remembered.recorded" and e["actor_type"] == "human" for e in store.audit_events(o1["id"]))
    # rule ids registered, with Finnish
    for rid in remembered.REMEMBERABLE.values():
        assert R.get(rid)["text_fi"]


def test_hard_rules_are_never_softened():
    mandate()
    c = TestClient(api.app)
    approve(c, S.place_order("a", "M", quote("Q-1"))["id"], SUB)
    # a hard limit still blocks
    mandate(hard_limit_eur=5.0, auto_approve_limit_eur=1.0)
    o = S.place_order("a", "M", quote("Q-2"))
    assert o["status"] != "ordered" and rules(o)["spend.hard_limit"] == "block"
    # a daily limit still asks
    mandate(daily_limit_eur=5.0)
    o = S.place_order("a", "M", quote("Q-3"))
    assert o["status"] == "pending_approval" and rules(o)["spend.daily_limit"] == "ask"
    assert rules(o)["remembered.substitution"] == "notify"
    # substitutions: block is never softened
    mandate(substitutions="block")
    assert rules(S.place_order("a", "M", quote("Q-4")))["substitution"] == "block"
    # an allergen finding of the same product is untouched
    PRODUCTS["1"]["allergens"] = {"declared": False, "contains": [], "may_contain": [], "free_from": []}
    mandate(avoid_allergens=["AM"], unknown_allergens="ask")
    o = S.place_order("a", "M", quote("Q-5"))
    assert rules(o)["allergen.unknown"] == "ask" and o["status"] == "pending_approval"
    # only remembered rules can be picked: allergen.unknown is refused and nothing is decided
    bad = approve(c, o["id"], [{"rule": "allergen.unknown", "product_id": "1"}])
    assert bad.status_code == 422 and S.get_order(o["id"])["status"] == "pending_approval"


def test_expiry_cap_product_change_and_revocation_each_restore_ask(monkeypatch):
    mandate()
    c = TestClient(api.app)
    r = approve(c, S.place_order("a", "M", quote("Q-1"))["id"],
                [{"rule": "substitution", "product_id": "1", "days": 7, "max_quantity": 12}]).json()
    rid = r["remembered"][0]["id"]
    assert S.place_order("a", "M", quote("Q-2"))["status"] == "ordered"
    # over the quantity cap
    assert rules(S.place_order("a", "M", quote("Q-3", need=20)))["substitution"] == "ask"
    # the product data changed (either product of the pair)
    PRODUCTS["9"] = product("9", "Milk 1 l NEW")
    assert rules(S.place_order("a", "M", quote("Q-4")))["substitution"] == "ask"
    PRODUCTS["9"] = product("9", "Milk 1 l")
    assert S.place_order("a", "M", quote("Q-5"))["status"] == "ordered"
    # expiry
    later = NOW + timedelta(days=8)
    monkeypatch.setattr(remembered, "_now", lambda: later)
    assert remembered.for_lines(S._mandate("M"), [{"product_id": "1"}]) == []
    monkeypatch.setattr(remembered, "_now", lambda: NOW)
    # revocation: approver only
    assert c.delete(f"/mandates/M/remembered/{rid}").status_code == 401
    assert c.delete(f"/mandates/OTHER/remembered/{rid}", headers=H).status_code == 404
    assert c.delete(f"/mandates/M/remembered/{rid}", headers=H).json()["active"] is False
    assert rules(S.place_order("a", "M", quote("Q-6")))["substitution"] == "ask"
    assert c.get("/mandates/M/remembered", headers=H).json()["remembered"] == []
    assert len(c.get("/mandates/M/remembered?include_inactive=true", headers=H).json()["remembered"]) == 1


def test_tampered_row_and_unit_reading():
    mandate()
    c = TestClient(api.app)
    issue = {"code": "UNIT_INTERPRETED", "severity": "warning",
             "message": "ST is not a unit of this product; read as 20 KI. A human confirms the quantity."}
    o = S.place_order("a", "M", quote("Q-1", issue=issue))
    assert rules(o)["unit.interpreted"] == "ask"
    r = approve(c, o["id"], SUB + [{"rule": "unit.interpreted", "product_id": "1"}]).json()
    assert {x["rule"] for x in r["remembered"]} == {"substitution", "unit.interpreted"}
    # same reading, other numbers: remembered
    o2 = S.place_order("a", "M", quote("Q-2", issue={**issue, "message": issue["message"].replace("20", "30")}))
    assert rules(o2)["remembered.unit_interpreted"] == "notify" and o2["status"] == "ordered"
    # another reading: asks again
    other = {**issue, "message": "40 kg read as 38.8 L of cream at 1.03 kg/L. A human confirms the quantity."}
    assert rules(S.place_order("a", "M", quote("Q-3", issue=other)))["unit.interpreted"] == "ask"
    # a row changed in the database without the gateway's signature no longer applies
    row = next(x for x in remembered.list_for("M") if x["rule"] == "substitution")
    remembered._put({**row, "max_quantity": 1000.0, "original_product_id": "9"} | {"expires_at": "2027-12-31T00:00:00+02:00"})
    assert rules(S.place_order("a", "M", quote("Q-4")))["substitution"] == "ask"


def test_agent_tokens_cannot_remember():
    mandate()
    c = TestClient(api.app)
    o = S.place_order("a", "M", quote("Q-1"))
    for h in ({"authorization": "Bearer vka_abcdefabcdef"}, {"x-approver-code": "vka_abcdefabcdef"}):
        assert approve(c, o["id"], SUB, headers=h).status_code == 403
    assert S.get_order(o["id"])["status"] == "pending_approval" and remembered.list_for("M") == []
    # nor revoke: an agent credential is no approver code
    assert c.delete("/mandates/M/remembered/REM-X", headers={"x-approver-code": "vka_abcdefabcdef"}).status_code == 401


def test_mcp_mandate_outputs_list_remembered_and_stay_schema_valid(monkeypatch):
    from jsonschema.validators import validator_for

    from sous import access, mcp_server as MS
    mandate()
    c = TestClient(api.app)
    approve(c, S.place_order("a", "M", quote("Q-1"))["id"], SUB)
    monkeypatch.setattr(access, "check_mandate", lambda *a, **k: None)
    monkeypatch.setattr(MS, "_own_mandates", lambda ctx, tok: {"M"})
    got = MS.get_mandate.__wrapped__("M") if hasattr(MS.get_mandate, "__wrapped__") else MS.get_mandate("M")
    got = getattr(got, "structured_content", None) or got
    schema = MS.output_schema(MS.MandateOut)
    validator_for(schema)(schema).validate(got)
    assert got["remembered"][0]["rule"] == "substitution" and got["remembered"][0]["jws"]
    mine = MS.my_mandates()
    mine = getattr(mine, "structured_content", None) or mine
    schema = MS.output_schema(MS.MyMandatesOut)
    validator_for(schema)(schema).validate(mine)
    assert mine["mandates"][0]["remembered"][0]["by"] == "Mikko"
