"""Judge sandbox (sous/sandbox.py): creation, isolation, scoped approvals, Valio budget, expiry.

Offline: the catalog is one product and Valio Aimo is an httpx.MockTransport, so live calls are counted.
"""

import json
import time
from datetime import date, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from sous import aimo, api, catalog, config, notify, quote as Q, sandbox, service as S, signing, store
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
OAT = {
    "id": "OAT", "name": "Oat drink 1 l", "names": {"fi": "", "en": "", "sv": ""}, "brand": "", "vendor": "",
    "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
    "allowed_lot_size": 1.0, "temperature": "chilled", "labels": [], "claims": [],
    "allergens": {"declared": True, "contains": [], "may_contain": [],
                  "free_from": ["AM", "AN", "AP", "AW", "AE"]},
    "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
    "official_substitutes": [], "deleted": False,
}
CODE = "1234"


class Valio:
    """Mock Valio Aimo: every product costs €1.00/L, plenty in stock; counts requests."""

    def __init__(self):
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        ids = json.loads(request.content)["products"]
        if request.url.path.endswith("/price"):
            items = [{"productId": p, "catalogPrice": {"value": 1.0, "valueWithTax": 1.14, "validTo": None},
                      "comparisonPrice": {"value": 1.0}, "comparisonUnit": "L", "tax": {"taxRate": 13.5},
                      "error": False} for p in ids]
        else:
            items = [{"productId": p, "logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200",
                      "salesBlock": False, "scheduleLines": [{"salesBlock": False, "availableAmount": 99999,
                                                              "availableDate": "2026-11-16", "salesUnit": "ST"}]}
                     for p in ids]
        return httpx.Response(200, json={"timeNow": "2026-11-13T21:00:00+0200", "products": items})


def offers_at_now(product_ids, requested, need=None, need_unit=None):
    """quote.offers with live (mocked) Valio calls but a fixed clock."""
    products = {pid: p for pid in product_ids if (p := catalog.get(pid))}
    prices, avail = aimo.prices(list(products), requested), aimo.availability(list(products), requested)
    return [Q.build_offer(p, prices.get(i), avail.get(i), requested, need, need_unit, NOW)
            for i, p in products.items()], NOW


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(config, "REQUIRE_AGENT_AUTH", False)
    monkeypatch.setattr(config, "APPROVER_CODE", CODE)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://sous.test")
    monkeypatch.setattr(config, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(config, "SANDBOX_QUOTES", 15)
    monkeypatch.setattr(config, "SANDBOX_CALLS_PER_MIN", 30)
    monkeypatch.setattr(config, "SANDBOX_PER_IP_HOUR", 3)
    monkeypatch.setattr(config, "SANDBOX_MAX_ACTIVE", 50)
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: OAT if pid == "OAT" else None)
    monkeypatch.setattr(catalog, "search", lambda *a, **k: [OAT])  # CI has no catalog data
    from scripts.seed import MANDATES  # OAT counts as the demo buyers' usual product (first-time rule)
    for m in MANDATES:
        monkeypatch.setattr(m, "usual_products", m.usual_products + ["OAT"])
    monkeypatch.setattr(Q, "offers", offers_at_now)
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(notify, "_deliver", lambda *a, **k: None)  # feed only, never a real push
    monkeypatch.setattr(sandbox, "_last_sweep", 0.0)
    monkeypatch.setenv("SOUS_TRUST_CF_HEADER", "1")
    valio = Valio()
    monkeypatch.setattr(aimo, "_client", httpx.Client(base_url="https://aimo.test/api",
                                                      transport=httpx.MockTransport(valio)))
    aimo.reset()
    api._hits.clear()  # the per-IP POST limit is process-wide; don't eat other tests' budget
    yield valio
    api._hits.clear()
    aimo.reset()
    signing._key.cache_clear()


@pytest.fixture
def c():
    return TestClient(api.app)


def seed_curated():
    from scripts import seed
    for m in seed.MANDATES:
        S.issue_mandate(m)


def new(c, ip="10.0.0.1"):
    r = c.post("/sandbox", headers={"cf-connecting-ip": ip})
    assert r.status_code == 200, r.text
    return r.json()


def quote(c, sbx, scenario="hotel", amount=10, day="2026-11-16", token=None):
    m = next(x for x in sbx["mandates"] if x["scenario"] == scenario)
    body = {"agent_id": m["agent_id"], "mandate_id": m["mandate_id"], "delivery_date": day,
            "items": [{"product_ids": ["OAT"], "amount": amount, "unit": "L"}]}
    return c.post("/quotes", json=body, headers={"Authorization": f"Bearer {token or sbx['agent_token']}"})


