"""Lost demand: what buyers' agents wanted from Valio Aimo and did not get, and why. Valio's own view.

Three measures, all from data the gateway already has (orders and the audit log), no Valio API calls:

1. Orders that did not go through, by cause, with their value (ex VAT):
   - supply:   Valio couldn't supply it: a blocking availability issue (out of stock, cut-off passed, sales block,
               undeliverable, no price, partial stock refused, allocated to others), a product hold, or agent
               ordering paused. This is the part Valio can act on.
   - too_late: held for the buyer's human, and the approval came too late (expired, past the approval deadline).
   - buyer:    refused by the buyer's own mandate (spend limits, allergens, claims, categories, autonomy): demand
               the buyer chose not to place.
   - declined: the buyer's human said no.
2. Quantity no product could cover: the `uncovered` part of every quote (an agent asked for 300 L, the orderable
   candidates covered 180 L), per item and unit, with the candidate products.
3. Why candidates were passed over: the blocking issue codes on quote lines (e.g. 22 products skipped because their
   cut-off had passed), per code and per product.

Judge sandboxes and QA test data are left out, like in the default UI views. Buyer names appear only in the
per-buyer breakdown, which needs the global approver code (GET /stats/lost-demand); the public response has totals.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from . import catalog, config, sandbox, store

SUPPLY_RULES = ("availability.", "product.held", "ordering.paused")
LATE = {"expired", "expired_cutoff"}
QA_AGENT, QA_MANDATE, QA_BUYER = re.compile(r"^qa[-_:]", re.I), re.compile(r"^M-QA-", re.I), re.compile(r"^QA\b")
ISSUE_TEXT = {
    "CUTOFF_PASSED": "order cut-off passed", "OUT_OF_STOCK": "out of stock", "SALES_BLOCK": "sales block",
    "UNDELIVERABLE": "not deliverable on any date", "NO_PRICE": "no price", "NO_AVAILABILITY": "no availability",
    "PRODUCT_HELD": "on hold (recall, quality, sales stop)", "ALLOCATED": "stock already promised to others",
    "PARTIAL_STOCK": "less stock than needed", "LATER_DATE": "only on a later date", "UNIT_MISMATCH": "unit mismatch",
    "CONTENT_UNKNOWN": "pack content unknown",
}


def _qa(agent: str | None, mandate: str | None, buyer: str | None) -> bool:
    return bool(QA_AGENT.match(agent or "") or QA_MANDATE.match(mandate or "") or QA_BUYER.match(buyer or ""))


def _cause(o: dict) -> str | None:
    """Why an order that didn't go through didn't; None for orders that did (or still may)."""
    st = o.get("status")
    if st in ("ordered", "pending_approval"):
        return None
    if st == "rejected":
        return "declined"
    if st in LATE:
        return "too_late"
    fired = [f["rule"] for f in (o.get("decision") or {}).get("findings") or [] if f.get("outcome") == "block"]
    reason = ((o.get("approval") or {}).get("result_reason") or "").lower()
    if any(r.startswith(SUPPLY_RULES) for r in fired) or "cut-off" in reason or "hold" in reason:
        return "supply"
    return "buyer"


def _value(o: dict) -> float:
    """What the order was worth: its lines at their quoted prices, unorderable ones included (an order's
    total_ex_vat leaves those out, so a refusal for stock would otherwise be worth nothing)."""
    lines = sum((l.get("line_total_ex_vat") if l.get("line_total_ex_vat") is not None
                 else (l.get("quantity") or 0) * (l.get("unit_price_ex_vat") or 0)) for l in o.get("lines") or [])
    return round(max(o.get("total_ex_vat") or 0, lines), 2)


def report(days: int | None = 7, now: datetime | None = None, with_buyers: bool = False) -> dict:
    now = now or datetime.now(config.TZ)
    since = now - timedelta(days=days) if days else None
    idx = sandbox.index()
    scope = sandbox.list_scope(None)

    def in_window(ts: str | None) -> bool:
        return not since or (ts is not None and datetime.fromisoformat(ts) >= since)

    orders = [o for o in store.list_orders() if sandbox.keep(scope, o.get("mandate_id"), idx)
              and not _qa(o.get("agent_id"), o.get("mandate_id"), o.get("buyer")) and in_window(o.get("created_at"))]
    causes: dict[str, dict] = {c: {"orders": 0, "value_ex_vat": 0.0} for c in ("supply", "too_late", "buyer", "declined")}
    rules_by_cause: dict[str, Counter] = defaultdict(Counter)
    by_buyer: dict[str, dict] = defaultdict(lambda: {"lost_orders": 0, "lost_value_ex_vat": 0.0, "supply_value_ex_vat": 0.0})
    placed_value = 0.0
    for o in orders:
        if o.get("status") == "ordered":
            placed_value += o.get("total_ex_vat") or 0
        c = _cause(o)
        if not c:
            continue
        v = _value(o)
        causes[c]["orders"] += 1
        causes[c]["value_ex_vat"] += v
        for f in (o.get("decision") or {}).get("findings") or []:
            if f.get("outcome") in ("block", "ask") and f.get("rule") != "mandate.ok":
                rules_by_cause[c][f["rule"]] += 1
        b = by_buyer[o.get("buyer") or "?"]
        b["lost_orders"] += 1
        b["lost_value_ex_vat"] += v
        if c == "supply":
            b["supply_value_ex_vat"] += v

    # Quotes: uncovered quantity and passed-over candidates (audit quote.issued, which records both).
    sandbox_buyers = set(idx["buyers"])
    quotes = [e for e in store.audit_events(limit=20000, where=(" AND event = ?", ["quote.issued"]))
              if (e.get("buyer") or "") not in sandbox_buyers and not _qa(e.get("actor"), None, e.get("buyer"))
              and in_window(e.get("ts"))]
    uncovered: dict[tuple[str, str], dict] = {}
    issues = Counter()
    issue_products: dict[str, Counter] = defaultdict(Counter)
    for e in quotes:
        d = e.get("data") or {}
        for g in d.get("uncovered") or []:
            item = g.get("item") or {}
            unit = g.get("unit") or item.get("unit") or "SALES_UNIT"
            label = (item.get("note") or item.get("product_type")
                     or _name((item.get("product_ids") or [None])[0]) or "unnamed item")
            u = uncovered.setdefault((label, unit), {"item": label, "unit": unit, "uncovered": 0.0, "quotes": 0,
                                                       "candidates": set()})
            u["uncovered"] += float(g.get("uncovered") or 0)
            u["quotes"] += 1
            u["candidates"].update((item.get("product_ids") or [])[:6])
        for line in d.get("lines") or []:
            for code in line.get("issues") or []:
                if code in ISSUE_TEXT and code not in ("LATER_DATE",):
                    issues[code] += 1
                    issue_products[code][line.get("product_id")] += 1

    unc = sorted(uncovered.values(), key=lambda x: -x["uncovered"])[:15]
    for u in unc:
        u["uncovered"] = round(u["uncovered"], 2)
        u["candidates"] = sorted(c for c in u["candidates"] if c)[:6]
    lost_total = sum(c["value_ex_vat"] for c in causes.values())
    out = {
        "generated_at": now.isoformat(timespec="seconds"),
        "window_days": days,
        "orders_seen": len(orders),
        "placed_value_ex_vat": round(placed_value, 2),
        "lost_value_ex_vat": round(lost_total, 2),
        "lost_share_pct": round(100 * lost_total / (lost_total + placed_value), 1) if lost_total + placed_value else None,
        "causes": {c: {"orders": v["orders"], "value_ex_vat": round(v["value_ex_vat"], 2),
                       "top_rules": [{"rule": r, "orders": n} for r, n in rules_by_cause[c].most_common(5)]}
                   for c, v in causes.items()},
        "quotes_seen": len(quotes),
        "uncovered": unc,
        "passed_over": [{"code": c, "what": ISSUE_TEXT[c], "lines": n,
                         "top_products": [{"product_id": p, "name": _name(p), "lines": k}
                                          for p, k in issue_products[c].most_common(3)]}
                        for c, n in issues.most_common(10)],
        "definitions": {
            "supply": "refused for a Valio-side reason (availability, product hold, ordering paused) or expired at "
                      "approval because a cut-off passed: the part Valio can act on",
            "too_late": "held for the buyer's human, who approved too late (expired)",
            "buyer": "refused by the buyer's own mandate (limits, allergens, claims, autonomy)",
            "declined": "the buyer's human rejected it",
            "uncovered": "quantity in quotes that no orderable candidate covered (quote.uncovered)",
            "passed_over": "blocking issues on quote lines: candidates the planner could not use",
            "excluded": "judge sandboxes and QA test data"},
    }
    if with_buyers:
        out["by_buyer"] = sorted(({"buyer": b, **{k: round(v, 2) if isinstance(v, float) else v for k, v in x.items()}}
                                  for b, x in by_buyer.items()), key=lambda r: -r["lost_value_ex_vat"])
    return out


def _name(pid: str | None) -> str | None:
    if not pid:
        return None
    try:
        p = catalog.get(pid)
    except Exception:  # noqa: BLE001 - no catalog (CI): ids only
        return None
    return (p or {}).get("name")
