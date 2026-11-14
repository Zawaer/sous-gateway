"""Evidence for GET /proof and /proof/data: every figure read from a file in the repo or computed from the loaded
catalog at request time. Nothing is typed in here; a missing source is reported as not available.

Each section carries `source`: the file path, what the file itself records about the run (generated_at, model,
CI run id and commit when the eval script recorded them) and, when a git checkout is present, the commit that last
changed the file. Files written before the eval scripts recorded their origin say so (`origin: "not recorded"`).

Computed once per process and kept in memory (the inputs only change with a deploy). No Valio Aimo calls, no
model calls.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config

ROOT = config.ROOT
NA = "not available"

_lock = threading.Lock()
_cache: dict | None = None


def run_provenance() -> dict:
    """Where an eval run happened, for the eval scripts to store next to generated_at. GitHub Actions sets
    GITHUB_RUN_ID/GITHUB_SHA; elsewhere it is a local run at the checkout's commit (if git is available)."""
    if os.environ.get("GITHUB_RUN_ID"):
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        run = os.environ["GITHUB_RUN_ID"]
        return {"origin": "ci", "ci_run_id": run, "commit": (os.environ.get("GITHUB_SHA") or "")[:7] or None,
                "ci_run_url": f"https://github.com/{repo}/actions/runs/{run}" if repo else None}
    return {"origin": "local", "ci_run_id": None, "commit": _git("rev-parse", "--short", "HEAD")}


def _git(*args: str) -> str | None:
    if not (ROOT / ".git").exists():
        return None
    try:
        out = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)


def _source(path: Path, doc: Any = None, **extra) -> dict:
    """What we know about where a file's numbers came from, only from the file and git."""
    rec = doc.get("provenance") if isinstance(doc, dict) and isinstance(doc.get("provenance"), dict) else {}
    last = _git("log", "-1", "--format=%h %cI", "--", _rel(path))
    out = {"path": _rel(path),
           "generated_at": doc.get("generated_at") if isinstance(doc, dict) else None,
           "origin": rec.get("origin") or "not recorded",
           "ci_run_id": rec.get("ci_run_id"), "ci_run_url": rec.get("ci_run_url"), "run_commit": rec.get("commit"),
           "file_last_commit": last.split()[0] if last else None,
           "file_last_commit_at": last.split()[1] if last else None}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _missing(path: Path, why: str = "file not found") -> dict:
    return {"available": False, "reason": why, "source": {"path": _rel(path)}}


# --- scripted eval ------------------------------------------------------------

def _first_order(case: dict) -> dict | None:
    orders = case.get("orders") or []
    step = case.get("unsafe_step")
    return next((o for o in orders if o.get("role") == step), orders[0] if orders else None)


def _handled_by(case: dict) -> str:
    """How the eval's unsafe step was stopped: the tier of that step's order (ask/block), else the error code the
    gateway refused it with, else 'other (see checks)' (e.g. a duplicate answered with the same order)."""
    o = next((x for x in case.get("orders") or [] if x.get("role") == case.get("unsafe_step")), None)
    if o and o.get("outcome") in ("ask", "block"):
        return o["outcome"]
    if errs := case.get("errors") or []:
        return f"refused: {errs[-1].get('code')}"
    return "other (see checks)"


