"""Gateway operations, shared by the REST API and the MCP server.

search -> quote (signed, expiring, pre-checked against the mandate) -> order
(tier silent/notify -> ordered, ask -> pending_approval, block -> refused) -> human approve or reject. Every step is
written to the audit log.
"""

from __future__ import annotations

import secrets
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from . import aimo, catalog, config, quote as Q, signing, store, substitutes
from .mandate import Mandate, evaluate


class GatewayError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4).upper()}"


def _now() -> datetime:
    return datetime.now(config.TZ)


def _mandate(mandate_id: str) -> Mandate:
    m = store.get_mandate(mandate_id)
    if not m:
        raise GatewayError("mandate_not_found", f"No mandate {mandate_id}.", 404)
    m.pop("_jws", None)
    return Mandate(**m)


# --- mandates ---------------------------------------------------------------

def issue_mandate(m: Mandate) -> dict:
    data = m.model_dump(mode="json")
    jws = signing.sign(data, "valtakirja-mandate+jws")
    store.put_mandate(data, jws)
    store.audit("mandate.issued", m.issued_by, "human", m.id, m.buyer, {"mandate": data})
    return {"mandate": data, "jws": jws}


def get_mandate(mandate_id: str) -> dict:
    m = store.get_mandate(mandate_id)
    if not m:
        raise GatewayError("mandate_not_found", f"No mandate {mandate_id}.", 404)
    jws = m.pop("_jws")
    return {"mandate": m, "jws": jws}


def revoke_mandate(mandate_id: str, by: str) -> dict:
    m = _mandate(mandate_id)
    m.revoked = True
    data = m.model_dump(mode="json")
    store.put_mandate(data, signing.sign(data, "valtakirja-mandate+jws"))
    store.audit("mandate.revoked", by, "human", m.id, m.buyer, {})
    return data


# --- search -----------------------------------------------------------------

def search(agent_id: str, query: str, *, claims=None, avoid_allergens=None, labels=None,
           temperature=None, category=None, limit: int = 15, buyer: str | None = None) -> dict:
    results = catalog.search(query, claims=claims, avoid_allergens=avoid_allergens, labels=labels,
                             temperature=temperature, category=category, limit=min(limit, 50))
    store.audit("catalog.search", agent_id, "agent", None, buyer,
                {"query": query, "claims": claims, "avoid_allergens": avoid_allergens,
                 "results": [r["id"] for r in results], "valio_api_calls": 0})
    return {"products": [catalog.card(r) for r in results],
            "note": "Static catalog only; no price or stock yet. Ask for a quote on the products you want."}


# --- quotes -----------------------------------------------------------------

class QuoteItem(BaseModel):
    product_ids: list[str] = Field(description="One product, or several candidates for the gateway to choose and split across")
    amount: float = Field(gt=0, description="How much is needed")
    unit: Literal["L", "KG", "SALES_UNIT"] = Field("SALES_UNIT", description="L or KG for volume/weight, SALES_UNIT for packs")
    substitute_for: str | None = Field(None, description="Product id this item replaces, if it is a substitute")
    note: str | None = None


class QuoteRequest(BaseModel):
    agent_id: str
    mandate_id: str | None = None
    delivery_date: date
    items: list[QuoteItem] = Field(min_length=1, max_length=50)


def order_url(order_id: str) -> str:
    return f"{config.PUBLIC_BASE_URL}/ui/#/orders/{order_id}"


def request_quote(req: QuoteRequest) -> dict:
    if sum(len(i.product_ids) for i in req.items) > config.MAX_CANDIDATES_PER_QUOTE:
        raise GatewayError("too_many_candidates",
                           f"At most {config.MAX_CANDIDATES_PER_QUOTE} candidate products per quote; narrow the search first.")
    try:
        return _request_quote(req)
    except aimo.AimoError as e:
        raise GatewayError("valio_unavailable", str(e), 503)


