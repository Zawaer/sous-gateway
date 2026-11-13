"""Valio Aimo's proactive suggestions: offline (catalog, prices and availability are stubbed)."""

import re
from datetime import date, datetime

import pytest

from valtakirja import aimo, catalog, config, service as S, signing, store, suggest
from valtakirja.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
DAY = date(2026, 11, 16)


def product(pid, name, cp, *, category="10400", labels=(), contains=("AM",), declared=True, rel=0.99, co2=1.4,
            claims=(), size=(1.0, "L"), stock=5000.0):
    return {
        "id": pid, "name": name, "names": {"fi": name, "en": name, "sv": ""}, "brand": "", "vendor": "",
        "category": category, "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
        "allowed_lot_size": 1.0, "temperature": "chilled", "labels": list(labels), "claims": list(claims),
        "allergens": {"declared": declared, "contains": list(contains) if declared else [], "may_contain": [],
                      "free_from": []},
        "size_from_name": size, "delivery_reliability": rel, "co2_kg_per_kg_group_avg": co2,
        "official_substitutes": [], "deleted": False, "_cp": cp, "_stock": stock,
    }


PRODUCTS = {p["id"]: p for p in [
    product("1", "Kevytmaito milk 1 l", 1.40, rel=0.85),  # the buyer's usual product
    product("2", "Kevytmaito milk 1 l value", 1.30),  # 7.1 % cheaper per L
    product("3", "Kevytmaito milk 1 l farm", 1.45, labels=["GOODS_FROM_FINLAND_BLUE_SWAN"]),  # +3.6 %, label
    product("4", "Kevytmaito milk 1 l nutty", 1.20, contains=["AM", "AN"]),  # adds an allergen
    product("5", "Kevytmaito milk 1 l mystery", 1.10, declared=False),  # no allergen data
    product("6", "Kevytmaito milk 1 l sold out", 1.00, stock=0),  # not deliverable
    product("7", "Kevytmaito milk 1 l luxury", 1.80, labels=["EU_ORGANIC_FARMING"]),  # +29 %: no
    product("10", "Marli glögi drink 1 l", 1.90, category="21400", contains=[]),
    product("11", "Joulu nut chocolate 200 g", 9.0, category="52290", contains=["AN"], size=(0.2, "KG")),
    product("12", "Kevytmaito oat-ish 1 l new", 1.50, contains=[]),
]}
PRODUCTS["10"]["allergens"]["free_from"] = ["X99"]
PRODUCTS["12"]["allergens"]["free_from"] = ["X99"]
CALLS = {"prices": 0, "availability": 0}


def fake_prices(ids, requested):
    CALLS["prices"] += 1
    return {i: {"catalogPrice": {"value": PRODUCTS[i]["_cp"], "valueWithTax": round(PRODUCTS[i]["_cp"] * 1.135, 2),
                                 "validTo": "2026-12-31T00:00:00+0200"},
                "comparisonPrice": {"value": PRODUCTS[i]["_cp"] / PRODUCTS[i]["size_from_name"][0]},
                "comparisonUnit": PRODUCTS[i]["size_from_name"][1], "tax": {"taxRate": 13.5}, "error": False}
            for i in ids}


def fake_availability(ids, requested):
    CALLS["availability"] += 1
    out = {}
    for i in ids:
        stock = PRODUCTS[i]["_stock"]
        out[i] = {"logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": False,
                  "scheduleLines": [{"salesBlock": False, "availableAmount": stock,
                                     "availableDate": "2026-11-16" if stock else None, "salesUnit": "ST"}]}
    return out


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(pid))
    monkeypatch.setattr(suggest, "_in_category", lambda c: [p for p in PRODUCTS.values() if p["category"] == c])
    monkeypatch.setattr(suggest, "_name_matches", lambda terms: [
        p for p in PRODUCTS.values() if any(t in p["name"].lower() for t in terms)])
    monkeypatch.setattr(suggest, "_new_ids", lambda: {"12"})
    monkeypatch.setattr(suggest, "_dates", lambda: {"12": "2025-10-03T10:00:00+0300"})
    monkeypatch.setattr(aimo, "prices", fake_prices)
    monkeypatch.setattr(aimo, "availability", fake_availability)
    monkeypatch.setattr(S, "_now", lambda: NOW)
    CALLS.update(prices=0, availability=0)
    yield
    signing._key.cache_clear()


def mandate(**kw):
    m = Mandate(**{"id": "M", "buyer": "Hotel", "agent_id": "a", "issued_by": "chef", "avoid_allergens": ["AN"],
                   "substitutions": "notify", **kw})
    S.issue_mandate(m)
    return m


