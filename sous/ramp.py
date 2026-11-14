"""Earned autonomy (the trust ramp): the buyer delegates more as the record shows the agent's orders were right.

A new customer reviews almost everything at first. Each held order the buyer's human approves unchanged is
evidence; each one they edit (approve with changes) or reject is evidence the other way. Per mandate and per
scope, Sous keeps that track record and, once it is clean enough, PROPOSES a mandate change that lowers that
scope's tier from ask to notify. It never changes a mandate on its own: the buyer's human accepts or declines.

Scope = (rule, product type). The product type is read from the product's name (sous/substitutes.py
product_type, e.g. kuohukerma = whipping cream); a product whose type can't be read is its own scope
(product:<id>). Only the rules in RAMPABLE can be delegated, and only from ask to notify.

The threshold (all must hold, counted since the record last restarted: since the mandate's first order, a
decline, an acceptance or a revert of the same scope, and at most WINDOW_DAYS back):
  * at least MIN_APPROVALS held orders with a finding in the scope approved unchanged,
  * spread over at least MIN_WEEKS distinct ISO weeks (one busy day is not a track record),
  * no rejection (MAX_REJECTED) and no edit (MAX_EDITED) of such an order.
An order held for several reasons counts for every rampable scope it was held for: approving it unchanged means
the human saw each finding and let it through; rejecting it counts against all of them (the conservative side).

Never proposed (NEVER, with the reason): block-tier findings, allergens, budget and hard limits (spend.*), price
caps (price.comparison_cap), category / claims / origin rules, Valio's holds and availability, mandate validity,
autonomy caps and pauses, and the findings remembered per product by sous/remembered.py.

Withdrawing trust: after a scope was delegated, one held order with that scope's earned.* finding that the buyer
rejects or edits makes Sous propose reverting it (back to ask). Also only a proposal.

Accepting a proposal is a normal mandate change: the gateway re-signs the mandate (sous-mandate+jws, a new
version in GET /mandates/{id}/history, audit mandate.updated) with `delegated` changed. A buyer-signed mandate
can't be re-signed by the gateway: the buyer re-signs it with the change (409 mandate_buyer_signed, with the
amended mandate). Audit: ramp.proposed (once per proposal, when first shown), ramp.accepted, ramp.declined.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from . import catalog, config, fi, store
from .mandate import Delegation, Finding, Mandate, readable_name

MIN_APPROVALS = 5  # held orders in the scope approved unchanged
MIN_WEEKS = 2  # ... in at least this many distinct ISO weeks
MAX_REJECTED = 0  # any rejection in the scope: no proposal
MAX_EDITED = 0  # any "approve with changes" in the scope: no proposal
WINDOW_DAYS = 365  # evidence older than this doesn't count

# ask-tier rule -> the notify rule it becomes in a delegated scope
RAMPABLE = {"substitution": "earned.substitution", "price.changed": "earned.price_changed",
            "price.comparison_changed": "earned.price_comparison_changed",
            "quantity.unusual": "earned.quantity_unusual", "supply.reliability": "earned.supply_reliability",
            "delivery.later_date": "earned.delivery_later_date"}
EARNED = {v: k for k, v in RAMPABLE.items()}
# What each rampable rule is about, in the evidence sentence
WHAT = {"substitution": ("a substitute", "korvaava tuote"),
        "price.changed": ("a price change", "hinnanmuutos"),
        "price.comparison_changed": ("a pricier product per L or kg", "kalliimpi litra- tai kilohinta"),
        "quantity.unusual": ("an unusual quantity", "tavallisesta poikkeava määrä"),
        "supply.reliability": ("a less reliable delivery record", "heikompi toimitusvarmuus"),
        "delivery.later_date": ("a later delivery date", "myöhempi toimituspäivä")}
# Never proposed, whatever the record says (documented; tests/test_ramp.py checks none is in RAMPABLE)
NEVER = {
    "block tier": "a finding at block is refused by the mandate; only ask can become notify",
    "allergen.*": "allergen rules protect people; a clean record never relaxes them",
    "spend.*": "budget and hard limits (per order, daily, weekly, split orders) are the buyer's money caps",
    "price.comparison_cap": "a maximum price per L or kg is a hard limit the buyer set",
    "product.category / product.claims / origin.*": "what may be bought at all is the buyer's policy",
    "product.held / availability.* / price.expires / need.partial": "Valio's facts, not the agent's judgement",
    "mandate.* / autonomy.* / ordering.paused": "who may order and whether ordering is on",
    "product.first_time / unit.interpreted / product.type_mismatch": "per product, remembered by sous/remembered.py",
    "purpose.*": "what a product is used for (a recipe, an allergy) is the buyer's call each time",
}

_EVENTS = ("ramp.proposed", "ramp.accepted", "ramp.declined")


def _now() -> datetime:
    return datetime.now(config.TZ)


def scope_of(pid: str) -> tuple[str, str]:
    """(scope key, label): the product's type when its name reveals one, else the product itself."""
    p = catalog.get(pid) or {}
    try:
        from .substitutes import product_type  # lazy: substitutes imports mandate
        t = product_type(p) if p else {}
    except Exception:  # an unreadable name never breaks an order
        t = {}
    if t.get("key"):
        return f"type:{t['key']}", str(t.get("label") or t["key"])
    return f"product:{pid}", readable_name(p.get("name") or pid)


