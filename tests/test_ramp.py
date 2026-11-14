"""Earned autonomy (sous/ramp.py): Sous proposes delegating a scope only after a clean approval record, the buyer
decides, and trust can be withdrawn. Offline: orders are written straight to the store."""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from sous import api, catalog, config, ramp, rules as R, service as S, signing, store
from sous.mandate import Mandate, evaluate

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=config.TZ)
H = {"x-approver-code": "c0de"}
PRODUCTS = {}


def product(pid, name, en):
    return {"id": pid, "name": name, "names": {"fi": name, "en": en, "sv": ""}, "brand": "Valio", "category": "10400",
            "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0}, "claims": [], "labels": [],
            "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": []}}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(config, "APPROVER_CODE", "c0de")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    PRODUCTS.clear()
    PRODUCTS["C"] = product("C", "VALIO KUOHUKERMA 35% 1 L", "Whipping cream 35% 1 l")
    PRODUCTS["M"] = product("M", "VALIO RASVATON MAITO 1 L", "Skimmed milk 1 l")
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(pid))
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(ramp, "_now", lambda: NOW)
    yield
    signing._key.cache_clear()


def mandate(**kw) -> Mandate:
    S.issue_mandate(Mandate(**{"id": "M1", "buyer": "Kitchen", "agent_id": "a", "issued_by": "chef", **kw}))
    return S._mandate("M1")


_n = [0]


def order(days_ago: float, result: str, findings: list[dict]) -> str:
    """A decided held order: result approved / edited / rejected."""
    _n[0] += 1
    oid = f"PO-{_n[0]:03d}"
    at = (NOW - timedelta(days=days_ago)).isoformat(timespec="milliseconds")
    status = "ordered" if result == "approved" else "rejected"
    approval = {"by": "Mikko", "approved": result == "approved", "at": at}
    if result == "edited":
        approval["replaced_by"] = "PO-NEW"
    store.insert_order({"id": oid, "quote_id": "Q" + oid, "mandate_id": "M1", "agent_id": "a", "buyer": "Kitchen",
                        "status": status, "total_ex_vat": 10.0, "lines": [],
                        "decision": {"outcome": "ask", "findings": findings}, "approval": approval,
                        "created_at": at, "updated_at": at})
    return oid


def price(pid="C", outcome="ask", rule="price.changed"):
    return {"rule": rule, "outcome": outcome, "message": "price moved", "product_id": pid}


def history(n=6, weeks=3, pid="C", rule="price.changed"):
    for i in range(n):
        order(1 + i * 7 * weeks / n, "approved", [price(pid, rule=rule)])


def test_threshold_not_met_means_no_proposal():
    m = mandate()
    history(n=ramp.MIN_APPROVALS - 1)  # one approval short
    assert ramp.status(m)["proposals"] == []
    order(0.5, "approved", [price()])
    assert len(ramp.status(m)["proposals"]) == 1


def test_one_week_or_one_rejection_or_edit_is_not_enough():
    m = mandate()
    for i in range(8):  # plenty, but all in one day
        order(1 + i * 0.01, "approved", [price()])
    assert ramp.status(m)["proposals"] == []
    history()
    order(2, "rejected", [price()])
    assert ramp.status(m)["proposals"] == []
    store._exec("DELETE FROM purchase_order")
    history()
    order(2, "edited", [price()])
    assert ramp.status(m)["proposals"] == []


def test_met_threshold_proposes_with_evidence_and_audits_once():
    m = mandate()
    history(n=6)
    order(3, "approved", [price("M")])  # another scope, too little record
    st = ramp.status(m)
    [p] = st["proposals"]
    assert (p["kind"], p["rule"], p["scope"], p["label"]) == ("relax", "price.changed", "type:kuohukerma",
                                                               "whipping cream")
    assert (p["from_tier"], p["to_tier"]) == ("ask", "notify")
    assert p["evidence"].startswith("You approved 6 of 6 held orders with a price change on whipping cream unchanged")
    assert "Hyväksyit 6/6" in p["evidence_fi"] and len(p["orders"]) == 6
    assert "type:kuohukerma" in {t["scope"] for t in st["track_record"]} and len(st["track_record"]) == 2
    ramp.status(m)
    assert sum(e["event"] == "ramp.proposed" for e in store.audit_events(subject="M1")) == 1


