"""OAuth for remote MCP connectors (sous/oauth.py): discovery, DCR, authorization code + S256 PKCE, refresh
rotation, and /connect/mcp mapping the access token to the agent credential so mandate checks apply unchanged.

Offline: the catalog and Valio's price/availability are replaced (tests/test_agent_auth.py's fixtures).
"""

import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from sous import api, catalog, config, oauth, service as S, store
from tests.test_agent_auth import env, issue  # noqa: F401 - autouse fixture: offline state DB, catalog, offers

CODE = "approver-1234"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
ARGS = {"agent_id": "a", "mandate_id": "M", "delivery_date": "2026-11-16",
        "items": [{"product_ids": ["MILK"], "amount": 10, "unit": "L"}]}
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def c(monkeypatch):
    monkeypatch.setattr(config, "APPROVER_CODE", CODE)  # read scopes enforced, like staging
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(catalog, "_db", lambda: None)
    monkeypatch.setattr(api.mcp.session_manager, "_has_started", False)  # the lifespan runs once per instance
    api._hits.clear()
    with TestClient(api.app) as client:
        yield client
    api._hits.clear()


def pkce():
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def register(c, uris=(CALLBACK,), **kw):
    r = c.post("/oauth/register", json={"redirect_uris": list(uris), "client_name": "Claude", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def authorize(c, client, token, redirect=CALLBACK, challenge=None, action="allow"):
    params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect,
              "state": "st-1", "code_challenge": challenge, "code_challenge_method": "S256", "scope": "agent",
              "resource": "http://testserver/connect/mcp"}
    page = c.get("/oauth/authorize", params=params)
    assert page.status_code == 200 and 'name="agent_token"' in page.text
    assert page.headers["x-frame-options"] == "DENY"
    return c.post("/oauth/authorize", data={**params, "agent_token": token, "action": action},
                  follow_redirects=False)


def code_from(r):
    assert r.status_code == 302, r.text
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["state"] == ["st-1"] and q["iss"] == ["http://testserver"]
    return q["code"][0]


def token(c, client, **form):
    return c.post("/oauth/token", data={"client_id": client["client_id"], **form})


def mcp_quote(c, access, calls=None):
    """initialize over /connect/mcp, then request_quote (or the given [(tool, args)]); returns (response, result
    of the last call)."""
    h = {**ACCEPT, "Authorization": f"Bearer {access}"}

    def rpc(body, sid=None):
        r = c.post("/connect/mcp", headers={**h, **({"Mcp-Session-Id": sid} if sid else {})}, json=body)
        data = [ln[5:].strip() for ln in r.text.splitlines() if ln.startswith("data:")]
        return r, (json.loads(data[-1]) if data else None)

    r, init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    if r.status_code != 200:
        return r, None
    sid = r.headers.get("mcp-session-id")
    rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    out = None
    for i, (name, args) in enumerate(calls or [("request_quote", ARGS)]):
        out = rpc({"jsonrpc": "2.0", "id": 2 + i, "method": "tools/call",
                   "params": {"name": name, "arguments": args}}, sid)[1]
    return r, out


def test_discovery_and_401_challenge(c):
    r = c.post("/connect/mcp", headers={**ACCEPT, "Origin": "https://claude.ai"}, json={})
    assert r.status_code == 401
    www = r.headers["www-authenticate"]
    assert www.startswith("Bearer ") and \
        'resource_metadata="http://testserver/.well-known/oauth-protected-resource/connect/mcp"' in www
    assert "WWW-Authenticate" in r.headers["access-control-expose-headers"]  # browser clients can read it
    pre = c.options("/connect/mcp", headers={"Origin": "https://claude.ai", "Access-Control-Request-Method": "POST",
                                             "Access-Control-Request-Headers": "authorization,mcp-protocol-version"})
    assert pre.status_code == 200 and "authorization" in pre.headers["access-control-allow-headers"].lower()
    prm = c.get("/.well-known/oauth-protected-resource/connect/mcp").json()
    assert prm["resource"] == "http://testserver/connect/mcp" and prm["authorization_servers"] == ["http://testserver"]
    assert c.get("/.well-known/oauth-protected-resource").json() == prm
    asm = c.get("/.well-known/oauth-authorization-server").json()
    assert asm["issuer"] == "http://testserver" and asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["registration_endpoint"] == "http://testserver/oauth/register" and "none" in \
        asm["token_endpoint_auth_methods_supported"]
    # the open /mcp is unchanged: no 401 without credentials
    assert c.post("/mcp", headers=ACCEPT, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    ).status_code == 200


def test_full_code_flow_with_pkce_maps_to_the_agent_credential(c):
    agent = issue(require_agent_auth=True)["agent_credential"]["token"]
    client = register(c)
    assert client["token_endpoint_auth_method"] == "none" and "client_secret" not in client

    # the approver credential and wrong tokens are refused on the consent page; no code is issued
    verifier, challenge = pkce()
    for bad in (CODE, "sbxa_whatever", "vka_wrong"):
        r = authorize(c, client, bad, challenge=challenge)
        assert r.status_code == 200 and ("not valid" in r.text or "approver" in r.text)
    denied = authorize(c, client, agent, challenge=challenge, action="deny")
    assert parse_qs(urlsplit(denied.headers["location"]).query)["error"] == ["access_denied"]

    # a wrong verifier burns the code
    code = code_from(authorize(c, client, agent, challenge=challenge))
    bad = token(c, client, grant_type="authorization_code", code=code, redirect_uri=CALLBACK,
                code_verifier=pkce()[0])
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    assert token(c, client, grant_type="authorization_code", code=code, redirect_uri=CALLBACK,
                 code_verifier=verifier).json()["error"] == "invalid_grant"

    code = code_from(authorize(c, client, agent, challenge=challenge))
    t = token(c, client, grant_type="authorization_code", code=code, redirect_uri=CALLBACK, code_verifier=verifier,
              resource="http://testserver/connect/mcp")
    assert t.status_code == 200 and t.headers["cache-control"] == "no-store", t.text
    t = t.json()
    assert t["token_type"] == "Bearer" and t["access_token"].startswith("vko_") and t["refresh_token"].startswith("vkr_")
    assert token(c, client, grant_type="authorization_code", code=code, redirect_uri=CALLBACK,
                 code_verifier=verifier).json()["error"] == "invalid_grant"  # single use

    # the access token acts as the agent: the mandate's require_agent_auth is satisfied, the quote is made
    r, out = mcp_quote(c, t["access_token"])
    assert r.status_code == 200 and not out["result"].get("isError"), out
    assert json.loads(out["result"]["content"][0]["text"])["quote"]["mandate_id"] == "M"
    # it is not the agent token: REST doesn't take it, and nothing in the database reveals the agent token
    assert c.post("/quotes", json=ARGS, headers={"Authorization": f"Bearer {t['access_token']}"}).status_code == 403
    dump = json.dumps(store._exec("SELECT * FROM oauth_grant")[0]) + json.dumps(store.audit_events(limit=500))
    assert agent not in dump and t["access_token"] not in dump and t["refresh_token"] not in dump
    assert any(e["event"] == "oauth.authorized" for e in store.audit_events(limit=500))

    # refresh rotates; the old refresh token stops working
    t2 = token(c, client, grant_type="refresh_token", refresh_token=t["refresh_token"]).json()
    assert t2["access_token"] != t["access_token"] and t2["refresh_token"] != t["refresh_token"]
    assert token(c, client, grant_type="refresh_token", refresh_token=t["refresh_token"]).json()["error"] == \
        "invalid_grant"
    assert mcp_quote(c, t2["access_token"])[0].status_code == 200

    # rotating the agent credential ends every grant made with the old one
    S.agent_credential("M", "chef", "rotate")
    r, _ = mcp_quote(c, t2["access_token"])
    assert r.status_code == 401 and 'error="invalid_token"' in r.headers["www-authenticate"]
    assert token(c, client, grant_type="refresh_token", refresh_token=t2["refresh_token"]).json()["error"] == \
        "invalid_grant"
    assert mcp_quote(c, "vko_made_up")[0].status_code == 401
    assert mcp_quote(c, "sbxa_nope")[0].status_code == 401


def test_static_agent_token_works_on_connect_too(c):
    agent = issue(require_agent_auth=True)["agent_credential"]["token"]
    r, out = mcp_quote(c, agent)
    assert r.status_code == 200 and not out["result"].get("isError")


def test_registration_and_redirect_rules(c, monkeypatch):
    assert c.post("/oauth/register", json={"redirect_uris": ["http://evil.example/cb"]}).status_code == 400
    assert c.post("/oauth/register", json={"redirect_uris": []}).status_code == 400
    assert c.post("/oauth/register", json={"redirect_uris": ["https://x.example/cb#frag"]}).status_code == 400
    agent = issue(require_agent_auth=True)["agent_credential"]["token"]
    # Claude Code: loopback redirect, any port, localhost or 127.0.0.1
    cc = register(c, ["http://localhost/callback", "http://127.0.0.1/callback"])
    verifier, challenge = pkce()
    code = code_from(authorize(c, cc, agent, redirect="http://localhost:3118/callback", challenge=challenge))
    assert token(c, cc, grant_type="authorization_code", code=code, redirect_uri="http://localhost:3118/callback",
                 code_verifier=verifier).status_code == 200
    page = c.get("/oauth/authorize", params={"response_type": "code", "client_id": cc["client_id"],
                                             "redirect_uri": "http://localhost:9/other", "code_challenge": challenge,
                                             "code_challenge_method": "S256"})
    assert page.status_code == 400  # unregistered path: an error page, never a redirect
    assert c.get("/oauth/authorize", params={"client_id": "nope", "redirect_uri": CALLBACK}).status_code == 400
    # no PKCE: error redirect to the registered URI
    r = c.get("/oauth/authorize", params={"response_type": "code", "client_id": cc["client_id"],
                                          "redirect_uri": "http://127.0.0.1:5000/callback"}, follow_redirects=False)
    assert r.status_code == 302 and "error=invalid_request" in r.headers["location"]
    # a confidential client must authenticate
    conf = register(c, token_endpoint_auth_method="client_secret_post")
    verifier, challenge = pkce()
    code = code_from(authorize(c, conf, agent, challenge=challenge))
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "code_verifier": verifier}
    assert token(c, conf, **form).status_code == 401
    basic = base64.b64encode(f"{conf['client_id']}:{conf['client_secret']}".encode()).decode()
    assert c.post("/oauth/token", data=form, headers={"Authorization": f"Basic {basic}"}).status_code == 200
    # expired codes and access tokens are refused
    monkeypatch.setattr(oauth, "_clock", lambda: 4e9)
    assert token(c, cc, grant_type="authorization_code", code="x", redirect_uri=CALLBACK,
                 code_verifier=verifier).status_code == 401  # the client registration expired too


