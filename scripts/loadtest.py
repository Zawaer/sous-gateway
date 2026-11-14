"""Capacity test of one gateway process, offline: Valio replayed, nothing leaves this machine (docs/CAPACITY.md).

Starts the real gateway (uvicorn, one worker, as in the Dockerfile) on 127.0.0.1, pinned with taskset to a few
CPUs to mimic the Verda CPU.4V.16G (4 vCPU), against a fresh SQLite state database, and drives a mix of agent
traffic with httpx/asyncio in closed loop, ramping concurrency 1 -> 64:

    search 50 %   GET  /products?q=...            (static catalog, FTS + ranking; writes an audit row)
    quote  30 %   POST /quotes                    (1-3 items from the agent's own search results, 1-3 candidates)
    order  15 %   POST /orders                    (the agent's latest quote; mandate decides; idempotency key)
    reads   5 %   GET  /orders/{id}, /quotes/{id}, /products/{id}

Each concurrent client is one buyer's agent with its own mandate (M-LOAD-NN). Valio Aimo is never called:

    --valio replay   SOUS_VALIO_REPLAY: sous/replay.py answers every price/availability lookup from
                     evals/redteam/valio_snapshot.json (the cache is bypassed). Measures the gateway's own CPU.
    --valio cached   The same snapshot behind the REAL client path (sous/aimo.py cache, in-flight de-duplication,
                     call budget): aimo's HTTP client gets an in-process transport that answers from the snapshot
                     and counts calls. Measures how many Valio calls a quote costs with the cache (TTL 600 s price,
                     60 s availability) at this traffic.

    uv run python -m scripts.loadtest --data-dir /tmp/sous-load/data            # full ramp, ~2.5 min of load
    uv run python -m scripts.loadtest --stages 1,8 --stage-s 5                    # smoke
    uv run python -m scripts.loadtest --valio cached --stages 16 --stage-s 60     # Valio calls per quote
    uv run python -m scripts.loadtest --profile /tmp/prof.txt --stages 16         # py-spy (raw folded stacks)

--data-dir needs Valio's products.json, delivery-reliability.csv and carbon-footprint.csv (scripts/fetch_data
downloads them once); the catalog index is built there if missing. The state database is always fresh (a temp dir).
The per-IP POST limit (SOUS_RATE_LIMIT_PER_MIN, 60) is raised for the test: every client is 127.0.0.1 here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "evals" / "redteam" / "valio_snapshot.json"
DEMO_NOW = "2026-11-14T12:00:00"  # frozen business clock: the snapshot's Stock cut-off is Sun 15.11 17:00
DELIVERY = "2026-11-16"

# Search phrases an agent sends (fi/en/sv), from the eval and red-team needs plus everyday kitchen staples.
QUERIES = [
    "maito", "laktoositon maito", "kevytmaito", "rasvaton maito", "kaurajuoma", "voi", "margariini", "kerma",
    "ruokakerma", "vispikerma", "kermaviili", "smetana", "jogurtti", "maustamaton jogurtti", "rahka", "raejuusto",
    "juusto", "emmental", "mozzarella", "parmesan", "fetajuusto", "kananmuna", "jauheliha", "broileri",
    "kananrintafilee", "lohi", "tonnikala", "kinkku", "pekoni", "makkara", "peruna", "porkkana", "sipuli",
    "tomaatti", "kurkku", "jäävuorisalaatti", "omena", "banaani", "appelsiinimehu", "ruisleipä", "kauraleipä",
    "riisi", "pasta", "spagetti", "vehnäjauho", "sokeri", "suola", "kahvi", "tee", "kaakao", "jäätelö",
    "vaniljajäätelö", "pakaste herne", "pakastemarjat", "mustikka", "tofu", "kaurahiutale", "puuro",
    "milk", "lactose free milk", "butter", "cream", "cheese", "eggs", "chicken breast", "minced meat", "salmon",
    "potatoes", "rice", "oat drink", "yoghurt", "vanilla ice cream", "coffee", "orange juice", "flour",
    "gluten free bread", "frozen peas", "napkins", "dish soap", "mjölk", "smör", "ost", "grädde",
]
FILTERS = [{}, {}, {}, {"avoid_allergens": ["AM"]}, {"avoid_allergens": ["AN"]}, {"claims": ["FREE_FROM_LACTOSE"]},
           {"temperature": "chilled"}, {"avoid_allergens": ["GL"]}]
MIX = [("search", 50), ("quote", 30), ("order", 15), ("read", 5)]


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, round(p / 100 * len(xs) + 0.5) - 1))]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- server side --------------------------------------------------------------------------------------------

class SnapshotValio:
    """Valio Aimo's /price and /availability as an httpx transport answering from the replay snapshot (no network)."""

    def __init__(self):
        self.calls = Counter()
        self.ids = Counter()

    def __call__(self, request):
        import httpx

        from sous import replay

        kind = request.url.path.rstrip("/").rsplit("/", 1)[-1]  # .../price or .../availability
        body = json.loads(request.content)
        self.calls[kind] += 1
        self.ids[kind] += len(body["products"])
        items = replay.lookup(kind, body["products"])
        return httpx.Response(200, json={"timeNow": "2026-11-14T12:00:00+02:00",
                                         "products": [{k: v for k, v in i.items() if k != "_replay"}
                                                      for i in items.values()]})


