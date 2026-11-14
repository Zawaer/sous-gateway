"""Use-aware substitution (sous/use_sensitivity.py): whether a substitute is acceptable depends on what the kitchen
uses the product for. Whipping cream for a dessert: another brand can whip differently, so a person confirms. Cream
for coffee: any brand will do. Offline: synthetic products, Valio's price/availability built locally."""

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from sous import api, catalog, config, quote as Q, remembered, rules as R, service as S, signing, store
from sous import substitutes as SUB, use_sensitivity as U
from sous.mandate import Mandate, evaluate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
MON = date(2026, 11, 16)
PRICE = {"catalogPrice": {"value": 4.0, "valueWithTax": 4.54, "validTo": "2026-12-31T00:00:00+0200"},
         "comparisonPrice": {"value": 4.0}, "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}
AVAIL = {"logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": False,
         "scheduleLines": [{"salesBlock": False, "availableAmount": 5000, "availableDate": "2026-11-16",
                            "salesUnit": "ST"}]}
H = {"x-approver-code": "c0de"}


def product(pid, fi, brand):
    return {"id": pid, "name": fi, "names": {"fi": fi, "en": "", "sv": ""}, "brand": brand, "vendor": "",
            "category": "10500", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
            "allowed_lot_size": 1.0, "temperature": "chilled", "labels": [], "claims": [],
            "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": []},
            "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
            "official_substitutes": [], "deleted": False}


PRODUCTS = {
    "WA": product("WA", "Valio kuohukerma 38 % 1 l", "Valio"),
    "WB": product("WB", "Arla kuohukerma 38 % 1 l", "Arla"),
    "WC": product("WC", "Valio vispikerma 35 % 1 l", "Valio"),
    "CA": product("CA", "Valio kahvikerma 12 % 1 l", "Valio"),
    "CB": product("CB", "Arla kahvikerma 12 % 1 l", "Arla"),
    "MA": product("MA", "Valio kevytmaito 1 l", "Valio"),
    "MB": product("MB", "Arla täysmaito 1 l", "Arla"),
}


def fake_offers(product_ids, requested, need=None, need_unit=None):
    return [Q.build_offer(PRODUCTS[p], PRICE, AVAIL, requested, need, need_unit, NOW) for p in product_ids], NOW


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(config, "REQUIRE_AGENT_AUTH", False)
    monkeypatch.setattr(config, "APPROVER_CODE", "c0de")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(pid))
    monkeypatch.setattr(SUB, "_same_category", lambda cat: [p for p in PRODUCTS.values() if p["category"] == cat])
    monkeypatch.setattr(Q, "offers", fake_offers)
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(remembered, "_now", lambda: NOW)
    monkeypatch.setattr(S.allergen_ai, "assess_lines", lambda *a, **k: {})
    monkeypatch.setattr(S.notify, "send", lambda *a, **k: None)
    yield
    signing._key.cache_clear()


def mandate(**kw):
    S.issue_mandate(Mandate(**{"id": "M", "buyer": "Kitchen", "agent_id": "a", "issued_by": "chef",
                               "auto_approve_limit_eur": 5000, "min_delivery_reliability": None,
                               "substitutions": "silent", **kw}))


def quote(pid, original, use=None, amount=10):
    item = S.QuoteItem(product_ids=[pid], amount=amount, unit="L", substitute_for=original, use=use)
    return S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=MON, items=[item]))["quote"]


def rules_of(q):
    return {f["rule"]: f["outcome"] for f in q["mandate_preview"]["findings"]}


# --- the table, offline ------------------------------------------------------------------------------------------

def test_classify_uses():
    assert U.classify("dessert") == ["whipping"] and U.classify("coffee") == ["coffee"]
    assert U.classify("ingredient-in-recipe:Tiramisu") == ["whipping"]
    assert set(U.classify("ingredient-in-recipe:Lasagne")) == {"melting", "cooking"}
    assert U.classify(None) == [] and U.classify("unknown") == [] and U.classify("for the staff room") == []


