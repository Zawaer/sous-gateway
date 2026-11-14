"""Run the evaluation set (evals/cases.yaml) through the gateway and score it.

Every case goes through sous.service directly (no HTTP): search -> quote (live Valio Aimo price
and availability) -> order under a mandate -> optional human step. Results go to evals/results.json.

    uv run python scripts/eval.py                 # all cases
    uv run python scripts/eval.py H01 P03         # some cases
    uv run python scripts/eval.py --state-db /tmp/eval-state.db

State (mandates, quotes, orders, audit) goes to a fresh SQLite file, never to data/state.db or
MariaDB, so the demo state is untouched and daily spend starts from zero on every run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("cases", nargs="*", help="case ids to run (default: all)")
ap.add_argument("--file", default=str(ROOT / "evals" / "cases.yaml"))
ap.add_argument("--out", default=str(ROOT / "evals" / "results.json"))
ap.add_argument("--state-db", default=os.getenv("SOUS_STATE_DB"),
                help="SQLite file for eval state (wiped first). Default: a new temp file.")
args = ap.parse_args()

state_db = Path(args.state_db or Path(tempfile.mkdtemp(prefix="sous-eval-")) / "state.db").resolve()
if state_db == (ROOT / "data" / "state.db").resolve():
    sys.exit("Refusing to use the shared data/state.db; pass another --state-db.")
if state_db.exists():
    state_db.unlink()
os.environ["SOUS_STATE_DB"] = str(state_db)
os.environ["SOUS_DB_URL"] = ""  # always SQLite: never write eval orders into MariaDB

import yaml  # noqa: E402

from sous import aimo, catalog, config, reservations, service as S  # noqa: E402
from sous.mandate import Mandate  # noqa: E402
from scripts.seed import MANDATES  # noqa: E402
from scripts.eval_checks import _order_summary, api_calls, check, safety_handled, setup_history  # noqa: E402,F401

APPROVER = "eval-runner (human stand-in)"
RUN_TAG = os.urandom(3).hex()  # reservation pools of this run (sous/reservations.py set_scope)


def call(fn, *a, **kw):
    """Gateway call that waits out the gateway's own per-minute cap on live Valio calls."""
    for attempt in range(6):
        try:
            return fn(*a, **kw)
        except S.GatewayError as e:
            if e.code != "valio_unavailable" or "budget" not in e.message or attempt == 5:
                raise
            print(f"   (Valio call budget used up for this minute; waiting 15 s)", flush=True)
            time.sleep(15)


# --- setup -------------------------------------------------------------------

def issue_mandates(spec: dict) -> None:
    for m in MANDATES:
        S.issue_mandate(m)
    for raw in spec.get("mandates", []):
        raw = dict(raw)
        revoke = raw.pop("revoke_before_run", False)
        S.issue_mandate(Mandate(**raw))
        if revoke:
            S.revoke_mandate(raw["id"], "Eval runner")


# --- one case ----------------------------------------------------------------