def order(c, sbx, scenario="hotel", amount=10, day="2026-11-16"):
    q = quote(c, sbx, scenario, amount, day)
    assert q.status_code == 200, q.text
    m = next(x for x in sbx["mandates"] if x["scenario"] == scenario)
    r = c.post("/orders", json={"agent_id": m["agent_id"], "mandate_id": m["mandate_id"],
                                "quote_id": q.json()["quote"]["id"]},
               headers={"Authorization": f"Bearer {sbx['agent_token']}"})
    assert r.status_code == 200, r.text
    return r.json()


# --- creation -------------------------------------------------------------------

def test_create_gives_signed_mandate_copies_tokens_and_quickstarts(c):
    seed_curated()
    s = new(c)
    sid = s["sandbox"]["id"]
    assert sandbox.ID_RE.match(sid)
    ids = {m["scenario"]: m["mandate_id"] for m in s["mandates"]}
    assert ids == {"hotel": f"M-HOTEL-AURORA-{sid}", "hospital": f"M-HOSPITAL-KITCHEN-{sid}",
                   "school": f"M-SCHOOL-ESPOO-{sid}"}
    hotel = S._mandate(ids["hotel"])  # signed
    assert hotel.sandbox == sid and hotel.require_agent_auth and hotel.agent_id == f"aurora-purchasing-agent-{sid.lower()}"
    assert hotel.auto_approve_limit_eur == 2500 and f"(sandbox {sid})" in hotel.buyer
    assert 23.9 * 3600 < (hotel.valid_until - datetime.now(config.TZ)).total_seconds() <= 24 * 3600
    token, approver = s["agent_token"], s["approver_token"]
    assert token.startswith("vka_") and approver.startswith("sbxa_")
    qs = s["quickstart"]
    assert qs["claude_code"] == (f'claude mcp add --transport http sous https://sous.test/mcp '
                                 f'--header "Authorization: Bearer {token}"')
    assert qs["connect"]["url"] == "https://sous.test/connect/mcp"  # OAuth connectors (claude.ai, ChatGPT)
    assert token not in json.dumps(qs["connect"])
    assert qs["seller_ui"].startswith(f"https://sous.test/ui/?sandbox={sid}")
    assert qs["buyer_ui"].startswith(f"https://sous.test/buyer/?sandbox={sid}&mandate=M-")
    assert token not in qs["buyer_ui"]  # the agent token is pasted separately, never put in a link
    assert approver in qs["curl_approve"] and token in qs["curl_quote"]
    assert len(s["prompts"]) == 4 and all(ids["hotel"] in p["prompt"] or ids["hospital"] in p["prompt"]
                                          or ids["school"] in p["prompt"] for p in s["prompts"])
    # one agent token for all three mandates; none without it; tokens never stored or logged in plain text
    for scenario in ("hotel", "hospital", "school"):
        assert quote(c, s, scenario).status_code == 200
    assert quote(c, s, token="vka_wrong").status_code == 403
    dump = json.dumps(store.audit_events(limit=1000)) + json.dumps(store._exec("SELECT * FROM sandbox")[0])
    assert token not in dump and approver not in dump
    st = c.get(f"/sandbox/{sid.lower()}").json()
    assert st["status"] == "active" and st["quotes_used"] == 1 and st["quotes_left"] == 14  # 2 were cached
    assert "token" not in json.dumps(st)


def test_sandbox_rules_text_names_allergens_not_codes():
    m = Mandate(id="M", buyer="b", agent_id="a", issued_by="x", avoid_allergens=["AM", "AN", "AP"])
    assert "avoids milk, tree nuts, peanuts" in sandbox._rules(m)


def test_sandbox_rules_text_lists_every_active_rule():
    from scripts.seed import MANDATES
    from sous import rules
    hotel = next(m for m in MANDATES if m.id == "M-HOTEL-AURORA")
    text = sandbox._rules(hotel)
    for part in ("FYI above €1,000", "auto-approve up to €2,500", "refused above €10,000", "€8,000 a day",
                 "€25,000 a week", "price cap: milk at €2.50 per L or kg", "20% off the last one paid",
                 "3× the usual quantity", "new products", "substitutes need a human", "under 90%",
                 "deliveries later than asked need a human"):
        assert part in text, (part, text)
    m = Mandate(id="M", buyer="b", agent_id="a", issued_by="x", required_claims=["FREE_FROM_LACTOSE"],
                max_comparison_price_eur={"L": 2.5}, terminal_products="ask", unknown_allergens="block",
                avoid_allergens=["AM"], require_agent_auth=True)
    text = sandbox._rules(m)
    assert "everything sold per L at €2.50/L" in text and "free from lactose" in text
    assert "supplier-shipped products need a human" in text and "no allergen data: refused" in text
    assert "the agent must present its token" in text
    detail = sandbox._rules_detail(m)
    assert all(rules.get(d["rule"]) and d["url"].endswith("/rules/" + d["rule"]) for d in detail)


