"""Recipe planner (agent/recipe.py), offline: a fake catalog, stubbed Valio prices/availability, a scripted LLM.

Covers the chain request -> recipe -> ingredient quantities -> candidates -> ONE basket quote -> order, the
table fallback, the LLM JSON validation and retry, mandate allergen filtering with a gateway substitute, and
the waste trim for small needs.
"""

import json
import math
from datetime import date, datetime

import pytest

from agent import buyer as B, recipe as R
from agent.llm import ChatResponse, LLMConfig
from sous import aimo, catalog, config, service as S, signing, store, substitutes
from sous.mandate import Mandate
from sous.mcp_server import mcp

TODAY = date(2026, 11, 13)  # a Friday
NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)


def product(pid, name, size, unit, price, contains=("X99",), claims=(), sales_unit="ST"):
    contains = [c for c in contains if c != "X99"]
    return {
        "id": pid, "name": name, "names": {"fi": name, "en": name, "sv": ""}, "brand": "", "vendor": "",
        "category": "10400", "country_of_origin": "fi", "sales_unit": sales_unit, "base_unit": sales_unit,
        "units": {sales_unit: 1.0}, "allowed_lot_size": 1.0, "temperature": "chilled", "labels": [],
        "claims": list(claims),
        "allergens": {"declared": True, "contains": contains, "may_contain": [],
                      "free_from": [] if contains else ["X99"]},
        "size_from_name": (size, unit), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": 1.0,
        "official_substitutes": [], "deleted": False, "_price": price,
    }


# (product, search words that find it)
CATALOG = [
    (product("100", "Lasagne plates 5kg", 5.0, "KG", 20.0, ("AW",)), ("lasagne",)),
    (product("101", "Beef-pork minced meat 2 kg", 2.0, "KG", 16.0), ("jauheliha",)),
    (product("102", "Chopped tomatoes 2,5kg", 2.5, "KG", 4.0), ("tomaatti",)),
    (product("103", "ONION PEELED 10KG", 10.0, "KG", 9.0), ("sipuli",)),
    (product("104", "ONION PEELED 1KG", 1.0, "KG", 1.2), ("sipuli",)),
    (product("105", "GARLIC PEELED 1KG", 1.0, "KG", 6.0), ("valkosipuli",)),
    (product("106", "Semi skimmed milk 1 l", 1.0, "L", 1.0, ("AM",)), ("maito", "kevytmaito")),
    (product("107", "Butter 500 g", 0.5, "KG", 3.5, ("AM",)), ("voi",)),
    (product("108", "Wheat flour 2 kg", 2.0, "KG", 2.0, ("AW",)), ("vehnäjauho",)),
    (product("109", "Grated cheese 2,5 kg", 2.5, "KG", 25.0, ("AM",)), ("juustoraaste",)),
    (product("110", "Oregano 60g", 0.06, "KG", 2.5), ("oregano",)),
    (product("111", "Fine salt 1 kg", 1.0, "KG", 0.8), ("suola",)),
    (product("112", "Black pepper ground 480g", 0.48, "KG", 9.0), ("mustapippuri",)),
    (product("113", "Black pepper ground 3 kg", 3.0, "KG", 40.0), ("mustapippuri",)),
    (product("120", "Oat drink 1 l", 1.0, "L", 1.3, (), ("VEGAN",)), ("kaurajuoma",)),
    (product("121", "Whipping cream 1 l lactose free", 1.0, "L", 4.0, ("AM",), ("FREE_FROM_LACTOSE",)), ("kuohukerma",)),
    (product("122", "Whipping cream 1 l", 1.0, "L", 3.5, ("AM",)), ("kuohukerma",)),
]
PRODUCTS = {p["id"]: p for p, _ in CATALOG}
UNAVAILABLE: set[str] = set()


def fake_search(query, *, claims=None, avoid_allergens=None, limit=20, **kw):
    q = query.lower()
    out = []
    for p, words in CATALOG:
        if not any(w in q for w in words):
            continue
        if claims and not set(claims) <= set(p["claims"]):
            continue
        p = dict(p)
        if avoid_allergens:
            st = catalog.allergen_status(p, avoid_allergens)
            if st["status"] in ("contains", "may_contain"):
                continue
            p["allergen_check"] = st
        out.append(p)
    return out[:limit]


