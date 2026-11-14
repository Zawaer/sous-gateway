"""Post-order exceptions as signed amendments: what Valio Aimo changes after an order went through.

Valio reports an exception on an `ordered` order (POST /orders/{id}/amendments, global approver code or, for a
judge sandbox's order, its sandbox approver token):

  short_pick            {line, delivered_quantity, reason}   fewer units were picked than ordered
  delay                 {new_delivery_date, line?, reason}   the delivery comes later (line None = whole order)
  substitution_at_pick  {line, product_id, quantity, unit_price_ex_vat?, reason}   another product was picked
  cancel_line           {line, reason}                        the line can't be delivered at all

`line` is the 0-based index into order.lines. Each amendment is signed (JWS typ sous-amendment+jws, re-signed
when it is resolved), appended to order.amendments[], audited (order.amended, order.amendment_accepted /
_rejected) and re-evaluated against the buyer's mandate as it stands now:

  short_pick, cancel_line      always notify: applied at once, the total drops, a credit note line is added
  delay                        the mandate's later_delivery tier (notify / ask / block)
  substitution_at_pick         the mandate engine on the substitute line (substitutions tier, allergens,
                               claims, categories, holds, ...) plus amendment.price_increase

notify -> `applied` at once (the buyer is told). ask / block -> `pending_buyer`: the buyer accepts or rejects
(POST /orders/{id}/amendments/{aid}/accept|reject). A human (approver code / sandbox approver token) may accept
an ask-tier amendment; nobody may accept a block-tier one until the mandate allows it. The buyer's agent (its
vka_ credential) may reject, and may accept only what the mandate, re-checked at that moment, would let through
without a human (notify or silent). Rejecting a substitution or a delay cancels the line(s) concerned, with a
credit. The order keeps status `ordered`; its receipt is re-signed with the new total, `original_total_ex_vat`
and `credit_notes[]`. Every change to the lines goes to the ERP outbox as one ORDCHG message (erp.enqueue_change).
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from . import catalog, erp, notify, signing, store
from . import service as S
from .mandate import Finding, Mandate, evaluate, readable_name

KINDS = ("short_pick", "delay", "substitution_at_pick", "cancel_line")
TYP = "sous-amendment+jws"
_RANK = {"silent": 0, "notify": 1, "ask": 2, "block": 3}


class AmendmentIn(BaseModel):
    kind: Literal["short_pick", "delay", "substitution_at_pick", "cancel_line"]
    line: int | None = Field(None, ge=0, description="0-based index into order.lines (delay: None = whole order)")
    delivered_quantity: float | None = Field(None, ge=0, description="short_pick: quantity actually picked")
    new_delivery_date: date | None = Field(None, description="delay: the new delivery date")
    product_id: str | None = Field(None, max_length=64, description="substitution_at_pick: the product picked")
    quantity: float | None = Field(None, gt=0, description="substitution_at_pick: quantity of the substitute")
    unit_price_ex_vat: float | None = Field(
        None, ge=0, description="substitution_at_pick: price charged per unit; default the original line's")
    reason: str = Field(min_length=1, max_length=300)
    by: str = Field("Valio Aimo", max_length=80, description="Who at Valio reports it")


class Resolution(BaseModel):
    by: str = Field(max_length=80)
    note: str | None = Field(None, max_length=300)


def _err(code: str, msg: str, status: int = 409) -> S.GatewayError:
    return S.GatewayError(code, msg, status)


def _sum(lines: list[dict]) -> float:
    return round(sum(l.get("line_total_ex_vat") or 0 for l in lines), 2)


def _lines_of(o: dict, a: dict) -> list[int]:
    return list(range(len(o["lines"]))) if a.get("line") is None else [a["line"]]


# --- evaluation against the mandate as it stands ----------------------------------------------------------------

def _sub_line(o: dict, a: dict) -> dict:
    p = catalog.get(a["product_id"]) or {}
    old = o["lines"][a["line"]]
    unit = a.get("unit_price_ex_vat")
    unit = old.get("unit_price_ex_vat") if unit is None else unit
    return {"product_id": a["product_id"], "name": p.get("name") or a["product_id"], "quantity": a["quantity"],
            "sales_unit": p.get("sales_unit") or old.get("sales_unit"), "unit_price_ex_vat": unit,
            "line_total_ex_vat": round((unit or 0) * a["quantity"], 2), "delivery_date": old.get("delivery_date"),
            "logistic_model": None, "order_cutoff": None, "claims": p.get("claims") or [],
            "delivery_reliability": p.get("delivery_reliability"), "issues": [], "orderable": True}


def assess(o: dict, a: dict, mandate: Mandate) -> tuple[str, list[dict]]:
    """(tier, findings) for one amendment under the mandate now. Never below notify: the buyer hears of each one."""
    f: list[Finding] = []
    kind = a["kind"]
    if kind in ("short_pick", "cancel_line"):
        l = o["lines"][a["line"]]
        what = (f"{readable_name(l['name'])}: {a['delivered_quantity']:g} of {l['quantity']:g} picked"
                if kind == "short_pick" else f"{readable_name(l['name'])}: cancelled by Valio Aimo")
        f.append(Finding(rule="amendment.short_pick" if kind == "short_pick" else "amendment.line_cancelled",
                         outcome="notify", message=f"{what} ({a['reason']}); the total drops and a credit note "
                         "line is added.", product_id=l["product_id"]))
    elif kind == "delay":
        tier = mandate.later_delivery
        msg = (f"Delivery moves to {a['new_delivery_date']} ({a['reason']}); the mandate's later_delivery is "
               f"'{tier}'.")
        f.append(Finding(rule="amendment.delay_beyond_mandate", outcome=tier, message=msg) if tier != "notify"
                 else Finding(rule="amendment.delay", outcome="notify", message=msg))
    else:  # substitution_at_pick
        old = o["lines"][a["line"]]
        new = _sub_line(o, a)
        outcome, fs = evaluate(mandate, o["agent_id"], [new],
                               requested_date=date.fromisoformat(old.get("delivery_date") or o["delivery_date"]),
                               substitute_for={new["product_id"]: old["product_id"]}, now=S._now())
        fired = [x for x in fs if x.rule != "mandate.ok" and x.outcome != "silent"]
        if outcome in ("ask", "block"):
            why = "; ".join(x.message for x in fired if x.outcome == outcome)
            f.append(Finding(rule="amendment.substitution_not_allowed", outcome=outcome,
                             message=f"Valio picked {readable_name(new['name'])} instead of "
                                     f"{readable_name(old['name'])} ({a['reason']}): {why}",
                             product_id=new["product_id"]))
        f += fired
        if new["line_total_ex_vat"] > (old.get("line_total_ex_vat") or 0):
            f.append(Finding(rule="amendment.price_increase", outcome="ask",
                             message=f"The substitute costs €{new['line_total_ex_vat']:,.2f}, more than the "
                                     f"€{old.get('line_total_ex_vat') or 0:,.2f} ordered: a human decides.",
                             product_id=new["product_id"]))
        if not f:
            f.append(Finding(rule="amendment.substitution", outcome="notify",
                             message=f"Valio picked {readable_name(new['name'])} instead of "
                                     f"{readable_name(old['name'])} ({a['reason']}); the mandate allows it.",
                             product_id=new["product_id"]))
    tier = max([x.outcome for x in f] + ["notify"], key=_RANK.__getitem__)
    return tier, [x.model_dump() for x in f]


# --- applying ---------------------------------------------------------------------------------------------------

def _cancel(o: dict, i: int, aid: str, credits: list[dict], reason: str) -> None:
    l = o["lines"][i]
    if not l.get("quantity"):
        return
    credits.append({"amendment_id": aid, "line": i, "product_id": l["product_id"], "name": l["name"],
                    "quantity": -l["quantity"], "amount_ex_vat": -(l.get("line_total_ex_vat") or 0),
                    "reason": reason})
    o["lines"][i] = {**l, "quantity": 0, "line_total_ex_vat": 0.0, "cancelled": True, "amended_by": aid}


def _apply(o: dict, a: dict, accepted: bool) -> dict:
    """Change the order's lines for an applied/accepted (or a rejected) amendment; returns the effect."""
    before = o["total_ex_vat"]
    o.setdefault("original_total_ex_vat", o["total_ex_vat"])
    o.setdefault("original_total_inc_vat", o.get("total_inc_vat"))
    o["lines"] = [dict(l) for l in o["lines"]]
    credits: list[dict] = []
    aid, kind = a["id"], a["kind"]
    if not accepted:  # a rejected substitution or delay: the line(s) won't be delivered as ordered
        for i in _lines_of(o, a):
            _cancel(o, i, aid, credits, f"{kind} rejected by the buyer")
    elif kind == "cancel_line":
        _cancel(o, a["line"], aid, credits, a["reason"])
    elif kind == "short_pick":
        l = o["lines"][a["line"]]
        new_total = round((l.get("unit_price_ex_vat") or 0) * a["delivered_quantity"], 2)
        credits.append({"amendment_id": aid, "line": a["line"], "product_id": l["product_id"], "name": l["name"],
                        "quantity": round(a["delivered_quantity"] - l["quantity"], 3),
                        "amount_ex_vat": round(new_total - (l.get("line_total_ex_vat") or 0), 2),
                        "reason": a["reason"]})
        o["lines"][a["line"]] = {**l, "quantity": a["delivered_quantity"], "line_total_ex_vat": new_total,
                                 "ordered_quantity": l.get("ordered_quantity", l["quantity"]), "amended_by": aid}
    elif kind == "delay":
        for i in _lines_of(o, a):
            if o["lines"][i].get("quantity"):
                o["lines"][i] = {**o["lines"][i], "delivery_date": a["new_delivery_date"], "amended_by": aid}
        o["delivery_date"] = max((l["delivery_date"] for l in o["lines"] if l.get("delivery_date")),
                                 default=o["delivery_date"])
    else:  # substitution_at_pick accepted
        old, new = o["lines"][a["line"]], _sub_line(o, a)
        diff = round(new["line_total_ex_vat"] - (old.get("line_total_ex_vat") or 0), 2)
        if diff:
            credits.append({"amendment_id": aid, "line": a["line"], "product_id": new["product_id"],
                            "name": new["name"], "quantity": new["quantity"], "amount_ex_vat": diff,
                            "reason": f"substitute for {old['product_id']}"})
        o["lines"][a["line"]] = {**{k: new.get(k) for k in ("product_id", "name", "quantity", "sales_unit",
                                                            "unit_price_ex_vat", "line_total_ex_vat",
                                                            "delivery_date", "logistic_model", "order_cutoff")},
                                 "substituted_for": {"product_id": old["product_id"], "name": old["name"],
                                                     "quantity": old["quantity"]}, "amended_by": aid}
    o["total_ex_vat"] = _sum(o["lines"])
    if o.get("original_total_inc_vat") and o["original_total_ex_vat"]:  # order lines carry no VAT: keep the ratio
        o["total_inc_vat"] = round(o["original_total_inc_vat"] * o["total_ex_vat"] / o["original_total_ex_vat"], 2)
    o["credit_notes"] = (o.get("credit_notes") or []) + credits
    return {"order_total_before": before, "order_total_after": o["total_ex_vat"],
            "credit_ex_vat": round(sum(c["amount_ex_vat"] for c in credits), 2), "credit_notes": credits}


