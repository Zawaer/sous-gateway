"""Approve-by deadlines tied to Valio's order cut-off (sous/deadline.py). Offline: no Valio calls, no data/."""

from datetime import date, datetime, timedelta

import pytest

from sous import catalog, clock, config, quote as Q, rules, service as S, signing, store
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)       # Fri; cut-off Sun 15.11 17:00 for Mon 16.11
CUT = "2026-11-15T17:00:00+0200"
SENT: list = []


def product(pid="1"):
    return {"id": pid, "name": "Milk 1 l", "names": {"fi": "", "en": "", "sv": ""}, "brand": "", "vendor": "",
            "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST",
            "units": {"ST": 1.0}, "allowed_lot_size": 1.0, "temperature": "chilled", "labels": [], "claims": [],
            "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": []},
            "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
            "official_substitutes": [], "deleted": False}


PRICE = {"catalogPrice": {"value": 1.0, "valueWithTax": 1.14, "validTo": None}, "comparisonPrice": {"value": 1.0},
         "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: product(pid))
    at(monkeypatch, NOW)
    SENT.clear()
    monkeypatch.setattr(S.notify, "send", lambda event, o, m: SENT.append((event, S.notify.compose(event, o, m))))
    S.issue_mandate(Mandate(id="M", buyer="Hotel", agent_id="a", issued_by="chef", auto_approve_limit_eur=100))
    yield
    signing._key.cache_clear()


def at(monkeypatch, t):
    monkeypatch.setattr(S, "_now", lambda: t)


def held(placed=NOW, qid="Q-1"):
    """A 500 € order (over the 100 € auto-approve limit): pending_approval."""
    line = Q.build_offer(product(), PRICE, {"logisticModel": "Stock", "closingTime": CUT, "salesBlock": False,
                                            "scheduleLines": [{"salesBlock": False, "availableAmount": 9999,
                                                               "availableDate": "2026-11-16", "salesUnit": "ST"}]},
                         date(2026, 11, 16), 500, "L", placed)
    q = {"id": qid, "agent_id": "a", "mandate_id": "M", "issued_at": placed.isoformat(),
         "expires_at": (placed + timedelta(minutes=5)).isoformat(), "requested_delivery_date": "2026-11-16",
         "currency": "EUR", "lines": [line], "total_ex_vat": line["line_total_ex_vat"],
         "total_inc_vat": line["line_total_inc_vat"], "uncovered": [], "alternatives_considered": []}
    store.put_quote(q, signing.sign(q, "sous-quote+jws"))
    o = S.place_order("a", "M", qid)
    assert o["status"] == "pending_approval"
    return o


def test_held_order_has_approve_by_cutoff_minus_margin():
    o = held()
    assert o["approve_by"] == "2026-11-15T16:45:00+02:00"
    assert o["approve_by_detail"]["basis"] == "cutoff" and o["approve_by_detail"]["margin_minutes"] == 15
    assert signing.verify(o["receipt_jws"], "sous-order+jws")["approve_by"] == o["approve_by"]
    ev = [e for e in store.audit_events() if e["event"] == "order.pending_approval"][-1]
    assert ev["data"]["approve_by"] == o["approve_by"]
    [(event, n)] = SENT
    assert event == "order.ask" and "Approve by Sun 15.11 16:45 or delivery on Mon 16.11 is lost." in n["message"]
    assert n["deadline"] == o["approve_by"]


def test_held_inside_the_margin_gets_the_cutoff_itself(monkeypatch):
    t = datetime(2026, 11, 15, 16, 50, tzinfo=config.TZ)
    at(monkeypatch, t)
    o = held(placed=t)
    assert o["approve_by"] == "2026-11-15T17:00:00+02:00" and o["approve_by_detail"]["margin_minutes"] == 0


def test_approve_before_approve_by_orders(monkeypatch):
    o = held()
    at(monkeypatch, datetime(2026, 11, 15, 16, 40, tzinfo=config.TZ))
    assert S.decide(o["id"], "chef", True)["status"] == "ordered"


def test_approve_after_approve_by_is_refused_and_order_expires(monkeypatch):
    o = held()
    SENT.clear()
    at(monkeypatch, datetime(2026, 11, 15, 16, 50, tzinfo=config.TZ))  # past 16:45, before the 17:00 cut-off
    with pytest.raises(S.GatewayError) as e:
        S.decide(o["id"], "chef", True)
    assert e.value.code == "approval_deadline_passed" and e.value.status == 409
    assert "2026-11-17" in e.value.message
    x = store.get_order(o["id"])
    assert x["status"] == "expired_cutoff" and x["approval"] is None and x["approval_url"] is None
    assert x["expiry"]["lost_delivery_date"] == "2026-11-16" and x["expiry"]["next_delivery_date"] == "2026-11-17"
    assert signing.verify(x["receipt_jws"], "sous-order+jws")["status"] == "expired_cutoff"
    [(event, n)] = SENT
    assert event == "order.expired" and "delivery on Mon 16.11 is lost" in n["message"]
    assert "quote it for Tue 17.11" in n["message"]
    assert [e["data"]["next_delivery_date"] for e in store.audit_events() if e["event"] == "order.expired_cutoff"] \
        == ["2026-11-17"]
    with pytest.raises(S.GatewayError) as e:  # rejecting afterwards is the same clear error
        S.decide(o["id"], "chef", False)
    assert e.value.code == "approval_deadline_passed"
    assert rules.get("approval_deadline_passed")["http_status"] == 409


def test_reads_and_sweep_expire_lazily(monkeypatch):
    a, b = held(), held(qid="Q-2")
    at(monkeypatch, datetime(2026, 11, 15, 17, 30, tzinfo=config.TZ))
    assert S.get_order(a["id"])["status"] == "expired_cutoff"
    assert S.sweep_overdue() == 1 and store.get_order(b["id"])["status"] == "expired_cutoff"
    assert S.sweep_overdue() == 0


def test_demo_clock_drives_the_deadline(monkeypatch):
    o = held()
    monkeypatch.setattr(S, "_now", lambda: clock.now())
    try:
        clock.configure("2026-11-15T16:30:00", "frozen")
        assert S.get_order(o["id"])["status"] == "pending_approval"
        clock.configure("2026-11-15T16:46:00", "frozen")
        assert S.get_order(o["id"])["status"] == "expired_cutoff"
        assert store.get_order(o["id"])["expiry"]["clock"]["mode"] == "demo"
    finally:
        clock.configure(None)
