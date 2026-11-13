"""Buyer-signed mandates: W3C VC (vc+jwt) issued by the buyer's did:key, countersigned by the gateway. Offline."""

import base64
import json
from datetime import date, datetime, timedelta

import pytest

from sous import catalog, config, credentials as C, quote as Q, service as S, signing, store
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


def mandate(mid="M-VC", buyer="Kahvila Kide", **kw):
    return {"id": mid, "buyer": buyer, "agent_id": "kide-agent", "issued_by": "Aino (owner)",
            "auto_approve_limit_eur": 200, "hard_limit_eur": 1000, "avoid_allergens": ["AM"],
            "min_delivery_reliability": None, **kw}


def vc(key, mid="M-VC", valid_from=NOW - timedelta(hours=1), valid_until=NOW + timedelta(days=30), **kw):
    return C.sign_vc(key, mandate(mid, **kw), valid_from, valid_until)


def code(fn, *a, **k):
    with pytest.raises(S.GatewayError) as e:
        fn(*a, **k)
    return e.value.code


def quote_and_order(mid="M-VC"):
    q = S.request_quote(S.QuoteRequest(agent_id="kide-agent", mandate_id=mid, delivery_date=MON,
                                       items=[S.QuoteItem(product_ids=["MILK"], amount=10, unit="L")]))["quote"]
    return S.place_order("kide-agent", mid, q["id"])


# --- did:key ----------------------------------------------------------------

def test_did_key_round_trip_and_known_vector():
    for alg, prefix in (("ES256", "did:key:zDn"), ("EdDSA", "did:key:z6Mk")):
        k = C.new_key(alg)
        did = C.did_key(k.public_key())
        assert did.startswith(prefix)
        assert C.did_key(C.resolve(did)) == did and C.did_key(C.resolve(C.kid_for(did))) == did
    # did:key spec test vector (Ed25519)
    did = "did:key:z6MkiTBz1ymuepAQ4HEHYSF1H8quG5GLVVQR3djdX3mDooWp"
    assert C.did_key(C.resolve(did)) == did
    for bad in ("did:web:example.com", "did:key:z111", "did:key:zDnaeXYZ0O"):
        with pytest.raises(C.CredentialError):
            C.resolve(bad)


# --- issue, countersign, verify ---------------------------------------------

@pytest.mark.parametrize("alg", ["ES256", "EdDSA"])
def test_buyer_signed_mandate_is_countersigned_and_usable(alg):
    key = C.new_key(alg)
    did = C.did_key(key.public_key())
    token = vc(key)
    header = json.loads(base64.urlsafe_b64decode(token.split(".")[0] + "=="))
    assert header == {"alg": alg, "typ": "vc+jwt", "kid": C.kid_for(did), "cty": "vc"}
    r = S.issue_credential_mandate(token)
    assert r["issuer"] == {"did": did, "kind": "buyer"} and r["credential"] == token
    # the gateway countersigned the mandate plus the issuer and the credential's hash
    p = signing.verify(r["jws"], "sous-mandate+jws")
    assert p["issuer"]["did"] == did and p["credential_sha256"] == C.sha256(token)
    # the mandate ends with the credential
    assert p["valid_until"] == (NOW + timedelta(days=30)).isoformat()
    got = S.get_mandate("M-VC")
    assert got["issuer"]["kind"] == "buyer" and got["credential"] == token
    v = S.verify_mandate("M-VC")
    assert v["verdict"] == "valid" and v["gateway_signature"]["ok"] and v["buyer_signature"]["ok"]
    assert v["buyer_signature"]["alg"] == alg and v["issuer_authorised"]["how"] == "pinned on first use"
    assert v["validity"]["in_window"]
    assert quote_and_order()["status"] == "ordered"
    assert C.buyer_keys("Kahvila Kide")[0]["did"] == did


def test_gateway_issued_mandates_still_work_and_report_gateway_issuer():
    S.issue_mandate(Mandate(**mandate("M-GW", buyer="Hotel")))
    assert S.get_mandate("M-GW")["issuer"]["kind"] == "gateway" and S.get_mandate("M-GW")["credential"] is None
    v = S.verify_mandate("M-GW")
    assert v["verdict"] == "valid" and v["buyer_signature"] is None
    assert v["issuer"]["did"] == C.gateway_did() and v["issuer"]["did"].startswith("did:key:z6Mk")
    assert quote_and_order("M-GW")["status"] == "ordered"