def test_coffee_cream_any_brand_but_dessert_cream_same_product_only():
    coffee = U.judge(PRODUCTS["CA"], PRODUCTS["CB"], "coffee")
    assert coffee["ok"] and coffee["use_sensitivity"] == "type" and "brand barely matters" in coffee["reason"]
    dessert = U.judge(PRODUCTS["WA"], PRODUCTS["WB"], "dessert")
    assert not dessert["ok"] and dessert["use_sensitivity"] == "exact"
    assert "whip differently" in dessert["reason"] and "Arla instead of Valio" in dessert["reason"]
    assert "vaahtoutua eri tavalla" in dessert["reason_fi"] and "ihmisen pitää vahvistaa" in dessert["reason_fi"]
    assert U.judge(PRODUCTS["WA"], PRODUCTS["WA"], "dessert")["ok"]  # the same product is fine
    # cooking: any brand with the same fat %; 35 % for 38 % needs a person
    assert U.judge(PRODUCTS["WA"], PRODUCTS["WB"], "sauce")["ok"]
    assert "fat 35% vs 38%" in U.judge(PRODUCTS["WA"], PRODUCTS["WC"], "sauce")["reason"]
    # milk to drink: same fat % needed; milk for baking: any milk
    assert not U.judge(PRODUCTS["MA"], PRODUCTS["MB"], "drinking")["ok"]
    assert U.judge(PRODUCTS["MA"], PRODUCTS["MB"], "baking")["ok"]
    # not in the table, or no use: the ordinary rules (None)
    assert U.judge(PRODUCTS["CA"], PRODUCTS["CB"], "melting") is None
    assert U.judge(PRODUCTS["WA"], PRODUCTS["WB"], None) is None


def test_evaluate_asks_even_when_substitutes_are_silent():
    m = Mandate(id="M", buyer="K", agent_id="a", issued_by="c", substitutions="silent")
    line = fake_offers(["WB"], MON, 10, "L")[0][0] | {"use": "dessert"}
    tier, found = evaluate(m, "a", [line], requested_date=MON, substitute_for={"WB": "WA"}, now=NOW)
    by = {f.rule: f for f in found}
    assert tier == "ask" and by["substitution.use_sensitive"].outcome == "ask"
    assert by["substitution"].outcome == "silent"
    assert by["substitution.use_sensitive"].message_fi
    assert R.get("substitution.use_sensitive")["text_fi"]


def test_mandate_override_is_signed_and_used():
    with pytest.raises(ValueError):
        Mandate(id="M", buyer="K", agent_id="a", issued_by="c", substitution_by_use={"kuohukerma": {"x": "type"}})
    with pytest.raises(ValueError):
        Mandate(id="M", buyer="K", agent_id="a", issued_by="c", substitution_by_use={"kuohukerma": {"dessert": "any"}})
    mandate(substitution_by_use={"kuohukerma": {"dessert": "type"}, "kahvikerma": {"coffee": "exact"}})
    m = S._mandate("M")
    assert m.substitution_by_use == {"kuohukerma": {"whipping": "type"}, "kahvikerma": {"coffee": "exact"}}
    signed = signing.verify(store.get_mandate("M")["_jws"], "sous-mandate+jws")
    assert signed["substitution_by_use"]["kahvikerma"] == {"coffee": "exact"}
    assert "substitution.use_sensitive" not in rules_of(quote("WB", "WA", "dessert"))
    q = quote("CB", "CA", "coffee")
    assert rules_of(q)["substitution.use_sensitive"] == "ask"
    msg = next(f["message"] for f in q["mandate_preview"]["findings"] if f["rule"] == "substitution.use_sensitive")
    assert "the buyer's mandate says so" in msg
    assert any(x["rule"] == "substitution.use_sensitive" for x in R.mandate_summary(m))


# --- through the gateway ------------------------------------------------------------------------------------------

def test_quote_coffee_cream_substitute_goes_through():
    mandate()
    q = quote("CB", "CA", "coffee")
    assert q["mandate_preview"]["outcome"] == "silent", rules_of(q)
    assert q["lines"][0]["use"] == "coffee" and q["lines"][0]["use_source"] == "request"
    assert q["items"][0]["use"] == "coffee"
    sub = next(f for f in q["mandate_preview"]["findings"] if f["rule"] == "substitution")
    assert "brand barely matters" in sub["message"]


