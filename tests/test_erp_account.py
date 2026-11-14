"""Customer account checks (sous/account.py) and the outbound ORDERS05 adapter with its outbox (sous/erp.py).

Offline: catalog and Valio's price/availability replaced as in test_holds.py; the ERP is the in-process stub or a
monkeypatched sender.
"""

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from sous import account, api, catalog, config, erp, quote as Q, rules, service as S, signing, store
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=config.TZ)  # Friday
MON = date(2026, 11, 16)
SAT = date(2026, 11, 14)
PRICE = 1.0


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
        pr = {"catalogPrice": {"value": PRICE, "valueWithTax": round(PRICE * 1.135, 2), "validTo": None},
              "comparisonPrice": {"value": PRICE}, "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}
        av = {"logisticModel": "Stock", "closingTime": f"{requested.isoformat()}T05:00:00+0200", "salesBlock": False,
              "scheduleLines": [{"salesBlock": False, "availableAmount": 99999, "availableDate": requested.isoformat(),
                                 "salesUnit": "ST"}]}
        out.append(Q.build_offer(product(pid), pr, av, requested, need, need_unit, NOW))
    return out, NOW


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(config, "APPROVER_CODE", "")
    monkeypatch.setattr(config, "REQUIRE_AGENT_AUTH", False)
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: product(pid) if pid == "MILK" else None)
    monkeypatch.setattr(Q, "offers", fake_offers)
    monkeypatch.setattr(Q, "prefetch", lambda ids, requested: None)
    monkeypatch.setattr(S.substitutes, "suggest", lambda *a, **k: {"options": []})
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(S.notify, "send", lambda *a, **k: None)
    monkeypatch.setattr(erp, "ERP_URL", "")
    S.issue_mandate(Mandate(id="M", buyer="Hotel Aurora, Helsinki", customer_number="0000104711", agent_id="a",
                            issued_by="chef", notify_above_eur=None, auto_approve_limit_eur=5000,
                            hard_limit_eur=20000, min_delivery_reliability=None, max_price_change_pct=None))
    yield
    signing._key.cache_clear()


def quote(amount=10, day=MON):
    return S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=day,
                                          items=[S.QuoteItem(product_ids=["MILK"], amount=amount, unit="L")]))["quote"]


def order(amount=10, day=MON, key=None):
    return S.place_order("a", "M", quote(amount, day)["id"], key)


def rules_fired(o):
    return {f["rule"] for f in o["decision"]["findings"] if f["outcome"] != "silent"}


def events(name):
    return [e for e in store.audit_events(limit=1000) if e["event"] == name]


def set_account(**kw):
    a = account.backend.get("0000104711")
    account.backend.put(account.Account(**{**a.model_dump(), **kw}))


# --- account ---------------------------------------------------------------------------------------------------

def test_rules_registered_as_valio_rules():
    for rid in ("account.credit_limit", "account.payment_block", "account.delivery_day"):
        assert rules.get(rid)["owner"] == "valio"


def test_demo_account_seeded_and_normal_order_passes():
    o = order()
    assert o["status"] == "ordered"
    assert not {r for r in rules_fired(o) if r.startswith("account.")}
    a = account.backend.get("0000104711")
    assert a.buyer == "Hotel Aurora, Helsinki" and a.credit_limit_eur == 60000


def test_credit_limit_asks_and_counts_open_sous_orders():
    set_account(credit_limit_eur=12400 + 30)  # 30 € left (incl. VAT)
    first = order(amount=20)  # 22.70 incl. VAT: fits
    assert first["status"] == "ordered"
    second = order(amount=20)  # 22.70 more, but only 7.30 left now
    assert second["status"] == "pending_approval"
    assert "account.credit_limit" in rules_fired(second)
    msg = next(f["message"] for f in second["decision"]["findings"] if f["rule"] == "account.credit_limit")
    assert f"open Sous orders €{first['total_inc_vat']:.2f}" in msg
    # a human approves: credit is a Valio check a human may confirm, not a block
    assert S.decide(second["id"], "Anna", True)["status"] == "ordered"


def test_quote_preview_shows_account_findings():
    set_account(credit_limit_eur=12400 + 1)
    q = quote(amount=10)
    assert q["mandate_preview"]["outcome"] == "ask"
    assert "account.credit_limit" in {f["rule"] for f in q["mandate_preview"]["findings"]}


def test_payment_block_blocks_order_and_approval():
    set_account(credit_limit_eur=12400 + 1)
    held = order(amount=10)
    assert held["status"] == "pending_approval"
    set_account(payment_block=True, payment_block_reason="invoices overdue")
    assert "account.payment_block" in rules_fired(order())
    assert S.decide(held["id"], "Anna", True)["status"] == "refused"