def test_accept_signs_an_amended_mandate_that_lowers_the_tier():
    m = mandate()
    history()
    [p] = ramp.status(m)["proposals"]
    c = TestClient(api.app)
    assert c.post(f"/mandates/M1/autonomy/{p['id']}/accept", json={"by": "Mikko"}).status_code == 401
    r = c.post(f"/mandates/M1/autonomy/{p['id']}/accept", headers=H, json={"by": "Mikko"})
    assert r.status_code == 200, r.text
    assert signing.verify(r.json()["jws"], "sous-mandate+jws")["delegated"][0]["scope"] == "type:kuohukerma"
    st = c.get("/mandates/M1/autonomy", headers=H).json()
    assert st["proposals"] == [] and st["delegated"][0]["rule"] == "price.changed"
    events = [e["event"] for e in store.audit_events(subject="M1")]
    assert "ramp.accepted" in events and "mandate.updated" in events
    m = S._mandate("M1")
    lines = [{"product_id": pid, "name": PRODUCTS[pid]["name"], "quantity": 1, "unit_price_ex_vat": 2.0,
              "line_total_ex_vat": 2.0, "sales_unit": "ST", "issues": []} for pid in ("C", "M")]
    last = {pid: {"unit_price_ex_vat": 1.0, "order_id": "PO-0", "at": "2026-11-01T10:00:00+02:00"} for pid in "CM"}
    outcome, fs = evaluate(m, "a", lines, requested_date=date(2026, 11, 16), now=NOW, last_prices=last)
    got = {(f.rule, f.product_id): f.outcome for f in fs}
    assert got[("earned.price_changed", "C")] == "notify"  # whipping cream: delegated
    assert got[("price.changed", "M")] == "ask" and outcome == "ask"  # milk: still asks
    assert "Delegated by the buyer" in next(f.message for f in fs if f.rule == "earned.price_changed")


def test_decline_changes_nothing_and_restarts_the_record():
    m = mandate()
    history()
    [p] = ramp.status(m)["proposals"]
    c = TestClient(api.app)
    r = c.post(f"/mandates/M1/autonomy/{p['id']}/decline", headers=H, json={"by": "Mikko", "note": "not yet"})
    assert r.status_code == 200
    assert c.post(f"/mandates/M1/autonomy/{p['id']}/accept", headers=H, json={"by": "Mikko"}).status_code == 409
    assert S._mandate("M1").delegated == [] and ramp.status(S._mandate("M1"))["proposals"] == []
    assert any(e["event"] == "ramp.declined" for e in store.audit_events(subject="M1"))


@pytest.mark.parametrize("rule", ["allergen.unknown", "spend.auto_limit", "spend.daily_limit", "price.comparison_cap",
                                  "origin.restricted", "product.first_time"])
def test_never_proposed_for_allergens_limits_or_hard_caps(rule):
    m = mandate()
    history(n=10, rule=rule)
    assert ramp.status(m)["proposals"] == []
    assert rule not in ramp.RAMPABLE


def test_block_tier_is_never_proposed_nor_relaxed():
    m = mandate(substitutions="block")
    for i in range(10):
        order(1 + i * 3, "approved", [price(rule="substitution", outcome="block")])
    assert ramp.status(m)["proposals"] == []
    m = m.model_copy(update={"delegated": [ramp.Delegation(rule="substitution", scope="type:kuohukerma",
                                                           since=NOW.isoformat())]})
    f = ramp.Finding(rule="substitution", outcome="block", message="no substitutes", product_id="C")
    assert ramp.relax([f], m) == [f]
    assert not any(r.startswith("allergen") or r.startswith("spend") for r in ramp.RAMPABLE)


