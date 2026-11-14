"""Read access control: who may read which mandates, quotes, orders, standing orders, notifications and audit events.

Writes were always guarded (approver code, agent bearer tokens); reads were open. Now a read is allowed with:

- the global approver code (X-Approver-Code)                        -> everything;
- a judge sandbox's approver token (sbxa_..., as X-Approver-Code or
  Authorization: Bearer)                                            -> that sandbox's mandates and their data;
- an agent bearer token (Authorization: Bearer vka_...)             -> the mandate(s) it was minted for,
                                                                       their quotes, orders, standing orders;
- nothing                                                           -> public demo data only: mandates marked
                                                                       `demo_public` (the curated demo buyers in
                                                                       scripts/seed.py) and everything under them.

Credentials add up (an agent token plus a sandbox token see both). A detail the caller may not read answers 404,
exactly like one that does not exist, so ids are not confirmed. Lists are filtered silently; the response header
X-Sous-Read-Scope says which scope applied. Notifications and audit events are filtered by buyer: a buyer is
visible when every mandate with that buyer name is readable (so a shared name never leaks a private mandate).

Without a configured approver code (SOUS_APPROVER_CODE unset: local dev, most tests) everything is readable,
the same as writes. MCP over stdio (no HTTP request) is the local operator and reads everything too.

Public on purpose: catalog, allergens, /.well-known/*, /schemas/*, /health, /docs, /openapi.json, /audit/verify
(only ok / count / head hash), buyer did:keys, sandbox status. Quotes requested without a mandate are readable by
id (they hold catalog prices only, and anyone may request one).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from fastapi import Request, Response

from . import agent_auth, config, sandbox, store


@dataclass
class Reader:
    everything: bool = False
    mandates: set[str] = field(default_factory=set)  # granted by a credential (agent or sandbox token)
    sandboxes: set[str] = field(default_factory=set)
    kinds: list[str] = field(default_factory=list)

    @property
    def scope(self) -> str:
        return "all" if self.everything else "+".join([*self.kinds, "public_demo"])


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _sandbox_grant(token: str) -> tuple[str, set[str]] | None:
    sandbox._ensure()
    rows = store._exec("SELECT id, data FROM sandbox WHERE approver_sha256 = ?", (_sha(token),))[0]
    if not rows:
        return None
    return rows[0]["id"], set(json.loads(rows[0]["data"])["mandates"].values())


def _agent_grant(token: str) -> set[str]:
    agent_auth._ensure()
    out = set()
    for r in store._exec("SELECT mandate_id, agent_id FROM agent_credential WHERE token_sha256 = ?", (_sha(token),))[0]:
        m = store.get_mandate(r["mandate_id"])
        if m and m.get("agent_id") == r["agent_id"]:  # a credential of a previous agent of the mandate reads nothing
            out.add(r["mandate_id"])
    return out


def from_headers(headers: Mapping | None) -> Reader:
    """The reader behind a request's headers. headers=None means no HTTP request (MCP over stdio): the local operator."""
    if headers is None or not config.APPROVER_CODE:
        return Reader(everything=True, kinds=["open"])
    code = headers.get("x-approver-code") or None
    bearer = agent_auth.bearer(headers.get("authorization"))
    if code and hmac.compare_digest(code.encode(), config.APPROVER_CODE.encode()):
        return Reader(everything=True, kinds=["approver"])
    r = Reader()
    for tok in dict.fromkeys(t for t in (code, bearer) if t):
        if tok.startswith(sandbox.APPROVER_PREFIX):
            if g := _sandbox_grant(tok):
                r.sandboxes.add(g[0])
                r.mandates |= g[1]
                r.kinds.append("sandbox:" + g[0])
        elif mids := _agent_grant(tok):
            r.mandates |= mids
            r.kinds.append("agent")
    return r


def reader(request: Request, response: Response) -> Reader:
    """FastAPI dependency: the caller's read scope (cached on the request), echoed in X-Sous-Read-Scope."""
    if not hasattr(request.state, "reader"):
        request.state.reader = from_headers(request.headers)
    response.headers["X-Sous-Read-Scope"] = request.state.reader.scope
    return request.state.reader


def public_mandate(m: dict | None) -> bool:
    return bool(m and m.get("demo_public") and not m.get("sandbox"))


def can_read_mandate(r: Reader, mandate_id: str | None, m: dict | None = None) -> bool:
    if r.everything:
        return True
    if not mandate_id:
        return False
    if mandate_id in r.mandates:
        return True
    return public_mandate(m if m is not None else store.get_mandate(mandate_id))


def readable_mandates(r: Reader) -> set[str] | None:
    """Every mandate id this reader may see; None = all."""
    if r.everything:
        return None
    return r.mandates | {m["id"] for m in store.list_mandates() if public_mandate(m)}


def readable_buyers(r: Reader) -> set[str] | None:
    """Buyers whose feed and audit events this reader may see; None = all. A buyer counts only if every mandate
    with that name is readable."""
    ids = readable_mandates(r)
    if ids is None:
        return None
    ok, blocked = set(), set()
    for m in store.list_mandates():
        (ok if m["id"] in ids else blocked).add(m["buyer"])
    return ok - blocked


def buyer_where(r: Reader, buyer_col: str = "buyer", subject_col: str | None = None) -> tuple[str, list] | None:
    """SQL condition (" AND ...", args) limiting a feed/audit query to this reader; None = no limit."""
    buyers = readable_buyers(r)
    if buyers is None:
        return None
    parts, args = [], []
    if buyers:
        parts.append(f"COALESCE({buyer_col}, '') IN ({','.join('?' * len(buyers))})")
        args += sorted(buyers)
    if subject_col and r.sandboxes:  # the sandbox's own lifecycle events (no buyer)
        parts.append(f"COALESCE({subject_col}, '') IN ({','.join('?' * len(r.sandboxes))})")
        args += sorted(r.sandboxes)
    return (f" AND ({' OR '.join(parts)})", args) if parts else (" AND 1=0", [])


