"""Standing-order drift demo: Hotel Aurora's weekly dairy order creeps up, and the gateway asks first.

    uv run python scripts/demo_standing.py                        # local gateway
    uv run python scripts/demo_standing.py --base https://<host>  # a deployed one (--code if it needs one)

1. The hotel's agent saves a weekly standing order (milk, butter, sour cream every Monday).
2. Four past renewals are stored as history, marked simulated=True: the amounts are made up
   (milk creeping 420 -> 455 L), priced at one real signed quote from Valio Aimo's live API.
   No Valio response is invented.
3. This week's renewal runs live: quote, drift check against the 4-week median, order. Milk is
   up ~18 % and the total over 10 %, so the order is held for a human instead of auto-renewing.

Needs the demo mandates (scripts/seed.py) and, if the gateway has one, the approver code
(SOUS_APPROVER_CODE or --code) for storing the simulated history.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from demo import TIER, Gateway, _c, next_monday, say  # noqa: E402

MANDATE, AGENT = "M-HOTEL-AURORA", "aurora-purchasing-agent"
ITEMS = [  # candidates per item; the gateway picks the cheapest orderable each week
    {"label": "milk", "product_ids": ["464201", "464073"], "amount": 420, "unit": "L"},          # lactose-free 1 l
    {"label": "butter", "product_ids": ["464411", "476584"], "amount": 20, "unit": "KG"},        # Valio 500 g
    {"label": "sour cream", "product_ids": ["470084", "476548"], "amount": 12, "unit": "KG"},    # lactose-free
]
HISTORY = [[420, 20, 12], [430, 20, 12], [440, 21, 12], [455, 21, 12]]  # oldest first, milk creeping
THIS_WEEK = [515, 22, 12]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="http://localhost:8000", help="gateway base URL")
    ap.add_argument("--date", type=date.fromisoformat, default=None, help="delivery date (default: next Monday)")
    ap.add_argument("--code", default=os.getenv("SOUS_APPROVER_CODE", ""), help="approver code, if required")
    a = ap.parse_args()
    gw = Gateway(a.base)
    if a.code:
        gw.http.headers["X-Approver-Code"] = a.code
    when = a.date or next_monday()
    say(_c("2", f"gateway {a.base} · storage {gw.get('/health').get('storage')} · delivery {when:%a %d.%m.%Y}"))

    so = gw.post("/standing-orders", {"mandate_id": MANDATE, "agent_id": AGENT, "weekday": "mon",
                                      "name": "Hotel Aurora weekly dairy", "items": ITEMS})
    say("\n" + _c("1", f"1. Standing order {so['id']}: {so['name']}, every Monday"))
    for it in so["items"]:
        say(f"    {it['amount']:g} {it['unit']} {it['label']}")

    q = gw.quote(AGENT, MANDATE, [{k: it[k] for k in ("product_ids", "amount", "unit")} for it in ITEMS], when)
    calls = sum(v for k, v in (q.get("valio_api_calls") or {}).items() if k != "cache_hits")
    say("\n" + _c("1", "2. History: 4 past renewals, SIMULATED for the demo"))
    say(_c("2", f"    amounts made up; priced at live signed quote {q['id']} ({calls} Valio API call(s))"))
    sim = gw.post(f"/standing-orders/{so['id']}/simulate-history",
                  {"by": "demo_standing.py", "quote_id": q["id"], "weeks": HISTORY})["simulated"]
    for r in sim:
        amounts = ", ".join(f"{i['amount']:g} {i['unit']} {i['label']}" for i in r["items"])
        say(f"    {r['delivery_date']}  {amounts}  €{r['total_ex_vat']:,.2f}  " + _c("2", "(simulated)"))

    say("\n" + _c("1", f"3. This week's renewal, live: " + ", ".join(
        f"{n:g} {it['unit']} {it['label']}" for n, it in zip(THIS_WEEK, ITEMS))))
    r = gw.post(f"/standing-orders/{so['id']}/renew",
                {"agent_id": AGENT, "delivery_date": when.isoformat(), "amounts": THIS_WEEK})
    ren, order, d = r["renewal"], r["order"], r["drift"]
    for i in ren["items"]:
        names = ", ".join(f"{p['quantity']:g} × {p['name']}" for p in i["products"]) or "nothing orderable"
        say(f"    {i['label']}: {names}  €{i['total_ex_vat']:,.2f}")
    say(_c("2", f"    total €{ren['total_ex_vat']:,.2f} ex VAT vs 4-week median "
                f"€{d['baseline']['median_total_ex_vat']:,.2f}"))
    for f in d["findings"]:
        say(_c("33", f"    ! {f['rule']}: {f['message']}"))
    color, label = TIER[order["decision"]["outcome"]]
    say("\n  " + _c(f"1;{color}", f"{label}") + f"  {order['id']} {order['status']}: {d['summary']}")
    for f in order["decision"]["findings"]:
        if not f["rule"].startswith("standing.") and f["outcome"] != "silent":
            say(_c("2", f"    also: {f['rule']}: {f['message']}"))
    if order.get("approval_url"):
        url = order["approval_url"]
        say(f"    approve or reject: {a.base + url if url.startswith('/') else url}")
    say(_c("2", f"\n  history + baseline: {a.base}/standing-orders/{so['id']}"))


if __name__ == "__main__":
    main()