# --- applied in evaluate() --------------------------------------------------------------------------------------

def relax(findings: list[Finding], mandate: Mandate) -> list[Finding]:
    """`findings` with each ask of a RAMPABLE rule in a delegated scope turned into its earned.* notify twin.
    Anything else (every block, every other rule) is returned unchanged."""
    keys = {(d.rule, d.scope): d for d in mandate.delegated if d.rule in RAMPABLE}
    if not keys:
        return findings
    out = []
    for f in findings:
        d = keys.get((f.rule, scope_of(f.product_id)[0])) if (f.outcome == "ask" and f.rule in RAMPABLE
                                                               and f.product_id) else None
        if d is None:
            out.append(f)
            continue
        out.append(f.model_copy(update={
            "rule": RAMPABLE[f.rule], "outcome": "notify", "rule_url": None,
            "message": f"{f.message} Delegated by the buyer on {d.since[8:10]}.{d.since[5:7]}. after a clean "
                       f"approval record ({d.label}): ordered, and the buyer is told.",
            "message_fi": f"{fi.finding(f)} Ostaja delegoi tämän {fi.short_day(d.since)} puhtaan "
                          f"hyväksyntähistorian perusteella ({d.label}): tilataan, ja ostajalle kerrotaan."}))
        out[-1].model_post_init(None)
    return out


# --- the track record -------------------------------------------------------------------------------------------

def _result(o: dict) -> str | None:
    """approved / edited / rejected for an order a human decided; None otherwise."""
    a = o.get("approval") or {}
    if not a or o.get("approval_changes"):  # not held, or the replacement order of an edit (counted on the original)
        return None
    if o.get("status") == "ordered" and a.get("approved"):
        return "approved"
    if o.get("status") == "rejected":
        return "edited" if a.get("replaced_by") else "rejected"
    return None


def _events(mandate_id: str) -> list[dict]:
    return [e for e in store.audit_events(subject=mandate_id, limit=5000, where=(" AND event LIKE ?", ["ramp.%"]))
            if e.get("subject") == mandate_id]


def records(mandate: Mandate, now: datetime | None = None) -> dict[tuple[str, str], dict]:
    """{(rule, scope): {rule, scope, label, held: [...], relaxed: [...]}} from the mandate's decided orders;
    `held` are orders held with the scope's ask finding, `relaxed` orders with its earned.* finding."""
    since = ((now or _now()) - timedelta(days=WINDOW_DAYS)).isoformat()
    out: dict[tuple[str, str], dict] = {}
    for o in store.list_orders(mandate_id=mandate.id):
        res = _result(o)
        at = (o.get("approval") or {}).get("at") or o.get("updated_at") or ""
        if res is None or at < since:
            continue
        seen = set()
        for f in (o.get("decision") or {}).get("findings", []):
            rule, pid = f.get("rule"), f.get("product_id")
            if not pid or (rule, pid) in seen:
                continue
            if f.get("outcome") == "ask" and rule in RAMPABLE:
                kind = "held"
            elif rule in EARNED:
                kind, rule = "relaxed", EARNED[rule]
            else:
                continue
            seen.add((rule, pid))
            scope, label = scope_of(pid)
            r = out.setdefault((rule, scope), {"rule": rule, "scope": scope, "label": label, "held": [],
                                               "relaxed": []})
            if not any(x["order_id"] == o["id"] for x in r[kind]):
                r[kind].append({"order_id": o["id"], "at": at, "result": res, "product_id": pid})
    for r in out.values():
        r["held"].sort(key=lambda x: x["at"])
        r["relaxed"].sort(key=lambda x: x["at"])
    return out


