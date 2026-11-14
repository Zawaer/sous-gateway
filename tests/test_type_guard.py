"""Quotes buy the kind of product asked for: coffee milk is never bought silently for milk.

Also: substitutes for N packs are priced for what those packs held (40 x 400 g -> 16 kg), not N of their own packs.
Offline: synthetic catalog, fake prices and availability, no data/ needed.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from sous import catalog, config, quote as Q, service as S, signing, store, substitutes as Sub
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=config.TZ)
DAY = date(2026, 11, 16)


def product(pid: str, fi: str, en: str = "", size=(1.0, "L"), category="10400", **kw) -> dict:
    p = {
        "id": pid, "name": en or fi, "names": {"fi": fi, "en": en, "sv": ""}, "brand": "Valio", "vendor": "",
        "category": category, "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST",
        "units": {"ST": 1.0}, "allowed_lot_size": 1, "temperature": "chilled", "labels": [],
        "claims": ["FREE_FROM_LACTOSE"],
        "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": []},
        "size_from_name": size, "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
        "official_substitutes": [], "deleted": False,
    }
    p.update(kw)
    return p


MILK = product("10", "Valio kevytmaito 1 l laktoositon UHT", "Valio semi skimmed milk 1 l lactose free UHT")
SKIM = product("11", "Valio rasvaton maito 1 l laktoositon UHT", "Valio skimmed milk 1 l lactose free UHT")
COFFEE = product("12", "Valio kahvimaito 1 l laktoositon UHT", "Valio coffee milk 1 l lactose free UHT")
CAFE = product("13", "Arla Cafè maito 1 L UHT laktoositon", "Arla Cafè milk 1 L UHT lactose free", brand="Arla")
COCOA = product("14", "Valio Play® maitokaakaojuoma 1 l laktoositon", "Valio Play® milk chocolate drink 1 l")
CATALOG = {p["id"]: p for p in (MILK, SKIM, COFFEE, CAFE, COCOA)}
PRICE_PER_L = {"10": 1.30, "11": 1.20, "12": 0.90, "13": 1.00, "14": 1.50}  # coffee milk is the cheapest


def price(pid: str) -> dict:
    v = PRICE_PER_L[pid]
    return {"catalogPrice": {"value": v, "valueWithTax": round(v * 1.135, 2), "validTo": "2026-12-31T00:00:00+0200"},
            "comparisonPrice": {"value": v}, "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}


def avail(ok: bool = True) -> dict:
    return {"logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": False,
            "scheduleLines": [{"salesBlock": False, "availableAmount": 9999999999.0 if ok else 0,
                               "availableDate": "2026-11-16" if ok else None, "salesUnit": "ST"}]}


@pytest.fixture
def gw(tmp_path, monkeypatch):
    """Gateway with a fake catalog; `gw(out_of_stock={...})` sets which products can't be delivered."""
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: CATALOG.get(pid))
    monkeypatch.setattr(Sub, "_same_category", lambda cat: list(CATALOG.values()))  # options look up substitutes
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(S.notify, "send", lambda *a, **k: None)
    blocked: set[str] = set()
    monkeypatch.setattr(Q, "offers", lambda ids, d, n=None, u=None, **k: (
        [Q.build_offer(CATALOG[i], price(i), avail(i not in blocked), d, n, u, NOW) for i in ids if i in CATALOG],
        NOW))
    S.issue_mandate(Mandate(id="M", buyer="Hotel", agent_id="a", issued_by="chef", auto_approve_limit_eur=5000,
                            hard_limit_eur=10000, substitutions="notify"))

    def run(ids, amount=1000, out_of_stock=(), product_type=None, unit="L"):
        blocked.clear()
        blocked.update(out_of_stock)
        return S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=DAY, items=[
            S.QuoteItem(product_ids=ids, amount=amount, unit=unit, product_type=product_type)]))["quote"]
    yield run
    signing._key.cache_clear()


def rules(q: dict) -> set[str]:
    return {f["rule"] for f in q["mandate_preview"]["findings"]}


# --- types ---------------------------------------------------------------------

