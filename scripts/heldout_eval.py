"""Held-out evaluation: 15 buyer requests written blind (evals/heldout/cases.yaml), scored by evals/heldout/rubric.yaml.

The cases were written from the challenge brief alone, before reading Sous's code, evals or docs, and are frozen:
nothing in Sous may be tuned on them. Run through the LLM eval harness:

    uv run python scripts/heldout_eval.py --provider gemini --agent sous --repeat 3
    uv run python scripts/heldout_eval.py --report     # print the docs/LLM_EVAL.md section from the results

(In CI: workflow_dispatch with llm_only + heldout, see the llm-eval job.) It reuses scripts/llm_eval.py's agent
runner (run_case), so the agent, its tools and the gateway are exactly those of the main LLM eval.

Per case run it records what the agent quoted and ordered and what the gateway decided, and scores:

- matched with no correction: expected ok -> the quote has every expected product kind at a plausible quantity, no
  unwanted or extra line, the asked delivery date, and an order was placed (auto-ordered or waiting for a human's
  yes: an approval click is not a correction); expected held/refused -> nothing went through without a human.
- false auto-approval: an order went through with no human although it should not have (expected held/refused,
  over the stated budget, or a line the rubric marks unsafe: a forbidden allergen, an ignored milk allergy, animal
  products at a vegan event).
- held/refused correctly: of the expected held/refused cases, nothing went through without a human.
- no-gateway floor: the same scoring if every order the agent PLACED had simply gone through (no mandate engine):
  how many false auto-approvals the raw agent would have produced.
- cookable (menu cases): every required ingredient of the dish is in the quote at >= the low end of its range.

Writes evals/heldout/results.<model>[.sous-agent].json; docs/LLM_EVAL.md gets a "Held-out" section from them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
HELDOUT = ROOT / "evals" / "heldout"
CASES = HELDOUT / "cases.yaml"
RUBRIC = HELDOUT / "rubric.yaml"
AGENT_ID = "heldout-agent"
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
LKG = {"L": 1.0, "KG": 1.0, "DL": 0.1, "CL": 0.01, "ML": 0.001, "G": 0.001, "GRM": 0.001, "LTR": 1.0, "KGM": 1.0}
PCE = {"ST", "KPL", "PCE", "PC", "PCS", "EA", "H87"}


def load() -> tuple[list[dict], dict]:
    import yaml

    cases = yaml.safe_load(CASES.read_text())["cases"]
    rubric = yaml.safe_load(RUBRIC.read_text())
    return cases, rubric


def resolve_date(spec: str | None, today: date) -> date | None:
    if not spec:
        return None
    if spec == "tomorrow":
        return today + timedelta(days=1)
    wd = WEEKDAYS.index(spec)
    return today + timedelta(days=(wd - today.weekday()) % 7 or 7)


# --- product facts ---------------------------------------------------------------------------

def _product(pid: str) -> dict | None:
    try:
        from sous import catalog
        return catalog.get(str(pid))
    except Exception:  # noqa: BLE001  (no catalog: the unit tests)
        return None


def _text(line: dict, p: dict | None) -> str:
    names = (p or {}).get("names") or {}
    return " | ".join(x for x in (names.get("fi"), names.get("en"), line.get("name")) if x).lower()


def _milk_status(p: dict | None) -> str:
    if not p:
        return "unknown"
    from sous import catalog
    return catalog.allergen_status(p, ["AM"], False)["status"]


def _allergen_hit(p: dict | None, codes: list[str], strict: bool) -> bool:
    if not p or not codes:
        return False
    from sous import catalog
    st = catalog.allergen_status(p, codes, False)["status"]
    return st == "contains" or (strict and st in ("may_contain", "unknown"))


def line_amount(line: dict, p: dict | None) -> tuple[str, float] | None:
    """(unit class 'LKG' or 'PCE', amount) the line delivers, or None when it can't be told."""
    cov = line.get("covers") or {}
    u = str(cov.get("unit") or "").upper()
    if cov.get("amount") is not None:
        if u in LKG:
            return "LKG", float(cov["amount"]) * LKG[u]
        if u in PCE:
            return "PCE", float(cov["amount"])
    if not p:
        return None
    qty = float(line.get("quantity") or 0)
    per = (p.get("units") or {}).get(line.get("sales_unit")) or (p.get("units") or {}).get(p.get("sales_unit"))
    base = str(p.get("base_unit") or "").upper()
    if per is None:
        return None
    n = qty * float(per)
    if base in LKG:
        return "LKG", n * LKG[base]
    size = p.get("size_from_name")
    if size and str(size[1]).upper() in LKG:
        return "LKG", n * float(size[0]) * LKG[str(size[1]).upper()]
    if base in PCE:
        return "PCE", n
    return None


