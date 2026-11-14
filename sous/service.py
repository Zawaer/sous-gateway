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

from . import agent_auth, aimo, catalog, clock, config, credentials, exposure, notify, options as OPT, quote as Q, sandbox, signing, store, substitutes, usual
from .mandate import Mandate, evaluate


class GatewayError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, retry_after: int | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.retry_after = code, message, status, retry_after


def valio_error(e: aimo.AimoError) -> GatewayError:
    if isinstance(e, aimo.SandboxLimit):  # a judge sandbox over its share of the Valio key (sandbox.py)
        return GatewayError("sandbox_limit", str(e), 429, e.retry_after)
    return GatewayError("valio_unavailable", str(e), 503, e.retry_after)


def _id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4).upper()}"


def _now() -> datetime:
    """Business time (cut-offs, expiry, deadlines, 'today'): the demo clock when SOUS_DEMO_NOW is set."""
    return clock.now()


def _real_now() -> datetime:
    """Credential/signature validity windows: always real time, even under the demo clock (in real mode this is
    _now(), so the two never disagree there)."""
    return clock.real_now() if clock.is_demo() else _now()


# The gateway runs as one process: this lock makes "check idempotency key, claim quote, sum today's spend,
# evaluate, store the order" and "check pending, decide, store" atomic, so concurrent requests can't double
# order, overrun the daily limit or both approve and reject the same order.
_order_lock = threading.RLock()


def _signed_mandate(mandate_id: str) -> dict:
    """The gateway-signed mandate payload; for a buyer-signed mandate the buyer's VC is re-verified too."""
    m = store.get_mandate(mandate_id)
    if not m:
        raise GatewayError("mandate_not_found", f"No mandate {mandate_id}.", 404)
    payload = signing.verify(m.pop("_jws", None) or "", "sous-mandate+jws")
    if payload is None or payload.get("id") != mandate_id:
        raise GatewayError("mandate_signature", f"Mandate {mandate_id}'s signature does not verify.", 409)
    if payload.get("credential_sha256"):
        try:
            _check_credential(payload)
        except credentials.CredentialError as e:
            raise GatewayError("mandate_signature", f"Mandate {mandate_id}'s buyer credential: {e.message}", 409)
    return payload


def _mandate(mandate_id: str) -> Mandate:
    return Mandate(**_signed_mandate(mandate_id))  # the signed mandate, not the stored copy


_BUYER_SIGNED = ("issuer", "credential_sha256")  # extra keys the gateway countersigns for a buyer-signed mandate


def _check_credential(payload: dict) -> dict:
    """The stored VC is the one the gateway countersigned, its signature verifies, its key may still speak for
    the buyer, and it says what the countersigned mandate says. Returns the parsed credential."""
    row = credentials.get(payload["id"])
    if not row or credentials.sha256(row["vc_jwt"]) != payload["credential_sha256"]:
        raise credentials.CredentialError("credential_missing", "the countersigned credential is missing or changed.")
    c = credentials.parse_vc(row["vc_jwt"])
    if c["issuer"] != (payload.get("issuer") or {}).get("did"):
        raise credentials.CredentialError("credential_issuer", "the issuer differs from the countersigned one.")
    if not credentials.key_status(c["mandate"].buyer, c["issuer"]):
        raise credentials.CredentialError("issuer_not_authorised",
                                          f"{c['issuer']} is no longer registered for buyer {c['mandate'].buyer}.")
    if _comparable(c["mandate"]) != _comparable(Mandate(**payload)):
        raise credentials.CredentialError("credential_mismatch", "the mandate differs from what the buyer signed.")
    return c


def _comparable(m: Mandate) -> dict:
    return m.model_dump(mode="json", exclude={"revoked"})


# --- mandates ---------------------------------------------------------------

def issue_mandate(m: Mandate, mint_agent_credential: bool | None = None) -> dict:
    """Sign and store the mandate. When the agent must authenticate (or when asked), also mint the agent's
    credential: its token is in the response this once and never again."""
    with _order_lock:  # an order being placed or approved sees the mandate before or after the change, not both
        return _issue_mandate(m, mint_agent_credential)


