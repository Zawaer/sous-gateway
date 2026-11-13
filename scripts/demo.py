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
import json
import os
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


SANDBOX_ID: list[str | None] = [None]  # set by --sandbox, used when printing approval links


def approver_code() -> str:
    return os.getenv("SOUS_APPROVER_CODE") or os.getenv("VALTAKIRJA_APPROVER_CODE", "")  # old name still works


class Gateway:
    """The gateway's REST API, as a buyer's agent sees it.

    Agent credentials: when a mandate requires agent authentication (its require_agent_auth, or the gateway's
    SOUS_REQUIRE_AGENT_AUTH=1), quotes and orders carry `Authorization: Bearer <token>`. Tokens come from
    SOUS_AGENT_TOKENS (JSON {mandate_id: token}) or the token file (--tokens, default .sous-agent-tokens.json,
    gitignored). If a needed token is missing and SOUS_APPROVER_CODE is set, the script rotates the mandate's
    credential (a human action, audited) and saves the new token to the file, chmod 600.
    """

    def __init__(self, base: str, token_file: str = ".sous-agent-tokens.json"):
        self.base = base.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=60, headers={"user-agent": "sous-demo/1"})
        self.why: str | None = None  # the buyer's request, sent as the agent's rationale
        self.token_file = token_file
        self.tokens: dict[str, dict[str, str]] = {}
        try:
            with open(token_file) as f:
                self.tokens = json.load(f)
        except FileNotFoundError:
            pass
        if env := os.getenv("SOUS_AGENT_TOKENS"):
            self.tokens.setdefault(self.base, {}).update(json.loads(env))
        self.sbx: str | None = None  # judge sandbox id: scenes run on its private copies of the demo mandates
        self.sbx_token: str | None = None

    # --- judge sandbox (POST /sandbox): private copies of the hotel, hospital and school mandates -------------
    SANDBOX_MANDATES = {"M-HOTEL-AURORA", "M-HOSPITAL-KITCHEN", "M-SCHOOL-ESPOO"}

    def use_sandbox(self, which: str) -> None:
        """'new' creates a sandbox (tokens saved to the token file); an SBX id reuses one from that file."""
        mine = self.tokens.setdefault(self.base, {})
        if which == "new":
            r = self.http.post("/sandbox")
            if r.status_code >= 400:
                raise RuntimeError(f"could not create a sandbox: {r.status_code} {r.text[:200]}")
            d = r.json()
            self.sbx, self.sbx_token = d["sandbox"]["id"], d["agent_token"]
            mine[f"sandbox:{self.sbx}"] = {"agent_token": d["agent_token"], "approver_token": d["approver_token"]}
            fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.tokens, f, indent=1)
            say(_c("2", f"  created sandbox {self.sbx} (expires {d['sandbox']['expires_at'][:16]}); tokens saved to {self.token_file}"))
        else:
            self.sbx = which.upper()
            saved = mine.get(f"sandbox:{self.sbx}") or {}
            self.sbx_token = os.getenv("SOUS_SANDBOX_AGENT_TOKEN") or saved.get("agent_token")
            if not self.sbx_token:
                raise RuntimeError(f"no agent token for {self.sbx}: set SOUS_SANDBOX_AGENT_TOKEN or use --sandbox new")

    def _m(self, mandate: str) -> str:
        if not self.sbx:
            return mandate
        if mandate not in self.SANDBOX_MANDATES:
            raise RuntimeError(f"{mandate} has no sandbox copy (sandboxes cover the hotel, hospital and school)")
        return f"{mandate}-{self.sbx}"

    def _a(self, agent: str) -> str:
        return f"{agent}-{self.sbx.lower()}" if self.sbx else agent

    def _headers(self, mandate: str) -> dict:
        """Authorization for this mandate's agent, minting a credential if one is needed and missing."""
        if self.sbx:
            return {"authorization": f"Bearer {self.sbx_token}"}
        mine = self.tokens.setdefault(self.base, {})
        if mandate not in mine:
            st = self.http.get(f"/mandates/{mandate}/agent-credential")
            if st.status_code != 200 or not st.json().get("required"):
                return {}
            code = approver_code()
            if not code:
                raise RuntimeError(f"{mandate} requires an agent token: put it in SOUS_AGENT_TOKENS or "
                                   f"{self.token_file}, or set SOUS_APPROVER_CODE to mint one")
            r = self.http.post(f"/mandates/{mandate}/agent-credential", json={"by": "demo script", "action": "rotate"},
                               headers={"x-approver-code": code})
            if r.status_code >= 400:
                raise RuntimeError(f"could not mint an agent token for {mandate}: {r.status_code} {r.text[:200]}")
            mine[mandate] = r.json()["agent_credential"]["token"]
            fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.tokens, f, indent=1)
            say(_c("2", f"  minted an agent token for {mandate}, saved to {self.token_file}"))
        return {"authorization": f"Bearer {mine[mandate]}"}

    def get(self, path: str, headers: dict | None = None, **params):
        if "mandate_id" in params:
            params["mandate_id"] = self._m(params["mandate_id"])
        if "agent_id" in params:
            params["agent_id"] = self._a(params["agent_id"])
        r = self.http.get(path, params=params, headers=headers or {})
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict, headers: dict | None = None):
        r = self.http.post(path, json=body, headers=headers or {})
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {r.text[:300]}")
        return r.json()

    def search(self, agent: str, q: str, take: int, **kw) -> list[str]:
        res = self.get("/products", q=q, agent_id=agent, limit=take, **kw)  # agent_id mapped in get()
        return [p["id"] for p in res["products"]]

    def quote(self, agent: str, mandate: str, items: list[dict], when: date) -> dict:
        return self.post("/quotes", {"agent_id": self._a(agent), "mandate_id": self._m(mandate), "rationale": self.why,
                                     "delivery_date": when.isoformat(), "items": items},
                         self._headers(mandate))["quote"]

    def order(self, agent: str, mandate: str, quote_id: str) -> dict:
        return self.post("/orders", {"agent_id": self._a(agent), "mandate_id": self._m(mandate), "quote_id": quote_id,
                                     "rationale": self.why, "idempotency_key": f"demo-{uuid.uuid4().hex[:12]}"},
                         self._headers(mandate))