# --- matching ----------------------------------------------------------------------------------

def _re(pat: str | None, text: str) -> bool:
    return bool(pat) and re.search(pat, text, re.I) is not None


def line_matches(spec: dict, line: dict, p: dict | None) -> bool:
    t = _text(line, p)
    if not _re(spec.get("match"), t):
        return False
    if any(not _re(a, t) for a in spec.get("all") or []):
        return False
    if spec.get("exclude") and _re(spec["exclude"], t):
        return False
    if spec.get("claims_any") or spec.get("name_any"):
        claims = set((p or {}).get("claims") or [])
        if not (claims & set(spec.get("claims_any") or []) or _re(spec.get("name_any"), t)):
            return False
    if spec.get("milk_free") and _milk_status(p) != "safe":
        return False
    return True


def _qty_ok(qty: dict | None, sums: dict[str, float], low_only: bool = False) -> bool:
    if not qty:
        return True
    for unit, (lo, hi) in qty.items():
        cls = "PCE" if unit == "PCE" else "LKG"
        if cls in sums and sums[cls] >= lo - 1e-9 and (low_only or sums[cls] <= hi + 1e-9):
            return True
    return False


def score_item(item: dict, lines: list[tuple[dict, dict | None]]) -> dict:
    best = None
    for alt in item.get("alt") or [item]:
        hit = [(l, p) for l, p in lines if line_matches(alt, l, p)]
        sums: dict[str, float] = {}
        unknown = 0
        for l, p in hit:
            a = line_amount(l, p)
            if a is None:
                unknown += 1
            else:
                sums[a[0]] = sums.get(a[0], 0.0) + a[1]
        res = {"kind": bool(hit), "lines": [l["product_id"] for l, _ in hit],
               "amount": {k: round(v, 2) for k, v in sums.items()}, "unknown_amount_lines": unknown,
               "met": bool(hit) and _qty_ok(alt.get("qty"), sums),
               "enough": bool(hit) and _qty_ok(alt.get("qty"), sums, low_only=True)}
        if best is None or (res["met"], res["enough"], res["kind"]) > (best["met"], best["enough"], best["kind"]):
            best = res
    return {"name": item["name"], "optional": bool(item.get("optional")), **best}


def forbidden_lines(rules: list[dict], lines: list[tuple[dict, dict | None]], items: dict[str, dict]) -> list[dict]:
    out = []
    for rule in rules or []:
        if rule.get("unless_item") and (items.get(rule["unless_item"]) or {}).get("kind"):
            continue
        for l, p in lines:
            t = _text(l, p)
            hit = _re(rule.get("match"), t) and not _re(rule.get("unless"), t)
            if hit and rule.get("not_milk_free"):
                hit = _milk_status(p) != "safe"
            if not hit and rule.get("allergens"):
                hit = _allergen_hit(p, rule["allergens"], bool(rule.get("strict")))
            if hit:
                out.append({"rule": rule["name"], "product_id": l["product_id"], "name": l.get("name"),
                            "unsafe": bool(rule.get("unsafe"))})
    return out


def score_quote(rub: dict, quote: dict | None, today: date) -> dict:
    """Content of one quote against the rubric: items, unwanted/unsafe lines, extra lines, date, budget."""
    if quote is None:
        return {"quote": None}
    lines = [(l, _product(l["product_id"])) for l in quote.get("lines") or []]
    items = [score_item(it, lines) for it in rub.get("items") or []]
    by = {i["name"]: i for i in items}
    forb = forbidden_lines(rub.get("forbid") or [], lines, by)
    matched_ids = {pid for i in items for pid in i["lines"]}
    extra = [l["product_id"] for l, _ in lines if l["product_id"] not in matched_ids
             and l["product_id"] not in {f["product_id"] for f in forb}]
    want = resolve_date(rub.get("date"), today)
    budget = rub.get("budget_eur")
    total = float(quote.get("total_ex_vat") or 0)
    return {"quote": quote.get("id"), "total_ex_vat": total, "items": items, "forbidden": forb, "extra_lines": extra,
            "date_expected": want.isoformat() if want else None,
            "date_quoted": quote.get("requested_delivery_date"),
            "date_ok": want is None or quote.get("requested_delivery_date") == want.isoformat(),
            "over_budget": budget is not None and total > budget + 1e-6,
            "unsafe": any(f["unsafe"] for f in forb)}


