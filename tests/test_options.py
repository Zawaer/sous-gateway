"""Response options for items that can't be covered as asked (sous/options.py). Offline: no data, no Valio calls."""

from datetime import date, datetime

import pytest

from sous import aimo, catalog, config, options as OPT, quote as Q, service as S, signing, store, substitutes
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
MON = date(2026, 11, 16)


def product(pid, name, **kw):
    p = {
        "id": pid, "name": name, "names": {"fi": name, "en": name, "sv": ""}, "brand": "", "vendor": "",
        "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
        "allowed_lot_size": 0.01, "temperature": "chilled", "labels": [], "claims": [],
        "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": []},
        "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
        "official_substitutes": [], "deleted": False,
    }
    p.update(kw)
    return p


PRODUCTS = {
    "A": product("A", "Milk A 1 l"),
    "B": product("B", "Milk B 1 l"),
    "C": product("C", "Milk C 1 l"),
    "S": product("S", "Milk S 1 l"),
}
# product id -> (price/L, availability). Availability: (date, amount or None = plenty, cut-off)
PRICE = {"A": 1.0, "B": 1.2, "C": 0.9, "S": 1.1}
AVAIL: dict[str, tuple] = {}


def price(v):
    return {"catalogPrice": {"value": v, "valueWithTax": round(v * 1.135, 2), "validTo": "2026-12-31T00:00:00+0200"},
            "comparisonPrice": {"value": v}, "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}


def avail(spec):
    if spec is None:  # undeliverable
        return {"logisticModel": "Stock", "closingTime": None, "salesBlock": False, "scheduleLines": []}
    day, amount, closing = spec
    return {"logisticModel": "Stock", "closingTime": closing or "2026-11-15T17:00:00+0200", "salesBlock": False,
            "scheduleLines": [{"salesBlock": False, "availableDate": day, "salesUnit": "ST",
                               "availableAmount": 9999999999 if amount is None else amount}]}


def offer(pid, need=100.0, requested=MON):
    return Q.build_offer(PRODUCTS[pid], price(PRICE[pid]), avail(AVAIL[pid]), requested, need, "L", NOW)


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(pid))
    CALLS.clear()

    def fake_fetch(kind, day, chunk):
        CALLS.append(kind)
        if kind == "price":
            return [{**price(PRICE[p]), "productId": p} for p in chunk]
        return [{**avail(AVAIL[p]), "productId": p} for p in chunk]
    monkeypatch.setattr(aimo, "_fetch", fake_fetch)
    monkeypatch.setattr(aimo, "_cache", {})
    monkeypatch.setattr(S, "_now", lambda: NOW)
    AVAIL.clear()
    AVAIL.update({"A": ("2026-11-16", None, None), "B": ("2026-11-16", None, None),
                  "C": ("2026-11-16", None, None), "S": ("2026-11-16", None, None)})
    S.issue_mandate(Mandate(id="M-T", buyer="Test kitchen", agent_id="agent", issued_by="chef",
                            auto_approve_limit_eur=500, substitutions="notify", later_delivery="ask"))
    yield
    signing._key.cache_clear()


CALLS: list[str] = []


def sub_entry(pid, tier="better", match="compatible", compromises=("milk drink instead of milk",), need=100.0):
    return {"tier": tier, "product_id": pid, "name": PRODUCTS[pid]["name"], "type": {"match": match},
            "compromises": list(compromises), "offer": offer(pid, need)}


def kinds(opts):
    return [o["kind"] for o in opts]


# --- options.build: pure ------------------------------------------------------

def test_short_stock_partial_now_rest_later_and_all_later():
    AVAIL["A"] = ("2026-11-16", 60, None)  # 60 on Monday, nothing known later
    AVAIL["B"] = ("2026-11-18", None, None)  # earliest Wednesday, plenty
    offers = [offer("A"), offer("B")]
    chosen, left = Q.plan(100, "L", offers)
    assert OPT.trouble(chosen, left, MON) == ["later_date"]
    opts = OPT.build(0, 100, "L", offers, chosen, MON)
    assert kinds(opts) == ["partial_now_rest_later", "all_later"]
    two = opts[0]
    assert two["delivery_dates"] == ["2026-11-16", "2026-11-18"] and two["covers"]["amount"] == 100
    assert [(l["product_id"], l["quantity"]) for l in two["_lines"]] == [("A", 60), ("B", 40)]
    assert "60 L arrive on 2026-11-16" in two["tradeoff"] and "40 L arrive by 2026-11-18" in two["tradeoff"]
    later = opts[1]
    assert later["delivery_dates"] == ["2026-11-18"] and not later["on_time"]
    assert "2 days later than asked" in later["tradeoff"]


