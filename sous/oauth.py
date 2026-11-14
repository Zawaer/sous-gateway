"""OAuth 2.1 for remote MCP connectors (claude.ai / Claude Desktop custom connectors, ChatGPT apps).

Those clients can't send a static `Authorization: Bearer vka_...` header: they speak the MCP authorization spec
(2025-06-18 / 2025-11-25), i.e. RFC 9728 protected-resource metadata, RFC 8414 authorization-server metadata,
RFC 7591 dynamic client registration and the authorization-code grant with S256 PKCE. This module is that, and
nothing more: the "login" is the buyer pasting the agent token they already hold (a judge sandbox's vka_...).

    MCP URL for connectors:  <base>/connect/mcp     (the plain /mcp stays as it was: open, header-authenticated)

- An unauthenticated request to /connect/mcp gets 401 + `WWW-Authenticate: Bearer resource_metadata=...`, which
  is how the client discovers everything below.
- /oauth/authorize shows a small page: who asks (client name, redirect host), a field for the agent token. An
  approver token (sbxa_...) or the approver code is refused there: an assistant never gets the power to approve.
- The issued access token (vko_..., 1 h) and refresh token (vkr_..., 30 days, rotated on every use) are opaque.
  The gateway stores only their SHA-256 hashes; the agent token they stand for is stored AES-GCM-encrypted under
  a key derived from the access/refresh token itself, so the database alone never reveals an agent token.
- At /connect/mcp the gateway swaps the access token for that agent token and serves the ordinary /mcp app, so
  every read scope (access.py), agent-auth check (agent_auth.py), mandate rule and sandbox limit applies exactly
  as with the header. Rotating or revoking the agent credential (or the sandbox expiring) ends the grant too.
- Static-header clients may also call /connect/mcp with the vka_ token itself.

Not implemented (not needed by the clients above): client ID metadata documents, token revocation/introspection
endpoints, DPoP. Claude falls back to DCR when CIMD isn't advertised.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from urllib.parse import urlencode, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import agent_auth, config, sandbox, store

CONNECT_PATH = "/connect/mcp"
SCOPE = "agent"
ACCESS_PREFIX, REFRESH_PREFIX = "vko_", "vkr_"
CODE_TTL, ACCESS_TTL, REFRESH_TTL, CLIENT_TTL = 300, 3600, 30 * 86400, 90 * 86400
DOCS = "https://github.com/Zawaer/sous-gateway/blob/main/docs/CONNECT.md"
_LOOPBACK = {"localhost", "127.0.0.1", "[::1]", "::1"}
_clock = time.time  # patched in tests

_SQLITE = ["""CREATE TABLE IF NOT EXISTS oauth_client (
  client_id TEXT PRIMARY KEY, expires_ts REAL NOT NULL, data TEXT NOT NULL)""",
           """CREATE TABLE IF NOT EXISTS oauth_grant (
  key_sha256 TEXT PRIMARY KEY, kind TEXT NOT NULL, expires_ts REAL NOT NULL, data TEXT NOT NULL)"""]
_MARIADB = ["""CREATE TABLE IF NOT EXISTS oauth_client (
  client_id VARCHAR(64) PRIMARY KEY, expires_ts DOUBLE NOT NULL, data LONGTEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS oauth_grant (
  key_sha256 CHAR(64) PRIMARY KEY, kind VARCHAR(16) NOT NULL, expires_ts DOUBLE NOT NULL, data LONGTEXT NOT NULL,
  INDEX (expires_ts))"""]
_ready_for = None

router = APIRouter(tags=["oauth"])


def _ensure() -> None:
    global _ready_for
    with store._lock:
        conn = store.db()
        if _ready_for is conn:
            return
        for stmt in (_MARIADB if config.DB_URL else _SQLITE):
            store._exec(stmt)
        _ready_for = conn


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _seal(secret: str, agent_token: str) -> dict:
    """The agent token encrypted under a key only the holder of `secret` (code / access / refresh token) has."""
    nonce = os.urandom(12)
    key = hashlib.sha256(b"sous-oauth-seal:" + secret.encode()).digest()
    return {"nonce": _b64(nonce), "sealed": _b64(AESGCM(key).encrypt(nonce, agent_token.encode(), None)),
            "agent_sha256": _sha(agent_token)}  # the same hash agent_credential stores: lets a close revoke grants


def _open(secret: str, d: dict) -> str | None:
    key = hashlib.sha256(b"sous-oauth-seal:" + secret.encode()).digest()
    try:
        return AESGCM(key).decrypt(_unb64(d["nonce"]), _unb64(d["sealed"]), None).decode()
    except Exception:  # noqa: BLE001 - wrong key or corrupted row: no grant
        return None


def _put(secret: str, kind: str, ttl: float, data: dict) -> None:
    store._exec("INSERT INTO oauth_grant (key_sha256, kind, expires_ts, data) VALUES (?, ?, ?, ?)",
                (_sha(secret), kind, _clock() + ttl, json.dumps(data)))


def _take(secret: str | None, kind: str, consume: bool) -> dict | None:
    """The live grant behind `secret`, or None. consume=True deletes it (single use: codes, refresh tokens);
    only the caller whose DELETE removed the row gets it, so a code or refresh token can't be redeemed twice."""
    if not secret:
        return None
    _ensure()
    k = _sha(secret)
    rows = store._exec("SELECT kind, expires_ts, data FROM oauth_grant WHERE key_sha256 = ?", (k,))[0]
    if not rows or rows[0]["kind"] != kind:
        return None
    if consume and store._exec("DELETE FROM oauth_grant WHERE key_sha256 = ?", (k,))[1] != 1:
        return None
    if rows[0]["expires_ts"] < _clock():
        return None
    return json.loads(rows[0]["data"])


def _sweep() -> None:
    now = _clock()
    store._exec("DELETE FROM oauth_grant WHERE expires_ts < ?", (now,))
    store._exec("DELETE FROM oauth_client WHERE expires_ts < ?", (now,))


def base(request: Request) -> str:
    return config.PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


def resource(b: str) -> str:
    return b + CONNECT_PATH


def _mandate_live(mid: str) -> bool:
    """The mandate exists, is not revoked, and if it belongs to a judge sandbox, that sandbox is still active
    (sandbox close keeps the credential rows, so the credential alone doesn't say this)."""
    m = store.get_mandate(mid)
    if not m or m.get("revoked"):
        return False
    if sid := sandbox.owner(mid):
        row = sandbox._row(sid)
        return bool(row) and sandbox._state(row) == "active"
    return True


def _agent_token_live(token: str | None) -> bool:
    """A current agent credential (not rotated or revoked) for at least one live mandate (not revoked, not of an
    expired or deleted sandbox)."""
    if not (token and token.startswith(agent_auth.PREFIX)):
        return False
    who = agent_auth.identify(token)
    return bool(who) and any(_mandate_live(mid) for mid in who["mandate_ids"])


def revoke_for_mandates(mandate_ids, by: str) -> int:
    """Delete every OAuth code/access/refresh grant made with the agent credential of these mandates (called when a
    sandbox closes). Grants carry the SHA-256 of their agent token; returns how many were deleted."""
    _ensure()
    hashes = set()
    for mid in mandate_ids:
        if row := agent_auth._row(mid):
            hashes.add(row["token_sha256"])
    if not hashes:
        return 0
    n = 0
    for r in store._exec("SELECT key_sha256, data FROM oauth_grant")[0]:
        if json.loads(r["data"]).get("agent_sha256") in hashes:
            n += store._exec("DELETE FROM oauth_grant WHERE key_sha256 = ?", (r["key_sha256"],))[1]
    if n:
        store.audit("oauth.revoked", by, "system", sorted(mandate_ids)[0], None,
                    {"mandate_ids": sorted(mandate_ids), "grants": n})
    return n


# --- discovery -------------------------------------------------------------------

def protected_resource_metadata(b: str) -> dict:
    return {"resource": resource(b), "authorization_servers": [b], "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"], "resource_name": "Sous: Valio Aimo agent gateway",
            "resource_documentation": DOCS}


