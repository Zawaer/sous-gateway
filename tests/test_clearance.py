"""Remembered allergen clearances (sous/clearance.py): a human clears an undeclared product once per buyer."""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from sous import api, catalog, clearance, config, quote as Q, service as S, signing, store
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
PRICE = {"catalogPrice": {"value": 1.4, "valueWithTax": 1.59, "validTo": "2026-12-31T00:00:00+0200"},
         "comparisonPrice": {"value": 1.4}, "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}
AVAIL = {"logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": False,
         "scheduleLines": [{"salesBlock": False, "availableAmount": 5000, "availableDate": "2026-11-16",
                            "salesUnit": "ST"}]}
PRODUCTS = {}


def product(pid="1", name="Oat drink 1 l"):
    return {"id": pid, "name": name, "names": {"fi": "", "en": "", "sv": ""}, "brand": "", "vendor": "",
            "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
            "allowed_lot_size": 0.01, "temperature": "chilled", "labels": [], "claims": [],
            "allergens": {"declared": False, "contains": [], "may_contain": [], "free_from": []},
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
    PRODUCTS["1"] = product()
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(pid))
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(clearance, "_now", lambda: NOW)
    monkeypatch.setattr(S.allergen_ai, "assess_lines", lambda *a, **k: {})
    yield
    signing._key.cache_clear()


def quote(qid):
    line = Q.build_offer(PRODUCTS["1"], PRICE, AVAIL, date(2026, 11, 16), 10, "L", NOW)
    q = {"id": qid, "agent_id": "a", "mandate_id": "M", "issued_at": NOW.isoformat(),
         "expires_at": "2026-11-13T23:00:00+02:00", "requested_delivery_date": "2026-11-16", "currency": "EUR",
         "lines": [line], "total_ex_vat": line["line_total_ex_vat"], "total_inc_vat": line["line_total_inc_vat"],
         "uncovered": [], "alternatives_considered": []}
    store.put_quote(q, signing.sign(q, "sous-quote+jws"))
    return qid


def mandate(**kw):
    S.issue_mandate(Mandate(**{"id": "M", "buyer": "Kitchen", "agent_id": "a", "issued_by": "chef",
                               "avoid_allergens": ["AM", "AN"], "unknown_allergens": "ask", **kw}))


def approve(c, oid, remember=True):
    return c.post(f"/orders/{oid}/approve", headers={"x-approver-code": "c0de"},
                  json={"by": "Mikko", "remember_allergen_clearance": remember}).json()


def rules(o):
    return {f["rule"] for f in o["decision"]["findings"]}


def test_remembered_clearance_skips_the_second_hold_and_is_narrow():
    mandate()
    c = TestClient(api.app)
    o1 = S.place_order("a", "M", quote("Q-1"))
    assert o1["status"] == "pending_approval" and "allergen.unknown" in rules(o1)
    r = approve(c, o1["id"])
    assert r["status"] == "ordered" and r["allergen_clearances"][0]["product_id"] == "1"

    o2 = S.place_order("a", "M", quote("Q-2"))
    assert o2["status"] == "ordered" and "allergen.cleared" in rules(o2) and "allergen.unknown" not in rules(o2)
    assert "Mikko confirmed it free of" in next(f for f in o2["decision"]["findings"] if f["rule"] == "allergen.cleared")["message"]

    # product data changes -> held again
    PRODUCTS["1"] = product(name="Oat drink 1 l NEW RECIPE")
    assert S.place_order("a", "M", quote("Q-3"))["status"] == "pending_approval"
    PRODUCTS["1"] = product()
    # the mandate now avoids more -> held again
    mandate(avoid_allergens=["AM", "AN", "AP"])
    assert S.place_order("a", "M", quote("Q-4"))["status"] == "pending_approval"


def test_no_clearance_without_the_tick_and_revoke_and_expiry(monkeypatch):
    mandate()
    c = TestClient(api.app)
    approve(c, S.place_order("a", "M", quote("Q-1"))["id"], remember=False)
    assert S.place_order("a", "M", quote("Q-2"))["status"] == "pending_approval"
    r = approve(c, S.place_order("a", "M", quote("Q-3"))["id"])
    cid = r["allergen_clearances"][0]["id"]
    assert [x["id"] for x in c.get("/clearances", headers={"x-approver-code": "c0de"}).json()["clearances"]] == [cid]
    assert c.delete(f"/clearances/{cid}", headers={"x-approver-code": "c0de"}).json()["active"] is False
    assert S.place_order("a", "M", quote("Q-4"))["status"] == "pending_approval"
    r = approve(c, S.place_order("a", "M", quote("Q-5"))["id"])
    later = NOW + timedelta(days=clearance.CLEARANCE_DAYS + 1)
    monkeypatch.setattr(S, "_now", lambda: later)
    monkeypatch.setattr(clearance, "_now", lambda: later)
    assert clearance.for_lines(S._mandate("M"), [{"product_id": "1"}]) == {}
    assert c.get("/clearances", headers={}).status_code == 401