def say(text: str, pause: float = 0.0) -> None:
    print(text, flush=True)
    if pause:
        time.sleep(pause)


def show_quote(q: dict) -> None:
    for l in q["lines"]:
        mark = _c("32", "✓") if l["orderable"] else _c("31", "✕")
        cov = l.get("covers") or {}
        packs = f"{l['quantity']:g} {l.get('sales_unit') or ''}".strip()
        if cov.get("amount") and cov.get("unit") and cov["unit"] != l.get("sales_unit"):
            packs += f" (= {cov['amount']:g} {cov['unit']})"
        say(f"    {mark} {packs} × {l['name']} ({l.get('logistic_model') or '?'}) "
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
        if SANDBOX_ID[0] and "sandbox=" not in url:  # keep the judge inside their sandbox
            url = url.replace("/ui/#", f"/ui/?sandbox={SANDBOX_ID[0]}#")
        if url.startswith("/"):  # gateway without SOUS_PUBLIC_BASE_URL
            url = base + url
        say(_c("1", f"    Waiting for a human. Approve on the phone: {url}"))


# --- scenes ---------------------------------------------------------------------------

def scene_1(gw: Gateway, when: date) -> None:
    """Hotel: 1,000 L lactose-free milk by Monday, split across packs, within the mandate [H01]."""
    agent, mandate = "aurora-purchasing-agent", "M-HOTEL-AURORA"
    gw.why = 'We need 1,000 litres of lactose-free milk delivered on Monday.'
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
    gw.why = 'Swap our usual 400 g chopped tomatoes for a bulk pack.'
    say(_c("1", '"Swap our usual 400 g chopped tomatoes for a bulk pack."') + _c("2", "  (Meilahti hospital kitchen's agent)"))
    subs = gw.get(f"/products/{original}/substitutes", delivery_date=when.isoformat(), amount=40,
                  unit="SALES_UNIT", mandate_id=mandate, agent_id=agent, limit=4,
                  headers=gw._headers(mandate))
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
    gw.why = '300 litres of lactose-free skimmed or semi-skimmed milk, cheapest that arrives Monday.'
    say(_c("1", '"300 litres of lactose-free skimmed or semi-skimmed milk, cheapest that arrives Monday."'))
    q = gw.quote(agent, mandate, [{"product_ids": ["464119", "476240", "464201"], "amount": 300, "unit": "L"}], when)
    show_quote(q)
    show_order(gw.order(agent, mandate, q["id"]), gw.base)
    gw.why = 'No, 500 litres of exactly 464119, the one we always buy.'
    say(_c("1", '"No, 500 litres of exactly 464119, the one we always buy."'))
    q = gw.quote(agent, mandate, [{"product_ids": ["464119"], "amount": 500, "unit": "L"}], when)
    show_quote(q)
    show_order(gw.order(agent, mandate, q["id"]), gw.base)


def scene_4(gw: Gateway, when: date) -> None:
    """Guardrails: over the spend limit -> ask; gluten for the school -> refused [P07, S02]."""
    gw.why = '400 litres of Oatly semi oat drink.'
    say(_c("1", '"400 litres of Oatly semi oat drink."') + _c("2", "  (hospital, auto-approve limit €400)"))
    q = gw.quote("meilahti-kitchen-agent", "M-HOSPITAL-KITCHEN",
                 [{"product_ids": ["470863"], "amount": 400, "unit": "L"}], when)
    show_quote(q)
    show_order(gw.order("meilahti-kitchen-agent", "M-HOSPITAL-KITCHEN", q["id"]), gw.base)
    gw.why = '40 loaves of Pekan Leipä rye bread.'
    say(_c("1", '"40 loaves of Pekan Leipä rye bread."') + _c("2", "  (Espoo schools, gluten-free mandate)"))
    q = gw.quote("espoo-school-agent", "M-SCHOOL-ESPOO",
                 [{"product_ids": ["472463"], "amount": 40, "unit": "SALES_UNIT"}], when)
    show_quote(q)
    show_order(gw.order("espoo-school-agent", "M-SCHOOL-ESPOO", q["id"]), gw.base)


# An event caterer with a mandate that checks milk, egg, tree nuts and peanuts.
KAARNA = {
    "id": "M-KAARNA-CATERING",
    "buyer": "Kaarna Catering Oy, Espoo",
    "agent_id": "kaarna-event-agent",
    "issued_by": "Laura Niemi, kitchen manager",
    "notify_above_eur": 300,
    "auto_approve_limit_eur": 600,
    "hard_limit_eur": 2500,
    "daily_limit_eur": 3000,
    "avoid_allergens": ["AM", "AE", "AN", "AP"],  # milk, egg, tree nuts, peanuts
    "allow_may_contain": False,
    "unknown_allergens": "ask",
    "substitutions": "notify",
    "terminal_products": "ask",
    "min_delivery_reliability": 0.9,
    "later_delivery": "ask",
}


def ensure_mandate(gw: Gateway, m: dict) -> None:
    """Issue the mandate unless the gateway already has it (needs SOUS_APPROVER_CODE if the gateway sets one)."""
    if gw.http.get(f"/mandates/{m['id']}").status_code == 200:
        return
    code = approver_code()
    r = gw.http.post("/mandates", json=m, headers={"x-approver-code": code} if code else {})
    if r.status_code >= 400:
        raise RuntimeError(f"could not issue {m['id']} ({r.status_code}); set SOUS_APPROVER_CODE "
                           "to the gateway's approver code")
    say(_c("2", f"  issued mandate {m['id']} for {m['buyer']} (signed by the gateway)"))


def scene_5(gw: Gateway, when: date) -> None:
    """New buyer: Kaarna Catering requests a five-item lunch order with allergen checks."""
    m = KAARNA
    agent, mandate = m["agent_id"], m["id"]
    ensure_mandate(gw, m)
    gw.why = 'Lunch for 150 guests: oat drink, chickpeas, crushed tomatoes, hummus and rice.'
    say(_c("1", '"Lunch for 150 guests: oat drink, chickpeas, crushed tomatoes, hummus and rice."'))
    say(_c("2", "  Kaarna's mandate checks for milk, egg, tree nuts and peanuts. Missing allergen data requires human review."))
    avoid = {"avoid_allergens": m["avoid_allergens"], "food_only": "true"}
    menu = [("kaurajuoma", "oat drink", 60, "L"), ("kikherne", "chickpeas", 20, "KG"),
            ("tomaattimurska", "crushed tomatoes", 30, "KG"), ("hummus", "hummus", 6, "KG"),
            ("riisi", "rice", 15, "KG")]
    items = []
    for q, label, amount, unit in menu:
        ids = gw.search(agent, q, 6, **avoid)
        items.append({"product_ids": ids, "amount": amount, "unit": unit, "note": label})
        say(f"  searched for {label}: {len(ids)} candidates after excluding declared “contains” and "
            f"“may contain” matches, 0 Valio API calls")
    q = gw.quote(agent, mandate, items, when)
    show_quote(q)
    why = {a["product_id"]: a["why_not"][0] for a in q.get("alternatives_considered", []) if a.get("why_not")}
    for g in q.get("uncovered", []):
        def short(r: str) -> str:
            if "cut-off" in r:
                return "order cut-off already passed"
            if "any date" in r:
                return "not deliverable on any date"
            return r.rstrip(".")
        ps = [p for p in g["item"]["product_ids"] if p in why]
        reasons = [f"{n} × {r}" for r, n in sorted({short(why[p]): sum(short(why[x]) == short(why[p]) for x in ps)
                                                   for p in ps}.items())]
        say(_c("33", f"    ! not covered: {g['uncovered']:g} {g.get('unit') or ''} of “{g['item'].get('note')}”. "
                     f"All {len(ps)} candidates out: {', '.join(reasons) or 'no orderable option'}"))
    show_order(gw.order(agent, mandate, q["id"]), gw.base)
    say(_c("2", "    The reviewer can verify the flagged ingredients and decide whether to accept the partial order."))

    gw.why = 'Add 2 jars of the Biona cashew nut butter for the dressing.'
    say(_c("1", '"Add 2 jars of the Biona cashew nut butter for the dressing."'))
    q = gw.quote(agent, mandate, [{"product_ids": ["465267"], "amount": 2, "unit": "SALES_UNIT"}], when)
    show_quote(q)
    show_order(gw.order(agent, mandate, q["id"]), gw.base)


SCENES = {"1": scene_1, "2": scene_2, "3": scene_3, "4": scene_4, "5": scene_5}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("scenes", nargs="*", help="scene numbers (default: all)")
    ap.add_argument("--base", default="http://localhost:8000", help="gateway base URL")
    ap.add_argument("--date", type=date.fromisoformat, default=None, help="delivery date (default: next Monday)")
    ap.add_argument("--pause", type=float, default=1.5, help="seconds between scenes")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--sandbox", default=None, metavar="new|SBX-XXXX",
                    help="run scenes 1-4 on a judge sandbox's private mandate copies (new = create one)")
    ap.add_argument("--tokens", default=".sous-agent-tokens.json",
                    help="agent token file {base: {mandate_id: token}} (gitignored)")
    a = ap.parse_args()
    if a.list:
        for k, f in SCENES.items():
            print(k, f.__doc__)
        return
    gw = Gateway(a.base, a.tokens)
    health = gw.get("/health")
    if a.sandbox:
        gw.use_sandbox(a.sandbox)
        SANDBOX_ID[0] = gw.sbx
    when = a.date or next_monday()
    storage = f" · storage {health['storage']}" if health.get("storage") else ""
    say(_c("2", f"gateway {a.base} ok{storage} · delivery {when:%a %d.%m.%Y}"))
    ui = f"{a.base}/ui/" + (f"?sandbox={gw.sbx}" if gw.sbx else "")
    say(_c("2", f"watch: {ui}#/inbox   audit: {ui}#/audit"))
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