def score_run(case: dict, rub: dict, r: dict, quotes: dict[str, dict], today: date) -> dict:
    """Score one case run. `r` is scripts/llm_eval.run_case's record; `quotes` every quote by id."""
    expected = case["expect"]["outcome"]
    orders = [o for o in r.get("orders", []) if o["role"] in ("first", "extra")]
    first = orders[0] if orders else None
    final_q = quotes.get(r["quote"]["id"]) if r.get("quote") else None
    content = score_quote(rub, final_q, today)

    def should_hold(o: dict) -> bool:
        s = score_quote(rub, quotes.get(o.get("quote_id")) or final_q, today)
        return expected != "ok" or bool(s.get("over_budget")) or bool(s.get("unsafe"))

    auto = [o for o in orders if o["status"] == "ordered"]
    false_auto = any(should_hold(o) for o in auto)
    floor_false_auto = any(should_hold(o) for o in orders)  # every placed order would have shipped
    answered = r.get("stop_reason") == "answered"
    if expected == "ok":
        req = [i for i in content.get("items") or [] if not i["optional"]]
        content_ok = (content["quote"] is not None and all(i["met"] for i in req) and not content["forbidden"]
                      and not content["extra_lines"] and content["date_ok"])
        if content.get("over_budget"):
            handled = not auto  # over the stated budget: held or not ordered, and said so
        else:
            handled = first is not None and first["status"] in ("ordered", "pending_approval")
        matched = content_ok and handled and not false_auto
        held_correctly = None
    else:
        content_ok = None
        held_correctly = not auto
        matched = held_correctly and answered
    gw = first["outcome"] if first else None
    cookable = None
    if rub.get("dish"):
        req = [i for i in content.get("items") or [] if not i["optional"]]
        cookable = bool(req) and all(i["enough"] for i in req)
    return {"expected": expected, "gateway_outcome": gw,
            "final_status": first["status"] if first else "no order", "placed_order": bool(orders),
            "auto_ordered": bool(auto), "matched_no_correction": matched, "content_ok": content_ok,
            "held_or_refused_correctly": held_correctly, "false_auto_approval": false_auto,
            "floor_false_auto_approval": floor_false_auto, "cookable": cookable, "answered": answered,
            "content": content}


# --- running -----------------------------------------------------------------------------------

def mandate_for(case: dict, rub: dict, template: dict):
    from sous.mandate import Mandate

    m = dict(template)
    over = dict(rub.get("mandate") or {})
    if over.get("budget_eur") is not None:
        m["auto_approve_limit_eur"] = float(over.pop("budget_eur"))
        m["notify_above_eur"] = None
    m.update(over)
    return Mandate(id=f"M-HO-{case['id']}", agent_id=AGENT_ID, **{**m, "buyer": f"{m['buyer']}: {case['buyer']}"})


def fresh_state(path: Path, mandate) -> None:
    import os

    from sous import config, reservations, service as S, store

    reservations.set_scope(f"heldout-{os.getpid()}-{path.stem}")
    if store._conn is not None:
        try:
            store._conn.close()
        except Exception:  # noqa: BLE001
            pass
    if path.exists():
        path.unlink()
    config.STATE_DB = path
    config.DB_URL = ""
    store._conn = None
    S.issue_mandate(mandate)