def test_tampered_credential_is_rejected():
    key = C.new_key()
    h, p, s = vc(key).split(".")
    body = json.loads(base64.urlsafe_b64decode(p + "=="))
    body["credentialSubject"]["mandate"]["hard_limit_eur"] = 999999
    p2 = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()
    assert code(S.issue_credential_mandate, f"{h}.{p2}.{s}") == "credential_signature"
    # issuer that isn't the signing key
    other = C.did_key(C.new_key().public_key())
    body["issuer"]["id"] = other
    assert code(S.issue_credential_mandate, C.jws_sign(body, key, "vc+jwt")) == "credential_issuer"
    # wrong typ, wrong subject
    assert code(S.issue_credential_mandate, C.jws_sign(json.loads(base64.urlsafe_b64decode(p + "==")), key,
                                                       "JWT")) == "credential_type"
    good = json.loads(base64.urlsafe_b64decode(p + "=="))
    good["credentialSubject"]["id"] = "urn:sous:agent:someone-else"
    assert code(S.issue_credential_mandate, C.jws_sign(good, key, "vc+jwt")) == "credential_invalid"
    assert store.get_mandate("M-VC") is None


def test_tampering_after_storage_is_caught_on_load():
    key = C.new_key()
    S.issue_credential_mandate(vc(key))
    # someone edits the stored VC in the database: the gateway's countersigned hash no longer matches
    other = vc(key, hard_limit_eur=99999)
    store._exec("UPDATE mandate_credential SET vc_jwt = ? WHERE mandate_id = ?", (other, "M-VC"))
    assert code(quote_and_order) == "mandate_signature"
    assert S.verify_mandate("M-VC")["verdict"] == "invalid"


def test_wrong_issuer_for_pinned_buyer_is_rejected_and_approver_can_register():
    first, second = C.new_key(), C.new_key("EdDSA")
    S.issue_credential_mandate(vc(first))
    assert code(S.issue_credential_mandate, vc(second, "M-VC2")) == "issuer_not_authorised"
    C.add_buyer_key("Kahvila Kide", C.did_key(second.public_key()), "approver", "valio-admin")
    assert S.issue_credential_mandate(vc(second, "M-VC2"))["issuer"]["did"] == C.did_key(second.public_key())
    # removing the first key: mandates it signed stop verifying
    C.remove_buyer_key("Kahvila Kide", C.did_key(first.public_key()))
    assert code(quote_and_order) == "mandate_signature"


def test_no_tofu_for_buyers_with_gateway_mandates_or_when_off(monkeypatch):
    S.issue_mandate(Mandate(**mandate("M-GW", buyer="Hotel")))
    assert code(S.issue_credential_mandate, vc(C.new_key(), "M-X", buyer="Hotel")) == "issuer_not_authorised"
    monkeypatch.setattr(config, "BUYER_KEY_TOFU", False)
    assert code(S.issue_credential_mandate, vc(C.new_key(), "M-Y", buyer="New Buyer")) == "issuer_not_authorised"
    # a VC can't take over a gateway-issued mandate's id either
    k = C.new_key()
    C.add_buyer_key("Hotel", C.did_key(k.public_key()), "approver", "valio-admin")
    assert code(S.issue_credential_mandate, vc(k, "M-GW", buyer="Hotel")) == "mandate_exists"


def test_resubmission_is_idempotent_and_replacement_must_be_newer():
    key = C.new_key()
    token = vc(key, require_agent_auth=True)
    assert "agent_credential" in S.issue_credential_mandate(token)
    again = S.issue_credential_mandate(token)
    assert again["unchanged"] and "agent_credential" not in again  # replaying the public VC mints nothing
    older = vc(key, valid_from=NOW - timedelta(hours=2), auto_approve_limit_eur=900)
    assert code(S.issue_credential_mandate, older) == "credential_stale"
    newer = vc(key, valid_from=NOW - timedelta(minutes=1), auto_approve_limit_eur=50)
    assert S.issue_credential_mandate(newer)["mandate"]["auto_approve_limit_eur"] == 50


