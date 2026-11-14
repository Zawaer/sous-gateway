"""Agent authentication: opt-in bearer credentials per mandate, over the service layer, REST, MCP and the seed.

Offline: the catalog and Valio's price/availability (quote.offers) are replaced.
"""

import asyncio
import json
import os
from datetime import date, datetime

import pytest

from sous import agent_auth, catalog, config, quote as Q, service as S, signing, standing, store
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
MON = date(2026, 11, 16)
MILK = {
    "id": "MILK", "name": "Milk 1 l", "names": {"fi": "", "en": "", "sv": ""}, "brand": "", "vendor": "",
    "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
    "allowed_lot_size": 1.0, "temperature": "chilled", "labels": [], "claims": [],
    "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": []},
    "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
    "official_substitutes": [], "deleted": False,
}


def fake_offers(product_ids, requested, need=None, need_unit=None):
    pr = {"catalogPrice": {"value": 1.0, "valueWithTax": 1.14, "validTo": None}, "comparisonPrice": {"value": 1.0},
          "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}
    av = {"logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": False,
          "scheduleLines": [{"salesBlock": False, "availableAmount": 99999, "availableDate": "2026-11-16",
                             "salesUnit": "ST"}]}
    return [Q.build_offer(MILK, pr, av, requested, need, need_unit, NOW) for _ in product_ids], NOW


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(config, "REQUIRE_AGENT_AUTH", False)
    monkeypatch.setattr(config, "APPROVER_CODE", "")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: MILK if pid == "MILK" else None)
    monkeypatch.setattr(Q, "offers", fake_offers)
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(S.notify, "send", lambda *a, **k: None)
    yield
    signing._key.cache_clear()


def issue(mid="M", **kw):
    return S.issue_mandate(Mandate(**{"id": mid, "buyer": "Hotel", "agent_id": "a", "issued_by": "chef",
                                      "auto_approve_limit_eur": 5000, "min_delivery_reliability": None, **kw}))


def req(mid="M", agent="a"):
    return S.QuoteRequest(agent_id=agent, mandate_id=mid, delivery_date=MON,
                          items=[S.QuoteItem(product_ids=["MILK"], amount=10, unit="L")])


def events(name):
    return [e for e in store.audit_events(limit=500) if e["event"] == name]


def code(fn, *a, **k):
    with pytest.raises(S.GatewayError) as e:
        fn(*a, **k)
    return e.value.code, e.value.status


# --- service layer ----------------------------------------------------------

def test_default_mandate_needs_no_token_and_ignores_one():
    r = issue()
    assert "agent_credential" not in r and r["mandate"]["require_agent_auth"] is False
    q = S.request_quote(req())["quote"]
    assert S.place_order("a", "M", q["id"], agent_token="anything")["status"] == "ordered"
    assert not events("agent.auth_failed")


def test_required_mandate_mints_once_and_stores_only_the_hash():
    cred = issue(require_agent_auth=True)["agent_credential"]
    token = cred["token"]
    assert token.startswith("vka_") and len(token) > 40 and cred["header"] == f"Authorization: Bearer {token}"
    [row] = store._exec("SELECT * FROM agent_credential")[0]
    assert token not in json.dumps(dict(row)) and row["last4"] == token[-4:] and row["agent_id"] == "a"
    # never in the audit log, the mandate listing or the status
    dump = json.dumps(store.audit_events(limit=500)) + json.dumps(store.list_mandates()) + \
        json.dumps(S.agent_credential_status("M"))
    assert token not in dump
    [ev] = events("agent.credential_issued")
    assert ev["data"]["last4"] == token[-4:]
    st = S.agent_credential_status("M")
    assert st["required"] is True and st["required_by"] == "mandate" and st["credential"]["last4"] == token[-4:]


