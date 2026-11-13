"""Agent credentials: proof that a call really comes from the agent a mandate was issued to.

`agent_id` alone is a name anyone can type (GET /mandates lists them). When a mandate requires agent
authentication (its `require_agent_auth`, or SOUS_REQUIRE_AGENT_AUTH=1 for every mandate), the calls
that spend money (request_quote with a mandate, place_order, renew_standing_order) must carry
`Authorization: Bearer <token>`: the secret the gateway minted for that mandate's agent.

The token is shown once, when it is minted. Only its SHA-256 hash is stored (with created_at and the
last 4 characters, for humans to tell credentials apart), in a table created lazily like
notifications_store.py's. Audit events never contain token values.

Production path: mTLS client certificates, DPoP-bound OAuth tokens, or an agent key bound to the mandate
inside a W3C Verifiable Credential, so the agent proves possession of a private key, not a shared secret.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from . import config, store
from .mandate import Mandate

PREFIX = "vka_"  # "sous agent": makes leaked tokens easy to grep for and to secret-scan

_SQLITE = """CREATE TABLE IF NOT EXISTS agent_credential (
  mandate_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, token_sha256 TEXT NOT NULL, last4 TEXT NOT NULL,
  created_at TEXT NOT NULL, created_by TEXT NOT NULL)"""
_MARIADB = """CREATE TABLE IF NOT EXISTS agent_credential (
  mandate_id VARCHAR(64) PRIMARY KEY, agent_id VARCHAR(191) NOT NULL, token_sha256 CHAR(64) NOT NULL,
  last4 VARCHAR(4) NOT NULL, created_at VARCHAR(40) NOT NULL, created_by VARCHAR(255) NOT NULL)"""

_ready_for = None  # the connection the table was last ensured on (tests swap connections)


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _ensure() -> None:
    global _ready_for
    with store._lock:
        conn = store.db()
        if _ready_for is conn:
            return
        store._exec(_MARIADB if config.DB_URL else _SQLITE)
        _ready_for = conn


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def required(m: Mandate) -> bool:
    return bool(m.require_agent_auth or config.REQUIRE_AGENT_AUTH)


def bearer(authorization: str | None) -> str | None:
    """The token from an `Authorization: Bearer <token>` header value, or None."""
    if not authorization:
        return None
    scheme, _, token = authorization.strip().partition(" ")
    return token.strip() or None if scheme.lower() == "bearer" else None


def _public(row: dict | None) -> dict | None:
    return None if row is None else {k: row[k] for k in ("agent_id", "last4", "created_at", "created_by")}


def _row(mandate_id: str) -> dict | None:
    _ensure()
    rows = store._exec("SELECT * FROM agent_credential WHERE mandate_id = ?", (mandate_id,))[0]
    return dict(rows[0]) if rows else None


def status(m: Mandate) -> dict:
    """What a human (or the UI) may see: whether auth is required and which credential is active, never the token."""
    return {"mandate_id": m.id, "agent_id": m.agent_id, "required": required(m),
            "required_by": "mandate" if m.require_agent_auth else ("gateway" if config.REQUIRE_AGENT_AUTH else None),
            "credential": _public(_row(m.id))}


def mint(m: Mandate, by: str, *, rotated: bool = False, token: str | None = None) -> dict:
    """A new credential for the mandate's agent, replacing any earlier one. The token is returned only here.
    `token`: reuse a token just minted for another mandate (a judge sandbox gives one token for its mandates)."""
    _ensure()
    token = token or PREFIX + secrets.token_urlsafe(32)
    row = {"mandate_id": m.id, "agent_id": m.agent_id, "token_sha256": _hash(token), "last4": token[-4:],
           "created_at": store.now_iso(), "created_by": by}
    store._upsert("agent_credential", list(row), tuple(row.values()))
    store.audit("agent.credential_rotated" if rotated else "agent.credential_issued", by, "human", m.id, m.buyer,
                {"mandate_id": m.id, "agent_id": m.agent_id, "last4": row["last4"]})
    return {"token": token, "header": f"Authorization: Bearer {token}", "agent_id": m.agent_id,
            "last4": row["last4"], "created_at": row["created_at"],
            "note": "Shown once; the gateway keeps only its SHA-256 hash. Rotate it if it is lost or leaked."}


def revoke(m: Mandate, by: str) -> dict:
    _ensure()
    old = _row(m.id)
    store._exec("DELETE FROM agent_credential WHERE mandate_id = ?", (m.id,))
    store.audit("agent.credential_revoked", by, "human", m.id, m.buyer,
                {"mandate_id": m.id, "agent_id": m.agent_id, "last4": old["last4"] if old else None})
    return status(m)


def check(m: Mandate, agent_id: str, token: str | None, operation: str) -> None:
    """Raise AuthError unless the call may act under mandate `m`. A no-op when the mandate doesn't require auth."""
    if not required(m):
        return
    reason = None
    if not token:
        code, status_, reason = "agent_auth_required", 401, "no token"
        msg = (f"Mandate {m.id} requires agent authentication: send 'Authorization: Bearer <token>' with the "
               f"credential issued to agent '{m.agent_id}'.")
    else:
        row = _row(m.id)
        if row is None:
            reason = "no credential issued"
        elif row["agent_id"] != m.agent_id:
            reason = "credential belongs to a previous agent of this mandate"
        elif not hmac.compare_digest(row["token_sha256"], _hash(token)):
            reason = "token mismatch"
        code, status_ = "agent_auth_invalid", 403
        msg = f"The agent credential is not valid for mandate {m.id}."
    if reason is None:
        return
    store.audit("agent.auth_failed", agent_id, "agent", m.id, m.buyer,
                {"mandate_id": m.id, "operation": operation, "claimed_agent_id": agent_id, "reason": reason})
    raise AuthError(code, msg, status_)
