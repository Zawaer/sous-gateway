"""Open-standard exports and discovery (sous/standards.py). Offline: fake offers, temp state, vendored AP2 schemas."""

import base64
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient
from referencing import Registry, Resource

from sous import api, catalog, config, credentials as C, quote as Q, service as S, signing, standards as STD, store
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
MON = date(2026, 11, 16)
MILK = {
    "id": "MILK", "name": "Milk 1 l", "names": {"fi": "", "en": "", "sv": ""}, "brand": "", "vendor": "",
    "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
    "allowed_lot_size": 1.0, "temperature": "chilled", "labels": [], "claims": [],
    "allergens": {"declared": True, "contains": [], "may_contain": [], "free_from": ["AM"]},
    "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
    "official_substitutes": [], "deleted": False,
}
AP2 = Path(__file__).parent / "fixtures" / "ap2"


def fake_offers(product_ids, requested, need=None, need_unit=None):
    pr = {"catalogPrice": {"value": 1.0, "valueWithTax": 1.14, "validTo": None}, "comparisonPrice": {"value": 1.0},
          "comparisonUnit": "L", "tax": {"taxRate": 14.0}, "error": False}
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
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(config, "BUYER_KEY_TOFU", True)
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: MILK if pid == "MILK" else None)
    monkeypatch.setattr(Q, "offers", fake_offers)
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(S.notify, "send", lambda *a, **k: None)
    yield
    signing._key.cache_clear()


def _registry() -> Registry:
    """The vendored AP2 v0.2.0 schemas, under their $id and under the URL their relative $refs resolve to."""
    reg = Registry()
    for p in AP2.rglob("*.json"):
        doc = json.loads(p.read_text())
        res = Resource.from_contents(doc)
        reg = reg.with_resource(doc["$id"], res)
        reg = reg.with_resource("https://ap2-protocol.org/schemas/" + p.relative_to(AP2).as_posix(), res)
    return reg


def ap2_valid(name: str, instance: dict, drop: tuple[str, ...] = ()) -> None:
    schema = json.loads((AP2 / f"{name}.json").read_text())
    for path in drop:  # e.g. "properties.constraints.contains"
        *parents, last = path.split(".")
        node = schema
        for k in parents:
            node = node[k]
        node.pop(last)
    jsonschema.Draft202012Validator(schema, registry=_registry()).validate(instance)


def issue(mid="M-STD", **kw):
    m = Mandate(id=mid, buyer="Kahvila Kide", agent_id="kide-agent", issued_by="Aino", auto_approve_limit_eur=200,
                hard_limit_eur=1000, avoid_allergens=["AM"], min_delivery_reliability=None,
                valid_until=NOW + timedelta(days=30), **kw)
    return S.issue_mandate(m)


def quote(mid="M-STD", amount=10):
    return S.request_quote(S.QuoteRequest(agent_id="kide-agent", mandate_id=mid, delivery_date=MON,
                                          items=[S.QuoteItem(product_ids=["MILK"], amount=amount, unit="L")]))["quote"]


def _payload(jwt: str) -> dict:
    p = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))


# --- mandate -----------------------------------------------------------------

def test_mandate_ap2_open_mandates_follow_the_published_schemas():
    issue()
    out = STD.ap2_mandate("M-STD")
    pay, chk = out["open_payment_mandate"], out["open_checkout_mandate"]
    ap2_valid("open_payment_mandate", pay)
    # The one documented gap: AP2 wants an enumerated checkout.line_items constraint.
    ap2_valid("open_checkout_mandate", chk, drop=("properties.constraints.contains",))
    with pytest.raises(jsonschema.ValidationError):
        ap2_valid("open_checkout_mandate", chk)
    assert "partial" in out["conformance"]["open_checkout_mandate"]
    rng = next(c for c in pay["constraints"] if c["type"] == "payment.amount_range")
    assert rng == {"type": "payment.amount_range", "currency": "EUR", "max": 100000}
    ref = next(c for c in pay["constraints"] if c["type"] == "payment.reference")
    assert ref["conditional_transaction_id"] == STD.b64_sha256(signing.canonical(chk))
    assert chk["cnf"] == {"kid": "urn:sous:agent:kide-agent"} and chk["exp"] == int((NOW + timedelta(days=30)).timestamp())
    assert chk["sous_constraints"]["avoid_allergens"] == ["AM"]
    assert "EUR 200.00" in out["ap2_v0_1_intent_mandate"]["natural_language_description"]
    # The gateway-issued VC view verifies against the gateway's did:key.
    header, vc = C.jws_verify(out["verifiable_credential"]["vc_jwt"], C.VC_TYP)
    assert vc["issuer"]["id"] == C.gateway_did() and vc["credentialSubject"]["mandate"]["id"] == "M-STD"
    with pytest.raises(C.CredentialError):  # an attestation, not a buyer credential: can't be replayed as one
        C.parse_vc(out["verifiable_credential"]["vc_jwt"])
    assert out["authorization"]["native_jws"] == S.get_mandate("M-STD")["jws"]
    assert STD.ap2_mandate("M-STD") == out  # deterministic


