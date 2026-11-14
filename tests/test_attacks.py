"""The "Try to break it" attacks (sous/attacks.py, POST /sandbox/{id}/attacks/{name}), offline: test_sandbox.py's
temp DB, mocked Valio (every live call is counted) and fixed clock, plus allergen data for the two seeded products."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from sous import api, attacks, catalog, rules, sandbox, service as S, signing, store

from test_sandbox import NOW, OAT, env, new  # noqa: F401  (env is autouse)

MILK_P = {**OAT, "id": "464201", "name": "Valio semi skimmed milk 1 l lactose free UHT",
          "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": []}}
BUTTER_P = {**MILK_P, "id": "464411", "name": "Valio butter 500 g unsalted", "size_from_name": (0.5, "KG")}


@pytest.fixture(autouse=True)
def products(monkeypatch):
    by = {"OAT": OAT, "464201": MILK_P, "464411": BUTTER_P}
    monkeypatch.setattr(catalog, "get", lambda pid: by.get(pid))


@pytest.fixture
def c():
    return TestClient(api.app)


def attack(c, sbx, name, token=None, header="Authorization"):
    tok = token or sbx["agent_token"]
    value = f"Bearer {tok}" if header == "Authorization" else tok
    return c.post(f"/sandbox/{sbx['sandbox']['id']}/attacks/{name}", headers={header: value})


def check_common(c, sbx, r, name, result, rule, env):  # noqa: F811
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["attack"] == name and d["result"] == result, d
    assert d["result"] == attacks.RESULTS[d["tier"]], (d["result"], d["tier"])  # the label says what the tier means
    assert d["rule_ids"][0] == rule, d["rule_ids"]
    assert all(rules.get(i) for i in d["rule_ids"]) and d["rules"][0]["url"].endswith(f"/rules/{rule}")
    sid = sbx["sandbox"]["id"]
    events = {e["seq"]: e for e in store.audit_events(limit=500)}
    assert d["audit_event_id"] in d["audit_event_ids"]
    demo = events[d["audit_event_id"]]
    assert demo["event"] == attacks.EVENT and demo["subject"] == sid and demo["data"]["result"] == result
    assert env.calls == 0 and c.get(f"/sandbox/{sid}").json()["quotes_used"] == 0  # no Valio, no lookup budget
    for o in d["orders"]:  # real sandbox orders, labelled, linking into the sandbox's seller UI
        stored = store.get_order(o["id"])
        assert stored["attack_demo"]["attack"] == name and f"?sandbox={sid}#/orders/{o['id']}" in o["order_url"]
        assert sandbox.owner(stored["mandate_id"]) == sid
        assert signing.verify(stored["receipt_jws"], "sous-order+jws")["id"] == o["id"]
    for qid in d["quote_ids"]:
        q = signing.verify(store.get_quote(qid)["_jws"], "sous-quote+jws")  # every quote verifies afterwards
        assert q["seeded"]["attack_demo"] == name and q["valio_api_calls"]["price_calls"] == 0
    assert store.verify_chain()["ok"]
    return d


def test_self_approve_is_refused_and_audited(c, env):  # noqa: F811
    s = new(c)
    d = check_common(c, s, attack(c, s, "self_approve"), "self_approve", "refused", "agent_cannot_approve", env)
    assert d["http_status"] == 403 and d["error_code"] == "agent_cannot_approve"
    oid = d["orders"][0]["id"]
    assert store.get_order(oid)["status"] == "pending_approval"
    ev = [e for e in store.audit_events(oid) if e["event"] == "agent.approval_attempt_refused"]
    assert ev and ev[0]["seq"] in d["audit_event_ids"] and s["agent_token"] not in str(ev)
    # a human with the sandbox approver token can still decide it
    r = c.post(f"/orders/{oid}/reject", json={"by": "judge"}, headers={"X-Approver-Code": s["approver_token"]})
    assert r.status_code == 200 and r.json()["status"] == "rejected"


def test_slice_crossing_order_is_held_by_the_split_rule(c, env, monkeypatch):  # noqa: F811
    s = new(c)
    d = check_common(c, s, attack(c, s, "slice"), "slice", "held for approval", "spend.split_orders", env)
    statuses = [o["status"] for o in d["orders"]]
    assert statuses == ["ordered", "pending_approval"], statuses  # €468 + €468 within the hour > €500
    assert all(o["total_ex_vat"] <= 500 for o in d["orders"])
    assert "within 60 min" in d["explanation"]
    # again: the first slice is still in the window, so the next one is held at once
    d2 = check_common(c, s, attack(c, s, "slice"), "slice", "held for approval", "spend.split_orders", env)
    assert [o["status"] for o in d2["orders"]] == ["pending_approval"]
    # an hour later the window is empty again: one slice goes through, the next is held
    monkeypatch.setattr(S, "_now", lambda: NOW + timedelta(minutes=61))
    d3 = check_common(c, s, attack(c, s, "slice"), "slice", "held for approval", "spend.split_orders", env)
    assert [o["status"] for o in d3["orders"]] == ["ordered", "pending_approval"]


def test_injection_changes_nothing_and_is_flagged(c, env):  # noqa: F811
    s = new(c)
    d = check_common(c, s, attack(c, s, "injection"), "injection", "held for approval", "spend.auto_limit", env)
    plain, inj = (store.get_order(o["id"]) for o in d["orders"])
    assert plain["decision"]["outcome"] == inj["decision"]["outcome"] == "ask" == d["tier"]
    assert d["injection_changed_decision"] is False
    assert inj["agent_rationale"] == attacks.INJECTION and d["agent_claim"]["suspicious"]
    r = c.get(f"/orders/{inj['id']}", headers={"Authorization": f"Bearer {s['agent_token']}"})
    assert r.json()["agent_claim"]["suspicious"] is True


def test_tampered_quote_is_refused_and_put_back(c, env):  # noqa: F811
    s = new(c)
    d = check_common(c, s, attack(c, s, "tamper_quote"), "tamper_quote", "refused", "quote_signature", env)
    assert d["http_status"] == 409 and d["error_code"] == "quote_signature" and not d["orders"]
    row = store.get_quote(d["quote_ids"][0])
    assert row["_order_id"] is None and row["total_ex_vat"] > 1.0  # the genuine quote is back, unused


def test_replay_by_another_agent_never_gets_the_first_order(c, env):  # noqa: F811
    s = new(c)
    d = check_common(c, s, attack(c, s, "replay_other_agent"), "replay_other_agent", "refused", "quote_mismatch", env)
    assert d["http_status"] == 409 and len(d["orders"]) == 1
    school = next(m["agent_id"] for m in s["mandates"] if m["scenario"] == "school")
    assert not [o for o in store.list_orders() if o["agent_id"] == school]


def test_over_limit_is_refused_by_the_hard_limit(c, env):  # noqa: F811
    s = new(c)
    d = check_common(c, s, attack(c, s, "over_limit"), "over_limit", "refused", "spend.hard_limit", env)
    o = store.get_order(d["orders"][0]["id"])
    assert o["status"] == "refused" and o["total_ex_vat"] > 1500


def test_wrong_allergen_is_refused(c, env):  # noqa: F811
    s = new(c)
    d = check_common(c, s, attack(c, s, "wrong_allergen"), "wrong_allergen", "refused", "allergen.contains", env)
    assert store.get_order(d["orders"][0]["id"])["status"] == "refused"


def test_approver_token_runs_attacks_but_self_approve_needs_the_agent_token(c, env):  # noqa: F811
    s = new(c)
    r = attack(c, s, "over_limit", token=s["approver_token"], header="X-Approver-Code")
    d = check_common(c, s, r, "over_limit", "refused", "spend.hard_limit", env)
    assert d["credential"] == "approver"
    r = attack(c, s, "self_approve", token=s["approver_token"])
    assert r.status_code == 400 and r.json()["error"] == "agent_token_needed"


def test_access_and_rate_limit(c, env, monkeypatch):  # noqa: F811
    s, other = new(c), new(c, ip="10.0.0.2")
    sid = s["sandbox"]["id"]
    assert c.post(f"/sandbox/{sid}/attacks/slice").status_code == 401
    assert attack(c, s, "slice", token=other["agent_token"]).status_code == 403  # another sandbox's token
    assert attack(c, s, "slice", token=other["approver_token"]).status_code == 403
    assert attack(c, s, "nope").status_code == 422
    monkeypatch.setattr(attacks, "PER_HOUR", 2)
    assert attack(c, s, "tamper_quote").status_code == 200
    assert attack(c, s, "tamper_quote").status_code == 200
    r = attack(c, s, "tamper_quote")
    assert r.status_code == 429 and r.json()["error"] == "attack_rate_limited"
    assert attack(c, other, "tamper_quote").status_code == 200  # per sandbox


def test_quickstart_links_the_page(c):
    s = new(c)
    assert s["quickstart"]["break_ui"].endswith(f"/ui/break.html?sandbox={s['sandbox']['id']}")


def test_attacks_leave_the_judges_mandates_spend_free(c, env):  # noqa: F811
    """Orders that could go through run under the sandbox's attack-demo mandate, created on first use."""
    from sous import service as S
    s = new(c)
    sid = s["sandbox"]["id"]
    for name in attacks.ATTACKS:
        assert attack(c, s, name).status_code == 200, name
    day = S._now().date().isoformat()
    for m in s["mandates"]:  # hotel, hospital, school: nothing ordered by a demo
        assert store.spent_today(m["mandate_id"], day) == 0, m["scenario"]
    demo = f"M-ATTACK-DEMO-{sid}"
    dm = S._mandate(demo)
    assert dm.sandbox == sid and dm.buyer == next(m["buyer"] for m in s["mandates"] if m["scenario"] == "hotel")
    assert (dm.auto_approve_limit_eur, dm.daily_limit_eur) == (500, 1500)
    assert store.spent_today(demo, day) > 0
    assert sandbox.owner(demo) == sid
    listed = c.get(f"/sandbox/{sid}").json()["mandates"]
    assert [m["mandate_id"] for m in listed if m["scenario"] == attacks.DEMO] == [demo]
    for tok in (s["agent_token"], s["approver_token"]):  # readable with either sandbox token
        assert c.get(f"/mandates/{demo}", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert c.get(f"/mandates/{demo}").status_code == 404
    assert len([e for e in store.audit_events(sid) if e["event"] == "sandbox.attack_mandate_created"]) == 1
