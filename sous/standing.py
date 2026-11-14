"""Standing orders: a kitchen's recurring weekly order, renewed by its agent, with drift detection.

The gateway remembers what each renewal actually ordered (quantities, chosen products, price per
L/KG/unit, total, Terminal and reliability issues). Before a renewal is placed it compares the new
live quote with that history, deterministically, and every anomaly becomes a finding that lifts
the order to at least "ask":

- standing.quantity_creep:   an item's amount is > +15 % over the median of the last 4 renewals
- standing.quantity_trend:   an item's amount went up 3 renewals in a row
- standing.cost_drift:       the total is > +10 % over the median total
- standing.price_drift:      an item's price per L/KG/unit is > +10 % over its median
- standing.product_switched: a different product was chosen than last time
- standing.new_terminal / standing.new_reliability: a Terminal (supplier-stock) or low-reliability
  product appears where the last renewal had none

Renewal timing is cut-off aware: next_renewal_at = the next delivery's order cut-off (estimated from the
last real renewal's Valio closingTime, else 17:00 the day before) minus renew_lead_hours (default
SOUS_RENEW_LEAD_HOURS = 24), so a held renewal still leaves the human time to approve; its order's approve_by
is that cut-off minus the safety margin (sous/deadline.py). A delivery whose cut-off has passed is skipped.

The baseline is the renewals that ended up ordered (approved drift becomes the new normal);
refused, rejected and still-pending renewals don't count. Synthetic history for demos is stored
with simulated=True and priced from a real signed quote, never from made-up Valio responses.
"""

from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from . import catalog, config, deadline, quote as Q, service as S, signing, standing_store as SS, store
from .mandate import Finding

HISTORY_WEEKS = 4        # baseline = median of this many past renewals
QTY_CREEP = 0.15         # +15 % amount vs median
TREND_RUN = 3            # this many increases in a row
COST_DRIFT = 0.10        # +10 % total or price per unit vs median
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
RENEW_LEAD_HOURS = float(config.env("RENEW_LEAD_HOURS", "24"))  # renew this long before the order cut-off


class StandingItem(BaseModel):
    product_ids: list[str] = Field(min_length=1, max_length=20,
                                   description="Candidate products; the gateway picks the cheapest orderable each week")
    amount: float = Field(gt=0, description="Usual weekly amount")
    unit: Q.Unit = Field("SALES_UNIT", description=Q.UNIT_DOC)
    label: str | None = Field(None, description="Short name for messages, e.g. 'milk'")
    purpose: str | None = Field(None, max_length=120, description="What it is for, e.g. 'coffee' or 'recipe: "
                                "whipped desserts'; decides how strictly a renewal may substitute it")
    substitution: Literal["exact", "ask", "allowed"] | None = Field(
        None, description="exact | ask | allowed (default read from the purpose); see QuoteItem.substitution")


class StandingOrderIn(BaseModel):
    mandate_id: str
    agent_id: str
    name: str = Field(min_length=1, max_length=200)
    items: list[StandingItem] = Field(min_length=1, max_length=20)
    weekday: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = Field(
        "mon", description="Weekly delivery day")
    renew_lead_hours: float | None = Field(
        None, ge=0, le=168, description="Renew this many hours before the next delivery's order cut-off, so a held "
                                        "renewal can still be approved in time (default SOUS_RENEW_LEAD_HOURS, 24)")


# --- standing orders --------------------------------------------------------

def create(req: StandingOrderIn) -> dict:
    m = S._mandate(req.mandate_id)
    if req.agent_id != m.agent_id:
        raise S.GatewayError("standing_agent_mismatch",
                             f"Agent '{req.agent_id}' is not the holder of mandate {m.id}.", 403)
    items = []
    for it in req.items:
        missing = [p for p in it.product_ids if not catalog.get(p)]
        if missing:
            raise S.GatewayError("product_not_found", f"No product {', '.join(missing)}.", 404)
        items.append({**it.model_dump(), "label": it.label or catalog.get(it.product_ids[0])["name"]})
    now = S._now().isoformat(timespec="milliseconds")
    so = {"id": S._id("SO"), "mandate_id": m.id, "agent_id": req.agent_id, "buyer": m.buyer, "name": req.name,
          "items": items, "cadence": {"every": "week", "weekday": req.weekday}, "active": True,
          "renew_lead_hours": req.renew_lead_hours if req.renew_lead_hours is not None else RENEW_LEAD_HOURS,
          "created_at": now, "updated_at": now}
    SS.put(so)
    store.audit("standing.created", req.agent_id, "agent", so["id"], m.buyer, {"standing_order": so})
    return so