def test_required_mandate_checks_quote_order_and_audits_failures():
    token = issue(require_agent_auth=True)["agent_credential"]["token"]
    assert code(S.request_quote, req()) == ("agent_auth_required", 401)
    assert code(S.request_quote, req(), agent_token="vka_wrong") == ("agent_auth_invalid", 403)
    q = S.request_quote(req(), agent_token=token)["quote"]
    assert code(S.place_order, "a", "M", q["id"]) == ("agent_auth_required", 401)
    assert code(S.place_order, "a", "M", q["id"], agent_token=token + "x") == ("agent_auth_invalid", 403)
    assert S.place_order("a", "M", q["id"], agent_token=token)["status"] == "ordered"
    fails = events("agent.auth_failed")
    assert sorted(e["data"]["reason"] for e in fails) == ["no token", "no token", "token mismatch", "token mismatch"]
    assert all(e["subject"] == "M" and e["actor"] == "a" and "vka_" not in json.dumps(e) for e in fails)
    assert {e["data"]["operation"] for e in fails} == {"request_quote", "place_order"}


def test_quote_without_mandate_stays_open_and_search_is_open(monkeypatch):
    issue(require_agent_auth=True)
    monkeypatch.setattr(S.catalog, "search", lambda *a, **k: [])  # CI has no catalog data
    no_mandate = S.QuoteRequest(agent_id="a", delivery_date=MON, items=[S.QuoteItem(product_ids=["MILK"], amount=1)])
    assert S.request_quote(no_mandate)["quote"]["mandate_id"] is None
    assert S.search("a", "milk")["products"] is not None


def test_rotate_and_revoke():
    old = issue(require_agent_auth=True)["agent_credential"]["token"]
    new = S.agent_credential("M", "chef")["agent_credential"]["token"]
    assert new != old and events("agent.credential_rotated")[0]["data"]["last4"] == new[-4:]
    assert code(S.request_quote, req(), agent_token=old) == ("agent_auth_invalid", 403)
    S.request_quote(req(), agent_token=new)
    st = S.agent_credential("M", "chef", "revoke")["status"]
    assert st["credential"] is None and events("agent.credential_revoked")[0]["data"]["last4"] == new[-4:]
    assert code(S.request_quote, req(), agent_token=new) == ("agent_auth_invalid", 403)


def test_reissue_to_another_agent_invalidates_old_credential():
    token = issue(require_agent_auth=True)["agent_credential"]["token"]
    issue(require_agent_auth=True, agent_id="b")  # mints a new one for b
    assert code(S.request_quote, req(agent="a"), agent_token=token) == ("agent_auth_invalid", 403)


def test_global_flag_requires_auth_for_every_mandate(monkeypatch):
    issue()
    monkeypatch.setattr(config, "REQUIRE_AGENT_AUTH", True)
    assert code(S.request_quote, req()) == ("agent_auth_required", 401)
    assert code(S.request_quote, req(), agent_token="vka_x") == ("agent_auth_invalid", 403)  # none issued yet
    token = S.agent_credential("M", "ops")["agent_credential"]["token"]
    S.request_quote(req(), agent_token=token)
    assert S.agent_credential_status("M")["required_by"] == "gateway"


def test_standing_renewal_needs_the_token():
    token = issue(require_agent_auth=True)["agent_credential"]["token"]
    so = standing.create(standing.StandingOrderIn(mandate_id="M", agent_id="a", name="Weekly", items=[
        {"product_ids": ["MILK"], "amount": 10, "unit": "L", "label": "milk"}]))
    assert code(standing.renew, so["id"], MON) == ("agent_auth_required", 401)
    assert standing.renew(so["id"], MON, agent_token=token)["order"]["status"] == "ordered"


def test_bearer_parsing():
    assert agent_auth.bearer("Bearer abc") == "abc" and agent_auth.bearer("bearer  abc ") == "abc"
    assert agent_auth.bearer("Basic abc") is None and agent_auth.bearer(None) is None
    assert agent_auth.bearer("Bearer ") is None


# --- REST -------------------------------------------------------------------

