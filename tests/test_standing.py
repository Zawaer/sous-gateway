"""Standing orders: CRUD, renewal through the mandate engine, drift findings, demo history, REST and MCP.

Offline: the catalog and Valio's price/availability (quote.offers) are replaced.
"""

import asyncio
import json
import os
import time
from datetime import date, datetime

import pytest

from sous import catalog, config, quote as Q, service as S, signing, standing, standing_store, store
from sous.mandate import Finding, Mandate, evaluate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
MON = date(2026, 11, 16)


def product(pid):
    size, unit, name = {"MILK": (1.0, "L", "Milk 1 l"), "MILK2": (1.0, "L", "Other milk 1 l"),
                        "BUTTER": (0.5, "KG", "Butter 500 g"), "CREAM": (1.0, "L", "Cream 1 l")}[pid]
    return {
        "id": pid, "name": name, "names": {"fi": "", "en": "", "sv": ""}, "brand": "", "vendor": "",
        "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
        "allowed_lot_size": 1.0, "temperature": "chilled", "labels": [], "claims": [],
        "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": []},
        "size_from_name": (size, unit), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
        "official_substitutes": [], "deleted": False,
    }


# per product: price per pack ex VAT, stock, logistic model; tests change these
MARKET: dict = {}


def reset_market():
    MARKET.clear()
    MARKET.update({"MILK": [1.0, 99999, "Stock"], "MILK2": [1.5, 99999, "Stock"],
                   "BUTTER": [4.0, 99999, "Stock"], "CREAM": [2.0, 99999, "Stock"]})


def fake_offers(product_ids, requested, need=None, need_unit=None):
    out = []
    for pid in product_ids:
        p = product(pid)
        price, stock, model = MARKET[pid]
        per = p["size_from_name"][0]
        pr = {"catalogPrice": {"value": price, "valueWithTax": round(price * 1.135, 2), "validTo": None},
              "comparisonPrice": {"value": round(price / per, 2)}, "comparisonUnit": p["size_from_name"][1],
              "tax": {"taxRate": 13.5}, "error": False}
        av = {"logisticModel": model, "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": False,
              "scheduleLines": [{"salesBlock": False, "availableAmount": stock if stock else 0,
                                 "availableDate": "2026-11-16" if stock else None, "salesUnit": "ST"}]}
        out.append(Q.build_offer(p, pr, av, requested, need, need_unit, NOW))
    return out, NOW


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: product(pid) if pid in MARKET else None)
    monkeypatch.setattr(Q, "offers", fake_offers)
    monkeypatch.setattr(Q, "prefetch", lambda ids, requested: None)
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(S.notify, "send", lambda *a, **k: None)
    reset_market()
    S.issue_mandate(Mandate(id="M", buyer="Hotel", agent_id="a", issued_by="chef", notify_above_eur=None,
                            auto_approve_limit_eur=5000, hard_limit_eur=20000, min_delivery_reliability=None))
    yield
    signing._key.cache_clear()


def create(**kw):
    return standing.create(standing.StandingOrderIn(**{
        "mandate_id": "M", "agent_id": "a", "name": "Weekly dairy",
        "items": [{"product_ids": ["MILK", "MILK2"], "amount": 400, "unit": "L", "label": "milk"},
                  {"product_ids": ["BUTTER"], "amount": 20, "unit": "KG", "label": "butter"},
                  {"product_ids": ["CREAM"], "amount": 30, "unit": "L", "label": "cream"}], **kw}))


def seed(so, weeks):
    q = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=MON, items=[
        S.QuoteItem(product_ids=it["product_ids"], amount=it["amount"], unit=it["unit"]) for it in so["items"]]))
    return standing.simulate_history(so["id"], q["quote"]["id"], weeks, "demo")


def rules(res):
    return {f["rule"] for f in res["drift"]["findings"]}


def events(name):
    return [e for e in store.audit_events(limit=500) if e["event"] == name]


# --- mandate hook -----------------------------------------------------------

def test_evaluate_extra_findings_is_backward_compatible():
    m = Mandate(id="X", buyer="B", agent_id="a", issued_by="h")
    line = {"product_id": "MILK", "name": "Milk", "line_total_ex_vat": 10, "issues": [], "claims": []}
    base = evaluate(m, "a", [line], requested_date=MON, now=NOW)
    assert base == evaluate(m, "a", [line], requested_date=MON, now=NOW, extra_findings=None)
    assert base[0] == "silent" and base[1][-1].rule == "mandate.ok"
    tier, fs = evaluate(m, "a", [line], requested_date=MON, now=NOW,
                        extra_findings=[Finding(rule="standing.cost_drift", outcome="ask", message="x")])
    assert tier == "ask" and [f.rule for f in fs] == ["standing.cost_drift"]