def run_case(case: dict, defaults: dict) -> dict:
    r = {"id": case["id"], "title": case["title"], "request": case["request"], "mandate": case["mandate"],
         "agent": case["agent"], "unsafe": bool(case.get("unsafe")), "unsafe_step": case.get("unsafe_step", "first"),
         "valio_api_calls": 0, "errors": [], "orders": [], "checks": []}
    t0 = time.perf_counter()
    delivery = date.fromisoformat(str(case.get("delivery_date", defaults["delivery_date"])))
    unit = case.get("unit", "SALES_UNIT")
    agent, mid = case["agent"], case["mandate"]

    # 1. search (static catalog, no Valio calls)
    candidates = [str(c) for c in case.get("candidates", [])]
    if s := case.get("search"):
        res = S.search(agent, s["query"], claims=s.get("claims"), avoid_allergens=s.get("avoid_allergens"),
                       limit=s.get("take", 10))
        r["search_results"] = [p["id"] for p in res["products"]]
        if not candidates:
            candidates = r["search_results"]
    r["candidates"] = candidates

    # 2. substitutes (optional): the gateway proposes; the agent picks one
    substitute_for = None
    if sub := case.get("substitutes"):
        substitute_for = str(sub["for"])
        sr = call(S.suggest_substitutes, agent, substitute_for, delivery, case["amount"], unit, mandate_id=mid)
        r["valio_api_calls"] += api_calls(sr.get("valio_api_calls"))
        r["substitutes"] = [{"tier": o["tier"], "product_id": o["product_id"], "name": o["name"],
                             "compromises": o["compromises"]} for o in sr["options"]]
        if not sr["options"]:
            r["latency_ms"] = round((time.perf_counter() - t0) * 1000)
            return r
        candidates = [sr["options"][sub.get("pick", 0)]["product_id"]]
        r["candidates"] = candidates

    # 3. the buyer's order history, when the case needs one (quantity.unusual)
    r["history_orders"] = setup_history(case, delivery)

    # 3b. a Valio product hold (recall, quality hold, sales stop) placed before quoting; no Valio call.
    # Holds are global, so it is lifted again when the case ends (run_case's caller never sees it).
    if h := case.get("hold"):
        from sous import holds
        r["hold"] = holds.create(holds.HoldIn(product_id=str(h["product"]), kind=h.get("kind", "recall"),
                                              reason=h.get("reason", "eval hold"), by="eval-runner"),
                                 (catalog.get(str(h["product"])) or {}).get("name"))["id"]
    try:
        return _quote_and_flow(case, r, t0, delivery, unit, agent, mid, candidates, substitute_for)
    finally:
        if r.get("hold"):
            from sous import holds
            holds.lift(r["hold"], "eval-runner")


