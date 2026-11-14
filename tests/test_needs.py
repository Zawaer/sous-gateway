"""Needs changed mid-contract (sous/needs.py): a ward goes nut-free or gluten-free, and everything already running
on the old constraints gets a visible control step. Offline: catalog and substitute lookups are stubbed."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from sous import api, catalog, clearance, config, needs, remembered, rules as R, service as S, signing, standing, \
    standing_store as SS, store
from sous.mandate import Mandate

NOW = datetime(2026, 11, 14, 10, 0, tzinfo=config.TZ)
H = {"x-approver-code": "c0de"}
PRODUCTS: dict[str, dict] = {}


def product(pid, name, contains=(), may=(), free=(), claims=(), declared=True):
    return {"id": pid, "name": name, "names": {"fi": "", "en": "", "sv": ""}, "brand": "", "vendor": "",
            "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1},
            "allowed_lot_size": 1, "temperature": "chilled", "labels": [], "claims": list(claims),
            "allergens": {"declared": declared, "contains": list(contains), "may_contain": list(may),
                          "free_from": list(free)},
            "size_from_name": None, "delivery_reliability": 0.99, "official_substitutes": [], "deleted": False}


def mandate(**kw):
    return Mandate(**{"id": "M", "buyer": "Hospital", "agent_id": "a", "issued_by": "chef",
                      "avoid_allergens": ["AM"], **kw})


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(config, "APPROVER_CODE", "c0de")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    PRODUCTS.clear()
    PRODUCTS.update({
        "GRA": product("GRA", "GRANOLA 500G", contains=["AN", "AW"]),
        "OAT": product("OAT", "Oat flakes 1 kg", contains=["AW"]),
        "RICE": product("RICE", "Rice 1 kg", free=["X99"]),
        "BAR": product("BAR", "Seed bar", may=["AN"]),
        "MYST": product("MYST", "Mystery muesli"),  # declares nothing at all
        "MILKFREE": product("MILKFREE", "Oat drink 1 l", contains=["AW"], free=["AM"]),
    })
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(pid))
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(remembered, "_now", lambda: NOW)
    monkeypatch.setattr(clearance, "_now", lambda: NOW)
    monkeypatch.setattr(S.notify, "send", lambda *a, **k: None)
    monkeypatch.setattr(needs, "_substitute", lambda pid, m, when, amount, unit: None)
    S.issue_mandate(mandate())
    yield
    signing._key.cache_clear()


def standing_order(name, *pids):
    return standing.create(standing.StandingOrderIn(mandate_id="M", agent_id="a", name=name, items=[
        {"product_ids": [p], "amount": 10, "unit": "SALES_UNIT"} for p in pids]))


def order(oid, status, pid, delivery="2026-11-17"):
    o = {"id": oid, "quote_id": "Q-" + oid, "mandate_id": "M", "agent_id": "a", "buyer": "Hospital", "status": status,
         "total_ex_vat": 10.0, "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
         "delivery_date": delivery,
         "lines": [{"product_id": pid, "name": PRODUCTS[pid]["name"], "quantity": 2, "sales_unit": "ST",
                    "line_total_ex_vat": 10.0, "delivery_date": delivery}],
         "decision": {"outcome": "ask" if status == "pending_approval" else "silent", "findings": [],
                      "by": "mandate-engine", "at": NOW.isoformat()}}
    store.insert_order(o)
    return o


def remember(pid, rule="product.first_time"):
    r = {"id": "REM-" + pid, "mandate_id": "M", "buyer": "Hospital", "rule": rule, "product_id": pid,
         "original_product_id": None, "name": PRODUCTS[pid]["name"],
         "fingerprints": {pid: remembered.fingerprint(PRODUCTS[pid])}, "by": "chef", "authority": "code",
         "order_id": "O-old", "active": True, "created_at": NOW.isoformat(timespec="seconds"),
         "expires_at": (NOW + timedelta(days=30)).isoformat(timespec="seconds")}
    remembered._put(remembered._sign(r))
    return r


def clear(pid, allergens=("AM",)):
    c = {"id": "CLR-" + pid, "mandate_id": "M", "buyer": "Hospital", "product_id": pid, "name": PRODUCTS[pid]["name"],
         "allergens": list(allergens), "fingerprint": clearance.fingerprint(PRODUCTS[pid]), "by": "chef",
         "authority": "code", "order_id": "O-old", "note": None, "active": True,
         "created_at": NOW.isoformat(timespec="seconds"),
         "expires_at": (NOW + timedelta(days=90)).isoformat(timespec="seconds")}
    clearance._put(c)
    return c


def events(name):
    return [e for e in store.audit_events(limit=500) if e["event"] == name]


def tighten(note="Ward 3 is now nut-free", **kw):
    r = TestClient(api.app).post("/mandates", params={"change_note": note} if note else {}, headers=H,
                                 json=mandate(**{"avoid_allergens": ["AM", "AN"], **kw}).model_dump(mode="json"))
    assert r.status_code == 200, r.text
    return r.json()


# --- 1. what is re-checked --------------------------------------------------------------------------------------

def test_standing_order_with_nuts_is_held_with_a_clear_reason():
    granola = standing_order("Breakfast", "GRA", "OAT")
    rice = standing_order("Lunch", "RICE")
    rev = tighten()["needs_review"]
    so = SS.get(granola["id"])
    assert so["active"] is False and so["needs_hold"]["review_id"] == rev["id"]
    f = so["needs_hold"]["findings"][0]
    assert f["rule"] == "needs.changed" and f["outcome"] == "ask" and f["product_id"] == "GRA"
    assert f["message"] == ("Ward 3 is now nut-free since 14 Nov: the standing order Breakfast for Granola 500g "
                            "contains tree nuts.")
    held = rev["held"][0]
    assert held["id"] == granola["id"] and "pähkinät" in held["findings"][0]["message_fi"]
    assert SS.get(rice["id"])["active"] is True  # unaffected keeps running
    assert {"kind": "standing_order", "id": rice["id"], "name": "Lunch"} in rev["unaffected"]


def test_held_order_gets_the_finding_and_cannot_be_approved_into_nuts():
    order("O-1", "pending_approval", "GRA")
    order("O-2", "pending_approval", "RICE")
    rev = tighten()
    o = store.get_order("O-1")
    assert o["status"] == "pending_approval" and o["decision"]["outcome"] == "ask"
    assert [f["rule"] for f in o["decision"]["findings"]] == ["needs.changed"]
    assert "the held order O-1 for Granola 500g contains tree nuts" in o["decision"]["findings"][0]["message"]
    assert signing.verify(o["receipt_jws"], "sous-order+jws")["decision"]["findings"][0]["rule"] == "needs.changed"
    assert store.get_order("O-2")["decision"]["findings"] == []
    assert [x["id"] for x in rev["needs_review"]["held"]] == ["O-1"]


def test_upcoming_ordered_order_is_flagged_never_changed():
    order("O-3", "ordered", "BAR", delivery="2026-11-17")
    order("O-4", "ordered", "GRA", delivery="2026-11-10")  # delivered already: nothing to stop
    before = store.get_order("O-3")
    rev = tighten()["needs_review"]
    assert store.get_order("O-3") == before  # not changed, not cancelled
    assert [x["id"] for x in rev["flagged"]] == ["O-3"]
    assert "may contain tree nuts" in rev["flagged"][0]["findings"][0]["message"]
    assert "Valio Aimo" in rev["flagged"][0]["next_step"]
    assert all(x["id"] != "O-4" for k in ("held", "flagged", "unaffected") for x in rev[k])
    assert [e["subject"] for e in events("needs.changed") if e["subject"].startswith("O-")] == ["O-3"]


def test_unsafe_remembered_decisions_and_clearances_are_revoked_with_audit():
    remember("GRA")
    remember("RICE")
    clear("MYST")       # cleared for milk only; nothing says it is nut-free
    clear("MILKFREE")   # declares its allergens: fine for nuts
    rev = tighten()["needs_review"]
    assert {(x["kind"], x["id"]) for x in rev["revoked"]} == {("remembered", "REM-GRA"), ("clearance", "CLR-MYST")}
    assert remembered.get("REM-GRA")["active"] is False and remembered.get("REM-RICE")["active"] is True
    assert clearance.get("CLR-MYST")["active"] is False
    assert [e["data"]["authority"] for e in events("remembered.revoked")] == ["needs.changed"]
    assert [e["data"]["id"] for e in events("allergen.clearance_revoked")] == ["CLR-MYST"]
    assert "the remembered approval for Granola 500g is revoked: it contains tree nuts" in \
        rev["revoked"][0]["findings"][0]["message"] + rev["revoked"][1]["findings"][0]["message"]


def test_new_required_claim_counts_too():
    standing_order("Coeliac ward", "OAT")
    rev = tighten(note=None, avoid_allergens=["AM"], required_claims=["FREE_FROM_GLUTEN"])["needs_review"]
    msg = rev["held"][0]["findings"][0]["message"]
    assert msg.startswith("Hospital's mandate must now be gluten-free since 14 Nov: ")
    assert msg.endswith("is not marked gluten-free.")


# --- 2. one summary, stored, audited, on the dashboard ---------------------------------------------------------

def test_single_summary_fi_en_stored_audited_and_notified():
    standing_order("Breakfast", "GRA")
    standing_order("Lunch", "RICE")
    rev = tighten()["needs_review"]
    assert rev["summary"].startswith("Ward 3 is now nut-free since 14 Nov. Re-checked everything running under "
                                     "mandate M: 1 held for you")
    assert "1 unaffected and still running" in rev["summary"]
    assert "14.11.2026 alkaen" in rev["summary_fi"] and "pysäytetty" in rev["summary_fi"]
    c = TestClient(api.app)
    got = c.get("/mandates/M/needs-reviews", headers=H).json()["needs_reviews"]
    assert [r["id"] for r in got] == [rev["id"]]
    assert [e["subject"] for e in events("needs.changed")] == ["M"]
    feed = c.get("/notifications", headers=H).json()["notifications"]
    assert any(n["event"] == "needs.changed" and n["tier"] == "ask" for n in feed)


def test_unrelated_update_runs_no_review():
    standing_order("Breakfast", "GRA")
    r = TestClient(api.app).post("/mandates", headers=H, json=mandate(auto_approve_limit_eur=900).model_dump(mode="json"))
    assert "needs_review" not in r.json() and needs.list_for("M") == []


def test_substitute_is_a_proposal_only(monkeypatch):
    monkeypatch.setattr(needs, "_substitute", lambda pid, m, when, amount, unit:
                        {"product_id": "OAT", "name": "Oat flakes 1 kg", "tier": "good", "compromises": []}
                        if pid == "GRA" else None)
    so = standing_order("Breakfast", "GRA")
    rev = tighten()["needs_review"]
    assert rev["substitutes"] == [{"for_product_id": "GRA", "for": so["id"], "proposal": {
        "product_id": "OAT", "name": "Oat flakes 1 kg", "tier": "good", "compromises": []},
        "next_step": "Quote it with substitute_for=GRA; the mandate decides."}]
    assert SS.get(so["id"])["items"][0]["product_ids"] == ["GRA"]  # nothing swapped in
    assert needs.list_for("M")[0]["substitutes"] == rev["substitutes"]


def test_substitute_lookup_failure_keeps_the_hold(monkeypatch):
    def boom(*a):
        raise RuntimeError("Valio unreachable")
    monkeypatch.setattr(needs, "_substitute", boom)
    so = standing_order("Breakfast", "GRA")
    rev = tighten()["needs_review"]
    assert SS.get(so["id"])["active"] is False and rev["substitutes"][0]["proposal"] is None


# --- 3. loosening never expands anything ----------------------------------------------------------------------

def test_loosening_keeps_holds_and_revocations():
    so = standing_order("Breakfast", "GRA")
    order("O-1", "pending_approval", "GRA")
    remember("GRA")
    tighten()
    r = TestClient(api.app).post("/mandates", headers=H, json=mandate().model_dump(mode="json")).json()
    rev = r["needs_review"]
    assert rev["change"]["tightened"] == [] and rev["change"]["loosened"][0]["codes"] == ["AN"]
    assert rev["held"] == rev["revoked"] == [] and "Nothing was expanded automatically" in rev["summary"]
    assert SS.get(so["id"])["active"] is False
    assert remembered.get("REM-GRA")["active"] is False
    assert [f["rule"] for f in store.get_order("O-1")["decision"]["findings"]] == ["needs.changed"]


def test_only_a_human_resume_clears_the_hold():
    so = standing_order("Breakfast", "GRA")
    rev = tighten()["needs_review"]
    out = standing.set_active(so["id"], True, "head chef")
    assert out["active"] is True and "needs_hold" not in SS.get(so["id"])
    assert events("standing.resumed")[0]["data"]["needs_review_id"] == rev["id"]


def test_buyer_signed_update_runs_the_same_review(monkeypatch):
    from sous import credentials as C
    monkeypatch.setattr(config, "BUYER_KEY_TOFU", True)
    key = C.new_key()
    body = {"id": "M-VC", "buyer": "Kide", "agent_id": "a", "issued_by": "Aino", "avoid_allergens": ["AM"]}
    S.issue_credential_mandate(C.sign_vc(key, body, NOW - timedelta(hours=2), NOW + timedelta(days=30)))
    so = standing.create(standing.StandingOrderIn(mandate_id="M-VC", agent_id="a", name="Breakfast",
                                                  items=[{"product_ids": ["GRA"], "amount": 1}]))
    out = S.issue_credential_mandate(C.sign_vc(key, {**body, "avoid_allergens": ["AM", "AN"]},
                                               NOW - timedelta(hours=1), NOW + timedelta(days=30)))
    assert [x["id"] for x in out["needs_review"]["held"]] == [so["id"]] and SS.get(so["id"])["active"] is False


# --- 4. the rule is registered ---------------------------------------------------------------------------------

def test_rule_registered_with_finnish():
    r = R.get("needs.changed")
    assert r["tiers"] == ["ask"] and r["owner"] == "buyer" and r["text_fi"]