def fake_prices(ids, requested):
    out = {}
    for i in ids:
        p = PRODUCTS[i]
        size, unit = p["size_from_name"]
        out[i] = {"catalogPrice": {"value": p["_price"], "valueWithTax": round(p["_price"] * 1.135, 2),
                                   "validTo": "2026-12-31T00:00:00+0200"},
                  "comparisonPrice": {"value": round(p["_price"] / size, 4)}, "comparisonUnit": unit,
                  "tax": {"taxRate": 13.5}, "error": False}
    return out


def fake_availability(ids, requested):
    return {i: {"logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": i in UNAVAILABLE,
                "scheduleLines": [{"salesBlock": i in UNAVAILABLE, "availableAmount": 0 if i in UNAVAILABLE else 100000,
                                   "availableDate": "2026-11-16", "salesUnit": "ST"}]} for i in ids}


CALLS = {"prices": 0, "availability": 0}


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(pid))
    monkeypatch.setattr(catalog, "search", fake_search)
    CALLS.update(prices=0, availability=0)

    def fake_fetch(kind, day, chunk):  # below aimo's cache, so the batching is what's counted
        CALLS["prices" if kind == "price" else "availability"] += 1
        fn = fake_prices if kind == "price" else fake_availability
        return [{**v, "productId": k} for k, v in fn(chunk, date.fromisoformat(day)).items()]
    monkeypatch.setattr(aimo, "_fetch", fake_fetch)
    monkeypatch.setattr(aimo, "_cache", {})
    monkeypatch.setattr(S, "_now", lambda: NOW)
    UNAVAILABLE.clear()
    S.issue_mandate(Mandate(id="M-HOTEL", buyer="Hotel", agent_id="hotel-agent", issued_by="chef",
                            auto_approve_limit_eur=500, hard_limit_eur=5000))
    S.issue_mandate(Mandate(id="M-NOMILK", buyer="Caterer", agent_id="cater-agent", issued_by="chef",
                            auto_approve_limit_eur=500, hard_limit_eur=5000, avoid_allergens=["AM"],
                            substitutions="notify"))
    yield
    signing._key.cache_clear()


@pytest.fixture(scope="module")
def gw():
    with B.MCPGateway(mcp) as g:
        yield g


class FakeLLM:
    def __init__(self, answers):
        self.answers = list(answers)
        self.cfg = LLMConfig(provider="fake", base_url="", model="scripted")
        self.seen = []

    def chat(self, messages, tools=None):
        self.seen.append(messages)
        a = self.answers.pop(0)
        return ChatResponse({"role": "assistant", "content": a if isinstance(a, str) else json.dumps(a)}, "stop",
                            {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70}, 5, 0)


SOUP = {"dishes": [{"name": "Kermainen keitto", "ingredients": [
    {"name_fi": "kuohukerma", "name_en": "whipping cream", "amount_per_portion": 50, "unit": "ml", "notes": None},
    {"name_fi": "sipuli", "name_en": "onion", "amount_per_portion": 20, "unit": "g", "notes": None},
    {"name_fi": "suola", "name_en": "salt", "amount_per_portion": 2, "unit": "g", "notes": None}]}],
    "portions": 40, "delivery_date": "2026-11-16", "stock_on_hand": [{"ingredient": "salt", "plenty": True}]}


# --- interpretation and arithmetic ---------------------------------------------------------------

def test_table_interpretation_parses_dishes_portions_date_and_stock():
    s = R.interpret_table("Lasagne for 120 on Monday, we have 10 kg flour and plenty of salt", TODAY)
    assert [d.name for d in s.dishes] == ["Lasagne"]
    assert s.portions == 120 and s.delivery_date == date(2026, 11, 16)
    stock = {x.ingredient: x for x in s.stock_on_hand}
    assert stock["salt"].plenty and stock["flour"].amount == 10 and stock["flour"].unit == "kg"

    s = R.interpret_table("Joululounas 80 hengelle: lohikeitto ja joulukinkku perjantaiksi, meillä on 5 kg sipulia", TODAY)
    assert [d.name.split(" (")[0] for d in s.dishes] == ["Lohikeitto", "Joulukinkku"]
    assert s.portions == 80 and s.delivery_date == date(2026, 11, 20)
    needs = {n.key: n for n in R.compute_needs(s)}
    assert needs["sipuli"].on_hand == 5.0  # "sipulia" (partitive) matched to onion

    with pytest.raises(R.InterpretError):
        R.interpret_table("Pizza for 30", TODAY)
    with pytest.raises(R.InterpretError):
        R.interpret_table("Lasagne please", TODAY)


