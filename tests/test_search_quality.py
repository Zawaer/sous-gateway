"""Search relevance on the real catalog: does the top of the list match what was asked?

Needs data/products.json (not in the repo, so CI skips this). Each case is a
buyer query (en / fi / sv) and a predicate every top-3 result should satisfy.
Run `uv run python tests/test_search_quality.py` for a per-query report.
"""

from __future__ import annotations

import re

import pytest

from sous import config

pytestmark = pytest.mark.skipif(not config.PRODUCTS_JSON.exists(), reason="needs data/products.json")

NON_FOOD = {"20", "30", "32", "63", "64", "67", "77", "90"}


def _food(p: dict) -> bool:
    return p["category"][:2] not in NON_FOOD


def fi(pattern: str, *, exclude: str | None = None, food: bool | None = True):
    """Predicate: the Finnish name matches pattern (and not exclude), food or not."""
    rx, ex = re.compile(pattern, re.I), re.compile(exclude, re.I) if exclude else None

    def ok(p: dict) -> bool:
        n = p["names"]["fi"] or p["name"]
        if not rx.search(n) or (ex and ex.search(n)):
            return False
        return food is None or _food(p) == food

    return ok


BUTTER = fi(r"(\b|kirnu)voi\b", exclude=r"levite|maapähkinä|pähkinä|pavut")
MILK = fi(r"maito\b", exclude=r"kondens|jauhe|suklaa|kaakao|juoma|kahvi")
CREAM = fi(r"kerma\b", exclude=r"jäätelö|juusto|kastike|lautasliina|kookos|kasvi")
CHEESE = fi(r"juusto\b|juustoraaste|\bjuusto", exclude=r"sipsi|chips|kastike|\bvoi\b|keksi|mac ")
EGG = fi(r"kananmun|munamassa|\bmunat?\b|keltuai|valkuai", exclude=r"levite|mauste|pasta|nuudel")
SALMON = fi(r"(^|\s|-)lohi|lohifilee|lohikuutio|\blohi", exclude=r"kirjolohi|laatikko|piirak|keitto|veitsi")
POTATO = fi(r"\bperuna\b", exclude=r"laatikko|muusi|kroketti|lohko")

CASES: list[tuple[str, object]] = [
    # English
    ("butter", BUTTER),
    ("milk", MILK),
    ("cream", CREAM),
    ("cheese", CHEESE),
    ("egg", EGG),
    ("eggs", EGG),
    ("flour", fi(r"jauhot?\b")),
    ("wheat flour", fi(r"vehnäjauho")),
    ("potato", POTATO),
    ("chicken", fi(r"broiler|\bkana", exclude=r"mauste|kastike|spice|liemi|fond|piirak")),
    ("salmon", SALMON),
    ("rice", fi(r"riisi\b", exclude=r"jauho|kakku|nuudel|puuro|piirak|viini")),
    ("sugar", fi(r"sokeri\b", exclude=r"siirappi|vanilja")),
    ("coffee", fi(r"kahvi", exclude=r"kerma|muki|kuppi|suodatinpaperi|suodatinpussi|lasi|maito")),
    ("bread", fi(r"leipä|leivät|sämpylä|patonki|limppu", exclude=r"kori|veitsi|lauta|jauho|mix|seos")),
    ("tomato", fi(r"tomaatti\b|tomaatit\b", exclude=r"sose|ketsuppi|kastike|murska")),
    ("olive oil", fi(r"oliiviöljy")),
    ("rapeseed oil", fi(r"rypsiöljy|rapsiöljy")),
    ("oat milk", fi(r"kaurajuoma|kauramaito")),
    ("lactose free milk", fi(r"laktoositon", exclude=r"kondens|jauhe|suklaa|kahvi|juoma|kerma|jogurtti|juusto")),
    ("yogurt", fi(r"jogurtti\b")),
    ("whipping cream", fi(r"vispikerma|kuohukerma|vispautuva")),
    ("napkin", fi(r"lautasliina|liina|napkin", food=False)),
    # Finnish
    ("maito", MILK),
    ("voi", BUTTER),
    ("kerma", CREAM),
    ("juusto", CHEESE),
    ("kananmuna", EGG),
    ("lohi", SALMON),
    ("peruna", POTATO),
    ("laktoositon maito", fi(r"laktoositon", exclude=r"kondens|jauhe|suklaa|kahvi|juoma|kerma|jogurtti|juusto")),
    ("kaurajuoma", fi(r"kaurajuoma")),
    # Swedish
    ("mjölk", MILK),
    ("smör", BUTTER),
    ("grädde", CREAM),
    ("ost", CHEESE),
    ("ägg", EGG),
    ("potatis", POTATO),
    ("lax", SALMON),
]


def evaluate(cases=CASES, k: int = 3) -> tuple[float, float, list[tuple[str, float, bool, list[str]]]]:
    """(mean precision@k, top-1 hit rate, per-query rows)."""
    from sous import catalog

    rows = []
    for q, ok in cases:
        top = catalog.search(q, limit=k)
        hits = [ok(p) for p in top]
        prec = sum(hits) / k
        rows.append((q, prec, bool(hits and hits[0]), [p["names"]["fi"] or p["name"] for p in top]))
    p_at_k = sum(r[1] for r in rows) / len(rows)
    top1 = sum(r[2] for r in rows) / len(rows)
    return p_at_k, top1, rows


def test_search_quality():
    p_at_3, top1, rows = evaluate()
    misses = [(q, names) for q, prec, hit, names in rows if not hit]
    assert top1 >= 0.95, f"top-1 {top1:.2f}, misses: {misses}"
    assert p_at_3 >= 0.9, f"P@3 {p_at_3:.2f}"


if __name__ == "__main__":
    p_at_3, top1, rows = evaluate()
    for q, prec, hit, names in rows:
        print(f"{'ok ' if hit else 'MISS'} {prec:.2f}  {q:20s} {' | '.join(n[:40] for n in names)}")
    print(f"\nP@3 {p_at_3:.3f}  top-1 {top1:.3f}  ({len(rows)} queries)")
