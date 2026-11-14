"""Passkey approval bound to the exact order (sous/passkey.py), with a software authenticator.

The authenticator is an EC P-256 key that builds WebAuthn responses the way a browser and platform authenticator
would: attestation "none" for registration, then assertions over authenticatorData || SHA-256(clientDataJSON).
Offline: catalog and Valio prices are replaced as in test_holds.py.
"""

import hashlib
import json
import secrets
import struct
import subprocess
import sys
from datetime import date, datetime

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from sous import api, catalog, config, holds, passkey, quote as Q, rules, service as S, signing, store
from sous.mandate import Mandate
from sous.passkey_verify import b64u, unb64u, verify_evidence

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
MON = date(2026, 11, 16)
ORIGIN, RP_ID = "http://testserver", "testserver"


def product(pid):
    return {
        "id": pid, "name": "Milk 1 l", "names": {"fi": "", "en": "", "sv": ""}, "brand": pid, "vendor": "",
        "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
        "allowed_lot_size": 1.0, "temperature": "chilled", "labels": [], "claims": [],
        "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": []},
        "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
        "official_substitutes": [], "deleted": False,
    }


def fake_offers(product_ids, requested, need=None, need_unit=None):
    out = []
    for pid in product_ids:
        pr = {"catalogPrice": {"value": 1.0, "valueWithTax": 1.14, "validTo": None},
              "comparisonPrice": {"value": 1.0}, "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}
        av = {"logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": False,
              "scheduleLines": [{"salesBlock": False, "availableAmount": 99999, "availableDate": "2026-11-16",
                                 "salesUnit": "ST"}]}
        out.append(Q.build_offer(product(pid), pr, av, requested, need, need_unit, NOW))
    return out, NOW


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(config, "APPROVER_CODE", "")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(config, "REQUIRE_AGENT_AUTH", False)
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: product(pid) if pid == "MILK" else None)
    monkeypatch.setattr(Q, "offers", fake_offers)
    monkeypatch.setattr(Q, "prefetch", lambda ids, requested: None)
    monkeypatch.setattr(S.substitutes, "suggest", lambda *a, **k: {"options": []})
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(holds, "_now", lambda: NOW)
    monkeypatch.setattr(S.notify, "send", lambda *a, **k: None)
    for mid, buyer, method in (("M", "Hotel", "any"), ("M2", "Bistro", "any"), ("MP", "Hotel", "passkey")):
        S.issue_mandate(Mandate(id=mid, buyer=buyer, agent_id="a", issued_by="chef", notify_above_eur=None,
                                auto_approve_limit_eur=50, hard_limit_eur=20000, min_delivery_reliability=None,
                                approval_method=method))
    api._hits.clear()  # these tests POST a lot: don't leave the per-IP rate limit used up for later tests
    yield
    api._hits.clear()
    signing._key.cache_clear()


def held(mandate_id="M", amount=100) -> dict:
    q = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id=mandate_id, delivery_date=MON,
                                       items=[S.QuoteItem(product_ids=["MILK"], amount=amount, unit="L")]))["quote"]
    o = S.place_order("a", mandate_id, q["id"])
    assert o["status"] == "pending_approval"
    return o