def _get(standing_id: str) -> dict:
    so = SS.get(standing_id)
    if not so:
        raise S.GatewayError("standing_not_found", f"No standing order {standing_id}.", 404)
    return so


def list_(mandate_id: str | None = None) -> list[dict]:
    return SS.list_(mandate_id)


def set_active(standing_id: str, active: bool, by: str) -> dict:
    so = _get(standing_id)
    so["active"] = active
    so["updated_at"] = S._now().isoformat(timespec="milliseconds")
    # a hold from a needs change (sous/needs.py) ends only when a human resumes; the audit keeps which one it was
    cleared = so.pop("needs_hold", None) if active else None
    SS.put(so)
    store.audit("standing.resumed" if active else "standing.paused", by, "human", standing_id, so["buyer"],
                {"needs_review_id": cleared["review_id"]} if cleared else {})
    return so


def get(standing_id: str) -> dict:
    """The standing order, its renewals (newest first, with each order's current status) and the drift baseline."""
    so = _get(standing_id)
    rens = [_with_status(r) for r in SS.renewals(standing_id)]
    hist = [r for r in rens if r["status"] == "ordered"][-HISTORY_WEEKS:]
    sched = schedule(so)
    return {"standing_order": so, "renewals": rens[::-1], "baseline": _baseline(so, hist),
            "next_delivery_date": sched["next_delivery_date"], "cutoff": sched["cutoff"],
            "next_renewal_at": sched["next_renewal_at"], "renewal_schedule": sched,
            "versions": SS.versions(standing_id),
            "rules": {"history_weeks": HISTORY_WEEKS, "quantity_creep": QTY_CREEP, "trend_run": TREND_RUN,
                      "cost_drift": COST_DRIFT}}


def next_delivery(so: dict, today: date | None = None, now: datetime | None = None) -> date:
    """The next delivery weekday whose order cut-off (estimated) hasn't passed yet."""
    now = now or S._now()
    today = today or now.date()
    wd = WEEKDAYS.index(so["cadence"]["weekday"])
    d = today + timedelta(days=(wd - today.weekday()) % 7 or 7)
    if datetime.fromisoformat(estimate_cutoff(so, d)["at"]) <= now:
        d += timedelta(weeks=1)
    return d


def _cutoff_reference(so: dict) -> list[tuple[str, str]]:
    """(earliest order cut-off, delivery date) of the newest real renewal whose order has a cut-off."""
    for r in reversed(SS.renewals(so["id"])):
        o = store.get_order(r["order_id"]) if r.get("order_id") else None
        cuts = [l["order_cutoff"] for l in (o or {}).get("lines") or [] if l.get("order_cutoff")]
        if cuts:
            return [(min(cuts, key=datetime.fromisoformat), r["delivery_date"])]
    return []


def estimate_cutoff(so: dict, delivery: date) -> dict:
    return deadline.estimate_cutoff(delivery, _cutoff_reference(so))


def schedule(so: dict, now: datetime | None = None) -> dict:
    """When to renew: the next delivery's (estimated) order cut-off minus renew_lead_hours.
    renew_due = it's time (or past time) to renew for that delivery."""
    now = now or S._now()
    nxt = next_delivery(so, now=now)
    cut = estimate_cutoff(so, nxt)
    lead = so.get("renew_lead_hours", RENEW_LEAD_HOURS)
    at = datetime.fromisoformat(cut["at"]) - timedelta(hours=lead)
    return {"next_delivery_date": nxt.isoformat(), "cutoff": cut, "renew_lead_hours": lead,
            "next_renewal_at": at.isoformat(timespec="seconds"), "renew_due": now >= at,
            "approve_by_margin_minutes": deadline.MARGIN_MIN}


def _with_status(r: dict) -> dict:
    """A live renewal's status follows its order (pending -> ordered after approval)."""
    if r.get("order_id") and (o := store.get_order(r["order_id"])):
        r = {**r, "status": o["status"]}
    return r


# --- snapshots and drift ----------------------------------------------------