def serve(port: int, valio: str) -> None:
    """Run in the server subprocess: the gateway exactly as the Dockerfile runs it, plus the chosen Valio stand-in."""
    import httpx
    import uvicorn

    from sous import aimo, api, replay

    if valio == "cached":
        fake = SnapshotValio()
        replay.enable(SNAPSHOT)  # data source for the fake Valio ...
        aimo.replay = type("NoReplay", (), {"active": staticmethod(lambda: False)})  # ... but the real cache path
        aimo._client = httpx.Client(base_url="http://valio.invalid/api", transport=httpx.MockTransport(fake))

        @api.app.get("/_loadtest/valio", include_in_schema=False)
        def _valio_calls():
            return {"calls": dict(fake.calls), "ids": dict(fake.ids), "aimo_stats": dict(aimo.stats)}
    elif not replay.active():
        raise SystemExit("--valio replay needs SOUS_VALIO_REPLAY")
    uvicorn.run(api.app, host="127.0.0.1", port=port, log_level="warning", access_log=False)


def _server_env(data_dir: Path, state_dir: Path, valio: str, rate_limit: int, db_url: str = "") -> dict:
    """A clean environment: no keys of any kind, nothing that could reach a network service."""
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "UV_CACHE_DIR", "VIRTUAL_ENV")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(SOUS_DATA_DIR=str(data_dir), SOUS_STATE_DB=str(state_dir / "state.db"),
               SOUS_DEMO_NOW=DEMO_NOW, SOUS_DEMO_CLOCK="frozen", SOUS_RATE_LIMIT_PER_MIN=str(rate_limit),
               SOUS_ALLERGEN_AI="0", SOUS_SANDBOX="0", SOUS_ERP_URL="stub",
               SOUS_AIMO_CALLS_PER_MIN="1000000", SOUS_VALIO_GLOBAL_CALLS_PER_MIN="1000000",
               VALIO_AIMO_API_BASE="http://127.0.0.1:9/api", VALIO_AIMO_API_KEY="loadtest-no-network",
               PYTHONUNBUFFERED="1")
    if valio == "replay":
        env["SOUS_VALIO_REPLAY"] = str(SNAPSHOT)
    if db_url:
        env["SOUS_DB_URL"] = db_url
    return env


def _fresh_mariadb(db_url: str) -> None:
    """Drop and re-create the (local, test-only) database named in db_url, so every run starts empty."""
    from urllib.parse import unquote, urlparse

    import pymysql

    u = urlparse(db_url)
    name = u.path.lstrip("/")
    if not name.startswith("sous_load"):
        raise SystemExit(f"--db-url must name a throwaway database starting with sous_load, not {name!r}")
    if u.hostname not in ("127.0.0.1", "localhost"):
        raise SystemExit("--db-url must be a local MariaDB (127.0.0.1): this test never touches a shared server")
    c = pymysql.connect(host=u.hostname, port=u.port or 3306, user=unquote(u.username or ""),
                        password=unquote(u.password or ""), autocommit=True)
    with c.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
        cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
    c.close()


