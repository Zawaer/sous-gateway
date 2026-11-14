"""Approve with changes: the human edits a held order's quantities instead of only approving or rejecting it.

POST /orders/{id}/approve-with-changes {by, note, changes: [{line, quantity}]} (approver code, or the order's
sandbox approver token):

1. the edited lines (quantity in each line's own sales unit; 0 drops the line) are quoted again live through the
   normal quote path: new Valio prices and availability, a new signed quote for the same agent and mandate;
2. that quote is placed as a new order and the mandate engine checks it like any order. A refusal (block)
   stops here: nothing changes and the original order still waits for a decision. An ask-tier result is
   approved by the same human in the same step (they are the human the mandate asks for); notify or silent
   orders go through on their own;
3. only then is the original order closed as rejected, with approval.replaced_by naming the new order, and the
   new order carries approval_changes {from_order, changes, by}. Both receipts are signed; the audit has
   order.approved_with_changes.

The agent is told through the usual order.decided notification (the original is rejected with the reason).
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from . import erp, notify, store
from . import service as S

MAX_FACTOR = 10  # an edit may not raise a line above 10x its quoted quantity (a typo guard, not a rule)


class LineChange(BaseModel):
    line: int = Field(ge=0, description="0-based index into order.lines")
    quantity: float = Field(ge=0, description="New quantity in the line's own sales unit; 0 removes the line")


class ChangesIn(BaseModel):
    by: str = Field(min_length=1, max_length=100)
    note: str | None = Field(None, max_length=500)
    changes: list[LineChange] = Field(min_length=1, max_length=50)


def approve_with_changes(order_id: str, req: ChangesIn, authority: str | None) -> dict:
    o = S.expire_if_overdue(store.get_order(order_id))
    if not o:
        raise S.GatewayError("order_not_found", f"No order {order_id}.", 404)
    if o["status"] != "pending_approval":
        raise S.GatewayError("not_pending", f"Order {order_id} is {o['status']}, not waiting for approval.", 409)
    if S._mandate(o["mandate_id"]).approval_method == "passkey":
        raise S.GatewayError("passkey_required", f"Mandate {o['mandate_id']} requires passkey approval, which approve "
                             f"with changes doesn't take: approve or reject {order_id} on the passkey page, or ask the "
                             "agent for a new quote with the quantities you want.", 403)
    lines = o.get("lines") or []
    qty = {i: float(l.get("quantity") or 0) for i, l in enumerate(lines)}
    for c in req.changes:
        if c.line >= len(lines):
            raise S.GatewayError("invalid_request", f"Order {order_id} has no line {c.line}.", 422)
        if c.quantity > max(qty[c.line], 1) * MAX_FACTOR:
            raise S.GatewayError("invalid_request", f"Line {c.line}: {c.quantity:g} is over {MAX_FACTOR}x the quoted "
                                 f"{qty[c.line]:g}; ask the agent for a new quote instead.", 422)
        qty[c.line] = c.quantity
    changed = [{"line": i, "product_id": lines[i]["product_id"], "name": lines[i].get("name"),
                "from": float(lines[i].get("quantity") or 0), "to": qty[i], "unit": lines[i].get("sales_unit")}
               for i in sorted({c.line for c in req.changes}) if qty[i] != float(lines[i].get("quantity") or 0)]
    if not changed:
        raise S.GatewayError("invalid_request", "No quantity changed; use approve instead.", 422)
    items = [S.QuoteItem(product_ids=[l["product_id"]], amount=qty[i], unit=l.get("sales_unit") or "SALES_UNIT",
                         substitute_for=l.get("substitute_for"))
             for i, l in enumerate(lines) if qty[i] > 0]
    if not items:
        raise S.GatewayError("invalid_request", "Every line would be removed; reject the order instead.", 422)
    mandate = S._mandate(o["mandate_id"])
    when = date.fromisoformat(o.get("requested_delivery_date") or o["delivery_date"])
    req_q = S.QuoteRequest(agent_id=o["agent_id"], mandate_id=o["mandate_id"], delivery_date=when, items=items,
                           rationale=o.get("agent_rationale"))  # the agent's own reason stays the agent's
    try:  # the approver's call, so no agent token (S.request_quote would ask for one); same quote path otherwise
        with S.sandbox.metered(mandate):
            q = S._request_quote(req_q, approver=True)["quote"]
    except S.aimo.AimoError as e:
        raise S.valio_error(e)
    outcome, found = S._evaluate(mandate, o["agent_id"], q, S._now())  # check before anything is stored
    if outcome == "block" or q.get("uncovered"):
        why = " ".join(f.message for f in found if f.outcome == "block") or "Not all of it can be delivered as changed."
        store.audit("order.changes_refused", req.by, "human", order_id, o["buyer"],
                    {"order_id": order_id, "quote_id": q["id"], "changes": changed, "reason": why, "authority": authority})
        raise S.GatewayError("changes_refused", f"The changed order is outside the mandate: {why} Nothing changed; "
                             f"{order_id} still waits for a decision.", 409)
    with S._order_lock:
        new, _ = S._place_order(o["agent_id"], o["mandate_id"], q["id"], f"{order_id}-changes-{q['id']}")
    if new["status"] == "refused":
        raise S.GatewayError("changes_refused", "The changed order is outside the mandate: "
                             + " ".join(f["message"] for f in new["decision"]["findings"] if f["outcome"] == "block")
                             + f" {order_id} still waits for a decision.", 409)
    if new["status"] == "pending_approval":  # the human the mandate asks for is the one making the change
        try:
            new, _ = S._decide_locked(new["id"], req.by, True, req.note, authority)
        except S.GatewayError:  # never leave a second held order behind: close the replacement, keep the original
            S._decide_locked(new["id"], req.by, False, f"Approve with changes of {order_id} failed; replacement "
                             "withdrawn.", authority)
            raise
    if new["status"] != "ordered":
        raise S.GatewayError("changes_not_ordered", f"The changed order {new['id']} is {new['status']}: "
                             f"{(new.get('approval') or {}).get('result_reason') or ''} {order_id} still waits.", 409)
    new = {**new, "approval_changes": {"from_order": order_id, "changes": changed, "by": req.by, "note": req.note,
                                       "authority": authority, "quote_id": q["id"]}}
    new["receipt_jws"] = S._receipt(new)
    store.update_order(new, expected_status="ordered")
    old, m = S._decide_locked(order_id, req.by, False,
                              f"Approved with changes as {new['id']}: " + "; ".join(
                                  f"{c['name']} {c['from']:g} → {c['to']:g} {c['unit'] or ''}".strip() for c in changed),
                              authority)
    old = {**old, "approval": {**old["approval"], "replaced_by": new["id"]}}
    old["receipt_jws"] = S._receipt(old)
    store.update_order(old, expected_status="rejected")
    store.audit("order.approved_with_changes", req.by, "human", order_id, o["buyer"],
                {"order_id": order_id, "new_order_id": new["id"], "quote_id": q["id"], "changes": changed,
                 "old_total_ex_vat": o.get("total_ex_vat"), "new_total_ex_vat": new.get("total_ex_vat"),
                 "authority": authority})
    notify.send("order.decided", old, m)
    return {"order": erp.submit(new), "replaced": old, "changes": changed}