def test_rejection_after_relax_proposes_revert_and_accepting_it_asks_again():
    m = mandate()
    history()
    [p] = ramp.status(m)["proposals"]
    ramp.accept("M1", p["id"], "Mikko")
    m = S._mandate("M1")
    assert ramp.status(m)["proposals"] == []
    monkey_later = NOW + timedelta(days=2)
    # an order held for another reason carried the delegated scope's earned.* finding; the buyer rejected it
    oid = order(-2, "rejected", [{"rule": "earned.price_changed", "outcome": "notify", "message": "x",
                                  "product_id": "C"},
                                 {"rule": "spend.auto_limit", "outcome": "ask", "message": "y"}])
    [rv] = ramp.status(m, now=monkey_later)["proposals"]
    assert (rv["kind"], rv["to_tier"], rv["orders"]) == ("revert", "ask", [oid])
    assert "you rejected order" in rv["evidence"]
    ramp.accept("M1", rv["id"], "Mikko")
    assert S._mandate("M1").delegated == []


def test_buyer_signed_mandate_is_not_rewritten_by_the_gateway():
    m = mandate()
    history()
    [p] = ramp.status(m)["proposals"]
    row = store.get_mandate("M1")
    jws = row.pop("_jws")
    store.put_mandate({**row, "credential_sha256": "x" * 64}, jws)
    real = S._signed_mandate
    try:
        S._signed_mandate = lambda mid: {**real(mid), "credential_sha256": "x" * 64}
        with pytest.raises(S.GatewayError) as e:
            ramp.accept("M1", p["id"], "Mikko")
    finally:
        S._signed_mandate = real
    assert e.value.code == "mandate_buyer_signed"


def test_every_earned_rule_is_registered_in_both_languages():
    reg = {r["id"]: r for r in R.register()}
    for rule in ramp.RAMPABLE.values():
        assert reg[rule]["tiers"] == ["notify"] and reg[rule]["text_fi"]
    assert set(ramp.WHAT) == set(ramp.RAMPABLE)


# --- accepting widens autonomy: the mandate's own approval rules apply (review2) ------------------------------

def _proposal(**kw):
    m = mandate(**kw)
    history()
    [p] = ramp.status(m)["proposals"]
    return p


def _delegated():
    return S._mandate("M1").delegated


def test_an_agent_credential_can_never_accept_or_decline():
    from sous import agent_auth
    p = _proposal()
    tok = agent_auth.mint(S._mandate("M1"), "owner")["token"]
    c = TestClient(api.app)
    for verb in ("accept", "decline"):
        for h in ({"Authorization": f"Bearer {tok}", **H}, {"x-approver-code": tok}):
            r = c.post(f"/mandates/M1/autonomy/{p['id']}/{verb}", headers=h, json={"by": "Mikko"})
            assert r.status_code == 403 and r.json()["error"] == "agent_cannot_approve", r.text
    assert _delegated() == []
    assert "agent.approval_attempt_refused" in [e["event"] for e in store.audit_events(subject="M1")]
    assert "ramp.declined" not in [e["event"] for e in store.audit_events(subject="M1")]


def test_a_passkey_only_mandate_is_not_widened_with_the_approver_code():
    p = _proposal(approval_method="passkey")
    r = TestClient(api.app).post(f"/mandates/M1/autonomy/{p['id']}/accept", headers=H, json={"by": "Mikko"})
    assert r.status_code == 403 and r.json()["error"] == "passkey_required", r.text
    assert "re-signs the mandate" in r.json()["message"] and _delegated() == []
    r = TestClient(api.app).post(f"/mandates/M1/autonomy/{p['id']}/decline", headers=H, json={"by": "Mikko"})
    assert r.status_code == 200  # declining widens nothing


def test_four_eyes_the_requester_cannot_accept_a_relax():
    from sous import agent_auth
    p = _proposal(separate_approver=True)
    agent_auth.mint(S._mandate("M1"), "Mikko")  # Mikko set the agent to work: its orders are requested by him
    c = TestClient(api.app)
    r = c.post(f"/mandates/M1/autonomy/{p['id']}/accept", headers=H, json={"by": " mikko "})
    assert r.status_code == 403 and r.json()["error"] == "separate_approver_required", r.text
    assert "proposal" in r.json()["message"] and _delegated() == []
    r = c.post(f"/mandates/M1/autonomy/{p['id']}/accept", headers=H, json={"by": "Anna"})
    assert r.status_code == 200, r.text
    assert [d.rule for d in _delegated()] == ["price.changed"]