def prepare(data_dir: Path, state_dir: Path, agents: int, env: dict) -> None:
    """Catalog index (if missing) and one mandate per simulated agent, in a subprocess with the server's env."""
    code = (
        "import sys\n"
        "from sous import catalog, config, service as S\n"
        "from sous.mandate import Mandate\n"
        "if not config.CATALOG_DB.exists(): print('catalog:', catalog.build(), 'products')\n"
        f"for n in range(1, {agents} + 1):\n"
        "    S.issue_mandate(Mandate(id=f'M-LOAD-{n:02d}', buyer=f'Load Kitchen {n:02d}', agent_id=f'load-agent-{n:02d}',\n"
        "        issued_by='loadtest', auto_approve_limit_eur=2000.0, hard_limit_eur=20000.0, notify_above_eur=None,\n"
        "        first_time_products=None, unusual_quantity_factor=None), mint_agent_credential=False)\n"
    )
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, check=True)


# --- client side --------------------------------------------------------------------------------------------

class Agent:
    def __init__(self, n: int, rnd: random.Random):
        self.n, self.rnd = n, rnd
        self.agent_id, self.mandate_id = f"load-agent-{n:02d}", f"M-LOAD-{n:02d}"
        self.results: list[list[str]] = []  # product id lists from this agent's recent searches
        self.quotes: list[str] = []  # quotes not yet ordered
        self.orders: list[str] = []
        self.seq = 0


async def _one(client, agent: Agent, kind: str, pool: list[str]) -> tuple[str, int, dict | None]:
    r = agent.rnd
    if kind == "order" and not agent.quotes:
        kind = "quote"  # nothing to order yet: the agent asks for a quote first
    if kind == "search":
        params = {"q": r.choice(QUERIES), "limit": r.choice([8, 10, 15]), "agent_id": agent.agent_id,
                  **r.choice(FILTERS)}
        resp = await client.get("/products", params=params)
        if resp.status_code == 200:
            ids = [p["id"] for p in resp.json()["products"]]
            if ids:
                agent.results = (agent.results + [ids])[-5:]
        return kind, resp.status_code, None
    if kind == "quote":
        items = []
        for _ in range(r.choice([1, 1, 2, 3])):
            ids = r.choice(agent.results) if agent.results else [r.choice(pool)]
            cands = ids[: r.choice([1, 2, 3])]
            items.append({"product_ids": cands, "amount": r.choice([1, 2, 3, 4, 6]), "unit": "SALES_UNIT"})
        resp = await client.post("/quotes", json={"agent_id": agent.agent_id, "mandate_id": agent.mandate_id,
                                                  "delivery_date": DELIVERY, "items": items})
        if resp.status_code == 200:
            agent.quotes = (agent.quotes + [resp.json()["quote"]["id"]])[-3:]
        return kind, resp.status_code, None
    if kind == "order":
        qid = agent.quotes.pop()
        agent.seq += 1
        resp = await client.post("/orders", json={"agent_id": agent.agent_id, "mandate_id": agent.mandate_id,
                                                  "quote_id": qid, "idempotency_key": f"{agent.agent_id}-{agent.seq}"})
        out = None
        if resp.status_code == 200:
            o = resp.json()
            out = {"status": o.get("status")}
            if o.get("id"):
                agent.orders = (agent.orders + [o["id"]])[-5:]
        return kind, resp.status_code, out
    # reads
    choice = r.random()
    if choice < 0.4 and agent.orders:
        resp = await client.get(f"/orders/{r.choice(agent.orders)}")
    elif choice < 0.7 and agent.quotes:
        resp = await client.get(f"/quotes/{agent.quotes[-1]}")
    else:
        resp = await client.get(f"/products/{r.choice(pool)}")
    return kind, resp.status_code, None


