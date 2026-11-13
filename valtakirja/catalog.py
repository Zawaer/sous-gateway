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


def _fts_query(text: str) -> str:
    # Trigram FTS needs >= 3 characters per term; quote each term so syntax is literal.
    terms = [t for t in re.findall(r"\w+", text.lower()) if len(t) >= 3]
    return " AND ".join(f'"{t}"' for t in terms)


def _relevance(p: dict, terms: list[str]) -> float:
    """How well the product's names match the terms, word-aware.

    Trigram FTS matches substrings, so 'voi' (butter) also hits 'Voima'. Rank a
    whole word highest, then a Finnish compound ending in the term ('kasvivoi'),
    then a word that merely starts with it, then any substring.
    """
    names = " ".join(v for v in p["names"].values() if v).lower()
    score = 0.0
    for t in terms:
        e = re.escape(t)
        if re.search(rf"\b{e}\b", names):
            score += 3
        elif re.search(rf"{e}\b", names):
            score += 2
        elif re.search(rf"\b{e}", names):
            score += 1
        elif t in names:
            score += 0.5
    return score


def search(
    query: str = "",
    *,
    claims: list[str] | None = None,
    avoid_allergens: list[str] | None = None,
    labels: list[str] | None = None,
    temperature: str | None = None,
    category: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search names (fi/sv/en), brand and ingredients, then filter structurally.

    Products with unknown allergen data are kept but flagged, so the caller sees
    them; the mandate engine decides what that means.
    """
    db = _db()
    q = _fts_query(query)
    if q:
        rows = db.execute(
            "SELECT p.data FROM product_fts f JOIN product p ON p.id = f.id "
            "WHERE product_fts MATCH ? ORDER BY bm25(product_fts, 0, 4.0, 4.0, 2.0, 1.0, 0.5) LIMIT 500",
            (q,),
        ).fetchall()
    else:
        rows = db.execute("SELECT data FROM product LIMIT 5000").fetchall()
    products = [json.loads(d) for (d,) in rows]
    if q:
        terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) >= 3]
        # stable sort: relevance first, bm25 order breaks ties
        products.sort(key=lambda p: -_relevance(p, terms))
    out = []
    for p in products:
        if p["deleted"]:
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