def _issue_mandate(m: Mandate, mint_agent_credential: bool | None) -> dict:
    data = m.model_dump(mode="json")
    prev = store.get_mandate(m.id)
    if prev and prev.get("revoked") and not m.revoked:
        raise GatewayError("mandate_revoked", f"Mandate {m.id} was revoked; issue a new mandate id instead.", 409)
    if prev and prev.get("credential_sha256"):  # the gateway doesn't overwrite what the buyer signed
        raise GatewayError("mandate_buyer_signed", f"Mandate {m.id} is signed by the buyer; they re-sign it "
                           "(POST /mandates/credential) or an approver revokes it.", 409)
    jws = signing.sign(data, "sous-mandate+jws")
    store.put_mandate(data, jws)
    store.audit("mandate.updated" if prev else "mandate.issued", m.issued_by, "human", m.id, m.buyer,
                {"mandate": data})
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
    cred = credentials.get(mandate_id) if m.get("credential_sha256") else None
    return {"mandate": m, "jws": jws, "issuer": m.get("issuer") or {"did": credentials.gateway_did(), "kind": "gateway"},
            "credential": cred["vc_jwt"] if cred else None,
            "revocation": cred["revocation_jws"] if cred else None}


def _revoke(payload: dict) -> dict:
    """Re-sign the mandate as revoked, keeping the buyer-signed fields."""
    data = {**Mandate(**{**payload, "revoked": True}).model_dump(mode="json"),
            **{k: payload[k] for k in _BUYER_SIGNED if k in payload}}
    store.put_mandate(data, signing.sign(data, "sous-mandate+jws"))
    return data


def revoke_mandate(mandate_id: str, by: str) -> dict:
    with _order_lock:  # an order being placed or approved right now sees the mandate before or after, not both
        data = _revoke(_signed_mandate(mandate_id))
    store.audit("mandate.revoked", by, "human", mandate_id, data["buyer"], {})
    return data


# --- buyer-signed mandates (W3C Verifiable Credentials, see credentials.py) --

def _cred_error(e: credentials.CredentialError) -> GatewayError:
    return GatewayError(e.code, e.message, e.status)


def issue_credential_mandate(token: str, mint_agent_credential: bool | None = None) -> dict:
    """Accept a mandate the buyer signed as a VC (vc+jwt): verify it, check its key may speak for the buyer
    (registered by an approver, or pinned on first use), store it with the VC and countersign it."""
    with _order_lock:
        try:
            c = credentials.parse_vc(token)
            credentials.check_window(c, _real_now())
        except credentials.CredentialError as e:
            raise _cred_error(e) from None
        m, did = c["mandate"], c["issuer"]
        keys = credentials.buyer_keys(m.buyer)
        pin = not keys
        if keys and not any(k["did"] == did for k in keys):
            raise GatewayError("issuer_not_authorised", f"{did} is not a registered key for buyer {m.buyer!r}; "
                               "an approver can add it at POST /buyers/{buyer}/keys.", 403)
        if pin and not config.BUYER_KEY_TOFU:
            raise GatewayError("issuer_not_authorised",
                               f"Buyer {m.buyer!r} has no registered keys and trust on first use is off.", 403)
        if pin and any(x["buyer"] == m.buyer for x in store.list_mandates()):
            raise GatewayError("issuer_not_authorised",
                               f"Buyer {m.buyer!r} already has gateway-issued mandates, so its first key isn't "
                               "pinned automatically; an approver must register it (POST /buyers/{buyer}/keys).", 403)
        prev = credentials.get(m.id)
        existing = store.get_mandate(m.id)
        if existing:
            if prev and prev["vc_sha256"] == c["sha256"]:  # the same credential again: idempotent, mints nothing
                return {**get_mandate(m.id), "countersigned_by": credentials.gateway_did(), "unchanged": True}
            if not prev or prev["buyer"] != m.buyer:
                raise GatewayError("mandate_exists",
                                   f"Mandate {m.id} exists and wasn't signed for this buyer; use another id.", 409)
            if existing.get("revoked"):
                raise GatewayError("mandate_revoked", f"Mandate {m.id} was revoked; sign a new mandate id instead.", 409)
            if c["valid_from"] <= datetime.fromisoformat(prev["valid_from"]):  # no rolling back to an older one
                raise GatewayError("credential_stale", f"Mandate {m.id} already has a credential valid from "
                                   f"{prev['valid_from']}; a replacement needs a later validFrom.", 409)
        if pin:
            credentials.add_buyer_key(m.buyer, did, "tofu", did)
            store.audit("buyer.key_pinned", did, "human", m.buyer, m.buyer, {"did": did, "source": "tofu"})
        data = {**m.model_dump(mode="json"), "issuer": {"did": did, "kind": "buyer"}, "credential_sha256": c["sha256"]}
        jws = signing.sign(data, "sous-mandate+jws")
        credentials.put(m.id, {**c, "token": token})
        store.put_mandate(data, jws)
    store.audit("mandate.updated" if existing else "mandate.issued", m.issued_by, "human", m.id, m.buyer,
                {"mandate": data, "issuer": did, "credential_sha256": c["sha256"], "alg": c["header"]["alg"]})
    out = {"mandate": data, "jws": jws, "issuer": data["issuer"], "credential": token, "revocation": None,
           "countersigned_by": credentials.gateway_did()}
    if mint_agent_credential if mint_agent_credential is not None else agent_auth.required(m):
        out["agent_credential"] = agent_auth.mint(m, m.issued_by)
    return out