@router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@router.get("/.well-known/oauth-protected-resource" + CONNECT_PATH)
def oauth_protected_resource(request: Request):
    """RFC 9728 protected-resource metadata for the connector MCP endpoint /connect/mcp."""
    return protected_resource_metadata(base(request))


@router.get("/.well-known/oauth-authorization-server")
def oauth_authorization_server(request: Request):
    """RFC 8414 authorization-server metadata: DCR, authorization code with S256 PKCE, refresh tokens."""
    b = base(request)
    return {"issuer": b, "authorization_endpoint": f"{b}/oauth/authorize", "token_endpoint": f"{b}/oauth/token",
            "registration_endpoint": f"{b}/oauth/register", "scopes_supported": [SCOPE],
            "response_types_supported": ["code"], "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "authorization_response_iss_parameter_supported": True, "service_documentation": DOCS}


def challenge(request: Request, error: str | None = None, description: str | None = None) -> Response:
    """401 that starts (or restarts) the OAuth flow (RFC 6750 + RFC 9728 resource_metadata)."""
    b = base(request)
    parts = [f'resource_metadata="{b}/.well-known/oauth-protected-resource{CONNECT_PATH}"', f'scope="{SCOPE}"']
    if error:
        parts += [f'error="{error}"', f'error_description="{description}"']
    body = {"error": error or "unauthorized",
            "message": description or ("This endpoint needs OAuth (or 'Authorization: Bearer <agent token>'). "
                                       f"Setup: {DOCS}")}
    return JSONResponse(body, status_code=401, headers={"WWW-Authenticate": "Bearer " + ", ".join(parts)})


# --- the gate in front of /connect/mcp ----------------------------------------------

async def connect_gate(request: Request, call_next):
    """/connect/mcp: require an OAuth access token (or an agent token), then serve the normal /mcp app with
    `Authorization: Bearer <agent token>`, so every existing check applies unchanged."""
    path = request.scope["path"]
    if path not in (CONNECT_PATH, CONNECT_PATH + "/"):
        return await call_next(request)
    if request.method == "OPTIONS":  # CORS preflight (answered by the CORS middleware outside this one)
        return await call_next(request)
    presented = agent_auth.bearer(request.headers.get("authorization"))
    if not presented:
        return challenge(request)
    if presented.startswith(ACCESS_PREFIX):
        g = _take(presented, "access", consume=False)
        agent_token = _open(presented, g) if g else None
        if not agent_token:
            return challenge(request, "invalid_token", "The access token is invalid or expired.")
    elif presented.startswith(agent_auth.PREFIX):
        agent_token = presented
    else:
        return challenge(request, "invalid_token", "Use an OAuth access token or the agent token (vka_...); "
                                                   "approver tokens are never accepted from an assistant.")
    if not _agent_token_live(agent_token):
        return challenge(request, "invalid_token", "The agent credential behind this token was rotated, revoked "
                                                   "or its sandbox expired. Reconnect with a current agent token.")
    headers = [(k, v) for k, v in request.scope["headers"] if k not in (b"authorization", b"x-approver-code")]
    request.scope["headers"] = headers + [(b"authorization", f"Bearer {agent_token}".encode())]
    request.scope["path"], request.scope["raw_path"] = "/mcp/", b"/mcp/"
    return await call_next(request)


# --- dynamic client registration (RFC 7591) ----------------------------------------------

def _redirect_ok(uri: str) -> bool:
    try:
        u = urlsplit(uri)
    except ValueError:
        return False
    if u.fragment or not u.hostname:
        return False
    if u.scheme == "https":
        return True
    return u.scheme == "http" and u.hostname in _LOOPBACK


def _register_error(desc: str, code: str = "invalid_client_metadata"):
    return JSONResponse({"error": code, "error_description": desc}, status_code=400)


@router.post("/oauth/register")
async def oauth_register(request: Request):
    """RFC 7591 dynamic client registration (open, as MCP clients expect). Redirect URIs must be https, or http on
    a loopback host (any port, RFC 8252)."""
    try:
        meta = await request.json()
    except ValueError:
        return _register_error("Body must be a JSON object.")
    if not isinstance(meta, dict):
        return _register_error("Body must be a JSON object.")
    uris = meta.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > 10 or not all(isinstance(u, str) for u in uris):
        return _register_error("redirect_uris must be a non-empty list of URLs.", "invalid_redirect_uri")
    if bad := [u for u in uris if not _redirect_ok(u)]:
        return _register_error(f"Redirect URI not allowed (https, or http on localhost): {bad[0]}",
                               "invalid_redirect_uri")
    method = meta.get("token_endpoint_auth_method") or "none"
    if method not in ("none", "client_secret_post", "client_secret_basic"):
        method = "none"
    grants = [g for g in (meta.get("grant_types") or ["authorization_code", "refresh_token"])
              if g in ("authorization_code", "refresh_token")] or ["authorization_code"]
    _ensure()
    _sweep()
    now = _clock()
    client_id = "sous-client-" + secrets.token_urlsafe(16)
    out = {"client_id": client_id, "client_id_issued_at": int(now), "redirect_uris": uris,
           "client_name": str(meta.get("client_name") or "")[:100], "token_endpoint_auth_method": method,
           "grant_types": grants, "response_types": ["code"], "scope": SCOPE}
    data = dict(out)
    if method != "none":
        secret = secrets.token_urlsafe(32)
        out |= {"client_secret": secret, "client_secret_expires_at": 0}
        data["secret_sha256"] = _sha(secret)
    store._exec("INSERT INTO oauth_client (client_id, expires_ts, data) VALUES (?, ?, ?)",
                (client_id, now + CLIENT_TTL, json.dumps(data)))
    return JSONResponse(out, status_code=201, headers={"Cache-Control": "no-store"})


def _client(client_id: str | None) -> dict | None:
    if not client_id:
        return None
    _ensure()
    rows = store._exec("SELECT expires_ts, data FROM oauth_client WHERE client_id = ?", (client_id,))[0]
    if not rows or rows[0]["expires_ts"] < _clock():
        return None
    return json.loads(rows[0]["data"])


def _redirect_registered(client: dict, uri: str | None) -> bool:
    """Exact match, except that loopback redirects match on any port (RFC 8252 7.3; Claude Code needs both
    localhost and 127.0.0.1)."""
    if not uri:
        return False
    if uri in client["redirect_uris"]:
        return True
    try:
        u = urlsplit(uri)
    except ValueError:
        return False
    if u.scheme != "http" or u.hostname not in _LOOPBACK:
        return False
    for r in client["redirect_uris"]:
        v = urlsplit(r)
        if v.scheme == "http" and v.hostname == u.hostname and v.path == u.path and v.query == u.query:
            return True
    return False


# --- authorization endpoint ------------------------------------------------------------

_PAGE_HEADERS = {"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'",
                 "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
_FIELDS = ("response_type", "client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method",
           "scope", "resource")


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:2rem auto;padding:0 1rem;color:#1d2433}}
h1{{font-size:1.3rem}} code,input{{font-family:ui-monospace,monospace}} .box{{border:1px solid #d5dae3;
border-radius:8px;padding:1rem;margin:1rem 0}} .warn{{background:#fff6e0;border-color:#f0c36d}}
.err{{background:#fde8e8;border-color:#e99}} input[type=password]{{width:100%;padding:.5rem;font-size:1rem;
box-sizing:border-box}} button{{padding:.5rem 1rem;font-size:1rem;margin:.75rem .5rem 0 0}}</style></head>
<body>{body}</body></html>"""
    return HTMLResponse(doc, status_code=status, headers=_PAGE_HEADERS)


def _redirect(uri: str, request: Request, **params) -> RedirectResponse:
    q = urlencode({k: v for k, v in {**params, "iss": base(request)}.items() if v is not None})
    return RedirectResponse(f"{uri}{'&' if '?' in uri else '?'}{q}", status_code=302,
                            headers={"Cache-Control": "no-store"})


def _check_authorize(p: dict, request: Request) -> tuple[dict | None, Response | None]:
    """(client, None) when the request is valid, else (None, the response to send)."""
    client = _client(p.get("client_id"))
    if client is None:
        return None, _page("Unknown client", "<h1>Unknown client</h1><p>This assistant's registration is unknown "
                           "or expired. Remove the connector and add it again.</p>", 400)
    if not _redirect_registered(client, p.get("redirect_uri")):
        return None, _page("Bad redirect", "<h1>Redirect URI not registered</h1><p>The assistant asked to "
                           "return to an address it did not register. Nothing was shared.</p>", 400)
    err = None
    if p.get("response_type") != "code":
        err = ("unsupported_response_type", "response_type must be code")
    elif not p.get("code_challenge") or p.get("code_challenge_method") != "S256":
        err = ("invalid_request", "PKCE with code_challenge_method=S256 is required")
    elif p.get("resource") and p["resource"].rstrip("/") != resource(base(request)):
        err = ("invalid_target", f"resource must be {resource(base(request))}")
    if err:
        return None, _redirect(p["redirect_uri"], request, error=err[0], error_description=err[1],
                               state=p.get("state"))
    return client, None


def _form(p: dict, client: dict, error: str | None = None) -> HTMLResponse:
    host = urlsplit(p["redirect_uri"]).hostname or "?"
    loopback = host in _LOOPBACK
    name = html.escape(client.get("client_name") or "An MCP client")
    hidden = "".join(f'<input type="hidden" name="{f}" value="{html.escape(p.get(f) or "")}">' for f in _FIELDS)
    warn = ('<div class="box warn">This client returns to <b>a program on this computer</b> '
            f"(<code>{html.escape(host)}</code>). Continue only if you just started the connection from your own "
            "assistant (e.g. Claude Code).</div>") if loopback else ""
    err = f'<div class="box err">{html.escape(error)}</div>' if error else ""
    body = f"""<h1>Connect an assistant to Sous</h1>
<div class="box"><b>{name}</b> (returning to <code>{html.escape(host)}</code>) asks to act as your purchasing
agent at the Valio Aimo gateway: search the catalog, request quotes and place orders under your mandate.</div>
{warn}{err}
<form method="post" action="/oauth/authorize" autocomplete="off">{hidden}
<label for="t">Paste the <b>agent token</b> (<code>vka_...</code>) from your sandbox or mandate:</label>
<input id="t" type="password" name="agent_token" required spellcheck="false">
<div><button name="action" value="allow">Connect</button><button name="action" value="deny" formnovalidate>
Cancel</button></div></form>
<div class="box"><b>No token yet?</b> Create a private demo sandbox (copies of a hotel, a hospital and a school
mandate, 24 h, a few live Valio lookups). You get its approver token on the next page, for you only; the assistant
gets the agent token.
<form method="post" action="/oauth/authorize">{hidden}<button name="action" value="sandbox">Create a demo
sandbox and connect</button></form></div>
<p>What the assistant can't do: approve or reject orders (that stays with your approver token, never paste it
here), change the mandate, or exceed it. Orders above your limits wait for a human. You can cut it off any time by
rotating the agent token. <a href="{DOCS}">More</a></p>"""
    return _page("Connect to Sous", body)


@router.get("/oauth/authorize", include_in_schema=False)
def oauth_authorize(request: Request):
    p = {f: request.query_params.get(f) for f in _FIELDS}
    client, bad = _check_authorize(p, request)
    return bad or _form(p, client)


@router.post("/oauth/authorize", include_in_schema=False)
async def oauth_authorize_submit(request: Request):
    form = await request.form()
    p = {f: (str(form.get(f)) if form.get(f) else None) for f in _FIELDS}
    client, bad = _check_authorize(p, request)
    if bad:
        return bad
    if form.get("action") == "sandbox":
        return _sandbox_and_connect(p, client, request)
    if form.get("action") != "allow":
        return _redirect(p["redirect_uri"], request, error="access_denied", error_description="The user cancelled.",
                         state=p.get("state"))
    token = str(form.get("agent_token") or "").strip()
    if token.startswith(sandbox.APPROVER_PREFIX) or (config.APPROVER_CODE and hmac.compare_digest(
            token.encode(), config.APPROVER_CODE.encode())):
        return _form(p, client, "That is an approver credential. An assistant must never hold it: paste the "
                                "agent token (vka_...) instead.")
    if not _agent_token_live(token):
        return _form(p, client, "That agent token is not valid (wrong, rotated, revoked or its sandbox expired).")
    return _redirect(p["redirect_uri"], request, code=_issue_code(p, client, token, request), state=p.get("state"))


def _issue_code(p: dict, client: dict, token: str, request: Request) -> str:
    """An authorization code for a valid agent token (sealed under the code), audited as oauth.authorized."""
    code = secrets.token_urlsafe(32)
    who = agent_auth.identify(token)
    _ensure()
    _put(code, "code", CODE_TTL, {"client_id": client["client_id"], "redirect_uri": p["redirect_uri"],
                                  "code_challenge": p["code_challenge"], "resource": resource(base(request)),
                                  **_seal(code, token)})
    m = store.get_mandate(who["mandate_ids"][0]) or {}
    store.audit("oauth.authorized", ",".join(who["agent_ids"]), "agent", who["mandate_ids"][0], m.get("buyer"),
                {"client_id": client["client_id"], "client_name": client.get("client_name"),
                 "redirect_host": urlsplit(p["redirect_uri"]).hostname, "mandate_ids": who["mandate_ids"],
                 "last4": who["last4"]})
    return code


def _client_ip(request: Request) -> str:
    """Same rule as api._client_ip: CF-Connecting-IP only when Cloudflare is the only way in."""
    if config.env("TRUST_CF_HEADER") == "1" and (ip := request.headers.get("cf-connecting-ip")):
        return ip
    return request.client.host if request.client else "?"


def _sandbox_and_connect(p: dict, client: dict, request: Request) -> HTMLResponse:
    """'No token yet?': a judge sandbox for this visitor (sandbox.create, same per-IP limit as POST /sandbox), then
    the code flow continues with its agent token. The approver token is shown here to the human, once; it never
    goes to the assistant. The code in the Continue link is valid for CODE_TTL seconds, like any code."""
    from . import service as S
    try:
        sb = sandbox.create(_client_ip(request), base(request) + "/")
    except S.GatewayError as e:
        return _form(p, client, e.message)
    code = _issue_code(p, client, sb["agent_token"], request)
    back = _redirect(p["redirect_uri"], request, code=code, state=p.get("state")).headers["location"]
    sid = html.escape(sb["sandbox"]["id"])
    inbox = html.escape(sb.get("quickstart", {}).get("seller_ui") or f"{base(request)}/ui/?sandbox={sid}#/inbox")
    rules = "".join(f"<li><b>{html.escape(m.get('scenario') or m.get('label') or m['mandate_id'])}</b>: "
                    f"{html.escape(m.get('rules') or m['mandate_id'])}</li>" for m in sb.get("mandates", []))
    name = html.escape(client.get("client_name") or "your assistant")
    body = f"""<h1>Your demo sandbox {sid} is ready</h1>
<div class="box warn"><b>Your approver token, for you only</b> (shown once, keep it): orders above a mandate's limits
wait for a person, and this token approves or rejects them in the <a href="{inbox}" target="_blank" rel="noopener">
approval inbox</a>. Never give it to an assistant.<br><code style="word-break:break-all">{html.escape(sb["approver_token"])}</code></div>
<div class="box">{name} will act as the purchasing agent for these mandates (valid 24 h):<ul>{rules}</ul></div>
<p><a href="{html.escape(back)}"><button type="button">Continue to {name}</button></a></p>
<p>Continue within 5 minutes. Then ask, for example: "Buy 100 litres of lactose-free milk for the hotel for
Monday."</p>"""
    return _page("Sandbox ready", body)


# --- token endpoint ------------------------------------------------------------------------

def _token_error(code: str, desc: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": code, "error_description": desc}, status_code=status,
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _client_auth(request: Request, form) -> tuple[dict | None, str | None]:
    """(client, None) or (None, why). Public clients send client_id only; confidential ones their secret."""
    client_id, secret = form.get("client_id"), form.get("client_secret")
    if (h := request.headers.get("authorization", "")).lower().startswith("basic "):
        try:
            cid, _, sec = base64.b64decode(h[6:]).decode().partition(":")
        except ValueError:
            return None, "malformed Basic credentials"
        from urllib.parse import unquote_plus
        client_id, secret = unquote_plus(cid), unquote_plus(sec)
    client = _client(client_id)
    if client is None:
        return None, "unknown client_id"
    if want := client.get("secret_sha256"):
        if not secret or not hmac.compare_digest(want, _sha(str(secret))):
            return None, "client authentication failed"
    return client, None


def _issue(client_id: str, agent_token: str) -> JSONResponse:
    access, refresh = ACCESS_PREFIX + secrets.token_urlsafe(32), REFRESH_PREFIX + secrets.token_urlsafe(32)
    _put(access, "access", ACCESS_TTL, {"client_id": client_id, **_seal(access, agent_token)})
    _put(refresh, "refresh", REFRESH_TTL, {"client_id": client_id, **_seal(refresh, agent_token)})
    return JSONResponse({"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
                         "refresh_token": refresh, "scope": SCOPE},
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.post("/oauth/token", include_in_schema=False)
async def oauth_token(request: Request):
    """Token endpoint (application/x-www-form-urlencoded): authorization_code with PKCE, refresh_token (rotated)."""
    form = await request.form()
    client, why = _client_auth(request, form)
    if client is None:
        return _token_error("invalid_client", why, 401)
    _ensure()
    grant = form.get("grant_type")
    if grant == "authorization_code":
        code = str(form.get("code") or "")
        g = _take(code, "code", consume=True)
        if not g or g["client_id"] != client["client_id"]:
            return _token_error("invalid_grant", "The code is invalid, expired or already used.")
        if form.get("redirect_uri") != g["redirect_uri"]:
            return _token_error("invalid_grant", "redirect_uri does not match the authorization request.")
        verifier = str(form.get("code_verifier") or "")
        if not (43 <= len(verifier) <= 128) or not hmac.compare_digest(
                _b64(hashlib.sha256(verifier.encode()).digest()), g["code_challenge"]):
            return _token_error("invalid_grant", "PKCE verification failed.")
        if (res := form.get("resource")) and str(res).rstrip("/") != g["resource"]:
            return _token_error("invalid_target", f"resource must be {g['resource']}")
        agent_token = _open(code, g)
    elif grant == "refresh_token":
        rt = str(form.get("refresh_token") or "")
        g = _take(rt, "refresh", consume=True)  # rotation: the old refresh token is gone either way
        if not g or g["client_id"] != client["client_id"]:
            return _token_error("invalid_grant", "The refresh token is invalid, expired or already used.")
        agent_token = _open(rt, g)
    else:
        return _token_error("unsupported_grant_type", "Use authorization_code or refresh_token.")
    if not _agent_token_live(agent_token):
        return _token_error("invalid_grant", "The agent credential was rotated, revoked or its sandbox expired.")
    return _issue(client["client_id"], agent_token)