def test_rest_is_never_assumed_from_the_same_short_product():
    """Valio reports only the earliest date: the short product's later stock is unknown, so no option uses it."""
    AVAIL["A"] = ("2026-11-16", 60, None)
    offers = [offer("A")]
    chosen = [offers[0]]  # single named product keeps PARTIAL_STOCK
    assert "partial_stock" in OPT.trouble(chosen, 0, MON)
    assert OPT.build(0, 100, "L", offers, chosen, MON) == []


def test_cheaper_late_candidate_gets_an_on_time_option_first():
    AVAIL["C"] = ("2026-11-17", None, None)  # cheapest, but Tuesday
    offers = [offer("C"), offer("B")]
    chosen, left = Q.plan(100, "L", offers)
    assert chosen[0]["product_id"] == "C" and OPT.trouble(chosen, left, MON) == ["later_date"]
    opts = OPT.build(0, 100, "L", offers, chosen, MON)
    assert opts[0]["kind"] == "on_time" and opts[0]["on_time"] and opts[0]["_lines"][0]["product_id"] == "B"
    assert opts[1]["kind"] == "all_later" and opts[1]["same_as_quote"]


def test_split_with_substitute_before_full_substitute_and_substitutes_last():
    AVAIL["A"] = ("2026-11-16", 60, None)
    AVAIL["B"] = ("2026-11-18", None, None)
    offers = [offer("A"), offer("B")]
    chosen, left = Q.plan(100, "L", offers)
    subs = [sub_entry("S")]
    opts = OPT.build(0, 100, "L", offers, chosen, MON, subs, "A")
    assert kinds(opts) == ["partial_now_rest_later", "all_later", "split_with_substitute", "substitute"]
    split = opts[2]
    assert [(l["product_id"], l["quantity"], l.get("substitute_for")) for l in split["_lines"]] == [
        ("A", 60, None), ("S", 40, "A")]
    assert split["substitute"]["tier"] == "better" and split["on_time"]
    assert all(l["substitute_for"] == "A" for l in opts[3]["_lines"])


def test_substitute_tiers_rank_best_first_and_different_type_never_splits():
    AVAIL["A"] = None  # undeliverable
    offers = [offer("A")]
    subs = [sub_entry("B", "good", "different", ["sour milk instead of milk"]),
            sub_entry("C", "better"), sub_entry("S", "best", "same", [])]
    opts = OPT.build(0, 100, "L", offers, [offers[0]], MON, subs, "A")
    assert [o["substitute"]["tier"] for o in opts] == ["best", "better", "good"]
    assert "no compromises" in opts[0]["tradeoff"]


def test_partial_substitute_stock_is_not_a_complete_option():
    AVAIL["A"] = None
    AVAIL["S"] = ("2026-11-16", 30, None)  # substitute itself is short
    offers = [offer("A")]
    assert OPT.build(0, 100, "L", offers, [offers[0]], MON, [sub_entry("S")], "A") == []


# --- through the gateway ------------------------------------------------------

def quote(ids, amount=100, mandate="M-T", day=MON):
    return S.request_quote(S.QuoteRequest(agent_id="agent", mandate_id=mandate, delivery_date=day,
                                          items=[S.QuoteItem(product_ids=ids, amount=amount, unit="L")]))


def fake_suggest(entries):
    def suggest(product_id, delivery_date, amount, unit, mandate=None, avoid=None, limit=3):
        return {"options": [sub_entry(p, t, m, c, amount) for p, t, m, c in entries]}
    return suggest


def test_quote_carries_signed_options_with_mandate_previews(monkeypatch):
    AVAIL["A"] = ("2026-11-16", 60, None)
    AVAIL["B"] = ("2026-11-18", None, None)
    monkeypatch.setattr(substitutes, "suggest", fake_suggest([("S", "better", "compatible", ["milk drink"])]))
    res = quote(["A", "B"])
    q = res["quote"]
    assert signing.verify(res["jws"], "sous-quote+jws")["options"] == q["options"]
    ids = [o["id"] for o in q["options"]]
    assert ids == [f"{q['id']}.O{n}" for n in range(1, len(ids) + 1)]
    by_kind = {o["kind"]: o for o in q["options"]}
    assert by_kind["partial_now_rest_later"]["rank"] == 1
    assert by_kind["partial_now_rest_later"]["mandate_preview"] == {"outcome": "ask", "rules": ["delivery.later_date"]}
    assert by_kind["split_with_substitute"]["mandate_preview"] == {"outcome": "notify", "rules": ["substitution"]}
    line = by_kind["split_with_substitute"]["lines"][1]
    assert line["product_id"] == "S" and line["substitute_for"] == "A" and line["issues"] == []


