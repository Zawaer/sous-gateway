"""Gateway operations, shared by the REST API and the MCP server.

search -> quote (signed, expiring, pre-checked against the mandate) -> order
(tier silent/notify -> ordered, ask -> pending_approval, block -> refused) -> human approve or reject. Every step is
written to the audit log.
"""

from __future__ import annotations

import secrets
import threading
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from . import agent_auth, aimo, catalog, config, notify, quote as Q, signing, store, substitutes
from .mandate import Mandate, evaluate


class GatewayError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, retry_after: int | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.retry_after = code, message, status, retry_after


def _id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4).upper()}"


def _now() -> datetime:
    return datetime.now(config.TZ)


# The gateway runs as one process: this lock makes "check idempotency key, claim quote, sum today's spend,
# evaluate, store the order" and "check pending, decide, store" atomic, so concurrent requests can't double
# order, overrun the daily limit or both approve and reject the same order.
_order_lock = threading.RLock()


def _mandate(mandate_id: str) -> Mandate:
    m = store.get_mandate(mandate_id)
    if not m:
        raise GatewayError("mandate_not_found", f"No mandate {mandate_id}.", 404)
    payload = signing.verify(m.pop("_jws", None) or "", "sous-mandate+jws")
    if payload is None or payload.get("id") != mandate_id:
        raise GatewayError("mandate_signature", f"Mandate {mandate_id}'s signature does not verify.", 409)
    return Mandate(**payload)  # the signed mandate, not the stored copy


# --- mandates ---------------------------------------------------------------

def issue_mandate(m: Mandate, mint_agent_credential: bool | None = None) -> dict:
    """Sign and store the mandate. When the agent must authenticate (or when asked), also mint the agent's
    credential: its token is in the response this once and never again."""
    data = m.model_dump(mode="json")
    jws = signing.sign(data, "sous-mandate+jws")
    store.put_mandate(data, jws)
    store.audit("mandate.issued", m.issued_by, "human", m.id, m.buyer, {"mandate": data})
    out = {"mandate": data, "jws": jws}
    if mint_agent_credential if mint_agent_credential is not None else agent_auth.required(m):
        out["agent_credential"] = agent_auth.mint(m, m.issued_by)
    return out


def authorize_agent(mandate_id: str, agent_id: str, agent_token: str | None, operation: str) -> Mandate:
    """Check the agent's bearer credential when the mandate requires one (agent_auth.py); returns the mandate."""
    m = _mandate(mandate_id)
    try:
        agent_auth.check(m, agent_id, agent_token, operation)
    except agent_auth.AuthError as e:
        raise GatewayError(e.code, e.message, e.status) from None
    return m


def agent_credential(mandate_id: str, by: str, action: Literal["rotate", "revoke"] = "rotate") -> dict:
    """Human-only: mint a new agent credential for the mandate (the old one stops working) or revoke it."""
    with _order_lock:
        m = _mandate(mandate_id)
        if action == "revoke":
            return {"status": agent_auth.revoke(m, by)}
        cred = agent_auth.mint(m, by, rotated=agent_auth.status(m)["credential"] is not None)
        return {"agent_credential": cred, "status": agent_auth.status(m)}


def agent_credential_status(mandate_id: str) -> dict:
    return agent_auth.status(_mandate(mandate_id))


def get_mandate(mandate_id: str) -> dict:
    m = store.get_mandate(mandate_id)
    if not m:
        raise GatewayError("mandate_not_found", f"No mandate {mandate_id}.", 404)
    jws = m.pop("_jws")
    return {"mandate": m, "jws": jws}


def revoke_mandate(mandate_id: str, by: str) -> dict:
    with _order_lock:  # an order being placed or approved right now sees the mandate before or after, not both
        m = _mandate(mandate_id)
        m.revoked = True
        data = m.model_dump(mode="json")
        store.put_mandate(data, signing.sign(data, "sous-mandate+jws"))
    store.audit("mandate.revoked", by, "human", m.id, m.buyer, {})
    return data