def test_rest_bearer_header_and_approver_only_rotation(monkeypatch):
    from fastapi.testclient import TestClient

    from sous import api
    monkeypatch.setattr(config, "APPROVER_CODE", "1234")
    AC = {"X-Approver-Code": "1234"}
    c = TestClient(api.app)
    m = {"id": "M", "buyer": "Hotel", "agent_id": "a", "issued_by": "chef", "auto_approve_limit_eur": 5000,
         "min_delivery_reliability": None, "require_agent_auth": True}
    r = c.post("/mandates", json=m, headers={"X-Approver-Code": "1234"}).json()
    token = r["agent_credential"]["token"]
    assert token not in c.get("/mandates", headers=AC).text and token not in c.get("/audit", headers=AC).text
    body = {"agent_id": "a", "mandate_id": "M", "delivery_date": "2026-11-16",
            "items": [{"product_ids": ["MILK"], "amount": 10, "unit": "L"}]}
    r = c.post("/quotes", json=body)
    assert r.status_code == 401 and r.json()["error"] == "agent_auth_required"
    assert c.post("/quotes", json=body, headers={"Authorization": "Bearer nope"}).status_code == 403
    q = c.post("/quotes", json=body, headers={"Authorization": f"Bearer {token}"}).json()["quote"]
    o = {"agent_id": "a", "mandate_id": "M", "quote_id": q["id"]}
    assert c.post("/orders", json=o).status_code == 401
    assert c.post("/orders", json=o, headers={"Authorization": f"Bearer {token}"}).json()["status"] == "ordered"

    assert c.post("/mandates/M/agent-credential", json={"by": "x"}).status_code == 401
    r = c.post("/mandates/M/agent-credential", json={"by": "chef"}, headers={"X-Approver-Code": "1234"}).json()
    assert r["agent_credential"]["token"] != token
    assert c.get("/mandates/M/agent-credential").status_code == 404  # not public
    st = c.get("/mandates/M/agent-credential", headers=AC).json()
    assert st["required"] and st["credential"]["last4"] == r["agent_credential"]["last4"] and "token" not in st
    assert "agentToken" in c.get("/openapi.json").json()["components"]["securitySchemes"]


# --- MCP --------------------------------------------------------------------

def test_mcp_in_process_with_agent_token_argument():
    from mcp import Client

    from sous.mcp_server import mcp
    token = issue(require_agent_auth=True)["agent_credential"]["token"]
    args = {"agent_id": "a", "mandate_id": "M", "delivery_date": "2026-11-16",
            "items": [{"product_ids": ["MILK"], "amount": 10, "unit": "L"}]}

    async def go():
        async with Client(mcp) as c:
            denied = await c.call_tool("request_quote", args)
            ok = await c.call_tool("request_quote", {**args, "agent_token": token})
            return denied, ok
    denied, ok = asyncio.run(go())
    assert denied.is_error and denied.structured_content["error"] == "agent_auth_required"
    assert "Authorization: Bearer" in denied.structured_content["hint"]
    assert not ok.is_error and json.loads(ok.content[0].text)["quote"]["mandate_id"] == "M"