def scripted_eval(path: Path) -> dict:
    doc = _load(path)
    if not isinstance(doc, dict) or "summary" not in doc:
        return _missing(path)
    s, cases = doc["summary"], doc.get("cases") or []
    rows, unsafe_tiers, unsafe_rules = [], Counter(), Counter()
    for c in cases:
        o = _first_order(c)
        checks = c.get("checks") or []
        rows.append({"id": c.get("id"), "title": c.get("title"), "unsafe": bool(c.get("unsafe")),
                     "passed": bool(c.get("passed")), "unsafe_handled": c.get("unsafe_handled"),
                     "tier": o.get("outcome") if o else None, "status": o.get("status") if o else None,
                     "rules": (o or {}).get("rules") or [],
                     "checks_ok": sum(1 for k in checks if k.get("ok")), "checks_total": len(checks),
                     "valio_api_calls": c.get("valio_api_calls"), "latency_ms": c.get("latency_ms")})
        if c.get("unsafe"):
            how = _handled_by(c)
            rows[-1]["handled_by"] = how
            unsafe_tiers[how] += 1
            if how in ("ask", "block"):
                unsafe_rules.update(o.get("rules") or [])
    keep = ("cases_passed", "cases_total", "unsafe_requests", "unsafe_blocked_or_escalated",
            "unsafe_blocked_or_escalated_pct", "order_decisions", "decisions_by_tier", "human_approval_rate_pct",
            "valio_api_calls_per_request_avg", "valio_api_calls_per_request_max", "valio_api_calls_total",
            "valio_cache_hits", "latency_ms_mean", "latency_ms_max", "quote_latency_ms_mean", "failed_cases",
            "unsafe_not_handled")
    return {"available": True, "source": _source(path, doc, cases_file=doc.get("cases_file"),
                                                 valio_api_base=doc.get("valio_api_base")),
            "summary": {k: s.get(k) for k in keep if k in s},
            "checks_ok": sum(r["checks_ok"] for r in rows), "checks_total": sum(r["checks_total"] for r in rows),
            "unsafe_handled_by": dict(unsafe_tiers),
            "unsafe_rules_fired": [{"rule": r, "cases": n} for r, n in unsafe_rules.most_common()],
            "definitions": doc.get("definitions") or {}, "cases": rows}


# --- LLM buyer agent ------------------------------------------------------------

def _run_id(doc: dict, prov: dict) -> str | None:
    """A CI run's id, else what identifies a local run: when it ran and at which commit."""
    if prov.get("ci_run_id"):
        return str(prov["ci_run_id"])
    if doc.get("generated_at") and prov.get("commit"):
        return f"local-{re.sub(r'[^0-9T]', '', doc['generated_at'][:16])}-{prov['commit']}"
    return None


def llm_evals(evals: Path) -> dict:
    models, excluded = [], []
    for path in sorted(evals.glob("llm_results.*.json")):
        doc = _load(path)
        if not isinstance(doc, dict) or "summary" not in doc:
            excluded.append({"path": _rel(path), "reason": "unreadable"})
            continue
        if doc.get("provider") == "oracle":
            excluded.append({"path": _rel(path), "label": doc.get("model_label"),
                             "reason": "oracle: a scripted plan with no model (a harness check, not an LLM score)",
                             "generated_at": doc.get("generated_at"),
                             "cases_passed": doc["summary"].get("cases_passed"),
                             "cases_run": doc["summary"].get("cases_run")})
            continue
        s = doc["summary"]
        keep = ("cases_total", "cases_run", "cases_n_a", "cases_passed", "matched_without_correction_pct",
                "need_understood_pct", "unsafe_requests", "unsafe_blocked_or_escalated",
                "unsafe_blocked_or_escalated_pct", "unsafe_not_handled", "mandate_violations_auto_ordered",
                "failed_cases", "tool_calls_per_request_avg", "tool_errors_total", "llm_calls_per_request_avg",
                "valio_api_calls_per_request_avg", "tokens_per_request_avg", "latency_ms_p50", "latency_ms_mean",
                "cost_usd_per_request", "cost_usd_total", "need_understood", "need_corrections_by_parser",
                "self_check_refusals")
        prov = doc.get("provenance") if isinstance(doc.get("provenance"), dict) else {}
        models.append({"model_label": doc.get("model_label"), "provider": doc.get("provider"),
                       "model": doc.get("model"), "where": doc.get("where"),
                       "agent": doc.get("agent") or "generic",
                       "reasoning_effort": doc.get("reasoning_effort"),
                       "summary": {**{k: s.get(k) for k in keep if k in s},
                                   # cases where the model API failed (e.g. HTTP 429 quota) before an answer
                                   "provider_errors": (s.get("stop_reasons") or {}).get("llm_error", 0)},
                       "source": _source(path, doc, cases_file=doc.get("cases_file"), run_id=_run_id(doc, prov))})
    # one row per run, a model's generic and purpose-built agent runs side by side; models by best pass rate
    best: dict[str, float] = {}
    for m in models:
        best[m["model"]] = max(best.get(m["model"], 0), m["summary"].get("matched_without_correction_pct") or 0)
    models.sort(key=lambda m: (-best[m["model"]], m["model"] or "", m["agent"] != "generic"))
    return {"available": bool(models), "models": models, "excluded": excluded,
            **({} if models else {"reason": "no evals/llm_results.*.json from a model"})}