def _quote_and_flow(case, r, t0, delivery, unit, agent, mid, candidates, substitute_for) -> dict:
    waited = 0.0
    # 4. quote
    def quote():
        req = S.QuoteRequest(agent_id=agent, mandate_id=mid, delivery_date=delivery, items=[S.QuoteItem(
            product_ids=candidates, amount=case["amount"], unit=unit, substitute_for=substitute_for,
            product_type=case.get("product_type"))], rationale=case.get("rationale"))
        tq = time.perf_counter()
        q = call(S.request_quote, req)["quote"]
        r.setdefault("quote_latency_ms", []).append(round((time.perf_counter() - tq) * 1000))
        r["valio_api_calls"] += api_calls(q["valio_api_calls"])
        return q

    ttl = config.QUOTE_TTL_SECONDS
    if "quote_ttl_seconds" in case:
        config.QUOTE_TTL_SECONDS = case["quote_ttl_seconds"]
    try:
        q = quote()
    finally:
        config.QUOTE_TTL_SECONDS = ttl
    r["quote"] = {"id": q["id"], "total_ex_vat": q["total_ex_vat"], "expires_at": q["expires_at"],
                  "preview": (q.get("mandate_preview") or {}).get("outcome"),
                  "lines": [{"product_id": l["product_id"], "name": l["name"], "quantity": l["quantity"],
                             "sales_unit": l["sales_unit"], "covers": l.get("covers"),
                             "line_total_ex_vat": l["line_total_ex_vat"], "logistic_model": l["logistic_model"],
                             "issues": [i["code"] for i in l["issues"]]} for l in q["lines"]],
                  "uncovered": [{"uncovered": g["uncovered"], "reason": g.get("reason")} for g in q["uncovered"]]}
    r["_quote"] = q
    if q.get("options"):
        r["options"] = [{"id": o["id"], "rank": o["rank"], "kind": o["kind"], "covers": o["covers"],
                         "delivery_dates": o["delivery_dates"], "total_ex_vat": o["total_ex_vat"],
                         "substitute_tier": (o.get("substitute") or {}).get("tier"),
                         "preview": (o.get("mandate_preview") or {}).get("outcome"), "tradeoff": o["tradeoff"],
                         "lines": [(l["product_id"], l["quantity"]) for l in o["lines"]]} for o in q["options"]]

    # 5. flow
    key = f"eval-{case['id']}-{q['id']}"
    first = None
    slices = 0
    for step in case.get("flow", ["order"]):
        try:
            if step == "order":
                first = call(S.place_order, agent, mid, q["id"], idempotency_key=key)
                r["orders"].append(_order_summary(first, "first"))
            elif step == "order_again":
                o = call(S.place_order, agent, mid, q["id"], idempotency_key=key + "-again")
                r["orders"].append(_order_summary(o, "second"))
            elif step == "retry_same_key":
                o = call(S.place_order, agent, mid, q["id"], idempotency_key=key)
                r["orders"].append(_order_summary(o, "second"))
            elif step == "requote_order":
                q2 = quote()
                o = call(S.place_order, agent, mid, q2["id"], idempotency_key=f"eval-{case['id']}-{q2['id']}")
                r["orders"].append(_order_summary(o, "second"))
            elif step == "replay_key_as_other_agent":
                other = case["other"]
                req = S.QuoteRequest(agent_id=other["agent"], mandate_id=other["mandate"], delivery_date=delivery,
                                     items=[S.QuoteItem(product_ids=candidates, amount=case["amount"], unit=unit)])
                q2 = call(S.request_quote, req)["quote"]
                r["valio_api_calls"] += api_calls(q2["valio_api_calls"])
                o = call(S.place_order, other["agent"], other["mandate"], q2["id"], idempotency_key=key)
                r["orders"].append({**_order_summary(o, "second"), "requested_by": other["agent"]})
            elif step == "choose_option":  # the agent takes one of the quote's options: re-quote, then order
                want = case.get("choose") or {}
                opts = [o for o in q.get("options") or [] if o["kind"] == want.get("kind", o["kind"])]
                if not opts:
                    raise RuntimeError(f"no option to choose ({want or 'any'})")
                req = S.QuoteRequest(agent_id=agent, mandate_id=mid, option_id=opts[0]["id"])
                q2 = call(S.request_quote, req)["quote"]
                r["valio_api_calls"] += api_calls(q2["valio_api_calls"])
                r["chosen_option"] = {"id": opts[0]["id"], "kind": opts[0]["kind"], "quote_id": q2["id"],
                                      "lines": [(l["product_id"], l["quantity"]) for l in q2["lines"]],
                                      "uncovered": [g["uncovered"] for g in q2["uncovered"]]}
                o = call(S.place_order, agent, mid, q2["id"], idempotency_key=f"eval-{case['id']}-{q2['id']}")
                r["orders"].append(_order_summary(o, "second"))
            elif step == "slice":  # the next slice of a split-up order: same need, new quote, new order
                slices += 1
                qn = quote()
                o = call(S.place_order, agent, mid, qn["id"], idempotency_key=f"eval-{case['id']}-{qn['id']}")
                r["orders"].append(_order_summary(o, f"slice{slices}"))
            elif step == "agent_self_approve":
                r["orders"].append(agent_self_approve(r, mid, first))
            elif step in ("approve", "reject"):
                o = S.decide(first["id"], APPROVER, approve=step == "approve", note="eval")
                first = o
                r["orders"].append(_order_summary(o, step))
            elif step == "revoke":
                S.revoke_mandate(mid, "Eval runner")
            elif step == "expire_quote":
                wait = max(0.0, (datetime.fromisoformat(q["expires_at"]) - datetime.now(config.TZ)).total_seconds())
                if wait > 30:
                    raise RuntimeError(f"quote expires in {wait:.0f} s; shorten quote_ttl_seconds")
                time.sleep(wait + 0.2)
                waited += wait + 0.2
            else:
                raise ValueError(f"unknown flow step {step}")
        except S.GatewayError as e:
            r["errors"].append({"step": step, "code": e.code, "message": e.message})
    r["latency_ms"] = round((time.perf_counter() - t0 - waited) * 1000)
    return r


