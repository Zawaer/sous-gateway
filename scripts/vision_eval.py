"""Vision eval: how well does the model read kitchen paper? Field-level accuracy per prompt variant.

    uv run python scripts/vision_eval.py                                   # nvidia, variants v1 v2 v2+verify
    uv run python scripts/vision_eval.py --variants v2+verify v2+verify+think
    uv run python scripts/vision_eval.py --provider oracle                 # offline: checks the scorer itself
    uv run python scripts/vision_eval.py --report                          # rewrite results.md from results.json

The set (evals/vision/truth.json, images/ rendered by scripts/vision_make_images.py): 8 pages, Finnish /
Swedish / English, handwritten and printed order lists, menus and delivery notes, with traps (a crossed-out
line, a smudged line, a decimal comma, pack abbreviations, a handwritten correction on a printed note).

A variant is a prompt version plus switches: "v1" (baseline prompt), "v2" (engineered prompt), "+verify" (code
cross-checks each line against its own transcription; mismatches and low-confidence lines get ONE targeted
second look), "+think" (Nemotron's reasoning mode on).

Metrics (per variant, over all pages):
  kind / date        the page type and the delivery date
  item recall        truth lines found;  precision: read lines that are real (crossed-out/smudged excluded)
  qty+unit exact     truth lines read with exactly the right quantity and unit
  delivered exact    delivery notes: the delivered quantity too
  dishes / portions  menus
  auto-correct       truth lines that would be ordered automatically AND correctly (confidence >= 0.75,
                     no cross-check flag, right quantity/unit/delivered)
  silent errors      lines that WOULD be ordered automatically but are wrong (wrong number/unit, invented,
                     crossed out, or a guessed smudge): the safety metric; low-confidence lines go to a human
  traps              crossed-out line not ordered + smudged line flagged (not guessed)
No gateway, no Valio calls: this measures reading only. Writes evals/vision/results.json and results.md.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import vision as V  # noqa: E402

DIR = ROOT / "evals" / "vision"
TRUTH = DIR / "truth.json"
OUT_JSON = DIR / "results.json"
OUT_MD = DIR / "results.md"
DEFAULT_VARIANTS = ["v1", "v2", "v3", "v3+verify"]


def _text(it: dict) -> str:
    return " ".join(str(it.get(k) or "") for k in ("name_fi", "name_en", "text_as_written")).lower()


def _hits(terms: list[str], it: dict) -> bool:
    t = _text(it)
    return any(term.lower() in t for term in terms)


def _eq(a, b) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) < 1e-6


def score_case(truth: dict, read: dict | None, flags: list[dict]) -> dict:
    """Field-level comparison of one reading with its truth."""
    flagged = {f["index"] for f in flags}
    s = {"file": truth["file"], "kind_ok": False, "date_ok": False, "items_truth": len(truth["items"]),
         "items_found": 0, "items_read": 0, "items_real": 0, "qty_unit_ok": 0, "delivered_truth": 0,
         "delivered_ok": 0, "dishes_truth": len(truth.get("dishes", [])), "dishes_found": 0, "portions_ok": 0,
         "auto_correct": 0, "silent_errors": 0, "traps": 0, "traps_ok": 0, "problems": []}
    traps = len(truth.get("must_skip", [])) + len(truth.get("must_flag", []))
    s["traps"] = traps
    if read is None:
        s["problems"].append("no valid reading")
        s["delivered_truth"] = sum(1 for t in truth["items"] if "delivered_quantity" in t)
        return s
    s["kind_ok"] = read.get("kind") == truth["kind"]
    s["date_ok"] = (read.get("delivery_date") or None) == truth.get("delivery_date")
    if not s["kind_ok"]:
        s["problems"].append(f"kind {read.get('kind')} != {truth['kind']}")
    if not s["date_ok"]:
        s["problems"].append(f"date {read.get('delivery_date')} != {truth.get('delivery_date')}")
    items = read.get("items") or []
    s["items_read"] = len(items)
    used: set[int] = set()

    def auto(i: int, it: dict) -> bool:
        return it.get("confidence", 0) >= V.CONFIRM_BELOW and i not in flagged and it.get("quantity") is not None

    for t in truth["items"]:
        j = next((i for i, it in enumerate(items) if i not in used and _hits(t["terms"], it)), None)
        has_deliv = "delivered_quantity" in t
        s["delivered_truth"] += has_deliv
        if j is None:
            s["problems"].append(f"missed {t['name_fi']}")
            continue
        used.add(j)
        it = items[j]
        s["items_found"] += 1
        qu = _eq(it.get("quantity"), t["quantity"]) and V.norm_unit(it.get("unit")) == t["unit"]
        dl = not has_deliv or _eq(it.get("delivered_quantity"), t["delivered_quantity"])
        s["qty_unit_ok"] += qu
        if has_deliv:
            s["delivered_ok"] += dl
        if not qu:
            s["problems"].append(f"{t['name_fi']}: read {it.get('quantity')} {it.get('unit')}, "
                                 f"truth {t['quantity']} {t['unit']} (conf {it.get('confidence')})")
        elif not dl:
            s["problems"].append(f"{t['name_fi']}: delivered {it.get('delivered_quantity')}, "
                                 f"truth {t['delivered_quantity']} (conf {it.get('confidence')})")
        if auto(j, it):
            if qu and dl:
                s["auto_correct"] += 1
            else:
                s["silent_errors"] += 1
    trap_idx: set[int] = set()
    for terms in truth.get("must_skip", []):
        bad = [i for i, it in enumerate(items) if _hits(terms, it)]
        trap_idx |= set(bad)
        if any(auto(i, items[i]) for i in bad):
            s["silent_errors"] += 1
            s["problems"].append(f"crossed-out {terms[0]} would be ordered")
        else:
            s["traps_ok"] += 1
    for terms in truth.get("must_flag", []):
        cand = [i for i, it in enumerate(items) if _hits(terms, it)]
        trap_idx |= set(cand)
        if any(auto(i, items[i]) for i in cand):
            s["silent_errors"] += 1
            s["problems"].append(f"smudged {terms[0]} guessed and would be ordered")
        elif cand or read.get("unreadable"):
            s["traps_ok"] += 1
        else:
            s["problems"].append(f"smudged {terms[0]} silently dropped (not flagged)")
    spurious = [i for i in range(len(items)) if i not in used and i not in trap_idx]
    s["items_real"] = len(items) - len(spurious) - len(trap_idx - used)
    for i in spurious:
        s["problems"].append(f"extra line {items[i].get('text_as_written')!r}")
        if auto(i, items[i]):
            s["silent_errors"] += 1
    dishes = read.get("dishes") or []
    dused: set[int] = set()
    for d in truth.get("dishes", []):
        j = next((i for i, x in enumerate(dishes) if i not in dused
                  and any(term in (x.get("name") or "").lower() for term in d["terms"])), None)
        if j is None:
            s["problems"].append(f"missed dish {d['name']}")
            continue
        dused.add(j)
        s["dishes_found"] += 1
        if dishes[j].get("portions") == d["portions"]:
            s["portions_ok"] += 1
        else:
            s["problems"].append(f"dish {d['name']}: portions {dishes[j].get('portions')} != {d['portions']}")
    return s


def summarize(cases: list[dict]) -> dict:
    def ratio(a: str, b: str) -> float | None:
        den = sum(c[b] for c in cases)
        return round(sum(c[a] for c in cases) / den, 3) if den else None
    lat = [c["latency_ms"] for c in cases if c.get("latency_ms")]
    return {
        "pages": len(cases),
        "kind_acc": round(sum(c["kind_ok"] for c in cases) / len(cases), 3),
        "date_acc": round(sum(c["date_ok"] for c in cases) / len(cases), 3),
        "item_recall": ratio("items_found", "items_truth"),
        "item_precision": ratio("items_real", "items_read"),
        "qty_unit_exact": ratio("qty_unit_ok", "items_truth"),
        "delivered_exact": ratio("delivered_ok", "delivered_truth"),
        "dish_recall": ratio("dishes_found", "dishes_truth"),
        "portions_exact": ratio("portions_ok", "dishes_truth"),
        "auto_correct": ratio("auto_correct", "items_truth"),
        "silent_errors": sum(c["silent_errors"] for c in cases),
        "traps_ok": f"{sum(c['traps_ok'] for c in cases)}/{sum(c['traps'] for c in cases)}",
        "invalid_json_pages": sum(1 for c in cases if c.get("error")),
        "json_repairs": sum(1 for c in cases if c.get("repaired")),
        "second_looks": sum(c.get("rechecked", 0) for c in cases),
        "calls": sum(c.get("calls", 0) for c in cases),
        "latency_ms_mean": round(statistics.mean(lat)) if lat else None,
        "latency_ms_max": max(lat) if lat else None,
        "prompt_tokens": sum(c.get("prompt_tokens", 0) for c in cases),
        "completion_tokens": sum(c.get("completion_tokens", 0) for c in cases),
    }


class OracleLLM:
    """Answers with the truth (in the model's JSON shape): checks the scorer and the plumbing offline."""

    def __init__(self, truth: dict):
        from agent.llm import LLMConfig
        self.cfg = LLMConfig(provider="oracle", base_url="", model="truth")
        self.by_size: dict[int, dict] = {}
        for c in truth["cases"]:
            data, _ = V.prepare_image(DIR / c["file"])
            self.by_size[len(data)] = c

    def chat(self, messages, tools=None):
        from agent.llm import ChatResponse
        part = next(p for p in messages[-1]["content"] if p["type"] == "image_url")
        import base64
        c = self.by_size[len(base64.b64decode(part["image_url"]["url"].split(",", 1)[1]))]
        ans = {"kind": c["kind"], "delivery_date": c.get("delivery_date"), "notes": None, "unreadable": [],
               "items": [{"text_as_written": f"{t['name_fi']} {t['quantity']:g} {t['unit']}", "name_fi": t["name_fi"],
                          "name_en": None, "quantity": t["quantity"], "unit": t["unit"],
                          "delivered_quantity": t.get("delivered_quantity"), "confidence": 0.95, "note": None}
                         for t in c["items"]],
               "dishes": [{"name": d["name"], "portions": d["portions"], "day": None, "confidence": 0.95}
                          for d in c.get("dishes", [])]}
        if c.get("must_flag"):
            ans["unreadable"] = ["a smudged line"]
        return ChatResponse({"role": "assistant", "content": json.dumps(ans)}, "stop",
                            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, 0, 0)


def make_llm(provider: str, model: str | None, think: bool):
    if provider == "oracle":
        return OracleLLM(json.loads(TRUTH.read_text(encoding="utf-8")))
    from agent.llm import ChatClient, config_from_env, key_present
    cfg = config_from_env(provider, model)
    if cfg.provider != "local" and not key_present(cfg.provider):
        return None
    if provider in ("nvidia", "verda-nemotron"):  # the same Nemotron; thinking is a chat-template switch
        cfg.extra_body = {"chat_template_kwargs": {"enable_thinking": think}}
        if think:
            cfg.max_tokens = 16384
            cfg.temperature = 0.6  # NVIDIA's recommended vision sampling with reasoning on
    cfg.timeout_s = 180
    return ChatClient(cfg)


def run_variant(variant: str, provider: str, model: str | None, truth: dict, repeat: int = 1) -> dict | None:
    parts = variant.split("+")
    version, verify, think = parts[0], "verify" in parts, "think" in parts
    llm = make_llm(provider, model, think)
    if llm is None:
        print(f"No API key for {provider}: skipping", file=sys.stderr)
        return None
    today = date.fromisoformat(truth["today"])
    cases = []
    # repeat > 1: every page read N times (the model is not deterministic: run-to-run variance is a result too)
    for c in [c for _ in range(repeat) for c in truth["cases"]]:
        t0 = time.perf_counter()
        try:
            vr = V.read_order_image(DIR / c["file"], None, llm, prompt_version=version, today=today, verify=verify)
            err = None
        except V.VisionError as e:
            vr, err = getattr(e, "result", None), str(e)
        except Exception as e:  # noqa: BLE001 - a provider failure is a result too
            vr, err = None, f"{type(e).__name__}: {str(e)[:300]}"
        read = vr.read.model_dump(mode="json") if vr and vr.read else None
        s = score_case(c, read, vr.flags if vr else [])
        s.update(error=err, latency_ms=round((time.perf_counter() - t0) * 1000), calls=vr.calls if vr else 0,
                 prompt_tokens=vr.prompt_tokens if vr else 0, completion_tokens=vr.completion_tokens if vr else 0,
                 repaired=bool(vr and vr.repaired), rechecked=len(vr.rechecked) if vr else 0,
                 read=read, second_look=vr.rechecked if vr else [], flags=vr.flags if vr else [])
        cases.append(s)
        print(f"  {variant:<18} {c['file']:<40} found {s['items_found']}/{s['items_truth']} "
              f"qty {s['qty_unit_ok']} silent {s['silent_errors']} {s['latency_ms'] / 1000:.1f}s"
              + (f"  ERROR {err[:80]}" if err else ""), flush=True)
    label = getattr(llm.cfg, "label", provider)
    return {"variant": variant, "model": label, "summary": summarize(cases), "cases": cases,
            "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


COLS = [("pages", "pages read"), ("item_recall", "item recall"), ("item_precision", "precision"), ("qty_unit_exact", "qty+unit exact"),
        ("delivered_exact", "delivered exact"), ("dish_recall", "dishes"), ("portions_exact", "portions"),
        ("kind_acc", "kind"), ("date_acc", "date"), ("auto_correct", "auto-correct"),
        ("silent_errors", "silent errors"), ("traps_ok", "traps"), ("calls", "calls"),
        ("latency_ms_mean", "mean s/page")]


def report(results: dict) -> str:
    out = ["# Vision eval: reading kitchen paper (agent/vision.py)", "",
           "Generated by `scripts/vision_eval.py` on the 8 pages in `evals/vision/` (see docs/NEMOTRON.md).", "",
           "| model | variant | " + " | ".join(h for _, h in COLS) + " |",
           "|---|---|" + "---|" * len(COLS)]
    for key, r in results.items():
        s = r["summary"]
        vals = []
        for k, _ in COLS:
            v = s.get(k)
            vals.append("-" if v is None else f"{v / 1000:.1f}" if k == "latency_ms_mean" else
                        f"{v:.0%}" if isinstance(v, float) else str(v))
        out.append(f"| {r['model']} | {r['variant']} | " + " | ".join(vals) + " |")
    out += ["", "Silent errors = lines that would be ordered automatically but are wrong (lower is better; "
            "low-confidence lines go to a human instead).", ""]
    for key, r in results.items():
        probs = [(c["file"], p) for c in r["cases"] for p in c["problems"]]
        if probs:
            out.append(f"<details><summary>{r['model']} {r['variant']}: {len(probs)} problem(s)</summary>\n")
            out += [f"- `{f}`: {p}" for f, p in probs]
            out.append("\n</details>\n")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--provider", default="nvidia")
    ap.add_argument("--model")
    ap.add_argument("--variants", nargs="+", default=DEFAULT_VARIANTS)
    ap.add_argument("--report", action="store_true", help="only rewrite results.md from results.json")
    ap.add_argument("--fresh", action="store_true", help="drop earlier results instead of merging")
    ap.add_argument("--repeat", type=int, default=1, help="read every page N times (run-to-run variance)")
    a = ap.parse_args(argv)
    global OUT_JSON, OUT_MD
    if a.provider == "oracle":  # never mixed into the real results
        OUT_JSON, OUT_MD = DIR / "results.oracle.json", DIR / "results.oracle.md"
    results = {} if a.fresh or not OUT_JSON.exists() else json.loads(OUT_JSON.read_text(encoding="utf-8"))
    if not a.report:
        truth = json.loads(TRUTH.read_text(encoding="utf-8"))
        ran = 0
        for v in a.variants:
            r = run_variant(v, a.provider, a.model, truth, a.repeat)
            if r:
                results[f"{r['model']} {v}"] = r
                ran += 1
                print(json.dumps({"variant": v, **r["summary"]}, ensure_ascii=False))
        if not ran:
            return 0
        OUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    OUT_MD.write_text(report(results), encoding="utf-8")
    print(f"wrote {OUT_MD.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
