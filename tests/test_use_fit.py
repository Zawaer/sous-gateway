"""Does the chosen product fit its use (sous/use_fit.py)? Skimmed milk for a dessert or coffee cream for whipping asks a
person (product.use_fit); whole milk for a dessert, unknown specs and lines without a use say nothing. Offline:
synthetic products, Valio's price/availability built locally."""

from datetime import date, datetime

import pytest

from sous import catalog, config, quote as Q, remembered, rules as R, service as S, signing, store
from sous import use_fit as FIT
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
MON = date(2026, 11, 16)
AVAIL = {"logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": False,
         "scheduleLines": [{"salesBlock": False, "availableAmount": 5000, "availableDate": "2026-11-16",
                            "salesUnit": "ST"}]}


def price(eur):
    return {"catalogPrice": {"value": eur, "valueWithTax": round(eur * 1.135, 2), "validTo": "2026-12-31T00:00:00+0200"},
            "comparisonPrice": {"value": eur}, "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}


def product(pid, fi, brand="Valio"):
    return {"id": pid, "name": fi, "names": {"fi": fi, "en": "", "sv": ""}, "brand": brand, "vendor": "",
            "category": "10500", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
            "allowed_lot_size": 1.0, "temperature": "chilled", "labels": [], "claims": [],
            "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": []},
            "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
            "official_substitutes": [], "deleted": False}


PRODUCTS = {
    "SKIM": product("SKIM", "Valio rasvaton laktoositon UHT maitojuoma 1 l"),
    "WHOLE": product("WHOLE", "Valio laktoositon täysmaitojuoma 1 l"),
    "MILK?": product("MILK?", "Valio maito 1 l"),
    "COFFEE": product("COFFEE", "Valio kahvikerma 12 % 1 l"),
    "WHIP": product("WHIP", "Valio kuohukerma 38 % 1 l"),
    "CREAM?": product("CREAM?", "Valio kerma 1 l"),
    "COOK": product("COOK", "Valio ruokakerma 15 % 1 l"),
    "SPREAD": product("SPREAD", "Valio levite 60 % 400 g"),
    "BUTTER": product("BUTTER", "Valio voi 500 g"),
}
PRICES = {"SKIM": 1.0, "WHOLE": 1.5}


def fake_offers(product_ids, requested, need=None, need_unit=None):
    return [Q.build_offer(PRODUCTS[p], price(PRICES.get(p, 4.0)), AVAIL, requested, need, need_unit, NOW)
            for p in product_ids], NOW


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(config, "REQUIRE_AGENT_AUTH", False)
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(pid))
    monkeypatch.setattr(Q, "offers", fake_offers)
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(remembered, "_now", lambda: NOW)
    monkeypatch.setattr(S.allergen_ai, "assess_lines", lambda *a, **k: {})
    monkeypatch.setattr(S.notify, "send", lambda *a, **k: None)
    yield
    signing._key.cache_clear()


def mandate(**kw):
    S.issue_mandate(Mandate(**{"id": "M", "buyer": "Kitchen", "agent_id": "a", "issued_by": "chef",
                               "auto_approve_limit_eur": 5000, "min_delivery_reliability": None, **kw}))


def quote(pids, use=None, purpose=None):
    item = S.QuoteItem(product_ids=pids, amount=10, unit="L", use=use, purpose=purpose)
    return S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=MON, items=[item]))


def fit_findings(r):
    return [f for f in r["quote"]["mandate_preview"]["findings"] if f["rule"] == "product.use_fit"]


def test_requirements_table():
    p = PRODUCTS
    assert FIT.check(p["SKIM"], "dessert")["reason"].startswith(
        "Skimmed milk for a dessert: a fat-free milk changes the texture")
    assert FIT.check(p["WHOLE"], "dessert") is None and FIT.fits(p["WHOLE"], "dessert") is True
    assert FIT.check(p["COFFEE"], "whipping")["want"].startswith("whipping cream")
    assert FIT.check(p["COOK"], "ingredient-in-recipe:Tiramisu")
    assert FIT.check(p["WHIP"], "whipping") is None
    assert FIT.check(p["COFFEE"], "sauce") and FIT.check(p["COOK"], "sauce") is None
    assert FIT.check(p["WHIP"], "sauce") is None
    assert FIT.check(p["SPREAD"], "croissant") and FIT.check(p["BUTTER"], "croissant") is None
    assert FIT.check(p["COFFEE"], "coffee") is None and FIT.check(p["SKIM"], "coffee") is None
    # unknown spec: no finding
    assert FIT.check(p["MILK?"], "dessert") is None and FIT.fits(p["MILK?"], "dessert") is None
    assert FIT.check(p["CREAM?"], "whipping") is None
    assert FIT.check(p["SKIM"], None) is None


def test_skimmed_milk_for_a_dessert_asks_with_a_suggestion():
    mandate(substitutions="silent")
    r = quote(["SKIM", "WHOLE"], use="lactose-free milk for a dessert")
    assert [l["product_id"] for l in r["quote"]["lines"]] == ["SKIM"]  # the cheaper one is chosen
    [f] = fit_findings(r)
    assert f["outcome"] == "ask" and r["quote"]["mandate_preview"]["outcome"] == "ask"
    assert "Skimmed milk for a dessert" in f["message"] and "a person should confirm" in f["message"]
    assert "täysmaitojuoma" in f["message"]  # the fitting product of the same search
    assert r["quote"]["lines"][0]["use_fit_suggestion"]["product_id"] == "WHOLE"
    fi_ = list(r["explanations_fi"]["findings"].values())
    assert any("Rasvaton maito jälkiruokaan: rasvaton maito muuttaa rakennetta" in str(x) for x in fi_)


def test_whole_milk_for_a_dessert_is_fine():
    mandate()
    assert fit_findings(quote(["WHOLE"], use="dessert")) == []


def test_coffee_cream_for_whipping_asks():
    mandate()
    [f] = fit_findings(quote(["COFFEE"], use="whipping"))
    assert f["outcome"] == "ask" and "Coffee cream for whipping" in f["message"]
    assert "whipping cream (at least 30 % fat)" in f["message"]  # no fitting candidate: says what is needed


def test_purpose_counts_as_the_use():
    mandate()
    assert fit_findings(quote(["COFFEE"], purpose="recipe: tiramisu"))


def test_no_use_says_nothing():
    mandate()
    assert fit_findings(quote(["SKIM"])) == []
    assert fit_findings(quote(["COFFEE"])) == []


def test_mandate_tier_overrides():
    mandate(use_fit="silent")
    assert fit_findings(quote(["SKIM"], use="dessert")) == []
    mandate(use_fit="notify")
    [f] = fit_findings(quote(["SKIM"], use="dessert"))
    assert f["outcome"] == "notify"


def test_rule_is_registered():
    r = R.get("product.use_fit")
    assert r and r["tiers"] == ["silent", "notify", "ask", "block"] and r["text_fi"]
