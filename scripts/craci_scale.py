"""Throughput vs CPUs on one big CRACI runner (the `redteam-scale` job in ci.yml, docs/CAPACITY.md, docs/CRACI.md).

Two workloads, both offline (Valio replayed from evals/redteam/valio_snapshot.json, no model, no Valio key):

1. The red team, scripted (`redteam_run.py --provider oracle`): every request runs through the real MCP gateway in
   its own process with its own state, so it should scale with processes. At each CPU count C < the machine's, a
   slice of 75*C requests runs with C workers pinned (taskset) to C CPUs; at the full machine all 4,000 requests run
   with one worker per CPU. That full run is the red-team score.
2. The capacity load test (`scripts/loadtest.py`): ONE gateway process (one uvicorn worker, as in the Dockerfile)
   with its MariaDB, both pinned to C CPUs, the load generator on the last 2 CPUs. docs/CAPACITY.md predicts this
   stays flat (GIL + order lock); this measures it on dedicated hardware.

    uv run python scripts/craci_scale.py --data-dir data --out scale [--cpus 4,8,16,32] [--db-url mysql://...]
        [--db-container <docker id>]   # MariaDB pinned to the same CPUs with `docker update --cpuset-cpus`

Writes <out>/scale.json and <out>/summary.md (also the job summary). Exit 1 on any gateway violation.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIENT_CPUS = 2  # the load generator's own CPUs (the last ones), so it never competes with the gateway


def _cpus(n: int) -> str:
    return f"0-{n - 1}"


def redteam(c: int, n: int, out: Path, data_dir: Path, pin: bool) -> dict:
    d = out / f"redteam-c{c}"
    cmd = [sys.executable, "scripts/redteam_run.py", "--provider", "oracle", "--concurrency", str(c),
           "--out-dir", str(d)] + (["--n", str(n)] if n else [])
    if pin:
        cmd = ["taskset", "-c", _cpus(c)] + cmd
    env = os.environ | {"SOUS_DATA_DIR": str(data_dir)}
    t0 = time.time()
    p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
    wall = time.time() - t0
    print(p.stdout[-1500:], p.stderr[-1500:], sep="\n", flush=True)
    s = json.loads((d / "summary.json").read_text()) if (d / "summary.json").exists() else {}
    keep = ("requests", "passed", "pass_pct", "unsafe_requests", "unsafe_handled", "unsafe_handled_pct",
            "gateway_violations_auto_ordered", "agent_violations_auto_ordered", "decisions_by_tier", "crashed",
            "latency_ms_p50", "latency_ms_p95", "wall_clock_s", "requests_per_min", "replay", "requests_sha256")
    return {"cpus": c, "workers": c, "pinned": pin, "exit": p.returncode, "process_wall_s": round(wall, 1)} | {
        k: s.get(k) for k in keep}


def loadtest(c: int, total: int, out: Path, data_dir: Path, a) -> dict:
    server = _cpus(min(c, total - CLIENT_CPUS))
    client = f"{total - CLIENT_CPUS}-{total - 1}"
    if a.db_container:
        subprocess.run(["docker", "update", "--cpuset-cpus", server, a.db_container], check=False,
                       capture_output=True)
    f = out / f"loadtest-c{c}.json"
    cmd = [sys.executable, "-m", "scripts.loadtest", "--data-dir", str(data_dir), "--stages", a.load_stages,
           "--stage-s", str(a.stage_s), "--server-cpus", server, "--client-cpus", client, "--out", str(f)]
    if a.db_url:
        cmd += ["--db-url", a.db_url]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    print(p.stdout[-3000:], p.stderr[-1500:], sep="\n", flush=True)
    r = json.loads(f.read_text()) if f.exists() else {"stages": []}
    stages = [{"agents": s["concurrency"], "rps": s["rps"], "server_cpu_cores": s.get("server_cpu_cores"),
               "orders_per_s": (s["endpoints"].get("order") or {}).get("rps"),
               "p50_ms": s["all"]["p50"], "p95_ms": s["all"]["p95"], "error_rate": s["error_rate"]}
              for s in r["stages"]]
    return {"cpus": c, "server_cpus": server, "client_cpus": client, "exit": p.returncode,
            "storage": (r.get("args") or {}).get("storage"), "stages": stages,
            "peak_rps": max((s["rps"] for s in stages), default=None),
            "peak_orders_per_s": max((s["orders_per_s"] or 0 for s in stages), default=None)}


def markdown(res: dict) -> str:
    out = [f"## Red team + capacity at scale on CRACI ({res['machine_cpus']} vCPU)", "",
           "Valio replayed from the snapshot, no model, no Valio key: zero live calls. Egress proof: `craci-evidence`.",
           "", "### Scripted red team: throughput vs CPUs (one worker process per CPU)", "",
           "| CPUs | Requests | Wall s | Requests/min | per CPU | Unsafe held | Gateway violations | Pass |",
           "|---:|---:|---:|---:|---:|---|---:|---:|"]
    for r in res["redteam"]:
        rpm = r.get("requests_per_min") or 0
        out.append(f"| {r['cpus']} | {r.get('requests')} | {r.get('wall_clock_s')} | {rpm} | {rpm / r['cpus']:.1f} | "
                   f"{r.get('unsafe_handled')}/{r.get('unsafe_requests')} | {r.get('gateway_violations_auto_ordered')}"
                   f" | {r.get('pass_pct')} % |")
    full = res["redteam"][-1] if res["redteam"] else {}
    if full:
        out += ["", f"Full set: {full.get('requests')} requests in {full.get('wall_clock_s')} s, "
                    f"unsafe held {full.get('unsafe_handled')}/{full.get('unsafe_requests')}, "
                    f"gateway violations {full.get('gateway_violations_auto_ordered')}, "
                    f"decisions {full.get('decisions_by_tier')}."]
    if res["loadtest"]:
        out += ["", "### One gateway process (uvicorn, 1 worker) + its database pinned to C CPUs", "",
                "| CPUs (server+DB) | Storage | Peak req/s | Peak orders/s | Gateway CPU at peak | per stage (agents: req/s) |",
                "|---:|---|---:|---:|---:|---|"]
        for r in res["loadtest"]:
            pk = max(r["stages"], key=lambda s: s["rps"], default={})
            out.append(f"| {r['server_cpus']} | {r['storage']} | {r['peak_rps']} | {r['peak_orders_per_s']} | "
                       f"{pk.get('server_cpu_cores')} cores | "
                       + ", ".join(f"{s['agents']}: {s['rps']}" for s in r["stages"]) + " |")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=os.getenv("SOUS_DATA_DIR") or str(ROOT / "data"))
    ap.add_argument("--out", default="scale")
    ap.add_argument("--cpus", default="4,8,16,32")
    ap.add_argument("--per-cpu", type=int, default=75, help="red-team requests per CPU for the partial runs")
    ap.add_argument("--full-n", type=int, default=0, help="requests for the largest CPU count (0: all 4,000)")
    ap.add_argument("--load-stages", default="1,4,16,64")
    ap.add_argument("--stage-s", type=float, default=15)
    ap.add_argument("--db-url", default="")
    ap.add_argument("--db-container", default="")
    ap.add_argument("--skip-loadtest", action="store_true")
    a = ap.parse_args()

    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    data_dir = Path(a.data_dir).resolve()
    total = os.cpu_count() or 1
    cpus = [c for c in (int(x) for x in a.cpus.split(",")) if c <= total]
    res = {"machine_cpus": total, "redteam": [], "loadtest": []}
    for c in cpus:
        full = c == max(cpus)
        print(f"== red team, {c} CPUs ({'the full set' if full and not a.full_n else f'{a.per_cpu * c} requests'})", flush=True)
        res["redteam"].append(redteam(c, a.full_n if full else a.per_cpu * c, out, data_dir, pin=not full or c < total))
    if not a.skip_loadtest:
        for c in cpus:
            print(f"== load test, gateway + DB on {min(c, total - CLIENT_CPUS)} CPUs", flush=True)
            res["loadtest"].append(loadtest(c, total, out, data_dir, a))
    (out / "scale.json").write_text(json.dumps(res, indent=1) + "\n")
    md = markdown(res)
    (out / "summary.md").write_text(md)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md)
    print(md)
    bad = sum(r.get("gateway_violations_auto_ordered") or 0 for r in res["redteam"])
    if bad:
        print(f"::error::{bad} gateway violations in the red team")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
