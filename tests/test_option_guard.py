"""Taking a signed exception option can never relax what the quote enforced (regression, QA blocker 14.11).

A buyer locks product A (purpose per item / product_purposes: exact or ask). A has only 60 L on the day; B, another
candidate, arrives two days later. The quote's options (all_later = B alone) used to drop the restriction: the
option's lines had no substitute_for and the re-quote rebuilt items without purpose / substitution / use, so
choosing it ordered B with a notify. Now option lines are marked like quote lines, an option that would put
another product in place of an exact one is never offered, and the re-quote carries every item restriction.
Offline: synthetic products, Valio's price/availability built locally."""

from datetime import date, datetime

import pytest

from sous import catalog, config, quote as Q, service as S, signing, store, substitutes as SUB
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
MON = date(2026, 11, 16)
PRICE = {"catalogPrice": {"value": 2.0, "valueWithTax": 2.27, "validTo": "2026-12-31T00:00:00+0200"},
         "comparisonPrice": {"value": 2.0}, "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}


def avail(amount, day):
    return {"logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": False,
            "scheduleLines": [{"salesBlock": False, "availableAmount": amount, "availableDate": day,
                               "salesUnit": "ST"}]}


AVAIL = {"A": avail(60, "2026-11-16"), "B": avail(5000, "2026-11-18"), "S": avail(0, "2026-11-16")}


def product(pid, fi):
    return {"id": pid, "name": fi, "names": {"fi": fi, "en": "", "sv": ""}, "brand": "", "vendor": "",
            "category": "10500", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
            "allowed_lot_size": 1.0, "temperature": "chilled", "labels": [], "claims": [],
            "allergens": {"declared": True, "contains": [], "may_contain": [], "free_from": []},
            "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
            "official_substitutes": [], "deleted": False}


PRODUCTS = {"A": product("A", "Kerma A 1 l"), "B": product("B", "Kerma B 1 l"), "S": product("S", "Kerma S 1 l")}


def fake_offers(product_ids, requested, need=None, need_unit=None):
    return [Q.build_offer(PRODUCTS[p], PRICE, AVAIL[p], requested, need, need_unit, NOW) for p in product_ids], NOW


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(config, "REQUIRE_AGENT_AUTH", False)
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(pid))
    monkeypatch.setattr(SUB, "_same_category", lambda cat: [])
    monkeypatch.setattr(Q, "offers", fake_offers)
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(S.allergen_ai, "assess_lines", lambda *a, **k: {})
    monkeypatch.setattr(S.notify, "send", lambda *a, **k: None)
    yield
    signing._key.cache_clear()


def mandate(**kw):
    S.issue_mandate(Mandate(**{"id": "M-T", "buyer": "Ward", "agent_id": "a", "issued_by": "chef",
                               "auto_approve_limit_eur": 500, "min_delivery_reliability": None,
                               "substitutions": "silent", "later_delivery": "notify", **kw}))


def quote(**item):
    return S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M-T", delivery_date=MON, items=[
        S.QuoteItem(amount=100, unit="L", **item)]))["quote"]


def take(option_id):
    return S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M-T", option_id=option_id))["quote"]


def rules(q):
    return {f["rule"]: f["outcome"] for f in q["mandate_preview"]["findings"]}


def b_options(q):
    return [o for o in q.get("options") or [] if any(l["product_id"] == "B" for l in o["lines"])]


def test_exact_product_in_the_mandate_no_option_swaps_it():
    """QA repro 1: A locked exact by the mandate, B chosen via all_later used to be ordered with a notify."""
    mandate(product_purposes={"A": {"purpose": "allergy menu", "substitution": "exact"}})
    q = quote(product_ids=["A", "B"])
    assert q["mandate_preview"]["outcome"] == "block" and rules(q)["purpose.exact"] == "block"
    assert b_options(q) == []  # never proposed in A's place
    for o in q.get("options") or []:  # what is offered keeps the restriction
        assert all(l["product_id"] == "A" for l in o["lines"])


def test_ask_product_in_the_mandate_survives_taking_the_option():
    mandate(product_purposes={"A": {"purpose": "recipe: whipped desserts", "substitution": "ask"}})
    q = quote(product_ids=["A", "B"])
    later = next(o for o in b_options(q) if o["kind"] == "all_later")
    assert "purpose.ask" in later["mandate_preview"]["rules"] and later["mandate_preview"]["outcome"] == "ask"
    assert later["lines"][0]["substitute_for"] == "A"
    q2 = take(later["id"])
    (line,) = q2["lines"]
    assert line["product_id"] == "B" and line["substitute_for"] == "A" and line["substitution"] == "ask"
    assert rules(q2)["purpose.ask"] == "ask"
    assert S.place_order("a", "M-T", q2["id"])["status"] == "pending_approval"


def test_item_restriction_survives_selecting_option():
    """QA repro 2: the item replaces S with purpose 'allergy menu', substitution exact; substitute_for survived but
    purpose and substitution were dropped on the re-quote."""
    mandate()
    q = quote(product_ids=["A", "B"], substitute_for="S", purpose="allergy menu", substitution="exact")
    assert q["mandate_preview"]["outcome"] == "block" and rules(q)["purpose.exact"] == "block"
    assert not [o for o in q.get("options") or [] if o["mandate_preview"]["outcome"] != "block"]
    assert b_options(q) == []


def test_item_ask_restriction_and_use_travel_with_the_option():
    mandate()
    q = quote(product_ids=["A", "B"], substitute_for="S", purpose="recipe: pavlova", substitution="ask",
              use="dessert")
    later = next(o for o in b_options(q) if o["kind"] == "all_later")
    q2 = take(later["id"])
    item = q2["items"][0]
    assert (item["substitute_for"], item["purpose"], item["substitution"], item["use"]) == \
        ("S", "recipe: pavlova", "ask", "dessert")
    line = q2["lines"][0]
    assert line["substitute_for"] == "S" and line["substitution"] == "ask" and line["use"] == "dessert"
    assert q2["mandate_preview"]["outcome"] == "ask" and rules(q2)["purpose.ask"] == "ask"
    assert S.place_order("a", "M-T", q2["id"])["status"] == "pending_approval"