def test_quote_dessert_cream_other_brand_asks_and_same_product_is_fine():
    mandate()
    q = quote("WB", "WA", "dessert")
    assert q["mandate_preview"]["outcome"] == "ask" and rules_of(q)["substitution.use_sensitive"] == "ask"
    # the original itself for a dessert: no substitution, nothing to ask
    plain = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=MON, items=[
        S.QuoteItem(product_ids=["WA"], amount=10, unit="L", use="dessert")]))["quote"]
    assert plain["mandate_preview"]["outcome"] == "silent"
    # without a use the signed items keep their earlier shape (no use key)
    assert "use" not in quote("CB", "CA")["items"][0]


def test_use_inferred_from_history():
    mandate()
    # an earlier order stated the use of the original product
    first = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=MON, items=[
        S.QuoteItem(product_ids=["WA"], amount=10, unit="L", use="ingredient-in-recipe:Pavlova")]))["quote"]
    assert S.place_order("a", "M", first["id"])["status"] == "ordered"
    q = quote("WB", "WA")  # no use given
    line = q["lines"][0]
    assert line["use"] == "ingredient-in-recipe:Pavlova" and line["use_source"].startswith("history: order ")
    assert rules_of(q)["substitution.use_sensitive"] == "ask"
    assert "use" not in q["items"][0]  # what was asked stays as asked


def test_use_inferred_from_a_recipe_line():
    from agent.recipe import Need, _purpose
    n = Need(key="kuohukerma", name_fi="kuohukerma", name_en="whipping cream", dishes=["Tiramisu"], unit="L",
             needed=2, per_portion=[], claims=[], queries=[], must=[], exclude=[])
    assert _purpose(n) == "recipe: Tiramisu"  # the planner's item purpose is the use
    mandate()
    q = quote("WB", "WA", _purpose(n))
    assert rules_of(q)["substitution.use_sensitive"] == "ask"
    soup = Need(key="kuohukerma", name_fi="kuohukerma", name_en="whipping cream", dishes=["Lohikeitto"], unit="L",
                needed=2, per_portion=[], claims=[], queries=[], must=[], exclude=[])
    assert "substitution.use_sensitive" not in rules_of(quote("WB", "WA", _purpose(soup)))


def test_suggest_ranks_same_brand_first_and_says_why():
    r = SUB.suggest("WA", MON, 10, "L", limit=6, use="dessert", use_source="request")
    assert r["use"]["sensitivity"] == "exact" and r["use"]["classes"] == ["whipping"]
    by = {o["product_id"]: o for o in r["options"]}
    assert by["WB"]["use_ok"] is False and "whip differently" in by["WB"]["use_reason"]
    assert any("needs a person" in c for c in by["WB"]["compromises"])
    assert r["options"][0]["brand"] == "Valio"  # same brand first when the product matters
    coffee = SUB.suggest("CA", MON, 10, "L", limit=6, use="coffee")
    assert coffee["options"][0]["product_id"] == "CB" and coffee["options"][0]["use_ok"] is True
    assert "use" not in SUB.suggest("CA", MON, 10, "L")


def test_remembered_substitution_does_not_cross_uses():
    mandate(substitutions="ask")
    c = TestClient(api.app)
    o1 = S.place_order("a", "M", quote("WB", "WA", "sauce")["id"])
    assert o1["status"] == "pending_approval"
    r = c.post(f"/orders/{o1['id']}/approve", headers=H,
               json={"by": "Mikko", "remember": [{"rule": "substitution", "product_id": "WB"}]}).json()
    assert r["status"] == "ordered" and r["remembered"][0]["use"] == "cooking"
    # the same pair for a sauce again: remembered
    o2 = S.place_order("a", "M", quote("WB", "WA", "sauce")["id"])
    assert o2["status"] == "ordered"
    assert {f["rule"] for f in o2["decision"]["findings"]} >= {"remembered.substitution"}
    # the same pair for a dessert: asks again, and says why
    o3 = S.place_order("a", "M", quote("WB", "WA", "dessert")["id"])
    got = {f["rule"]: f["outcome"] for f in o3["decision"]["findings"]}
    assert o3["status"] == "pending_approval"
    assert got["substitution"] == "ask" and got["substitution.use_sensitive"] == "ask"
