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


def price_kg(per_pack, per_kg):
    """A pack priced per pack with Valio's comparison price per KG (pack / comparison = kg in a pack)."""
    return {"catalogPrice": {"value": per_pack, "valueWithTax": round(per_pack * 1.135, 2),
                             "validTo": "2026-12-31T00:00:00+0200"},
            "comparisonPrice": {"value": per_kg}, "comparisonUnit": "KG", "tax": {"taxRate": 13.5}, "error": False}


# Packs of different sizes: a need in 400 g packs met by 10 kg packs (equal content, not equal pack count)
PRODUCTS.update({
    "T": product("T", "Chopped tomatoes 400 g", size_from_name=(0.4, "KG"), temperature="ambient"),
    "T10": product("T10", "Chopped tomatoes 10 kg", size_from_name=(10.0, "KG"), temperature="ambient"),
})
RAW_PRICE = {"T": price_kg(0.8, 2.0), "T10": price_kg(15.0, 1.5)}


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
            return [{**(RAW_PRICE.get(p) or price(PRICE[p])), "productId": p} for p in chunk]
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


# --- a need in packs: substitutes sized by equal content ------------------------------------------

def pack_offer(pid, need, unit=None):
    return Q.build_offer(PRODUCTS[pid], RAW_PRICE[pid], avail(AVAIL[pid]), MON, need, unit, NOW)


def tomato_sub(need_kg=16.0):
    """What substitutes.suggest returns for 40 x 400 g: the 10 kg pack priced for 16 kg (2 packs)."""
    return {"tier": "better", "product_id": "T10", "name": PRODUCTS["T10"]["name"], "type": {"match": "same"},
            "compromises": ["10 kg pack instead of 400 g"], "offer": pack_offer("T10", need_kg, "KG")}


def test_substitute_for_packs_is_sized_by_content_not_pack_count():
    """40 x 400 g = 16 kg -> 2 x 10 kg, never 40 x 10 kg."""
    AVAIL.update({"T": None, "T10": ("2026-11-16", None, None)})
    offers = [pack_offer("T", 40)]
    opts = OPT.build(0, 40, None, offers, [offers[0]], MON, [tomato_sub()], "T")
    assert kinds(opts) == ["substitute"]
    o = opts[0]
    assert [(l["product_id"], l["quantity"], l["substitute_for"]) for l in o["_lines"]] == [("T10", 2, "T")]
    assert o["total_ex_vat"] == 30.0
    assert o["covers_content"] == {"amount": 20.0, "unit": "KG"}
    assert o["covers"] == {"amount": 50.0, "unit": "SALES_UNIT"} and o["covers"]["amount"] >= 40
    assert "40 units (16 KG) as 2 ST, 20 KG, of Chopped tomatoes 10 kg" in o["tradeoff"]


def test_split_with_substitute_fills_the_gap_by_content():
    """30 x 400 g on time (12 kg); the 10 packs missing (4 kg) come as 1 x 10 kg."""
    AVAIL.update({"T": ("2026-11-16", 30, None), "T10": ("2026-11-16", None, None)})
    offers = [pack_offer("T", 40)]
    chosen, left = Q.plan(40, None, offers)
    assert left == 10
    opts = OPT.build(0, 40, None, offers, chosen, MON, [tomato_sub()], "T")
    split = next(o for o in opts if o["kind"] == "split_with_substitute")
    assert [(l["product_id"], l["quantity"]) for l in split["_lines"]] == [("T", 30), ("T10", 1)]
    assert split["covers_content"] == {"amount": 22.0, "unit": "KG"} and split["covers"]["amount"] == 55
    assert "the remaining 10 units (4 KG) as 1 ST, 10 KG, of Chopped tomatoes 10 kg" in split["tradeoff"]
    full = next(o for o in opts if o["kind"] == "substitute")
    assert [(l["product_id"], l["quantity"]) for l in full["_lines"]] == [("T10", 2)]