def agent_self_approve(r: dict, mid: str, first: dict) -> dict:
    """The agent calls REST approve on its own held order with its own bearer credential (minted here, as the
    buyer would hand it over). The gateway must refuse (403 agent_cannot_approve) and audit the attempt."""
    from fastapi.testclient import TestClient

    from sous import api, store

    token = S.agent_credential(mid, "Eval runner")["agent_credential"]["token"]
    resp = TestClient(api.app).post(f"/orders/{first['id']}/approve", json={"by": "the agent itself"},
                                    headers={"Authorization": f"Bearer {token}"})
    body = resp.json()
    if resp.status_code >= 400:
        r["errors"].append({"step": "agent_self_approve", "code": body.get("error"), "message": body.get("message"),
                            "http_status": resp.status_code})
    r["audit_events"] = [e["event"] for e in store.audit_events(subject=first["id"], limit=50)]
    return _order_summary(S.get_order(first["id"]), "agent_self_approve")


# --- main ----------------------------------------------------------------------

def _rel(p) -> str:
    p = Path(p).resolve()
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)


def main() -> int:
    spec = yaml.safe_load(Path(args.file).read_text())
    defaults = spec.get("defaults", {})
    cases = [c for c in spec["cases"] if not args.cases or c["id"] in args.cases]
    issue_mandates(spec)
    print(f"Running {len(cases)} cases against {config.AIMO_API_BASE} (state: {state_db})\n")
    results = []
    for case in cases:
        try:
            # each case its own stock-reservation pool: earlier cases' orders never eat later cases' stock
            reservations.set_scope(f"eval-{RUN_TAG}-{case['id']}")
            r = run_case(case, defaults)
            checks = check(case, r)
        except Exception as ex:  # a crash is a failed case, not a crashed run
            r = {"id": case["id"], "title": case["title"], "request": case["request"], "unsafe": bool(case.get("unsafe")),
                 "unsafe_step": case.get("unsafe_step", "first"), "orders": [], "errors": [], "valio_api_calls": 0,
                 "latency_ms": None}
            checks = [("ran without exception", False, f"{type(ex).__name__}: {ex}")]
        r.pop("_quote", None)
        r["checks"] = [{"check": n, "ok": ok, "detail": d} for n, ok, d in checks]
        r["passed"] = all(ok for _, ok, _ in checks)
        r["unsafe_handled"] = safety_handled(r) if r.get("orders") is not None else None
        results.append(r)
        o = r["orders"][0] if r["orders"] else None
        fin = [x for x in r["orders"] if x["role"] in ("first", "approve", "reject")]
        shown = fin[-1]["status"] if fin else (r["errors"][0]["code"] if r["errors"] else "-")
        if r["errors"] and fin:
            shown += f" +{r['errors'][0]['code']}"
        print(f"{'PASS' if r['passed'] else 'FAIL'} {r['id']:4} {(o['outcome'] if o else '-'):6} {shown:28} "
              f"api={r['valio_api_calls']:<2} {r['latency_ms'] or 0:>5} ms  {r['title']}", flush=True)
        for n, ok, d in checks:
            if not ok:
                print(f"       x {n}: {d}")

    summary = summarize(results)
    from sous.proof import run_provenance
    out = {"generated_at": datetime.now(config.TZ).isoformat(timespec="seconds"), "provenance": run_provenance(),
           "valio_api_base": config.AIMO_API_BASE, "cases_file": _rel(args.file),
           "summary": summary, "definitions": DEFINITIONS, "cases": results}
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    print_summary(summary)
    print(f"\nWrote {_rel(args.out)}")
    return 0 if summary["cases_passed"] == summary["cases_total"] else 1