def _sign(a: dict) -> dict:
    a = {k: v for k, v in a.items() if k not in ("jws", "erp")}
    return {**a, "jws": signing.sign(a, TYP)}


def receipt_summary(o: dict) -> list[dict]:
    """What the order receipt signs about its amendments (each one's own JWS is hashed in)."""
    return [{"id": a["id"], "kind": a["kind"], "status": a["status"], "tier": a["tier"], "line": a.get("line"),
             "jws_sha256": hashlib.sha256(a["jws"].encode()).hexdigest()} for a in o.get("amendments") or []]


def _store(o: dict, now_iso: str) -> None:
    o["updated_at"] = now_iso
    o["receipt_jws"] = S._receipt(o)
    if not store.update_order(o, expected_status="ordered"):
        raise _err("not_ordered", f"Order {o['id']} changed meanwhile; read it again.")


def _after(o: dict, a: dict, event: str, mandate: Mandate, changed: bool) -> dict:
    if changed:
        try:
            erp.submit_change(o, a)
        except Exception:  # noqa: BLE001 - never fail the amendment over the outbox; enqueue_change audits
            pass
    notify.send(event, {**o, "amendment": a}, mandate)
    return view(store.get_order(o["id"]) or o)


def view(o: dict) -> dict:
    """The order for reading, each amendment with its ERP change-message status."""
    if not o or not o.get("amendments"):
        return o
    out = []
    for a in o["amendments"]:
        try:
            st = erp.status(erp.change_key(o["id"], a["id"]))
        except Exception:  # noqa: BLE001
            st = None
        out.append({**a, "erp": st})
    return {**o, "amendments": out}