def test_coffee_milk_cafe_milk_and_cocoa_are_not_milk(monkeypatch):
    milk = Sub.product_type(MILK)
    for p in (COFFEE, CAFE, COCOA):
        assert Sub.compare_types(milk, Sub.product_type(p))[0] == "different", p["name"]
    assert Sub.compare_types(milk, Sub.product_type(SKIM))[0] in ("same", "compatible")


def test_type_from_hint_in_english_or_finnish():
    assert Sub.type_from_hint("milk")["key"] == "maito"
    assert Sub.type_from_hint("lactose-free milk")["key"] == "maito"
    assert Sub.type_from_hint("maito")["key"] == "maito"
    assert Sub.type_from_hint("coffee milk")["key"] == "kahvimaito"
    assert Sub.type_from_hint("") is None and Sub.type_from_hint("zzz") is None


def test_dominant_type_wins_and_single_candidate_is_not_second_guessed(monkeypatch):
    monkeypatch.setattr(catalog, "get", lambda pid: CATALOG.get(pid))
    offers = [{"product_id": i} for i in ("12", "10", "11", "13", "11")]
    intended, v = Sub.type_guard(offers)
    assert intended["key"] == "maito"
    assert v["12"] == ("different", "different product type (coffee milk ≠ milk)")
    assert v["10"][0] == v["11"][0] == "ok"
    assert Sub.type_guard([{"product_id": "12"}]) == (None, {})


# --- quotes --------------------------------------------------------------------

def test_flagship_milk_quote_skips_cheaper_coffee_milk(gw):
    q = gw(["10", "13", "12", "11", "14"])  # 2 milks, 2 coffee milks: a tie goes to the first listed
    assert [l["product_id"] for l in q["lines"]] == ["11"]  # cheapest proper milk, not coffee milk at 0.90
    assert q["lines"][0]["quantity"] == 1000
    why = {a["product_id"]: a["why_not"] for a in q["alternatives_considered"]}
    assert why["12"] == ["different product type (coffee milk ≠ milk)"]
    assert why["13"] == ["different product type (coffee milk ≠ milk)"]
    assert why["14"] == ["different product type (cocoa drink ≠ milk)"]
    assert "product.type_mismatch" not in rules(q)
    assert q["mandate_preview"]["outcome"] == "silent"


def test_only_coffee_milk_orderable_is_quoted_but_asks(gw):
    q = gw(["10", "11", "12"], out_of_stock={"10", "11"})
    assert [l["product_id"] for l in q["lines"]] == ["12"]
    assert q["mandate_preview"]["outcome"] == "ask"
    [f] = [f for f in q["mandate_preview"]["findings"] if f["rule"] == "product.type_mismatch"]
    assert f["outcome"] == "ask" and "coffee milk ≠ milk" in f["message"]


def test_hint_flags_a_single_named_coffee_milk(gw):
    q = gw(["12"], product_type="milk")
    assert "product.type_mismatch" in rules(q) and q["mandate_preview"]["outcome"] == "ask"
    assert "product.type_mismatch" not in rules(gw(["12"]))  # named explicitly, no hint: the buyer's choice
    assert "product.type_mismatch" not in rules(gw(["12"], product_type="coffee milk"))


def test_out_of_type_never_tops_up_a_short_need(gw):
    # the right type out of stock everywhere except one: the rest is a gap, never filled with coffee milk
    q = gw(["10", "12"], out_of_stock={"10"}, product_type="milk")
    assert [l["product_id"] for l in q["lines"]] == ["12"]
    assert "product.type_mismatch" in rules(q)
    q = gw(["10", "11", "12"], out_of_stock={"10"})
    assert [l["product_id"] for l in q["lines"]] == ["11"]


def test_milk_drink_is_bought_for_milk_only_with_a_human(gw):
    CATALOG["15"] = product("15", "Valio Eila® kevytmaitojuoma 1 l laktoositon", "Valio Eila semi skimmed milk drink")
    PRICE_PER_L["15"] = 1.0
    try:
        q = gw(["10", "11", "15"])  # the milk drink is cheapest, but it is not milk
        assert [l["product_id"] for l in q["lines"]] == ["11"]
        why = {a["product_id"]: a["why_not"] for a in q["alternatives_considered"]}
        assert why["15"] == ["a related but different product (milk drink ≠ milk)"]
        q = gw(["10", "11", "15", "12"], out_of_stock={"10", "11"})  # milk drink before coffee milk, and asks
        assert [l["product_id"] for l in q["lines"]] == ["15"] and "product.type_mismatch" in rules(q)
    finally:
        CATALOG.pop("15"), PRICE_PER_L.pop("15")


