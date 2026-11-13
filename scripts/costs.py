"""Render docs/COSTS.md from evals/costs.yaml: what Sous spent, per provider.

Run: uv run python scripts/costs.py [--now "2026-11-15 09:00"]
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "evals" / "costs.yaml"
OUT = ROOT / "docs" / "COSTS.md"
FMT = "%Y-%m-%d %H:%M"


def run_cost(r: dict, prices: dict) -> float | None:
    if r.get("usd") is not None:
        return float(r["usd"])
    p_in, p_out = (prices.get(r["model"]) or [None, None])
    if p_in is None or p_out is None:
        return None
    return (r.get("tokens_in", 0) * p_in + r.get("tokens_out", 0) * p_out) / 1e6


def service_cost(s: dict, now: datetime) -> tuple[float | None, float]:
    hours = max(0.0, (now - datetime.strptime(s["since"], FMT)).total_seconds() / 3600)
    if s.get("usd_per_hour") is not None:
        return s["usd_per_hour"] * hours, hours
    if s.get("usd_per_month") is not None:
        return s["usd_per_month"] * hours / 730, hours
    return None, hours


def money(v: float | None) -> str:
    return "unknown" if v is None else f"${v:,.2f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", help=f"time to prorate services to ({FMT}, Helsinki); default: now")
    a = ap.parse_args()
    now = datetime.strptime(a.now, FMT) if a.now else datetime.now(ZoneInfo("Europe/Helsinki")).replace(tzinfo=None)
    led = yaml.safe_load(LEDGER.read_text())
    prices = led.get("prices_usd_per_1m", {})

    by_provider: dict[str, dict] = defaultdict(lambda: {"in": 0, "out": 0, "usd": 0.0, "unknown": False, "runs": 0})
    rows = []
    for r in led.get("runs", []):
        c = run_cost(r, prices)
        p = by_provider[r["provider"]]
        p["in"] += r.get("tokens_in", 0)
        p["out"] += r.get("tokens_out", 0)
        p["runs"] += 1
        if c is None:
            p["unknown"] = p["unknown"] or bool(r.get("tokens_in") or r.get("tokens_out"))
        else:
            p["usd"] += c
        rows.append(f"| {r['when']} | {r['provider']} | {r['model']} | {r['purpose']} | "
                    f"{r.get('tokens_in', 0):,} / {r.get('tokens_out', 0):,} | {money(c)} |"
                    + (f" {r['note']}" if r.get("note") else ""))

    svc_rows, svc_total, svc_unknown = [], 0.0, False
    for s in led.get("services", []):
        c, h = service_cost(s, now)
        if c is None:
            svc_unknown = True
        else:
            svc_total += c
        svc_rows.append(f"| {s['provider']} | {s['item']} | {s['since']} | {h:.1f} h | {money(c)} | {s.get('note', '')} |")

    known = sum(p["usd"] for p in by_provider.values()) + svc_total
    lines = [
        "# What Sous spent",
        "",
        f"_Generated from [evals/costs.yaml](../evals/costs.yaml) by `scripts/costs.py`, prorated to {now:%a %d.%m %H:%M}"
        " (Helsinki)._ Every paid model or API run and every running service is in the ledger;"
        " \"unknown\" means the price still has to be read from that provider's console.",
        "",
        "## Totals per provider (model usage)",
        "",
        "| Provider | Runs | Tokens in / out | Cost |",
        "|---|---|---|---|",
    ]
    for name, p in sorted(by_provider.items()):
        cost = money(p["usd"]) + (" + unknown" if p["unknown"] and p["usd"] else "") if not (p["unknown"] and not p["usd"]) else "unknown"
        lines.append(f"| {name} | {p['runs']} | {p['in']:,} / {p['out']:,} | {cost} |")
    lines += [
        "",
        f"**Known total so far: {money(known)}**"
        + (" (plus the items marked unknown)" if svc_unknown or any(p['unknown'] for p in by_provider.values()) else ""),
        "",
        "## Services (prorated)",
        "",
        "| Provider | What | Since | Running | Cost so far | Note |",
        "|---|---|---|---|---|---|",
        *svc_rows,
        "",
        "## Every model run",
        "",
        "| When | Provider | Model | Purpose | Tokens in / out | Cost |",
        "|---|---|---|---|---|---|",
        *rows,
        "",
        "## Notes",
        "",
        *[f"- {n}" for n in led.get("notes", [])],
        "- Valio Aimo API calls are counted separately per request in the eval results (they cost nothing but rate limit).",
        "",
    ]
    OUT.write_text("\n".join(lines))
    print(f"wrote {OUT.relative_to(ROOT)}: known total {money(known)}")


if __name__ == "__main__":
    main()
