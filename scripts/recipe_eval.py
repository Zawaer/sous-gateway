"""Recipe planning eval: menu requests (evals/recipe_cases.yaml) through agent/recipe.py, per provider.

Checks per case: the dishes and portions were understood, the delivery date, every main ingredient is in the
plan (quoted, substituted, on hand or reported as not covered), the quantities are within +-20 % of the
expected kg/L, stock on hand was subtracted, no quoted line breaks the mandate's allergen rules, and the
mandate preview tier is one of the expected ones.

    uv run python scripts/recipe_eval.py                                   # table provider (no model)
    uv run python scripts/recipe_eval.py --providers table gemini verda:kimi-k3-1-pro
    uv run python scripts/recipe_eval.py --providers gemini --interpret-only   # no gateway, no Valio calls

Providers: table | gemini[:model] | verda[:model] | local[:model]; a provider without an API key is skipped.
--interpret-only checks the interpretation and quantities only (zero Valio calls; offline with the table).
Writes evals/recipe_results.json and evals/recipe_results.md. Every case gets a fresh SQLite state.
Live Valio calls: one batched price + availability call per basket quote, plus substitutes; cached in-process
across providers for up to 10 minutes (prices) / 1 minute (availability).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CASES = ROOT / "evals" / "recipe_cases.yaml"
OUT_JSON = ROOT / "evals" / "recipe_results.json"
OUT_MD = ROOT / "evals" / "recipe_results.md"
TOLERANCE = 0.20
WEEKDAY = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}


def expected_date(spec: str | None, today: date) -> date | None:
    if not spec:
        return None
    m = re.fullmatch(r"next (\w+)", spec.strip().lower())
    if m and m[1] in WEEKDAY:
        return today + timedelta(days=(WEEKDAY[m[1]] - today.weekday()) % 7 or 7)
    return date.fromisoformat(spec)


def check(case: dict, p, today: date, mandate: dict | None, interpret_only: bool) -> dict:
    """Score one plan. Returns {checks: {name: bool}, notes: [...]}."""
    from agent import recipe as R
    from sous import catalog

    checks: dict[str, bool] = {}
    notes: list[str] = []
    if p.spec is None:
        return {"checks": {"plan made": False}, "notes": [p.error or "no plan"]}
    checks["plan made"] = True
    aliases = {k: [R._norm(a) for a in r["aliases"]] for k, r in R.table()["recipes"].items()}
    names = [R._norm(d.name) for d in p.spec.dishes]
    for dish in case["dishes"]:
        checks[f"dish {dish}"] = any(a in n for n in names for a in aliases[dish])
    checks["portions"] = all((d.portions or p.spec.portions) == case["portions"] for d in p.spec.dishes)
    want = expected_date(case.get("delivery_date"), today)
    if want:
        checks["delivery date"] = p.spec.delivery_date == want
        if not checks["delivery date"]:
            notes.append(f"date {p.spec.delivery_date} != {want}")
    needs = {n.key: n for n in p.needs}
    ok_status = {"quoted", "quoted_with_substitute", "covered_by_stock", "uncovered", "not_found",
                 "ruled_out_by_mandate"} if not interpret_only else {"pending", "covered_by_stock"}
    present = qty = 0
    for key, amount in case["expect_kg_l"].items():
        n = needs.get(key)
        if n is None or n.status not in ok_status:
            notes.append(f"{key}: missing from the plan" if n is None else f"{key}: status {n.status}")
            continue
        present += 1
        if n.unit in ("KG", "L") and abs(n.needed - amount) <= TOLERANCE * amount:
            qty += 1
        else:
            notes.append(f"{key}: {n.needed:.2f} {n.unit} vs expected {amount:g} (+-20 %)")
    total = len(case["expect_kg_l"])
    checks["main ingredients in plan"] = present == total
    checks["quantities within 20 %"] = qty == total
    for key in case.get("on_hand", []):
        n = needs.get(key)
        checks[f"on hand: {key}"] = bool(n) and n.status == "covered_by_stock"
    if interpret_only:
        return {"checks": checks, "notes": notes, "ingredients_ok": f"{present}/{total}", "quantities_ok": f"{qty}/{total}"}
    q = p.quote
    checks["one basket quote"] = q is not None
    if q is None:
        notes.append(p.error or "no quote")
        return {"checks": checks, "notes": notes}
    covered = sum(1 for k in case["expect_kg_l"] if (n := needs.get(k)) and n.status in
                  ("quoted", "quoted_with_substitute", "covered_by_stock"))
    avoid = (mandate or {}).get("avoid_allergens") or []
    bad = []
    if avoid:
        for l in q["lines"]:
            prod = catalog.get(l["product_id"]) or {}
            st = catalog.allergen_status(prod, avoid, (mandate or {}).get("allow_may_contain", False))
            if st["status"] in ("contains", "may_contain"):
                bad.append(f"{l['name']} ({st['status']} {','.join(st['codes'])})")
    checks["mandate allergens respected"] = not bad
    notes += [f"allergen: {b}" for b in bad]
    tier = (q.get("mandate_preview") or {}).get("outcome")
    checks["tier as expected"] = tier in case["tier_in"]
    if not checks["tier as expected"]:
        notes.append(f"tier {tier} not in {case['tier_in']}")
    return {"checks": checks, "notes": notes, "tier": tier, "total_ex_vat": q["total_ex_vat"],
            "quote_id": q["id"], "lines": len(q["lines"]), "covered": f"{covered}/{total}",
            "ingredients_ok": f"{present}/{total}", "quantities_ok": f"{qty}/{total}"}


def run_provider(provider: str, cases: list[dict], state_dir: Path, interpret_only: bool, today: date) -> dict | None:
    from agent import recipe as R
    from agent.buyer import MCPGateway
    from scripts.llm_eval import fresh_state

    name, _, model = provider.partition(":")
    llm = None
    if name != "table":
        from agent.llm import ChatClient, config_from_env, key_present
        cfg = config_from_env(name, model or None)
        if cfg.provider != "local" and not key_present(cfg.provider):
            print(f"[{provider}] no API key: skipped", file=sys.stderr)
            return None
        llm = ChatClient(cfg)
    label = "table" if llm is None else llm.cfg.label
    results = []
    gw = None
    if not interpret_only:
        from sous.mcp_server import mcp
        gw = MCPGateway(mcp)
    try:
        for i, case in enumerate(cases):
            t0 = time.perf_counter()
            if interpret_only:
                use = R.LLMUse()
                p = R.Plan(request=case["request"], interpretation="", mandate_id=case["mandate"], agent_id=case["agent"],
                           llm=use)
                try:
                    p.spec, p.interpretation = R.interpret(case["request"], today, llm, use)
                    p.needs = R.compute_needs(p.spec)
                except R.InterpretError as e:
                    p.error = str(e)
                mandate = None
            else:
                fresh_state(state_dir / f"{label.replace(':', '_')}-{case['id']}.db", {})
                from sous import service as S
                mandate = S.get_mandate(case["mandate"])["mandate"]
                p = R.plan(case["request"], gateway=gw, mandate_id=case["mandate"], agent_id=case["agent"], llm=llm,
                           today=today)
            r = check(case, p, today, mandate, interpret_only)
            r.update({"id": case["id"], "request": case["request"], "interpretation": p.interpretation,
                      "passed": all(r["checks"].values()), "seconds": round(time.perf_counter() - t0, 1),
                      "valio_api_calls": p.trace.valio_api_calls, "llm_calls": p.llm.calls,
                      "tokens": {"prompt": p.llm.prompt_tokens, "completion": p.llm.completion_tokens},
                      "plan": p.to_dict()})
            results.append(r)
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"[{label}] {case['id']} {mark} {p.interpretation} tier={r.get('tier')} "
                  f"ingredients={r.get('ingredients_ok')} qty={r.get('quantities_ok')} valio={p.trace.valio_api_calls} "
                  + "; ".join(r["notes"][:4]), file=sys.stderr)
    finally:
        if gw is not None:
            gw.close()
    return {"provider": label, "interpret_only": interpret_only, "today": today.isoformat(),
            "passed": sum(r["passed"] for r in results), "cases": len(results),
            "valio_api_calls": sum(r["valio_api_calls"] for r in results),
            "llm_calls": sum(r["llm_calls"] for r in results), "results": results}


def report(runs: list[dict]) -> str:
    out = ["# Recipe planning eval", "",
           "Menu requests from `evals/recipe_cases.yaml` through `agent/recipe.py` (`scripts/recipe_eval.py`). "
           "The model (or the curated table) only interprets the request; quantities, product choice constraints "
           "and the mandate tier are code and the gateway.", "",
           "| provider | mode | passed | Valio calls | LLM calls |", "|---|---|---|---|---|"]
    for r in runs:
        out.append(f"| {r['provider']} | {'interpret only' if r['interpret_only'] else 'full (quote)'} | "
                   f"{r['passed']}/{r['cases']} | {r['valio_api_calls']} | {r['llm_calls']} |")
    for r in runs:
        out += ["", f"## {r['provider']}", "", "| case | pass | interpreted by | ingredients | quantities | tier | total € | notes |",
                "|---|---|---|---|---|---|---|---|"]
        for c in r["results"]:
            notes = "; ".join(c["notes"][:3]).replace("|", "/")
            out.append(f"| {c['id']} | {'yes' if c['passed'] else 'no'} | {c['interpretation'][:40]} | "
                       f"{c.get('ingredients_ok', '-')} | {c.get('quantities_ok', '-')} | {c.get('tier') or '-'} | "
                       f"{c.get('total_ex_vat', '-')} | {notes} |")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--providers", nargs="+", default=["table"])
    ap.add_argument("--cases", nargs="*", help="case ids (default: all)")
    ap.add_argument("--interpret-only", action="store_true", help="no gateway / Valio calls")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in Helsinki)")
    ap.add_argument("--state-dir")
    a = ap.parse_args(argv)
    state_dir = Path(a.state_dir or tempfile.mkdtemp(prefix="sous-recipe-eval-")).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    os.environ["SOUS_STATE_DB"] = str(state_dir / "init.db")
    os.environ["SOUS_DB_URL"] = ""
    import yaml
    from agent.buyer import helsinki_today
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]
    if a.cases:
        cases = [c for c in cases if c["id"] in a.cases]
    today = date.fromisoformat(a.today) if a.today else helsinki_today()
    runs = [r for p in a.providers if (r := run_provider(p, cases, state_dir, a.interpret_only, today))]
    OUT_JSON.write_text(json.dumps(runs, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    OUT_MD.write_text(report(runs), encoding="utf-8")
    print(report(runs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