def test_substitute_in_another_measure_is_not_offered_for_packs():
    AVAIL.update({"T": None, "S": ("2026-11-16", None, None)})
    offers = [pack_offer("T", 40)]
    milk = sub_entry("S", need=16.0)  # measured in L: can't hold 16 kg
    assert OPT.build(0, 40, None, offers, [offers[0]], MON, [milk], "T") == []


def test_no_substitute_options_for_packs_when_the_original_content_is_unknown(monkeypatch):
    """No size in the name and no comparison price: 2 packs of the original can't be shown equal to anything,
    so no option claims '2 units ... no compromises' as 2 x 10 kg (QA, board 03:40Z)."""
    PRODUCTS["X"] = product("X", "Chopped tomatoes", size_from_name=None, temperature="ambient")
    monkeypatch.setitem(RAW_PRICE, "X", {**price_kg(0.8, 2.0), "comparisonPrice": None, "comparisonUnit": None})
    try:
        AVAIL.update({"X": None, "T10": ("2026-11-16", None, None)})
        offers = [pack_offer("X", 2)]
        assert offers[0]["content_per_sales_unit"] is None
        sub = {**tomato_sub(), "tier": "best", "compromises": [], "offer": pack_offer("T10", 2)}  # 2 own packs
        assert OPT.build(0, 2, None, offers, [offers[0]], MON, [sub], "X") == []
        # through the gateway: the quote has no substitute option an agent could order under notify
        monkeypatch.setattr(substitutes, "suggest", lambda *a, **k: {"options": [sub]})
        q = S.request_quote(S.QuoteRequest(agent_id="agent", mandate_id="M-T", delivery_date=MON,
                                           items=[S.QuoteItem(product_ids=["X"], amount=2)]))["quote"]
        assert not [o for o in q.get("options") or [] if o["substitute"]]
        assert q["mandate_preview"]["outcome"] == "block"
    finally:
        del PRODUCTS["X"]


def test_choosing_a_content_sized_substitute_requotes_its_own_packs(monkeypatch):
    """The option's lines are in the substitute's own packs; option_id re-quotes exactly those (2 x 10 kg)."""
    AVAIL.update({"T": None, "T10": ("2026-11-16", None, None)})
    seen = []

    def suggest(product_id, delivery_date, amount, unit, mandate=None, avoid=None, limit=3):
        seen.append((product_id, amount, unit))
        return {"options": [tomato_sub(amount * 0.4)]}
    monkeypatch.setattr(substitutes, "suggest", suggest)
    q = S.request_quote(S.QuoteRequest(agent_id="agent", mandate_id="M-T", delivery_date=MON,
                                       items=[S.QuoteItem(product_ids=["T"], amount=40)]))["quote"]
    assert seen == [("T", 40, None)]
    opt = next(o for o in q["options"] if o["kind"] == "substitute")
    assert [(l["product_id"], l["quantity"]) for l in opt["lines"]] == [("T10", 2)]
    assert opt["covers_content"] == {"amount": 20.0, "unit": "KG"}
    q2 = S.request_quote(S.QuoteRequest(agent_id="agent", mandate_id="M-T", option_id=opt["id"]))["quote"]
    assert [(l["product_id"], l["quantity"], l["substitute_for"]) for l in q2["lines"]] == [("T10", 2, "T")]
    assert q2["total_ex_vat"] == opt["total_ex_vat"] == 30.0


# --- cut-off passed for a single named product: same-type substitutes and the next deliverable date ---------

SUN = date(2026, 11, 15)


def cutoff_passed_on_sunday(monkeypatch, subs=()):
    """A asked for Sunday: Valio's earliest date is Monday and its cut-off (Fri 12:00) has passed. Asked for
    Tuesday or later, A comes on Tuesday with a cut-off still ahead."""
    def fake_fetch(kind, day, chunk):
        CALLS.append((kind, day))
        if kind == "price":
            return [{**price(PRICE[p]), "productId": p} for p in chunk]
        spec = [("2026-11-17", None, "2026-11-16T12:00:00+0200") if p == "A" and day >= "2026-11-17"
                else AVAIL[p] for p in chunk]
        return [{**avail(s), "productId": p} for p, s in zip(chunk, spec)]
    monkeypatch.setattr(aimo, "_fetch", fake_fetch)
    AVAIL["A"] = ("2026-11-16", None, "2026-11-13T12:00:00+0200")
    monkeypatch.setattr(substitutes, "suggest", fake_suggest(list(subs)))