def revoke_by_credential(mandate_id: str, revocation: str) -> dict:
    """The buyer revokes their own mandate with a revocation statement signed by the mandate's issuer key."""
    with _order_lock:
        payload = _signed_mandate(mandate_id)
        row = credentials.get(mandate_id) if payload.get("credential_sha256") else None
        if not row:
            raise GatewayError("not_buyer_signed", f"Mandate {mandate_id} was issued by the gateway; "
                               "revoke it at POST /mandates/{id}/revoke.", 409)
        try:
            p = credentials.parse_revocation(revocation, row["issuer"], mandate_id, row["vc_sha256"])
        except credentials.CredentialError as e:
            raise _cred_error(e) from None
        already = bool(payload.get("revoked"))
        data = payload if already else _revoke(payload)
        if not row["revocation_jws"]:
            credentials.set_revocation(mandate_id, revocation)
    if not already:
        store.audit("mandate.revoked", row["issuer"], "human", mandate_id, data["buyer"],
                    {"by": "buyer credential", "issuer": row["issuer"], "reason": p.get("reason")})
    return {"mandate": data, "revocation": revocation, "already_revoked": already}


def verify_mandate(mandate_id: str) -> dict:
    """Both signature checks, the issuer and the validity window, as a report (for auditors and judges)."""
    m = store.get_mandate(mandate_id)
    if not m:
        raise GatewayError("mandate_not_found", f"No mandate {mandate_id}.", 404)
    jws = m.pop("_jws")
    now = _real_now()
    payload = signing.verify(jws, "sous-mandate+jws")
    gw_ok = payload is not None and payload.get("id") == mandate_id
    p = payload if gw_ok else m
    out: dict = {
        "mandate_id": mandate_id,
        "issuer": p.get("issuer") or {"did": credentials.gateway_did(), "kind": "gateway"},
        "gateway_signature": {"ok": gw_ok, "typ": "sous-mandate+jws", "alg": "EdDSA", "kid": signing.kid(),
                              "did": credentials.gateway_did(), "jwks": "/.well-known/jwks.json"},
    }
    valid_from, valid_until = None, p.get("valid_until")
    if p.get("credential_sha256"):
        row = credentials.get(mandate_id)
        bs: dict = {"ok": False, "typ": credentials.VC_TYP, "countersigned_sha256": p["credential_sha256"]}
        try:
            if not row:
                raise credentials.CredentialError("credential_missing", "The credential is not stored.")
            c = credentials.parse_vc(row["vc_jwt"])
            bs.update(alg=c["header"]["alg"], kid=c["header"]["kid"], issuer=c["issuer"], sha256=c["sha256"],
                      matches_countersignature=c["sha256"] == p["credential_sha256"],
                      mandate_matches_credential=gw_ok and _comparable(c["mandate"]) == _comparable(Mandate(**p)))
            bs["ok"] = bs["matches_countersignature"] and bs["mandate_matches_credential"]
            valid_from = c["valid_from"].isoformat()
            valid_until = c["mandate"].valid_until.isoformat() if c["mandate"].valid_until else None
            key = credentials.key_status(c["mandate"].buyer, c["issuer"])
            out["issuer_authorised"] = {
                "ok": key is not None, "buyer": c["mandate"].buyer, "since": key["created_at"] if key else None,
                "how": None if key is None else
                {"tofu": "pinned on first use", "approver": "registered by an approver"}.get(key["source"])}
        except credentials.CredentialError as e:
            bs["error"] = e.message
        out["buyer_signature"] = bs
        out["credential"] = row["vc_jwt"] if row else None
        if row and row["revocation_jws"]:
            try:
                rp = credentials.parse_revocation(row["revocation_jws"], row["issuer"], mandate_id, row["vc_sha256"])
                out["revocation"] = {"ok": True, "statement": row["revocation_jws"], "iat": rp.get("iat"),
                                     "reason": rp.get("reason")}
            except credentials.CredentialError as e:
                out["revocation"] = {"ok": False, "statement": row["revocation_jws"], "error": e.message}
    else:
        out["buyer_signature"] = None
        out["note"] = ("Issued through the gateway by an approver: the gateway signed on the buyer's behalf. "
                       "Buyer-signed mandates: POST /mandates/credential.")
    vf = datetime.fromisoformat(valid_from) if valid_from else None
    vu = datetime.fromisoformat(valid_until) if valid_until else None
    if vu is not None and vu.tzinfo is None:
        vu = vu.replace(tzinfo=config.TZ)
    out["validity"] = {"valid_from": valid_from, "valid_until": valid_until, "now": now.isoformat(timespec="seconds"),
                       "in_window": (vf is None or now + credentials.CLOCK_SKEW >= vf) and (vu is None or now <= vu)}
    out["revoked"] = bool(p.get("revoked"))
    sigs_ok = gw_ok and (out["buyer_signature"] is None
                         or (out["buyer_signature"]["ok"] and out.get("issuer_authorised", {}).get("ok", False)))
    out["verdict"] = ("invalid" if not sigs_ok else "revoked" if out["revoked"]
                      else "valid" if out["validity"]["in_window"] else "outside_validity")
    return out


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
    product_ids: list[str] = Field(min_length=1, description="One product, or several candidates for the gateway to choose and split across")
    amount: float = Field(gt=0, description="How much is needed")
    unit: Literal["L", "KG", "SALES_UNIT"] = Field("SALES_UNIT", description="L or KG for volume/weight, SALES_UNIT for packs")
    substitute_for: str | None = Field(None, description="Product id this item replaces, if it is a substitute")
    product_type: str | None = Field(None, description="The kind of product the buyer asked for, e.g. 'milk' or "
                                     "'maito'. Candidates of another type (coffee milk for milk) are never chosen "
                                     "silently. Default: the most common type among the candidates")
    note: str | None = None