def test_delivery_day_and_route_cutoff():
    set_account(delivery_days=["MON", "TUE", "WED", "THU", "FRI"])
    o = order(day=SAT)
    assert o["status"] == "refused" and "account.delivery_day" in rules_fired(o)
    set_account(delivery_days=["MON", "SAT"], route=account.Route(id="HKI-03", cutoff_local="10:00", cutoff_days_before=1))
    o = order(day=SAT)  # Friday 12:00 is past Friday 10:00
    assert o["status"] == "refused"
    assert "Route HKI-03" in next(f["message"] for f in o["decision"]["findings"] if f["rule"] == "account.delivery_day")
    assert order(day=MON)["status"] == "ordered"


def test_no_account_no_checks_unless_required(monkeypatch):
    S.issue_mandate(Mandate(id="M2", buyer="Unknown Oy", agent_id="a", issued_by="chef", min_delivery_reliability=None))
    q = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M2", delivery_date=MON,
                                       items=[S.QuoteItem(product_ids=["MILK"], amount=1, unit="L")]))["quote"]
    assert q["mandate_preview"]["outcome"] == "silent"
    monkeypatch.setattr(account, "REQUIRE_ACCOUNT", True)
    q = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M2", delivery_date=MON,
                                       items=[S.QuoteItem(product_ids=["MILK"], amount=1, unit="L")]))["quote"]
    assert "account.unknown" in {f["rule"] for f in q["mandate_preview"]["findings"]}


def test_sandbox_buyer_gets_its_own_account():
    a = account.backend.get(account.sandbox_number("0000104711", "SBX-7Q2K"))
    assert a.buyer == "Hotel Aurora, Helsinki (sandbox SBX-7Q2K)" and a.credit_limit_eur == 60000
    assert account.backend.customer_number_for("Espoo school kitchens (sandbox SBX-7Q2K)") == "0000309822-SBX-7Q2K"


# --- ERP adapter -----------------------------------------------------------------------------------------------

def test_ordered_order_is_sent_and_confirmed_by_stub():
    o = order()
    assert o["erp"]["status"] == "confirmed" and len(o["erp"]["confirmation_id"]) == 10
    assert o["erp"]["endpoint"] == "stub" and o["erp"]["customer_number"] == "0000104711"
    assert [e["subject"] for e in events("order.erp_sent")] == [o["id"]]
    assert events("order.erp_confirmed")[0]["data"]["confirmation_id"] == o["erp"]["confirmation_id"]
    assert S.get_order(o["id"])["erp"] == o["erp"]
    # the stored order (and its signed receipt) are untouched: erp is attached on read, never signed
    assert "erp" not in store.get_order(o["id"])
    assert "erp" not in signing.verify(o["receipt_jws"], "sous-order+jws")


def test_orders05_mapping():
    o = order(amount=12)
    doc = erp.document(o["id"])
    p = doc["payload"]
    assert p["EDI_DC40"]["IDOCTYP"] == "ORDERS05"
    assert {k["PARVW"]: k["PARTN"] for k in p["E1EDKA1"]} == {"AG": "0000104711", "WE": "0000104711"}
    assert p["E1EDK02"][0] == {"QUALF": "001", "BELNR": o["id"], "DATUM": "20261113"}
    item = p["E1EDP01"][0]
    assert item["E1EDP19"][0]["IDTNR"] == "MILK" and item["MENGE"] == "12" and item["MENEE"] == "ST"
    assert item["E1EDP20"][0]["EDATU"] == "20261116"
    assert doc["erp"]["payload_hash"] == erp.payload_hash(p)


def test_pending_order_not_sent_until_approved():
    S.issue_mandate(Mandate(id="M", buyer="Hotel Aurora, Helsinki", customer_number="0000104711", agent_id="a",
                            issued_by="chef", auto_approve_limit_eur=1, min_delivery_reliability=None))
    o = order()
    assert o["status"] == "pending_approval" and "erp" not in o and erp.document(o["id"]) is None
    approved = S.decide(o["id"], "Anna", True)
    assert approved["erp"]["status"] == "confirmed"


