"""Ed25519 signatures as compact JWS (RFC 7515, alg EdDSA per RFC 8037).

The gateway signs quotes, mandates and order receipts so a buyer (or an auditor)
can verify them offline against the public key at /.well-known/jwks.json.
"""

from __future__ import annotations

import base64
import hashlib
import json
from functools import lru_cache

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import config

KEY_PATH = config.DATA_DIR / "gateway_ed25519.pem"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@lru_cache(maxsize=1)
def _key() -> Ed25519PrivateKey:
    if KEY_PATH.exists():
        return serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)
    key = Ed25519PrivateKey.generate()
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEY_PATH.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    KEY_PATH.chmod(0o600)
    return key


def _pub_raw() -> bytes:
    return _key().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def kid() -> str:
    return hashlib.sha256(_pub_raw()).hexdigest()[:16]


def jwks() -> dict:
    return {"keys": [{"kty": "OKP", "crv": "Ed25519", "x": _b64(_pub_raw()), "kid": kid(), "alg": "EdDSA", "use": "sig"}]}


def sign(payload: dict, typ: str) -> str:
    header = {"alg": "EdDSA", "kid": kid(), "typ": typ}
    signing_input = f"{_b64(canonical(header))}.{_b64(canonical(payload))}"
    return f"{signing_input}.{_b64(_key().sign(signing_input.encode()))}"


def verify(token: str, typ: str | None = None) -> dict | None:
    """Payload if the signature is ours and valid (and the header typ is `typ`, when given), else None."""
    try:
        h, p, s = token.split(".")
        header = json.loads(_unb64(h))
        if header.get("alg") != "EdDSA" or header.get("kid") != kid():
            return None
        # Tokens signed before the Valtakirja -> Sous rename carry valtakirja-* typ values; still valid.
        if typ is not None and header.get("typ") not in (typ, typ.replace("sous-", "valtakirja-", 1)):
            return None
        _key().public_key().verify(_unb64(s), f"{h}.{p}".encode())
        return json.loads(_unb64(p))
    except (ValueError, TypeError, AttributeError, InvalidSignature):
        return None