def test_cutoff_message_names_the_requested_date_and_the_reported_earliest_separately():
    AVAIL["A"] = ("2026-11-16", None, "2026-11-13T12:00:00+0200")
    o = offer("A", requested=SUN)
    msgs = {i["code"]: i["message"] for i in o["issues"]}
    assert msgs["CUTOFF_PASSED"].startswith("Delivery on 2026-11-15 can't be ordered")
    assert "earliest date for it is 2026-11-16" in msgs["CUTOFF_PASSED"]
    assert "2026-11-16" in msgs["LATER_DATE"] and "cut-off has passed too" in msgs["LATER_DATE"]


def test_single_product_past_cutoff_gets_substitute_and_next_date_options(monkeypatch):
    cutoff_passed_on_sunday(monkeypatch, [("S", "better", "compatible", ["milk drink"])])
    q = quote(["A"], day=SUN)["quote"]
    assert q["mandate_preview"]["outcome"] == "block"
    # nothing orderable: the orderable total is 0, the indicative total still prices the line (100 L x EUR 1)
    assert q["total_ex_vat"] == 0 and q["indicative_total_ex_vat"] == 100.0
    by_kind = {o["kind"]: o for o in q["options"]}
    assert kinds(q["options"]) == ["next_date", "substitute"]  # both later than Sunday: same product first
    nxt = by_kind["next_date"]
    assert nxt["delivery_dates"] == ["2026-11-17"] and [l["product_id"] for l in nxt["lines"]] == ["A"]
    assert "earliest date Milk A 1 l can still be ordered for is 2026-11-17" in nxt["tradeoff"]
    assert nxt["mandate_preview"] == {"outcome": "ask", "rules": ["delivery.later_date"]}
    # choosing it re-quotes A for Tuesday, still flagged as later than the Sunday asked for
    q2 = S.request_quote(S.QuoteRequest(agent_id="agent", mandate_id="M-T", option_id=nxt["id"]))["quote"]
    assert q2["requested_delivery_date"] == "2026-11-15"
    assert [(l["product_id"], l["delivery_date"], l["orderable"]) for l in q2["lines"]] == [("A", "2026-11-17", True)]
    assert "LATER_DATE" in {i["code"] for i in q2["lines"][0]["issues"]}
    assert q2["mandate_preview"]["outcome"] == "ask" and "options" not in q2


def test_accepted_next_date_option_gets_its_own_approve_by(monkeypatch):
    """QA 06:02Z: asked for Sun 15.11, the signed next_date option (Tue 17.11, cut-off Mon 16.11 12:00) was accepted;
    approve_by was Sun 00:00 (the superseded date) and the order expired at Sun 00:01 saying Sunday was lost."""
    from sous import deadline
    sent = []
    monkeypatch.setattr(S.notify, "send", lambda event, o, m: sent.append((event, S.notify.compose(event, o, m))))
    cutoff_passed_on_sunday(monkeypatch)
    q = quote(["A"], day=SUN)["quote"]
    nxt = next(o for o in q["options"] if o["kind"] == "next_date")
    q2 = S.request_quote(S.QuoteRequest(agent_id="agent", mandate_id="M-T", option_id=nxt["id"]))["quote"]
    o = S.place_order("agent", "M-T", q2["id"])
    assert o["status"] == "pending_approval" and o["requested_delivery_date"] == "2026-11-15"
    assert o["approve_by"] == "2026-11-16T11:45:00+02:00"  # Mon 12:00 cut-off minus the 15 min margin
    assert o["approve_by_detail"]["basis"] == "cutoff" and o["approve_by_detail"]["delivery_date"] == "2026-11-17"
    assert "Approve by Mon 16.11 11:45 or delivery on Tue 17.11 is lost" in sent[-1][1]["message"]
    monkeypatch.setattr(S, "_now", lambda: datetime(2026, 11, 15, 0, 1, tzinfo=config.TZ))
    assert S.get_order(o["id"])["status"] == "pending_approval" and S.sweep_overdue() == 0
    late = datetime(2026, 11, 16, 11, 50, tzinfo=config.TZ)
    monkeypatch.setattr(S, "_now", lambda: late)
    x = S.get_order(o["id"])
    assert x["status"] == "expired_cutoff" and x["expiry"]["lost_delivery_date"] == "2026-11-17"
    assert "delivery on 2026-11-17 is lost" in x["expiry"]["reason"] and x["expiry"]["next_delivery_date"] == "2026-11-18"
    assert "delivery on Tue 17.11 is lost" in sent[-1][1]["message"]
    assert deadline.delivery_days(x) == ["2026-11-17"]