def test_coffee_beans_are_one_kind():
    a = Sub.product_type(product("1", "Paulig Brazil Original kahvi kahvipapu 8x500g", size=(4.0, "KG")))
    b = Sub.product_type(product("2", "Paulig Juhla Mokka kahvi papukahvi 8x500g", size=(4.0, "KG")))
    assert a["key"] == b["key"] == "kahvi"


def test_nothing_orderable_keeps_the_real_reasons(gw):
    q = gw(["10", "12"], out_of_stock={"10", "12"})
    assert q["mandate_preview"]["outcome"] == "block"
    assert "product.type_mismatch" not in rules(q)


# --- substitute quantities -------------------------------------------------------

TOM = product("20", "Mutti tomaattimurska 400g", "Mutti chopped tomatoes 400g", size=(0.4, "KG"), category="T",
              temperature="ambient", claims=[], allergens={"declared": True, "contains": [], "may_contain": [],
                                                           "free_from": []})
TOM10 = product("21", "Bravo tomaattimurska 10kg", "Bravo chopped tomatoes 10 kg", size=(10.0, "KG"), category="T",
                temperature="ambient", claims=[], allergens=TOM["allergens"])
TOM1 = product("22", "Pirkka tomaattimurska 1kg", "chopped tomatoes 1 kg", size=(1.0, "KG"), category="T",
               temperature="ambient", claims=[], allergens=TOM["allergens"])
TOMS = {p["id"]: p for p in (TOM, TOM10, TOM1)}
KG_PRICE = {"20": (1.0, 2.5), "21": (20.0, 2.0), "22": (2.2, 2.2)}  # (per pack, per kg)


def tprice(pid):
    pack, kg = KG_PRICE[pid]
    return {"catalogPrice": {"value": pack, "valueWithTax": pack, "validTo": "2026-12-31T00:00:00+0200"},
            "comparisonPrice": {"value": kg}, "comparisonUnit": "KG", "tax": {"taxRate": 14}, "error": False}


def test_substitutes_cover_the_same_kilos_not_the_same_pack_count(monkeypatch):
    monkeypatch.setattr(catalog, "get", lambda pid: TOMS.get(pid))
    monkeypatch.setattr(Sub, "_same_category", lambda cat: list(TOMS.values()))
    unavailable = {"20"}
    monkeypatch.setattr(Q, "offers", lambda ids, d, n=None, u=None, **k: (
        [Q.build_offer(TOMS[i], tprice(i), avail(i not in unavailable), d, n, u, NOW) for i in ids], NOW))
    r = Sub.suggest("20", DAY, 40, None, limit=6)
    assert r["need"]["amount"] == 16.0 and r["need"]["unit"] == "KG"
    by = {o["product_id"]: o for o in r["options"]}
    assert by["21"]["quantity"] == 2 and by["21"]["covers"] == {"amount": 20.0, "unit": "KG"}
    assert by["22"]["quantity"] == 16 and by["22"]["covers"] == {"amount": 16.0, "unit": "KG"}
    assert by["22"]["total_ex_vat"] == 35.2
    assert by["22"]["total_change_ex_vat"] == round(35.2 - 2.5 * 16, 2)  # vs the original, like for like