# --- mandate drafting -----------------------------------------------------------

def draft_evals(evals: Path) -> dict:
    runs = []
    for path in sorted(evals.glob("draft_results.*.json")):
        doc = _load(path)
        if not isinstance(doc, dict) or "passed" not in doc:
            continue
        res = doc.get("results") or []
        parsers = sorted({r.get("parser") for r in res if r.get("parser")})
        runs.append({"provider": doc.get("provider"), "parser": ", ".join(parsers) or None,
                     "passed": doc.get("passed"), "cases": doc.get("cases"),
                     "traps": sum(1 for r in res if r.get("trap")),
                     "traps_passed": sum(1 for r in res if r.get("trap") and r.get("pass")),
                     "llm_calls": (doc.get("tokens") or {}).get("calls"),
                     "failed": [r.get("id") for r in res if not r.get("pass")],
                     "source": _source(path, doc)})
    return {"available": bool(runs), "runs": runs, **({} if runs else {"reason": "no evals/draft_results.*.json"})}


# --- recipe planner -------------------------------------------------------------

def recipe_eval(path: Path) -> dict:
    doc = _load(path)
    if not isinstance(doc, list) or not doc:
        return _missing(path)
    runs = [{"provider": r.get("provider"), "passed": r.get("passed"), "cases": r.get("cases"),
             "valio_api_calls": r.get("valio_api_calls"), "llm_calls": r.get("llm_calls"), "today": r.get("today"),
             "failed": [x.get("id") for x in r.get("results") or [] if not x.get("passed")]}
            for r in doc if isinstance(r, dict)]
    return {"available": True, "runs": runs,
            "source": _source(path, None, run_date=next((r["today"] for r in runs if r.get("today")), None))}


# --- Gemini Decisions API on undeclared allergens -------------------------------

def allergen_ai_eval(path: Path) -> dict:
    doc = _load(path)
    if not isinstance(doc, dict) or "ground_truth" not in doc:
        return _missing(path)
    gt = doc["ground_truth"]
    return {"available": True, "model": doc.get("model"), "threshold": doc.get("threshold"),
            "families": doc.get("families"), "products_scored": gt.get("products_scored"),
            "total": gt.get("total"), "per_family": gt.get("per_family"),
            "false_likely": (gt.get("false_likely") or [])[:10], "undeclared": doc.get("undeclared"),
            "definitions": doc.get("definitions"), "source": _source(path, doc)}


# --- vision ---------------------------------------------------------------------

def vision_eval(path: Path) -> dict:
    doc = _load(path)
    if not isinstance(doc, dict) or not doc:
        return _missing(path)
    keep = ("pages", "auto_correct", "silent_errors", "traps_ok", "item_recall", "item_precision", "qty_unit_exact",
            "second_looks", "latency_ms_mean")
    variants = [{"variant": v.get("variant"), "model": v.get("model"), "ran_at": v.get("ran_at"),
                 "history_file": f"{_rel(path.parent)}/history/{v['source']}" if v.get("source") else None,
                 "summary": {k: (v.get("summary") or {}).get(k) for k in keep}}
                for v in doc.values() if isinstance(v, dict)]
    return {"available": True, "variants": variants, "source": _source(path, None),
            "note": "Each variant row records its own run (ran_at, history file); rows can come from different runs."}


# --- conformance ----------------------------------------------------------------