def test_my_mandates_and_agent_id_derived_from_the_credential(c):
    """claude.ai asked the human for the mandate and agent ids: my_mandates answers it, and agent_id may be left
    out when the credential covers the mandate."""
    from jsonschema.validators import validator_for

    from sous import mcp_server
    agent = issue(require_agent_auth=True)["agent_credential"]["token"]
    issue("OTHER", agent_id="b", require_agent_auth=True)
    client = register(c)
    verifier, challenge = pkce()
    code = code_from(authorize(c, client, agent, challenge=challenge))
    t = token(c, client, grant_type="authorization_code", code=code, redirect_uri=CALLBACK,
              code_verifier=verifier).json()
    _, mine = mcp_quote(c, t["access_token"], [("my_mandates", {})])
    body = mine["result"]["structuredContent"]
    assert [m["mandate_id"] for m in body["mandates"]] == ["M"]  # not OTHER
    m = body["mandates"][0]
    assert m["agent_id"] == "a" and m["buyer"] == "Hotel" and m["active"] and "auto-approve up to" in m["rules"]
    schema = mcp_server.mcp._tool_manager.get_tool("my_mandates").fn_metadata.output_schema
    validator_for(schema)(schema).validate(body)
    no_agent = {k: v for k, v in ARGS.items() if k != "agent_id"}
    _, q = mcp_quote(c, t["access_token"], [("request_quote", no_agent)])
    assert not q["result"].get("isError"), q
    quote = q["result"]["structuredContent"]["quote"]
    assert quote["agent_id"] == "a" and quote["mandate_id"] == "M"
    _, o = mcp_quote(c, t["access_token"], [("place_order", {"mandate_id": "M", "quote_id": quote["id"],
                                                            "idempotency_key": "k1"})])
    assert not o["result"].get("isError") and o["result"]["structuredContent"]["status"] == "ordered", o
    # a mandate the credential doesn't cover: agent_id is not derived
    _, e = mcp_quote(c, t["access_token"], [("request_quote", {**no_agent, "mandate_id": "OTHER"})])
    assert e["result"]["isError"] and e["result"]["structuredContent"]["error"] == "invalid_argument"
    assert "my_mandates" in e["result"]["structuredContent"]["message"]
    # REST: GET /mandates with the agent token lists its own (non-public) mandate, marked
    lst = c.get("/mandates", headers={"Authorization": f"Bearer {agent}"}).json()["mandates"]
    assert [x["id"] for x in lst if x.get("yours")] == ["M"] and "OTHER" not in [x["id"] for x in lst]
    assert not any(x.get("yours") for x in c.get("/mandates").json()["mandates"])