def test_quantities_scale_subtract_stock_and_round_up():
    s = R.interpret_table("Lasagne for 120 on Monday, we have 10 kg flour and plenty of salt", TODAY)
    needs = {n.key: n for n in R.compute_needs(s)}
    assert needs["jauheliha"].unit == "KG" and needs["jauheliha"].needed == pytest.approx(10.8)
    assert needs["maito"].unit == "L" and needs["maito"].to_buy == pytest.approx(14.4)
    assert needs["vehnajauho"].status == "covered_by_stock" and needs["vehnajauho"].to_buy == 0
    assert needs["suola"].on_hand == math.inf and needs["suola"].to_buy == 0
    assert needs["mustapippuri"].to_buy == 0.03  # 0.024 kg rounded up, never down
    # stock concentrate: 300 ml x 80 of fish stock is bought as 15 g/L of bouillon
    s = R.interpret_table("lohikeitto for 80", TODAY)
    k = {n.key: n for n in R.compute_needs(s)}["kalaliemi"]
    assert k.unit == "KG" and k.needed == pytest.approx(0.36)
    assert R.round_up(0.361, "KG") == 0.37 and R.round_up(12.01, "KG") == 12.1 and R.round_up(30.2, "L") == 30.5


def test_pieces_convert_by_weight_and_dishes_add_up():
    spec = R.MenuSpec.model_validate({"portions": 10, "dishes": [
        {"name": "A", "ingredients": [{"name_fi": "kananmuna", "name_en": "egg", "amount_per_portion": 1, "unit": "pcs"},
                                      {"name_fi": "sipuli", "name_en": "onion", "amount_per_portion": 50, "unit": "g"}]},
        {"name": "B", "portions": 20, "ingredients": [{"name_fi": "keltasipuli", "name_en": "onion", "amount_per_portion": 25,
                                                       "unit": "g"}]}]})
    needs = {n.key: n for n in R.compute_needs(spec)}
    assert needs["kananmuna"].unit == "KG" and needs["kananmuna"].needed == pytest.approx(0.6)
    assert needs["sipuli"].needed == pytest.approx(0.5 + 0.5) and needs["sipuli"].dishes == ["A", "B"]


def test_llm_json_is_validated_and_retried_once():
    llm = FakeLLM(["Sure! here is the plan", {**SOUP, "portions": 40}])
    use = R.LLMUse()
    spec = R.interpret_llm("Creamy soup for 40 on Monday, plenty of salt", TODAY, llm, use)
    assert spec.portions == 40 and use.calls == 2 and use.errors
    assert "not valid" in llm.seen[1][-1]["content"]  # the retry tells the model what was wrong
    # invalid twice: the table takes over when it knows the dish, and says so
    bad = {**SOUP, "portions": -1}
    spec, path = R.interpret("Lasagne for 50", TODAY, FakeLLM([bad, "```json\n{}\n```"]))
    assert path.startswith("table (LLM fake:scripted answer invalid") and spec.portions == 50
    with pytest.raises(R.InterpretError):
        R.interpret("Creamy soup for 40", TODAY, FakeLLM([bad, bad]))


# --- the whole chain through the in-process gateway ----------------------------------------------

def test_table_plan_builds_one_basket_quote_and_orders(gw):
    p = R.plan("Lasagne for 120 on Monday, we have 10 kg flour and plenty of salt", gateway=gw,
               mandate_id="M-HOTEL", agent_id="hotel-agent", today=TODAY, place=True)
    assert p.error is None and p.interpretation == "table"
    quotes = [s for s in p.trace.steps if s["tool"] == "request_quote"]
    assert quotes, "one basket quote"
    q = p.quote
    items = {i["note"]: i for i in q["items"]}
    assert "vehnäjauho" not in items and "suola" not in items  # on hand
    assert items["naudan-sian jauheliha"]["amount"] == pytest.approx(10.8) and items["maito"]["unit"] == "L"
    mince = next(l for l in q["lines"] if l["product_id"] == "101")
    assert mince["quantity"] == 6  # 10.8 kg in 2 kg packs
    assert not q["uncovered"]
    assert q["requested_delivery_date"] == "2026-11-16"
    assert p.order["status"] == "ordered" and p.order["quote_id"] == q["id"]
    # small need: the 3 kg pepper (cheapest per kg) is not bought for 30 g
    assert {l["product_id"] for l in q["lines"]} & {"112", "113"} == {"112"}
    # a basket costs one batched price + availability call, not two per item
    assert CALLS == {"prices": 1, "availability": 1}
    text = R.render(p)
    for part in ("1. Recipe", "2. Ingredients", "3. Products", "4. Quote", "5. Mandate preview", "6. Order"):
        assert part in text
    d = p.to_dict()
    assert d["order"]["status"] == "ordered" and d["ingredients"][0]["candidates"]
    assert "Lasagne for 120" in d["summary"]


