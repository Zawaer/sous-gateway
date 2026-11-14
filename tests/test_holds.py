"""Valio product holds (sous/holds.py): recalls, quality holds and sales stops.

Offline: the catalog and Valio's price/availability (quote.offers) are replaced, as in test_standing.py.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from sous import api, catalog, config, holds, notify, quote as Q, rules, service as S, signing, standing, store
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
MON = date(2026, 11, 16)
PRICES = {"MILK": 1.0, "MILK2": 1.5}


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
        price = PRICES[pid]
        pr = {"catalogPrice": {"value": price, "valueWithTax": round(price * 1.135, 2), "validTo": None},
              "comparisonPrice": {"value": price}, "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}
        av = {"logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": False,
              "scheduleLines": [{"salesBlock": False, "availableAmount": 99999, "availableDate": "2026-11-16",
                                 "salesUnit": "ST"}]}
        out.append(Q.build_offer(product(pid), pr, av, requested, need, need_unit, NOW))
    return out, NOW


SENT: list = []


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(config, "APPROVER_CODE", "")
    monkeypatch.setattr(config, "REQUIRE_AGENT_AUTH", False)
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: product(pid) if pid in PRICES else None)
    monkeypatch.setattr(Q, "offers", fake_offers)
    monkeypatch.setattr(Q, "prefetch", lambda ids, requested: None)
    monkeypatch.setattr(S.substitutes, "suggest", lambda *a, **k: {"options": []})
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(holds, "_now", lambda: NOW)
    SENT.clear()
    monkeypatch.setattr(S.notify, "send", lambda event, order, mandate: SENT.append((event, order)))
    S.issue_mandate(Mandate(id="M", buyer="Hotel", agent_id="a", issued_by="chef", notify_above_eur=None,
                            auto_approve_limit_eur=50, hard_limit_eur=20000, min_delivery_reliability=None))
    yield
    signing._key.cache_clear()


def quote(pids=("MILK",), amount=10):
    return S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=MON,
                                          items=[S.QuoteItem(product_ids=list(pids), amount=amount, unit="L")]))["quote"]


def hold(pid="MILK", kind="recall", **kw):
    return holds.create(holds.HoldIn(product_id=pid, kind=kind, reason="Listeria found in batch 42", **kw),
                        product(pid)["name"])


def events(name):
    return [e for e in store.audit_events(limit=1000) if e["event"] == name]


def test_rule_is_registered_as_valio_block():
    r = rules.get("product.held")
    assert r and r["owner"] == "valio" and r["tiers"] == ["block"]


def test_held_product_is_quoted_unorderable_and_blocked():
    hold()
    q = quote()
    line = q["lines"][0]
    assert not line["orderable"] and [i["code"] for i in line["issues"] if i["severity"] == "blocking"] == ["PRODUCT_HELD"]
    assert "recalled" in line["issues"][-1]["message"] and "Listeria" in line["issues"][-1]["message"]
    assert q["mandate_preview"]["outcome"] == "block"
    assert [f["rule"] for f in q["mandate_preview"]["findings"] if f["outcome"] == "block"] == ["product.held"]
    assert q["total_ex_vat"] == 0


def test_held_candidate_is_skipped_for_another():
    hold("MILK", "sales_stop")
    q = quote(("MILK", "MILK2"))
    assert [l["product_id"] for l in q["lines"]] == ["MILK2"]
    assert q["mandate_preview"]["outcome"] == "silent", q["mandate_preview"]
    assert any(c["product_id"] == "MILK" and "sales stop" in " ".join(c["why_not"]) for c in q["alternatives_considered"])


def test_hold_after_quote_blocks_the_order():
    q = quote()
    hold("MILK", "quality_hold")
    o = S.place_order("a", "M", q["id"])
    assert o["status"] == "refused"
    assert "product.held" in [f["rule"] for f in o["decision"]["findings"]]


def test_hold_on_pending_order_notifies_and_blocks_approval():
    q = quote(amount=100)  # 100 EUR > auto limit 50: waits for a human
    o = S.place_order("a", "M", q["id"])
    assert o["status"] == "pending_approval"
    SENT.clear()
    h = hold("MILK", "quality_hold")
    assert h["affected"] == [{"mandate_id": "M", "buyer": "Hotel", "orders": [
        {"id": o["id"], "role": "pending_approval", "status": "pending_approval", "created_at": o["created_at"],
         "delivery_date": o["delivery_date"]}]}]
    assert [(e, n["id"]) for e, n in SENT] == [("product.held", o["id"])]
    ev = events("product.held")
    assert len(ev) == 1 and ev[0]["subject"] == o["id"] and ev[0]["buyer"] == "Hotel"
    assert ev[0]["data"]["hold"]["kind"] == "quality_hold"
    assert events("hold.created")[0]["subject"] == h["id"]
    decided = S.decide(o["id"], "chef", True)
    assert decided["status"] == "refused" and "on quality hold" in decided["approval"]["result_reason"]


def test_recall_lists_recent_orders_but_a_quality_hold_does_not():
    placed = S.place_order("a", "M", quote()["id"])
    assert placed["status"] == "ordered"
    assert hold("MILK", "quality_hold")["affected"] == []
    h = hold("MILK", "recall")
    roles = [(r["id"], r["role"]) for g in h["affected"] for r in g["orders"]]
    assert roles == [(placed["id"], "recently_ordered")]
    event, n = SENT[-1]
    msg = notify.compose(event, n, None)
    assert msg["title"].startswith("Recall: ") and placed["id"] in msg["message"] and "14 days" in msg["message"]


def test_recall_ignores_orders_older_than_14_days(monkeypatch):
    placed = S.place_order("a", "M", quote()["id"])
    monkeypatch.setattr(holds, "_now", lambda: NOW + timedelta(days=holds.RECALL_LOOKBACK_DAYS + 1))
    assert placed["status"] == "ordered" and hold()["affected"] == []


def test_active_standing_order_is_notified_paused_one_is_not():
    so = standing.create(standing.StandingOrderIn(mandate_id="M", agent_id="a", name="Milk",
                                                  items=[standing.StandingItem(product_ids=["MILK"], amount=5, unit="L")]))
    h = hold("MILK", "sales_stop")
    assert [r["role"] for g in h["affected"] for r in g["orders"]] == ["standing_order"]
    assert events("product.held")[0]["subject"] == so["id"]
    holds.lift(h["id"])
    standing.set_active(so["id"], False, "chef")
    assert hold("MILK", "sales_stop")["affected"] == []


def test_expired_hold_is_inactive():
    hold(until=NOW - timedelta(minutes=1))
    assert holds.list_active() == [] and quote()["lines"][0]["orderable"]


def test_rest_create_needs_global_code_list_is_public_and_lift_restores(monkeypatch):
    monkeypatch.setattr(config, "APPROVER_CODE", "secret-code")
    c = TestClient(api.app)
    body = {"product_id": "MILK", "kind": "recall", "reason": "Foreign object"}
    assert c.post("/holds", json=body).status_code == 401
    assert c.post("/holds", json=body, headers={"X-Approver-Code": "wrong"}).status_code == 401
    assert c.post("/holds", json={**body, "product_id": "NOPE"},
                  headers={"X-Approver-Code": "secret-code"}).status_code == 404
    r = c.post("/holds", json=body, headers={"X-Approver-Code": "secret-code"})
    assert r.status_code == 200, r.text
    hid = r.json()["id"]
    listed = c.get("/holds").json()["holds"]
    assert [(h["id"], h["product_id"], h["kind"], h["reason"]) for h in listed] == [(hid, "MILK", "recall", "Foreign object")]
    assert listed[0]["since"] and "by" not in listed[0]
    assert not quote()["lines"][0]["orderable"]
    assert c.delete(f"/holds/{hid}").status_code == 401
    assert c.delete(f"/holds/{hid}", headers={"X-Approver-Code": "secret-code"}).status_code == 200
    assert c.get("/holds").json()["holds"] == []
    assert quote()["lines"][0]["orderable"]  # lifted at once in this process (cache invalidated)
    assert [e["event"] for e in store.audit_events(subject=hid)][:2] == ["hold.lifted", "hold.created"]


def test_search_card_and_mcp_resource_show_the_hold(monkeypatch):
    from sous import mcp_server

    monkeypatch.setattr(catalog, "search", lambda q, **kw: [product("MILK"), product("MILK2")])
    monkeypatch.setattr(catalog, "card", lambda p: {"id": p["id"], "name": p["name"]})
    h = hold("MILK", "quality_hold")
    cards = {c["id"]: c for c in S.search("a", "milk")["products"]}
    assert cards["MILK"]["held"]["kind"] == "quality_hold" and cards["MILK"]["held"]["id"] == h["id"]
    assert "held" not in cards["MILK2"]
    assert [x["id"] for x in mcp_server.holds_resource()["holds"]] == [h["id"]]


def test_build_offer_without_state_db_needs_no_database(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "none" / "state.db")
    monkeypatch.setattr(store, "_conn", None)
    assert holds.active_for("MILK") is None and not (tmp_path / "none").exists()
