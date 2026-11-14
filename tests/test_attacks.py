"""The "Try to break it" attacks (sous/attacks.py, POST /sandbox/{id}/attacks/{name}), offline: test_sandbox.py's
temp DB, mocked Valio (every live call is counted) and fixed clock, plus allergen data for the two seeded products."""

import pytest
from fastapi.testclient import TestClient

from sous import api, attacks, catalog, rules, sandbox, signing, store

from test_sandbox import OAT, env, new  # noqa: F401  (env is autouse)

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


def test_slice_crossing_order_is_held_by_the_daily_limit(c, env):  # noqa: F811
    s = new(c)
    d = check_common(c, s, attack(c, s, "slice"), "slice", "held", "spend.daily_limit", env)
    statuses = [o["status"] for o in d["orders"]]
    assert statuses == ["ordered", "ordered", "ordered", "pending_approval"], statuses
    assert all(o["total_ex_vat"] < 2500 for o in d["orders"])
    # again: today's spend is already high, so the first slice is held at once
    d2 = check_common(c, s, attack(c, s, "slice"), "slice", "held", "spend.daily_limit", env)
    assert [o["status"] for o in d2["orders"]] == ["pending_approval"]


def test_injection_changes_nothing_and_is_flagged(c, env):  # noqa: F811
    s = new(c)
    d = check_common(c, s, attack(c, s, "injection"), "injection", "blocked", "mandate.ok", env)
    plain, inj = (store.get_order(o["id"]) for o in d["orders"])
    assert plain["decision"]["outcome"] == inj["decision"]["outcome"] == "silent"
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