def test_no_options_when_the_item_is_covered_on_time():
    q = quote(["A"])["quote"]
    assert "options" not in q and q["mandate_preview"]["outcome"] == "silent"


def test_choosing_an_option_requotes_with_its_lines(monkeypatch):
    AVAIL["A"] = ("2026-11-16", 60, None)
    monkeypatch.setattr(substitutes, "suggest", fake_suggest([("S", "better", "compatible", ["milk drink"])]))
    q = quote(["A"])["quote"]  # single product, short: the quote itself needs a human
    assert q["mandate_preview"]["outcome"] == "ask"
    split = next(o for o in q["options"] if o["kind"] == "split_with_substitute")
    q2 = S.request_quote(S.QuoteRequest(agent_id="agent", mandate_id="M-T", option_id=split["id"]))["quote"]
    assert q2["chosen_option"] == split["id"] and q2["requested_delivery_date"] == q["requested_delivery_date"]
    assert [(l["product_id"], l["quantity"], l.get("substitute_for")) for l in q2["lines"]] == [
        ("A", 60, None), ("S", 40, "A")]
    assert not q2["uncovered"] and "options" not in q2
    assert q2["mandate_preview"]["outcome"] == split["mandate_preview"]["outcome"] == "notify"
    order = S.place_order("agent", "M-T", q2["id"], idempotency_key="k1")
    assert order["status"] == "ordered" and order["decision"]["outcome"] == "notify"


def test_option_id_is_checked(monkeypatch):
    AVAIL["A"] = ("2026-11-16", 60, None)
    monkeypatch.setattr(substitutes, "suggest", fake_suggest([("S", "better", "compatible", [])]))
    q = quote(["A"])["quote"]
    S.issue_mandate(Mandate(id="M-OTHER", buyer="Other", agent_id="other", issued_by="x"))
    with pytest.raises(S.GatewayError) as e:
        S.request_quote(S.QuoteRequest(agent_id="other", mandate_id="M-OTHER", option_id=q["options"][0]["id"]))
    assert e.value.code == "quote_mismatch"
    with pytest.raises(S.GatewayError) as e:
        S.request_quote(S.QuoteRequest(agent_id="agent", mandate_id="M-T", option_id=q["id"] + ".O99"))
    assert e.value.code == "option_not_found"
    with pytest.raises(S.GatewayError) as e:
        S.request_quote(S.QuoteRequest(agent_id="agent", mandate_id="M-T"))
    assert e.value.code == "invalid_request"


def test_substitute_lookup_failure_still_returns_the_quote(monkeypatch):
    AVAIL["A"] = None

    def boom(*a, **k):
        raise aimo.AimoError("rate limited")
    monkeypatch.setattr(substitutes, "suggest", boom)
    q = quote(["A"])["quote"]
    assert q["mandate_preview"]["outcome"] == "block" and "options" not in q


def test_single_product_short_is_capped_at_stock_and_the_rest_is_uncovered(monkeypatch):
    """Approving must never commit to stock that doesn't exist: the line is sized to stock, the rest is a gap."""
    AVAIL["A"] = ("2026-11-16", 60, None)
    monkeypatch.setattr(substitutes, "suggest", fake_suggest([("S", "better", "compatible", [])]))
    q = quote(["A"])["quote"]
    assert [(l["product_id"], l["quantity"]) for l in q["lines"]] == [("A", 60)]
    assert not any(i["code"] == "PARTIAL_STOCK" for i in q["lines"][0]["issues"])
    assert [g["uncovered"] for g in q["uncovered"]] == [40]
    rules = {f["rule"] for f in q["mandate_preview"]["findings"]}
    assert "need.partial" in rules and q["mandate_preview"]["outcome"] == "ask"
    assert {o["kind"] for o in q["options"]} == {"split_with_substitute", "substitute"}
