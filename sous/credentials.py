"""Buyer-signed mandates: the mandate as a W3C Verifiable Credential (VC Data Model 2.0, secured with JOSE).

The buyer's human signs their own mandate with a key only they hold (a passkey-style P-256 key in the browser,
or a key file); the gateway verifies it, checks the key may speak for that buyer, and countersigns it as before
(sous-mandate+jws). Anyone can then check offline who authorised what.

- Credential: compact JWS, header {"alg": "ES256" | "EdDSA", "typ": "vc+jwt", "cty": "vc", "kid": "<did>#<fragment>"},
  payload = the VC itself (VC-JOSE-COSE): issuer {id: did:key, name: buyer org}, validFrom/validUntil,
  credentialSubject {id: urn:sous:agent:<agent_id>, mandate: {...Mandate fields}}.
- Issuer identifiers are did:key (P-256 multicodec 0x1200, Ed25519 0xed), resolved locally: no network.
- Which keys may speak for a buyer: the buyer_key registry. The first credential for a buyer nobody has
  issued for yet pins its key (trust on first use) unless an approver registered keys via POST /buyers/{id}/keys.
- Revocation: a small JWS (typ sous-revocation+jws) signed by the same key, naming the mandate and the
  credential's SHA-256.

The signing helpers (new_key, sign_vc, sign_revocation) are what scripts/sign_mandate.py and the tests use;
web/sign-mandate.html does the same in WebCrypto.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

from . import config, store

VC_TYP = "vc+jwt"
REVOCATION_TYP = "sous-revocation+jws"
VC_CONTEXT = "https://www.w3.org/ns/credentials/v2"
VC_TYPE = "SousMandateCredential"
AGENT_URN = "urn:sous:agent:"
CLOCK_SKEW = timedelta(minutes=5)


class CredentialError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


# --- base64url, base58btc, did:key ------------------------------------------

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c)  # ValueError on a non-base58 character
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(s) - len(s.lstrip("1"))) + body


# multicodec varint prefixes: p256-pub 0x1200, ed25519-pub 0xed
_P256 = b"\x80\x24"
_ED25519 = b"\xed\x01"


def did_key(public_key) -> str:
    """did:key for a P-256 or Ed25519 public key (multibase base58btc 'z' + multicodec + key bytes)."""
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        if not isinstance(public_key.curve, ec.SECP256R1):
            raise ValueError("only P-256")
        raw = _P256 + public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
    elif isinstance(public_key, Ed25519PublicKey):
        raw = _ED25519 + public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    else:
        raise ValueError("unsupported key type")
    return "did:key:z" + b58encode(raw)


def resolve(did: str):
    """The public key a did:key names (resolved locally). Raises CredentialError for anything else."""
    did = did.split("#", 1)[0]
    if not did.startswith("did:key:z"):
        raise CredentialError("issuer_unsupported", f"Issuer must be a did:key (got {did[:40]!r}).")
    try:
        raw = b58decode(did[len("did:key:z"):])
        if raw[:2] == _P256 and len(raw) == 35:
            return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw[2:])
        if raw[:2] == _ED25519 and len(raw) == 34:
            return Ed25519PublicKey.from_public_bytes(raw[2:])
    except ValueError:
        pass
    raise CredentialError("issuer_unsupported", "Issuer did:key is not a valid P-256 or Ed25519 key.")


def kid_for(did: str) -> str:
    return f"{did}#{did[len('did:key:'):]}"


def gateway_did() -> str:
    from . import signing
    return did_key(signing._key().public_key())


# --- JWS (ES256 / EdDSA) ----------------------------------------------------

def _alg(key) -> str:
    return "ES256" if isinstance(key, (ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey)) else "EdDSA"


def jws_sign(payload: dict, key, typ: str, extra_header: dict | None = None) -> str:
    """Compact JWS signed with a P-256 (ES256, raw r||s) or Ed25519 (EdDSA) private key; kid = the did:key."""
    did = did_key(key.public_key())
    header = {"alg": _alg(key), "typ": typ, "kid": kid_for(did), **(extra_header or {})}
    si = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}." \
         f"{_b64(json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode())}"
    if isinstance(key, ec.EllipticCurvePrivateKey):
        r, s = decode_dss_signature(key.sign(si.encode(), ec.ECDSA(hashes.SHA256())))
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    else:
        sig = key.sign(si.encode())
    return f"{si}.{_b64(sig)}"


def jws_verify(token: str, typ: str) -> tuple[dict, dict]:
    """(header, payload) of a JWS whose kid is a did:key and whose signature verifies with that key."""
    try:
        h, p, s = token.split(".")
        header, payload, sig = json.loads(_unb64(h)), json.loads(_unb64(p)), _unb64(s)
    except (ValueError, AttributeError):
        raise CredentialError("credential_malformed", "Not a compact JWS (header.payload.signature).") from None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise CredentialError("credential_malformed", "JWS header and payload must be JSON objects.")
    if header.get("typ") != typ:
        raise CredentialError("credential_type", f"JWS typ must be {typ!r} (got {header.get('typ')!r}).")
    key = resolve(str(header.get("kid") or ""))
    alg = header.get("alg")
    if alg != _alg(key):
        raise CredentialError("credential_alg", f"alg {alg!r} does not match the key in kid ({_alg(key)} expected).")
    try:
        if alg == "ES256":
            if len(sig) != 64:
                raise InvalidSignature
            der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
            key.verify(der, f"{h}.{p}".encode(), ec.ECDSA(hashes.SHA256()))
        else:
            key.verify(sig, f"{h}.{p}".encode())
    except InvalidSignature:
        raise CredentialError("credential_signature", "The buyer's signature does not verify.", 409) from None
    return header, payload


def sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- the mandate credential -------------------------------------------------

def _dt(s, field: str) -> datetime:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        raise CredentialError("credential_invalid", f"{field} must be an ISO 8601 date-time.") from None
    return d if d.tzinfo else d.replace(tzinfo=config.TZ)


def parse_vc(token: str) -> dict:
    """Verify the signature and shape of a mandate VC (no clock checks: see check_window).

    Returns {"header", "vc", "issuer", "mandate": Mandate, "valid_from", "valid_until", "sha256"}."""
    from .mandate import Mandate

    header, vc = jws_verify(token, VC_TYP)
    issuer = vc.get("issuer")
    issuer_id = issuer.get("id") if isinstance(issuer, dict) else issuer
    if not isinstance(issuer_id, str) or header["kid"].split("#", 1)[0] != issuer_id:
        raise CredentialError("credential_issuer", "The VC's issuer must be the DID of the key that signed it (kid).")
    ctx, types = vc.get("@context") or [], vc.get("type") or []
    if not isinstance(ctx, list) or not ctx or ctx[0] != VC_CONTEXT:
        raise CredentialError("credential_invalid", f"@context must start with {VC_CONTEXT}.")
    if "VerifiableCredential" not in types or VC_TYPE not in types:
        raise CredentialError("credential_invalid", f"type must include VerifiableCredential and {VC_TYPE}.")
    subject = vc.get("credentialSubject")
    if not isinstance(subject, dict) or not isinstance(subject.get("mandate"), dict):
        raise CredentialError("credential_invalid", "credentialSubject.mandate must hold the mandate.")
    data = dict(subject["mandate"])
    if data.pop("revoked", False):
        raise CredentialError("credential_invalid", "A credential can't issue a revoked mandate.")
    valid_from = _dt(vc.get("validFrom"), "validFrom")
    valid_until = _dt(vc["validUntil"], "validUntil") if vc.get("validUntil") else None
    try:
        m = Mandate(**data)
    except Exception as e:  # pydantic.ValidationError
        raise CredentialError("credential_invalid", f"credentialSubject.mandate is not a valid mandate: {e}") from None
    if subject.get("id") != AGENT_URN + m.agent_id:
        raise CredentialError("credential_invalid",
                              f"credentialSubject.id must be {AGENT_URN}{m.agent_id} (the agent holding the mandate).")
    if isinstance(issuer, dict) and issuer.get("name") not in (None, m.buyer):
        raise CredentialError("credential_invalid", "issuer.name must be the mandate's buyer.")
    # The mandate ends no later than the credential.
    if valid_until and (m.valid_until is None or m.valid_until > valid_until):
        m.valid_until = valid_until
    return {"header": header, "vc": vc, "issuer": issuer_id, "mandate": m, "valid_from": valid_from,
            "valid_until": valid_until, "sha256": sha256(token)}


def check_window(c: dict, now: datetime) -> None:
    if now + CLOCK_SKEW < c["valid_from"]:
        raise CredentialError("credential_not_yet_valid", f"The credential is valid from {c['valid_from'].isoformat()}.")
    if c["valid_until"] and now > c["valid_until"]:
        raise CredentialError("credential_expired", f"The credential expired at {c['valid_until'].isoformat()}.")


def parse_revocation(token: str, issuer: str, mandate_id: str, vc_sha256: str) -> dict:
    header, p = jws_verify(token, REVOCATION_TYP)
    if header["kid"].split("#", 1)[0] != issuer:
        raise CredentialError("revocation_issuer", "The revocation must be signed by the mandate's issuer.", 403)
    if p.get("mandate_id") != mandate_id or p.get("credential_sha256") != vc_sha256:
        raise CredentialError("revocation_mismatch",
                              "The revocation must name this mandate_id and its credential_sha256.")
    return p


# --- registries: which keys speak for a buyer, and the stored credentials ----

_SQLITE = [
    """CREATE TABLE IF NOT EXISTS buyer_key (buyer TEXT NOT NULL, did TEXT NOT NULL, source TEXT NOT NULL,
       created_by TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (buyer, did))""",
    """CREATE TABLE IF NOT EXISTS mandate_credential (mandate_id TEXT PRIMARY KEY, buyer TEXT NOT NULL,
       issuer TEXT NOT NULL, vc_jwt TEXT NOT NULL, vc_sha256 TEXT NOT NULL, valid_from TEXT NOT NULL,
       revocation_jws TEXT, created_at TEXT NOT NULL)""",
]
_MARIADB = [
    """CREATE TABLE IF NOT EXISTS buyer_key (buyer VARCHAR(191) NOT NULL, did VARCHAR(255) NOT NULL,
       source VARCHAR(16) NOT NULL, created_by VARCHAR(255) NOT NULL, created_at VARCHAR(40) NOT NULL,
       PRIMARY KEY (buyer, did))""",
    """CREATE TABLE IF NOT EXISTS mandate_credential (mandate_id VARCHAR(64) PRIMARY KEY, buyer VARCHAR(191) NOT NULL,
       issuer VARCHAR(255) NOT NULL, vc_jwt LONGTEXT NOT NULL, vc_sha256 CHAR(64) NOT NULL,
       valid_from VARCHAR(40) NOT NULL, revocation_jws LONGTEXT, created_at VARCHAR(40) NOT NULL)""",
]
_ready_for = None


def _ensure() -> None:
    global _ready_for
    with store._lock:
        conn = store.db()
        if _ready_for is conn:
            return
        for sql in (_MARIADB if config.DB_URL else _SQLITE):
            store._exec(sql)
        _ready_for = conn


def buyer_keys(buyer: str) -> list[dict]:
    _ensure()
    return [dict(r) for r in store._exec("SELECT * FROM buyer_key WHERE buyer = ? ORDER BY created_at", (buyer,))[0]]


def add_buyer_key(buyer: str, did: str, source: str, by: str) -> dict:
    resolve(did)
    _ensure()
    row = {"buyer": buyer, "did": did.split("#", 1)[0], "source": source, "created_by": by, "created_at": store.now_iso()}
    store._upsert("buyer_key", list(row), tuple(row.values()))
    return row


def remove_buyer_key(buyer: str, did: str) -> bool:
    _ensure()
    return bool(store._exec("DELETE FROM buyer_key WHERE buyer = ? AND did = ?", (buyer, did))[1])


def key_status(buyer: str, did: str) -> dict | None:
    """The registry row if `did` may sign for `buyer`, else None."""
    return next((k for k in buyer_keys(buyer) if k["did"] == did), None)


def get(mandate_id: str) -> dict | None:
    _ensure()
    r = store._exec("SELECT * FROM mandate_credential WHERE mandate_id = ?", (mandate_id,))[0]
    return dict(r[0]) if r else None


def put(mandate_id: str, c: dict) -> None:
    _ensure()
    row = {"mandate_id": mandate_id, "buyer": c["mandate"].buyer, "issuer": c["issuer"], "vc_jwt": c["token"],
           "vc_sha256": c["sha256"], "valid_from": c["valid_from"].isoformat(), "revocation_jws": None,
           "created_at": store.now_iso()}
    store._upsert("mandate_credential", list(row), tuple(row.values()))


def set_revocation(mandate_id: str, jws: str) -> None:
    _ensure()
    store._exec("UPDATE mandate_credential SET revocation_jws = ? WHERE mandate_id = ?", (jws, mandate_id))


# --- client side: what the buyer runs ----------------------------------------

def new_key(alg: str = "ES256"):
    return ec.generate_private_key(ec.SECP256R1()) if alg == "ES256" else Ed25519PrivateKey.generate()


def build_vc(mandate: dict, did: str, valid_from: datetime, valid_until: datetime | None = None) -> dict:
    vc = {
        "@context": [VC_CONTEXT],
        "id": f"urn:sous:mandate:{mandate['id']}",
        "type": ["VerifiableCredential", VC_TYPE],
        "issuer": {"id": did, "name": mandate["buyer"]},
        "validFrom": valid_from.isoformat(timespec="seconds"),
        "credentialSubject": {"id": AGENT_URN + mandate["agent_id"], "mandate": mandate},
    }
    if valid_until:
        vc["validUntil"] = valid_until.isoformat(timespec="seconds")
    return vc


def sign_vc(key, mandate: dict, valid_from: datetime | None = None, valid_until: datetime | None = None) -> str:
    """The buyer's side: wrap a mandate (dict of Mandate fields) in a VC and sign it as vc+jwt."""
    vf = valid_from or datetime.now(config.TZ).replace(microsecond=0)
    return jws_sign(build_vc(mandate, did_key(key.public_key()), vf, valid_until), key, VC_TYP, {"cty": "vc"})


def sign_revocation(key, mandate_id: str, vc_jwt: str, reason: str | None = None) -> str:
    payload = {"type": "SousMandateRevocation", "mandate_id": mandate_id, "credential_sha256": sha256(vc_jwt),
               "iat": int(time.time())}
    if reason:
        payload["reason"] = reason
    return jws_sign(payload, key, REVOCATION_TYP)