@pytest.mark.parametrize("unit", ["ST", "st", "PCS", "KPL", "pack", "packs", "SALES_UNIT"])
def test_pack_units_are_one_unit_everywhere_and_substitutes_size_by_content(gw, monkeypatch, unit):
    """Judge audit: suggest_substitutes took unit 'ST' as a measure and kept 40 packs of 10 kg for a 40 x 400 g need
    (covers null), while request_quote rejected 'ST'. Pack words now all count packs (sous/quote.py); these
    products are sold per ST, so ST and SALES_UNIT mean the same here."""
    monkeypatch.setattr(catalog, "get", lambda pid: TOMS.get(pid))
    monkeypatch.setattr(Sub, "_same_category", lambda cat: list(TOMS.values()))
    monkeypatch.setattr(Q, "offers", lambda ids, d, n=None, u=None, **k: (
        [Q.build_offer(TOMS[i], tprice(i), avail(i != "20"), d, n, u, NOW) for i in ids], NOW))
    r = S.suggest_substitutes("a", "20", DAY, 40, unit, limit=6)
    assert r["need"]["amount"] == 16.0 and r["need"]["unit"] == "KG"
    by = {o["product_id"]: o for o in r["options"]}
    assert by["21"]["quantity"] == 2 and by["21"]["covers"] == {"amount": 20.0, "unit": "KG"}
    assert by["22"]["quantity"] == 16 and by["22"]["covers"] == {"amount": 16.0, "unit": "KG"}
    want = "SALES_UNIT" if unit.upper() in ("PACK", "PACKS", "SALES_UNIT") else "ST"  # PCS / KPL spell ST
    assert S.QuoteItem(product_ids=["20"], amount=40, unit=unit).unit == want
    from sous import standing
    assert standing.StandingItem(product_ids=["20"], amount=40, unit=unit).unit == want


def test_measure_units_any_case_and_unknown_units_are_refused(gw):
    assert S.QuoteItem(product_ids=["10"], amount=1, unit="kg").unit == "KG"
    assert S.QuoteItem(product_ids=["10"], amount=1, unit=" l ").unit == "L"
    with pytest.raises(ValueError):
        S.QuoteItem(product_ids=["10"], amount=1, unit="crate")
    with pytest.raises(S.GatewayError) as e:
        S.suggest_substitutes("a", "10", DAY, 1, "crate")
    assert e.value.code == "invalid_unit" and e.value.status == 422


# --- Valio unit codes are literal when the product defines them (judge audit, 473479) ----------------------
# Saclá garlic pesto 190 g, sold per KI (carton) of 6 jars (ST). '20 ST' = 20 jars, not 20 cartons.
PESTO = product("40", "Saclà Villi Valkosipuli Pesto 190 g", "Saclá 190g Garlic Pesto", size=(0.19, "KG"),
                category="P", temperature="ambient", claims=[], sales_unit="KI", base_unit="ST",
                units={"KRS": 216.0, "PAL": 1944.0, "KI": 6.0, "ST": 1.0})
NO_ST = product("41", "Pesto 1 kg", "Pesto 1 kg", size=(1.0, "KG"), category="P", temperature="ambient", claims=[],
                sales_unit="KI", base_unit="KI", units={"KI": 1.0, "PAL": 100.0})
PESTOS = {p["id"]: p for p in (PESTO, NO_ST)}


def pprice(pid):
    return {"catalogPrice": {"value": 12.0, "valueWithTax": 13.68, "validTo": "2026-12-31T00:00:00+0200"},
            "comparisonPrice": {"value": 12.0 / PESTOS[pid]["units"]["KI"] / 0.19 if pid == "40" else 12.0},
            "comparisonUnit": "KG", "tax": {"taxRate": 14}, "error": False}


def test_unit_code_the_product_defines_is_counted_literally(monkeypatch):
    monkeypatch.setattr(catalog, "get", lambda pid: PESTOS.get(pid))
    assert S.QuoteItem(product_ids=["40"], amount=20, unit="st").unit == "ST"
    o = Q.build_offer(PESTO, pprice("40"), avail(), DAY, 20, "ST", NOW)
    assert o["quantity"] == 4 and o["sales_unit"] == "KI"  # 20 jars -> 4 cartons of 6, rounded up
    assert o["covers"] == {"amount": 24.0, "unit": "ST"}
    assert o["unit_note"] == "20 ST asked -> 4 KI (24 ST) ordered, rounded up to whole KI"
    [line], left = Q.plan(20, "ST", [o])
    assert line["quantity"] == 4 and line["covers"] == {"amount": 24.0, "unit": "ST"} and left == 0
    # SALES_UNIT stays explicit for callers who mean cartons
    assert Q.build_offer(PESTO, pprice("40"), avail(), DAY, 20, None, NOW)["quantity"] == 20
    assert Q.build_offer(PESTO, pprice("40"), avail(), DAY, 2, "KI", NOW)["quantity"] == 2