def and_where(a: tuple[str, list] | None, b: tuple[str, list] | None) -> tuple[str, list] | None:
    if a is None or b is None:
        return a or b
    return a[0] + b[0], list(a[1]) + list(b[1])


# --- checks used by the detail routes (raise the same 404 as a missing record) -------------------------------

def _not_found(code: str, message: str):
    from . import service as S
    return S.GatewayError(code, message, 404)


def check_mandate(r: Reader, mandate_id: str) -> None:
    if not can_read_mandate(r, mandate_id):
        raise _not_found("mandate_not_found", f"No mandate {mandate_id}.")


def check_live(r: Reader | None, mandate_id: str) -> None:
    """Live lookups under a mandate (GET /mandates/{id}/suggestions, GET /products/{id}/substitutes?mandate_id=,
    MCP get_suggestions / suggest_substitutes) are scoped like GET /mandates/{id}. Call it before anything else
    (mandate or product lookup) so the answer never depends on whether the mandate exists:

    - readable (approver code, its agent or sandbox token, or a public demo mandate) -> go on;
    - the caller's credentials grant nothing (none sent, or none recognised)        -> 401 agent_auth_required, for
      any id, existing or not: agents keep the error code they already handle (it was the answer for a sandbox
      mandate before), and the seller UI's code dialog appears on the 401;
    - the credentials grant some scope, but not this mandate                         -> 404 mandate_not_found,
      exactly like an id that doesn't exist.
    r=None: no read scope applies (internal callers)."""
    if r is None or can_read_mandate(r, mandate_id):
        return
    if not r.kinds:
        from . import service as S
        raise S.GatewayError(
            "agent_auth_required",
            f"Lookups under mandate {mandate_id} need a credential: the agent's 'Authorization: Bearer <token>', "
            "the sandbox's approver token, or X-Approver-Code. Public demo mandates need none.", 401)
    raise _not_found("mandate_not_found", f"No mandate {mandate_id}.")


def check_sandbox_list(r: Reader, sandbox_id: str | None) -> None:
    """A list that names ?sandbox=<id> needs a credential that reads that sandbox: the global approver code, the
    sandbox's approver token (sbxa_), or its agent token. Otherwise 401 approver_code_required, the same for an
    unknown, expired or malformed id, so the answer never says whether a sandbox exists. The 401 makes the seller
    UI ask for the code instead of showing an empty inbox. Lists without ?sandbox= are filtered silently."""
    if not sandbox_id or r.everything:
        return
    sid = (sandbox_id or "").strip().upper()
    if sid in r.sandboxes:
        return
    if r.mandates:
        owners = sandbox.index()["mandates"]
        if any(owners.get(m) == sid for m in r.mandates):
            return
    from . import service as S
    raise S.GatewayError(
        "approver_code_required",
        f"Sandbox {sid}'s lists need its approver token (sbxa_..., as X-Approver-Code or Authorization: Bearer), "
        "its agent token, or the approver code.", 401)


def skips_agent_check(r: Reader | None, m) -> bool:
    """True when the reader is the global approver, or holds the approver token of m's sandbox: the seller UI acts
    for the buyer then and needs no agent token for live lookups (sandbox.authorize)."""
    if r is None:
        return False
    return "approver" in r.kinds or bool(getattr(m, "sandbox", None) and m.sandbox in r.sandboxes)


def check_order(r: Reader, order_id: str) -> None:
    if r.everything:
        return
    o = store.get_order(order_id)
    if not o or not can_read_mandate(r, o["mandate_id"]):
        raise _not_found("order_not_found", f"No order {order_id}.")


def check_quote(r: Reader, quote_id: str) -> None:
    if r.everything:
        return
    q = store.get_quote(quote_id)
    if not q or (q.get("mandate_id") and not can_read_mandate(r, q["mandate_id"])):
        raise _not_found("quote_not_found", f"No quote {quote_id}.")


def check_standing(r: Reader, standing_id: str) -> None:
    if r.everything:
        return
    from . import standing_store as SS
    so = SS.get(standing_id)
    if not so or not can_read_mandate(r, so.get("mandate_id")):
        raise _not_found("standing_not_found", f"No standing order {standing_id}.")


# --- the agent's rationale: shown to humans, never trusted -----------------------------------------------------

SUSPICIOUS = ("approved", "authorised", "authorized", "system", "ignore", "override", "instruction", "bypass",
              "disregard", "pre-cleared")
_CLAIM_NOTE = "The agent's own words; not used by any rule."


def agent_claim(text: str | None) -> dict | None:
    """Label free text from an agent for a human reader. `suspicious` flags approval- or instruction-like phrasing
    (a prompt-injection attempt at the approver); it changes nothing in the decision either way."""
    if not text:
        return None
    low = text.lower()
    hits = sorted({w for w in SUSPICIOUS if w in low})
    return {"text": text, "verified": False, "note": _CLAIM_NOTE, "suspicious": bool(hits),
            **({"suspicious_terms": hits} if hits else {})}


def label_order(o: dict) -> dict:
    """An order for display: adds agent_claim next to the stored agent_rationale."""
    if isinstance(o, dict) and "agent_rationale" in o:
        o = {**o, "agent_claim": agent_claim(o.get("agent_rationale"))}
    return o