DEFINITIONS = {
    "cases_passed": "Cases where every expectation held (tier, final status, rules fired, properties).",
    "handled_without_manual_correction_pct": "Share of requests the gateway resolved correctly on its own: "
        "ordered, refused with the rule, or held for approval exactly when the mandate says so. "
        "A failed expectation counts as a request a human would have had to correct.",
    "unsafe_blocked_or_escalated_pct": "Of the requests where an automatic order would be wrong (allergen, budget, "
        "cut-off, stock, wrong agent, revoked mandate, double order, expired quote...), the share that was "
        "refused or held for a human. Target 100 %.",
    "human_approval_rate_pct": "Share of order decisions that went to a human (tier ask -> pending_approval).",
    "valio_api_calls_per_request": "Live Valio Aimo price + availability calls per request (quote + substitutes), "
        "from quote.valio_api_calls; the gateway batches a shortlist into at most 2 calls and caches.",
    "latency_ms": "Wall-clock per request through sous.service, including the live Valio calls "
        "(deliberate waits excluded).",
}


def summarize(results: list[dict]) -> dict:
    n = len(results)
    passed = sum(r["passed"] for r in results)
    unsafe = [r for r in results if r["unsafe"]]
    handled = sum(1 for r in unsafe if r["unsafe_handled"])
    decisions = [o for r in results for o in r["orders"] if o["role"] in ("first", "second")]
    asks = sum(1 for o in decisions if o["outcome"] == "ask")
    calls = [r["valio_api_calls"] for r in results if r.get("quote")]
    lat = [r["latency_ms"] for r in results if r.get("latency_ms") is not None]
    qlat = [x for r in results for x in r.get("quote_latency_ms", [])]
    outcomes: dict[str, int] = {}
    for o in decisions:
        outcomes[o["outcome"]] = outcomes.get(o["outcome"], 0) + 1
    pct = lambda a, b: round(100 * a / b, 1) if b else None  # noqa: E731
    return {
        "cases_passed": passed, "cases_total": n,
        "handled_without_manual_correction_pct": pct(passed, n),
        "unsafe_requests": len(unsafe), "unsafe_blocked_or_escalated": handled,
        "unsafe_blocked_or_escalated_pct": pct(handled, len(unsafe)),
        "unsafe_not_handled": [r["id"] for r in unsafe if not r["unsafe_handled"]],
        "order_decisions": len(decisions), "decisions_by_tier": outcomes,
        "human_approval_rate_pct": pct(asks, len(decisions)),
        "valio_api_calls_per_request_avg": round(sum(calls) / len(calls), 2) if calls else None,
        "valio_api_calls_per_request_max": max(calls) if calls else None,
        "valio_api_calls_total": sum(calls), "valio_cache_hits": aimo.stats["cache_hits"],
        "latency_ms_mean": round(sum(lat) / len(lat)) if lat else None,
        "latency_ms_max": max(lat) if lat else None,
        "quote_latency_ms_mean": round(sum(qlat) / len(qlat)) if qlat else None,
        "failed_cases": [r["id"] for r in results if not r["passed"]],
    }


def print_summary(s: dict) -> None:
    print("\n" + "-" * 64)
    print(f"Cases passed                          {s['cases_passed']}/{s['cases_total']}")
    print(f"Handled without manual correction     {s['handled_without_manual_correction_pct']} %")
    print(f"Unsafe actions blocked or escalated   {s['unsafe_blocked_or_escalated']}/{s['unsafe_requests']}"
          f" = {s['unsafe_blocked_or_escalated_pct']} %")
    print(f"Human-approval rate                   {s['human_approval_rate_pct']} % of {s['order_decisions']} decisions "
          f"{s['decisions_by_tier']}")
    print(f"Valio API calls per request           avg {s['valio_api_calls_per_request_avg']}, "
          f"max {s['valio_api_calls_per_request_max']} (total {s['valio_api_calls_total']}, "
          f"cache hits {s['valio_cache_hits']})")
    print(f"Latency per request                   mean {s['latency_ms_mean']} ms, max {s['latency_ms_max']} ms "
          f"(quote mean {s['quote_latency_ms_mean']} ms)")
    if s["failed_cases"]:
        print(f"Failed                                {', '.join(s['failed_cases'])}")


if __name__ == "__main__":
    sys.exit(main())
