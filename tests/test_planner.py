"""The quote planner (sous/service.py _request_quote -> sous/quote.py plan): mandate-aware candidate choice.
Offline: no data, no Valio calls."""

from datetime import date, datetime

import pytest

from sous import aimo, catalog, config, quote as Q, service as S, signing, store
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
MON = date(2026, 11, 16)


def product(pid, name, **kw):
    p = {
        "id": pid, "name": name, "names": {"fi": name, "en": name, "sv": ""}, "brand": "", "vendor": "",
        "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
        "allowed_lot_size": 0.01, "temperature": "ambient", "labels": [], "claims": [],
        "allergens": {"declared": True, "contains": [], "may_contain": [], "free_from": ["X99"]},
        "size_from_name": (1.0, "KG"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
        "official_substitutes": [], "deleted": False,
    }
    p.update(kw)
    return p


PRODUCTS: dict[str, dict] = {}
PRICE: dict[str, dict] = {}
AVAIL: dict[str, dict] = {}


def price(per_pack, cmp_=None, per="KG"):
    return {"catalogPrice": {"value": per_pack, "valueWithTax": round(per_pack * 1.14, 2),
                             "validTo": "2026-12-31T00:00:00+0200"},
            **({"comparisonPrice": {"value": cmp_}, "comparisonUnit": per} if cmp_ is not None else {}),
            "tax": {"taxRate": 14}, "error": False}


def stock(day="2026-11-16", closing="2026-11-15T17:00:00+0200", model="Stock", amount=9999999999):
    return {"logisticModel": model, "closingTime": closing, "salesBlock": False,
            "scheduleLines": [{"salesBlock": False, "availableDate": day, "salesUnit": "ST", "availableAmount": amount}]}


def add(pid, name, per_pack, cmp_=None, per="KG", avail=None, **kw):
    PRODUCTS[pid] = product(pid, name, **kw)
    PRICE[pid] = price(per_pack, cmp_, per)
    AVAIL[pid] = avail or stock()


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(pid))

    def fake_fetch(kind, day, chunk):
        src = PRICE if kind == "price" else AVAIL
        return [{**src[p], "productId": p} for p in chunk]
    monkeypatch.setattr(aimo, "_fetch", fake_fetch)
    monkeypatch.setattr(aimo, "_cache", {})
    monkeypatch.setattr(S, "_now", lambda: NOW)
    PRODUCTS.clear(), PRICE.clear(), AVAIL.clear()
    S.issue_mandate(Mandate(id="M-GF", buyer="Test school", agent_id="agent", issued_by="chef",
                            auto_approve_limit_eur=500, avoid_allergens=["AW"]))
    yield
    signing._key.cache_clear()


def quote(ids, amount, unit="KG", mandate="M-GF", **kw):
    return S.request_quote(S.QuoteRequest(agent_id="agent", mandate_id=mandate, delivery_date=MON,
                                          items=[S.QuoteItem(product_ids=ids, amount=amount, unit=unit, **kw)]))["quote"]


# --- 1. the planner respects the mandate --------------------------------------------------------------------------

def test_cheapest_with_avoided_allergen_is_passed_over_for_a_safe_candidate():
    add("W", "Wheat flour 1 kg", 1.0, 1.0, allergens={"declared": True, "contains": ["UW"], "may_contain": [],
                                                    "free_from": []})
    add("G", "Gluten-free flour mix 1 kg", 3.0, 3.0)
    q = quote(["W", "G"], 10)
    assert [l["product_id"] for l in q["lines"]] == ["G"]
    assert q["mandate_preview"]["outcome"] != "block"
    alt = next(a for a in q["alternatives_considered"] if a["product_id"] == "W")
    assert alt["orderable"] is False and alt["mandate_refuses"] == ["allergen.contains"]
    assert "contains wheat (UW)" in alt["why_not"][0] and "Test school avoids cereals containing gluten (AW)" in alt["why_not"][0]


def test_only_refused_candidates_still_block_by_name():
    add("W", "Wheat flour 1 kg", 1.0, 1.0, allergens={"declared": True, "contains": ["UW"], "may_contain": [],
                                                    "free_from": []})
    q = quote(["W"], 10)
    assert [l["product_id"] for l in q["lines"]] == ["W"]  # the check itself is untouched
    assert q["mandate_preview"]["outcome"] == "block"
    assert "allergen.contains" in {f["rule"] for f in q["mandate_preview"]["findings"]}


def test_category_and_claims_refusals_are_left_out():
    S.issue_mandate(Mandate(id="M-LF", buyer="Test school", agent_id="agent", issued_by="chef",
                            allowed_categories=["10400"], required_claims=["FREE_FROM_LACTOSE"]))
    add("X", "Cream 1 kg", 1.0, 1.0, category="99999", claims=["FREE_FROM_LACTOSE"])
    add("Y", "Cream 1 kg", 1.1, 1.1)
    add("Z", "Cream lactose free 1 kg", 2.0, 2.0, claims=["FREE_FROM_LACTOSE"])
    q = quote(["X", "Y", "Z"], 5, mandate="M-LF")
    assert [l["product_id"] for l in q["lines"]] == ["Z"]
    refuses = {a["product_id"]: a["mandate_refuses"] for a in q["alternatives_considered"]}
    assert refuses == {"X": ["product.category"], "Y": ["product.claims"]}


def test_unknown_allergens_left_out_only_when_the_mandate_blocks_them():
    add("U", "Mystery mix 1 kg", 1.0, 1.0, allergens={"declared": False, "contains": [], "may_contain": [],
                                                     "free_from": []})
    add("G", "Safe mix 1 kg", 2.0, 2.0)
    assert [l["product_id"] for l in quote(["U", "G"], 3)["lines"]] == ["U"]  # unknown_allergens=ask: a human decides
    S.issue_mandate(Mandate(id="M-UB", buyer="Test school", agent_id="agent", issued_by="chef",
                            avoid_allergens=["AW"], unknown_allergens="block"))
    assert [l["product_id"] for l in quote(["U", "G"], 3, mandate="M-UB")["lines"]] == ["G"]


def test_no_comparison_price_is_not_called_more_expensive():
    add("A", "Flour A 1 kg", 2.0, 2.0)
    add("B", "Flour B", 1.0)  # no comparison price from Valio
    q = quote(["A", "B"], 2, mandate=None)
    alt = q["alternatives_considered"]
    assert all("More expensive" not in w for a in alt for w in a["why_not"])
    assert any("not comparable" in w for a in alt for w in a["why_not"])