def _request_quote(req: QuoteRequest) -> dict:
    mandate = _mandate(req.mandate_id) if req.mandate_id else None
    before = dict(aimo.stats)
    lines: list[dict] = []
    considered: list[dict] = []
    gaps: list[dict] = []
    now = _now()
    for item in req.items:
        unit = None if item.unit == "SALES_UNIT" else item.unit
        offers, now = Q.offers(item.product_ids, req.delivery_date, item.amount, unit)
        if not offers:
            gaps.append({"item": item.model_dump(), "uncovered": item.amount, "reason": "No such product ids."})
            continue
        chosen, uncovered = Q.plan(item.amount, unit, offers) if len(offers) > 1 else (
            [offers[0]] if offers[0]["orderable"] else [], 0.0 if offers[0]["orderable"] else item.amount)
        if len(offers) == 1 and not offers[0]["orderable"]:
            # A single named product that can't be ordered: keep it so the reasons are visible.
            chosen = [offers[0]]
            uncovered = 0.0
        for l in chosen:
            if item.substitute_for:
                l["substitute_for"] = item.substitute_for
            lines.append(l)
        picked = {l["product_id"] for l in chosen}
        considered += [{"product_id": o["product_id"], "name": o["name"],
                        "comparison_price": o["comparison_price"], "orderable": o["orderable"],
                        "why_not": [i["message"] for i in o["issues"] if i["severity"] == "blocking"]
                        or ["More expensive per unit than the chosen option."]}
                       for o in sorted(offers, key=Q.rank_key) if o["product_id"] not in picked][:6]
        if uncovered > 0:
            gaps.append({"item": item.model_dump(), "uncovered": uncovered, "unit": item.unit})

    total_ex = round(sum(l["line_total_ex_vat"] or 0 for l in lines), 2)
    total_inc = round(sum(l["line_total_inc_vat"] or 0 for l in lines), 2)
    qid = _id("Q")
    body = {
        "id": qid,
        "agent_id": req.agent_id,
        "mandate_id": req.mandate_id,
        "issued_at": now.isoformat(timespec="seconds"),
        "expires_at": Q.quote_expiry(now, [l for l in lines if l["orderable"]]).isoformat(timespec="seconds"),
        "requested_delivery_date": req.delivery_date.isoformat(),
        "currency": "EUR",
        "lines": lines,
        "total_ex_vat": total_ex,
        "total_inc_vat": total_inc,
        "uncovered": gaps,
        "alternatives_considered": considered,
        "valio_api_calls": {k: aimo.stats[k] - before[k] for k in aimo.stats},
    }
    if mandate:
        outcome, findings = _evaluate(mandate, req.agent_id, body, now)
        body["mandate_preview"] = {"outcome": outcome, "findings": [f.model_dump() for f in findings]}
    jws = signing.sign(body, "valtakirja-quote+jws")
    store.put_quote(body, jws)
    store.audit("quote.issued", req.agent_id, "agent", qid, mandate.buyer if mandate else None,
                {"quote_id": qid, "total_ex_vat": total_ex, "lines": [
                    {"product_id": l["product_id"], "quantity": l["quantity"], "issues": [i["code"] for i in l["issues"]]}
                    for l in lines], "uncovered": gaps, "preview": body.get("mandate_preview", {}).get("outcome"),
                 "valio_api_calls": body["valio_api_calls"]})
    return {"quote": body, "jws": jws}


def _evaluate(mandate: Mandate, agent_id: str, q: dict, now: datetime):
    uncovered = sum(g["uncovered"] for g in q["uncovered"])
    unit = q["uncovered"][0].get("unit") if q["uncovered"] else None
    subs = {l["product_id"]: l["substitute_for"] for l in q["lines"] if l.get("substitute_for")}
    return evaluate(mandate, agent_id, q["lines"], requested_date=date.fromisoformat(q["requested_delivery_date"]),
                    substitute_for=subs, uncovered=uncovered, need_unit=unit,
                    spent_today_eur=store.spent_today(mandate.id, now.date().isoformat()), now=now)


def _refresh_cutoffs(q: dict, now: datetime) -> None:
    """Re-check every line's cut-off against the clock (no API call: the quote carries it)."""
    for l in q["lines"]:
        if l.get("order_cutoff") and datetime.fromisoformat(l["order_cutoff"]) <= now and \
                not any(i["code"] == "CUTOFF_PASSED" for i in l["issues"]):
            l["issues"].append({"code": "CUTOFF_PASSED", "severity": "blocking",
                                "message": f"The order cut-off {l['order_cutoff']} passed after the quote was issued."})
            l["orderable"] = False


# --- orders -----------------------------------------------------------------