def test_failure_is_queued_and_retried_idempotently(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(erp, "_clock", lambda: t[0])
    monkeypatch.setattr(erp, "ERP_URL", "https://erp.invalid/orders")
    calls = []

    def flaky(endpoint, payload, oid):
        calls.append(oid)
        if len(calls) < 3:
            raise erp.SendError("HTTP 503: down")
        return {"status": "confirmed", "confirmation_id": "4500012345"}

    monkeypatch.setattr(erp, "_send", flaky)
    o = order(key="k1")
    assert o["status"] == "ordered"  # the order stands whatever the ERP does
    assert o["erp"]["status"] == "failed" and o["erp"]["attempts"] == 1 and o["erp"]["next_retry_at"]
    assert erp.process_due() == 0  # not due yet
    t[0] += 31
    assert erp.process_due() == 1
    assert S.get_order(o["id"])["erp"]["attempts"] == 2
    t[0] += 61
    erp.process_due()
    e = S.get_order(o["id"])["erp"]
    assert e["status"] == "confirmed" and e["confirmation_id"] == "4500012345"
    # an idempotent retry of the order and later sweeps don't send again
    assert S.place_order("a", "M", o["quote_id"], "k1")["id"] == o["id"]
    t[0] += 10000
    erp.process_due()
    assert len(calls) == 3
    assert len(events("order.erp_failed")) == 2 and len(events("order.erp_confirmed")) == 1


def test_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(erp, "ERP_URL", "stub:fail")
    t = [0.0]
    monkeypatch.setattr(erp, "_clock", lambda: t[0])
    o = order()
    for _ in range(erp.MAX_ATTEMPTS + 2):
        t[0] += 7200
        erp.process_due()
    e = S.get_order(o["id"])["erp"]
    assert e["status"] == "failed" and e["gave_up"] and e["attempts"] == erp.MAX_ATTEMPTS
    assert len(events("order.erp_gave_up")) == 1


def test_sandbox_orders_always_use_stub(monkeypatch):
    monkeypatch.setattr(erp, "ERP_URL", "https://erp.example/orders")
    assert erp.endpoint_for({"mandate_id": "M", "sandbox": "SBX-7Q2K"}) == "stub"
    assert erp.endpoint_for({"mandate_id": "M"}) == "https://erp.example/orders"


def test_api_erp_and_account_endpoints():
    c = TestClient(api.app)
    o = order()
    r = c.get(f"/orders/{o['id']}/erp")
    assert r.status_code == 200 and r.json()["payload"]["EDI_DC40"]["MESTYP"] == "ORDERS"
    assert c.get(f"/orders/{o['id']}").json()["erp"]["status"] == "confirmed"
    a = c.get("/accounts/0000104711").json()
    assert a["credit"]["sous_open_orders_eur"] == o["total_inc_vat"]
    r = c.patch("/accounts/0000104711", json={"payment_block": True})
    assert r.status_code == 200 and r.json()["payment_block"] is True
    assert events("account.updated")


# --- durability: order and outbox row commit together; the sweep reconciles anything missing --------------------

def test_outbox_insert_failure_rolls_back_the_order(monkeypatch):
    """QA (board 6875727497): INSERT INTO erp_outbox fails once while the order store works. The order must not be
    left `ordered` without an outbox row: the transaction rolls both back, the quote is free again, and the
    agent's retry with the same idempotency key goes through and is sent."""
    import sqlite3
    real = store._exec
    fail = [True]

    def flaky(sql, args=()):
        if fail[0] and sql.startswith("INSERT INTO erp_outbox") and "SELECT" not in sql:
            fail[0] = False
            raise sqlite3.OperationalError("disk I/O error")
        return real(sql, args)

    erp._ensure()
    monkeypatch.setattr(store, "_exec", flaky)
    q = quote()
    with pytest.raises(sqlite3.OperationalError):
        S.place_order("a", "M", q["id"], "k-qa")
    assert store.list_orders(status="ordered") == []
    assert store.get_quote(q["id"])["_order_id"] is None
    o = S.place_order("a", "M", q["id"], "k-qa")
    assert o["status"] == "ordered" and o["erp"]["status"] == "confirmed"


def test_crash_between_order_and_outbox_is_reconciled_by_sweep():
    """An order stored as ordered with no outbox row (a crash between steps, or a write path that skipped the
    outbox) is queued and sent by the next sweep, once."""
    o = order()
    crashed = {**o, "id": "PO-CRASHED1", "idempotency_key": None}
    crashed.pop("erp", None)
    store.insert_order(crashed)
    assert erp.document("PO-CRASHED1") is None
    assert erp.process_due() == 1
    assert S.get_order("PO-CRASHED1")["erp"]["status"] == "confirmed"
    assert [x["subject"] for x in events("order.erp_reconciled")] == ["PO-CRASHED1"]
    assert erp.process_due() == 0 and len(events("order.erp_reconciled")) == 1


def test_orders_from_before_activation_are_never_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "old.db")
    monkeypatch.setattr(store, "_conn", None)
    store.insert_order({"id": "PO-OLD", "quote_id": "Q", "mandate_id": "M", "agent_id": "a", "status": "ordered",
                        "total_ex_vat": 1.0, "created_at": "2026-11-01T10:00:00+02:00",
                        "updated_at": "2026-11-01T10:00:00+02:00", "lines": []})
    assert erp.process_due() == 0
    assert erp.activated_at() and erp.document("PO-OLD") is None and erp.status("PO-OLD") is None
    assert not events("order.erp_sent")


def test_approval_writes_outbox_in_same_transaction(monkeypatch):
    S.issue_mandate(Mandate(id="M", buyer="Hotel Aurora, Helsinki", customer_number="0000104711", agent_id="a",
                            issued_by="chef", auto_approve_limit_eur=1, min_delivery_reliability=None))
    o = order()

    def down(row):
        raise RuntimeError("outbox down")

    monkeypatch.setattr(erp, "write", down)
    with pytest.raises(RuntimeError):
        S.decide(o["id"], "Anna", True)
    assert store.get_order(o["id"])["status"] == "pending_approval"  # rolled back: still waiting, nothing lost
