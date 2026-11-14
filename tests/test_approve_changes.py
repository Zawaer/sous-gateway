"""Approve with changes (sous/approve_changes.py): the approver edits quantities; re-quoted live, re-checked."""

import pytest
from fastapi.testclient import TestClient

from sous import service as S, store
from tests.test_amendments import env, issue, order, PRODUCTS  # noqa: F401  same offline setup (EUR 1.00 each)


def change(c, oid, *changes, by="Anna"):
    return c.post(f"/orders/{oid}/approve-with-changes",
                  json={"by": by, "changes": [{"line": i, "quantity": q} for i, q in changes]})


def held(amount=10):
    q = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=__import__("tests.test_amendments",
                        fromlist=["MON"]).MON, items=[S.QuoteItem(product_ids=["OAT"], amount=amount, unit="SALES_UNIT")]))
    o = S.place_order("a", "M", q["quote"]["id"])
    assert o["status"] == "pending_approval", o["decision"]
    return o


def test_smaller_quantity_goes_through_and_replaces_the_original():
    issue(auto_approve_limit_eur=5, hard_limit_eur=50)
    c = TestClient(__import__("sous.api", fromlist=["app"]).app)
    o = held(10)
    r = change(c, o["id"], (0, 4))
    assert r.status_code == 200, r.text
    new, old = r.json()["order"], r.json()["replaced"]
    assert new["status"] == "ordered" and new["lines"][0]["quantity"] == 4 and new["total_ex_vat"] == 4.0
    assert new["approval_changes"]["from_order"] == o["id"] and new["approval_changes"]["changes"][0]["to"] == 4
    assert old["status"] == "rejected" and old["approval"]["replaced_by"] == new["id"]
    assert store.get_order(o["id"])["approval"]["replaced_by"] == new["id"]
    assert [e for e in store.audit_events(limit=200) if e["event"] == "order.approved_with_changes"]
    assert store.verify_chain()["ok"]


def test_still_over_the_limit_is_approved_by_the_same_human_and_block_changes_nothing():
    issue(auto_approve_limit_eur=5, hard_limit_eur=50)
    c = TestClient(__import__("sous.api", fromlist=["app"]).app)
    o = held(10)
    r = change(c, o["id"], (0, 8))
    assert r.status_code == 200 and r.json()["order"]["status"] == "ordered"
    assert r.json()["order"]["approval"]["by"] == "Anna"

    o2 = held(10)
    before = len(store.list_orders())
    r = change(c, o2["id"], (0, 60))  # over the EUR 50 hard limit
    assert r.status_code == 409 and r.json()["error"] == "changes_refused"
    assert store.get_order(o2["id"])["status"] == "pending_approval" and len(store.list_orders()) == before
    assert change(c, o2["id"], (0, 10)).status_code == 422  # nothing changed
    assert change(c, o2["id"], (0, 500)).status_code == 422  # over 10x


def test_passkey_mandate_is_refused_up_front_and_leaves_nothing_behind():
    issue(auto_approve_limit_eur=5, hard_limit_eur=50, approval_method="passkey")
    c = TestClient(__import__("sous.api", fromlist=["app"]).app)
    o = held(10)
    before = len(store.list_orders())
    for q in (4, 8):
        r = change(c, o["id"], (0, q))
        assert r.status_code == 403 and r.json()["error"] == "passkey_required"
    assert store.get_order(o["id"])["status"] == "pending_approval" and len(store.list_orders()) == before