class Authenticator:
    """A software passkey: P-256, attestation 'none', user present + verified, counter per signature."""

    def __init__(self, rp_id=RP_ID, origin=ORIGIN, counter=True):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = secrets.token_bytes(20)
        self.rp_id, self.origin, self.counter_on, self.count = rp_id, origin, counter, 0

    def _cose(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def _cd(self, typ, challenge) -> bytes:
        return json.dumps({"type": typ, "challenge": challenge, "origin": self.origin, "crossOrigin": False}).encode()

    def create(self, options: dict) -> dict:
        pk = options["publicKey"]
        cd = self._cd("webauthn.create", pk["challenge"])
        auth = (hashlib.sha256(pk["rp"]["id"].encode()).digest() + bytes([0x45]) + struct.pack(">I", 0)
                + bytes(16) + struct.pack(">H", len(self.cred_id)) + self.cred_id + self._cose())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        cid = b64u(self.cred_id)
        return {"id": cid, "rawId": cid, "type": "public-key",
                "response": {"clientDataJSON": b64u(cd), "attestationObject": b64u(att)}}

    def get(self, options: dict, rp_id: str | None = None) -> dict:
        pk = options["publicKey"]
        if self.counter_on:
            self.count += 1
        cd = self._cd("webauthn.get", pk["challenge"])
        auth = hashlib.sha256((rp_id or pk["rpId"]).encode()).digest() + bytes([0x05]) + struct.pack(">I", self.count)
        sig = self.key.sign(auth + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        cid = b64u(self.cred_id)
        return {"id": cid, "rawId": cid, "type": "public-key",
                "response": {"clientDataJSON": b64u(cd), "authenticatorData": b64u(auth), "signature": b64u(sig),
                             "userHandle": None}}


@pytest.fixture
def client():
    return TestClient(api.app)


def enrol(client, auth: Authenticator, name="Chef Anna", headers=None, **scope) -> dict:
    scope = scope or {"mandate_id": "M"}
    r = client.post("/approvers/register/options", json={"name": name, **scope}, headers=headers or {})
    assert r.status_code == 200, r.text
    r = client.post("/approvers/register/verify", json={"credential": auth.create(r.json())})
    assert r.status_code == 200, r.text
    return r.json()


def options(client, order_id, action="approve"):
    r = client.post(f"/orders/{order_id}/approve/options", json={"action": action})
    assert r.status_code == 200, r.text
    return r.json()


def test_register_then_approve_records_passkey_authority_and_verifiable_evidence(client):
    auth = Authenticator()
    reg = enrol(client, auth)
    assert reg["approver"] == "Chef Anna" and reg["scope"] == {"type": "mandate", "value": "M"}
    assert [e["event"] for e in store.audit_events(limit=50)].count("approver.passkey_registered") == 1
    o = held()
    opts = options(client, o["id"])
    b = opts["binding"]
    assert b["order_id"] == o["id"] and b["total_ex_vat"] == o["total_ex_vat"] and b["action"] == "approve"
    assert opts["publicKey"]["challenge"] == opts["challenge"]
    r = client.post(f"/orders/{o['id']}/approve/passkey", json={"credential": auth.get(opts), "note": "ok"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "ordered"
    assert out["approval"]["by"] == "Chef Anna"
    assert out["approval"]["authority"] == f"passkey Chef Anna (credential …{reg['credential_id'][-8:]})"
    assert out["approval"]["passkey"]["challenge"] == opts["challenge"]
    # the re-signed receipt covers the passkey reference
    assert signing.verify(out["receipt_jws"], "sous-order+jws")["approval"]["passkey"]["challenge"] == opts["challenge"]
    ev = [e for e in store.audit_events(o["id"]) if e["event"] == "order.approved"][0]["data"]["passkey"]
    assert set(ev["assertion"]) >= {"authenticatorData", "clientDataJSON", "signature"}
    assert verify_evidence(ev)["ok"]
    v = client.get(f"/orders/{o['id']}/approval/verify").json()
    assert v["ok"] and v["passkey_decisions"][0]["approver"] == "Chef Anna"
    # tampering with the stored evidence breaks the offline check
    bad = json.loads(json.dumps(ev))
    bad["binding"]["total_ex_vat"] = 1.0
    assert not verify_evidence(bad)["ok"]
    bad = json.loads(json.dumps(ev))
    bad["assertion"]["signature"] = b64u(unb64u(bad["assertion"]["signature"])[:-1] + b"\x00")
    assert not verify_evidence(bad)["ok"]


def test_assertion_for_another_orders_challenge_fails(client):
    auth = Authenticator()
    enrol(client, auth)
    a, b = held(), held()
    cred = auth.get(options(client, a["id"]))
    r = client.post(f"/orders/{b['id']}/approve/passkey", json={"credential": cred})
    assert r.status_code == 400 and r.json()["error"] == "passkey_invalid"
    assert S.get_order(b["id"])["status"] == "pending_approval"
    # a challenge signed for "reject" can't approve
    cred = auth.get(options(client, b["id"], "reject"))
    r = client.post(f"/orders/{b['id']}/approve/passkey", json={"credential": cred})
    assert r.status_code == 400 and r.json()["error"] == "passkey_invalid"
    assert client.post(f"/orders/{b['id']}/reject/passkey", json={"credential": cred}).json()["status"] == "rejected"


def test_replay_fails_by_one_time_challenge_and_sign_count(client):
    auth = Authenticator()
    enrol(client, auth)
    o = held()
    opts = options(client, o["id"])
    cred = auth.get(opts)
    passkey.verify_approval(S.get_order(o["id"]), "approve", cred, [ORIGIN])  # first use consumes the challenge
    with pytest.raises(S.GatewayError) as e:
        passkey.verify_approval(S.get_order(o["id"]), "approve", cred, [ORIGIN])
    assert e.value.code == "passkey_invalid" and e.value.status == 409
    # a fresh challenge signed with a counter that didn't move (a cloned key) fails on the sign count
    auth.count -= 1
    r = client.post(f"/orders/{o['id']}/approve/passkey", json={"credential": auth.get(options(client, o["id"]))})
    assert r.status_code == 403 and "sign count" in r.json()["message"]
    # after a successful approval the same assertion is refused again
    auth.count = 10
    good = auth.get(options(client, o["id"]))
    assert client.post(f"/orders/{o['id']}/approve/passkey", json={"credential": good}).json()["status"] == "ordered"
    r = client.post(f"/orders/{o['id']}/approve/passkey", json={"credential": good})
    assert r.status_code == 409


def test_wrong_key_wrong_scope_and_wrong_rp_fail(client):
    auth, other = Authenticator(), Authenticator()
    enrol(client, auth)
    o = held()
    opts = options(client, o["id"])
    forged = other.get(opts)
    forged["id"] = forged["rawId"] = b64u(auth.cred_id)  # claims Anna's credential, signed with another key
    r = client.post(f"/orders/{o['id']}/approve/passkey", json={"credential": forged})
    assert r.status_code == 403 and r.json()["error"] == "passkey_invalid"
    # Anna's passkey is for mandate M only: Bistro's order under M2 has no passkey
    o2 = held("M2")
    r = client.post(f"/orders/{o2['id']}/approve/options", json={})
    assert r.status_code == 409 and r.json()["error"] == "passkey_not_registered"
    # an assertion made for another RP ID (a phishing site) fails
    r = client.post(f"/orders/{o['id']}/approve/passkey", json={"credential": auth.get(opts, rp_id="evil.example")})
    assert r.status_code == 403
    assert S.get_order(o["id"])["status"] == "pending_approval"


def test_buyer_scope_covers_every_mandate_of_the_buyer(client):
    auth = Authenticator()
    enrol(client, auth, buyer="Hotel")
    o = held("MP")
    r = client.post(f"/orders/{o['id']}/approve/passkey", json={"credential": auth.get(options(client, o["id"]))})
    assert r.json()["status"] == "ordered"


def test_mandate_requiring_passkey_refuses_code_approval_but_allows_rejection(client, monkeypatch):
    monkeypatch.setattr(config, "APPROVER_CODE", "s3cret")
    h = {"X-Approver-Code": "s3cret"}
    o = held("MP")
    r = client.post(f"/orders/{o['id']}/approve", json={"by": "Boss"}, headers=h)
    assert r.status_code == 403 and r.json()["error"] == "passkey_required"
    assert S.get_order(o["id"])["status"] == "pending_approval"
    auth = Authenticator()
    enrol(client, auth, mandate_id="MP", headers=h)
    r = client.post(f"/orders/{o['id']}/approve/options", json={}, headers=h)
    cred = auth.get(r.json())
    r = client.post(f"/orders/{o['id']}/approve/passkey", json={"credential": cred})  # no code needed
    assert r.json()["status"] == "ordered" and r.json()["approval"]["authority"].startswith("passkey Chef Anna")
    o2 = held("MP")
    assert client.post(f"/orders/{o2['id']}/reject", json={"by": "Boss"}, headers=h).json()["status"] == "rejected"
    # code-approved orders on ordinary mandates still work as before
    o3 = held("M")
    assert client.post(f"/orders/{o3['id']}/approve", json={"by": "Boss"}, headers=h).json()["status"] == "ordered"


def test_enrolment_needs_a_human_credential(client, monkeypatch):
    monkeypatch.setattr(config, "APPROVER_CODE", "s3cret")
    body = {"name": "Mallory", "mandate_id": "M"}
    assert client.post("/approvers/register/options", json=body).status_code == 401
    r = client.post("/approvers/register/options", json=body, headers={"X-Approver-Code": "vka_whatever"})
    assert r.status_code == 403 and r.json()["error"] == "agent_cannot_approve"
    assert client.post("/approvers/register/options", json={"name": "x"}, headers={"X-Approver-Code": "s3cret"}
                       ).status_code == 400
    # a registration response can't be replayed into a second credential row
    auth = Authenticator()
    r = client.post("/approvers/register/options", json=body, headers={"X-Approver-Code": "s3cret"})
    cred = auth.create(r.json())
    assert client.post("/approvers/register/verify", json={"credential": cred}).status_code == 200
    assert client.post("/approvers/register/verify", json={"credential": cred}).status_code == 409


def test_challenge_bound_to_order_contents():
    """The binding hashes the signed quote JWS and the lines: a stored order whose lines changed no longer
    matches the challenge."""
    o = held()
    b = passkey.binding_fields(o)
    assert b["lines_sha256"] == hashlib.sha256(signing.canonical(o["lines"])).hexdigest()
    assert b["quote_jws_sha256"] == hashlib.sha256(store.get_quote(o["quote_id"])["_jws"].encode()).hexdigest()


def test_rules_register_lists_passkey_refusals():
    ids = {r["id"] for r in rules.register()}
    assert {"passkey_required", "passkey_invalid", "passkey_not_registered"} <= ids


def test_offline_script_verifies_saved_event(client, tmp_path):
    auth = Authenticator()
    enrol(client, auth)
    o = held()
    client.post(f"/orders/{o['id']}/approve/passkey", json={"credential": auth.get(options(client, o["id"]))})
    ev = [e for e in store.audit_events(o["id"]) if e["event"] == "order.approved"][0]
    (tmp_path / "ev.json").write_text(json.dumps(ev))
    script = str(config.ROOT / "scripts" / "verify_passkey_approval.py")

    def run(order):
        (tmp_path / "o.json").write_text(json.dumps(order))
        return subprocess.run([sys.executable, script, "--event", str(tmp_path / "ev.json"), "--order-json",
                               str(tmp_path / "o.json")], capture_output=True, text=True)

    r = run(S.get_order(o["id"]))
    assert r.returncode == 0 and "VERIFIED" in r.stdout, r.stdout + r.stderr
    r = run(S.get_order(held()["id"]))
    assert r.returncode == 1 and "FAIL order_id" in r.stdout


def test_approval_url_points_to_passkey_page_when_mandate_requires_it():
    o, op = held("M"), held("MP")
    assert op["approval_url"] == f"/ui/approve.html?order={op['id']}"
    assert op["order_url"] != op["approval_url"]
    assert o["approval_url"] == o["order_url"]  # code-approvable mandates keep the seller inbox link