async def _stage(base: str, concurrency: int, seconds: float, agents: list[Agent], pool: list[str],
                 timeout: float) -> dict:
    import httpx

    lat: dict[str, list[float]] = defaultdict(list)
    codes: dict[str, Counter] = defaultdict(Counter)
    order_status: Counter = Counter()
    errors: Counter = Counter()
    kinds = [k for k, w in MIX for _ in range(w)]
    stop = time.perf_counter() + seconds
    limits = httpx.Limits(max_connections=concurrency + 4, max_keepalive_connections=concurrency + 4)

    async with httpx.AsyncClient(base_url=base, timeout=timeout, limits=limits) as client:
        async def worker(a: Agent):
            while time.perf_counter() < stop:
                want = a.rnd.choice(kinds)
                t = time.perf_counter()
                try:
                    kind, code, extra = await _one(client, a, want, pool)
                except httpx.HTTPError as e:
                    errors[type(e).__name__] += 1
                    lat[want].append((time.perf_counter() - t) * 1000)
                    codes[want]["transport"] += 1
                    continue
                lat[kind].append((time.perf_counter() - t) * 1000)
                codes[kind][code] += 1
                if extra:
                    order_status[extra["status"]] += 1

        t0 = time.perf_counter()
        await asyncio.gather(*(worker(a) for a in agents[:concurrency]))
        wall = time.perf_counter() - t0
    n = sum(len(v) for v in lat.values())
    bad = sum(c for k in codes.values() for code, c in k.items() if code == "transport" or code >= 500)
    client_4xx = sum(c for k in codes.values() for code, c in k.items() if code != "transport" and 400 <= code < 500)
    return {"concurrency": concurrency, "seconds": round(wall, 1), "requests": n, "rps": round(n / wall, 1),
            "error_rate": round(bad / n, 4) if n else 0.0, "client_4xx": client_4xx,
            "all": {"p50": round(_pct([x for v in lat.values() for x in v], 50), 1),
                    "p95": round(_pct([x for v in lat.values() for x in v], 95), 1),
                    "p99": round(_pct([x for v in lat.values() for x in v], 99), 1)},
            "endpoints": {k: {"n": len(v), "rps": round(len(v) / wall, 1), "p50": round(_pct(v, 50), 1),
                              "p95": round(_pct(v, 95), 1), "p99": round(_pct(v, 99), 1),
                              "codes": {str(c): x for c, x in codes[k].items()}}
                          for k, v in sorted(lat.items())},
            "order_status": dict(order_status), "transport_errors": dict(errors)}


def _cpu_seconds(pid: int) -> float:
    """utime + stime of a process and its threads, in seconds."""
    f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")