RATIONALE_DOC = ("The agent's business reason for this call, in a sentence (e.g. 'weekly breakfast milk for 400 "
                 "guests'). Recorded as stated in the signed quote, the order and the audit log; never used by the rules.")


def clean_rationale(text: str | None) -> str | None:
    """One line, at most 500 characters, or None."""
    text = " ".join((text or "").split())[:500]
    return text or None


class QuoteRequest(BaseModel):
    agent_id: str
    mandate_id: str | None = None
    delivery_date: date | None = Field(None, description="Required unless option_id is given")
    items: list[QuoteItem] = Field(default_factory=list, max_length=50,
                                   description="Required (at least one) unless option_id is given")
    rationale: str | None = Field(None, max_length=500, description=RATIONALE_DOC)
    option_id: str | None = Field(None, description="Re-quote with one of a previous quote's options[] "
                                  "(e.g. 'Q-1A2B3C4D.O2'): that item's lines become the option's, other items stay")


def order_url(order_id: str, sandbox_id: str | None = None) -> str:
    """Link to the order in the seller UI; sandbox orders keep the visitor inside their own sandbox."""
    q = f"?sandbox={sandbox_id}" if sandbox_id else ""
    return f"{config.PUBLIC_BASE_URL}/ui/{q}#/orders/{order_id}"


def request_quote(req: QuoteRequest, agent_token: str | None = None, approver: bool = False) -> dict:
    """`approver`: the caller proved the global approver code (exact stock under SOUS_STOCK_EXPOSURE=approver_exact)."""
    m = authorize_agent(req.mandate_id, req.agent_id, agent_token, "request_quote") if req.mandate_id else None
    skip: set[int] = set()
    if req.option_id:
        req, skip = _apply_option(req)
    if not req.items or req.delivery_date is None:
        raise GatewayError("invalid_request", "Give delivery_date and at least one item, or an option_id.", 422)
    if sum(len(i.product_ids) for i in req.items) > config.MAX_CANDIDATES_PER_QUOTE:
        raise GatewayError("too_many_candidates",
                           f"At most {config.MAX_CANDIDATES_PER_QUOTE} candidate products per quote; narrow the search first.")
    try:
        with sandbox.metered(m):
            return _request_quote(req, skip, approver)
    except aimo.AimoError as e:
        raise valio_error(e)


