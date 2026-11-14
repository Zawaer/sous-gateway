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


def held(placed=NOW, qid="Q-1", mid="M"):
    """A 500 € order (over the 100 € auto-approve limit): pending_approval."""
    line = Q.build_offer(product(), PRICE, {"logisticModel": "Stock", "closingTime": CUT, "salesBlock": False,
                                            "scheduleLines": [{"salesBlock": False, "availableAmount": 9999,
                                                               "availableDate": "2026-11-16", "salesUnit": "ST"}]},
                         date(2026, 11, 16), 500, "L", placed)
    q = {"id": qid, "agent_id": "a", "mandate_id": mid, "issued_at": placed.isoformat(),
         "expires_at": (placed + timedelta(minutes=5)).isoformat(), "requested_delivery_date": "2026-11-16",
         "currency": "EUR", "lines": [line], "total_ex_vat": line["line_total_ex_vat"],
         "total_inc_vat": line["line_total_inc_vat"], "uncovered": [], "alternatives_considered": []}
    store.put_quote(q, signing.sign(q, "sous-quote+jws"))
    o = S.place_order("a", mid, qid)
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


def _pre_deadline_order(**kw):
    """QA 06:04Z: a pending order stored before the deadline feature (no approve_by, no approve_by_detail)."""
    o = held(**kw)
    old = {k: v for k, v in store.get_order(o["id"]).items() if k not in ("approve_by", "approve_by_detail")}
    assert store.update_order(old)
    return old


def test_sweep_backfills_old_pending_orders_and_expires_them(monkeypatch):
    o = _pre_deadline_order()
    at(monkeypatch, datetime(2026, 11, 14, 9, 0, tzinfo=config.TZ))  # before the deadline: backfilled, still pending
    assert S.sweep_overdue() == 0
    x = store.get_order(o["id"])
    assert x["status"] == "pending_approval" and x["approve_by"] == "2026-11-15T16:45:00+02:00"
    assert signing.verify(x["receipt_jws"], "sous-order+jws")["approve_by"] == x["approve_by"]
    ev = [e for e in store.audit_events() if e["event"] == "order.approve_by_backfilled"]
    assert [e["subject"] for e in ev] == [o["id"]] and ev[0]["data"]["approve_by"] == x["approve_by"]
    assert S.sweep_overdue() == 0  # idempotent: no second backfill
    assert len([e for e in store.audit_events() if e["event"] == "order.approve_by_backfilled"]) == 1
    SENT.clear()
    at(monkeypatch, datetime(2026, 11, 16, 9, 0, tzinfo=config.TZ))  # past the cut-off and into the delivery day
    assert S.sweep_overdue() == 1
    x = store.get_order(o["id"])
    assert x["status"] == "expired_cutoff" and x["expiry"]["lost_delivery_date"] == "2026-11-16"
    assert [e for e, _ in SENT] == ["order.expired"]


def test_old_pending_order_without_cutoff_gets_the_delivery_day_start(monkeypatch):
    o = _pre_deadline_order()
    assert store.update_order({**o, "lines": [{**l, "order_cutoff": None} for l in o["lines"]]})
    at(monkeypatch, datetime(2026, 11, 16, 0, 30, tzinfo=config.TZ))
    x = S.get_order(o["id"])  # a read backfills and expires too
    assert x["status"] == "expired_cutoff" and x["approve_by"] == "2026-11-16T00:00:00+02:00"
    assert x["approve_by_detail"]["basis"] == "delivery_date"


def test_backfill_leaves_decided_orders_alone():
    o = _pre_deadline_order()
    assert store.update_order({**o, "status": "rejected"})  # decided before the deadline feature
    assert S.sweep_overdue() == 0 and S.get_order(o["id"])["status"] == "rejected"
    assert not store.get_order(o["id"]).get("approve_by")
    assert not [e for e in store.audit_events() if e["event"] == "order.approve_by_backfilled"]


SBX_END = datetime(2026, 11, 15, 14, 38, tzinfo=config.TZ)  # claude.ai test: sandbox copy expired Sun 14:38


