"""Substitutes understand product type: sour milk is not milk, spread is not butter.

The first part is offline with synthetic products. The last test runs the ranking on
the real catalog (skipped without data/products.json) with live price/availability
replaced by a flat fake, so it needs no Valio calls.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from sous import catalog, config, quote as Q, substitutes as S


def product(pid: str, fi: str, en: str = "", **kw) -> dict:
    p = {
        "id": pid, "name": en or fi, "names": {"fi": fi, "en": en, "sv": ""}, "brand": "Valio", "vendor": "",
        "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST",
        "units": {"ST": 1.0}, "allowed_lot_size": 1, "temperature": "chilled", "labels": [],
        "claims": ["FREE_FROM_LACTOSE"],
        "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": ["ML"]},
        "size_from_name": catalog.parse_size(fi), "delivery_reliability": 0.99,
        "co2_kg_per_kg_group_avg": None, "official_substitutes": [], "deleted": False,
    }
    p.update(kw)
    return p


def t(fi: str, en: str = "") -> dict:
    return S.product_type(product("x", fi, en))


# --- type from the name ---------------------------------------------------------

@pytest.mark.parametrize("fi,key,family", [
    ("Valio kevytmaito 1 l laktoositon UHT", "maito", "milk"),
    ("Valio Eila® kevytmaitojuoma 1 l laktoositon", "maitojuoma", "milk"),
    ("Valio Eila® rasvaton piimä 1 l laktoositon", "piimä", "sour milk"),
    ("Valio kermaviili 2 kg laktoositon", "kermaviili", "sour cream"),
    ("Valio smetana 2 kg laktoositon", "smetana", "sour cream"),
    ("Valio crème fraîche 2 kg laktoositon", "ranskankerma", "sour cream"),
    ("Valio kuohukerma 1 l laktoositon", "kuohukerma", "cream"),
    ("Valio ruokakerma 15 % 1 l UHT laktoositon", "ruokakerma", "cream"),
    ("Valio voi 500 g normaalisuolainen", "voi", "butter"),
    ("Keiju kasvirasvalevite 60 % 600 g", "levite", "spread"),
    ("Valio pehmeä maitorahka 250 g", "rahka", "quark"),
    ("Valio jogurtti 1 kg metsämustikka laktoositon", "jogurtti", "yogurt"),
    ("Atria Kanan Fileesuikale 3kg", "filee", "boneless cut"),
    ("Vaasan Viipaloitu Maalaisleipä 6x500g", "leipä", "bread"),
    ("Fazer Näkkileipä 20 x 250 g", "näkkileipä", "crispbread"),
    ("TOMAATTI 1LK SUOMI", "tomaatti", "tomato"),
    ("Oddlygood® Barista kaurajuoma 0,5 l", "kasvijuoma", "plant drink"),
])
def test_head_noun(fi, key, family):
    got = t(fi)
    assert (got["key"], got["family"]) == (key, family)


def test_english_name_is_the_backup():
    assert t("", "Valio skimmed sour milk 1 l")["key"] == "piimä"
    assert t("", "Valio semi skimmed milk drink 1 l")["key"] == "maitojuoma"


def test_fat_flavour_species_and_prep():
    assert t("Valio kevytmaito 1 l")["fat_pct"] == 1.5
    assert t("Valio täysmaito 1 l")["fat_pct"] == 3.5
    assert t("Valio rasvaton maito 1 l")["fat_pct"] == 0.0
    assert t("Valio Eila® 3 % maitojuoma 1 l")["fat_pct"] == 3.0
    # a berry after the head is the flavour, not the type
    yog = t("Valio jogurtti 1 kg metsämustikka")
    assert (yog["key"], yog["flavour"]) == ("jogurtti", "blueberry")
    assert t("Arla Mansikkajogurtti 150 g")["flavour"] == "strawberry"
    assert t("Valiojogurtti® 1 kg maustamaton")["plain"] is True
    raw, cooked = t("Atria Kanan Fileesuikale 3kg"), t("Atria Kypsä Kanan Fileesuikale 2kg")
    assert (raw["species"], raw["form"], raw["prep"]) == ("chicken", "strips", None)
    assert cooked["prep"] == "cooked"
    assert t("Kalavapriikki Kirjolohifilee annospala")["species"] == "rainbow trout"
    assert t("Oatly kaurapohjainen jogurtti 1 kg")["plant_based"] is True
    assert t("Valio jogurtti ja kauramuro 200 g")["plant_based"] is False


@pytest.mark.parametrize("orig,cand,level", [
    ("Valio kevytmaito 1 l", "Valio kevytmaito 1 l", "same"),
    ("Valio kevytmaito 1 l", "Valio kevytmaitojuoma 1 l", "compatible"),
    ("Valio kevytmaito 1 l", "Valio täysmaito 1 l", "compatible"),
    ("Valio kevytmaito 1 l", "Valio rasvaton piimä 1 l", "different"),
    ("Valio voi 500 g", "Keiju levite 600 g", "different"),
    ("Valio maustamaton jogurtti 1 kg", "Valio jogurtti 1 kg mansikka", "different"),
    ("Valio jogurtti 1 kg mansikka", "Valio jogurtti 1 kg mustikka", "compatible"),
    ("Valio jogurtti 1 kg", "Valio maitorahka 1 kg", "different"),
    ("Valio kuohukerma 1 l", "Valio ruokakerma 1 l", "different"),  # cooking cream does not whip
    ("Valio ruokakerma 1 l", "Valio kuohukerma 1 l", "compatible"),
    ("Valio smetana 2 kg", "Valio kermaviili 2 kg", "compatible"),
    ("Valio kevytmaito 1 l", "Valio kauramaito 1 l", "different"),
    ("Atria Kanan Filee 3kg", "Atria Kypsä Kanan Filee 3kg", "different"),
    ("Atria Kanan Filee 3kg", "Atria Kalkkunan Filee 3kg", "different"),
    ("Kalavapriikki lohifilee 10 kg", "Kalavapriikki kirjolohifilee 10 kg", "compatible"),
    ("Lohi kokonainen perattu 5 kg", "Kalavapriikki lohifilee 5 kg", "different"),
])
def test_compare_types(orig, cand, level):
    assert S.compare_types(t(orig), t(cand))[0] == level


def test_compromises_are_plain_words():
    _, minor, _ = S.compare_types(t("Valio kevytmaito 1 l"), t("Valio täysmaito 1 l"))
    assert minor == ["higher fat: 3.5% vs 1.5%"]
    _, minor, _ = S.compare_types(t("Valio kevytmaito 1 l"), t("Valio kevytmaitojuoma 1 l"))
    assert minor == ["milk drink instead of milk"]
    _, _, major = S.compare_types(t("Valio kevytmaito 1 l"), t("Valio rasvaton piimä 1 l"))
    assert major == ["sour milk instead of milk"]


@pytest.mark.parametrize("name,text,total", [
    ("Valio kevytmaito 1 l", "1 l", 1.0),
    ("5 x Arla 1 L kevytmaito Suomesta", "5 x 1 l", 5.0),
    ("12 x Arla Kevytmaito 1 L Suomi", "12 x 1 l", 12.0),
    ("Arla Annosmaito 100x2 cl laktoositon", "100 x 2 cl", 2.0),
    ("Santa Maria Harissa 350gx6", "6 x 350 g", 2.1),
    ("Atria Kanan Filee n3kg/n150g", "3 kg", 3.0),
    ("Valio kirjolohifilee annospala 5 kg (á 150 g)", "5 kg", 5.0),
])
def test_pack(name, text, total):
    p = S.pack(name)
    assert (p["text"], p["total"]) == (text, total)


# --- suggest() end to end, offline --------------------------------------------

ORIG = product("1", "Valio kevytmaito 1 l laktoositon UHT", "Valio semi skimmed milk 1 l lactose free UHT")
SOUR = product("2", "Valio kevytmaito-piimä 1 l laktoositon UHT", "Valio semi skimmed sour milk 1 l lactose free")
TEN = product("3", "10 x Valio kevytmaito 1 l laktoositon", "")
DRINK = product("4", "Valio Eila® kevytmaitojuoma 1 l laktoositon", "")
WHOLE = product("5", "Valio täysmaito 1 l laktoositon UHT", "")


def offer(pid: str, orderable: bool = True) -> dict:
    return {"product_id": pid, "orderable": orderable, "comparison_price": {"ex_vat": 1.0, "per": "L"},
            "sales_unit": "ST", "issues": [], "unit_price_ex_vat": 1.0}


def _setup(monkeypatch, products, unorderable=()):
    by_id = {p["id"]: p for p in products}
    monkeypatch.setattr(catalog, "get", lambda pid: by_id.get(pid))
    monkeypatch.setattr(S, "_same_category", lambda cat: list(by_id.values()))
    monkeypatch.setattr(Q, "offers", lambda ids, *a, **k: ([offer(i, i not in unorderable) for i in ids], None))


def test_sour_milk_is_not_offered_for_milk(monkeypatch):
    _setup(monkeypatch, [ORIG, SOUR, TEN, DRINK, WHOLE])
    r = S.suggest("1", date(2026, 11, 20), 100, "L", limit=6)
    ids = [o["product_id"] for o in r["options"]]
    assert "2" not in ids
    assert r["considered"]["excluded"]["product_type"] == 1
    assert r["constraints"]["type"]["key"] == "maito"
    by = {o["product_id"]: o for o in r["options"]}
    assert by["3"]["type"]["match"] == "same" and by["3"]["compromises"] == ["10 x 1 l instead of 1 l"]
    assert by["4"]["compromises"] == ["milk drink instead of milk"]
    assert by["5"]["compromises"] == ["higher fat: 3.5% vs 1.5%"]
    assert all(o["tier"] == "better" for o in r["options"])
    assert r["options"][0]["product_id"] == "3"  # same type ranks first within a tier


def test_different_type_only_as_last_resort(monkeypatch):
    _setup(monkeypatch, [ORIG, SOUR, DRINK], unorderable={"4"})
    r = S.suggest("1", date(2026, 11, 20), 100, "L")
    assert [o["product_id"] for o in r["options"]] == ["2"]
    o = r["options"][0]
    assert o["tier"] == "good" and o["type"]["match"] == "different"
    assert "different product type: sour milk instead of milk" in o["compromises"]


def test_identical_product_is_best(monkeypatch):
    twin = product("6", "Valio kevytmaito 1 l laktoositon ESL", "")
    _setup(monkeypatch, [ORIG, twin, SOUR])
    r = S.suggest("1", date(2026, 11, 20), 100, "L")
    assert r["options"][0]["product_id"] == "6" and r["options"][0]["tier"] == "best"
    assert r["options"][0]["compromises"] == []


# --- real catalog -----------------------------------------------------------------

# (original, the top option's Finnish name must match, must not match)
QUALITY = [
    ("464201", r"maito", r"piimä"),                          # semi-skimmed milk, lactose free
    ("477050", r"piimä", None),                              # skimmed sour milk
    ("465845", r"luonnon|maustamaton", None),                # plain yogurt
    ("476751", r"jogurtti", r"rahka"),                       # blueberry yogurt
    ("469210", r"kuohukerma|vispikerma", r"ruokakerma"),     # whipping cream
    ("464249", r"ruokakerma", None),                         # cooking cream
    ("476890", r"smetana", None),                            # smetana
    ("464411", r"\bvoi\b", r"levite|margariini"),            # butter
    ("463196", r"rahka", r"jogurtti"),                       # quark
    ("470527", r"jauheliha", None),                          # minced beef
    ("463232", r"filee", r"kypsä|paneroi"),                  # raw chicken fillet
    ("461903", r"lohifilee", r"savu|graavi"),                # raw salmon fillet
    ("462674", r"filee", None),                              # pork loin
    ("460065", r"tomaatti", None),                           # tomatoes
    ("460586", r"peruna", None),                             # potatoes
    ("460131", r"kurkku", None),                             # cucumber
    ("460884", r"croissant", None),                          # croissant
    ("461404", r"leipä", None),                              # sliced bread
    ("462683", r"ruis", None),                               # rye bread
]


@pytest.mark.skipif(not config.PRODUCTS_JSON.exists(), reason="needs data/products.json")
@pytest.mark.parametrize("pid,want,avoid", QUALITY)
def test_real_catalog_never_ranks_a_different_type_first(monkeypatch, pid, want, avoid):
    monkeypatch.setattr(Q, "offers", lambda ids, *a, **k: ([offer(i) for i in ids], None))
    r = S.suggest(pid, date(2026, 11, 20), 10, None, limit=3)
    assert r["options"], pid
    assert all(o["type"]["match"] != "different" for o in r["options"])
    top = catalog.get(r["options"][0]["product_id"])["names"]["fi"]
    assert re.search(want, top, re.I), top
    assert not (avoid and re.search(avoid, top, re.I)), top