def place_order(agent_id: str, mandate_id: str, quote_id: str, idempotency_key: str | None = None) -> dict:
    if idempotency_key and (prev := store.order_by_idempotency_key(idempotency_key)):
        return prev
    mandate = _mandate(mandate_id)
    q = store.get_quote(quote_id)
    if not q:
        raise GatewayError("quote_not_found", f"No quote {quote_id}.", 404)
    if not signing.verify(q.pop("_jws")):
        raise GatewayError("quote_signature", "Quote signature does not verify.", 409)
    if q.pop("_order_id"):
        raise GatewayError("quote_used", f"Quote {quote_id} was already used for an order.", 409)
    now = _now()
    if now > datetime.fromisoformat(q["expires_at"]):
        store.audit("order.rejected", agent_id, "agent", quote_id, mandate.buyer, {"reason": "quote expired"})
        raise GatewayError("quote_expired", f"Quote {quote_id} expired at {q['expires_at']}; ask for a new one.", 409)

    _refresh_cutoffs(q, now)
    outcome, findings = _evaluate(mandate, agent_id, q, now)
    status = {"silent": "ordered", "notify": "ordered", "ask": "pending_approval", "block": "refused"}[outcome]
    oid = _id("PO")
    if not store.claim_quote(quote_id, oid):
        raise GatewayError("quote_used", f"Quote {quote_id} was already used for an order.", 409)
    order = {
        "id": oid, "quote_id": quote_id, "mandate_id": mandate.id, "agent_id": agent_id, "buyer": mandate.buyer,
        "status": status, "total_ex_vat": q["total_ex_vat"], "total_inc_vat": q["total_inc_vat"],
        "delivery_date": max((l["delivery_date"] for l in q["lines"] if l.get("delivery_date")),
                             default=q["requested_delivery_date"]),
        "lines": [{k: l[k] for k in ("product_id", "name", "quantity", "sales_unit", "unit_price_ex_vat",
                                     "line_total_ex_vat", "delivery_date", "logistic_model", "order_cutoff")}
                  for l in q["lines"]],
        "decision": {"outcome": outcome, "findings": [f.model_dump() for f in findings], "by": "mandate-engine",
                     "at": now.isoformat(timespec="seconds")},
        "approval": None,
        "order_url": order_url(oid),
        "approval_url": order_url(oid) if status == "pending_approval" else None,
        "idempotency_key": idempotency_key,
        "created_at": now.isoformat(timespec="seconds"), "updated_at": now.isoformat(timespec="seconds"),
    }
    order["receipt_jws"] = signing.sign({k: order[k] for k in ("id", "quote_id", "mandate_id", "status", "total_ex_vat",
                                                                "lines", "decision")}, "valtakirja-order+jws")
    store.put_order(order)
    store.audit(f"order.{status}", "mandate-engine", "system", oid, mandate.buyer,
                {"order_id": oid, "quote_id": quote_id, "agent_id": agent_id, "outcome": outcome,
                 "total_ex_vat": q["total_ex_vat"],
                 "fired": [f.model_dump() for f in findings if f.outcome != "silent"] or [findings[-1].model_dump()]})
    return order


def decide(order_id: str, approver: str, approve: bool, note: str | None = None) -> dict:
    o = store.get_order(order_id)
    if not o:
        raise GatewayError("order_not_found", f"No order {order_id}.", 404)
    if o["status"] != "pending_approval":
        raise GatewayError("not_pending", f"Order {order_id} is {o['status']}, not waiting for approval.", 409)
    now = _now()
    mandate = _mandate(o["mandate_id"])
    status = "ordered" if approve else "rejected"
    reason = note
    if approve:
        if mandate.revoked:
            status, reason = "refused", "Mandate was revoked before approval."
        for l in o["lines"]:
            if l.get("order_cutoff") and datetime.fromisoformat(l["order_cutoff"]) <= now:
                status = "expired"
                reason = f"Approved too late: the order cut-off for {l['name']} ({l['order_cutoff']}) has passed."
    o["status"] = status
    o["approval"] = {"by": approver, "approved": approve, "note": note, "at": now.isoformat(timespec="seconds"),
                     "result_reason": reason}
    o["updated_at"] = now.isoformat(timespec="seconds")
    store.put_order(o)
    store.audit("order.approved" if approve else "order.rejected_by_human", approver, "human", order_id,
                o["buyer"], {"order_id": order_id, "note": note, "resulting_status": status, "reason": reason})
    return o


def get_order(order_id: str) -> dict:
    o = store.get_order(order_id)
    if not o:
        raise GatewayError("order_not_found", f"No order {order_id}.", 404)
    return o


# --- substitutes ------------------------------------------------------------

def suggest_substitutes(agent_id: str, product_id: str, delivery_date: date, amount: float,
                        unit: str = "SALES_UNIT", mandate_id: str | None = None,
                        avoid_allergens: list[str] | None = None, limit: int = 3) -> dict:
    mandate = _mandate(mandate_id) if mandate_id else None
    before = dict(aimo.stats)
    try:
        r = substitutes.suggest(product_id, delivery_date, amount, None if unit == "SALES_UNIT" else unit,
                                mandate, avoid_allergens, limit=min(limit, 6))
    except KeyError:
        raise GatewayError("product_not_found", f"No product {product_id}.", 404)
    except aimo.AimoError as e:
        raise GatewayError("valio_unavailable", str(e), 503)
    r["valio_api_calls"] = {k: aimo.stats[k] - before[k] for k in aimo.stats}
    store.audit("substitutes.suggested", agent_id, "agent", product_id, mandate.buyer if mandate else None,
                {"product_id": product_id, "constraints": r["constraints"],
                 "options": [{"tier": o["tier"], "product_id": o["product_id"], "compromises": o["compromises"]}
                             for o in r["options"]],
                 "no_safe_substitute": r["no_safe_substitute"], "valio_api_calls": r["valio_api_calls"]})
    return r