# --- the three calls --------------------------------------------------------------------------------------------

def report(order_id: str, req: AmendmentIn, authority: str) -> dict:
    """Valio reports an exception on an ordered order."""
    with S._order_lock:
        o = store.get_order(order_id)
        if not o:
            raise _err("order_not_found", f"No order {order_id}.", 404)
        if o["status"] != "ordered":
            raise _err("not_ordered", f"Order {order_id} is {o['status']}: only an ordered order can be amended.")
        a = req.model_dump(mode="json")
        n = len(o["lines"])
        need = {"short_pick": ("line", "delivered_quantity"), "cancel_line": ("line",),
                "delay": ("new_delivery_date",), "substitution_at_pick": ("line", "product_id", "quantity")}
        if missing := [k for k in need[req.kind] if a.get(k) is None]:
            raise _err("invalid_argument", f"{req.kind} needs {', '.join(missing)}.", 400)
        if a.get("line") is not None:
            if a["line"] >= n:
                raise _err("invalid_argument", f"Order {order_id} has lines 0..{n - 1}; no line {a['line']}.", 400)
            if not o["lines"][a["line"]].get("quantity"):
                raise _err("line_cancelled", f"Line {a['line']} of {order_id} was already cancelled.")
        lines = set(_lines_of(o, a))
        if any(x["status"] == "pending_buyer" and lines & set(_lines_of(o, x)) for x in o.get("amendments") or []):
            raise _err("amendment_pending", f"A pending amendment on {order_id} already covers this line; it must "
                       "be accepted or rejected first.")
        if req.kind == "short_pick" and a["delivered_quantity"] >= o["lines"][a["line"]]["quantity"]:
            raise _err("invalid_argument", "delivered_quantity must be below the line's quantity "
                       f"({o['lines'][a['line']]['quantity']:g}).", 400)
        if req.kind == "delay":
            cur = max(o["lines"][i].get("delivery_date") or o["delivery_date"] for i in lines)
            if a["new_delivery_date"] <= cur:
                raise _err("invalid_argument", f"new_delivery_date must be after {cur}.", 400)
        if req.kind == "substitution_at_pick":
            if not catalog.get(a["product_id"]):
                raise _err("product_not_found", f"No product {a['product_id']}.", 404)
            if a["product_id"] == o["lines"][a["line"]]["product_id"]:
                raise _err("invalid_argument", "A substitute must be another product.", 400)
        mandate = S._mandate(o["mandate_id"])
        now = S._now().isoformat(timespec="milliseconds")
        tier, findings = assess(o, a, mandate)
        by = a.pop("by")
        a = {"id": S._id("AM"), "order_id": order_id, **{k: v for k, v in a.items() if v is not None or k == "line"},
             "reported_by": by, "authority": authority, "reported_at": now, "tier": tier, "findings": findings,
             "status": "applied" if tier == "notify" else "pending_buyer", "resolution": None, "effect": None}
        if a["status"] == "applied":
            a["effect"] = _apply(o, a, True)
        a = _sign(a)
        o["amendments"] = (o.get("amendments") or []) + [a]
        _store(o, now)
        store.audit("order.amended", a["reported_by"], "human", order_id, o["buyer"],
                    {"order_id": order_id, "amendment_id": a["id"], "kind": a["kind"], "line": a.get("line"),
                     "tier": tier, "status": a["status"], "reason": a["reason"], "authority": authority,
                     "fired": findings, "effect": a["effect"], "jws": a["jws"]})
    return _after(o, a, "order.amended" if a["status"] == "applied" else "order.amendment_pending", mandate,
                  a["status"] == "applied")


