"""How far can the Gemini Decisions API be trusted on allergens? Measured where we know the answer.

1. Ground truth: food products that DO declare allergens. For each of the main allergen families the declaration
   says contains (positive) or the product is declared without it (negative); may-contain is skipped. The API sees
   only what it sees for an undeclared product (names, brand, key ingredients: sous/allergen_ai.product_text), and
   we count, at the rule's threshold (allergen_ai.LIKELY), how often "likely contains" is right (precision: a wrong
   one refuses a safe product) and how many real ones it catches (recall).
2. Reach: every food product that declares NO allergens, asked the same questions: how many would rule
   allergen.likely refuse for a buyer avoiding each family.

    uv run python scripts/allergen_ai_eval.py --sample 400        # needs data/ and GEMINI_API_KEY
    uv run python scripts/allergen_ai_eval.py --sample 50 --no-undeclared

Writes evals/allergen_ai_results.json. One API request per product (all families as questions in it); answers are
also stored in the allergen_ai cache of a fresh SQLite file, never data/state.db.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SOUS_STATE_DB", str(Path(tempfile.mkdtemp()) / "allergen-eval.db"))

from sous import allergen_ai as AI, catalog, config  # noqa: E402

FAMILIES = ["AM", "AN", "AW", "AE", "AP", "AF", "AC", "AY", "AS", "BC", "BM"]


def truth(p: dict, code: str) -> bool | None:
    st = catalog.allergen_status(p, [code], allow_may_contain=True)
    return {"contains": True, "safe": False}.get(st["status"])


def ask(p: dict) -> dict[str, float] | None:
    for attempt in range(4):
        try:
            return {c: a["p"] for c, a in AI.ask(AI.product_text(p), FAMILIES).items()}
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                print(f"  ! {p['id']}: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
                return None
            time.sleep(2 * (attempt + 1))


def run(products: list[dict], workers: int) -> list[tuple[dict, dict | None]]:
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(zip(products, ex.map(ask, products)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sample", type=int, default=400, help="declared food products to score (ground truth)")
    ap.add_argument("--no-undeclared", action="store_true", help="skip the reach count over undeclared products")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--seed", type=int, default=14)
    ap.add_argument("--out", default=str(ROOT / "evals" / "allergen_ai_results.json"))
    a = ap.parse_args()
    if not os.getenv("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is not set", file=sys.stderr)
        return 2

    products = [json.loads(r[0]) for r in catalog._db().execute("SELECT data FROM product")]
    food = [p for p in products if not p.get("deleted") and catalog.is_food(p)]
    declared = [p for p in food if any(truth(p, c) is not None for c in FAMILIES)
                and (p["allergens"]["contains"] or "X99" in p["allergens"]["free_from"])]
    rng = random.Random(a.seed)
    # Half the sample has at least one of the families, so precision is measured on enough positives.
    pos = [p for p in declared if any(truth(p, c) for c in FAMILIES)]
    neg = [p for p in declared if p not in pos]
    sample = rng.sample(pos, min(len(pos), a.sample // 2)) + rng.sample(neg, min(len(neg), a.sample - a.sample // 2))
    t0 = time.time()
    print(f"ground truth: {len(sample)} declared food products ({len(declared)} eligible), {len(FAMILIES)} families")
    scored = run(sample, a.workers)

    per = {c: {"tp": 0, "fp": 0, "fn": 0, "tn": 0} for c in FAMILIES}
    wrong: list[dict] = []
    for p, ans in scored:
        if ans is None:
            continue
        for c in FAMILIES:
            t = truth(p, c)
            if t is None:
                continue
            hit = ans[c] >= AI.LIKELY
            key = ("tp" if t else "fp") if hit else ("fn" if t else "tn")
            per[c][key] += 1
            if key == "fp":
                wrong.append({"product_id": p["id"], "name": p.get("name"), "code": c, "p": round(ans[c], 3)})
    tot = {k: sum(v[k] for v in per.values()) for k in ("tp", "fp", "fn", "tn")}

    def rates(m):
        pr = m["tp"] / (m["tp"] + m["fp"]) if m["tp"] + m["fp"] else None
        rc = m["tp"] / (m["tp"] + m["fn"]) if m["tp"] + m["fn"] else None
        return {**m, "precision": round(pr, 3) if pr is not None else None,
                "recall": round(rc, 3) if rc is not None else None}

    out = {"generated_at": datetime.now(config.TZ).isoformat(timespec="seconds"),
           "model": AI.MODEL, "threshold": AI.LIKELY, "families": FAMILIES,
           "ground_truth": {"products_scored": sum(1 for _, x in scored if x is not None),
                            "products_failed": sum(1 for _, x in scored if x is None),
                            "eligible_declared_food_products": len(declared), "seed": a.seed,
                            "total": rates(tot), "per_family": {c: rates(m) for c, m in per.items()},
                            "false_likely": wrong[:50]},
           "definitions": {
               "positive": "the product's declaration says it contains the family (catalog.allergen_status 'contains')",
               "negative": "the product has an allergen list without the family (status 'safe'); may-contain skipped",
               "precision": "of 'likely contains' (p >= threshold), the share the declaration confirms; a miss here "
                            "would refuse a safe product",
               "recall": "of declared contains, the share the API marks likely from names and ingredients alone"}}
    try:
        from sous.proof import run_provenance
        out["provenance"] = run_provenance()
    except Exception:  # noqa: BLE001
        pass

    if not a.no_undeclared:
        undeclared = [p for p in food if not p["allergens"].get("declared")]
        print(f"reach: {len(undeclared)} undeclared food products")
        reach = run(undeclared, a.workers)
        flagged = {c: [] for c in FAMILIES}
        for p, ans in reach:
            for c in FAMILIES if ans else ():
                if ans[c] >= AI.LIKELY:
                    flagged[c].append(p["id"])
        any_flag = {pid for ids in flagged.values() for pid in ids}
        out["undeclared"] = {"products": len(undeclared),
                             "products_failed": sum(1 for _, x in reach if x is None),
                             "likely_any_family": len(any_flag),
                             "likely_per_family": {c: len(v) for c, v in flagged.items()},
                             "examples": [{"product_id": p["id"], "name": p.get("name"),
                                           "likely": {c: round(ans[c], 2) for c in FAMILIES if ans[c] >= AI.LIKELY}}
                                          for p, ans in reach if ans and any(ans[c] >= AI.LIKELY for c in FAMILIES)][:25]}

    out["seconds"] = round(time.time() - t0, 1)
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    t = out["ground_truth"]["total"]
    print(f"precision {t['precision']}  recall {t['recall']}  (tp {t['tp']} fp {t['fp']} fn {t['fn']} tn {t['tn']})")
    if "undeclared" in out:
        u = out["undeclared"]
        print(f"undeclared: {u['likely_any_family']} of {u['products']} likely contain at least one family")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