def test_mandate_ap2_for_buyer_signed_mandate_reuses_the_buyers_vc():
    key = C.new_key()
    mandate = {"id": "M-VC", "buyer": "Kahvila Kide", "agent_id": "kide-agent", "issued_by": "Aino",
               "auto_approve_limit_eur": 200, "hard_limit_eur": 1000, "min_delivery_reliability": None}
    token = C.sign_vc(key, mandate, NOW - timedelta(hours=1), NOW + timedelta(days=30))
    S.issue_credential_mandate(token)
    out = STD.ap2_mandate("M-VC")
    assert out["verifiable_credential"]["vc_jwt"] == token
    assert out["verifiable_credential"]["issuer_kind"] == "buyer"
    assert out["open_checkout_mandate"]["iat"] == int((NOW - timedelta(hours=1)).timestamp())
    ap2_valid("open_payment_mandate", out["open_payment_mandate"])


def test_tampered_mandate_is_not_exported():
    issue()
    row = store.get_mandate("M-STD")
    jws = row.pop("_jws")
    store.put_mandate({**row, "hard_limit_eur": 99999}, jws[:-4] + "AAAA")
    with pytest.raises(S.GatewayError) as e:
        STD.ap2_mandate("M-STD")
    assert e.value.code == "mandate_signature"


# --- quote -------------------------------------------------------------------

def test_quote_ap2_checkout_is_merchant_signed_and_bound_by_hash():
    issue()
    q = quote()
    out = STD.ap2_quote(q["id"], "https://sous.example")
    ap2_valid("checkout_mandate", out["checkout_mandate"])
    jwt = out["checkout_jwt"]
    assert signing.verify(jwt, "JWT") == out["checkout"]  # verifiable with /.well-known/jwks.json
    assert out["checkout_hash"] == STD.b64_sha256(jwt)
    co = out["checkout"]
    assert co["status"] == "ready_for_complete" and co["currency"] == "EUR" and co["id"] == q["id"]
    assert [t["type"] for t in co["totals"]] == ["subtotal", "tax", "total"]
    assert co["totals"][0]["amount"] == round(q["total_ex_vat"] * 100)
    assert co["totals"][2]["amount"] == round(q["total_inc_vat"] * 100)
    li = co["line_items"][0]
    assert li["item"] == {"id": "MILK", "title": "Milk 1 l", "price": 114} and li["quantity"] == 10
    # v0.1 CartMandate: merchant_authorization binds the cart contents.
    cart = out["ap2_v0_1_cart_mandate"]
    auth = signing.verify(cart["merchant_authorization"], "JWT")
    assert auth["cart_hash"] == STD.b64_sha256(signing.canonical(cart["contents"]))
    assert STD.ap2_quote(q["id"], "https://sous.example") == out  # deterministic


def test_quote_checkout_status_follows_the_mandate_preview():
    issue()
    big = quote(amount=500)  # EUR 500 > auto limit 200: ask
    assert big["mandate_preview"]["outcome"] == "ask"
    co = STD.ap2_quote(big["id"], "https://x")["checkout"]
    assert co["status"] == "requires_escalation"
    assert any(m["code"] == "mandate.spend.auto_limit" for m in co["messages"])


def test_quote_schema_org_offer():
    issue()
    q = quote()
    o = STD.schema_org_quote(q["id"], "https://sous.example")
    assert o["@context"] == "https://schema.org" and o["@type"] == "Offer"
    assert o["price"] == q["total_inc_vat"] and o["validThrough"] == q["expires_at"]
    assert o["includesObject"][0]["amountOfThisGood"] == 10 and o["includesObject"][0]["unitCode"] == "H87"
    assert o["addOn"][0]["availability"] == "https://schema.org/InStock"
    assert o["seller"]["name"] == "Valio Aimo"


