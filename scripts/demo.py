"""Play the demo video's scenes against a running gateway, over its public REST API.

Each scene is what a buyer's purchasing agent would do: search the catalog, ask for a
signed quote, order it. The mandate decides; this script only narrates. Open the UI
(<base>/ui/) next to the terminal while it runs: Approvals, Orders and the audit log
update live.

    uv run python scripts/demo.py                       # all scenes, local gateway
    uv run python scripts/demo.py 2                     # one scene
    uv run python scripts/demo.py --base https://<host> # the deployed gateway
    uv run python scripts/demo.py --list

The scenes reuse cases from evals/cases.yaml (ids in brackets), so their outcomes are
checked by the eval set too. Needs the demo mandates (scripts/seed.py).
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from datetime import date, timedelta

import httpx

TTY = sys.stdout.isatty()


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if TTY else s


TIER = {"silent": ("32", "AUTO"), "notify": ("34", "AUTO + FYI"), "ask": ("33", "ASK HUMAN"), "block": ("31", "BLOCKED")}


def next_monday(today: date | None = None) -> date:
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


class Gateway:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=60, headers={"user-agent": "valtakirja-demo/1"})

    def get(self, path: str, **params):
        r = self.http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict):
        r = self.http.post(path, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {r.text[:300]}")
        return r.json()

    def search(self, agent: str, q: str, take: int, **kw) -> list[str]:
        res = self.get("/products", q=q, agent_id=agent, limit=take, **kw)
        return [p["id"] for p in res["products"]]

    def quote(self, agent: str, mandate: str, items: list[dict], when: date) -> dict:
        return self.post("/quotes", {"agent_id": agent, "mandate_id": mandate,
                                     "delivery_date": when.isoformat(), "items": items})["quote"]

    def order(self, agent: str, mandate: str, quote_id: str) -> dict:
        return self.post("/orders", {"agent_id": agent, "mandate_id": mandate, "quote_id": quote_id,
                                     "idempotency_key": f"demo-{uuid.uuid4().hex[:12]}"})


def say(text: str, pause: float = 0.0) -> None:
    print(text, flush=True)
    if pause:
        time.sleep(pause)


def show_quote(q: dict) -> None:
    for l in q["lines"]:
        mark = _c("32", "✓") if l["orderable"] else _c("31", "✕")
        say(f"    {mark} {l['quantity']:g} × {l['name']} ({l.get('logistic_model') or '?'}) "
            f"€{l['line_total_ex_vat'] or 0:,.2f}  delivery {l.get('delivery_date') or '–'}")
    for a in q.get("alternatives_considered", [])[:3]:
        say(_c("2", f"    – skipped {a['name']}: {a['why_not'][0]}"))
    calls = sum(v for k, v in (q.get("valio_api_calls") or {}).items() if k != "cache_hits")
    say(_c("2", f"    signed quote {q['id']}, €{q['total_ex_vat']:,.2f} ex VAT, expires {q['expires_at'][11:16]}, "
                f"{calls} Valio API call(s)"))


def show_order(o: dict, base: str) -> None:
    color, label = TIER[o["decision"]["outcome"]]
    say(f"  → {_c(color + ';1', label)}  {o['id']} is {o['status'].replace('_', ' ')}")
    for f in o["decision"]["findings"]:
        if f["rule"] != "mandate.ok" or o["decision"]["outcome"] in ("silent", "notify"):
            say(f"    {_c(TIER[f['outcome']][0], '▮')} {f['message']}  " + _c("2", f["rule"]))
    if o["status"] == "pending_approval":
        url = o.get("approval_url") or f"/ui/#/orders/{o['id']}"
        if url.startswith("/"):  # gateway without VALTAKIRJA_PUBLIC_BASE_URL
            url = base + url
        say(_c("1", f"    Waiting for a human. Approve on the phone: {url}"))


# --- scenes ---------------------------------------------------------------------------

def scene_1(gw: Gateway, when: date) -> None:
    """Hotel: 1,000 L lactose-free milk by Monday, split across packs, within the mandate [H01]."""
    agent, mandate = "aurora-purchasing-agent", "M-HOTEL-AURORA"
    say(_c("1", '"We need 1,000 litres of lactose-free milk delivered on Monday."') + _c("2", "  (Hotel Aurora's agent)"))
    ids = gw.search(agent, "laktoositon maito", 12, claims=["FREE_FROM_LACTOSE"])
    say(f"  searched the static catalog: {len(ids)} lactose-free candidates, 0 Valio API calls")
    q = gw.quote(agent, mandate, [{"product_ids": ids, "amount": 1000, "unit": "L"}], when)
    show_quote(q)
    show_order(gw.order(agent, mandate, q["id"]), gw.base)


def scene_2(gw: Gateway, when: date) -> None:
    """Hospital: a substitute with no allergen data is held for a human (milk- and nut-free kitchen) [P06]."""
    agent, mandate = "meilahti-kitchen-agent", "M-HOSPITAL-KITCHEN"
    original = "468376"  # Bravo Chef chopped tomatoes 400 g
    say(_c("1", '"Swap our usual 400 g chopped tomatoes for a bulk pack."') + _c("2", "  (Meilahti hospital kitchen's agent)"))
    subs = gw.get(f"/products/{original}/substitutes", delivery_date=when.isoformat(), amount=40,
                  unit="SALES_UNIT", mandate_id=mandate, agent_id=agent, limit=4)
    for o in subs["options"]:
        say(f"    {o['tier']:<6} {o['name']}" + _c("2", f"  {'; '.join(o['compromises']) or 'no compromises'}"))
    pick = next((o for o in subs["options"] if o["allergen_check"]["status"] == "unknown"), None) or subs["options"][0]
    say(f"  agent picks {pick['name']} ({pick['product_id']})")
    q = gw.quote(agent, mandate, [{"product_ids": [pick["product_id"]], "amount": 4, "unit": "SALES_UNIT",
                                   "substitute_for": original}], when)
    show_quote(q)
    show_order(gw.order(agent, mandate, q["id"]), gw.base)


def scene_3(gw: Gateway, when: date) -> None:
    """Cheapest options are Terminal products whose cut-off has passed: skipped, or refused when named [H09, H08]."""
    agent, mandate = "aurora-purchasing-agent", "M-HOTEL-AURORA"
    say(_c("1", '"300 litres of lactose-free skimmed or semi-skimmed milk, cheapest that arrives Monday."'))
    q = gw.quote(agent, mandate, [{"product_ids": ["464119", "476240", "464201"], "amount": 300, "unit": "L"}], when)
    show_quote(q)
    show_order(gw.order(agent, mandate, q["id"]), gw.base)
    say(_c("1", '"No, 500 litres of exactly 464119, the one we always buy."'))
    q = gw.quote(agent, mandate, [{"product_ids": ["464119"], "amount": 500, "unit": "L"}], when)
    show_quote(q)
    show_order(gw.order(agent, mandate, q["id"]), gw.base)


def scene_4(gw: Gateway, when: date) -> None:
    """Guardrails: over the spend limit -> ask; gluten for the school -> refused [P07, S02]."""
    say(_c("1", '"400 litres of Oatly semi oat drink."') + _c("2", "  (hospital, auto-approve limit €400)"))
    q = gw.quote("meilahti-kitchen-agent", "M-HOSPITAL-KITCHEN",
                 [{"product_ids": ["470863"], "amount": 400, "unit": "L"}], when)
    show_quote(q)
    show_order(gw.order("meilahti-kitchen-agent", "M-HOSPITAL-KITCHEN", q["id"]), gw.base)
    say(_c("1", '"40 loaves of Pekan Leipä rye bread."') + _c("2", "  (Espoo schools, gluten-free mandate)"))
    q = gw.quote("espoo-school-agent", "M-SCHOOL-ESPOO",
                 [{"product_ids": ["472463"], "amount": 40, "unit": "SALES_UNIT"}], when)
    show_quote(q)
    show_order(gw.order("espoo-school-agent", "M-SCHOOL-ESPOO", q["id"]), gw.base)


SCENES = {"1": scene_1, "2": scene_2, "3": scene_3, "4": scene_4}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("scenes", nargs="*", help="scene numbers (default: all)")
    ap.add_argument("--base", default="http://localhost:8000", help="gateway base URL")
    ap.add_argument("--date", type=date.fromisoformat, default=None, help="delivery date (default: next Monday)")
    ap.add_argument("--pause", type=float, default=1.5, help="seconds between scenes")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        for k, f in SCENES.items():
            print(k, f.__doc__)
        return
    gw = Gateway(a.base)
    health = gw.get("/health")
    when = a.date or next_monday()
    storage = f" · storage {health['storage']}" if health.get("storage") else ""
    say(_c("2", f"gateway {a.base} ok{storage} · delivery {when:%a %d.%m.%Y}"))
    say(_c("2", f"watch: {a.base}/ui/#/inbox   audit: {a.base}/ui/#/audit"))
    for k in a.scenes or SCENES:
        f = SCENES[k]
        say("\n" + _c("1;4", f"Scene {k}") + "  " + f.__doc__)
        try:
            f(gw, when)
        except Exception as e:  # keep going: a demo run should show every scene it can
            say(_c("31", f"  scene failed: {e}"))
        time.sleep(a.pause)
    say("\n" + _c("2", f"stats: {a.base}/ui/#/stats"))


if __name__ == "__main__":
    main()