def _week(at: str) -> str:
    y, w, _ = datetime.fromisoformat(at).isocalendar()
    return f"{y}-W{w:02d}"


def _pid(mandate_id: str, kind: str, rule: str, scope: str, start: str) -> str:
    return "RAMP-" + hashlib.sha256(f"{mandate_id}|{kind}|{rule}|{scope}|{start}".encode()).hexdigest()[:10].upper()


def _restart(events: list[dict], rule: str, scope: str) -> str:
    """When the scope's record last restarted: its latest acceptance or decline ('' if never)."""
    ts = [e["ts"] for e in events if e["event"] in ("ramp.accepted", "ramp.declined")
          and e["data"].get("rule") == rule and e["data"].get("scope") == scope]
    return max(ts, default="")


def _count(entries: list[dict]) -> dict:
    n = {k: sum(1 for x in entries if x["result"] == k) for k in ("approved", "edited", "rejected")}
    ok = [x for x in entries if x["result"] == "approved"]
    return {**n, "weeks": len({_week(x["at"]) for x in ok}), "first_at": entries[0]["at"] if entries else None,
            "last_at": entries[-1]["at"] if entries else None}


def _relax_text(r: dict, c: dict) -> tuple[str, str]:
    what, what_fi = WHAT[r["rule"]]
    d, n, total = c["first_at"], c["approved"], c["approved"] + c["edited"] + c["rejected"]
    en = (f"You approved {n} of {total} held orders with {what} on {r['label']} unchanged since "
          f"{d[8:10]}.{d[5:7]}., over {c['weeks']} weeks; none were edited or rejected. Sous proposes: {what} "
          f"on {r['label']} goes through and you are told (notify), instead of waiting for you (ask).")
    fi_ = (f"Hyväksyit {n}/{total} pidätettyä tilausta ({r['label']}: {what_fi}) muuttamatta "
           f"{fi.short_day(d)} lähtien {c['weeks']} eri viikolla; yhtään ei muutettu eikä hylätty. Sous ehdottaa: "
           f"{what_fi} ({r['label']}) menee läpi ja saat ilmoituksen sen sijaan, että tilaus odottaa sinua.")
    return en, fi_


def _revert_text(d: Delegation, bad: dict) -> tuple[str, str]:
    what, what_fi = WHAT[d.rule]
    verb, verb_fi = ("rejected", "hylkäsit") if bad["result"] == "rejected" else ("changed", "muutit")
    en = (f"After {what} on {d.label} was delegated on {d.since[8:10]}.{d.since[5:7]}., you {verb} order "
          f"{bad['order_id']} on {bad['at'][8:10]}.{bad['at'][5:7]}. that relied on it. Sous proposes to ask you "
          f"again (ask) for {what} on {d.label}.")
    fi_ = (f"Kun {what_fi} ({d.label}) delegoitiin {fi.short_day(d.since)}, {verb_fi} tilauksen "
           f"{bad['order_id']} {fi.short_day(bad['at'])}, joka nojasi siihen. Sous ehdottaa, että sinulta "
           f"kysytään taas ({d.label}: {what_fi}).")
    return en, fi_


