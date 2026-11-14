"""Re-verify a passkey approval offline: no gateway, no database, only `cryptography`.

A passkey approval's audit event (`order.approved` / `order.rejected_by_human`, field `passkey`) carries
everything needed:

  binding          what the approver's device signed for: order id, quote id, SHA-256 of the quote JWS, total,
                   SHA-256 of the order lines, approve_by, action, the findings to remember
                   (approve and remember), RP ID, a one-time nonce
  challenge        base64url(SHA-256(canonical JSON of binding)): the WebAuthn challenge
  public_key_spki  the approver's passkey public key (DER SubjectPublicKeyInfo, base64url), as registered
  assertion        authenticatorData, clientDataJSON and signature exactly as the browser returned them

The check is the WebAuthn one (https://www.w3.org/TR/webauthn-3/#sctn-verifying-assertion) minus the parts that
need server state (sign count, challenge still unused): the signature over authenticatorData ||
SHA-256(clientDataJSON) verifies with the public key, clientDataJSON is a webauthn.get for this challenge from an
origin on the RP ID, the authenticator saw the user (UP) and verified them (UV), and the challenge is the hash of
the binding. `check_order` then ties the binding to an order as published (GET /orders/{id} or its receipt).

Command line: scripts/verify_passkey_approval.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

PURPOSE = "sous-order-approval"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def canonical(obj) -> bytes:
    """Same canonical JSON as the gateway's JWS (sous/signing.py): sorted keys, no spaces, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_hex(b: bytes | str) -> str:
    return hashlib.sha256(b.encode() if isinstance(b, str) else b).hexdigest()


def challenge_for(binding: dict) -> str:
    return b64u(hashlib.sha256(canonical(binding)).digest())


def lines_hash(lines: list) -> str:
    return sha256_hex(canonical(lines or []))


def _verify_sig(spki_der: bytes, signature: bytes, data: bytes) -> None:
    key = serialization.load_der_public_key(spki_der)
    if isinstance(key, ec.EllipticCurvePublicKey):
        key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
    elif isinstance(key, ed25519.Ed25519PublicKey):
        key.verify(signature, data)
    elif isinstance(key, rsa.RSAPublicKey):
        key.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
    else:
        raise InvalidSignature("unsupported key type")


def verify_evidence(ev: dict) -> dict:
    """{ok, checks: [{check, ok, detail?}]} for one passkey evidence object (the audit event's `passkey`)."""
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str | None = None):
        checks.append({"check": name, "ok": bool(ok), **({"detail": detail} if detail else {})})

    try:
        binding, a = ev["binding"], ev["assertion"]
        auth_data, cdj, sig = unb64u(a["authenticatorData"]), unb64u(a["clientDataJSON"]), unb64u(a["signature"])
        cd = json.loads(cdj)
        rp_id = ev.get("rp_id") or binding.get("rp_id")
    except (KeyError, TypeError, ValueError) as e:
        return {"ok": False, "checks": [{"check": "evidence_complete", "ok": False, "detail": str(e)}]}

    add("binding_purpose", binding.get("purpose") == PURPOSE, binding.get("purpose"))
    add("challenge_is_hash_of_binding", challenge_for(binding) == ev.get("challenge"))
    add("client_data_type", cd.get("type") == "webauthn.get", cd.get("type"))
    add("client_data_challenge", cd.get("challenge") == ev.get("challenge"))
    host = urlparse(cd.get("origin") or "").hostname or ""
    add("origin_on_rp_id", bool(rp_id) and (host == rp_id or host.endswith("." + rp_id)), cd.get("origin"))
    add("rp_id_matches_binding", rp_id == binding.get("rp_id"), rp_id)
    add("rp_id_hash", len(auth_data) >= 37 and auth_data[:32] == hashlib.sha256((rp_id or "").encode()).digest())
    flags = auth_data[32] if len(auth_data) >= 37 else 0
    add("user_present", bool(flags & 0x01))
    add("user_verified", bool(flags & 0x04))
    try:
        _verify_sig(unb64u(ev["public_key_spki"]), sig, auth_data + hashlib.sha256(cdj).digest())
        add("signature", True)
    except (KeyError, ValueError, TypeError, InvalidSignature) as e:
        add("signature", False, type(e).__name__)
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def check_order(ev: dict, order: dict, quote_jws: str | None = None) -> dict:
    """Whether the evidence's binding is this order: id, quote, total, lines, approve_by; and (when the quote's
    JWS is given) that its hash matches. `order` is the order as served (GET /orders/{id})."""
    b = ev.get("binding") or {}
    checks = [
        {"check": "order_id", "ok": b.get("order_id") == order.get("id")},
        {"check": "quote_id", "ok": b.get("quote_id") == order.get("quote_id")},
        {"check": "total_ex_vat", "ok": b.get("total_ex_vat") == order.get("total_ex_vat")},
        {"check": "lines_sha256", "ok": b.get("lines_sha256") == lines_hash(order.get("lines"))},
        {"check": "approve_by", "ok": b.get("approve_by") == order.get("approve_by")},
    ]
    if quote_jws is not None:
        checks.append({"check": "quote_jws_sha256", "ok": b.get("quote_jws_sha256") == sha256_hex(quote_jws)})
    approval = (order.get("approval") or {}).get("passkey") or {}
    if approval:
        checks.append({"check": "receipt_names_this_challenge", "ok": approval.get("challenge") == ev.get("challenge")})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