def _apply_option(req: QuoteRequest) -> tuple[QuoteRequest, set[int]]:
    """The request with the chosen option's lines in place of its item, from the previous quote's signed payload."""
    qid = req.option_id.rpartition(".")[0]
    row = store.get_quote(qid) if qid else None
    if not row:
        raise GatewayError("option_not_found", f"No quote {qid or req.option_id} for option {req.option_id}.", 404)
    q = signing.verify(row["_jws"], "sous-quote+jws")
    if q is None or q.get("id") != qid:
        raise GatewayError("quote_signature", "Quote signature does not verify.", 409)
    if q.get("agent_id") != req.agent_id or q.get("mandate_id") != req.mandate_id:
        raise GatewayError("quote_mismatch", f"Quote {qid} was issued to agent '{q.get('agent_id')}' under mandate "
                           f"{q.get('mandate_id')}; ask for a quote of your own.", 409)
    if _now() > datetime.fromisoformat(q["expires_at"]):
        raise GatewayError("quote_expired", f"Quote {qid} expired at {q['expires_at']}; ask for a new one.", 409)
    opt = next((o for o in q.get("options") or [] if o["id"] == req.option_id), None)
    if not opt:
        raise GatewayError("option_not_found", f"Quote {qid} has no option {req.option_id}.", 404)
    items, skip = [], set()
    for i, it in enumerate(q["items"]):
        if i != opt["item_index"]:
            items.append(QuoteItem(**it))
            continue
        for l in opt["lines"]:
            skip.add(len(items))
            items.append(QuoteItem(product_ids=[l["product_id"]], amount=l["quantity"], unit="SALES_UNIT",
                                   substitute_for=l.get("substitute_for") or it.get("substitute_for"),
                                   note=f"{opt['id']} ({opt['kind']})"))
    return req.model_copy(update={
        "items": items, "delivery_date": date.fromisoformat(q["requested_delivery_date"]),
        "rationale": req.rationale or q.get("rationale")}), skip