def _snapshot(items: list[dict], q: dict) -> dict:
    """Per standing item: amount, chosen products, total, price per need unit, Terminal / issue codes."""
    groups: list[list[dict]] = [[] for _ in items]
    i = 0
    for l in q["lines"]:  # lines come in item order; an item can be split over several products
        while i < len(items) and l["product_id"] not in items[i]["product_ids"]:
            i += 1
        if i == len(items):
            break
        groups[i].append(l)
    out = []
    for it, ls in zip(items, groups):
        total = round(sum(l.get("line_total_ex_vat") or 0 for l in ls), 2)
        out.append({
            "label": it["label"], "amount": it["amount"], "unit": it["unit"],
            "product_ids": [l["product_id"] for l in ls],
            "products": [{"product_id": l["product_id"], "name": l["name"], "quantity": l["quantity"],
                          "unit_price_ex_vat": l.get("unit_price_ex_vat"),
                          "comparison_price": l.get("comparison_price")} for l in ls],
            "total_ex_vat": total,
            "price_per_unit": round(total / it["amount"], 4) if ls and total else None,
            "logistic_models": sorted({l.get("logistic_model") or "?" for l in ls}),
            "issue_codes": sorted({i["code"] for l in ls for i in l.get("issues", [])}),
        })
    return {"items": out, "total_ex_vat": round(sum(x["total_ex_vat"] for x in out), 2)}


def _unit(u: str) -> str:
    return "units" if u == "SALES_UNIT" else u