def test_unit_code_the_product_lacks_means_its_sales_units(monkeypatch):
    monkeypatch.setattr(catalog, "get", lambda pid: PESTOS.get(pid))
    o = Q.build_offer(NO_ST, pprice("41"), avail(), DAY, 20, "ST", NOW)
    # the fallback is never silent, and covers names the real unit, not a false '20 ST'
    assert o["quantity"] == 20 and o["unit_note"] == "ST is not a unit of this product; read as 20 KI"
    assert o["covers"] == {"amount": 20.0, "unit": "KI"}
    [line], left = Q.plan(20, "ST", [o])
    assert line["quantity"] == 20 and left == 0
    assert line["covers"]["unit"] == "KI" and line["unit_note"].startswith("ST is not a unit of this product")


# Filos artichokes (465976): sold per KI of 6 jars, coded PRK (purkki); no ST unit at all. Judge audit: '20 ST'
# came back as 20 KI (120 jars, EUR 306.80) with covers '20 ST' and no unit_note.
ARTI = product("42", "Filos artisokansydämet 6x", "Filos artichoke hearts", size=(0.28, "KG"), category="P",
               temperature="ambient", claims=[], sales_unit="KI", base_unit="PRK", units={"PRK": 1.0, "KI": 6.0})
# two piece-type codes at ratio 1: no single piece unit, so ST falls back to cartons (and says so)
ARTI2 = product("43", "Filos artisokansydämet 6x", "Filos artichoke hearts", size=(0.28, "KG"), category="P",
                temperature="ambient", claims=[], sales_unit="KI", base_unit="PRK",
                units={"PRK": 1.0, "BOT": 1.0, "KI": 6.0})


def test_pieces_of_a_product_coded_in_jars_are_jars(monkeypatch):
    monkeypatch.setattr(catalog, "get", lambda pid: {"42": ARTI, "43": ARTI2}.get(pid))
    assert Q.piece_unit(ARTI) == "PRK" and Q.packs_per_sales_unit(ARTI, "ST") == 6
    for unit in ("ST", "PCS", "kpl"):
        o = Q.build_offer(ARTI, pprice("41"), avail(), DAY, 20, Q.normalize_unit(unit), NOW)
        assert o["quantity"] == 4 and o["covers"] == {"amount": 24.0, "unit": "PRK"}
        assert o["unit_note"] == ("ST read as PRK (this product's piece unit): 20 PRK asked -> 4 KI (24 PRK) "
                                  "ordered, rounded up to whole KI")
    [line], left = Q.plan(20, "ST", [o])
    assert line["quantity"] == 4 and line["covers"] == {"amount": 24.0, "unit": "PRK"} and left == 0
    assert "PRK" in line["unit_note"]
    # PRK asked directly is exact
    assert Q.build_offer(ARTI, pprice("41"), avail(), DAY, 12, "PRK", NOW)["quantity"] == 2
    # ambiguous pieces: sales units, said out loud, covers in KI
    assert Q.piece_unit(ARTI2) is None
    o = Q.build_offer(ARTI2, pprice("41"), avail(), DAY, 20, "ST", NOW)
    assert o["quantity"] == 20 and o["covers"] == {"amount": 20.0, "unit": "KI"}
    assert o["unit_note"] == "ST is not a unit of this product; read as 20 KI = 120 PRK"
    [line], _ = Q.plan(20, "ST", [o])
    assert line["covers"]["unit"] == "KI" and line["unit_note"] == o["unit_note"]


