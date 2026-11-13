"""Static catalog: compact product records plus a full-text index.

Built once from Valio Aimo's product JSON (17.5k products) into SQLite with an
FTS5 trigram index, so Finnish compound words ("laktoositon", "maitojuoma") match
on substrings. Search never calls the price/availability APIs: those are only
called later, for a shortlist.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from functools import lru_cache
from typing import Any

from . import config

# GS1 allergen type codes seen in the data (UN/CEFACT AllergenTypeCode list).
ALLERGENS: dict[str, str] = {
    "AM": "milk", "ML": "lactose", "AE": "egg", "AF": "fish", "AC": "crustaceans",
    "UM": "molluscs", "AP": "peanuts", "AN": "tree nuts", "SA": "almond",
    "SH": "hazelnut", "SW": "walnut", "SC": "cashew", "SP": "pecan",
    "SR": "brazil nut", "ST": "pistachio", "SM": "macadamia", "NM": "nuts (other)",
    "AW": "cereals containing gluten", "UW": "wheat", "NR": "rye", "GB": "barley",
    "GO": "oats", "GS": "spelt", "GK": "kamut", "AY": "soy", "AS": "sesame",
    "BM": "mustard", "BC": "celery", "NL": "lupin", "AU": "sulphites",
    "X99": "no declarable allergens",
}

# A buyer saying "nut-free" or "milk-free" means the whole family.
ALLERGEN_GROUPS: dict[str, set[str]] = {
    "AM": {"AM", "ML"},
    "AN": {"AN", "SA", "SH", "SW", "SC", "SP", "SR", "ST", "SM", "NM"},
    "AW": {"AW", "UW", "NR", "GB", "GO", "GS", "GK"},
}

TEMPERATURE_LABELS = {"warmTemperature": "ambient", "coolTemperature": "chilled", "freezeTemperature": "frozen"}

_SIZE_RE = re.compile(
    r"(?:(\d+)\s*[x×]\s*)?(\d+(?:[.,]\d+)?)\s*(kg|g|l|dl|cl|ml)\b", re.IGNORECASE
)
_TO_BASE = {"kg": ("KG", 1.0), "g": ("KG", 0.001), "l": ("L", 1.0), "dl": ("L", 0.1), "cl": ("L", 0.01), "ml": ("L", 0.001)}


def parse_size(name: str) -> tuple[float, str] | None:
    """Net content of one base unit from its name, e.g. '... 1 l ...' -> (1.0, 'L')."""
    m = None
    for m in _SIZE_RE.finditer(name):
        pass  # last match is usually the pack size ("Eila 3 % ... 1 l")
    if not m:
        return None
    count = int(m.group(1)) if m.group(1) else 1
    unit, factor = _TO_BASE[m.group(3).lower()]
    return count * float(m.group(2).replace(",", ".")) * factor, unit


def _lang(values: list[dict] | None, lang: str) -> str:
    for v in values or []:
        if v.get("language") == lang:
            return v.get("value") or ""
    return ""


def _classification(s: dict, name: str) -> list[dict]:
    for c in s.get("classifications") or []:
        if c.get("name") == name:
            return c.get("values") or []
    return []


def compact(p: dict, reliability: dict[str, float], co2: dict[str, float]) -> dict[str, Any]:
    s = p.get("synkkaData") or {}
    allergen_vals = _classification(s, "allergen")
    contains = sorted({v["id"] for v in allergen_vals if v.get("unit") == "CONTAINS"})
    may_contain = sorted({v["id"] for v in allergen_vals if v.get("unit") == "MAY_CONTAIN"})
    free_from = sorted({v["id"] for v in _classification(s, "nonAllergen")})
    labels = [v["id"] for v in _classification(s, "packagingMarkedLabel")]
    names = {lang: _lang(s.get("names"), lang) for lang in ("fi", "sv", "en")}
    name = names["en"] or names["fi"] or names["sv"] or p["id"]
    return {
        "id": p["id"],
        "name": name,
        "names": names,
        "brand": s.get("brand") or "",
        "vendor": p.get("vendorName") or "",
        "category": p.get("category") or "",
        "country_of_origin": p.get("countryOfOrigin") or "",
        "sales_unit": p.get("salesUnit"),
        "base_unit": p.get("baseUnit"),
        "units": {u["unitId"]: u["sizeInBaseUnits"] for u in p.get("units") or []},
        "allowed_lot_size": p.get("allowedLotSize"),
        "temperature": next((TEMPERATURE_LABELS[l] for l in labels if l in TEMPERATURE_LABELS), None),
        "labels": [l for l in labels if l not in TEMPERATURE_LABELS],
        "claims": sorted({v["id"] for v in _classification(s, "nutritionalClaim")}),
        "allergens": {
            "declared": bool(allergen_vals or free_from),
            "contains": contains,
            "may_contain": may_contain,
            "free_from": free_from,
        },
        "ingredients_en": _lang(s.get("keyIngredients"), "en"),
        "ingredients_fi": _lang(s.get("keyIngredients"), "fi"),
        "size_from_name": parse_size(names["fi"] or name),
        "delivery_reliability": reliability.get(p["id"]),
        "co2_kg_per_kg_group_avg": co2.get(p["id"]),
        "official_substitutes": [x["substituteProduct"] for x in p.get("substitutions") or []],
        "deleted": bool(p.get("deleted")),
    }


def _read_csv(path, col: int = 1) -> dict[str, float]:
    with open(path, newline="") as f:
        rows = csv.reader(f)
        next(rows)
        return {r[0]: float(r[col]) for r in rows if len(r) > col and r[col]}


def build(db_path=config.CATALOG_DB) -> int:
    products = json.load(open(config.PRODUCTS_JSON))
    reliability = _read_csv(config.RELIABILITY_CSV)
    co2 = _read_csv(config.CARBON_CSV)
    db_path.unlink(missing_ok=True)
    db = sqlite3.connect(db_path)
    db.executescript(
        """
        CREATE TABLE product (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE VIRTUAL TABLE product_fts USING fts5(
            id UNINDEXED, name_fi, name_en, name_sv, brand, ingredients, tokenize='trigram');
        """
    )
    for p in products:
        c = compact(p, reliability, co2)
        db.execute("INSERT INTO product VALUES (?, ?)", (c["id"], json.dumps(c, ensure_ascii=False)))
        db.execute(
            "INSERT INTO product_fts VALUES (?, ?, ?, ?, ?, ?)",
            (c["id"], c["names"]["fi"], c["names"]["en"], c["names"]["sv"], c["brand"],
             f'{c["ingredients_en"]} {c["ingredients_fi"]}'),
        )
    db.commit()
    db.close()
    return len(products)


@lru_cache(maxsize=1)
def _db() -> sqlite3.Connection:
    if not config.CATALOG_DB.exists():
        build()
    return sqlite3.connect(config.CATALOG_DB, check_same_thread=False)


def get(product_id: str) -> dict | None:
    row = _db().execute("SELECT data FROM product WHERE id = ?", (product_id,)).fetchone()
    return json.loads(row[0]) if row else None


def allergen_status(product: dict, avoid: list[str], allow_may_contain: bool = False) -> dict:
    """Deterministic allergen verdict for one product against codes to avoid.

    Unknown data is never treated as safe: the result is 'unknown' and the
    mandate engine escalates it to a human.
    """
    codes: set[str] = set()
    for a in avoid:
        codes |= ALLERGEN_GROUPS.get(a, {a})
    a = product["allergens"]
    hit = sorted(codes & set(a["contains"]))
    if hit:
        return {"status": "contains", "codes": hit}
    may = sorted(codes & set(a["may_contain"]))
    if may and not allow_may_contain:
        return {"status": "may_contain", "codes": may}
    if not codes:
        return {"status": "safe", "codes": []}
    if not a["declared"]:
        return {"status": "unknown", "codes": sorted(codes)}
    return {"status": "safe", "codes": []}


# Category codes are numeric with no names; the first two digits separate food
# from equipment. Measured on the data: these groups have no nutrition facts and
# standard (non-food) VAT: 20 kitchen equipment/tableware, 30/32 garden/lighters,
# 63 cleaning/napkins/hygiene, 64 disposables, 67 seasonal, 77 gases, 90 office.
NON_FOOD_CATEGORY_PREFIXES = frozenset({"20", "30", "32", "63", "64", "67", "77", "90"})


def is_food(p: dict) -> bool:
    return (p.get("category") or "")[:2] not in NON_FOOD_CATEGORY_PREFIXES


# Common foodservice terms as (fi, sv, en), alternatives separated by '/'.
# Buyers' agents query in any of the three; Finnish names are the most complete,
# so every term expands to its whole group. Multi-word entries match as phrases
# before single words ('olive oil' before 'oil').
_SYNONYM_GROUPS: list[tuple[str, str, str]] = [
    ("maito", "mjölk", "milk"),
    ("voi", "smör", "butter"),
    ("kerma", "grädde", "cream"),
    ("juusto", "ost", "cheese"),
    ("kananmuna/kananmunat/muna/munat", "ägg", "egg/eggs"),
    ("jauho/jauhot", "mjöl", "flour"),
    ("vehnäjauho/vehnäjauhot", "vetemjöl", "wheat flour"),
    ("ruisjauho/ruisjauhot", "rågmjöl", "rye flour"),
    ("peruna/perunat", "potatis", "potato/potatoes"),
    ("kana/kanan/broileri/broilerin", "kyckling", "chicken/broiler"),
    ("lohi", "lax", "salmon"),
    ("kala", "fisk", "fish"),
    ("kaura", "havre", "oat/oats"),
    ("kaurajuoma", "havredryck", "oat drink/oat milk"),
    ("kaurahiutale/kaurahiutaleet", "havregryn", "oat flakes/rolled oats/oatmeal"),
    ("riisi", "ris", "rice"),
    ("sokeri", "socker", "sugar"),
    ("öljy", "olja", "oil"),
    ("oliiviöljy", "olivolja", "olive oil"),
    ("rypsiöljy/rapsiöljy", "rapsolja", "rapeseed oil/canola oil"),
    ("kahvi", "kaffe", "coffee"),
    ("tee", "te", "tea"),
    ("leipä", "bröd", "bread"),
    ("tomaatti/tomaatit", "tomat/tomater", "tomato/tomatoes"),
    ("sipuli", "lök", "onion/onions"),
    ("porkkana", "morot", "carrot/carrots"),
    ("kurkku", "gurka", "cucumber"),
    ("nauta/naudan", "nöt", "beef"),
    ("sika/sian/porsas/porsaan", "fläsk/gris", "pork"),
    ("jauheliha", "köttfärs", "minced meat/ground beef/mince"),
    ("jogurtti", "yoghurt", "yogurt/yoghurt"),
    ("smetana/hapankerma", "gräddfil", "sour cream"),
    ("kuohukerma/vispikerma", "vispgrädde", "whipping cream/whipped cream"),
    ("ruokakerma", "matlagningsgrädde", "cooking cream"),
    ("kahvikerma", "kaffegrädde", "coffee cream"),
    ("tuorejuusto", "färskost", "cream cheese"),
    ("rahka", "kvarg", "quark"),
    ("laktoositon", "laktosfri", "lactose free"),
    ("gluteeniton", "glutenfri", "gluten free"),
    ("suola", "salt", "salt"),
    ("pippuri", "peppar", "pepper"),
    ("mehu", "juice", "juice"),
    ("omena", "äpple", "apple/apples"),
    ("banaani", "banan", "banana/bananas"),
    ("mansikka", "jordgubb/jordgubbar", "strawberry/strawberries"),
    ("mustikka", "blåbär", "blueberry/blueberries"),
    ("katkarapu/katkaravut", "räka/räkor", "shrimp/shrimps/prawn/prawns"),
    ("kinkku", "skinka", "ham"),
    ("makkara", "korv", "sausage/sausages"),
    ("jäätelö", "glass", "ice cream"),
    ("suklaa", "choklad", "chocolate"),
    ("soija", "soja", "soy/soya"),
    ("soijajuoma", "sojadryck", "soy drink/soy milk/soya drink"),
    ("kasviöljy", "vegetabilisk olja", "vegetable oil"),
    ("tomaattikastike", "tomatsås", "tomato sauce"),
    ("lohifilee", "laxfilé", "salmon fillet"),
    ("lautasliina", "servett/servetter", "napkin/napkins"),
]

# A concept: interchangeable terms per language ('*' for words not in the map).
Concept = dict[str, tuple[str, ...]]


def _concept(group: tuple[str, str, str]) -> Concept:
    return {lang: tuple(s.split("/")) for lang, s in zip(("fi", "sv", "en"), group)}


_SYNONYMS: dict[str, Concept] = {}
for _g in _SYNONYM_GROUPS:
    _c = _concept(_g)
    for _terms in _c.values():
        for _t in _terms:
            # Swedish 'glass' (ice cream) must not hijack English 'glass'; 'te'
            # is too short to match safely. Such terms only count via their group.
            if _t not in ("glass", "te"):
                _SYNONYMS.setdefault(_t, _c)
_MAX_PHRASE = max(len(t.split()) for t in _SYNONYMS)

# Words that make a product a dish, seasoning, sweet or accessory made from the
# thing named rather than the thing itself ('kana maustevalmiste' is not
# chicken). Penalised unless the query itself mentions them.
_DERIVED = (
    "mauste", "spice", "seasoning", "kastike", "sauce", "liemi", "fond", "bouillon", "aromi",
    "sipsi", "chips", "keksi", "levite", "spread", "piirak", "laatikko", "keitto", "soup",
    "stabilointi", "kondens", "condensed", "jauhe", "powder", "sose", "ketsuppi", "ketchup",
    "salaatti", "pizza", "lasagne", "pyörykkä", "täyte", "makeinen", "karamelli", "candy",
    "candies", "suklaa", "kroketti", "croquette", "pasteija", "hampurilai", "vuoka", "muusi",
    "krispies", "murot", "pasta", "rapeat",
    # plant-based stand-ins: 'cream' means dairy cream unless the query says otherwise
    "kookos", "coconut", "soija", "vegaan", "vegan", "kasvirasva", "kasvipohj", "plant based",
)
# A plain product named so is what a query without a flavour usually wants.
_PLAIN = ("maustamaton", "luonnon", "natural", "plain", "naturell")

# A query naming equipment or disposables ('milk jug', 'coffee cup', 'glass')
# wants the non-food item; otherwise food ranks first. Finnish words match as
# the head of a compound ('maitokannu' ends in 'kannu').
_NON_FOOD_HINTS = (
    "kannu", "veitsi", "muki", "kuppi", "lasi", "astia", "lautanen", "liina", "kulho",
    "kauha", "lusikka", "haarukka", "tarjotin", "kattila", "pannu", "rasia", "käsine",
    "folio", "kelmu", "saippua", "pesuaine", "puhdistusaine", "pyyhe", "harja",
    "jug", "jugs", "knife", "knives", "cup", "cups", "mug", "mugs", "glass", "glasses", "dish",
    "dishes", "plate", "plates", "bowl", "bowls", "scoop", "spoon", "fork", "tray", "pan",
    "pot", "gloves", "foil", "film", "soap", "detergent", "cleaner", "towel", "brush", "lid",
    "lids", "container", "containers", "basket", "slicer", "spreader", "napkin", "napkins",
    "kanna", "kniv", "kopp", "skål", "tallrik", "bricka", "tvål", "diskmedel", "servett",
)
_FOOD_PRIOR = 2.0
_LANG_WEIGHT = {"fi": 0.6, "en": 0.25, "sv": 0.15}
_FOREIGN = 0.8  # a Swedish/English term found in the Finnish name counts less


def _concepts(text: str) -> list[Concept]:
    """Split a query into concepts.

    'lactose free milk' -> [{fi: (laktoositon,), sv: (laktosfri,), en: (lactose free,)},
    {fi: (maito,), ...}]. Unknown words stand alone (plus the singular of an
    English '-s' plural that is in the map).
    """
    words = re.findall(r"\w+", text.lower().replace("-", " "))
    out: list[Concept] = []
    i = 0
    while i < len(words):
        for n in range(min(_MAX_PHRASE, len(words) - i), 0, -1):
            phrase = " ".join(words[i:i + n])
            if phrase in _SYNONYMS:
                out.append(_SYNONYMS[phrase])
                i += n
                break
        else:
            w = words[i]
            if len(w) > 4 and w.endswith("s") and w[:-1] in _SYNONYMS:
                out.append(_SYNONYMS[w[:-1]])
            else:
                out.append({"*": (w,)})
            i += 1
    return out


def _wants_non_food(text: str) -> bool:
    words = re.findall(r"\w+", text.lower())
    return any(w == h or (len(h) >= 4 and w.endswith(h)) for w in words for h in _NON_FOOD_HINTS)


def _terms(c: Concept) -> list[str]:
    return [t for ts in c.values() for t in ts]


def _fts_query(text: str, op: str = "AND") -> str:
    # Trigram FTS needs >= 3 characters per term; quote each term so syntax is literal.
    # Each concept is an OR group of its synonyms; concepts are joined with op.
    groups = []
    for c in _concepts(text):
        terms = sorted({t for t in _terms(c) if len(t) >= 3})
        if terms:
            groups.append("(" + " OR ".join(f'"{t}"' for t in terms) + ")")
    return f" {op} ".join(groups)


def _term_score(t: str, name: str) -> float:
    """Whole word 3; a compound ending in the term (its head: 'kasvivoi') 2.5;
    a word starting with it, or 'term-' (a modifier: 'maitosuklaa',
    'muna-riisipasteija') 1; any substring 0.5."""
    e = re.escape(t)
    if re.search(rf"\b{e}\b(?!-)", name):
        return 3.0
    if re.search(rf"{e}\b(?!-)", name):
        return 2.5
    if re.search(rf"\b{e}", name):
        return 1.0
    return 0.5 if t in name else 0.0


def _concept_score(c: Concept, lang: str, name: str) -> float:
    native = c.get(lang) or c.get("*") or ()
    best = max((_term_score(t, name) for t in native), default=0.0)
    for other, terms in c.items():
        if other != lang and other != "*":
            best = max(best, _FOREIGN * max(_term_score(t, name) for t in terms))
    return best


def _relevance(p: dict, concepts: list[Concept]) -> float:
    """How well the product's names match the query concepts, word-aware.

    Trigram FTS matches substrings, so 'voi' (butter) also hits 'Voima'. Each
    concept scores its best synonym per language, and languages are weighted
    (Finnish names are the most complete), so 'milk chocolate' does not count as
    milk when the Finnish name says 'maitosuklaa'. The brand is cut out of the
    names first ('Kuusamon Juusto' is a brand, not cheese). Then: a penalty for
    derived products (seasonings, sauces, dishes, sweets) the query did not ask
    for, a bonus for 'plain/unflavoured', and a small penalty per extra word, so
    the plain product wins over long variants.
    """
    brand = (p.get("brand") or "").lower().strip()
    names = {}
    for lang, v in p["names"].items():
        n = (v or "").lower()
        if brand and len(brand) >= 3:
            n = n.replace(brand, " ")
        names[lang] = n
    weights = {lang: w for lang, w in _LANG_WEIGHT.items() if names.get(lang, "").strip()}
    total_w = sum(weights.values()) or 1.0
    score = sum(
        sum(w * _concept_score(c, lang, names[lang]) for lang, w in weights.items()) / total_w
        for c in concepts
    )
    fi = names.get("fi") or names.get("en") or ""
    query_terms = [t for c in concepts for t in _terms(c)]
    query_text = " ".join(query_terms)
    if any(d in fi and d not in query_text for d in _DERIVED):
        score -= 1.5
    if score > 0 and any(w in fi for w in _PLAIN):
        score += 0.3
    brand_words = set(re.findall(r"\w+", brand))
    extra = [
        w for w in re.findall(r"[^\W\d_]{3,}", fi)
        if w not in brand_words and not any(t in w or w in t for t in query_terms)
    ]
    return score - min(0.1 * len(extra), 1.0)


def search(
    query: str = "",
    *,
    claims: list[str] | None = None,
    avoid_allergens: list[str] | None = None,
    labels: list[str] | None = None,
    temperature: str | None = None,
    category: str | None = None,
    limit: int = 20,
    food_only: bool | None = None,
) -> list[dict]:
    """Search names (fi/sv/en), brand and ingredients, then filter structurally.

    Queries may be English, Finnish or Swedish: common foodservice terms expand
    to all three ('butter' also searches 'voi' and 'smör').

    food_only: None (default) ranks food above kitchenware/cleaning/disposables
    that share a word ('butter' -> butter, not butter dishes) but still returns
    non-food when that is what matches ('napkin'), and flips to non-food first
    when the query names equipment ('milk jug', 'coffee cup'); True drops
    non-food; False turns the preference off.

    Products with unknown allergen data are kept but flagged, so the caller sees
    them; the mandate engine decides what that means.
    """
    db = _db()
    q = _fts_query(query)
    if q:
        sql = ("SELECT p.data FROM product_fts f JOIN product p ON p.id = f.id "
               "WHERE product_fts MATCH ? ORDER BY bm25(product_fts, 0, 4.0, 4.0, 2.0, 1.0, 0.5) LIMIT 2000")
        rows = db.execute(sql, (q,)).fetchall()
        if not rows and " AND " in q:
            # Nothing has every word ('butter knife'): rank partial matches
            # instead of returning nothing; relevance favours the most words.
            rows = db.execute(sql, (_fts_query(query, "OR"),)).fetchall()
    else:
        rows = db.execute("SELECT data FROM product LIMIT 5000").fetchall()
    products = [json.loads(d) for (d,) in rows]
    if q:
        concepts = _concepts(query)
        prior = 0.0 if food_only is not None else -_FOOD_PRIOR if _wants_non_food(query) else _FOOD_PRIOR
        # stable sort: relevance first, bm25 order breaks ties
        products.sort(key=lambda p: -(_relevance(p, concepts) + (prior if is_food(p) else 0.0)))
    out = []
    for p in products:
        if p["deleted"]:
            continue
        if food_only and not is_food(p):
            continue
        if claims and not set(claims) <= set(p["claims"]):
            continue
        if labels and not set(labels) <= set(p["labels"]):
            continue
        if temperature and p["temperature"] != temperature:
            continue
        if category and p["category"] != category:
            continue
        if avoid_allergens:
            st = allergen_status(p, avoid_allergens)
            if st["status"] in ("contains", "may_contain"):
                continue
            p["allergen_check"] = st
        out.append(p)
        if len(out) >= limit:
            break
    return out


def card(p: dict) -> dict:
    """The subset an agent needs to choose candidates (keeps tool responses small)."""
    keys = ("id", "name", "brand", "sales_unit", "size_from_name", "temperature", "claims",
            "labels", "allergens", "delivery_reliability", "co2_kg_per_kg_group_avg", "category")
    c = {k: p[k] for k in keys}
    if "allergen_check" in p:
        c["allergen_check"] = p["allergen_check"]
    return c