def test_per_ip_and_global_limits(c, monkeypatch):
    for _ in range(3):
        new(c, "10.0.0.1")
    r = c.post("/sandbox", headers={"cf-connecting-ip": "10.0.0.1"})
    assert r.status_code == 429 and r.json()["error"] == "sandbox_rate_limited"
    assert 3500 < r.json()["retry_after"] <= 3601 and r.headers["Retry-After"]
    new(c, "10.0.0.2")  # another visitor is fine
    monkeypatch.setattr(config, "SANDBOX_MAX_ACTIVE", 4)
    r = c.post("/sandbox", headers={"cf-connecting-ip": "10.0.0.3"})
    assert r.status_code == 429 and r.json()["error"] == "sandbox_full"


def test_disabled(c, monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_ENABLED", False)
    assert c.post("/sandbox").json()["error"] == "sandbox_disabled"


# --- isolation --------------------------------------------------------------------

def test_lists_hide_sandboxes_by_default_and_show_one_with_the_param(c):
    seed_curated()
    c.headers["X-Approver-Code"] = CODE  # the global approver reads everything; sandboxes still hidden by default
    a, b = new(c, "10.0.0.1"), new(c, "10.0.0.2")
    sa, sb = a["sandbox"]["id"], b["sandbox"]["id"]
    curated = c.post("/quotes", json={"agent_id": "aurora-purchasing-agent", "mandate_id": "M-HOTEL-AURORA",
                                      "delivery_date": "2026-11-16",
                                      "items": [{"product_ids": ["OAT"], "amount": 1200, "unit": "L"}]}).json()
    co = c.post("/orders", json={"agent_id": "aurora-purchasing-agent", "mandate_id": "M-HOTEL-AURORA",
                                 "quote_id": curated["quote"]["id"]}).json()
    oa, ob = order(c, a, amount=1200), order(c, b, amount=1200)  # notify tier: feed notifications
    assert oa["status"] == "ordered" and oa["decision"]["outcome"] == "notify"

    assert [o["id"] for o in c.get("/orders").json()["orders"]] == [co["id"]]
    assert {o["id"] for o in c.get("/orders", params={"sandbox": sa}).json()["orders"]} == \
        {oa["id"], a["seeded"]["held_order_id"]}
    assert {o["id"] for o in c.get("/orders", params={"sandbox": sb.lower()}).json()["orders"]} == \
        {ob["id"], b["seeded"]["held_order_id"]}
    assert oa["id"] in [o["id"] for o in c.get("/orders", params={"mandate_id": oa["mandate_id"]}).json()["orders"]]
    assert c.get(f"/orders/{oa['id']}").status_code == 200  # direct links still work

    mids = [m["id"] for m in c.get("/mandates").json()["mandates"]]
    assert "M-HOTEL-AURORA" in mids and not any("SBX" in m for m in mids)
    assert sorted(m["id"] for m in c.get("/mandates", params={"sandbox": sa}).json()["mandates"]) == \
        sorted(m["mandate_id"] for m in a["mandates"])

    def blob(path, **params):
        return json.dumps(c.get(path, params=params).json())
    assert "SBX-" not in blob("/audit", limit=1000) and co["id"] in blob("/audit", limit=1000)
    sa_audit = blob("/audit", sandbox=sa, limit=1000)
    assert oa["id"] in sa_audit and sb not in sa_audit and co["id"] not in sa_audit
    assert "sandbox.created" in sa_audit
    assert oa["id"] in blob("/audit", subject=oa["id"])
    assert c.get("/audit/verify").json()["ok"]  # one chain for everyone

    assert "SBX-" not in blob("/notifications") and co["id"] in blob("/notifications")
    feed = c.get("/notifications", params={"sandbox": sa}).json()["notifications"]
    assert [n["order_id"] for n in feed] == [oa["id"], a["seeded"]["held_order_id"]]  # newest first

    so = c.post("/standing-orders", json={"mandate_id": a["mandates"][0]["mandate_id"],
                                          "agent_id": a["mandates"][0]["agent_id"], "name": "weekly",
                                          "items": [{"product_ids": ["OAT"], "amount": 10, "unit": "L"}]})
    assert so.status_code == 200, so.text
    assert c.get("/standing-orders").json()["standing_orders"] == []
    assert len(c.get("/standing-orders", params={"sandbox": sa}).json()["standing_orders"]) == 2  # + the seeded one

    assert c.get("/orders", params={"sandbox": "nope"}).status_code == 400
    assert c.get("/orders", params={"sandbox": "SBX-ZZZZ"}).json()["orders"] == []


def test_sandbox_mandates_never_push_to_the_demo_phone(monkeypatch):
    monkeypatch.setenv("SOUS_NTFY_TOPIC", "demo-phone")
    from sous.mandate import Mandate
    m = Mandate(id="M-X-SBX-AAAA", buyer="b", agent_id="a", issued_by="h", sandbox="SBX-AAAA")
    assert notify.channels(m, "buyer") == {}
    assert notify.channels(m.model_copy(update={"sandbox": None}), "buyer") == {"ntfy": "demo-phone"}


# --- scoped approvals ----------------------------------------------------------------

def test_sandbox_approver_token_decides_only_its_own_orders(c):
    seed_curated()
    a, b = new(c, "10.0.0.1"), new(c, "10.0.0.2")
    pa = order(c, a, "hospital", amount=600)  # over the hospital's €400: ask
    pb = order(c, b, "hospital", amount=600)
    assert pa["status"] == pb["status"] == "pending_approval"
    q = c.post("/quotes", json={"agent_id": "meilahti-kitchen-agent", "mandate_id": "M-HOSPITAL-KITCHEN",
                                "delivery_date": "2026-11-16",
                                "items": [{"product_ids": ["OAT"], "amount": 600, "unit": "L"}]}).json()
    pc = c.post("/orders", json={"agent_id": "meilahti-kitchen-agent", "mandate_id": "M-HOSPITAL-KITCHEN",
                                 "quote_id": q["quote"]["id"]}).json()
    tok = {"X-Approver-Code": a["approver_token"]}
    assert c.post(f"/orders/{pc['id']}/approve", json={"by": "judge"}, headers=tok).status_code == 401
    assert c.post(f"/orders/{pb['id']}/reject", json={"by": "judge"}, headers=tok).status_code == 401
    assert c.post(f"/orders/{pa['id']}/approve", json={"by": "judge"},
                  headers={"X-Approver-Code": "sbxa_forged"}).status_code == 401
    r = c.post(f"/orders/{pa['id']}/approve", json={"by": "judge"}, headers=tok)
    assert r.status_code == 200 and r.json()["status"] == "ordered"
    assert r.json()["approval"]["authority"] == f"sandbox approver {a['sandbox']['id']}"
    # nothing else a human does
    assert c.post(f"/mandates/{a['mandates'][0]['mandate_id']}/revoke", json={"by": "judge"},
                  headers=tok).status_code == 401
    assert c.delete(f"/sandbox/{a['sandbox']['id']}", headers=tok).status_code == 401
    # the real approver code still decides any order
    assert c.post(f"/orders/{pb['id']}/reject", json={"by": "Rene"},
                  headers={"X-Approver-Code": CODE}).json()["status"] == "rejected"


# --- Valio budget ---------------------------------------------------------------------

def test_per_sandbox_quote_cap_and_cached_offers_still_work(c, monkeypatch, env):
    monkeypatch.setattr(config, "SANDBOX_QUOTES", 2)
    s = new(c)
    assert quote(c, s, day="2026-11-16").status_code == 200
    assert quote(c, s, day="2026-11-17").status_code == 200
    calls = env.calls
    r = quote(c, s, day="2026-11-18")
    assert r.status_code == 429 and r.json()["error"] == "sandbox_limit" and r.json()["retry_after"] > 0
    assert env.calls == calls  # refused before calling Valio
    r = quote(c, s, day="2026-11-17", amount=20)  # same products and day: served from the cache
    assert r.status_code == 200 and r.json()["quote"]["valio_api_calls"]["price_calls"] == 0
    st = c.get(f"/sandbox/{s['sandbox']['id']}").json()
    assert st["quotes_used"] == 2 and st["quotes_left"] == 0
    # another sandbox and the curated demo are unaffected
    assert quote(c, new(c, "10.0.0.9"), day="2026-11-18").status_code == 200


def test_sandboxes_share_a_per_minute_sub_budget(c, monkeypatch):
    seed_curated()
    monkeypatch.setattr(config, "SANDBOX_CALLS_PER_MIN", 2)  # one uncached quote = price + availability
    a, b = new(c, "10.0.0.1"), new(c, "10.0.0.2")
    assert quote(c, a, day="2026-11-16").status_code == 200
    r = quote(c, b, day="2026-11-17")
    assert r.status_code == 429 and r.json()["error"] == "sandbox_limit" and 1 <= r.json()["retry_after"] <= 61
    assert c.get(f"/sandbox/{b['sandbox']['id']}").json()["quotes_used"] == 0
    curated = c.post("/quotes", json={"agent_id": "aurora-purchasing-agent", "mandate_id": "M-HOTEL-AURORA",
                                      "delivery_date": "2026-11-18",
                                      "items": [{"product_ids": ["OAT"], "amount": 10, "unit": "L"}]})
    assert curated.status_code == 200  # the demo keeps the rest of the budget
    assert aimo.health()["sandbox_calls_last_minute"] == 2


def test_substitutes_and_suggestions_are_metered_too(monkeypatch, c):
    s = new(c)
    mid = s["mandates"][0]["mandate_id"]
    m = S._mandate(mid)
    with sandbox.metered(m):
        aimo.prices(["OAT"], date(2026, 11, 20))
    assert c.get(f"/sandbox/{s['sandbox']['id']}").json()["quotes_used"] == 1
    with sandbox.metered(m):  # cached: free
        aimo.prices(["OAT"], date(2026, 11, 20))
    assert c.get(f"/sandbox/{s['sandbox']['id']}").json()["quotes_used"] == 1
    monkeypatch.setattr(config, "SANDBOX_QUOTES", 1)
    with pytest.raises(aimo.SandboxLimit):
        with sandbox.metered(m):
            aimo.prices(["OAT"], date(2026, 11, 21))
    assert S.valio_error(aimo.SandboxLimit("x", retry_after=5)).status == 429


# --- expiry and deletion ----------------------------------------------------------------

def test_expired_sandbox_is_hidden_and_its_mandates_revoked(c, monkeypatch):
    s = new(c)
    sid = s["sandbox"]["id"]
    o = order(c, s, "hospital", amount=600)
    later = time.time() + 25 * 3600
    monkeypatch.setattr(sandbox, "_clock", lambda: later)
    st = c.get(f"/sandbox/{sid}").json()
    assert st["status"] == "expired" and st["orders"]["total"] == 2  # the seeded held order + this one
    assert all(S._mandate(m["mandate_id"]).revoked for m in s["mandates"])
    assert c.get("/orders", params={"sandbox": sid}).status_code == 401  # no credential: 401, as for any id
    assert c.get("/orders", params={"sandbox": sid},
                 headers={"X-Approver-Code": s["approver_token"]}).json()["orders"] == []
    r = quote(c, s)
    assert r.status_code == 410 and r.json()["error"] == "sandbox_expired"
    assert c.post(f"/orders/{o['id']}/approve", json={"by": "judge"},
                  headers={"X-Approver-Code": s["approver_token"]}).status_code == 401
    assert any(e["event"] == "sandbox.expired" for e in store.audit_events(subject=sid))


def test_approver_deletes_a_sandbox(c):
    s = new(c)
    sid = s["sandbox"]["id"]
    assert c.delete(f"/sandbox/{sid}").status_code == 401
    r = c.delete(f"/sandbox/{sid}", params={"by": "Rene"}, headers={"X-Approver-Code": CODE})
    assert r.status_code == 200 and r.json()["status"] == "deleted"
    assert all(S._mandate(m["mandate_id"]).revoked for m in s["mandates"])
    assert c.get("/mandates", params={"sandbox": sid}, headers={"X-Approver-Code": CODE}).json()["mandates"] == []
    assert c.get("/sandbox/SBX-ZZZZ").status_code == 404


# --- nobody else can drain a sandbox, and nobody can hide by naming --------------------------

def test_substitutes_and_suggestions_under_a_sandbox_mandate_need_its_token(c, monkeypatch):
    from sous import substitutes, suggest
    s, other = new(c, "10.0.0.1"), new(c, "10.0.0.2")
    m = next(x for x in s["mandates"] if x["scenario"] == "hospital")
    calls = []
    monkeypatch.setattr(substitutes, "suggest", lambda *a, **k: calls.append(a) or
                        {"constraints": {}, "options": [], "no_safe_substitute": True})
    params = {"delivery_date": "2026-11-16", "amount": 4, "mandate_id": m["mandate_id"], "agent_id": m["agent_id"]}

    def subs(token=None, **extra):
        return c.get("/products/OAT/substitutes", params={**params, **extra},
                     headers={"Authorization": f"Bearer {token}"} if token else {})

    r = subs()
    assert r.status_code == 401 and r.json()["error"] == "agent_auth_required" and not calls
    assert subs(other["agent_token"]).status_code == 404 and not calls  # another sandbox's token: read scope
    assert subs("vka_nope").status_code == 401 and not calls  # a token that grants nothing
    assert subs(s["agent_token"]).status_code == 200
    assert subs(s["approver_token"]).status_code == 200
    assert subs(other["approver_token"]).status_code == 404
    assert len(calls) == 2
    r = c.get(f"/mandates/{m['mandate_id']}/suggestions",
              params={"delivery_date": "2026-11-16", "agent_id": m["agent_id"]})
    assert r.status_code == 401 and r.json()["error"] == "agent_auth_required"
    with pytest.raises(S.GatewayError) as e:
        suggest.suggestions(m["mandate_id"], m["agent_id"], date(2026, 11, 16), agent_token="vka_nope")
    assert e.value.status == 403
    # curated mandates are unchanged: no token needed
    seed_curated()
    assert subs(mandate_id="M-HOSPITAL-KITCHEN", agent_id="meilahti-kitchen-agent").status_code == 200


def test_hiding_follows_the_sandbox_registry_not_names(c):
    from sous.mandate import Mandate
    c.headers["X-Approver-Code"] = CODE
    s = new(c)
    sid = s["sandbox"]["id"]
    # an approver-issued mandate that only looks like a sandbox, and one claiming an unregistered sandbox
    S.issue_mandate(Mandate(id="M-LOOKALIKE-SBX-AAAA", buyer="Lookalike (sandbox SBX-AAAA)",
                            agent_id="x-sbx-aaaa", issued_by="h", min_delivery_reliability=None))
    S.issue_mandate(Mandate(id="M-CLAIM", buyer="Claim", agent_id="claim-agent", issued_by="h",
                            sandbox="SBX-AAAA", min_delivery_reliability=None))
    mids = [m["id"] for m in c.get("/mandates").json()["mandates"]]
    assert "M-LOOKALIKE-SBX-AAAA" in mids and "M-CLAIM" in mids and not any(sid in m for m in mids)
    q = c.post("/quotes", json={"agent_id": "x-sbx-aaaa", "mandate_id": "M-LOOKALIKE-SBX-AAAA",
                                "delivery_date": "2026-11-16",
                                "items": [{"product_ids": ["OAT"], "amount": 10, "unit": "L"}]}).json()
    o = c.post("/orders", json={"agent_id": "x-sbx-aaaa", "mandate_id": "M-LOOKALIKE-SBX-AAAA",
                                "quote_id": q["quote"]["id"]}).json()
    assert o["id"] in [x["id"] for x in c.get("/orders").json()["orders"]]
    # a self-chosen agent id with the sandbox's name in it doesn't hide anything
    c.get("/products", params={"q": "oat", "agent_id": f"sneaky-{sid.lower()}"})
    events = c.get("/audit", params={"limit": 1000}).json()["events"]
    assert any(e["actor"] == f"sneaky-{sid.lower()}" for e in events)
    assert any(e["subject"] == o["id"] for e in events)
    assert any(e["subject"] == "M-LOOKALIKE-SBX-AAAA" for e in events)
    assert not any(e["subject"] == sid or (e["buyer"] or "").endswith(f"(sandbox {sid})") for e in events)
    # none of it shows inside the real sandbox's view
    sbx = c.get("/audit", params={"limit": 1000, "sandbox": sid}).json()["events"]
    assert sbx and not any(e["subject"] in (o["id"], "M-LOOKALIKE-SBX-AAAA") for e in sbx)
    assert [x["id"] for x in c.get("/orders", params={"sandbox": sid}).json()["orders"]] == \
        [s["seeded"]["held_order_id"]]
    # ownership comes from the registry, so a sandbox approver token can't touch lookalikes
    assert sandbox.owner("M-LOOKALIKE-SBX-AAAA") is None and sandbox.owner("M-CLAIM") is None
    assert sandbox.owner(s["mandates"][0]["mandate_id"]) == sid


# --- seed, quickstart key, idempotency scope ------------------------------------------------

def test_new_sandbox_is_seeded_with_a_drifted_standing_order_and_a_held_order_without_valio_calls(c, env):
    from sous import standing
    s = new(c)
    sid, seeded = s["sandbox"]["id"], s["seeded"]
    assert env.calls == 0  # no live Valio call at creation
    assert c.get(f"/sandbox/{sid}").json()["quotes_used"] == 0
    assert seeded["status"] == "pending_approval" and "No Valio call" in seeded["note"]
    o = store.get_order(seeded["held_order_id"])
    # approve_by comes from the reference cut-off (17:00 Helsinki the day before delivery) minus the margin, not
    # from delivery-day midnight (judge audit: 16.11 00:00 although Valio's milk cut-off is 15.11 17:00)
    from datetime import date as _date, datetime as _datetime, timedelta as _td
    from sous import config as _config, deadline as _deadline
    day = _date.fromisoformat(o["delivery_date"])
    cut = _datetime.combine(day - _td(days=1), _datetime.min.time(), _config.TZ).replace(hour=17)
    # ... unless the sandbox (and its mandates) expire before that: then that is the deadline
    end = _datetime.fromisoformat(s["sandbox"]["expires_at"])
    want = min(cut - _td(minutes=_deadline.MARGIN_MIN), end)
    assert o["approve_by_detail"]["basis"] == ("cutoff" if cut - _td(minutes=_deadline.MARGIN_MIN) <= end else "sandbox_expires")
    assert _datetime.fromisoformat(o["approve_by"]) == want
    assert all(l["order_cutoff"] == cut.isoformat() for l in o["lines"])
    rules = {f["rule"] for f in o["decision"]["findings"]}
    assert {"standing.quantity_creep", "standing.quantity_trend", "standing.cost_drift"} <= rules, rules
    assert o["decision"]["outcome"] == "ask" and "Seeded demo data" in o["agent_rationale"]
    # that note is Sous's own text, so the order view labels it as a system note, not "the agent's own words"
    from sous import access
    claim = access.label_order(o)["agent_claim"]
    assert claim["source"] == "system" and "not by an agent" in claim["note"] and not claim["suspicious"]
    # the reasons that held it come first; FYI-level ones (the €1,000 notify line) follow as "also: ..."
    by_msg = {f["message"]: f for f in o["decision"]["findings"]}
    held = [by_msg[m] for m in seeded["reasons"] if m in by_msg]
    assert held and held[0]["rule"].startswith("standing.") and all(f["outcome"] == "ask" for f in held)
    assert seeded["reasons"][:len(held)] == [f["message"] for f in held]
    also = seeded["reasons"][len(held):]
    assert also and all(r.startswith("also: ") for r in also)
    assert {by_msg[r[6:]]["outcome"] for r in also} <= {"notify"}
    # the seeded quote and the receipt are signed and verify like any other
    q = signing.verify(store.get_quote(o["quote_id"])["_jws"], "sous-quote+jws")
    assert q["seeded"]["sandbox"] == sid and q["valio_api_calls"]["price_calls"] == 0
    # seeded lines never imply live stock: no Valio availability was read
    assert all(l["stock_level"] == "unknown" and l["stock"] == "unknown" for l in q["lines"])
    assert signing.verify(o["receipt_jws"], "sous-order+jws")["id"] == o["id"]
    so = standing.get(seeded["standing_order_id"])
    assert so["baseline"]["weeks"] == 4 and all(x["simulated"] for x in so["baseline"]["from"])
    assert so["renewals"][0]["order_id"] == o["id"] and so["renewals"][0]["seeded"]
    c.headers["Authorization"] = f"Bearer {s['agent_token']}"
    assert [x["id"] for x in c.get("/standing-orders", params={"sandbox": sid}).json()["standing_orders"]] == \
        [seeded["standing_order_id"]]
    assert c.get("/standing-orders").json()["standing_orders"] == []  # hidden from the curated view
    del c.headers["Authorization"]  # the judge approves it with the sandbox approver token
    r = c.post(f"/orders/{o['id']}/approve", json={"by": "judge"}, headers={"X-Approver-Code": s["approver_token"]})
    assert r.status_code == 200 and r.json()["status"] == "ordered", r.text


def test_quickstart_order_key_is_per_sandbox_and_keys_are_scoped_per_agent(c):
    a, b = new(c, "10.0.0.1"), new(c, "10.0.0.2")
    for s in (a, b):
        assert f'"idempotency_key": "{s["sandbox"]["id"].lower()}-<quote.id>"' in s["quickstart"]["curl_order"]

    def post(s, quote_id, key):
        m = s["mandates"][0]
        return c.post("/orders", json={"agent_id": m["agent_id"], "mandate_id": m["mandate_id"],
                                       "quote_id": quote_id, "idempotency_key": key},
                      headers={"Authorization": f"Bearer {s['agent_token']}"})

    qa = quote(c, a).json()["quote"]["id"]
    oa, ob = post(a, qa, "try-1"), post(b, quote(c, b).json()["quote"]["id"], "try-1")
    assert oa.status_code == ob.status_code == 200, (oa.text, ob.text)  # same key, other agent: independent
    assert oa.json()["id"] != ob.json()["id"]
    assert post(a, qa, "try-1").json()["id"] == oa.json()["id"]  # same agent + key + quote: the same order
    again = post(a, quote(c, a).json()["quote"]["id"], "try-1")  # same agent and key, another quote
    assert again.status_code == 409 and again.json()["error"] == "idempotency_key_reused"


def test_sqlite_migration_drops_the_global_idempotency_unique(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(store.SQLITE_SCHEMA.replace("idempotency_key TEXT,", "idempotency_key TEXT UNIQUE,"))
    old.execute("INSERT INTO purchase_order VALUES ('PO-1','Q-1','M','a1','ordered',1.0,'{}','k','t','t')")
    old.commit()
    old.close()
    monkeypatch.setattr(config, "STATE_DB", path)
    for _ in range(2):  # idempotent on restart
        monkeypatch.setattr(store, "_conn", None)
        store.db()
    store._exec("INSERT INTO purchase_order VALUES ('PO-2','Q-2','M','a2','ordered',1.0,'{}','k','t','t')")
    with pytest.raises(sqlite3.IntegrityError):
        store._exec("INSERT INTO purchase_order VALUES ('PO-3','Q-3','M','a1','ordered',1.0,'{}','k','t','t')")
    assert [r["id"] for r in store._exec("SELECT id FROM purchase_order ORDER BY id")[0]] == ["PO-1", "PO-2"]


# --- OAuth connector grants end with the sandbox (sous/oauth.py) ----------------------------------

def _connector(c, s):
    """claude.ai-style grant for the sandbox: DCR, authorize with the pasted agent token, PKCE code exchange."""
    from tests.test_oauth import CALLBACK, authorize, code_from, pkce, register, token
    client = register(c)
    verifier, challenge = pkce()
    code = code_from(authorize(c, client, s["agent_token"], challenge=challenge))
    t = token(c, client, grant_type="authorization_code", code=code, redirect_uri=CALLBACK, code_verifier=verifier)
    assert t.status_code == 200, t.text
    return client, t.json()


def _get_mandate(c, access, mandate_id):
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
         "Authorization": f"Bearer {access}"}
    r = c.post("/connect/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    if r.status_code != 200:
        return r.status_code, None
    h["Mcp-Session-Id"] = r.headers["mcp-session-id"]
    c.post("/connect/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    r = c.post("/connect/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "get_mandate", "arguments": {"mandate_id": mandate_id}}})
    data = [ln[5:].strip() for ln in r.text.splitlines() if ln.startswith("data:")]
    return r.status_code, json.loads(data[-1])["result"]