def _rpc(c, session, method, params, rid=1, token=None):
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if session:
        h["Mcp-Session-Id"] = session
    if token:
        h["Authorization"] = f"Bearer {token}"
    r = c.post("/mcp/", headers=h, json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    data = [ln[5:].strip() for ln in r.text.splitlines() if ln.startswith("data:")]
    return r, json.loads(data[-1]) if data else (r.json() if r.text.strip() else None)


def test_mcp_streamable_http_reads_the_authorization_header(monkeypatch):
    from fastapi.testclient import TestClient

    from sous import api
    monkeypatch.setattr(catalog, "_db", lambda: None)
    token = issue(require_agent_auth=True)["agent_credential"]["token"]
    args = {"agent_id": "a", "mandate_id": "M", "delivery_date": "2026-11-16",
            "items": [{"product_ids": ["MILK"], "amount": 10, "unit": "L"}]}
    with TestClient(api.app) as c:
        def call(tok):
            r, init = _rpc(c, None, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                   "clientInfo": {"name": "t", "version": "1"}}, token=tok)
            sid = r.headers.get("mcp-session-id")
            h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
            if sid:
                h["Mcp-Session-Id"] = sid
            if tok:
                h["Authorization"] = f"Bearer {tok}"
            c.post("/mcp/", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            return _rpc(c, sid, "tools/call", {"name": "request_quote", "arguments": args}, rid=2, token=tok)[1]
        denied, ok = call(None), call(token)
    assert denied["result"]["isError"] and denied["result"]["structuredContent"]["error"] == "agent_auth_required"
    assert not ok["result"].get("isError")
    assert json.loads(ok["result"]["content"][0]["text"])["quote"]["mandate_id"] == "M"


# --- seed -------------------------------------------------------------------

def test_seed_prints_the_secure_token_once(capsys):
    from scripts import seed
    seed.main()
    out = capsys.readouterr().out
    assert "M-HOTEL-AURORA-SECURE" in out and out.count("Authorization: Bearer vka_") == 1
    token = out.split("Authorization: Bearer ")[1].split()[0]
    for mid in ("M-HOTEL-AURORA", "M-HOSPITAL-KITCHEN", "M-SCHOOL-ESPOO", "M-KAARNA-CATERING"):
        assert S.agent_credential_status(mid)["required"] is False  # existing demos unchanged
    seed.main()
    assert "Bearer" not in capsys.readouterr().out  # idempotent: not shown again
    S.authorize_agent("M-HOTEL-AURORA-SECURE", "aurora-secure-agent", token, "test")
    seed.main(rotate=True)
    assert "Bearer" in capsys.readouterr().out
    assert code(S.authorize_agent, "M-HOTEL-AURORA-SECURE", "x", token, "test") == ("agent_auth_invalid", 403)


@pytest.mark.skipif(not os.getenv("SOUS_TEST_MARIADB_URL"), reason="needs SOUS_TEST_MARIADB_URL")
def test_mariadb_credential_roundtrip(monkeypatch):
    monkeypatch.setattr(config, "DB_URL", os.environ["SOUS_TEST_MARIADB_URL"])
    monkeypatch.setattr(store, "_conn", None)
    store._exec("DROP TABLE IF EXISTS agent_credential")
    monkeypatch.setattr(agent_auth, "_ready_for", None)
    token = issue("M-AUTH-MARIA", require_agent_auth=True)["agent_credential"]["token"]
    S.authorize_agent("M-AUTH-MARIA", "a", token, "test")
    new = S.agent_credential("M-AUTH-MARIA", "chef")["agent_credential"]["token"]  # upsert path
    assert code(S.authorize_agent, "M-AUTH-MARIA", "a", token, "test")[0] == "agent_auth_invalid"
    S.authorize_agent("M-AUTH-MARIA", "a", new, "test")
    store._conn = None


# --- rationale ----------------------------------------------------------------

def test_rationale_is_signed_into_the_quote_kept_on_the_order_and_audited():
    issue()
    r = req()
    r.rationale = "  Breakfast milk for\n400 guests  "
    out = S.request_quote(r)
    assert out["quote"]["rationale"] == "Breakfast milk for 400 guests"
    assert signing.verify(out["jws"], "sous-quote+jws")["rationale"] == "Breakfast milk for 400 guests"
    assert events("quote.issued")[-1]["data"]["rationale"] == "Breakfast milk for 400 guests"
    o = S.place_order("a", "M", out["quote"]["id"], rationale="Monday delivery, as asked")
    assert o["agent_rationale"] == "Monday delivery, as asked"
    assert events("order.ordered")[-1]["data"]["agent_rationale"] == "Monday delivery, as asked"
    # without one at order time, the quote's stated reason carries over; none at all stays None
    q2 = S.request_quote(r)["quote"]
    assert S.place_order("a", "M", q2["id"])["agent_rationale"] == "Breakfast milk for 400 guests"
    q3 = S.request_quote(req())["quote"]
    assert "rationale" not in q3 and S.place_order("a", "M", q3["id"])["agent_rationale"] is None


def test_rationale_over_rest_and_its_length_limit():
    from fastapi.testclient import TestClient
    from sous.api import app
    issue()
    c = TestClient(app)
    body = {"agent_id": "a", "mandate_id": "M", "delivery_date": "2026-11-16",
            "items": [{"product_ids": ["MILK"], "amount": 10, "unit": "L"}]}
    assert c.post("/quotes", json={**body, "rationale": "x" * 501}).status_code == 422
    q = c.post("/quotes", json={**body, "rationale": "Weekly milk"}).json()["quote"]
    o = c.post("/orders", json={"agent_id": "a", "mandate_id": "M", "quote_id": q["id"], "rationale": "As quoted"})
    assert o.status_code == 200 and o.json()["agent_rationale"] == "As quoted"