def test_expiry_and_not_yet_valid(monkeypatch):
    key = C.new_key()
    assert code(S.issue_credential_mandate, vc(key, valid_from=NOW - timedelta(days=2),
                                               valid_until=NOW - timedelta(days=1))) == "credential_expired"
    assert code(S.issue_credential_mandate, vc(key, valid_from=NOW + timedelta(days=1))) == "credential_not_yet_valid"
    # stored while valid, then time passes: the mandate engine blocks it as expired
    S.issue_credential_mandate(vc(key, valid_until=NOW + timedelta(minutes=5)))
    later = NOW + timedelta(minutes=10)  # quote still valid (15 min)
    monkeypatch.setattr(S, "_now", lambda: later)
    order = quote_and_order()
    assert order["status"] == "refused" and "mandate.expired" in {f["rule"] for f in order["decision"]["findings"]}
    assert S.verify_mandate("M-VC")["verdict"] == "outside_validity"


def test_buyer_revocation():
    key, stranger = C.new_key(), C.new_key()
    token = vc(key)
    S.issue_credential_mandate(token)
    assert code(S.revoke_by_credential, "M-VC", C.sign_revocation(stranger, "M-VC", token)) == "revocation_issuer"
    assert code(S.revoke_by_credential, "M-VC", C.sign_revocation(key, "M-VC", "other")) == "revocation_mismatch"
    r = S.revoke_by_credential("M-VC", C.sign_revocation(key, "M-VC", token, "agent replaced"))
    assert r["mandate"]["revoked"] and not r["already_revoked"]
    assert r["mandate"]["issuer"]["kind"] == "buyer"  # re-signed revoked mandate keeps the buyer binding
    v = S.verify_mandate("M-VC")
    assert v["verdict"] == "revoked" and v["revocation"]["ok"] and v["revocation"]["reason"] == "agent replaced"
    assert v["buyer_signature"]["ok"]
    assert quote_and_order()["status"] == "refused"
    assert S.revoke_by_credential("M-VC", C.sign_revocation(key, "M-VC", token))["already_revoked"]
    ev = [e for e in store.audit_events(limit=50) if e["event"] == "mandate.revoked"]
    assert len(ev) == 1 and ev[0]["actor"] == C.did_key(key.public_key())
    # replaying the original VC doesn't un-revoke it
    S.issue_credential_mandate(token)
    assert S._mandate("M-VC").revoked


def test_approver_revoke_of_buyer_signed_mandate_keeps_it_verifiable():
    S.issue_credential_mandate(vc(C.new_key()))
    S.revoke_mandate("M-VC", "valio-admin")
    v = S.verify_mandate("M-VC")
    assert v["verdict"] == "revoked" and v["buyer_signature"]["ok"]
    assert code(S.revoke_by_credential, "M-GW", "x") == "mandate_not_found"


# --- REST -------------------------------------------------------------------

def test_rest_endpoints(monkeypatch):
    from fastapi.testclient import TestClient

    from sous import api
    monkeypatch.setattr(config, "APPROVER_CODE", "1234")
    c = TestClient(api.app)
    key = C.new_key()
    token = vc(key)
    r = c.post("/mandates/credential", json={"credential": token})  # no approver code: the key is the authority
    assert r.status_code == 200, r.text
    assert r.json()["issuer"]["kind"] == "buyer" and r.json()["countersigned_by"] == C.gateway_did()
    assert c.get("/mandates/M-VC").json()["credential"] == token
    assert c.get("/mandates/M-VC/verify").json()["verdict"] == "valid"
    assert c.get("/buyers/Kahvila Kide/keys").json()["keys"][0]["source"] == "tofu"
    bad = c.post("/mandates/credential", json={"credential": "a.b.c"})
    assert bad.status_code == 400 and bad.json()["error"] == "credential_malformed"
    k2 = C.did_key(C.new_key().public_key())
    assert c.post("/buyers/Kahvila Kide/keys", json={"did": k2, "by": "x"}).status_code == 401
    r = c.post("/buyers/Kahvila Kide/keys", json={"did": k2, "by": "valio-admin"}, headers={"X-Approver-Code": "1234"})
    assert r.status_code == 200 and len(r.json()["keys"]) == 2
    r = c.post("/mandates/M-VC/revoke-credential", json={"revocation": C.sign_revocation(key, "M-VC", token)})
    assert r.status_code == 200 and r.json()["mandate"]["revoked"]
    assert c.get("/mandates/M-VC/verify").json()["verdict"] == "revoked"
