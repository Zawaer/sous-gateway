"""REST routes for passkey approval (sous/passkey.py), included by sous/api.py.

  POST /approvers/register/options   approver code or sandbox approver token: start enrolling a passkey
  POST /approvers/register/verify    finish it (the browser's navigator.credentials.create() result)
  GET  /orders/{id}/passkeys         who may decide this order with a passkey (scoped read)
  POST /orders/{id}/approve/options  a challenge bound to this exact order (scoped read)
  POST /orders/{id}/approve/passkey  approve with the assertion (no code: the passkey is the authority)
  POST /orders/{id}/reject/passkey   reject with the assertion
  GET  /orders/{id}/approval/verify  re-verify the stored passkey evidence (scoped read)
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from . import access, agent_auth, config, oauth, passkey, sandbox, service as S, store
from .passkey_verify import check_order, verify_evidence
from .remembered import RememberIn

router = APIRouter(tags=["approvals"])


def _base(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def _read_order(order_id: str, r: access.Reader = Depends(access.reader)) -> None:
    access.check_order(r, order_id)


class RegisterOptionsIn(BaseModel):
    name: str = Field(min_length=1, max_length=80, description="The approver's name, as it will appear in the "
                      "order's approval and audit (\"passkey <name> (credential …last8)\")")
    mandate_id: str | None = Field(None, description="Scope: orders under this mandate (give this or buyer)")
    buyer: str | None = Field(None, description="Scope: orders under every mandate of this buyer")


class CredentialIn(BaseModel):
    credential: dict = Field(description="The PublicKeyCredential as JSON (base64url fields), as the browser "
                             "returned it")
    note: str | None = Field(None, max_length=500)
    remember: list[RememberIn] = Field(default_factory=list, max_length=20, description=(
        "Approve only: findings to remember, as in POST /orders/{id}/approve (sous/remembered.py)"))


_AGENT_PREFIXES = (agent_auth.PREFIX, oauth.ACCESS_PREFIX, oauth.REFRESH_PREFIX)


def _enrol_authority(code: str | None, body: RegisterOptionsIn, *presented: str | None
                     ) -> tuple[str, str, str, str | None]:
    """(authority, scope_type, scope, sandbox_id) for enrolling a passkey, or raise. Global approver code: any
    scope; a sandbox's approver token: only its own mandates and buyers; no code configured: open (development).
    Never the order id, its approval_url or an agent credential: an agent credential in either header (`presented`)
    is refused even in open mode, so an agent can't become its own approver device, not even for the first passkey."""
    if bool(body.mandate_id) == bool(body.buyer):
        raise S.GatewayError("invalid_argument", "Give exactly one of mandate_id or buyer.", 400)
    if any(t and t.startswith(_AGENT_PREFIXES) for t in (code, *presented)):
        raise S.GatewayError("agent_cannot_approve", "Agents can't enrol approvers: an agent credential was "
                             "presented.", 403)
    if body.mandate_id:
        m = store.get_mandate(body.mandate_id)
        if not m:
            raise S.GatewayError("mandate_not_found", f"No mandate {body.mandate_id}.", 404)
        scope_type, scope, owner = "mandate", body.mandate_id, sandbox.owner(body.mandate_id)
    else:
        buyers = {}
        for m in store.list_mandates():
            buyers.setdefault(m.get("buyer"), sandbox.owner(m["id"]))
        if body.buyer not in buyers:
            raise S.GatewayError("mandate_not_found", f"No mandate for buyer {body.buyer}.", 404)
        scope_type, scope, owner = "buyer", body.buyer, buyers[body.buyer]
    if config.APPROVER_CODE and access.approver_code_ok(code):
        return "global approver code", scope_type, scope, owner
    if code and code.startswith(sandbox.APPROVER_PREFIX):
        g = access._sandbox_grant(code)
        if g and owner == g[0] and passkey._sandbox_active(g[0]):
            return f"sandbox approver {g[0]}", scope_type, scope, owner
    if not config.APPROVER_CODE:
        return "open (no approver code configured)", scope_type, scope, owner
    raise HTTPException(401, "Enrolling a passkey needs the approver code (X-Approver-Code header) or this "
                             "sandbox's approver token.")


@router.post("/approvers/register/options")
def register_options(body: RegisterOptionsIn, request: Request, x_approver_code: str | None = Header(default=None),
                     authorization: str | None = Header(default=None, include_in_schema=False)):
    """Start enrolling an approver's passkey for a mandate or a buyer. Needs the global approver code, or a judge
    sandbox's approver token for its own mandates. Returns WebAuthn creation options (`publicKey`) for
    navigator.credentials.create(); finish with POST /approvers/register/verify within 5 minutes."""
    code = x_approver_code or agent_auth.bearer(authorization)
    authority, scope_type, scope, sid = _enrol_authority(code, body, x_approver_code,
                                                         agent_auth.bearer(authorization))
    rp_id, _ = passkey.rp(_base(request))
    return passkey.register_options(body.name, scope_type, scope, sid, authority, rp_id)