_CHECK = re.compile(r"^(PASS|FAIL|SKIP)\s+(\w+)\s+(.+)$")
_DETAIL = re.compile(r"^\s+-> (.+)$")
_TALLY = re.compile(r"^(\d+) passed, (\d+) failed, (\d+) skipped")
_RUN_ON = re.compile(r"^Run on (\S+) at ([^.;]+?) against ([^,.;]+)")
_CMD = re.compile(r"^\$ .*conformance\.py.*--base (\S+)")


def conformance(json_path: Path, doc_path: Path, kit: Path) -> dict:
    """A stored `scripts/conformance.py --json` report if one exists, else the run recorded in CONFORMANCE.md."""
    kit_checks = None
    if kit.exists():
        m = re.search(r"CHECKS = \[(.*?)\n    \]", kit.read_text(encoding="utf-8"), re.S)
        kit_checks = len(re.findall(r'^\s*\("(\w+)",', m[1], re.M)) if m else None
    doc = _load(json_path)
    if isinstance(doc, dict) and isinstance(doc.get("checks"), list):
        checks = [{"id": r.get("id"), "status": str(r.get("status", "")).upper(), "title": r.get("title"),
                   "detail": r.get("detail")} for r in doc["checks"]]
        return {"available": True, "checks": checks, "kit_checks": kit_checks, **_tally(checks),
                "target": doc.get("base"), "source": _source(json_path, doc)}
    if not doc_path.exists():
        return {**_missing(json_path, "no stored conformance report"), "kit_checks": kit_checks}
    checks, run, tally = [], {}, None
    for line in doc_path.read_text(encoding="utf-8").splitlines():
        if m := _RUN_ON.match(line):
            run = {"run_date": m[1], "run_time": m[2].strip(), "target": m[3].strip()}
        elif m := _CMD.match(line):
            run["target_url"] = m[1]
        elif m := _CHECK.match(line):
            checks.append({"id": m[2], "status": m[1], "title": m[3].strip(), "detail": None})
        elif (m := _DETAIL.match(line)) and checks and checks[-1]["detail"] is None:
            checks[-1]["detail"] = m[1].strip()
        elif m := _TALLY.match(line):
            tally = {"passed": int(m[1]), "failed": int(m[2]), "skipped": int(m[3])}
    if not checks:
        return {**_missing(doc_path, "no recorded run in the document"), "kit_checks": kit_checks}
    counted = _tally(checks)
    return {"available": True, "checks": checks, "kit_checks": kit_checks, **counted,
            "tally_line_matches": tally == {k: counted[k] for k in ("passed", "failed", "skipped")} if tally else None,
            "target": run.get("target_url") or run.get("target"),
            "ci_test": "tests/test_conformance.py" if (ROOT / "tests" / "test_conformance.py").exists() else None,
            "source": {**_source(doc_path, None), "origin": "recorded run pasted in the document",
                       "run_date": run.get("run_date"), "run_time": run.get("run_time")},
            "note": "No stored --json report in the repo; these rows are the staging run printed in the document. "
                    "The document's own text says whether a failure has been fixed since."}


def _tally(checks: list[dict]) -> dict:
    c = Counter(x["status"] for x in checks)
    return {"passed": c.get("PASS", 0), "failed": c.get("FAIL", 0), "skipped": c.get("SKIP", 0), "total": len(checks)}


# --- search quality -------------------------------------------------------------

def search_quality(path: Path) -> dict:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return _missing(path, "test file not present (it is not shipped in every build)")
    for node in ast.walk(tree):
        target = node.target if isinstance(node, ast.AnnAssign) else (
            node.targets[0] if isinstance(node, ast.Assign) and node.targets else None)
        if isinstance(target, ast.Name) and target.id == "CASES" and isinstance(node.value, ast.List):
            queries = [e.elts[0].value for e in node.value.elts
                       if isinstance(e, ast.Tuple) and e.elts and isinstance(e.elts[0], ast.Constant)]
            return {"available": True, "queries": len(queries), "examples": queries[:8],
                    "source": _source(path, None),
                    "note": "Count of query cases only: no stored pass/fail result. The test needs data/products.json, "
                            "so CI skips it; run it locally with the command below."}
    return _missing(path, "CASES list not found")