def cutoff_passed_offers(product_ids, requested, need=None, need_unit=None):
    pr = {"catalogPrice": {"value": 1.0, "valueWithTax": 1.14, "validTo": None}, "comparisonPrice": {"value": 1.0},
          "comparisonUnit": "L", "tax": {"taxRate": 14.0}, "error": False}
    av = {"logisticModel": "Terminal", "closingTime": "2026-11-12T15:00:00+0200", "salesBlock": False,
          "scheduleLines": [{"salesBlock": False, "availableAmount": 999999, "availableDate": "2026-11-16",
                             "salesUnit": "ST"}]}
    return [Q.build_offer(MILK, pr, av, requested, need, need_unit, NOW) for _ in product_ids], NOW


def test_unorderable_line_is_indicative_not_chargeable(monkeypatch):
    """Regression: a quote whose only priced line is past cut-off has total 0; no export may charge the line."""
    issue()
    monkeypatch.setattr(Q, "offers", cutoff_passed_offers)
    monkeypatch.setattr(S.substitutes, "suggest", lambda *a, **k: {"options": []})  # needs data/ otherwise
    q = quote()
    assert [l["orderable"] for l in q["lines"]] == [False] and q["total_inc_vat"] == 0
    assert q["lines"][0]["line_total_inc_vat"] == 11.4
    out = STD.ap2_quote(q["id"], "https://x")
    ap2_valid("checkout_mandate", out["checkout_mandate"])
    co = out["checkout"]
    assert co["status"] == "incomplete" and co["line_items"] == []
    assert sum(t["amount"] for li in co["line_items"] for t in li["totals"] if t["type"] == "total") \
        == next(t["amount"] for t in co["totals"] if t["type"] == "total") == 0
    (u,) = co["sous"]["unavailable_lines"]
    assert u["id"] == "L1" and "CUTOFF_PASSED" in u["reasons"]
    assert next(t["amount"] for t in u["indicative"]["totals"] if t["type"] == "total") == 1140
    cutoff = next(m for m in co["messages"] if m["code"] == "CUTOFF_PASSED")
    assert cutoff["path"] == "$.sous.unavailable_lines[0]"
    details = out["ap2_v0_1_cart_mandate"]["contents"]["payment_request"]["details"]
    assert details["display_items"] == [] and details["total"]["amount"]["value"] == 0
    (ui,) = out["ap2_v0_1_cart_mandate"]["contents"]["sous_unavailable_items"]
    assert ui["indicative_amount"]["value"] == 11.4 and "CUTOFF_PASSED" in ui["reasons"]
    so = STD.schema_org_quote(q["id"], "https://x")
    assert so["price"] == 0 and so["includesObject"] == []
    assert so["addOn"][0]["availability"] == "https://schema.org/OutOfStock"
    assert "Indicative" in so["addOn"][0]["description"]


# --- order -------------------------------------------------------------------

def test_order_receipt_and_schema_org_order():
    issue()
    q = quote()
    o = S.place_order("kide-agent", "M-STD", q["id"])
    assert o["status"] == "ordered"
    r = STD.ap2_receipt(o["id"], "https://x")
    ap2_valid("checkout_receipt", r["receipt"])
    assert r["receipt"]["status"] == "Success" and r["receipt"]["order_id"] == o["id"]
    assert r["receipt"]["reference"] == STD.ap2_quote(q["id"], "https://x")["checkout_hash"]
    assert signing.verify(r["receipt_jwt"], "JWT") == r["receipt"]
    so = STD.schema_org_order(o["id"], "https://x")
    assert so["@type"] == "Order" and so["orderStatus"] == "https://schema.org/OrderProcessing"
    assert so["orderedItem"][0]["@type"] == "OrderItem" and so["orderedItem"][0]["orderQuantity"]["value"] == 10


def test_refused_and_pending_orders():
    issue()
    big = quote(amount=500)
    pending = S.place_order("kide-agent", "M-STD", big["id"])
    assert STD.ap2_receipt(pending["id"], "https://x")["final"] is False
    S.decide(pending["id"], "Aino", False, "too much")
    r = STD.ap2_receipt(pending["id"], "https://x")["receipt"]
    ap2_valid("checkout_receipt", r)
    assert r["status"] == "Error" and r["error"] == "rejected_by_human"
    huge = quote(amount=2000)  # above the hard limit: refused
    refused = S.place_order("kide-agent", "M-STD", huge["id"])
    r = STD.ap2_receipt(refused["id"], "https://x")["receipt"]
    ap2_valid("checkout_receipt", r)
    assert r["error"] == "mandate_refused" and "hard limit" in r["error_description"]


# --- JSON Schemas of native objects -----------------------------------------