def test_accepted_next_date_option_can_be_approved_after_the_superseded_date(monkeypatch):
    cutoff_passed_on_sunday(monkeypatch)
    q = quote(["A"], day=SUN)["quote"]
    nxt = next(o for o in q["options"] if o["kind"] == "next_date")
    q2 = S.request_quote(S.QuoteRequest(agent_id="agent", mandate_id="M-T", option_id=nxt["id"]))["quote"]
    o = S.place_order("agent", "M-T", q2["id"])
    monkeypatch.setattr(S, "_now", lambda: datetime(2026, 11, 16, 11, 0, tzinfo=config.TZ))  # Sunday is past
    d = S.decide(o["id"], "chef", True)
    assert d["status"] == "ordered", d["approval"]["result_reason"]


def test_no_next_date_lookup_when_the_product_is_blocked_for_another_reason(monkeypatch):
    cutoff_passed_on_sunday(monkeypatch)
    AVAIL["A"] = None  # undeliverable, not a passed cut-off: nothing to ask again for
    q = quote(["A"], day=SUN)["quote"]
    assert "options" not in q
    assert not any(day >= "2026-11-17" for _, day in CALLS)


def test_next_date_option_preview_is_its_own_date_and_cutoff(monkeypatch):
    """Judge audit: a next_date option said block (availability.cutoff_passed), order_cutoff null and
    CUTOFF_UNKNOWN. Real Valio keeps reporting the earliest date (whose cut-off passed) when asked for a later
    day, and a second item past its own cut-off must not make this option look blocked by a cut-off."""
    monkeypatch.setattr(substitutes, "suggest", lambda *a, **k: {"options": []})
    AVAIL["A"] = ("2026-11-16", None, "2026-11-13T12:00:00+0200")  # passed at NOW (Fri 21:00), on any day asked
    AVAIL["B"] = ("2026-11-16", None, "2026-11-13T12:00:00+0200")
    q = S.request_quote(S.QuoteRequest(agent_id="agent", mandate_id="M-T", delivery_date=MON, items=[
        S.QuoteItem(product_ids=["A"], amount=100, unit="L"),
        S.QuoteItem(product_ids=["B"], amount=50, unit="L")]))["quote"]
    assert q["mandate_preview"]["outcome"] == "block"
    nxt = [o for o in q["options"] if o["kind"] == "next_date"]
    assert [o["item_index"] for o in nxt] == [0, 1]
    for o in nxt:
        assert o["delivery_dates"] == ["2026-11-17"]
        assert o["mandate_preview"] == {"outcome": "ask", "rules": ["delivery.later_date"]}
        # its own cut-off: the original's offset (12:00 three days before) applied to Tuesday, marked estimated
        assert o["order_cutoff"]["estimated"] is True and o["order_cutoff"]["at"] == "2026-11-14T12:00:00+02:00"
        assert "original date" in o["order_cutoff"]["basis"]