def _request_quote(req: QuoteRequest, skip_options: set[int] = frozenset(), approver: bool = False) -> dict:
    mandate = _mandate(req.mandate_id) if req.mandate_id else None
    before = dict(aimo.stats)
    lines: list[dict] = []
    considered: list[dict] = []
    gaps: list[dict] = []
    troubled: list[tuple[int, QuoteItem, list[dict], list[dict]]] = []
    now = _now()
    if len(req.items) > 1:
        Q.prefetch([pid for i in req.items for pid in i.product_ids], req.delivery_date)
    for item_index, item in enumerate(req.items):
        unit = None if item.unit == "SALES_UNIT" else item.unit
        offers, now = Q.offers(item.product_ids, req.delivery_date, item.amount, unit)
        if not offers:
            gaps.append({"item": item.model_dump(), "item_index": item_index, "uncovered": item.amount, "unit": item.unit,
                         "reason": "No such product ids."})
            continue
        # Sized to known stock, even for one named product: what isn't in stock goes to `uncovered` (and options),
        # never into a line a human could approve for stock that doesn't exist. Only the type asked for is
        # chosen silently (coffee milk is not milk).
        pool, off_type, mismatch = _type_pool(offers, item.product_type)
        chosen, uncovered = Q.plan(item.amount, unit, pool)
        if not chosen and (blocked := [o for o in offers if not o["orderable"]]):
            # Nothing orderable for this need: keep the products that can't be ordered as (unorderable) lines,
            # so the mandate engine checks them and the refusal names the real reasons (allergen, undeliverable,
            # cut-off), not just "nothing orderable". Same as for a single named product: lines, no gap.
            chosen, uncovered = blocked, 0.0
        for l in chosen:
            l["item_index"] = item_index
            if l["product_id"] in mismatch and l["orderable"]:
                l["issues"] = l["issues"] + [{"code": "TYPE_MISMATCH", "severity": "warning",
                                              "message": mismatch[l["product_id"]]}]
            if item.substitute_for:
                l["substitute_for"] = item.substitute_for
            lines.append(l)
        picked = {l["product_id"] for l in chosen}
        considered += [{"product_id": o["product_id"], "name": o["name"], "item_index": item_index,
                        "comparison_price": o["comparison_price"], "orderable": o["orderable"],
                        "why_not": [r for r in [off_type.get(o["product_id"])] if r]
                        + [i["message"] for i in o["issues"] if i["severity"] == "blocking"]
                        or ["More expensive per unit than the chosen option."]}
                       for o in sorted(offers, key=lambda o: (o["product_id"] not in off_type, Q.rank_key(o)))
                       if o["product_id"] not in picked][:6]
        if uncovered > 0:
            gaps.append({"item": item.model_dump(), "item_index": item_index, "uncovered": uncovered, "unit": item.unit})
        if item_index not in skip_options and OPT.trouble(chosen, uncovered, req.delivery_date):
            troubled.append((item_index, item, [o for o in offers if o["product_id"] not in off_type], chosen))

    # Lines that can't be ordered are shown for their reasons but never bought, so they cost nothing.
    total_ex = round(sum(l["line_total_ex_vat"] or 0 for l in lines if l["orderable"]), 2)
    total_inc = round(sum(l["line_total_inc_vat"] or 0 for l in lines if l["orderable"]), 2)
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
        "clock": clock.info(now),
    }
    if rationale := clean_rationale(req.rationale):
        body["rationale"] = rationale
    if req.option_id:
        body["chosen_option"] = req.option_id
    if troubled and (opts := _options(body, troubled, req.delivery_date, mandate, now)):
        body["options"] = opts
    body["valio_api_calls"] = {k: aimo.stats[k] - before[k] for k in aimo.stats}
    if mandate:
        outcome, findings = _evaluate(mandate, req.agent_id, body, now)
        body["mandate_preview"] = {"outcome": outcome, "findings": [f.model_dump() for f in findings]}
    # The stock policy (sous/exposure.py) applies before signing: the JWS, the stored quote and the response are the
    # same view. Everything above (plan, options, mandate preview) used the exact numbers; nothing after needs them.
    body = exposure.view(body, approver)
    jws = signing.sign(body, "sous-quote+jws")
    store.put_quote(body, jws)
    store.audit("quote.issued", req.agent_id, "agent", qid, mandate.buyer if mandate else None,
                {"quote_id": qid, "total_ex_vat": total_ex, "lines": [
                    {"product_id": l["product_id"], "quantity": l["quantity"], "issues": [i["code"] for i in l["issues"]]}
                    for l in lines], "uncovered": gaps, "preview": body.get("mandate_preview", {}).get("outcome"),
                 "valio_api_calls": body["valio_api_calls"], "rationale": body.get("rationale")})
    return {"quote": body, "jws": jws}


def _type_pool(offers: list[dict], hint: str | None) -> tuple[list[dict], dict[str, str], dict[str, str]]:
    """The candidates the gateway may choose from silently: those of the type the buyer asked for (see
    substitutes.type_guard). Returns (pool, {product_id: why its type was passed over}, {product_id: mismatch
    reason}).
    When only other types can be ordered they are quoted, with a reason that makes the mandate ask a human."""
    intended, verdicts = substitutes.type_guard(offers, hint)
    if intended is None:
        return offers, {}, {}
    tiers = ("ok", "related", "unconfirmed", "different")  # closest kind first
    by = {v: [o for o in offers if verdicts[o["product_id"]][0] == v] for v in tiers}
    for v in tiers:
        if any(o["orderable"] for o in by[v]):
            pool = by[v]
            break
    else:
        return offers, {}, {}
    ids = {o["product_id"] for o in pool}
    off = {o["product_id"]: verdicts[o["product_id"]][1] for o in offers
           if o["product_id"] not in ids and verdicts[o["product_id"]][0] != "ok"}
    if v == "ok":
        return pool, off, {}
    label = intended["label"]
    if v == "unconfirmed":  # nothing says it is another kind; a human just confirms what it is
        return pool, off, {pid: f"Asked for {label}; this product's name does not confirm it is {label} (type "
                                f"unconfirmed, not a different type). A human must confirm it." for pid in ids}
    return pool, off, {pid: f"Asked for {label}, but no {label} among the candidates can be delivered, so this "
                            f"line is the closest orderable product: {verdicts[pid][1]}. A human must confirm it."
                       for pid in ids}