def _server_pid(proc: subprocess.Popen) -> int:
    """The python process serving (py-spy and uv put a parent in between)."""
    pid = proc.pid
    for _ in range(4):
        kids = Path(f"/proc/{pid}/task/{pid}/children")
        nxt = kids.read_text().split() if kids.exists() else []
        if not nxt:
            return pid
        pid = int(nxt[0])
    return pid


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=os.getenv("SOUS_DATA_DIR") or str(ROOT / "data"))
    ap.add_argument("--valio", choices=["replay", "cached"], default="replay")
    ap.add_argument("--stages", default="1,2,4,8,16,32,64", help="concurrency per stage")
    ap.add_argument("--stage-s", type=float, default=20, help="seconds per stage")
    ap.add_argument("--warmup-s", type=float, default=3)
    ap.add_argument("--server-cpus", default="0-3", help="taskset CPU list for the gateway (4 = CPU.4V.16G)")
    ap.add_argument("--client-cpus", default="4-5", help="taskset CPU list for this load generator")
    ap.add_argument("--db-url", default="", help="state in a LOCAL MariaDB (mysql://u:p@127.0.0.1:port/sous_load...), "
                    "dropped and re-created first; default: a fresh SQLite file")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--profile", help="run the server under py-spy record, raw folded stacks to this file")
    ap.add_argument("--out", help="write the results as JSON here")
    ap.add_argument("--serve", type=int, help=argparse.SUPPRESS)  # internal: the server subprocess
    a = ap.parse_args()

    if a.serve:
        serve(a.serve, a.valio)
        return 0

    stages = [int(x) for x in a.stages.split(",")]
    data_dir = Path(a.data_dir).resolve()
    if not (data_dir / "products.json").exists():
        print(f"{data_dir}/products.json is missing: run scripts.fetch_data once (needs the Valio key)", file=sys.stderr)
        return 1
    state_dir = Path(tempfile.mkdtemp(prefix="sous-load-"))
    env = _server_env(data_dir, state_dir, a.valio, rate_limit=10**9, db_url=a.db_url)
    if a.db_url:
        _fresh_mariadb(a.db_url)
    prepare(data_dir, state_dir, max(stages), env)
    if a.client_cpus:
        os.sched_setaffinity(0, {c for part in a.client_cpus.split(",") for c in
                                 (range(int(part.split("-")[0]), int(part.split("-")[-1]) + 1))})

    port = _free_port()
    cmd = [sys.executable, "-m", "scripts.loadtest", "--serve", str(port), "--valio", a.valio]
    if a.profile:
        cmd = ["py-spy", "record", "-f", "raw", "-r", "200", "-o", a.profile, "--"] + cmd
    cmd = ["taskset", "-c", a.server_cpus] + cmd
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    try:
        import httpx

        for _ in range(300):
            try:
                h = httpx.get(base + "/health", timeout=2).json()
                break
            except httpx.HTTPError:
                if proc.poll() is not None:
                    raise SystemExit("server exited during start-up")
                time.sleep(0.2)
        else:
            raise SystemExit("server did not start")
        spid = _server_pid(proc)
        print(f"gateway pid {spid} on {base}, cpus {a.server_cpus}, storage {h['storage']}, valio {a.valio}, "
              f"clock {h['clock'].get('mode')}", flush=True)
        pool = [p["id"] for q in ("maito", "juusto", "kerma", "jauheliha", "peruna", "kahvi", "leipä", "voi")
                for p in httpx.get(base + "/products", params={"q": q, "limit": 50}).json()["products"]]
        rnd = random.Random(a.seed)
        agents = [Agent(n, random.Random(a.seed * 1000 + n)) for n in range(1, max(stages) + 1)]
        if a.warmup_s:
            asyncio.run(_stage(base, min(4, max(stages)), a.warmup_s, agents, pool, a.timeout))
        results = []
        for c in stages:
            cpu0, t0 = _cpu_seconds(spid), time.perf_counter()
            r = asyncio.run(_stage(base, c, a.stage_s, agents, pool, a.timeout))
            r["server_cpu_cores"] = round((_cpu_seconds(spid) - cpu0) / (time.perf_counter() - t0), 2)
            results.append(r)
            e = r["endpoints"]
            print(f"c={c:>3}  {r['rps']:>7.1f} req/s  cpu {r['server_cpu_cores']:.2f} cores  "
                  f"err {r['error_rate']:.2%}  p50/p95/p99 {r['all']['p50']}/{r['all']['p95']}/{r['all']['p99']} ms  | "
                  + "  ".join(f"{k} {v['p50']}/{v['p95']}/{v['p99']}" for k, v in e.items())
                  + f"  | orders {r['order_status']}", flush=True)
        extra = {}
        if a.valio == "cached":
            extra = httpx.get(base + "/_loadtest/valio").json()
            quotes = sum(r["endpoints"].get("quote", {}).get("n", 0) for r in results)
            orders = sum(r["endpoints"].get("order", {}).get("n", 0) for r in results)
            calls = sum(extra["calls"].values())
            extra.update(quotes_and_orders=quotes + orders,
                         valio_calls_per_quote_or_order=round(calls / max(1, quotes + orders), 3))
            print(f"fake Valio: {extra['calls']} calls for {quotes} quotes + {orders} orders (+warm-up) = "
                  f"{extra['valio_calls_per_quote_or_order']} calls each; cache hits {extra['aimo_stats'].get('cache_hits')}")
        if a.out:
            args = {k: v for k, v in vars(a).items() if k != "db_url"} | {"storage": h["storage"]}
            Path(a.out).write_text(json.dumps({"args": args, "health": {k: h.get(k) for k in ("commit", "storage")},
                                               "stages": results, "valio": extra}, indent=1))
    finally:
        os.killpg(proc.pid, signal.SIGINT)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
        shutil.rmtree(state_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