# --- search -----------------------------------------------------------------

def search(agent_id: str, query: str, *, claims=None, avoid_allergens=None, labels=None,
           temperature=None, category=None, limit: int = 15, buyer: str | None = None,
           food_only: bool | None = None) -> dict:
    results = catalog.search(query, claims=claims, avoid_allergens=avoid_allergens, labels=labels,
                             temperature=temperature, category=category, limit=min(limit, 50), food_only=food_only)
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


def request_quote(req: QuoteRequest, agent_token: str | None = None) -> dict:
    if req.mandate_id:
        authorize_agent(req.mandate_id, req.agent_id, agent_token, "request_quote")
    if sum(len(i.product_ids) for i in req.items) > config.MAX_CANDIDATES_PER_QUOTE:
        raise GatewayError("too_many_candidates",
                           f"At most {config.MAX_CANDIDATES_PER_QUOTE} candidate products per quote; narrow the search first.")
    try:
        return _request_quote(req)
    except aimo.AimoError as e:
        raise GatewayError("valio_unavailable", str(e), 503, e.retry_after)


def _request_quote(req: QuoteRequest) -> dict:
    mandate = _mandate(req.mandate_id) if req.mandate_id else None
    before = dict(aimo.stats)
    lines: list[dict] = []
    considered: list[dict] = []
    gaps: list[dict] = []
    now = _now()
    for item_index, item in enumerate(req.items):
        unit = None if item.unit == "SALES_UNIT" else item.unit
        offers, now = Q.offers(item.product_ids, req.delivery_date, item.amount, unit)
        if not offers:
            gaps.append({"item": item.model_dump(), "item_index": item_index, "uncovered": item.amount, "unit": item.unit,
                         "reason": "No such product ids."})
            continue
        chosen, uncovered = Q.plan(item.amount, unit, offers) if len(offers) > 1 else (
            [offers[0]] if offers[0]["orderable"] else [], 0.0 if offers[0]["orderable"] else item.amount)
        if len(offers) == 1 and not offers[0]["orderable"]:
            # A single named product that can't be ordered: keep it so the reasons are visible.
            chosen = [offers[0]]
            uncovered = 0.0
        for l in chosen:
            l["item_index"] = item_index
            if item.substitute_for:
                l["substitute_for"] = item.substitute_for
            lines.append(l)
        picked = {l["product_id"] for l in chosen}
        considered += [{"product_id": o["product_id"], "name": o["name"], "item_index": item_index,
                        "comparison_price": o["comparison_price"], "orderable": o["orderable"],
                        "why_not": [i["message"] for i in o["issues"] if i["severity"] == "blocking"]
                        or ["More expensive per unit than the chosen option."]}
                       for o in sorted(offers, key=Q.rank_key) if o["product_id"] not in picked][:6]
        if uncovered > 0:
            gaps.append({"item": item.model_dump(), "item_index": item_index, "uncovered": uncovered, "unit": item.unit})

    total_ex = round(sum(l["line_total_ex_vat"] or 0 for l in lines), 2)
    total_inc = round(sum(l["line_total_inc_vat"] or 0 for l in lines), 2)
    qid = _id("Q")
    body = {
        "id": qid,
        "agent_id": req.agent_id,
        "mandate_id": req.mandate_id,
        "issued_at": now.isoformat(timespec="milliseconds"),
        "expires_at": Q.quote_expiry(now, [l for l in lines if l["orderable"]]).isoformat(timespec="milliseconds"),
        "requested_delivery_date": req.delivery_date.isoformat(),
        "currency": "EUR",
        "items": [item.model_dump() for item in req.items],
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
    jws = signing.sign(body, "sous-quote+jws")
    store.put_quote(body, jws)
    store.audit("quote.issued", req.agent_id, "agent", qid, mandate.buyer if mandate else None,
                {"quote_id": qid, "total_ex_vat": total_ex, "lines": [
                    {"product_id": l["product_id"], "quantity": l["quantity"], "issues": [i["code"] for i in l["issues"]]}
                    for l in lines], "uncovered": gaps, "preview": body.get("mandate_preview", {}).get("outcome"),
                 "valio_api_calls": body["valio_api_calls"]})
    return {"quote": body, "jws": jws}


def _evaluate(mandate: Mandate, agent_id: str, q: dict, now: datetime, extra_findings=None):
    uncovered: dict[str, float] = {}  # per unit: litres and kilos don't add up
    for g in q["uncovered"]:
        u = g.get("unit") or (g.get("item") or {}).get("unit") or "SALES_UNIT"
        uncovered[u] = uncovered.get(u, 0.0) + g["uncovered"]
    subs = {l["product_id"]: l["substitute_for"] for l in q["lines"] if l.get("substitute_for")}
    return evaluate(mandate, agent_id, q["lines"], requested_date=date.fromisoformat(q["requested_delivery_date"]),
                    substitute_for=subs, uncovered=uncovered,
                    spent_today_eur=store.spent_today(mandate.id, now.date().isoformat()), now=now,
                    extra_findings=extra_findings)


def _refresh_cutoffs(q: dict, now: datetime) -> None:
    """Re-check every line's cut-off against the clock (no API call: the quote carries it)."""
    for l in q["lines"]:
        if l.get("order_cutoff") and datetime.fromisoformat(l["order_cutoff"]) <= now and \
                not any(i["code"] == "CUTOFF_PASSED" for i in l["issues"]):
            l["issues"].append({"code": "CUTOFF_PASSED", "severity": "blocking",
                                "message": f"The order cut-off {l['order_cutoff']} passed after the quote was issued."})
            l["orderable"] = False


# --- orders -----------------------------------------------------------------

def place_order(agent_id: str, mandate_id: str, quote_id: str, idempotency_key: str | None = None,
                extra_findings=None, agent_token: str | None = None) -> dict:
    """`extra_findings` (list of mandate.Finding) join the mandate's own; standing.py passes drift findings.
    `agent_token` is the agent's bearer credential, needed when the mandate requires agent authentication."""
    authorize_agent(mandate_id, agent_id, agent_token, "place_order")
    with _order_lock:
        order, mandate = _place_order(agent_id, mandate_id, quote_id, idempotency_key, extra_findings)
    if mandate is not None and order["decision"]["outcome"] in ("notify", "ask"):
        notify.send(f"order.{order['decision']['outcome']}", order, mandate)  # FYI / approval request; never blocks
    return order


def _place_order(agent_id: str, mandate_id: str, quote_id: str,
                 idempotency_key: str | None, extra_findings=None) -> tuple[dict, Mandate | None]:
    if idempotency_key and (prev := store.order_by_idempotency_key(idempotency_key)):
        if prev["agent_id"] != agent_id or prev["mandate_id"] != mandate_id or prev["quote_id"] != quote_id:
            raise GatewayError("idempotency_key_reused",
                               "This idempotency key was already used for a different order; use a fresh key.", 409)
        return prev, None
    mandate = _mandate(mandate_id)
    row = store.get_quote(quote_id)
    if not row:
        raise GatewayError("quote_not_found", f"No quote {quote_id}.", 404)
    # Everything below works from the signed payload, never from the stored copy.
    q = signing.verify(row["_jws"], "sous-quote+jws")
    if q is None or q.get("id") != quote_id:
        raise GatewayError("quote_signature", "Quote signature does not verify.", 409)
    if q.get("agent_id") != agent_id or (q.get("mandate_id") and q["mandate_id"] != mandate_id):
        raise GatewayError("quote_mismatch",
                           f"Quote {quote_id} was issued to agent '{q.get('agent_id')}'"
                           + (f" for mandate {q['mandate_id']}" if q.get("mandate_id") else "")
                           + "; ask for a quote of your own.", 409)
    if row["_order_id"]:
        raise GatewayError("quote_used", f"Quote {quote_id} was already used for an order.", 409)
    now = _now()
    if now > datetime.fromisoformat(q["expires_at"]):
        store.audit("order.rejected", agent_id, "agent", quote_id, mandate.buyer, {"reason": "quote expired"})
        raise GatewayError("quote_expired", f"Quote {quote_id} expired at {q['expires_at']}; ask for a new one.", 409)

    _refresh_cutoffs(q, now)
    outcome, findings = _evaluate(mandate, agent_id, q, now, extra_findings)
    status = {"silent": "ordered", "notify": "ordered", "ask": "pending_approval", "block": "refused"}[outcome]
    oid = _id("PO")
    order = {
        "id": oid, "quote_id": quote_id, "mandate_id": mandate.id, "agent_id": agent_id, "buyer": mandate.buyer,
        "status": status, "total_ex_vat": q["total_ex_vat"], "total_inc_vat": q["total_inc_vat"],
        "delivery_date": max((l["delivery_date"] for l in q["lines"] if l.get("delivery_date")),
                             default=q["requested_delivery_date"]),
        "requested_delivery_date": q["requested_delivery_date"],
        "lines": [{k: l.get(k) for k in ("product_id", "name", "quantity", "sales_unit", "unit_price_ex_vat",
                                         "line_total_ex_vat", "delivery_date", "logistic_model", "order_cutoff")}
                  for l in q["lines"]],
        "decision": {"outcome": outcome, "findings": [f.model_dump() for f in findings], "by": "mandate-engine",
                     "at": now.isoformat(timespec="milliseconds")},
        "approval": None,
        "order_url": order_url(oid),
        "approval_url": order_url(oid) if status == "pending_approval" else None,
        "idempotency_key": idempotency_key,
        "created_at": now.isoformat(timespec="milliseconds"), "updated_at": now.isoformat(timespec="milliseconds"),
    }
    order["receipt_jws"] = _receipt(order)
    # Claim the quote and store the order together: if storing fails, the quote is released again.
    if not store.claim_quote(quote_id, oid):
        raise GatewayError("quote_used", f"Quote {quote_id} was already used for an order.", 409)
    try:
        store.insert_order(order)
    except store.Conflict:
        store.release_quote(quote_id, oid)
        raise GatewayError("idempotency_key_reused",
                           "This idempotency key is already taken by another order; use a fresh key.", 409)
    except Exception:
        store.release_quote(quote_id, oid)
        raise
    store.audit(f"order.{status}", "mandate-engine", "system", oid, mandate.buyer,
                {"order_id": oid, "quote_id": quote_id, "agent_id": agent_id, "outcome": outcome,
                 "total_ex_vat": q["total_ex_vat"],
                 "fired": [f.model_dump() for f in findings if f.outcome != "silent"] or [findings[-1].model_dump()]})
    return order, mandate


def _receipt(order: dict) -> str:
    """Signed receipt of the order's current state (re-issued after a human decision)."""
    return signing.sign({k: order.get(k) for k in ("id", "quote_id", "mandate_id", "status", "total_ex_vat",
                                                   "lines", "decision", "approval", "updated_at")},
                        "sous-order+jws")


def decide(order_id: str, approver: str, approve: bool, note: str | None = None) -> dict:
    with _order_lock:
        o, mandate = _decide(order_id, approver, approve, note)
    notify.send("order.decided", o, mandate)  # FYI to the agent side
    return o


def _approval_blocker(o: dict, mandate: Mandate, now: datetime) -> tuple[str, str] | None:
    """Why an approval can no longer become an order: (status, reason), or None if it still can."""
    if mandate.revoked:
        return "refused", "Mandate was revoked before approval."
    if mandate.valid_until and now > mandate.valid_until:
        return "refused", f"Mandate {mandate.id} expired on {mandate.valid_until:%d.%m.%Y}, before approval."
    for l in o["lines"]:
        if l.get("order_cutoff") and datetime.fromisoformat(l["order_cutoff"]) <= now:
            return "expired", f"Approved too late: the order cut-off for {l['name']} ({l['order_cutoff']}) has passed."
    today = now.astimezone(config.TZ).date()
    days = [l["delivery_date"] for l in o["lines"] if l.get("delivery_date")]
    days += [d for d in (o.get("requested_delivery_date"), o.get("delivery_date")) if d]
    for d in sorted(set(days)):
        if date.fromisoformat(d) < today:
            return "expired", f"Approved too late: the delivery date {d} has already passed."
    return None


def _decide(order_id: str, approver: str, approve: bool, note: str | None) -> tuple[dict, Mandate]:
    o = store.get_order(order_id)
    if not o:
        raise GatewayError("order_not_found", f"No order {order_id}.", 404)
    if o["status"] != "pending_approval":
        raise GatewayError("not_pending", f"Order {order_id} is {o['status']}, not waiting for approval.", 409)
    now = _now()
    mandate = _mandate(o["mandate_id"])
    status = "ordered" if approve else "rejected"
    reason = note
    if approve and (blocked := _approval_blocker(o, mandate, now)):
        status, reason = blocked
    o["status"] = status
    o["approval"] = {"by": approver, "approved": approve, "note": note, "at": now.isoformat(timespec="milliseconds"),
                     "result_reason": reason}
    o["updated_at"] = now.isoformat(timespec="milliseconds")
    o["approval_url"] = None
    o["receipt_jws"] = _receipt(o)
    # Conditional on the order still being pending, so a concurrent decision is never overwritten.
    if not store.update_order(o, expected_status="pending_approval"):
        current = store.get_order(order_id) or {}
        raise GatewayError("not_pending",
                           f"Order {order_id} is {current.get('status')}, not waiting for approval.", 409)
    store.audit("order.approved" if approve else "order.rejected_by_human", approver, "human", order_id,
                o["buyer"], {"order_id": order_id, "note": note, "resulting_status": status, "reason": reason})
    return o, mandate


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
        raise GatewayError("valio_unavailable", str(e), 503, e.retry_after)
    r["valio_api_calls"] = {k: aimo.stats[k] - before[k] for k in aimo.stats}
    store.audit("substitutes.suggested", agent_id, "agent", product_id, mandate.buyer if mandate else None,
                {"product_id": product_id, "constraints": r["constraints"],
                 "options": [{"tier": o["tier"], "product_id": o["product_id"], "compromises": o["compromises"]}
                             for o in r["options"]],
                 "no_safe_substitute": r["no_safe_substitute"], "valio_api_calls": r["valio_api_calls"]})
    return r


# --- history (MariaDB system versioning) ------------------------------------

def order_history(order_id: str) -> dict:
    """Every version of an order, and the mandate exactly as it was when each decision was made."""
    o = get_order(order_id)
    out = {
        "order_id": order_id,
        "backend": store.backend(),
        "versions": [{"valid_from": v["valid_from"], "valid_to": v["valid_to"], "status": v["status"]}
                     for v in store.order_versions(order_id)],
        "mandate_now": _mandate(o["mandate_id"]).model_dump(mode="json"),
    }
    if store.backend() == "mariadb":
        out["mandate_at_decision"] = store.mandate_as_of(o["mandate_id"], o["decision"]["at"])
        if o.get("approval"):
            out["mandate_at_approval"] = store.mandate_as_of(o["mandate_id"], o["approval"]["at"])
        out["query"] = ("SELECT data FROM mandate FOR SYSTEM_TIME AS OF TIMESTAMP '<decision time>' "
                        "WHERE id = '<mandate id>'")
        changed = [k for k in out["mandate_now"]
                   if (out["mandate_at_decision"] or {}).get(k) != out["mandate_now"][k]]
        out["mandate_changed_since_decision"] = changed
    else:
        out["note"] = "Point-in-time history needs MariaDB (SOUS_DB_URL); SQLite keeps only the current row."
    return out


def mandate_history(mandate_id: str) -> dict:
    _mandate(mandate_id)
    return {"mandate_id": mandate_id, "backend": store.backend(), "versions": store.mandate_versions(mandate_id)}