# --- CRUD -------------------------------------------------------------------

def test_create_list_get_pause():
    so = create()
    assert so["id"].startswith("SO-") and so["active"] and so["cadence"] == {"every": "week", "weekday": "mon"}
    assert [s["id"] for s in standing.list_("M")] == [so["id"]] and standing.list_("OTHER") == []
    g = standing.get(so["id"])
    assert g["renewals"] == [] and g["baseline"]["weeks"] == 0 and g["next_delivery_date"] == "2026-11-16"
    assert standing.set_active(so["id"], False, "chef")["active"] is False
    with pytest.raises(S.GatewayError) as e:
        standing.renew(so["id"], MON)
    assert e.value.code == "standing_paused" and e.value.status == 409
    assert standing.set_active(so["id"], True, "chef")["active"] is True
    assert {"standing.created", "standing.paused", "standing.resumed"} <= {e["event"] for e in store.audit_events()}


def test_create_checks_agent_and_products():
    with pytest.raises(S.GatewayError) as e:
        create(agent_id="intruder")
    assert e.value.code == "standing_agent_mismatch"
    with pytest.raises(S.GatewayError) as e:
        create(items=[{"product_ids": ["NOPE"], "amount": 1}])
    assert e.value.code == "product_not_found"
    with pytest.raises(S.GatewayError) as e:
        standing.get("SO-NOPE")
    assert e.value.code == "standing_not_found"


# --- renewal and drift ------------------------------------------------------

def test_first_renewal_has_no_baseline_and_orders():
    so = create()
    r = standing.renew(so["id"], MON)
    assert r["order"]["status"] == "ordered" and r["drift"]["findings"] == []
    assert r["renewal"]["items"][0]["product_ids"] == ["MILK"] and r["renewal"]["total_ex_vat"] == 400 + 160 + 60
    assert "no history" in r["drift"]["summary"]
    with pytest.raises(S.GatewayError) as e:
        standing.renew(so["id"], MON)
    assert e.value.code == "already_renewed"
    assert events("standing.renewal_checked") and events("standing.renewed")


def test_creeping_quantities_escalate_to_ask():
    so = create()
    sim = seed(so, [[400, 20, 30], [420, 20, 30], [430, 20, 30], [450, 20, 30]])["simulated"]
    assert [r["delivery_date"] for r in sim] == ["2026-10-19", "2026-10-26", "2026-11-02", "2026-11-09"]
    assert all(r["simulated"] and r["status"] == "ordered" for r in sim)
    assert sim[0]["total_ex_vat"] == 400 + 160 + 60

    r = standing.renew(so["id"], MON, amounts=[500, 20, 30])
    assert r["order"]["status"] == "pending_approval" and r["order"]["decision"]["outcome"] == "ask"
    assert rules(r) == {"standing.quantity_creep", "standing.quantity_trend", "standing.cost_drift"}
    order_rules = {f["rule"] for f in r["order"]["decision"]["findings"]}
    assert {"standing.quantity_creep", "standing.cost_drift"} <= order_rules
    creep = next(f for f in r["drift"]["findings"] if f["rule"] == "standing.quantity_creep")
    assert creep["message"] == "milk: 500 L is up 18% vs your 4-week median of 425 L."
    # median total 425 + 160 + 60 = 645; now 720 -> +75
    assert r["drift"]["summary"] == "milk up 18% vs your 4-week median; cost +€75; asking before renewing"
    assert r["drift"]["baseline"]["median_total_ex_vat"] == 645
    chk = events("standing.renewal_checked")[0]["data"]
    assert chk["baseline"]["simulated_weeks"] == 4 and len(chk["drift"]) == 3
    assert events("standing.history_simulated")[0]["data"]["simulated"] is True

    g = standing.get(so["id"])
    assert g["renewals"][0]["status"] == "pending_approval" and len(g["renewals"]) == 5
    assert g["baseline"]["weeks"] == 4  # the pending renewal is not part of the baseline