def past_order(product_id="1", qty=60):
    o = {"id": "PO-1", "quote_id": "Q-1", "mandate_id": "M", "agent_id": "a", "buyer": "Hotel", "status": "ordered",
         "total_ex_vat": 84.0, "lines": [{"product_id": product_id, "name": PRODUCTS[product_id]["name"],
                                          "quantity": qty, "sales_unit": "ST"}],
         "idempotency_key": None, "created_at": "2026-11-12T10:00:00.000+02:00",
         "updated_at": "2026-11-12T10:00:00.000+02:00"}
    store.insert_order(o)


def by_product(r):
    return {s["product"]["id"]: s for s in r["suggestions"]}


def test_suggestions_are_labelled_mandate_checked_and_audited():
    mandate()
    past_order()
    r = suggest.suggestions("M", "a", DAY, limit=10)
    got = by_product(r)
    # cheaper, certified, seasonal and new; never the allergen, unknown, undeliverable or too dear ones
    assert set(got) == {"2", "3", "10", "12"}
    assert got["2"]["kind"] == "cheaper_alternative"
    assert "7.1% cheaper per L (€1.30 vs €1.40 ex VAT)" in got["2"]["reason"]
    assert "saves about €6.00 on 60 L" in got["2"]["reason"] and "same allergens" in got["2"]["reason"]
    assert got["3"]["kind"] == "certified_alternative" and "'Hyvää Suomesta' label" in got["3"]["reason"]
    assert "3.6% more per L" in got["3"]["reason"]
    assert got["10"]["kind"] == "seasonal" and "Christmas" in got["10"]["reason"]
    assert got["12"]["kind"] == "new" and "proxy" in got["12"]["reason"]
    for s in r["suggestions"]:
        assert s["sponsored_by"] == "Valio Aimo" and "not neutral advice" in s["disclosure"]
        assert s["mandate_check"]["outcome"] in ("silent", "notify", "ask")
    # an alternative is a substitution for the usual product, at the usual quantity
    assert got["2"]["instead_of"]["product_id"] == "1" and got["2"]["usual_quantity"]["quantity"] == 60
    assert got["2"]["mandate_check"]["outcome"] == "notify"
    assert r["considered"]["excluded"]["not_orderable"] == 1
    # one price call and one availability call for everything
    assert CALLS == {"prices": 1, "availability": 1}
    # offers only: no order or quote was created; the batch is in the audit log
    assert len(store.list_orders()) == 1
    ev = [e for e in store.audit_events() if e["event"] == "suggestions.offered"]
    assert len(ev) == 1 and ev[0]["subject"] == "M"
    assert {s["product_id"] for s in ev[0]["data"]["suggestions"]} == set(got)
    assert store.verify_chain()["ok"]


def test_mandate_rules_filter_before_anything_is_shown():
    mandate(allowed_categories=["10400"])
    past_order()
    got = by_product(suggest.suggestions("M", "a", DAY, limit=10))
    assert "10" not in got and "11" not in got  # other categories are out
    assert set(got) == {"2", "3", "12"}


def test_required_claims_and_price_cap():
    PRODUCTS["2"]["claims"] = ["FREE_FROM_LACTOSE"]
    PRODUCTS["1"]["claims"] = ["FREE_FROM_LACTOSE"]
    try:
        mandate(required_claims=["FREE_FROM_LACTOSE"], max_comparison_price_eur={"L": 1.35})
        past_order()
        got = by_product(suggest.suggestions("M", "a", DAY, limit=10))
        assert set(got) == {"2"}  # 3 is over the cap; 10 and 12 lack the claim
    finally:
        PRODUCTS["2"]["claims"] = PRODUCTS["1"]["claims"] = []


def test_without_history_only_seasonal():
    mandate()
    r = suggest.suggestions("M", "a", DAY)
    assert [s["product"]["id"] for s in r["suggestions"]] == ["10"]
    assert r["suggestions"][0]["usual_quantity"]["quantity"] == 1
    assert suggest.suggestions("M", "a", date(2026, 7, 1))["suggestions"] == []  # no season in July


def test_wrong_agent_and_revoked_mandate():
    mandate()
    with pytest.raises(S.GatewayError) as e:
        suggest.suggestions("M", "someone-else", DAY)
    assert e.value.code == "agent_mismatch"
    S.revoke_mandate("M", "chef")
    r = suggest.suggestions("M", "a", DAY)
    assert r["suggestions"] == [] and "not in force" in r["note"]
    assert CALLS == {"prices": 0, "availability": 0}


def test_season_keywords():
    assert [s[0] for s in suggest._season(DAY)] == ["Christmas"]
    autumn = next(s for s in suggest.SEASONS if s[0].startswith("autumn"))
    rx = re.compile(autumn[4], re.IGNORECASE)
    assert rx.search("KURPITSA SUOMI") and not rx.search("KESÄKURPITSA VIHREÄ")