def test_my_mandates_without_a_credential_is_empty_and_says_why(c):
    r = c.post("/mcp/", headers=ACCEPT, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    h = {**ACCEPT, "Mcp-Session-Id": r.headers["mcp-session-id"]}
    c.post("/mcp/", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    r = c.post("/mcp/", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                         "params": {"name": "my_mandates", "arguments": {}}})
    data = json.loads([ln[5:] for ln in r.text.splitlines() if ln.startswith("data:")][-1])
    body = data["result"]["structuredContent"]
    assert body["mandates"] == [] and "agent token" in body["note"]


def test_no_token_yet_creates_a_sandbox_and_connects(c, monkeypatch):
    """'Create a demo sandbox and connect': the human sees the approver token once; the assistant gets a code that
    maps to the sandbox's agent token, never the approver token."""
    import re
    from sous import sandbox
    monkeypatch.setattr(config, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(config, "SANDBOX_PER_IP_HOUR", 1)
    monkeypatch.setattr(sandbox, "_seed_standing", lambda *a, **k: None, raising=False)
    client = register(c)
    verifier, challenge = pkce()
    params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": CALLBACK, "state": "st-1",
              "code_challenge": challenge, "code_challenge_method": "S256", "scope": "agent",
              "resource": "http://testserver/connect/mcp"}
    page = c.get("/oauth/authorize", params=params)
    assert 'value="sandbox"' in page.text
    r = c.post("/oauth/authorize", data={**params, "action": "sandbox"}, follow_redirects=False)
    assert r.status_code == 200, r.text
    approver = re.search(r"(sbxa_[A-Za-z0-9_\-]+)", r.text).group(1)
    link = re.search(r'href="(https://claude\.ai/api/mcp/auth_callback\?[^"]+)"', r.text).group(1).replace("&amp;", "&")
    q = parse_qs(urlsplit(link).query)
    assert q["state"] == ["st-1"] and approver not in link
    t = token(c, client, grant_type="authorization_code", code=q["code"][0], redirect_uri=CALLBACK,
              code_verifier=verifier).json()
    assert t["access_token"].startswith(oauth.ACCESS_PREFIX)
    r2, out = mcp_quote(c, t["access_token"], [("my_mandates", {})])
    mandates = out["result"]["structuredContent"]["mandates"]
    assert r2.status_code == 200 and len(mandates) == 3 and all(m["sandbox"] for m in mandates)
    # the per-IP sandbox limit shows on the consent page instead of an error page
    again = c.post("/oauth/authorize", data={**params, "action": "sandbox"}, follow_redirects=False)
    assert again.status_code == 200 and "sandboxes in the last hour" in again.text and 'name="agent_token"' in again.text