def test_steady_order_within_baseline_goes_through():
    so = create()
    seed(so, [[400, 20, 30], [410, 20, 30], [400, 20, 30], [405, 20, 30]])
    r = standing.renew(so["id"], MON, amounts=[410, 20, 30])
    assert r["drift"]["findings"] == [] and r["order"]["status"] == "ordered"
    assert r["drift"]["summary"] == "In line with the last 4 renewals."


def test_approved_drift_becomes_baseline():
    so = create()
    seed(so, [[400, 20, 30]] * 3)
    r = standing.renew(so["id"], MON, amounts=[500, 20, 30])
    assert r["order"]["status"] == "pending_approval"
    S.decide(r["order"]["id"], "chef", True)
    g = standing.get(so["id"])
    assert g["renewals"][0]["status"] == "ordered" and g["baseline"]["weeks"] == 4
    assert g["baseline"]["items"][0]["amounts"] == [400, 400, 400, 500]


def test_product_switch_and_price_drift():
    so = create()
    seed(so, [[400, 20, 30]] * 4)
    MARKET["MILK"][1] = 0          # usual milk out of stock -> the other one is chosen
    MARKET["BUTTER"][0] = 4.8      # butter +20 %
    r = standing.renew(so["id"], MON)
    assert {"standing.product_switched", "standing.price_drift", "standing.cost_drift"} <= rules(r)
    sw = next(f for f in r["drift"]["findings"] if f["rule"] == "standing.product_switched")
    assert sw["message"] == "milk: now Other milk 1 l; last time Milk 1 l." and sw["product_id"] == "MILK2"
    assert "milk product switched" in r["drift"]["summary"] and "butter price +20%" in r["drift"]["summary"]
    assert r["order"]["status"] == "pending_approval"


def test_new_terminal_is_flagged():
    so = create()
    seed(so, [[400, 20, 30]] * 2)
    MARKET["CREAM"][2] = "Terminal"
    r = standing.renew(so["id"], MON)
    assert rules(r) == {"standing.new_terminal"} and r["order"]["status"] == "pending_approval"


def test_amounts_must_match_items():
    so = create()
    with pytest.raises(S.GatewayError) as e:
        standing.renew(so["id"], MON, amounts=[1, 2])
    assert e.value.code == "invalid_argument"


def test_simulate_history_needs_quote_for_the_mandate():
    so = create()
    with pytest.raises(S.GatewayError) as e:
        standing.simulate_history(so["id"], "Q-NOPE", [[1, 1, 1]], "demo")
    assert e.value.code == "quote_not_found"


# --- REST and MCP -----------------------------------------------------------

def test_rest_routes(monkeypatch):
    from fastapi.testclient import TestClient

    from sous import api
    monkeypatch.setattr(config, "APPROVER_CODE", "1234")
    c = TestClient(api.app)
    body = {"mandate_id": "M", "agent_id": "a", "name": "Weekly dairy",
            "items": [{"product_ids": ["MILK"], "amount": 400, "unit": "L", "label": "milk"}]}
    so = c.post("/standing-orders", json=body).json()
    assert c.get("/standing-orders", params={"mandate_id": "M"}).json()["standing_orders"] == []  # not public
    assert c.get("/standing-orders", params={"mandate_id": "M"}, headers={"X-Approver-Code": "1234"}).json()["standing_orders"][0]["id"] == so["id"]
    r = c.post(f"/standing-orders/{so['id']}/renew", json={"delivery_date": "2026-11-16", "agent_id": "a"})
    assert r.status_code == 200 and r.json()["order"]["status"] == "ordered"
    assert c.post(f"/standing-orders/{so['id']}/renew", json={"delivery_date": "2026-11-16"}).status_code == 409
    g = c.get(f"/standing-orders/{so['id']}", headers={"X-Approver-Code": "1234"}).json()
    assert len(g["renewals"]) == 1 and g["baseline"]["weeks"] == 1
    assert c.post(f"/standing-orders/{so['id']}/pause", json={"by": "chef"}).status_code == 401
    p = c.post(f"/standing-orders/{so['id']}/pause", json={"by": "chef"}, headers={"X-Approver-Code": "1234"})
    assert p.json()["active"] is False
    assert c.get("/standing-orders/SO-NOPE").status_code == 404