def proposals(mandate: Mandate, recs: dict | None = None, events: list[dict] | None = None,
              now: datetime | None = None) -> list[dict]:
    """Open proposals for this mandate: relax (ask -> notify) for a scope whose record passes the threshold,
    revert (back to ask) for a delegated scope with a rejection or edit since it was delegated."""
    if mandate.revoked:
        return []
    recs = records(mandate, now) if recs is None else recs
    events = _events(mandate.id) if events is None else events
    delegated = {(d.rule, d.scope): d for d in mandate.delegated}
    out = []
    for (rule, scope), d in delegated.items():
        start = max(d.since, _restart(events, rule, scope))
        r = recs.get((rule, scope)) or {"relaxed": []}
        bad = next((x for x in r["relaxed"] if x["at"] > start and x["result"] in ("rejected", "edited")), None)
        if bad and rule in RAMPABLE:
            en, fi_ = _revert_text(d, bad)
            out.append({"id": _pid(mandate.id, "revert", rule, scope, start), "kind": "revert", "rule": rule,
                        "scope": scope, "label": d.label, "from_tier": "notify", "to_tier": "ask",
                        "evidence": en, "evidence_fi": fi_, "orders": [bad["order_id"]]})
    for (rule, scope), r in recs.items():
        if (rule, scope) in delegated or rule not in RAMPABLE:
            continue
        start = _restart(events, rule, scope)
        held = [x for x in r["held"] if x["at"] > start]
        c = _count(held)
        if c["approved"] < MIN_APPROVALS or c["weeks"] < MIN_WEEKS or c["rejected"] > MAX_REJECTED \
                or c["edited"] > MAX_EDITED:
            continue
        en, fi_ = _relax_text(r, c)
        out.append({"id": _pid(mandate.id, "relax", rule, scope, start), "kind": "relax", "rule": rule,
                    "scope": scope, "label": r["label"], "from_tier": "ask", "to_tier": "notify",
                    "evidence": en, "evidence_fi": fi_, "record": c, "orders": [x["order_id"] for x in held]})
    return out


def status(mandate: Mandate, audit: bool = True, now: datetime | None = None) -> dict:
    """GET /mandates/{id}/autonomy: the track record per scope, what is delegated, and the open proposals.
    With `audit`, a proposal shown for the first time is written to the audit log (ramp.proposed)."""
    recs, events = records(mandate, now), _events(mandate.id)
    props = proposals(mandate, recs, events, now)
    if audit:
        shown = {e["data"].get("proposal_id") for e in events if e["event"] == "ramp.proposed"}
        for p in props:
            if p["id"] not in shown:
                store.audit("ramp.proposed", "sous-ramp", "gateway", mandate.id, mandate.buyer,
                            {"proposal_id": p["id"], **{k: p[k] for k in ("kind", "rule", "scope", "label",
                                                                          "from_tier", "to_tier", "evidence",
                                                                          "orders")}})
    track = []
    for (rule, scope), r in sorted(recs.items()):
        c = _count(r["held"])
        track.append({"rule": rule, "scope": scope, "label": r["label"], **c,
                      "delegated": any(d.rule == rule and d.scope == scope for d in mandate.delegated),
                      "orders": [x["order_id"] for x in r["held"]]})
    return {"mandate_id": mandate.id,
            "policy": {"min_approvals_unchanged": MIN_APPROVALS, "min_distinct_weeks": MIN_WEEKS,
                       "max_rejected": MAX_REJECTED, "max_edited": MAX_EDITED, "window_days": WINDOW_DAYS,
                       "only": "ask -> notify, never automatic: the buyer accepts or declines each proposal",
                       "rampable_rules": list(RAMPABLE), "never": NEVER},
            "track_record": track, "delegated": [d.model_dump() for d in mandate.delegated], "proposals": props}


def _find(mandate: Mandate, proposal_id: str) -> dict:
    from .service import GatewayError
    p = next((p for p in proposals(mandate) if p["id"] == proposal_id), None)
    if p is None:
        raise GatewayError("proposal_not_open", f"No open proposal {proposal_id} for mandate {mandate.id} (it was "
                           "decided already, or the record changed): GET /mandates/{id}/autonomy.", 409)
    return p