@pytest.fixture
def live(monkeypatch):
    """TestClient with the app's lifespan (the MCP session manager) running."""
    monkeypatch.setattr(catalog, "_db", lambda: None)
    monkeypatch.setattr(api.mcp.session_manager, "_has_started", False)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "http://testserver")  # the OAuth resource tests/test_oauth uses
    with TestClient(api.app) as client:
        yield client


def test_a_sandbox_token_finds_its_own_mandates(live):
    """Toivo's question on staging: which mandate am I? GET /mandates without ?sandbox= and MCP my_mandates."""
    from tests.test_oauth import mcp_quote
    seed_curated()
    s, other = new(live, "10.0.0.1"), new(live, "10.0.0.2")
    ids = {m["scenario"]: m["mandate_id"] for m in s["mandates"]}
    for tok in (s["agent_token"], s["approver_token"]):
        h = {"Authorization": f"Bearer {tok}"}
        mine = [m["id"] for m in live.get("/mandates", headers=h).json()["mandates"] if m.get("yours")]
        assert sorted(mine) == sorted(ids.values())
    assert not any(m.get("yours") or m.get("sandbox") for m in live.get("/mandates").json()["mandates"])
    _, out = mcp_quote(live, s["agent_token"], [("my_mandates", {})])
    got = {m["label"]: (m["mandate_id"], m["agent_id"], m["sandbox"]) for m in out["result"]["structuredContent"]["mandates"]}
    assert {k: v[0] for k, v in got.items()} == ids
    assert got["hotel"][1] == s["mandates"][0]["agent_id"] and got["hotel"][2] == s["sandbox"]["id"]
    assert not set(m["mandate_id"] for m in other["mandates"]) & {v[0] for v in got.values()}