# --- rules ----------------------------------------------------------------------

def rule_register() -> dict:
    try:
        from . import rules
        reg = rules.register()
    except Exception as e:  # the register imports the mandate model; never let it take /proof down
        return {"available": False, "reason": f"rule register failed to load: {type(e).__name__}",
                "source": {"path": "sous/rules.py"}}
    return {"available": True, "count": len(reg),
            "by_kind": dict(Counter(r.get("kind") for r in reg)),
            "by_owner": dict(Counter(r.get("owner") for r in reg)),
            "source": {"path": "sous/rules.py", "origin": "computed live from the running code (GET /rules)",
                       "file_last_commit": (_git("log", "-1", "--format=%h", "--", "sous/rules.py") or None)}}


# --- allergen data audit --------------------------------------------------------

def allergen_audit() -> dict:
    """How many products carry allergen data, from the catalog this process serves. Two definitions, both the
    engine's own: `allergens.declared` (any allergen or free-from classification, set in catalog.compact) and the
    stricter 'allergen list' test catalog.allergen_status applies (a contains / may-contain entry, or X99 'nothing
    to declare'); without a list, an avoided allergen counts as safe only if it is explicitly declared absent."""
    from . import catalog
    db, src = config.CATALOG_DB, config.PRODUCTS_JSON
    if not db.exists() and not src.exists():
        return {"available": False, "reason": "no catalog loaded in this process (data/ is not in the repo; CI has none)",
                "source": {"path": "data/catalog.db (built from data/products.json, Valio Aimo's product download)"}}
    try:
        products = [json.loads(r[0]) for r in catalog._db().execute("SELECT data FROM product")]
    except Exception as e:
        return {"available": False, "reason": f"catalog unreadable: {type(e).__name__}",
                "source": {"path": "data/catalog.db"}}
    active = [p for p in products if not p.get("deleted")]
    food = [p for p in active if catalog.is_food(p)]

    def no_declared(p):
        return not (p.get("allergens") or {}).get("declared")

    def no_list(p):
        a = p.get("allergens") or {}
        return not (a.get("contains") or a.get("may_contain") or "X99" in (a.get("free_from") or []))

    def block(rows):
        n = len(rows)
        nd, nl = sum(map(no_declared, rows)), sum(map(no_list, rows))
        return {"products": n, "no_declaration": nd, "no_declaration_pct": round(100 * nd / n, 1) if n else None,
                "no_allergen_list": nl, "no_allergen_list_pct": round(100 * nl / n, 1) if n else None}

    mtime = datetime.fromtimestamp((src if src.exists() else db).stat().st_mtime, config.TZ)
    return {"available": True,
            "products_total": len(products), "deleted": len(products) - len(active),
            "all_active": block(active), "food": block(food),
            "non_food": block([p for p in active if not catalog.is_food(p)]),
            "definitions": {
                "active": "Products in Valio Aimo's product download without the deleted flag.",
                "food": "Active products whose category does not start with a non-food prefix "
                        f"({', '.join(sorted(catalog.NON_FOOD_CATEGORY_PREFIXES))}): kitchen equipment, garden, "
                        "cleaning and hygiene, disposables, seasonal, gases, office. Those groups have no nutrition "
                        "facts and standard VAT (sous/catalog.py, is_food; the same split the search uses).",
                "no_declaration": "allergens.declared is false: the product has no allergen and no free-from "
                                  "classification at all (sous/catalog.py, compact).",
                "no_allergen_list": "No contains or may-contain entry and no X99 ('nothing to declare'). For these, "
                                    "catalog.allergen_status calls an avoided allergen safe only if that allergen is "
                                    "explicitly declared absent; otherwise the verdict is unknown and the order goes "
                                    "to a human.",
            },
            "source": {"path": f"data/{(src if src.exists() else db).name}",
                       "origin": "computed live from the catalog this gateway serves",
                       "data_file_modified_at": mtime.isoformat(timespec="seconds")}}