def test_llm_plan_respects_mandate_allergens_with_a_gateway_substitute(gw, monkeypatch):
    def fake_suggest(product_id, delivery_date, amount, unit, mandate, avoid, limit=3):
        assert mandate.avoid_allergens == ["AM"]
        from sous import quote as Q
        offers, _ = Q.offers(["120"], delivery_date, amount, unit)
        return {"original": {"product_id": product_id}, "constraints": {}, "considered": {}, "no_safe_substitute": None,
                "options": [{"tier": "best", "product_id": "120", "name": "Oat drink 1 l", "compromises": ["oat, not milk"],
                             "allergen_check": {"status": "safe", "codes": []}, "offer": offers[0]}]}
    monkeypatch.setattr(substitutes, "suggest", fake_suggest)
    llm = FakeLLM([SOUP])
    p = R.plan("Creamy soup for 40 on Monday, plenty of salt", gateway=gw, mandate_id="M-NOMILK",
               agent_id="cater-agent", llm=llm, today=TODAY)
    assert p.interpretation == "llm:fake:scripted" and p.error is None
    needs = {n.key: n for n in p.needs}
    assert needs["suola"].status == "covered_by_stock"
    cream = needs["kuohukerma"]
    assert cream.status == "ruled_out_by_mandate" and cream.substitute["product_id"] == "120"
    lines = {l["product_id"]: l for l in p.quote["lines"]}
    assert "121" not in lines and "122" not in lines  # milk products never quoted under an AM mandate
    assert lines["120"]["substitute_for"] in ("121", "122") and lines["120"]["quantity"] == 2
    assert p.quote["mandate_preview"]["outcome"] == "notify"  # the mandate says substitutes notify
    # the onion: 0.8 kg needed, the 10 kg sack is left out
    assert "103" not in lines and lines["104"]["quantity"] == 1


def test_uncovered_item_gets_a_substitute_and_a_revised_quote(gw, monkeypatch):
    UNAVAILABLE.update({"121", "122"})  # no whipping cream on Monday

    def fake_suggest(product_id, delivery_date, amount, unit, mandate, avoid, limit=3):
        from sous import quote as Q
        offers, _ = Q.offers(["120"], delivery_date, amount, unit)
        return {"original": {"product_id": product_id}, "constraints": {}, "considered": {}, "no_safe_substitute": None,
                "options": [{"tier": "good", "product_id": "120", "name": "Oat drink 1 l", "compromises": ["different type"],
                             "allergen_check": {"status": "safe", "codes": []}, "offer": offers[0]}]}
    monkeypatch.setattr(substitutes, "suggest", fake_suggest)
    p = R.plan("Creamy soup for 40", gateway=gw, mandate_id="M-HOTEL", agent_id="hotel-agent",
               llm=FakeLLM([SOUP]), today=TODAY)
    assert p.first_quote is not None and p.first_quote["uncovered"]
    assert not p.quote["uncovered"]
    sub = next(l for l in p.quote["lines"] if l["product_id"] == "120")
    assert sub["substitute_for"] in ("121", "122")
    assert {n.key: n for n in p.needs}["kuohukerma"].status == "quoted_with_substitute"
    assert p.quote["mandate_preview"]["outcome"] == "ask"  # M-HOTEL: substitutes need a human


def test_nothing_to_buy_and_unknown_mandate(gw):
    p = R.plan("Lasagne for 10, we have plenty of everything: plenty of lasagne plates", gateway=gw,
               mandate_id="M-NOPE", agent_id="x", today=TODAY)
    assert p.error and "mandate" in p.error and p.quote is None


def test_buyer_routes_menu_requests_to_the_planner():
    assert R.looks_like_menu("Lasagne for 120 on Monday; we have 10 kg flour")
    assert R.looks_like_menu("Karjalanpaisti 60 annosta")
    assert not R.looks_like_menu("We need 1,000 litres of lactose-free milk delivered on Monday.")
    assert not R.looks_like_menu("Lasagne plates, 10 packs")