@pytest.mark.parametrize("how", ["expired", "expired_unswept", "deleted"])
def test_connector_grants_end_with_the_sandbox(live, monkeypatch, how):
    from tests.test_oauth import authorize, pkce, token
    seed_curated()
    s = new(live)
    sid, hotel = s["sandbox"]["id"], s["mandates"][0]["mandate_id"]
    client, t = _connector(live, s)
    status, res = _get_mandate(live, t["access_token"], hotel)
    assert status == 200 and not res.get("isError") and res["structuredContent"]["mandate"]["id"] == hotel
    if how == "deleted":
        r = live.delete(f"/sandbox/{sid}", params={"by": "Rene"}, headers={"X-Approver-Code": CODE})
        assert r.status_code == 200
    else:
        later = time.time() + 25 * 3600  # sandbox clock only: the OAuth tokens themselves are still fresh
        monkeypatch.setattr(sandbox, "_clock", lambda: later)
        if how == "expired":
            sandbox.sweep(force=True)
    assert _get_mandate(live, t["access_token"], hotel)[0] == 401
    r = token(live, client, grant_type="refresh_token", refresh_token=t["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    if how != "expired_unswept":  # closing the sandbox deleted its grants outright
        assert store._exec("SELECT COUNT(*) AS n FROM oauth_grant")[0][0]["n"] == 0
        assert any(e["event"] == "oauth.revoked" for e in store.audit_events(limit=500))
    r = authorize(live, client, s["agent_token"], challenge=pkce()[1])  # and no new grant for it
    assert r.status_code == 200 and "not valid" in r.text


def test_agent_catalog_search_is_filed_under_its_sandbox_buyer(c):
    """An authenticated agent's search shows in its sandbox audit (the order's 'How it happened' links it);
    an unauthenticated search naming the same agent_id is not filed under that buyer."""
    sbx = new(c)
    sid, hotel = sbx["sandbox"]["id"], next(x for x in sbx["mandates"] if x["scenario"] == "hotel")
    tok = {"Authorization": f"Bearer {sbx['agent_token']}"}
    assert c.get("/products", params={"q": "oat drink", "agent_id": hotel["agent_id"]}, headers=tok).status_code == 200
    assert c.get("/products", params={"q": "forged", "agent_id": hotel["agent_id"]}).status_code == 200
    events = c.get("/audit", params={"limit": 1000, "sandbox": sid}, headers=tok).json()["events"]
    searches = [e for e in events if e["event"] == "catalog.search"]
    assert [e["data"]["query"] for e in searches] == ["oat drink"]
    assert searches[0]["actor"] == hotel["agent_id"] and sid in searches[0]["buyer"]
    # MCP search_products files it the same way when the Bearer covers the agent's mandate
    assert S.credential_buyer(hotel["agent_id"], {hotel["mandate_id"]}) == searches[0]["buyer"]
    assert S.credential_buyer("someone-else", {hotel["mandate_id"]}) is None