def test_substitutes_for_jar_pieces_size_by_the_jars(monkeypatch):
    cat = {"42": ARTI, "41": NO_ST}
    monkeypatch.setattr(catalog, "get", lambda pid: cat.get(pid))
    monkeypatch.setattr(Sub, "_same_category", lambda c: list(cat.values()))
    monkeypatch.setattr(Q, "offers", lambda ids, d, n=None, u=None, **k: (
        [Q.build_offer(cat[i], pprice("41"), avail(i != "42"), d, n, u, NOW) for i in ids], NOW))
    r = Sub.suggest("42", DAY, 20, "ST")
    assert r["need"]["amount"] == 5.6 and r["need"]["unit"] == "KG"  # 20 jars x 280 g, not 20 cartons


def test_quote_in_pieces_orders_whole_cartons(gw):
    CATALOG["16"] = product("16", "Valio kevytmaito 1 l laktoositon UHT", "", sales_unit="KI", units={"KI": 6.0, "ST": 1.0})
    PRICE_PER_L["16"] = 1.30
    try:
        q = gw(["16"], amount=20, unit="ST")
        [line] = q["lines"]
        assert line["quantity"] == 4 and line["covers"] == {"amount": 24.0, "unit": "ST"}
        assert line["unit_note"] == "20 ST asked -> 4 KI (24 ST) ordered, rounded up to whole KI"
        assert gw(["16"], amount=20, unit="SALES_UNIT")["lines"][0]["quantity"] == 20
        assert gw(["10"], amount=20, unit="ST")["lines"][0]["quantity"] == 20  # sold per ST: same as before
    finally:
        CATALOG.pop("16"), PRICE_PER_L.pop("16")


def test_substitutes_for_pieces_size_by_the_pieces_content(monkeypatch):
    monkeypatch.setattr(catalog, "get", lambda pid: PESTOS.get(pid))
    monkeypatch.setattr(Sub, "_same_category", lambda cat: list(PESTOS.values()))
    monkeypatch.setattr(Q, "offers", lambda ids, d, n=None, u=None, **k: (
        [Q.build_offer(PESTOS[i], pprice(i), avail(i != "40"), d, n, u, NOW) for i in ids], NOW))
    r = Sub.suggest("40", DAY, 20, "ST")
    assert r["need"]["amount"] == 3.8 and r["need"]["unit"] == "KG"  # 20 x 190 g, not 20 cartons
    assert r["need"]["asked"] == "20 ST x 190 g = 3.8 kg (sold per KI (6 x 190 g))"
    assert Sub.suggest("40", DAY, 20, None)["need"]["asked"] == "20 x KI (6 x 190 g) = 22.8 kg"


# --- false mismatches reported by QA (board 02:09Z oat milk, 03:00Z cashew butter) ------------

OAT = product("30", "Oatly Kaurajuoma Kevyt 1L", "Oatly Oat Drink Semi 1L", brand="Oatly", claims=[],
              allergens={"declared": True, "contains": ["GO"], "may_contain": [], "free_from": []})
CASHEW = product("31", "Biona luomu cashewpähkinä tahna 170g", "Biona organic cashew nut butter 170g",
                 size=(0.17, "KG"), brand="Biona", category="21453", temperature="ambient", claims=[],
                 allergens={"declared": True, "contains": ["AN"], "may_contain": ["AP"], "free_from": []})


@pytest.fixture
def extra():
    """OAT and CASHEW in the fake catalog for one test."""
    for p, v in ((OAT, 1.4), (CASHEW, 30.0)):
        CATALOG[p["id"]], PRICE_PER_L[p["id"]] = p, v
    yield
    for p in (OAT, CASHEW):
        CATALOG.pop(p["id"]), PRICE_PER_L.pop(p["id"])


def test_oat_milk_in_finnish_or_english_is_the_oat_drink():
    oat = Sub.product_type(OAT)
    for hint in ("kauramaito", "oat milk", "kaurajuoma", "oat drink"):
        t = Sub.type_from_hint(hint)
        assert t["key"] == oat["key"] == "kasvijuoma", hint
        assert Sub.compare_types(t, oat)[0] == "same", hint
    # still not dairy milk, either way round
    assert Sub.compare_types(Sub.product_type(MILK), oat)[0] == "different"
    assert Sub.compare_types(Sub.type_from_hint("kauramaito"), Sub.product_type(MILK))[0] == "different"


