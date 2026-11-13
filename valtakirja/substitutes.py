"""Substitutes within Valio Aimo's own assortment, ranked good / better / best.

Valio Aimo's official substitution map covers only 196 products, so we build our
own: same category, same unit of measure (L or KG), same temperature class, and
everything the original promised (its nutritional claims and the mandate's
allergen rules). Each candidate lists its compromises against the original in
plain language; the tier follows from them:

  best:   no compromises
  better: only minor ones (somewhat dearer, a certification label lost, different pack)
  good:   still usable, but with a real trade-off (much dearer, later delivery,
          low reliability, allergen data missing -> the mandate will ask a human)

Static filtering first; live price and availability only for the top ~25.
"""

from __future__ import annotations

import json
import re
from datetime import date

from . import catalog, quote as Q
from .mandate import Mandate

SHORTLIST = 25
_WORD = re.compile(r"[a-zåäö]{3,}")
_STOP = {"the", "and", "with", "ltd", "free", "laktoositon", "lactose", "uht", "pack", "pcs"}


def _words(p: dict) -> set[str]:
    text = " ".join(v for v in p["names"].values() if v).lower()
    return {w for w in _WORD.findall(text) if w not in _STOP}


def _same_category(category: str) -> list[dict]:
    rows = catalog._db().execute(
        "SELECT data FROM product WHERE json_extract(data, '$.category') = ?", (category,)).fetchall()
    return [json.loads(d) for (d,) in rows]


def _static_score(orig: dict, cand: dict, ow: set[str]) -> float:
    cw = _words(cand)
    sim = len(ow & cw) / max(1, len(ow | cw))
    score = 3 * sim
    score += 2 if cand["id"] in orig["official_substitutes"] else 0
    score += 0.5 * len(set(orig["labels"]) & set(cand["labels"]))
    score += 0.5 if cand["allergens"]["declared"] else 0
    score += 0.3 if cand["brand"] and cand["brand"] == orig["brand"] else 0
    return score


def suggest(
    product_id: str,
    delivery_date: date,
    amount: float,
    unit: str | None,
    mandate: Mandate | None = None,
    avoid_allergens: list[str] | None = None,
    limit: int = 3,
) -> dict:
    orig = catalog.get(product_id)
    if not orig:
        raise KeyError(product_id)
    avoid = sorted(set(avoid_allergens or []) | set(mandate.avoid_allergens if mandate else []))
    required_claims = set(orig["claims"]) & {
        "FREE_FROM_LACTOSE", "FREE_FROM_GLUTEN", "VEGAN", "VEGETARIAN", "LOW_LACTOSE", "FREE_FROM_MILK",
        "FREE_FROM_EGGS", "FREE_FROM_NUTS"} | set(mandate.required_claims if mandate else [])
    measure = (orig["size_from_name"] or [None, None])[1]

    # 1. static filter: hard constraints never relaxed
    pool, excluded = [], {"allergen": 0, "claims": 0, "measure_or_temperature": 0}
    for c in _same_category(orig["category"]):
        if c["id"] == orig["id"] or c["deleted"]:
            continue
        if orig["temperature"] and c["temperature"] and c["temperature"] != orig["temperature"]:
            excluded["measure_or_temperature"] += 1
            continue
        if measure and c["size_from_name"] and c["size_from_name"][1] != measure:
            excluded["measure_or_temperature"] += 1
            continue
        if not required_claims <= set(c["claims"]):
            excluded["claims"] += 1
            continue
        if avoid and catalog.allergen_status(c, avoid, mandate.allow_may_contain if mandate else False)["status"] in (
                "contains", "may_contain"):
            excluded["allergen"] += 1
            continue
        pool.append(c)
    ow = _words(orig)
    pool.sort(key=lambda c: -_static_score(orig, c, ow))
    shortlist = pool[:SHORTLIST]

    # 2. live price and availability for the original + shortlist (two batched calls)
    offers, _ = Q.offers([orig["id"]] + [c["id"] for c in shortlist], delivery_date, amount, unit)
    by_id = {o["product_id"]: o for o in offers}
    o_orig = by_id.get(orig["id"])
    ref_price = (o_orig or {}).get("comparison_price") or {}
    ref_cp = ref_price.get("ex_vat")

    # 3. compromises -> tier
    options = []
    for c in shortlist:
        o = by_id.get(c["id"])
        if not o or not o["orderable"]:
            continue
        minor, major = [], []
        cp = (o.get("comparison_price") or {}).get("ex_vat")
        delta = None
        if cp is not None and ref_cp:
            delta = (cp - ref_cp) / ref_cp
            if delta > 0.25:
                major.append(f"{delta:+.0%} per {ref_price.get('per')} vs the original")
            elif delta > 0.05:
                minor.append(f"{delta:+.0%} per {ref_price.get('per')} vs the original")
        lost = sorted(set(orig["labels"]) - set(c["labels"]))
        if lost:
            minor.append("loses label " + ", ".join(lost))
        if (o_orig or {}).get("sales_unit") and o["sales_unit"] != o_orig["sales_unit"]:
            minor.append(f"sold per {o['sales_unit']} instead of {o_orig['sales_unit']}")
        for i in o["issues"]:
            if i["code"] in ("LATER_DATE", "LOW_RELIABILITY", "PRICE_EXPIRES"):
                major.append(i["message"])
            elif i["code"] in ("STOCK_UNKNOWN", "PARTIAL_STOCK"):
                minor.append(i["message"])
        allergen = catalog.allergen_status(c, avoid, mandate.allow_may_contain if mandate else False) if avoid else {"status": "safe", "codes": []}
        if allergen["status"] == "unknown":
            major.append("no allergen data: a human must confirm it is free of "
                         + ", ".join(catalog.ALLERGENS.get(a, a) for a in avoid))
        tier = "best" if not minor and not major else ("better" if not major else "good")
        options.append({
            "tier": tier,
            "product_id": c["id"],
            "name": c["name"],
            "brand": c["brand"],
            "official_substitute": c["id"] in orig["official_substitutes"],
            "match_score": round(_static_score(orig, c, ow), 2),
            "price_change": round(delta, 3) if delta is not None else None,
            "compromises": major + minor,
            "allergen_check": allergen,
            "offer": o,
        })
    rank = {"best": 0, "better": 1, "good": 2}
    options.sort(key=lambda x: (rank[x["tier"]], -x["match_score"],
                                (x["offer"]["comparison_price"] or {}).get("ex_vat") or 1e9))
    # one per tier first (good / better / best), then the rest
    picked, seen = [], set()
    for x in options:
        if x["tier"] not in seen:
            picked.append(x)
            seen.add(x["tier"])
    picked += [x for x in options if x not in picked]
    return {
        "original": {"product_id": orig["id"], "name": orig["name"],
                     "why_substitute": [i["message"] for i in (o_orig or {}).get("issues", [])
                                        if i["severity"] == "blocking"] or ["(original is orderable)"],
                     "comparison_price": ref_price},
        "constraints": {"required_claims": sorted(required_claims), "avoid_allergens": avoid,
                        "same_category": orig["category"], "measure": measure, "temperature": orig["temperature"]},
        "considered": {"in_category": len(pool) + sum(excluded.values()), "excluded": excluded,
                       "priced_live": len(shortlist)},
        "options": picked[:limit],
        "no_safe_substitute": None if picked else (
            "No product in this category meets every constraint and can be delivered. "
            "Nothing will be swapped automatically; a human needs to decide."),
        "next_step": "Request a quote with substitute_for set to the original product id; "
                     "the mandate decides whether a human must approve the substitution.",
    }