def sandbox_mandate(monkeypatch, valid_until=SBX_END, sandbox_end=SBX_END):
    """A sandbox copy of the mandate (valid_until = its sandbox's expiry), its sandbox row stubbed."""
    monkeypatch.setattr(S.sandbox, "_row", lambda sid: {"id": sid, "expires_ts": sandbox_end.timestamp()}
                        if sid == "SBX-TEST" else None)
    S.issue_mandate(Mandate(id="MS", buyer="Hotel (sandbox SBX-TEST)", agent_id="a", issued_by="chef",
                            auto_approve_limit_eur=100, valid_until=valid_until, sandbox="SBX-TEST"))


def test_sandbox_order_must_be_decided_before_the_sandbox_expires(monkeypatch):
    sandbox_mandate(monkeypatch)
    o = held(mid="MS")
    assert o["approve_by"] == "2026-11-15T14:38:00+02:00"  # not 16:45 (cut-off minus margin)
    d = o["approve_by_detail"]
    assert d["basis"] == "sandbox_expires" and d["sandbox"] == "SBX-TEST" and d["margin_minutes"] == 0
    assert d["cutoff"] == "2026-11-15T17:00:00+02:00" and d["delivery_date"] == "2026-11-16"
    [(event, n)] = SENT
    assert event == "order.ask" and "Decide by Sun 15.11 14:38: the sandbox and its mandate expire then" in n["message"]
    assert "is lost" not in n["message"] and n["deadline"] == o["approve_by"]
    SENT.clear()
    at(monkeypatch, datetime(2026, 11, 15, 14, 40, tzinfo=config.TZ))
    with pytest.raises(S.GatewayError) as e:
        S.decide(o["id"], "chef", True)
    assert e.value.code == "approval_deadline_passed" and "sandbox expired" in e.value.message
    x = store.get_order(o["id"])
    assert x["status"] == "expired_cutoff" and x["expiry"]["cause"] == "sandbox_expired"
    assert "sandbox" in x["expiry"]["reason"] and "new sandbox" in x["expiry"]["next_step"]
    [(event, n)] = SENT
    assert event == "order.expired" and "sandbox (and its mandate) expired" in n["message"]


def test_mandate_expiring_before_the_cutoff_sets_approve_by(monkeypatch):
    S.issue_mandate(Mandate(id="ME", buyer="Hotel", agent_id="a", issued_by="chef", auto_approve_limit_eur=100,
                            valid_until=datetime(2026, 11, 14, 12, 0, tzinfo=config.TZ)))
    o = held(mid="ME")
    assert o["approve_by"] == "2026-11-14T12:00:00+02:00"
    assert o["approve_by_detail"]["basis"] == "mandate_expires" and o["approve_by_detail"]["mandate_id"] == "ME"
    assert "the mandate expires then" in SENT[0][1]["message"]
    at(monkeypatch, datetime(2026, 11, 14, 11, 59, tzinfo=config.TZ))
    assert S.decide(o["id"], "chef", True)["status"] == "ordered"  # still in time


def test_mandate_or_sandbox_ending_after_the_cutoff_changes_nothing(monkeypatch):
    sandbox_mandate(monkeypatch, valid_until=datetime(2026, 11, 20, tzinfo=config.TZ),
                    sandbox_end=datetime(2026, 11, 15, 17, 0, tzinfo=config.TZ))
    o = held(mid="MS")
    assert o["approve_by"] == "2026-11-15T16:45:00+02:00" and o["approve_by_detail"]["basis"] == "cutoff"
    assert "Approve by Sun 15.11 16:45 or delivery on Mon 16.11 is lost." in SENT[0][1]["message"]


def test_backfill_caps_by_the_mandate_end(monkeypatch):
    sandbox_mandate(monkeypatch)
    o = _pre_deadline_order(mid="MS")
    at(monkeypatch, datetime(2026, 11, 14, 9, 0, tzinfo=config.TZ))
    assert S.sweep_overdue() == 0
    x = store.get_order(o["id"])
    assert x["approve_by"] == "2026-11-15T14:38:00+02:00" and x["approve_by_detail"]["basis"] == "sandbox_expires"