def test_oat_drink_quoted_for_kauramaito_without_a_type_review(gw, extra):
    q = gw(["30"], product_type="kauramaito")
    assert [l["product_id"] for l in q["lines"]] == ["30"]
    assert "product.type_mismatch" not in rules(q) and q["mandate_preview"]["outcome"] == "silent"
    assert "product.type_mismatch" in rules(gw(["30"], product_type="milk"))  # oat drink is not milk


def test_cashew_butter_is_read_from_its_english_name_when_the_finnish_has_no_type():
    t = Sub.product_type(CASHEW)
    assert t["key"] == "pähkinävoi"
    assert Sub.compare_types(Sub.type_from_hint("cashew nut butter"), t)[0] == "same"
    assert Sub.compare_types(Sub.type_from_hint("peanut butter"), t)[0] == "same"  # 'nut' is the type, not a flavour


def test_cashew_butter_needs_no_type_review_and_the_allergen_decision_is_separate(gw, extra):
    q = gw(["31"], amount=1, unit="SALES_UNIT", product_type="cashew nut butter")  # this mandate permits nuts
    assert "product.type_mismatch" not in rules(q) and q["mandate_preview"]["outcome"] == "silent"
    S.issue_mandate(Mandate(id="MN", buyer="Hotel", agent_id="a", issued_by="chef", auto_approve_limit_eur=5000,
                            hard_limit_eur=10000, avoid_allergens=["AN", "AP"]))
    q = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="MN", delivery_date=DAY, items=[
        S.QuoteItem(product_ids=["31"], amount=1, product_type="cashew nut butter")]))["quote"]
    assert q["mandate_preview"]["outcome"] == "block"
    assert "product.type_mismatch" not in rules(q)
    assert rules(q) & {"allergen.contains", "allergen.may_contain"}


def test_a_recognised_finnish_type_beats_a_contradicting_translation():
    wrong_en = product("40", "Valio kahvimaito 1 l laktoositon", "Valio milk 1 l lactose free")
    assert Sub.product_type(wrong_en)["key"] == "kahvimaito"
    cafe = product("41", "Arla Cafè maito 1 L", "Arla milk 1 L", brand="Arla")
    cocoa = product("42", "Valio maitokaakaojuoma 1 l", "Valio milk 1 l")
    milk = Sub.type_from_hint("lactose-free milk")
    for p in (wrong_en, cafe, cocoa):
        assert Sub.compare_types(milk, Sub.product_type(p))[0] == "different", p["name"]
    beans = product("43", "Paulig Juhla Mokka papukahvi 1kg", "Paulig Juhla Mokka beans 1kg", size=(1.0, "KG"))
    assert Sub.product_type(beans)["key"] == "kahvi"
    assert Sub.compare_types(Sub.type_from_hint("beans"), Sub.product_type(beans))[0] == "different"


def test_english_fallback_only_trusts_a_name_that_ends_in_its_type():
    def key(fi, en):
        return Sub.product_type(product("50", fi, en, category="T"))["key"]
    assert key("MONIN karamelli 70cl sokeriton", "MONIN caramel syrup 70cl sugar free") is None
    assert key("Topfish tonnikalapaloja auringonkukkaöljyssä 1kg", "Topfish tuna chunks in sunflower oil 1kg") is None
    assert key("Rajamäen 10l etikka", "Rajamäen 10l Apple wine vinegar") is None
    assert key("Santa Maria 300G Orange & Pepper", "Santa Maria 300G Orange & Pepper") is None
    assert key("Snellman Kunnon lauantai tanko 1 kg", "Snellman Cooked sausage 1 kg frozen") == "makkara"
    # non-food is never typed from English ('Paper Towel Roll' is not a bread roll)
    assert Sub.product_type(product("51", "Katrin pyyherulla", "Katrin Paper Towel Roll", category="6301"))["key"] is None