@router.post("/approvers/register/verify")
def register_verify(body: CredentialIn, request: Request):
    """Finish enrolling: the attestation is verified (challenge from /register/options, used once; RP ID, origin,
    user verification) and the credential id, public key, sign count, name and scope are stored. Audited as
    approver.passkey_registered."""
    return passkey.register_verify(body.credential, passkey.rp(_base(request))[1])


@router.get("/orders/{order_id}/passkeys", dependencies=[Depends(_read_order)])
def order_passkeys(order_id: str):
    """The registered passkeys that may decide this order (name and last 8 characters of the credential id)."""
    o = S.get_order(order_id)
    return {"order_id": order_id, "approval_method": S._mandate(o["mandate_id"]).approval_method,
            "passkeys": [{"approver": c["approver"], "last8": c["credential_id"][-8:],
                          "scope": {"type": c["scope_type"], "value": c["scope"]}, "created_at": c["created_at"]}
                         for c in passkey.credentials_for(o)]}


class OptionsIn(BaseModel):
    action: Literal["approve", "reject"] = "approve"
    remember: list[RememberIn] = Field(default_factory=list, max_length=20, description=(
        "Approve only: the findings this approval will remember (sous/remembered.py). They are part of the signed "
        "binding (binding.remember), so /approve/passkey must send exactly the same list."))


def _pending(order_id: str) -> dict:
    o = S.get_order(order_id)  # expires it first when past approve_by
    if o["status"] == "expired_cutoff":
        raise S.GatewayError("approval_deadline_passed", f"Order {order_id} could only be decided until "
                             f"{o.get('approve_by')}; it is expired_cutoff and nothing was ordered.", 409)
    if o["status"] != "pending_approval":
        raise S.GatewayError("not_pending", f"Order {order_id} is {o['status']}, not waiting for approval.", 409)
    return o


@router.post("/orders/{order_id}/approve/options", dependencies=[Depends(_read_order)])
def approve_options(order_id: str, request: Request, body: OptionsIn | None = None):
    """A WebAuthn challenge bound to this exact order: SHA-256 of the canonical JSON `binding` (order id, quote id,
    SHA-256 of the signed quote JWS, total, SHA-256 of the lines, approve_by, action, the `remember` picks, RP ID,
    one-time nonce).
    Returns `publicKey` for navigator.credentials.get(), the `binding` and the approvers who may answer it."""
    o = _pending(order_id)
    body = body or OptionsIn()
    if body.action != "approve" and body.remember:
        raise S.GatewayError("invalid_request", "remember is for approving only.", 422)
    return passkey.approve_options(o, body.action, passkey.rp(_base(request))[0], body.remember)


def _decide_passkey(order_id: str, approve: bool, body: CredentialIn, request: Request) -> dict:
    o = _pending(order_id)
    if not approve and body.remember:
        raise S.GatewayError("invalid_request", "remember is for approving only.", 422)
    ev = passkey.verify_approval(o, "approve" if approve else "reject", body.credential,
                                 passkey.rp(_base(request))[1], body.remember)
    return S.decide(order_id, ev["approver"], approve, body.note, authority=passkey.authority(ev), evidence=ev,
                    remember=(body.remember or None) if approve else None)


@router.post("/orders/{order_id}/approve/passkey")
def approve_passkey(order_id: str, body: CredentialIn, request: Request):
    """Approve with a passkey assertion for a challenge from /approve/options. Decided through the same path as
    the approver code (mandate, cut-offs, holds re-checked); `approval.authority` is "passkey <name> (credential
    …last8)" and the order.approved audit event stores the binding, public key and raw assertion."""
    return _decide_passkey(order_id, True, body, request)


@router.post("/orders/{order_id}/reject/passkey")
def reject_passkey(order_id: str, body: CredentialIn, request: Request):
    """Reject with a passkey assertion for a challenge from /approve/options with action "reject"."""
    return _decide_passkey(order_id, False, body, request)


@router.get("/orders/{order_id}/approval/verify", dependencies=[Depends(_read_order)])
def approval_verify(order_id: str):
    """Re-verify the order's passkey decision from the audit log alone: signature, challenge = hash of binding,
    RP ID, user presence and verification, and that the binding is this order (id, quote and its JWS hash, total,
    lines, approve_by). The same check runs offline: scripts/verify_passkey_approval.py."""
    o = S.get_order(order_id)
    q = store.get_quote(o["quote_id"]) or {}
    out = []
    for ev in passkey.evidence_for(order_id):
        sig, bound = verify_evidence(ev), check_order(ev, o, q.get("_jws"))
        out.append({"approver": ev.get("approver"), "credential_id": ev.get("credential_id"),
                    "action": (ev.get("binding") or {}).get("action"), "ok": sig["ok"] and bound["ok"],
                    "checks": sig["checks"] + bound["checks"]})
    return {"order_id": order_id, "passkey_decisions": out, "ok": bool(out) and all(e["ok"] for e in out)}