# --- reproduce ------------------------------------------------------------------

REPRODUCE = [
    {"what": "Offline test suite", "cmd": "uv run pytest -q"},
    {"what": "Scripted eval (no LLM, live Valio prices; needs data/ and VALIO_AIMO_API_KEY)",
     "cmd": "uv run python scripts/eval.py"},
    {"what": "LLM buyer agent per model (needs a model key)",
     "cmd": "uv run python scripts/llm_eval.py --provider verda"},
    {"what": "Mandate drafting eval", "cmd": "uv run python scripts/draft_eval.py"},
    {"what": "Recipe planner eval", "cmd": "uv run python scripts/recipe_eval.py --providers table gemini"},
    {"what": "Vision eval (needs NVIDIA_API_KEY)", "cmd": "uv run python scripts/vision_eval.py"},
    {"what": "Gemini Decisions API on allergens (needs data/ and GEMINI_API_KEY)",
     "cmd": "uv run python scripts/allergen_ai_eval.py --sample 600"},
    {"what": "Conformance kit against any gateway",
     "cmd": "uv run python scripts/conformance.py --base https://<gateway> --json"},
    {"what": "Search quality report (needs data/products.json)", "cmd": "uv run python tests/test_search_quality.py"},
]


def _reproduce() -> list[dict]:
    """Keep only commands whose script exists here, so the list never points at something missing."""
    out = []
    for r in REPRODUCE:
        m = re.search(r"((?:scripts|tests)/\w+\.py)", r["cmd"])
        out.append({**r, "script_present": (ROOT / m[1]).exists() if m else None})
    return out


def collect() -> dict:
    evals = ROOT / "evals"
    scripted = scripted_eval(evals / "results.json")
    adopt = scripted_eval(evals / "results.adopt.json")
    llm = llm_evals(evals)
    rules_ = rule_register()
    headline = {
        "scripted_cases": _pair(scripted, "cases_passed", "cases_total"),
        "unsafe_blocked_or_escalated": _pair(scripted, "unsafe_blocked_or_escalated", "unsafe_requests"),
        "llm_models": [{"model_label": m["model_label"], "agent": m["agent"],
                        "passed": m["summary"].get("cases_passed"),
                        "run": m["summary"].get("cases_run"),
                        "mandate_violations_auto_ordered": m["summary"].get("mandate_violations_auto_ordered")}
                       for m in llm.get("models", [])],
        "rules": rules_.get("count") if rules_.get("available") else None,
    }
    return {"generated_at": datetime.now(config.TZ).isoformat(timespec="seconds"),
            "gateway_commit": os.environ.get("SOUS_COMMIT") or _git("rev-parse", "--short", "HEAD"),
            "not_available": NA,
            "headline": headline,
            "scripted_eval": scripted,
            "attack_eval": adopt,
            "llm_agents": llm,
            "drafting": draft_evals(evals),
            "recipe": recipe_eval(evals / "recipe_results.json"),
            "vision": vision_eval(evals / "vision" / "results.json"),
            "allergen_ai": allergen_ai_eval(evals / "allergen_ai_results.json"),
            "conformance": conformance(evals / "conformance.json", ROOT / "docs" / "CONFORMANCE.md",
                                       ROOT / "scripts" / "conformance.py"),
            "search_quality": search_quality(ROOT / "tests" / "test_search_quality.py"),
            "rules": rules_,
            "allergen_audit": allergen_audit(),
            "reproduce": _reproduce()}


def _pair(section: dict, num: str, den: str) -> dict | None:
    if not section.get("available"):
        return None
    s = section["summary"]
    return {"passed": s.get(num), "total": s.get(den), "source": section["source"]["path"]}


def data(refresh: bool = False) -> dict:
    global _cache
    with _lock:
        if _cache is None or refresh:
            _cache = collect()
        return _cache
