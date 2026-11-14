"""Passkey (WebAuthn) approval of held orders, bound to the exact order.

The shared approver code says "someone who knows the code decided"; a passkey says "this registered person, on
their own device, with biometric or PIN, approved exactly this order at this total". Optional: the approver code
keeps working, but a mandate with `approval_method: passkey` can only be approved with a passkey.

Registration (POST /approvers/register/options|verify) is gated by the global approver code or a judge sandbox's
approver token (sbxa_), and enrols a passkey for one scope: a mandate or a buyer. Stored: credential id, public key
(COSE and DER SPKI), sign count, approver name, scope, the sandbox it belongs to, RP ID, who enrolled it.

Approval (POST /orders/{id}/approve/options, then /approve/passkey or /reject/passkey): the WebAuthn challenge is
SHA-256 over a canonical JSON `binding` of the order (order id, quote id, SHA-256 of the signed quote JWS, total,
SHA-256 of the lines, approve_by, action, RP ID, a one-time nonce). The assertion is verified with py_webauthn
(signature, RP ID hash, origin, user presence and verification, sign count), the challenge is used once, and the
binding is recomputed from the order at verify time. The order is then decided through service.decide (every
re-check still applies) with authority "passkey <name> (credential …last8)", and the audit event stores the
binding, public key and raw assertion so anyone can re-verify it offline (sous/passkey_verify.py,
scripts/verify_passkey_approval.py, GET /orders/{id}/approval/verify).

RP ID and origin: SOUS_PASSKEY_RP_ID / SOUS_PASSKEY_ORIGINS, else the host of SOUS_PUBLIC_BASE_URL, else the
request's own host (development). WebAuthn needs HTTPS except on localhost; the sslip.io hosts qualify.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from webauthn import (generate_authentication_options, generate_registration_options, options_to_json,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers import decode_credential_public_key, decoded_public_key_to_cryptography
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse, WebAuthnException
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

from . import config, store
from .passkey_verify import PURPOSE, b64u, challenge_for, lines_hash, sha256_hex, unb64u

CHALLENGE_TTL_S = 300
RP_NAME = "Sous"

_SQLITE = [
    """CREATE TABLE IF NOT EXISTS passkey_credential (
  id_sha256 TEXT PRIMARY KEY, credential_id TEXT NOT NULL, approver TEXT NOT NULL, scope_type TEXT NOT NULL,
  scope TEXT NOT NULL, sandbox_id TEXT, public_key TEXT NOT NULL, spki TEXT NOT NULL, sign_count INTEGER NOT NULL,
  rp_id TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL, last_used_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS passkey_challenge (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, data TEXT NOT NULL, expires_ts REAL NOT NULL,
  used_at TEXT)""",
]
_MARIADB = [
    """CREATE TABLE IF NOT EXISTS passkey_credential (
  id_sha256 CHAR(64) PRIMARY KEY, credential_id TEXT NOT NULL, approver VARCHAR(191) NOT NULL,
  scope_type VARCHAR(16) NOT NULL, scope VARCHAR(191) NOT NULL, sandbox_id VARCHAR(32) NULL, public_key TEXT NOT NULL,
  spki TEXT NOT NULL, sign_count BIGINT NOT NULL, rp_id VARCHAR(255) NOT NULL, created_at VARCHAR(40) NOT NULL,
  created_by VARCHAR(255) NOT NULL, last_used_at VARCHAR(40) NULL, INDEX (scope))""",
    """CREATE TABLE IF NOT EXISTS passkey_challenge (
  id VARCHAR(64) PRIMARY KEY, kind VARCHAR(16) NOT NULL, subject VARCHAR(191) NOT NULL, data TEXT NOT NULL,
  expires_ts DOUBLE NOT NULL, used_at VARCHAR(40) NULL)""",
]

_ready_for = None
_clock = time.time  # patched in tests


def _err(code: str, message: str, status: int):
    from . import service as S
    return S.GatewayError(code, message, status)


def _ensure() -> None:
    global _ready_for
    with store._lock:
        conn = store.db()
        if _ready_for is conn:
            return
        for sql in (_MARIADB if config.DB_URL else _SQLITE):
            store._exec(sql)
        _ready_for = conn


# --- relying party ---------------------------------------------------------------

def rp(request_base: str = "") -> tuple[str, list[str]]:
    """(RP ID, accepted origins). Config first, else the request's own origin (local development)."""
    base = config.PUBLIC_BASE_URL or request_base.rstrip("/")
    rp_id = config.env("PASSKEY_RP_ID") or urlparse(base).hostname or "localhost"
    origins = [o.strip().rstrip("/") for o in (config.env("PASSKEY_ORIGINS") or "").split(",") if o.strip()]
    for o in (base, request_base.rstrip("/")):
        host = urlparse(o).hostname or ""
        if o and o not in origins and (host == rp_id or host.endswith("." + rp_id)):
            origins.append(o)
    return rp_id, origins


# --- challenges ------------------------------------------------------------------

def _put_challenge(kind: str, subject: str, data: dict, challenge: str) -> None:
    _ensure()
    store._exec("DELETE FROM passkey_challenge WHERE expires_ts < ?", (_clock() - 3600,))
    store._exec("INSERT INTO passkey_challenge (id, kind, subject, data, expires_ts) VALUES (?,?,?,?,?)",
                (challenge, kind, subject, json.dumps(data, ensure_ascii=False), _clock() + CHALLENGE_TTL_S))


def _client_challenge(credential: dict) -> str:
    try:
        return json.loads(unb64u(credential["response"]["clientDataJSON"]))["challenge"]
    except (KeyError, TypeError, ValueError) as e:
        raise _err("passkey_invalid", f"The passkey response is malformed ({type(e).__name__}).", 400)


def _take_challenge(challenge: str, kind: str, subject: str | None = None) -> dict:
    """The stored challenge if it is live and unused (not consumed yet; see _consume)."""
    _ensure()
    rows = store._exec("SELECT * FROM passkey_challenge WHERE id = ?", (challenge,))[0]
    row = rows[0] if rows else None
    if not row or row["kind"] != kind or (subject is not None and row["subject"] != subject):
        raise _err("passkey_invalid", "This passkey response answers no challenge the gateway issued for this "
                   "order. Start the approval again.", 400)
    if row["used_at"]:
        raise _err("passkey_invalid", "This passkey response was already used (one approval per challenge).", 409)
    if row["expires_ts"] < _clock():
        raise _err("passkey_invalid", f"The challenge expired ({CHALLENGE_TTL_S // 60} minutes). Start again.", 400)
    return json.loads(row["data"])


def _consume(challenge: str) -> None:
    _, n, _ = store._exec("UPDATE passkey_challenge SET used_at = ? WHERE id = ? AND used_at IS NULL",
                          (store.now_iso(), challenge))
    if n != 1:
        raise _err("passkey_invalid", "This passkey response was already used (one approval per challenge).", 409)


# --- credentials -----------------------------------------------------------------

def _public(row: dict) -> dict:
    return {"credential_id": row["credential_id"], "last8": row["credential_id"][-8:], "approver": row["approver"],
            "scope": {"type": row["scope_type"], "value": row["scope"]}, "sandbox": row["sandbox_id"],
            "rp_id": row["rp_id"], "created_at": row["created_at"], "created_by": row["created_by"],
            "last_used_at": row["last_used_at"]}


def _sandbox_active(sid: str) -> bool:
    from . import sandbox
    row = sandbox._row(sid)
    return bool(row and sandbox._state(row) == "active")


def credentials_for(order: dict) -> list[dict]:
    """Registered passkeys that may decide this order: scope is its mandate or its buyer; a passkey enrolled in a
    judge sandbox only for that sandbox's orders, and only while the sandbox is active."""
    from . import sandbox
    _ensure()
    rows = store._exec("SELECT * FROM passkey_credential WHERE (scope_type = 'mandate' AND scope = ?) "
                       "OR (scope_type = 'buyer' AND scope = ?)", (order["mandate_id"], order.get("buyer") or ""))[0]
    owner = sandbox.owner(order["mandate_id"])
    return [r for r in rows if not r["sandbox_id"] or (r["sandbox_id"] == owner and _sandbox_active(owner))]


def _credential(credential_id: str) -> dict | None:
    _ensure()
    rows = store._exec("SELECT * FROM passkey_credential WHERE id_sha256 = ?", (sha256_hex(credential_id),))[0]
    return rows[0] if rows else None


def register_options(name: str, scope_type: str, scope: str, sandbox_id: str | None, authority: str,
                     rp_id: str) -> dict:
    name = " ".join((name or "").split())[:80]
    if not name:
        raise _err("invalid_argument", "Give the approver's name (who this passkey belongs to).", 400)
    _ensure()
    existing = store._exec("SELECT credential_id FROM passkey_credential WHERE scope_type = ? AND scope = ?",
                           (scope_type, scope))[0]
    user_id = secrets.token_bytes(16)
    opts = generate_registration_options(
        rp_id=rp_id, rp_name=RP_NAME, user_name=f"{name} ({scope})", user_display_name=name, user_id=user_id,
        timeout=CHALLENGE_TTL_S * 1000,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED, user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=unb64u(r["credential_id"])) for r in existing])
    challenge = b64u(opts.challenge)
    _put_challenge("register", scope, {"name": name, "scope_type": scope_type, "scope": scope,
                                       "sandbox_id": sandbox_id, "authority": authority, "rp_id": rp_id}, challenge)
    return {"publicKey": json.loads(options_to_json(opts)), "challenge": challenge,
            "expires_in": CHALLENGE_TTL_S}