def test_unknown_type_is_unconfirmed_not_different(gw, extra):
    CATALOG["32"] = product("32", "Biona luomu tahna 170g", "Biona organic paste 170g", size=(0.17, "KG"),
                            brand="Biona", category="21453", claims=[])
    PRICE_PER_L["32"] = 20.0
    try:
        _, v = Sub.type_guard([{"product_id": "32"}], "nut butter")
        assert v["32"][0] == "unconfirmed" and "different" not in v["32"][1]
        q = gw(["32"], amount=1, unit="SALES_UNIT", product_type="nut butter")
        [f] = [f for f in q["mandate_preview"]["findings"] if f["rule"] == "product.type_mismatch"]
        assert f["outcome"] == "ask" and "unconfirmed" in f["message"] and "different product type" not in f["message"]
    finally:
        CATALOG.pop("32"), PRICE_PER_L.pop("32")


# --- an unnamed flavour means any flavour; English qualifiers count (QA: pesto false mismatch, 473479) -----------

GARLIC_PESTO = product("60", "Saclà Villi Valkosipuli Pesto 190 g", "Saclá 190g Garlic Pesto", size=(0.19, "KG"), brand="Saclá",
                category="21453", temperature="ambient", claims=[],
                allergens={"declared": True, "contains": ["AM"], "may_contain": ["AP"], "free_from": []})
YOG_PLAIN = product("61", "Valio maustamaton jogurtti 1 kg", "Valio plain yoghurt 1 kg", size=(1.0, "KG"))
YOG_STRAW = product("62", "Valio jogurtti 1 kg mansikka", "Valio strawberry yoghurt 1 kg", size=(1.0, "KG"))


@pytest.fixture
def pesto():
    for p, v in ((GARLIC_PESTO, 20.0), (YOG_PLAIN, 3.0), (YOG_STRAW, 3.2)):
        CATALOG[p["id"]], PRICE_PER_L[p["id"]] = p, v
    yield
    for p in (GARLIC_PESTO, YOG_PLAIN, YOG_STRAW):
        CATALOG.pop(p["id"]), PRICE_PER_L.pop(p["id"])


def test_garlic_pesto_hint_reads_the_english_qualifier():
    assert Sub.type_from_hint("garlic pesto")["flavour"] == "garlic"
    assert Sub.type_from_hint("valkosipulipesto")["flavour"] == "garlic"
    generic = Sub.type_from_hint("pesto")
    assert generic["flavour"] is None and generic["plain"] is False and generic["any_flavour"] is True
    t = Sub.product_type(GARLIC_PESTO)
    for hint in ("pesto", "garlic pesto", "valkosipulipesto"):
        assert Sub.compare_types(Sub.type_from_hint(hint), t)[0] == "same", hint


@pytest.mark.parametrize("hint", ["pesto", "garlic pesto", "valkosipulipesto"])
def test_pesto_hints_quote_garlic_pesto_without_a_type_review(gw, pesto, hint):
    q = gw(["60"], amount=1, unit="SALES_UNIT", product_type=hint)
    assert "product.type_mismatch" not in rules(q), hint
    assert not any(i["code"] == "TYPE_MISMATCH" for l in q["lines"] for i in l["issues"]), hint


def test_explicit_plain_yoghurt_still_flags_a_flavoured_one(gw, pesto):
    for hint in ("plain yoghurt", "maustamaton jogurtti"):
        assert Sub.type_from_hint(hint)["plain"] is True
        assert Sub.compare_types(Sub.type_from_hint(hint), Sub.product_type(YOG_STRAW))[0] == "different"
        assert Sub.compare_types(Sub.type_from_hint(hint), Sub.product_type(YOG_PLAIN))[0] == "same"
        assert "product.type_mismatch" in rules(gw(["62"], amount=1, unit="SALES_UNIT", product_type=hint))
    assert "product.type_mismatch" not in rules(gw(["62"], amount=1, unit="SALES_UNIT", product_type="yoghurt"))


def test_pesto_allergens_are_still_enforced(gw, pesto):
    S.issue_mandate(Mandate(id="MM", buyer="Hotel", agent_id="a", issued_by="chef", auto_approve_limit_eur=5000,
                            hard_limit_eur=10000, avoid_allergens=["AM"]))
    q = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="MM", delivery_date=DAY, items=[
        S.QuoteItem(product_ids=["60"], amount=1, unit="SALES_UNIT", product_type="garlic pesto")]))["quote"]
    assert q["mandate_preview"]["outcome"] == "block" and "product.type_mismatch" not in rules(q)