def test_native_objects_validate_against_published_schemas():
    issue()
    q = quote()
    o = S.place_order("kide-agent", "M-STD", q["id"])
    cases = {"mandate": S.get_mandate("M-STD")["mandate"], "quote": q, "order": o,
             "receipt": signing.verify(o["receipt_jws"], "sous-order+jws")}
    for name, instance in cases.items():
        schema = STD.json_schema(name, "https://x")
        assert schema["$id"] == f"https://x/schemas/{name}.json"
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(json.loads(json.dumps(instance, default=str)))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(STD.json_schema("receipt", "https://x")).validate({**cases["receipt"],
                                                                                         "status": "shipped"})


# --- discovery over HTTP ----------------------------------------------------

def test_discovery_and_export_routes():
    c = TestClient(api.app)
    card = c.get("/.well-known/agent-card.json").json()
    for k in ("name", "description", "supportedInterfaces", "version", "capabilities", "defaultInputModes",
              "defaultOutputModes", "skills"):  # required by A2A v1.0 AgentCard
        assert card[k] not in (None, ""), k
    assert {i["protocolBinding"] for i in card["supportedInterfaces"]} == {"MCP", "OPENAPI"}
    assert card["supportedInterfaces"][0]["url"] == "http://testserver/mcp"
    assert {"search_products", "request_quote", "place_order"} <= {s["id"] for s in card["skills"]}
    assert all(s["tags"] for s in card["skills"])
    params = card["capabilities"]["extensions"][0]["params"]
    assert params["jwks_uri"] == "http://testserver/.well-known/jwks.json"
    assert card["securitySchemes"]["agentToken"]["httpAuthSecurityScheme"]["scheme"] == "Bearer"
    assert c.get("/.well-known/agent.json").json() == card

    sc = c.get("/.well-known/mcp/server-card.json").json()
    assert sc["$schema"] == STD.MCP_SERVER_CARD_SCHEMA and sc["name"] == "io.github.zawaer/sous"
    assert sc["remotes"][0] == {**sc["remotes"][0], "type": "streamable-http", "url": "http://testserver/mcp"}
    assert c.get("/mcp/server-card").json() == sc
    assert c.get("/.well-known/ai-catalog.json").json()["entries"][0]["type"] == "application/mcp-server-card+json"

    for name in ("mandate", "quote", "order", "receipt"):
        r = c.get(f"/schemas/{name}.json")
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/schema+json")
    assert c.get("/schemas/nope.json").status_code == 422

    issue()
    q = quote()
    assert c.get("/mandates/M-STD/ap2").json()["open_payment_mandate"]["vct"] == "mandate.payment.open.1"
    assert c.get(f"/quotes/{q['id']}/ap2").json()["checkout_mandate"]["vct"] == "mandate.checkout.1"
    r = c.get(f"/quotes/{q['id']}/schema-org")
    assert r.headers["content-type"].startswith("application/ld+json") and r.json()["@type"] == "Offer"
    o = S.place_order("kide-agent", "M-STD", q["id"])
    assert c.get(f"/orders/{o['id']}/ap2").json()["receipt"]["status"] == "Success"
    assert c.get(f"/orders/{o['id']}/schema-org").json()["@type"] == "Order"
    assert c.get("/quotes/Q-NOPE/ap2").status_code == 404
    assert c.get("/mandates/M-NOPE/ap2").status_code == 404


def test_cards_use_public_base_url_and_card_url_is_never_null(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://sous.example")
    c = TestClient(api.app)
    card = c.get("/.well-known/agent-card.json").json()
    assert card["url"] == "https://sous.example/mcp"  # A2A 0.3 readers; v1.0 readers use supportedInterfaces
    assert card["supportedInterfaces"][0]["url"] == card["url"] and card["preferredTransport"] == "MCP"
    assert {i["url"] for i in card["additionalInterfaces"]} == {i["url"] for i in card["supportedInterfaces"]}
    assert c.get("/.well-known/mcp/server-card.json").json()["remotes"][0]["url"] == card["url"]
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    assert c.get("/.well-known/agent-card.json").json()["url"] == "http://testserver/mcp"


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_mcp_initialize_works_with_and_without_trailing_slash(path, monkeypatch):
    monkeypatch.setattr(catalog, "_db", lambda: None)
    monkeypatch.setattr(api.mcp.session_manager, "_has_started", False)  # the lifespan runs once per instance
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(api.app, follow_redirects=False) as c:
        r = c.post(path, headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        assert r.status_code == 200 and "serverInfo" in r.text  # served in place: no 307 for a POST
        sid = r.headers["mcp-session-id"]
        r = c.post(path, headers={**h, "Mcp-Session-Id": sid},
                   json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert r.status_code == 200 and "request_quote" in r.text