def _median(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _names(item: dict) -> str:
    return ", ".join(p["name"] for p in item["products"]) or "nothing"


def _baseline(so: dict, hist: list[dict]) -> dict:
    items = []
    for k, it in enumerate(so["items"]):
        past = [h["items"][k] for h in hist if k < len(h["items"])]
        items.append({"label": it["label"], "unit": it["unit"],
                      "median_amount": _median([p["amount"] for p in past]),
                      "median_price_per_unit": _median([p["price_per_unit"] for p in past]),
                      "amounts": [p["amount"] for p in past],
                      "last_products": past[-1]["product_ids"] if past else None})
    return {"weeks": len(hist), "median_total_ex_vat": _median([h["total_ex_vat"] for h in hist]),
            "totals": [h["total_ex_vat"] for h in hist], "items": items,
            "from": [{"delivery_date": h["delivery_date"], "simulated": h.get("simulated", False)} for h in hist]}


def drift(so: dict, snap: dict, hist: list[dict]) -> tuple[list[Finding], dict]:
    """Compare this week's snapshot with past ordered renewals (oldest first). Pure; no I/O."""
    hist = hist[-HISTORY_WEEKS:]
    base = _baseline(so, hist)
    f: list[Finding] = []
    base["item_rules"] = [[] for _ in snap["items"]]  # which rules fired per item, for summary()

    def flag(rule, message, pid=None, item=None):
        f.append(Finding(rule=rule, outcome="ask", message=message, product_id=pid))
        if item is not None:
            base["item_rules"][item].append(rule)

    if not hist:
        return f, base
    n = len(hist)
    for k, (cur, b) in enumerate(zip(snap["items"], base["items"])):
        label, u = cur["label"], _unit(cur["unit"])
        add = lambda rule, message, pid=None: flag(rule, message, pid, k)  # noqa: E731
        med = b["median_amount"]
        if med and cur["amount"] > med * (1 + QTY_CREEP):
            add("standing.quantity_creep",
                f"{label}: {cur['amount']:g} {u} is up {cur['amount'] / med - 1:.0%} vs your {n}-week median "
                f"of {med:g} {u}.")
        run = b["amounts"][-TREND_RUN:] + [cur["amount"]]
        if len(run) == TREND_RUN + 1 and all(a < c for a, c in zip(run, run[1:])):
            add("standing.quantity_trend",
                f"{label} has gone up {TREND_RUN} renewals in a row: {' → '.join(f'{a:g}' for a in run)} {u}.")
        mp = b["median_price_per_unit"]
        if mp and cur["price_per_unit"] and cur["price_per_unit"] > mp * (1 + COST_DRIFT):
            add("standing.price_drift",
                f"{label}: €{cur['price_per_unit']:.2f}/{u} is {cur['price_per_unit'] / mp - 1:+.0%} vs the "
                f"median €{mp:.2f}/{u}.")
        last = next((h["items"][k] for h in reversed(hist) if k < len(h["items"]) and h["items"][k]["product_ids"]),
                    None)
        if last and cur["product_ids"] and set(cur["product_ids"]) != set(last["product_ids"]):
            add("standing.product_switched", f"{label}: now {_names(cur)}; last time {_names(last)}.",
                cur["product_ids"][0])
        if last and "Terminal" in cur["logistic_models"] and "Terminal" not in last["logistic_models"]:
            add("standing.new_terminal",
                f"{label} now ships from the supplier's warehouse (Terminal, stock unknown); last time it didn't.")
        if last and "LOW_RELIABILITY" in cur["issue_codes"] and "LOW_RELIABILITY" not in last["issue_codes"]:
            add("standing.new_reliability", f"{label}: the chosen product has a poor delivery record; "
                                            "last time's didn't.")
    mt = base["median_total_ex_vat"]
    if mt and snap["total_ex_vat"] > mt * (1 + COST_DRIFT):
        d = snap["total_ex_vat"] - mt
        flag("standing.cost_drift",
             f"Total €{snap['total_ex_vat']:,.2f} is +€{d:,.2f} ({d / mt:+.0%}) vs the {n}-week median "
             f"€{mt:,.2f}.")
    return f, base


def summary(findings: list[Finding], snap: dict, base: dict) -> str:
    """One line for the human: 'milk up 18% vs your 4-week median; cost +€212; asking before renewing'."""
    if not base["weeks"]:
        return "First renewal: no history yet, so no drift baseline."
    parts = []
    for cur, b, rules in zip(snap["items"], base["items"], base.get("item_rules") or [[]] * len(snap["items"])):
        if "standing.quantity_creep" in rules or "standing.quantity_trend" in rules:
            parts.append(f"{cur['label']} up {cur['amount'] / b['median_amount'] - 1:.0%} vs your "
                         f"{base['weeks']}-week median")
        if "standing.price_drift" in rules:
            parts.append(f"{cur['label']} price {cur['price_per_unit'] / b['median_price_per_unit'] - 1:+.0%}")
        if "standing.product_switched" in rules:
            parts.append(f"{cur['label']} product switched")
        if "standing.new_terminal" in rules or "standing.new_reliability" in rules:
            parts.append(f"{cur['label']} supply risk")
    if any(x.rule == "standing.cost_drift" for x in findings):
        parts.append(f"cost +€{snap['total_ex_vat'] - base['median_total_ex_vat']:,.0f}")
    if not parts:
        return f"In line with the last {base['weeks']} renewals."
    return "; ".join(parts) + "; asking before renewing"


# --- renewal ----------------------------------------------------------------

def _items(so: dict, amounts: list[float] | None) -> list[dict]:
    if amounts is None:
        return so["items"]
    if len(amounts) != len(so["items"]) or any(a is None or a <= 0 for a in amounts):
        raise S.GatewayError("invalid_argument",
                             f"amounts must list {len(so['items'])} positive numbers, one per item.")
    return [{**it, "amount": float(a)} for it, a in zip(so["items"], amounts)]


def renew(standing_id: str, delivery_date: date | None = None, *, agent_id: str | None = None,
          amounts: list[float] | None = None, agent_token: str | None = None,
          rationale: str | None = None) -> dict:
    """Quote this week's order live, check it for drift against past renewals, and place it.

    `amounts` (one per item) are this week's amounts if they differ from the standing order's usual ones.
    Drift findings join the mandate's own rules, so any drift means the order waits for a human (ask).
    """
    so = _get(standing_id)
    if not so["active"]:
        raise S.GatewayError("standing_paused", f"Standing order {standing_id} is paused.", 409)
    if agent_id and agent_id != so["agent_id"]:
        raise S.GatewayError("standing_agent_mismatch",
                             f"Standing order {standing_id} belongs to agent '{so['agent_id']}'.", 403)
    S.authorize_agent(so["mandate_id"], agent_id or so["agent_id"], agent_token, "renew_standing_order")
    when = delivery_date or next_delivery(so)
    rens = [_with_status(r) for r in SS.renewals(standing_id)]
    for r in rens:
        if not r.get("simulated") and r["delivery_date"] == when.isoformat() and \
                r["status"] in ("ordered", "pending_approval"):
            raise S.GatewayError("already_renewed",
                                 f"Standing order {standing_id} was already renewed for {when}: order "
                                 f"{r['order_id']} is {r['status']}.", 409)
    items = _items(so, amounts)

    with S.aimo.background():  # a renewal never takes the Valio calls kept for interactive quotes (a sandbox's do)
        q = S.request_quote(S.QuoteRequest(
            agent_id=so["agent_id"], mandate_id=so["mandate_id"], delivery_date=when,
            items=[S.QuoteItem(product_ids=it["product_ids"], amount=it["amount"], unit=it["unit"],
                               purpose=it.get("purpose"), substitution=it.get("substitution")) for it in items],
            rationale=rationale or f"Weekly renewal of standing order {so['name']}",
        ), agent_token=agent_token)["quote"]
    return place_renewal(so, items, q, when, rens, agent_token=agent_token, rationale=rationale)


def place_renewal(so: dict, items: list[dict], q: dict, when: date, rens: list[dict], *,
                  agent_token: str | None = None, rationale: str | None = None, mark: dict | None = None) -> dict:
    """Check a renewal's (already issued, signed) quote for drift and place the order. renew() and the
    sandbox seed (sous/sandbox.py) both end here."""
    standing_id = so["id"]
    hist = [r for r in rens if r["status"] == "ordered" and r["delivery_date"] < when.isoformat()]
    snap = _snapshot(items, q)
    findings, base = drift(so, snap, hist)
    text = summary(findings, snap, base)
    store.audit("standing.renewal_checked", so["agent_id"], "agent", standing_id, so["buyer"],
                {"standing_id": standing_id, "quote_id": q["id"], "delivery_date": when.isoformat(),
                 "amounts": [it["amount"] for it in items], "total_ex_vat": snap["total_ex_vat"],
                 "drift": [x.model_dump() for x in findings], "summary": text,
                 "baseline": {"weeks": base["weeks"], "median_total_ex_vat": base["median_total_ex_vat"],
                              "median_amounts": [b["median_amount"] for b in base["items"]],
                              "simulated_weeks": sum(1 for x in base["from"] if x["simulated"])}})

    tries = sum(1 for r in rens if not r.get("simulated") and r["delivery_date"] == when.isoformat())
    order = S.place_order(so["agent_id"], so["mandate_id"], q["id"],
                          idempotency_key=f"standing:{standing_id}:{when}:{tries + 1}", extra_findings=findings,
                          agent_token=agent_token, rationale=rationale)
    ren = {"id": S._id("SR"), "standing_id": standing_id, "delivery_date": when.isoformat(), "simulated": False,
           "quote_id": q["id"], "order_id": order["id"], "status": order["status"],
           "outcome": order["decision"]["outcome"], **snap,
           "drift": [x.model_dump() for x in findings], "summary": text,
           "approve_by": order.get("approve_by"),
           "created_at": S._now().isoformat(timespec="milliseconds"), **(mark or {})}
    SS.add_renewal(ren)
    store.audit("standing.renewed", so["agent_id"], "agent", standing_id, so["buyer"],
                {"standing_id": standing_id, "renewal_id": ren["id"], "order_id": order["id"],
                 "status": order["status"], "outcome": ren["outcome"], "drift_rules": [x.rule for x in findings],
                 "approve_by": order.get("approve_by")})
    return {"renewal": ren, "order": order,
            "drift": {"findings": ren["drift"], "summary": text, "baseline": base}}


# --- demo history -----------------------------------------------------------

def simulate_history(standing_id: str, quote_id: str, weeks: list[list[float]], by: str,
                     priced_from: str = "live quote") -> dict:
    """Store synthetic past renewals (simulated=True) so a demo has a baseline to drift from.

    `weeks` = amounts per item, oldest first, delivered weekly up to the week before the quote's
    delivery date. Each is priced at the live signed quote `quote_id` (same products, same price per
    L/KG/unit): only the amounts are made up, no Valio response is.
    """
    so = _get(standing_id)
    row = store.get_quote(quote_id)
    q = signing.verify(row["_jws"], "sous-quote+jws") if row else None
    if not q or q.get("mandate_id") != so["mandate_id"]:
        raise S.GatewayError("quote_not_found", f"No signed quote {quote_id} under mandate {so['mandate_id']}.", 404)
    priced = _snapshot(so["items"], q)["items"]
    first = date.fromisoformat(q["requested_delivery_date"]) - timedelta(weeks=len(weeks))
    out = []
    for w, amounts in enumerate(weeks):
        items = []
        for p, a in zip(priced, _items(so, amounts)):
            total = round((p["price_per_unit"] or 0) * a["amount"], 2)
            items.append({**p, "amount": a["amount"], "total_ex_vat": total,
                          "products": [{**x, "quantity": None} for x in p["products"]]})
        ren = {"id": S._id("SR"), "standing_id": standing_id,
               "delivery_date": (first + timedelta(weeks=w)).isoformat(), "simulated": True,
               "quote_id": None, "order_id": None, "status": "ordered", "outcome": "simulated",
               "items": items, "total_ex_vat": round(sum(x["total_ex_vat"] for x in items), 2), "drift": [],
               "summary": f"Simulated for the demo: amounts given by {by}, priced at {priced_from} {quote_id}.",
               "created_at": S._now().isoformat(timespec="milliseconds")}
        SS.add_renewal(ren)
        out.append(ren)
    store.audit("standing.history_simulated", by, "human", standing_id, so["buyer"],
                {"standing_id": standing_id, "simulated": True, "priced_from_quote": quote_id,
                 "weeks": [{"delivery_date": r["delivery_date"], "amounts": [i["amount"] for i in r["items"]],
                            "total_ex_vat": r["total_ex_vat"]} for r in out]})
    return {"standing_id": standing_id, "simulated": out}
