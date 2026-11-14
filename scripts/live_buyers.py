"""Virtual buyer companies for the live view (web/live.html): cheap-model agents ordering through the gateway.

Three made-up companies, each with its own signed mandate (issued once; demo_public so the live view shows them),
order every few minutes in their own words. A cheap model writes each company's request (varied: everyday
orders, the occasional one over its limit or with a product its mandate avoids), and the reference buyer agent
(agent/buyer.py, the same cheap model) turns it into the gateway's MCP tool calls. The mandate engine decides,
as for any agent. Without a model key it runs scripted requests through REST instead (no LLM, same gateway).

    SOUS_APPROVER_CODE=... GEMINI_API_KEY=... uv run python scripts/live_buyers.py --base <gateway> --minutes 30

Cost guards: one order at a time, --every seconds apart (default 150), --max-orders (default 20); the gateway's
own Valio budget still applies. Agent tokens are kept in the gitignored token file (as scripts/demo.py does).
Held orders are left for a human: approve them on the phone or in the seller UI, and the live view shows it.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.demo import Gateway, approver_code, ensure_mandate, next_monday, say, _c  # noqa: E402

PERSONAS = [
    {"mandate": {"id": "M-LIVE-KAHVILA-PURSI", "buyer": "Kahvila Pursi, Turku", "agent_id": "pursi-cafe-agent",
                 "issued_by": "Sanna Aalto, owner", "demo_public": True, "require_agent_auth": True,
                 "notify_above_eur": 150, "auto_approve_limit_eur": 300, "hard_limit_eur": 1200, "daily_limit_eur": 900,
                 "substitutions": "notify", "first_time_products": "notify", "terminal_products": "notify"},
     "who": "a small harbour café in Turku: coffee drinks, pastries, soups and sandwiches for about 150 guests a day",
     "scripted": [("kaurajuoma barista", 24, "L"), ("laktoositon maito", 40, "L"), ("voi", 5, "KG"), ("kerma", 10, "L")]},
    {"mandate": {"id": "M-LIVE-PAIVAKOTI-SATAKIELI", "buyer": "Päiväkoti Satakieli, Vantaa", "agent_id": "satakieli-daycare-agent",
                 "issued_by": "Mervi Laakso, head of the kitchen", "demo_public": True, "require_agent_auth": True,
                 "auto_approve_limit_eur": 250, "hard_limit_eur": 1000, "avoid_allergens": ["AN", "AP", "AE"],
                 "allow_may_contain": False, "unknown_allergens": "ask", "substitutions": "ask",
                 "first_time_products": "ask"},
     "who": "a daycare kitchen in Vantaa feeding 60 children; no nuts, peanuts or egg in the kitchen",
     "scripted": [("laktoositon maito", 60, "L"), ("ruisleipä", 12, "KG"), ("kaurahiutale", 8, "KG"), ("jogurtti", 20, "KG")]},
    {"mandate": {"id": "M-LIVE-LOUNAS-KIDE", "buyer": "Lounasravintola Kide, Tampere", "agent_id": "kide-lunch-agent",
                 "issued_by": "Joni Heikkinen, restaurant manager", "demo_public": True, "require_agent_auth": True,
                 "notify_above_eur": 500, "auto_approve_limit_eur": 900, "hard_limit_eur": 3000, "daily_limit_eur": 2500,
                 "substitutions": "notify", "first_time_products": "ask", "max_price_change_pct": 15},
     "who": "a lunch restaurant in Tampere serving 300 lunches a day: soups, stews, salads",
     "scripted": [("peruna", 80, "KG"), ("kerma", 30, "L"), ("juusto", 12, "KG"), ("tomaattimurska", 25, "KG")]},
]

ASK = ("You write one purchase request, as the buyer would type it to their purchasing agent, for {who}. "
       "Delivery is {day}. Name 1 to 3 everyday foodservice products with quantities in litres, kilos or packs. "
       "Usually an ordinary amount; about one time in five make it unusually large, or ask for something the "
       "kitchen should not have. One sentence, plain English, no preamble.")


def model_request(chat, persona: dict, day: date) -> str:
    r = chat.chat([{"role": "user", "content": ASK.format(who=persona["who"], day=f"{day:%A %d.%m.}")}])
    return " ".join((r.content or "").split())[:300] or "Two kilos of butter for Monday, please."


def run_llm(gw: Gateway, persona: dict, day: date, chat, cfg) -> dict:
    from agent.buyer import BuyerAgent, BuyerConfig, MCPGateway
    m = persona["mandate"]
    sentence = model_request(chat, persona, day)
    token = gw._headers(m["id"]).get("authorization", "").removeprefix("Bearer ") or None
    with MCPGateway(gw.base, agent_token=token) as mcp:
        run = BuyerAgent(chat, mcp, BuyerConfig(m["id"], m["agent_id"], date.today(), max_tool_calls=10)).run(sentence)
    s = run.summary()
    return {"request": sentence, "order": s.get("order"), "cost_usd": cfg.cost_usd(run.prompt_tokens, run.completion_tokens)}


def run_scripted(gw: Gateway, persona: dict, day: date) -> dict:
    m = persona["mandate"]
    q, amount, unit = random.choice(persona["scripted"])
    amount = round(amount * random.choice([0.5, 1, 1, 1.5, 4]), 1)  # sometimes far over the usual
    sentence = f"{amount:g} {unit.lower()} of {q} for {day:%A}"
    gw.why = sentence
    ids = gw.search(m["agent_id"], q, 6, **({"avoid_allergens": m["avoid_allergens"]} if m.get("avoid_allergens") else {}))
    if not ids:
        return {"request": sentence, "order": None}
    quote = gw.quote(m["agent_id"], m["id"], [{"product_ids": ids, "amount": amount, "unit": unit}], day)
    o = gw.order(m["agent_id"], m["id"], quote["id"])
    return {"request": sentence, "order": {"id": o["id"], "status": o["status"], "total_ex_vat": o["total_ex_vat"]}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", required=True)
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--every", type=float, default=150, help="seconds between orders (default 150)")
    ap.add_argument("--max-orders", type=int, default=20)
    ap.add_argument("--provider", default=os.getenv("LLM_PROVIDER", "gemini"))
    ap.add_argument("--model", default=os.getenv("LIVE_MODEL", "gemini-3.8-flash"), help="a cheap model")
    ap.add_argument("--scripted", action="store_true", help="no model: scripted requests over REST")
    ap.add_argument("--tokens", default=".sous-agent-tokens.json")
    a = ap.parse_args()
    if not approver_code():
        sys.exit("Set SOUS_APPROVER_CODE: the virtual companies' mandates are issued (once) with it.")
    gw = Gateway(a.base, a.tokens)
    for p in PERSONAS:
        ensure_mandate(gw, p["mandate"])
    chat = cfg = None
    if not a.scripted:
        from agent.llm import ChatClient, config_from_env, key_present
        if key_present(a.provider):
            cfg = config_from_env(a.provider, a.model)
            cfg.temperature, cfg.max_tokens = 0.9, 600
            chat = ChatClient(cfg)
        else:
            say(_c("33", f"no {a.provider} key: scripted requests instead (no model)"))
    day = next_monday()
    stop, n, spent = time.time() + a.minutes * 60, 0, 0.0
    say(_c("1", f"virtual buyers on {a.base}: {len(PERSONAS)} companies, one order every {a.every:g} s, "
                f"at most {a.max_orders}, {'model ' + cfg.label if cfg else 'scripted'}"))
    while time.time() < stop and n < a.max_orders:
        p = random.choice(PERSONAS)
        try:
            r = run_llm(gw, p, day, chat, cfg) if chat else run_scripted(gw, p, day)
            o = r.get("order") or {}
            spent += r.get("cost_usd") or 0
            say(f"{p['mandate']['buyer'].split(',')[0]:<28} \"{r['request']}\" → "
                + (f"{o.get('id')} {o.get('status')} €{o.get('total_ex_vat', 0):,.2f}" if o else "no order"))
        except Exception as e:  # noqa: BLE001 - one company's bad turn never stops the others
            say(_c("31", f"{p['mandate']['buyer']}: {str(e)[:200]}"))
        n += 1
        time.sleep(a.every * random.uniform(0.7, 1.3))
    say(_c("2", f"done: {n} turns" + (f", model cost ~${spent:.3f}" if cfg else "")))


if __name__ == "__main__":
    main()