MAX_SUBSTITUTE_LOOKUPS = 3  # items per quote whose options look up substitutes (2 batched Valio calls each)


def _options(body: dict, troubled: list, requested: date, mandate: Mandate | None, now: datetime) -> list[dict]:
    """Response options for every item that can't be covered as asked (sous/options.py)."""
    out, lookups = [], 0
    for item_index, item, offers, chosen in troubled:
        unit = None if item.unit == "SALES_UNIT" else item.unit
        opts = OPT.build(item_index, item.amount, unit, offers, chosen, requested)
        original = item.substitute_for or item.product_ids[0]
        if not any(o["on_time"] for o in opts) and lookups < MAX_SUBSTITUTE_LOOKUPS and catalog.get(original):
            lookups += 1
            try:
                subs = substitutes.suggest(original, requested, item.amount, unit, mandate, None, 4)["options"]
            except aimo.BudgetLimit:  # our own per-minute budget: a retryable wait, not "no substitutes"
                raise
            except (aimo.AimoError, KeyError):
                subs = []
            subs = [s for s in subs if s["product_id"] not in item.product_ids]
            opts = OPT.build(item_index, item.amount, unit, offers, chosen, requested, subs, original)
        out += opts

    def preview(opt: dict):
        q = {**body, "lines": [l for l in body["lines"] if l.get("item_index") != opt["item_index"]] + opt["_lines"],
             "uncovered": [g for g in body["uncovered"] if g["item_index"] != opt["item_index"]]}
        tier, findings = _evaluate(mandate, body["agent_id"], q, now)
        return tier, sorted({f.rule for f in findings if f.outcome != "silent" and f.rule != "mandate.ok"})

    return OPT.finish(out, body["id"], preview if mandate else None)


def _evaluate(mandate: Mandate, agent_id: str, q: dict, now: datetime, extra_findings=None):
    uncovered: dict[str, float] = {}  # per unit: litres and kilos don't add up
    for g in q["uncovered"]:
        u = g.get("unit") or (g.get("item") or {}).get("unit") or "SALES_UNIT"
        uncovered[u] = uncovered.get(u, 0.0) + g["uncovered"]
    subs = {l["product_id"]: l["substitute_for"] for l in q["lines"] if l.get("substitute_for")}
    return evaluate(mandate, agent_id, q["lines"], requested_date=date.fromisoformat(q["requested_delivery_date"]),
                    substitute_for=subs, uncovered=uncovered,
                    spent_today_eur=store.spent_today(mandate.id, now.date().isoformat()), now=now,
                    extra_findings=extra_findings, quantities=usual.profile(q["lines"]),
                    usual_quantities=usual.history(mandate.id, now) if mandate.unusual_quantity_factor else None)


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
                extra_findings=None, agent_token: str | None = None, rationale: str | None = None) -> dict:
    """`extra_findings` (list of mandate.Finding) join the mandate's own; standing.py passes drift findings.
    `agent_token` is the agent's bearer credential, needed when the mandate requires agent authentication.
    `rationale` is the agent's stated reason, kept with the order and in the audit log (see RATIONALE_DOC)."""
    authorize_agent(mandate_id, agent_id, agent_token, "place_order")
    with _order_lock:
        order, mandate = _place_order(agent_id, mandate_id, quote_id, idempotency_key, extra_findings,
                                      clean_rationale(rationale))
    if mandate is not None and order["decision"]["outcome"] in ("notify", "ask"):
        notify.send(f"order.{order['decision']['outcome']}", order, mandate)  # FYI / approval request; never blocks
    return order


