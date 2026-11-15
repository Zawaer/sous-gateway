"""Approve with changes vs. a concurrent approval of the original (rene-codex-qa, board 6876738865 and 2026-11-14
21:25Z): the change call answered 409 not_pending but BOTH orders ended up ordered and both went to the ERP.
Exactly one outcome must win, and the outbox must never send a replacement whose original wasn't replaced."""

from fastapi.testclient import TestClient

from sous import approve_changes as AC, erp, service as S, store
from tests.test_amendments import env, issue, PRODUCTS  # noqa: F401  same offline setup (EUR 1.00 each)
from tests.test_approve_changes import change, held


def _client():
    return TestClient(__import__("sous.api", fromlist=["app"]).app)


def _sent():
    """Order ids the (stub) ERP received."""
    return {r["order_id"] for r in store._exec("SELECT order_id FROM erp_outbox WHERE status IN "
                                               "('sent', 'confirmed', 'simulated')", ())[0]}


def test_original_approved_while_the_replacement_is_priced_places_nothing(monkeypatch):
    issue(auto_approve_limit_eur=5, hard_limit_eur=50)
    c = _client()
    o = held(10)
    real = S._request_quote

    def pricing(req, **kw):  # the second approver approves the original while the change is being priced
        out = real(req, **kw)
        assert S.decide(o["id"], "Bea", True)["status"] == "ordered"
        return out

    monkeypatch.setattr(S, "_request_quote", pricing)
    before = len(store.list_orders())
    r = change(c, o["id"], (0, 4))
    assert r.status_code == 409 and r.json()["error"] == "not_pending", r.text
    orders = store.list_orders()
    assert len(orders) == before  # no replacement stored at all
    assert [x["id"] for x in orders if x["status"] == "ordered"] == [o["id"]]
    erp.process_due()
    assert _sent() == {o["id"]}


def test_original_decided_by_another_worker_after_placing_withdraws_the_replacement(monkeypatch):
    """The cross-process window: the replacement is already ordered (outbox row pending) when another worker
    approves the original. The replacement is withdrawn, its outbox row cancelled, the ERP sees only the original."""
    issue(auto_approve_limit_eur=5, hard_limit_eur=50)
    c = _client()
    o = held(10)
    seen = {}

    def other_worker(order_id, new):
        seen["new"] = new["id"]
        assert erp.attempt(new["id"])["status"] == "pending"  # the sweep must not send an unfinished replacement
        S._decide(order_id, "Bea", True, None)  # another process, not behind this process's lock

    monkeypatch.setattr(AC, "_before_commit", other_worker)
    r = change(c, o["id"], (0, 4))
    assert r.status_code == 409 and r.json()["error"] == "not_pending", r.text
    assert store.get_order(o["id"])["status"] == "ordered"
    new = store.get_order(seen["new"])
    assert new["status"] == "rejected" and not new.get("approval_changes")
    assert erp._get(new["id"])["status"] == "cancelled"
    erp.process_due()
    erp.attempt(new["id"])
    assert _sent() == {o["id"]}
    assert store.verify_chain()["ok"]


def test_outbox_never_sends_a_replacement_whose_original_was_not_replaced(monkeypatch):
    issue(auto_approve_limit_eur=5, hard_limit_eur=50)
    o = held(10)
    r = change(_client(), o["id"], (0, 4))
    assert r.status_code == 200
    new = r.json()["order"]
    assert new["id"] in _sent()  # the normal path still sends the replacement
    # a forged/stale state: a queued replacement whose original is not closed as replaced by it is never sent
    o2 = held(10)
    store._exec("UPDATE erp_outbox SET status = 'pending' WHERE order_id = ?", (new["id"],))
    fake = {**store.get_order(new["id"]), "approval_changes": {"from_order": o2["id"]}}
    store.update_order(fake)
    assert erp.attempt(new["id"])["status"] == "cancelled"