def register_verify(credential: dict, origins: list[str]) -> dict:
    challenge = _client_challenge(credential)
    data = _take_challenge(challenge, "register")
    try:
        v = verify_registration_response(credential=credential, expected_challenge=unb64u(challenge),
                                         expected_rp_id=data["rp_id"], expected_origin=origins,
                                         require_user_verification=True)
    except (InvalidRegistrationResponse, WebAuthnException, ValueError, KeyError) as e:
        raise _err("passkey_invalid", f"The passkey registration did not verify: {e}", 400)
    _consume(challenge)
    cid = b64u(v.credential_id)
    if _credential(cid):
        raise _err("passkey_invalid", "This passkey is already registered.", 409)
    pub = decoded_public_key_to_cryptography(decode_credential_public_key(v.credential_public_key))
    spki = b64u(pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))
    row = {"id_sha256": sha256_hex(cid), "credential_id": cid, "approver": data["name"],
           "scope_type": data["scope_type"], "scope": data["scope"], "sandbox_id": data["sandbox_id"],
           "public_key": b64u(v.credential_public_key), "spki": spki, "sign_count": v.sign_count,
           "rp_id": data["rp_id"], "created_at": store.now_iso(), "created_by": data["authority"],
           "last_used_at": None}
    store._exec(f"INSERT INTO passkey_credential ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                tuple(row.values()))
    buyer = data["scope"] if data["scope_type"] == "buyer" else (store.get_mandate(data["scope"]) or {}).get("buyer")
    store.audit("approver.passkey_registered", data["name"], "human", data["scope"], buyer,
                {"credential_id": cid, "scope": {"type": data["scope_type"], "value": data["scope"]},
                 "sandbox": data["sandbox_id"], "rp_id": data["rp_id"], "enrolled_with": data["authority"],
                 "public_key_spki": spki, "aaguid": v.aaguid, "user_verified": v.user_verified})
    return _public(row)


# --- approval ----------------------------------------------------------------------

def binding_fields(order: dict) -> dict:
    """What a passkey approval commits to, recomputed from the stored order and its signed quote."""
    q = store.get_quote(order["quote_id"]) or {}
    return {"purpose": PURPOSE, "order_id": order["id"], "mandate_id": order["mandate_id"],
            "buyer": order.get("buyer"), "quote_id": order["quote_id"],
            "quote_jws_sha256": sha256_hex(q.get("_jws") or ""), "total_ex_vat": order.get("total_ex_vat"),
            "currency": "EUR", "lines_sha256": lines_hash(order.get("lines")), "approve_by": order.get("approve_by")}


def approve_options(order: dict, action: str, rp_id: str) -> dict:
    creds = credentials_for(order)
    if not creds:
        raise _err("passkey_not_registered", f"No passkey is registered for {order['mandate_id']} or buyer "
                   f"{order.get('buyer')}. Register one first (POST /approvers/register/options).", 409)
    binding = {**binding_fields(order), "action": action, "rp_id": rp_id, "nonce": b64u(secrets.token_bytes(16)),
               "issued_at": datetime.now(config.TZ).isoformat(timespec="seconds")}
    challenge = challenge_for(binding)
    opts = generate_authentication_options(
        rp_id=rp_id, challenge=unb64u(challenge), timeout=CHALLENGE_TTL_S * 1000,
        allow_credentials=[PublicKeyCredentialDescriptor(id=unb64u(c["credential_id"])) for c in creds],
        user_verification=UserVerificationRequirement.REQUIRED)
    _put_challenge("approve", order["id"], binding, challenge)
    return {"publicKey": json.loads(options_to_json(opts)), "challenge": challenge, "binding": binding,
            "expires_in": CHALLENGE_TTL_S,
            "approvers": [{"approver": c["approver"], "last8": c["credential_id"][-8:]} for c in creds]}


def verify_approval(order: dict, action: str, credential: dict, origins: list[str]) -> dict:
    """Verify a passkey assertion for deciding `order`; consumes the challenge and advances the sign count.
    Returns the evidence for the audit event (also re-verifiable offline)."""
    challenge = _client_challenge(credential)
    binding = _take_challenge(challenge, "approve", order["id"])
    if binding.get("action") != action:
        raise _err("passkey_invalid", f"This passkey response was made to {binding.get('action')} the order, "
                   f"not to {action} it.", 400)
    now_fields = binding_fields(order)
    changed = [k for k, v in now_fields.items() if binding.get(k) != v]
    if changed:
        raise _err("passkey_invalid", f"The order changed since the challenge was issued ({', '.join(changed)}). "
                   "Start the approval again.", 409)
    cid = credential.get("rawId") or credential.get("id") or ""
    cred = _credential(cid) if isinstance(cid, str) else None
    if not cred or cred["id_sha256"] not in {c["id_sha256"] for c in credentials_for(order)}:
        raise _err("passkey_invalid", "This passkey is not registered for this order's mandate or buyer.", 403)
    if cred["rp_id"] != binding["rp_id"]:
        raise _err("passkey_invalid", "This passkey was registered for another site.", 403)
    try:
        v = verify_authentication_response(
            credential=credential, expected_challenge=unb64u(challenge), expected_rp_id=binding["rp_id"],
            expected_origin=origins, credential_public_key=unb64u(cred["public_key"]),
            credential_current_sign_count=cred["sign_count"], require_user_verification=True)
    except (InvalidAuthenticationResponse, WebAuthnException, ValueError, KeyError) as e:
        raise _err("passkey_invalid", f"The passkey signature did not verify: {e}", 403)
    _consume(challenge)
    store._exec("UPDATE passkey_credential SET sign_count = ?, last_used_at = ? WHERE id_sha256 = ?",
                (v.new_sign_count, store.now_iso(), cred["id_sha256"]))
    r = credential["response"]
    return {"method": "passkey", "approver": cred["approver"], "credential_id": cred["credential_id"],
            "last8": cred["credential_id"][-8:], "rp_id": cred["rp_id"], "public_key_spki": cred["spki"],
            "enrolled_with": cred["created_by"], "binding": binding, "challenge": challenge,
            "sign_count": v.new_sign_count, "user_verified": v.user_verified,
            "assertion": {"authenticatorData": r["authenticatorData"], "clientDataJSON": r["clientDataJSON"],
                          "signature": r["signature"], "userHandle": r.get("userHandle")}}


def authority(evidence: dict) -> str:
    return f"passkey {evidence['approver']} (credential …{evidence['last8']})"


def evidence_for(order_id: str) -> list[dict]:
    """Passkey evidence in the order's decision audit events, oldest first."""
    evs = store.audit_events(order_id, limit=1000)
    return [e["data"]["passkey"] for e in reversed(evs)
            if e["subject"] == order_id and e["event"] in ("order.approved", "order.rejected_by_human") and (e.get("data") or {}).get("passkey")]