def _place_order(agent_id: str, mandate_id: str, quote_id: str, idempotency_key: str | None,
                 extra_findings=None, rationale: str | None = None) -> tuple[dict, Mandate | None]:
    if idempotency_key and (prev := store.order_by_idempotency_key(agent_id, idempotency_key)):
        if prev["mandate_id"] != mandate_id or prev["quote_id"] != quote_id:
            raise GatewayError("idempotency_key_reused",
                               "You already used this idempotency key for a different order; use a fresh key.", 409)
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
        store.audit("order.rejected", agent_id, "agent", quote_id, mandate.buyer,
                    {"reason": "quote expired", "agent_rationale": rationale})
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
        "order_url": order_url(oid, getattr(mandate, "sandbox", None)),
        "approval_url": order_url(oid, getattr(mandate, "sandbox", None)) if status == "pending_approval" else None,
        "idempotency_key": idempotency_key,
        "agent_rationale": rationale or q.get("rationale"),
        "created_at": now.isoformat(timespec="milliseconds"), "updated_at": now.isoformat(timespec="milliseconds"),
        "clock": clock.info(now),
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
                           "You already used this idempotency key for another order; use a fresh key.", 409)
    except Exception:
        store.release_quote(quote_id, oid)
        raise
    store.audit(f"order.{status}", "mandate-engine", "system", oid, mandate.buyer,
                {"order_id": oid, "quote_id": quote_id, "agent_id": agent_id, "outcome": outcome,
                 "total_ex_vat": q["total_ex_vat"], "agent_rationale": order["agent_rationale"],
                 "fired": [f.model_dump() for f in findings if f.outcome != "silent"] or [findings[-1].model_dump()]})
    return order, mandate


def _receipt(order: dict) -> str:
    """Signed receipt of the order's current state (re-issued after a human decision)."""
    return signing.sign({k: order.get(k) for k in ("id", "quote_id", "mandate_id", "status", "total_ex_vat",
                                                   "lines", "decision", "approval", "updated_at", "clock")},
                        "sous-order+jws")


def decide(order_id: str, approver: str, approve: bool, note: str | None = None) -> dict:
    with _order_lock:
        o, mandate = _decide(order_id, approver, approve, note)
    notify.send("order.decided", o, mandate)  # FYI to the agent side
    return o


def refuse_agent_approval(order_id: str, token: str, action: Literal["approve", "reject"]) -> GatewayError:
    """An agent credential was presented to approve or reject an order: only a human decides. Audits the
    attempt (agent, order, action; never the token) and returns the error to raise."""
    who = agent_auth.identify(token)
    o = store.get_order(order_id) or {}
    agent = (who or {}).get("agent_id") or "unidentified agent"
    store.audit("agent.approval_attempt_refused", agent, "agent", order_id, o.get("buyer"),
                {"order_id": order_id, "action": action, "agent_id": (who or {}).get("agent_id"),
                 "credential_mandate_id": (who or {}).get("mandate_id"), "order_mandate_id": o.get("mandate_id"),
                 "credential_last4": (who or {}).get("last4"), "order_status": o.get("status")})
    return GatewayError("agent_cannot_approve",
                        f"Agents can't {action} orders: an agent credential was presented. Order {order_id} waits "
                        "for a human approver (X-Approver-Code or the approval link); this attempt is in the audit log.",
                        403)


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
    # The human may have tightened the mandate since the order was held (lower caps, blocked products,
    # allergen rules): re-run the rules on the signed quote against the mandate as it is now.
    row = store.get_quote(o["quote_id"])
    q = signing.verify(row["_jws"], "sous-quote+jws") if row else None
    if not q:
        return "refused", "The order's signed quote can no longer be verified."
    outcome, findings = _evaluate(mandate, o["agent_id"], q, now)
    if outcome == "block":
        fired = "; ".join(f.message for f in findings if f.outcome == "block")
        return "refused", f"The mandate as it is now blocks this order: {fired}"
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
                     "result_reason": reason, "clock": clock.info(now)}
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
                        avoid_allergens: list[str] | None = None, limit: int = 3,
                        agent_token: str | None = None, reader=None) -> dict:
    from . import access
    if mandate_id:
        access.check_live(reader, mandate_id)  # before the mandate and product lookups: no existence oracle
    mandate = _mandate(mandate_id) if mandate_id else None
    if not access.skips_agent_check(reader, mandate):
        sandbox.authorize(mandate, agent_id, agent_token, "suggest_substitutes")
    before = dict(aimo.stats)
    try:
        with sandbox.metered(mandate):
            r = substitutes.suggest(product_id, delivery_date, amount, None if unit == "SALES_UNIT" else unit,
                                    mandate, avoid_allergens, limit=min(limit, 6))
    except KeyError:
        raise GatewayError("product_not_found", f"No product {product_id}.", 404)
    except aimo.AimoError as e:
        raise valio_error(e)
    r["valio_api_calls"] = {k: aimo.stats[k] - before[k] for k in aimo.stats}
    r = exposure.view(r, exposure.is_approver(reader))  # stock policy (sous/exposure.py), also for the audit copy
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
