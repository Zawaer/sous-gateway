"""Security headers on every response (sous/api.py SecurityHeaders), per-credential rate limits
(credential_rate_limit), Retry-After on both 429s, and the MCP endpoints still working underneath.

Offline: state DB, catalog and offers replaced (tests/test_agent_auth.py's autouse fixture).
"""

import json

import pytest
from fastapi.testclient import TestClient

from sous import api, catalog, config
from tests.test_agent_auth import env  # noqa: F401 - autouse fixture: offline state DB, catalog, offers

ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
ALWAYS = {"x-content-type-options": "nosniff", "referrer-policy": "no-referrer",
          "strict-transport-security": "max-age=31536000"}


@pytest.fixture
def c(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(catalog, "_db", lambda: None)
    monkeypatch.setattr(api.mcp.session_manager, "_has_started", False)  # the lifespan runs once per instance
    api._hits.clear()
    with TestClient(api.app) as client:
        yield client
    api._hits.clear()


def csp(r) -> dict[str, str]:
    return {d.split(" ", 1)[0]: d.split(" ", 1)[1] if " " in d else ""
            for d in (x.strip() for x in r.headers["content-security-policy"].split(";")) if d}


def test_json_responses_get_the_baseline_headers_and_no_csp(c):
    r = c.get("/health")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    for k, v in ALWAYS.items():
        assert r.headers[k] == v, k
    assert "camera=()" in r.headers["permissions-policy"] and "microphone=()" in r.headers["permissions-policy"]
    assert "content-security-policy" not in r.headers and "x-frame-options" not in r.headers


def test_ui_pages_get_a_csp_that_forbids_framing_and_foreign_connections(c):
    r = c.get("/ui/index.html")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    for k, v in ALWAYS.items():
        assert r.headers[k] == v, k
    p = csp(r)
    assert r.headers["x-frame-options"] == "DENY" and p["frame-ancestors"] == "'none'"
    # a deployed host (testserver is not loopback): ?api= can't send the approver code elsewhere
    assert p["connect-src"] == "'self'"
    assert p["object-src"] == "'none'" and p["base-uri"] == "'none'"
    assert "https://fonts.googleapis.com" in p["style-src"] and "https://fonts.gstatic.com" in p["font-src"]
    assert "unsafe-eval" not in r.headers["content-security-policy"]
    # loopback development: another local gateway port is allowed
    local = csp(c.get("/ui/index.html", headers={"Host": "127.0.0.1:8766"}))
    assert "http://127.0.0.1:*" in local["connect-src"] and "https:" not in local["connect-src"]


def test_page_families_get_their_own_policy(c):
    card = c.get("/ui/mcp-card.html")  # framed by /ui/cards.html (same origin)
    assert csp(card)["frame-ancestors"] == "'self'" and card.headers["x-frame-options"] == "SAMEORIGIN"
    buyer = c.get("/buyer/")  # the reference buyer agent talks to any gateway
    assert "https:" in csp(buyer)["connect-src"] and csp(buyer)["frame-ancestors"] == "'none'"
    docs = c.get("/docs")
    assert "https://cdn.jsdelivr.net" in csp(docs)["script-src"]
    assert "cdn.jsdelivr.net" not in c.get("/ui/index.html").headers["content-security-policy"]
    proof = c.get("/proof")  # an HTML route (not a static file) is covered too
    assert proof.headers["content-type"].startswith("text/html") and csp(proof)["frame-ancestors"] == "'none'"


def test_oauth_consent_pages_keep_their_own_stricter_policy(c):
    r = c.get("/oauth/authorize", params={"client_id": "nobody"})
    assert r.status_code == 400 and r.headers["content-type"].startswith("text/html")
    p = csp(r)
    assert p["default-src"] == "'none'" and p["frame-ancestors"] == "'none'" and "script-src" not in p
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"


def test_per_credential_limit_returns_429_with_retry_after(c, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MIN", 1000)
    monkeypatch.setattr(config, "RATE_LIMIT_PER_CREDENTIAL_MIN", 3)
    stolen = {**ACCEPT, "Authorization": "Bearer vka_stolen-token"}
    for _ in range(3):
        assert c.post("/mcp/", headers=stolen, json=INIT).status_code != 429
    r = c.post("/mcp/", headers=stolen, json=INIT)
    assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= 61
    assert r.json()["error"] == "rate_limited" and r.json()["retry_after_s"] == int(r.headers["retry-after"])
    assert r.headers["x-content-type-options"] == "nosniff"  # headers on the 429 too
    # the bucket is the secret, not the header: the same token as X-Approver-Code on a REST write is also out
    assert c.post("/orders/x/approve", headers={"X-Approver-Code": "vka_stolen-token"}).status_code == 429
    # another credential, and anonymous calls, are unaffected
    assert c.post("/mcp/", headers={**ACCEPT, "Authorization": "Bearer vka_other"}, json=INIT).status_code == 200
    assert c.post("/mcp/", headers=ACCEPT, json=INIT).status_code == 200
    # only a hash is kept, never the raw token
    assert not any("stolen" in k for k in api._hits) and any(k.startswith("cred:") for k in api._hits)


def test_per_ip_429_carries_retry_after(c, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MIN", 2)
    for _ in range(2):
        c.post("/mcp/", headers=ACCEPT, json=INIT)
    r = c.post("/mcp/", headers=ACCEPT, json=INIT)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and r.json()["error"] == "rate_limited"


def test_mcp_streamable_http_still_works_under_the_headers(c):
    r = c.post("/mcp", headers=ACCEPT, json=INIT)
    assert r.status_code == 200 and r.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in r.headers
    h = {**ACCEPT, "Mcp-Session-Id": r.headers["mcp-session-id"]}
    c.post("/mcp/", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    r = c.post("/mcp/", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert r.status_code == 200
    data = json.loads([ln[5:] for ln in r.text.splitlines() if ln.startswith("data:")][-1])
    assert "place_order" in {t["name"] for t in data["result"]["tools"]}
    # the OAuth connector endpoint still challenges, with CORS-readable headers
    r = c.post("/connect/mcp", headers={**ACCEPT, "Origin": "https://claude.ai"}, json=INIT)
    assert r.status_code == 401 and "WWW-Authenticate" in r.headers["access-control-expose-headers"]
    assert "Retry-After" in r.headers["access-control-expose-headers"]