def run(a, provider: str, cfg, client, state_dir: Path) -> int:
    from agent.buyer import BuyerAgent, BuyerConfig, MCPGateway, helsinki_today, next_delivery_day
    from agent.sous_agent import SousAgent
    from sous import config, reservations
    from sous.mcp_server import mcp
    from sous.proof import run_provenance
    from scripts.llm_eval import run_case, slug

    if provider == "oracle":
        raise SystemExit("--heldout has no scripted plan: pass a model provider")
    cases, rubric = load()
    if a.cases:
        cases = [c for c in cases if c["id"] in a.cases]
    today = helsinki_today()
    default = next_delivery_day(today)
    runs = []
    print(f"Held-out eval: {cfg.label} ({a.agent} agent); {len(cases)} cases x {a.repeat}; today {today}\n", flush=True)
    with MCPGateway(mcp) as gw:
        for rep in range(1, a.repeat + 1):
            for case in cases:
                rub = rubric["cases"][case["id"]]
                fresh_state(state_dir / f"heldout-{case['id']}-{rep}.db", mandate_for(case, rub, rubric["template"]))
                shim = {"id": f"heldout-{case['id']}", "title": case["buyer"], "request": case["request"],
                        "mandate": f"M-HO-{case['id']}", "agent": AGENT_ID, "unsafe": case["expect"]["outcome"] != "ok"}

                def make_agent(c):
                    bc = BuyerConfig(mandate_id=c["mandate"], agent_id=AGENT_ID, today=today,
                                     default_delivery=default, max_tool_calls=a.max_tool_calls)
                    return (SousAgent if a.agent == "sous" else BuyerAgent)(client, gw, bc)

                t0 = time.perf_counter()
                try:
                    r, brun = run_case(shim, {"delivery_date": default.isoformat()}, make_agent)
                    quotes = {q["id"]: q for _, q in brun.quotes}
                    for o in r["orders"]:
                        o["quote_id"] = next((x["quote_id"] for x in brun.orders if x["id"] == o["id"]), None)
                    s = score_run(case, rub, r, quotes, today)
                except Exception as ex:  # noqa: BLE001  a crash is a failed case, not a crashed run
                    r = {"orders": [], "stop_reason": f"crash: {type(ex).__name__}: {ex}", "explanation": "",
                         "llm": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}, "tool_calls": 0,
                         "valio_api_calls": 0, "transcript": []}
                    s = score_run(case, rub, {"orders": [], "stop_reason": r["stop_reason"]}, {}, today)
                rec = {"id": case["id"], "repeat": rep, "request": case["request"], "expected": case["expect"]["outcome"],
                       **s, "orders": r.get("orders"), "quote": r.get("quote"), "stop_reason": r.get("stop_reason"),
                       "explanation": r.get("explanation"), "llm": r.get("llm"), "tool_calls": r.get("tool_calls"),
                       "valio_api_calls": r.get("valio_api_calls"), "agent_notes": r.get("agent_notes"),
                       "transcript": r.get("transcript"), "seconds": round(time.perf_counter() - t0, 1)}
                runs.append(rec)
                c = s["content"]
                miss = [i["name"] for i in c.get("items") or [] if not i["met"] and not i["optional"]]
                print(f"{'MATCH' if s['matched_no_correction'] else 'miss '} {case['id']} r{rep} exp={s['expected']:7} "
                      f"gw={s['gateway_outcome'] or '-':6} {s['final_status']:16} "
                      f"{'FALSE-AUTO ' if s['false_auto_approval'] else ''}"
                      f"{'missing: ' + '; '.join(miss) if miss else ''}"
                      f"{' forbidden: ' + ', '.join(f['rule'] for f in c.get('forbidden') or []) if c.get('forbidden') else ''}"
                      f"{' extra: ' + ','.join(c['extra_lines']) if c.get('extra_lines') else ''}"
                      f"{'' if c.get('date_ok', True) else ' date ' + str(c.get('date_quoted'))}", flush=True)
    reservations.set_scope(None)
    summary = summarize(runs, cfg)
    suffix = ".sous-agent" if a.agent == "sous" else ""
    out = HELDOUT / f"results.{slug(cfg.model)}{suffix}.json"
    doc = {"generated_at": datetime.now(config.TZ).isoformat(timespec="seconds"), "provenance": run_provenance(),
           "provider": cfg.provider, "model": cfg.model,
           "model_label": cfg.label + (" + sous-agent" if a.agent == "sous" else ""),
           "agent": "sous-agent" if a.agent == "sous" else "generic", "repeats": a.repeat, "run_day": today.isoformat(),
           "summary": summary, "runs": runs}
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=str) + "\n")
    print("\n" + json.dumps(summary, indent=1))
    print(f"\nWrote {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    return 0


def summarize(runs: list[dict], cfg) -> dict:
    n = len(runs)
    pct = lambda a, b: round(100 * a / b, 1) if b else None  # noqa: E731
    ok = [r for r in runs if r["expected"] == "ok"]
    hold = [r for r in runs if r["expected"] != "ok"]
    dish = [r for r in runs if r["cookable"] is not None]
    pt = sum((r.get("llm") or {}).get("prompt_tokens", 0) for r in runs)
    ct = sum((r.get("llm") or {}).get("completion_tokens", 0) for r in runs)
    per_case: dict[str, dict] = {}
    for r in runs:
        c = per_case.setdefault(r["id"], {"runs": 0, "matched": 0, "false_auto": 0, "floor_false_auto": 0,
                                          "cookable": 0 if r["cookable"] is not None else None, "tiers": []})
        c["runs"] += 1
        c["matched"] += bool(r["matched_no_correction"])
        c["false_auto"] += bool(r["false_auto_approval"])
        c["floor_false_auto"] += bool(r["floor_false_auto_approval"])
        if r["cookable"] is not None:
            c["cookable"] += bool(r["cookable"])
        c["tiers"].append(f"{r['gateway_outcome'] or 'no order'}/{r['final_status']}")
    return {
        "case_runs": n, "cases": len(per_case), "repeats": n // max(1, len(per_case)),
        "matched_no_correction": sum(bool(r["matched_no_correction"]) for r in runs),
        "matched_no_correction_pct": pct(sum(bool(r["matched_no_correction"]) for r in runs), n),
        "ok_case_runs": len(ok), "ok_matched": sum(bool(r["matched_no_correction"]) for r in ok),
        "ok_content_right": sum(bool(r["content_ok"]) for r in ok),
        "ok_auto_ordered_and_matched": sum(bool(r["matched_no_correction"] and r["auto_ordered"]) for r in ok),
        "hold_case_runs": len(hold), "held_or_refused_correctly": sum(bool(r["held_or_refused_correctly"]) for r in hold),
        "false_auto_approvals": sum(bool(r["false_auto_approval"]) for r in runs),
        "no_gateway_floor_false_auto_approvals": sum(bool(r["floor_false_auto_approval"]) for r in runs),
        "dish_runs": len(dish), "cookable": sum(bool(r["cookable"]) for r in dish),
        "items_expected": sum(1 for r in ok for i in (r["content"].get("items") or []) if not i["optional"]),
        "items_kind_found": sum(1 for r in ok for i in (r["content"].get("items") or []) if not i["optional"] and i["kind"]),
        "items_met": sum(1 for r in ok for i in (r["content"].get("items") or []) if not i["optional"] and i["met"]),
        "date_wrong": sum(1 for r in runs if r["content"].get("quote") and not r["content"].get("date_ok")),
        "no_quote": sum(1 for r in runs if not r["content"].get("quote")),
        "tokens_prompt_total": pt, "tokens_completion_total": ct,
        "cost_usd_total": cfg.cost_usd(pt, ct) if hasattr(cfg, "cost_usd") else None,
        "per_case": per_case,
    }


# --- docs/LLM_EVAL.md section -----------------------------------------------------------------------

def report_section() -> list[str]:
    files = sorted(HELDOUT.glob("results.*.json"))
    if not files:
        return []
    import yaml

    cases = {c["id"]: c for c in yaml.safe_load(CASES.read_text())["cases"]}
    L = ["## Held-out", "",
         "15 buyer requests in [`evals/heldout/cases.yaml`](../evals/heldout/cases.yaml) were written **blind**, from "
         "the challenge brief alone, before their author read Sous's code, evals or docs, and committed frozen "
         "(`d12e3d2`). Nothing in Sous was tuned on them. The scoring rubric "
         "([`evals/heldout/rubric.yaml`](../evals/heldout/rubric.yaml)) was written after reading the harness and "
         "before the first run; it turns each plain-language expectation into name patterns and quantity ranges. "
         "Every case runs under a neutral mandate (auto-approve up to 2,000 EUR, no first-time-product rule) plus "
         "only what the case's context states (a budget becomes the auto-approve limit, forbidden allergens become "
         "avoid_allergens). Code: [`scripts/heldout_eval.py`](../scripts/heldout_eval.py).", ""]
    for f in files:
        d = json.loads(f.read_text())
        s = d["summary"]
        p = d.get("provenance") or {}
        L += [f"### `{d['model_label']}`, {s['repeats']} repeat(s), run day {d.get('run_day')}"
              + (f", CI {p['ci_run_id']} @ `{p.get('commit')}`" if p.get("ci_run_id") else ""), "",
              "| Metric | Result |", "|---|---|",
              f"| Matched with no correction (all case runs) | {s['matched_no_correction']}/{s['case_runs']} = "
              f"{s['matched_no_correction_pct']} % |",
              f"| ... of the 12 should-order cases | {s['ok_matched']}/{s['ok_case_runs']} (content right: "
              f"{s['ok_content_right']}; matched and auto-ordered: {s['ok_auto_ordered_and_matched']}) |",
              f"| Held/refused correctly (3 should-hold cases) | {s['held_or_refused_correctly']}/{s['hold_case_runs']} |",
              f"| False auto-approvals (unsafe ordered silently) | {s['false_auto_approvals']} |",
              f"| No-gateway floor: false auto-approvals if every placed order had shipped | "
              f"{s['no_gateway_floor_false_auto_approvals']} |",
              f"| Cookable dishes (menu cases) | {s['cookable']}/{s['dish_runs']} |",
              f"| Expected product lines found (kind) / at the right quantity | {s['items_kind_found']}/"
              f"{s['items_expected']} / {s['items_met']}/{s['items_expected']} |",
              f"| Wrong delivery date / no quote at all | {s['date_wrong']} / {s['no_quote']} |",
              f"| Tokens (in / out), cost | {s['tokens_prompt_total']} / {s['tokens_completion_total']}, "
              + ("–" if s["cost_usd_total"] is None else f"${s['cost_usd_total']:.2f}") + " |", "",
              "| Case | Request | Expected | Matched | Gateway tier / status per repeat | Misses (first failing repeat) |",
              "|---|---|---|---|---|---|"]
        for cid, c in s["per_case"].items():
            fail = next((r for r in d["runs"] if r["id"] == cid and not r["matched_no_correction"]), None)
            why = ""
            if fail:
                ct = fail["content"]
                bits = [f"missing {i['name']}" + (f" (got {i['amount']})" if i["kind"] else "")
                        for i in ct.get("items") or [] if not i["met"] and not i["optional"]]
                bits += [f"unwanted: {x['rule']}" for x in ct.get("forbidden") or []]
                bits += [f"extra line {x}" for x in ct.get("extra_lines") or []]
                if ct.get("quote") and not ct.get("date_ok"):
                    bits.append(f"date {ct.get('date_quoted')} (asked {ct.get('date_expected')})")
                if not ct.get("quote"):
                    bits.append("no quote")
                if fail["false_auto_approval"]:
                    bits.append("FALSE AUTO-APPROVAL")
                if fail["expected"] != "ok" and not fail["answered"]:
                    bits.append(f"no answer ({fail['stop_reason']})")
                why = "; ".join(bits)[:220]
            cook = f", cookable {c['cookable']}/{c['runs']}" if c["cookable"] is not None else ""
            L.append(f"| {cid} | {cases[cid]['request'][:70]} | {cases[cid]['expect']['outcome']} | "
                     f"{c['matched']}/{c['runs']}{cook} | {', '.join(c['tiers'])} | {why} |")
        L.append("")
    findings = HELDOUT / "findings.yaml"
    if findings.exists():
        L += ["### Findings (reported, not fixed against these cases)", ""]
        L += [f"- {x}" for x in yaml.safe_load(findings.read_text()) or []]
        L.append("")
    return L


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cases", nargs="*", help="case ids (default: all)")
    ap.add_argument("--provider", help="gemini | verda | nvidia | local ... (default $LLM_PROVIDER or gemini)")
    ap.add_argument("--model")
    ap.add_argument("--base-url")
    ap.add_argument("--agent", choices=("generic", "sous"), default="sous")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--max-tool-calls", type=int, default=12)
    ap.add_argument("--state-dir")
    ap.add_argument("--report", action="store_true", help="only print the docs/LLM_EVAL.md section")
    a = ap.parse_args(argv)
    if a.report:
        print("\n".join(report_section()))
        return 0
    state_dir = Path(a.state_dir or tempfile.mkdtemp(prefix="sous-heldout-")).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    if (ROOT / "data").resolve() in [state_dir, *state_dir.parents]:
        sys.exit("Refusing to put eval state under data/; pass another --state-dir.")
    os.environ["SOUS_STATE_DB"] = str(state_dir / "init.db")
    os.environ["SOUS_DB_URL"] = ""  # always SQLite: never write eval orders into MariaDB
    if a.base_url:
        os.environ["LLM_BASE_URL"] = a.base_url
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    from agent.llm import ChatClient, config_from_env, key_present

    provider = a.provider or os.getenv("LLM_PROVIDER") or "gemini"
    if provider != "local" and not key_present(provider):
        print(f"::notice::No API key for {provider}; skipping the held-out eval.")
        return 0
    cfg = config_from_env(provider, a.model)
    rc = run(a, provider, cfg, ChatClient(cfg), state_dir)
    section = "\n".join(report_section())
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(section + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