def refuse_agent(mandate_id: str, proposal_id: str, token: str):
    """An agent credential tried to accept or decline a proposal: audited, and the 403 to raise."""
    from . import agent_auth
    from .service import GatewayError
    who = agent_auth.identify(token) or {}
    m = store.get_mandate(mandate_id) or {}
    store.audit("agent.approval_attempt_refused", f"agent credential ...{who.get('last4', '?')}", "agent",
                mandate_id, m.get("buyer"), {"mandate_id": mandate_id, "proposal_id": proposal_id,
                                             "action": "autonomy proposal", "credential_last4": who.get("last4"),
                                             "credential_agent_ids": who.get("agent_ids") or []})
    return GatewayError("agent_cannot_approve", f"Agents can't accept or decline earned-autonomy proposals: an agent "
                        f"credential was presented. Proposal {proposal_id} waits for the buyer's human; this attempt "
                        "is in the audit log.", 403)


def _check_approval_rules(m: Mandate, p: dict, by: str) -> None:
    """A relax lets through orders a human approves today, so it needs what approving them needs: a passkey on a
    passkey-only mandate (there is no passkey flow for proposals: the buyer re-signs the mandate with the change),
    and four eyes (the accepter is not the identity the agent's orders are requested by)."""
    from . import controls
    from .service import GatewayError
    if m.approval_method == "passkey":
        raise GatewayError("passkey_required", f"Mandate {m.id} requires passkey approval, so delegating more can't be "
                           f"accepted with the approver code: the buyer re-signs the mandate with {p['rule']} on "
                           f"{p['label']} delegated (POST /mandates/credential), or declines the proposal.", 403)
    if m.separate_approver:
        controls.check_separate_approver(m, {"id": p["id"], "kind": "proposal"}, by)


def accept(mandate_id: str, proposal_id: str, by: str, authority: str | None = None) -> dict:
    """The buyer's human accepts a proposal: the mandate is re-signed with `delegated` changed (a normal mandate
    update, new version). Returns {proposal, mandate, jws}."""
    from . import service as S
    with S._order_lock:
        payload = S._signed_mandate(mandate_id)
        m = Mandate(**payload)
        p = _find(m, proposal_id)
        if p["kind"] == "relax":  # widens autonomy: the mandate's own approval rules apply (a revert only narrows)
            _check_approval_rules(m, p, by)
        keep = [d for d in m.delegated if (d.rule, d.scope) != (p["rule"], p["scope"])]
        if p["kind"] == "relax":
            keep.append(Delegation(rule=p["rule"], scope=p["scope"], label=p["label"],
                                   since=_now().isoformat(timespec="seconds"), proposal_id=p["id"],
                                   evidence=p["evidence"]))
        new = Mandate(**{**m.model_dump(), "delegated": [d.model_dump() for d in keep]})
        if payload.get("credential_sha256"):
            raise S.GatewayError("mandate_buyer_signed", f"Mandate {mandate_id} is signed by the buyer, so the "
                                 "gateway can't change it: the buyer re-signs it with this change (POST "
                                 f"/mandates/credential). Amended delegated: {new.model_dump(mode='json')['delegated']}",
                                 409)
        out = S._issue_mandate(new, False)
    store.audit("ramp.accepted", by, "human", mandate_id, m.buyer,
                {"proposal_id": p["id"], "kind": p["kind"], "rule": p["rule"], "scope": p["scope"],
                 "label": p["label"], "from_tier": p["from_tier"], "to_tier": p["to_tier"], "orders": p["orders"],
                 "authority": authority, "mandate_jws_sha256": hashlib.sha256(out["jws"].encode()).hexdigest()})
    return {"proposal": p, "mandate": out["mandate"], "jws": out["jws"]}


def decline(mandate_id: str, proposal_id: str, by: str, note: str | None = None,
            authority: str | None = None) -> dict:
    """The buyer's human declines: nothing changes, and the scope's record restarts (no new proposal until it
    passes the threshold again after now)."""
    from . import service as S
    m = S._mandate(mandate_id)
    p = _find(m, proposal_id)
    store.audit("ramp.declined", by, "human", mandate_id, m.buyer,
                {"proposal_id": p["id"], "kind": p["kind"], "rule": p["rule"], "scope": p["scope"],
                 "label": p["label"], "note": note, "orders": p["orders"], "authority": authority})
    return {"proposal": p, "declined": True}