def test_mcp_tools():
    from mcp import Client

    from sous.mcp_server import mcp

    async def go():
        async with Client(mcp) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            so = await c.call_tool("create_standing_order", {
                "mandate_id": "M", "agent_id": "a", "name": "Weekly dairy",
                "items": [{"product_ids": ["MILK"], "amount": 400, "unit": "L", "label": "milk"}]})
            sid = json.loads(so.content[0].text)["id"]
            r = await c.call_tool("renew_standing_order", {"standing_id": sid, "agent_id": "a",
                                                           "delivery_date": "2026-11-16"})
            g = await c.call_tool("get_standing_order", {"standing_id": sid})
            bad = await c.call_tool("renew_standing_order", {"standing_id": sid, "agent_id": "intruder"})
            return tools, r, g, bad

    tools, r, g, bad = asyncio.run(go())
    assert tools["get_standing_order"].annotations.read_only_hint is True
    assert tools["renew_standing_order"].annotations.destructive_hint is True
    assert not r.is_error and json.loads(r.content[0].text)["order"]["status"] == "ordered"
    assert len(json.loads(g.content[0].text)["renewals"]) == 1
    assert bad.is_error and bad.structured_content["error"] == "standing_agent_mismatch"


# --- MariaDB ----------------------------------------------------------------

MARIA = os.getenv("SOUS_TEST_MARIADB_URL")


@pytest.mark.skipif(not MARIA, reason="needs SOUS_TEST_MARIADB_URL")
def test_mariadb_versions_standing_order(monkeypatch):
    monkeypatch.setattr(config, "DB_URL", MARIA)
    monkeypatch.setattr(store, "_conn", None)
    for t in ("standing_renewal", "standing_order", "audit_event", "purchase_order", "quote", "mandate"):
        store._exec(f"DROP TABLE IF EXISTS {t}")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(standing_store, "_ready_for", None)
    S.issue_mandate(Mandate(id="M", buyer="Hotel", agent_id="a", issued_by="chef", auto_approve_limit_eur=5000,
                            min_delivery_reliability=None))
    so = create()
    time.sleep(0.05)
    standing.set_active(so["id"], False, "chef")
    vs = standing_store.versions(so["id"])
    assert [v["standing_order"]["active"] for v in vs] == [True, False] and vs[0]["valid_to"]
    standing.set_active(so["id"], True, "chef")
    seed(so, [[400, 20, 30]] * 4)
    r = standing.renew(so["id"], MON, amounts=[500, 20, 30])
    assert r["order"]["status"] == "pending_approval"
    assert len(standing.get(so["id"])["renewals"]) == 5
    store._conn = None


# --- cut-off-aware renewals (sous/deadline.py) ------------------------------

def test_schedule_renews_lead_hours_before_the_cutoff(monkeypatch):
    so = create()
    assert so["renew_lead_hours"] == 24
    g = standing.get(so["id"])
    # no renewal yet: Valio's Stock rule, 17:00 the day before the Monday delivery
    assert g["cutoff"]["at"] == "2026-11-15T17:00:00+02:00" and g["cutoff"]["estimated"]
    assert g["next_renewal_at"] == "2026-11-14T17:00:00+02:00" and g["renewal_schedule"]["renew_due"] is False
    early = create(renew_lead_hours=48)
    s = standing.get(early["id"])["renewal_schedule"]
    assert s["next_renewal_at"] == "2026-11-13T17:00:00+02:00" and s["renew_due"] is True  # NOW is Fri 21:00
    # past the Monday's cut-off, the next delivery is the following Monday
    monkeypatch.setattr(S, "_now", lambda: datetime(2026, 11, 15, 18, 0, tzinfo=config.TZ))
    assert standing.get(so["id"])["next_delivery_date"] == "2026-11-23"


def test_held_renewal_carries_approve_by_and_schedule_uses_its_cutoff():
    so = create()
    seed(so, [[300, 20, 30]] * 4)
    r = standing.renew(so["id"], MON)  # milk 400 vs median 300: held
    assert r["order"]["status"] == "pending_approval"
    assert r["order"]["approve_by"] == r["renewal"]["approve_by"] == "2026-11-15T16:45:00+02:00"
    assert events("standing.renewed")[-1]["data"]["approve_by"] == "2026-11-15T16:45:00+02:00"
    g = standing.get(so["id"])
    assert g["cutoff"]["basis"].startswith("last renewal's Valio cut-off")
    assert g["cutoff"]["at"] == "2026-11-15T17:00:00+02:00"  # the real Sun 17:00 for Mon, applied to Mon 16.11