def resolve(order_id: str, amendment_id: str, accept: bool, res: Resolution, *, authority: str,
            agent: bool) -> dict:
    """The buyer accepts or rejects a pending_buyer amendment. `agent`: decided with the agent's credential."""
    with S._order_lock:
        o = store.get_order(order_id)
        if not o:
            raise _err("order_not_found", f"No order {order_id}.", 404)
        i = next((k for k, x in enumerate(o.get("amendments") or []) if x["id"] == amendment_id), None)
        if i is None:
            raise _err("amendment_not_found", f"Order {order_id} has no amendment {amendment_id}.", 404)
        a = dict(o["amendments"][i])
        if a["status"] != "pending_buyer":
            raise _err("amendment_not_pending", f"Amendment {amendment_id} is {a['status']}, not waiting for the "
                       "buyer.")
        if o["status"] != "ordered":
            raise _err("not_ordered", f"Order {order_id} is {o['status']}.")
        mandate = S._mandate(o["mandate_id"])
        tier, findings = assess(o, a, mandate)  # the mandate as it stands now
        if accept and agent and tier not in ("silent", "notify"):
            store.audit("agent.amendment_accept_refused", o["agent_id"], "agent", order_id, o["buyer"],
                        {"order_id": order_id, "amendment_id": amendment_id, "tier": tier, "fired": findings})
            raise _err("agent_cannot_accept", f"Amendment {amendment_id} is {tier}-tier under mandate "
                       f"{mandate.id} as it stands: only a human approver can accept it (X-Approver-Code). The "
                       "agent may reject it. This attempt is in the audit log.", 403)
        if accept and tier == "block":
            raise _err("amendment_blocked", f"The mandate as it stands blocks amendment {amendment_id}: "
                       + "; ".join(f["message"] for f in findings if f["outcome"] == "block")
                       + ". Reject it (the line is cancelled with a credit), or change the mandate first.")
        now = S._now().isoformat(timespec="milliseconds")
        a["status"] = "accepted" if accept else "rejected"
        a["resolution"] = {"by": res.by, "authority": authority, "actor_type": "agent" if agent else "human",
                           "accepted": accept, "note": res.note, "at": now, "tier_at_resolution": tier,
                           "findings_at_resolution": findings}
        a["effect"] = _apply(o, a, accept)
        a = _sign(a)
        o["amendments"] = [*o["amendments"][:i], a, *o["amendments"][i + 1:]]
        _store(o, now)
        store.audit("order.amendment_accepted" if accept else "order.amendment_rejected", res.by,
                    "agent" if agent else "human", order_id, o["buyer"],
                    {"order_id": order_id, "amendment_id": amendment_id, "kind": a["kind"], "authority": authority,
                     "tier": tier, "note": res.note, "effect": a["effect"], "jws": a["jws"]})
    return _after(o, a, "order.amendment_decided", mandate, True)


def verify(order: dict) -> list[dict]:
    """Each amendment's JWS verifies and matches what the order stores (for tests and auditors)."""
    out = []
    for a in order.get("amendments") or []:
        p = signing.verify(a.get("jws") or "", TYP)
        body = {k: v for k, v in a.items() if k not in ("jws", "erp")}
        out.append({"id": a["id"], "valid": p is not None and p == body})
    return out
