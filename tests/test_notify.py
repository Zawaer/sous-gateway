"""Notifications: messages, channels, feed and the service hooks. Offline: httpx is mocked."""

import hashlib
import hmac
import json
import threading
from datetime import date, datetime

import httpx
import pytest

from sous import catalog, config, notify, notifications_store, quote as Q, service as S, signing, store
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)


def product(pid="1", declared=True):
    return {
        "id": pid, "name": "Lactose-free milk 1 l", "names": {"fi": "", "en": "", "sv": ""}, "brand": "", "vendor": "",
        "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
        "allowed_lot_size": 0.01, "temperature": "chilled", "labels": [], "claims": ["FREE_FROM_LACTOSE"],
        "allergens": {"declared": declared, "contains": [], "may_contain": [], "free_from": []},
        "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": 1.4,
        "official_substitutes": [], "deleted": False,
    }


PRICE = {"catalogPrice": {"value": 1.4, "valueWithTax": 1.59, "validTo": "2026-12-31T00:00:00+0200"},
         "comparisonPrice": {"value": 1.4}, "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}
AVAIL = {"logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": False,
         "scheduleLines": [{"salesBlock": False, "availableAmount": 5000, "availableDate": "2026-11-16",
                            "salesUnit": "ST"}]}


class Calls:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, url, **kw):
        self.calls.append({"url": url, **kw})
        if self.fail:
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(200, request=httpx.Request("POST", url))


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://sous.example")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: product(pid))
    monkeypatch.setattr(S, "_now", lambda: NOW)
    for k in ("SOUS_NTFY_TOPIC", "SOUS_NTFY_SERVER", "SOUS_NTFY_TOKEN",
              "SOUS_NOTIFY_WEBHOOK_URL", "SOUS_NOTIFY_WEBHOOK_SECRET"):
        monkeypatch.delenv(k, raising=False)
    calls = Calls()
    monkeypatch.setattr(httpx, "post", calls)  # nothing leaves the machine
    yield calls
    signing._key.cache_clear()


def wait():
    for t in threading.enumerate():
        if t.name.startswith("notify-"):
            t.join(5)


def quote(qid="Q-1", amount=1000):
    line = Q.build_offer(product(), PRICE, AVAIL, date(2026, 11, 16), amount, "L", NOW)
    q = {"id": qid, "agent_id": "a", "mandate_id": "M", "issued_at": NOW.isoformat(),
         "expires_at": "2026-11-13T23:00:00+02:00", "requested_delivery_date": "2026-11-16", "currency": "EUR",
         "lines": [line], "total_ex_vat": line["line_total_ex_vat"], "total_inc_vat": line["line_total_inc_vat"],
         "uncovered": [], "alternatives_considered": []}
    store.put_quote(q, signing.sign(q, "sous-quote+jws"))
    return qid


def mandate(**kw):
    m = Mandate(**{"id": "M", "buyer": "Hotel", "agent_id": "a", "issued_by": "chef", **kw})
    S.issue_mandate(m)
    return m


def events(name):
    return [e for e in store.audit_events(limit=500) if e["event"] == name]


def test_notify_tier_message_feed_and_ntfy(env, monkeypatch):
    monkeypatch.setenv("SOUS_NTFY_TOPIC", "hotel-test")
    mandate(notify_above_eur=1000, auto_approve_limit_eur=2000)
    o = S.place_order("a", "M", quote())
    wait()
    assert o["status"] == "ordered"
    [n] = notifications_store.list_("Hotel")
    assert n["event"] == "order.notify" and n["audience"] == "buyer" and n["order_id"] == o["id"]
    assert n["message"] == "Ordered 1,000 × Lactose-free milk 1 l for €1,400 ex VAT (above your €1,000 FYI threshold)."
    [c] = env.calls
    assert c["url"] == "https://ntfy.sh" and c["json"]["topic"] == "hotel-test"
    assert c["json"]["click"].endswith(o["id"]) and "actions" not in c["json"]
    sent = events("notification.sent")
    assert {e["data"]["channel"] for e in sent} == {"feed", "ntfy"}


def test_ask_tier_has_deadline_default_and_approve_action(env, monkeypatch):
    monkeypatch.setenv("SOUS_NTFY_TOPIC", "hotel-test")
    monkeypatch.setattr(catalog, "get", lambda pid: product(pid, declared=False))
    mandate(avoid_allergens=["AM"], auto_approve_limit_eur=5000)
    o = S.place_order("a", "M", quote())
    wait()
    assert o["status"] == "pending_approval"
    [n] = notifications_store.list_("Hotel")
    assert n["message"].startswith("Approval needed: Lactose-free milk 1 l has no allergen data")
    assert "Approve by Sun 15.11 16:45 or delivery on Mon 16.11 is lost." in n["message"]
    assert n["data"]["deadline"] == o["approve_by"] == "2026-11-15T16:45:00+02:00"
    assert n["message"].endswith("If nobody answers, it expires and nothing is ordered.")
    assert n["url"] == o["approval_url"]
    act = env.calls[0]["json"]["actions"][0]
    assert act["action"] == "view" and act["url"] == o["approval_url"]
    assert env.calls[0]["json"]["priority"] == 4

    S.decide(o["id"], "chef", False, "Use the Valio one.")
    wait()
    latest = notifications_store.list_("Hotel", limit=1)[0]
    assert latest["event"] == "order.decided" and latest["audience"] == "agent"
    assert latest["message"] == f"chef rejected {o['id']}: Use the Valio one. Nothing was ordered."
    assert len(env.calls) == 1  # agent-side FYI doesn't go to the human's phone


def test_silent_sends_nothing(env):
    mandate()
    S.place_order("a", "M", quote("Q-1", 10))
    wait()
    assert notifications_store.list_() == [] and env.calls == []


def test_block_tells_the_buyer(env, monkeypatch):
    monkeypatch.setenv("SOUS_NTFY_TOPIC", "t")
    mandate(hard_limit_eur=10)
    o = S.place_order("a", "M", quote("Q-2", 100))
    wait()
    [n] = notifications_store.list_()
    assert n["event"] == "order.block" and n["audience"] == "buyer" and n["order_id"] == o["id"]
    assert n["title"] == f"Refused: {o['id']}" and "nothing was ordered" in n["message"]
    [c] = env.calls
    assert c["json"]["tags"] == ["no_entry"]


def test_webhook_is_signed(env, monkeypatch):
    monkeypatch.setenv("SOUS_NOTIFY_WEBHOOK_URL", "https://hooks.example/v")
    monkeypatch.setenv("SOUS_NOTIFY_WEBHOOK_SECRET", "s3cret")
    mandate(notify_above_eur=1000, auto_approve_limit_eur=2000)
    S.place_order("a", "M", quote())
    wait()
    [c] = env.calls
    body, sig = c["content"], c["headers"]["X-Sous-Signature"]
    assert sig == "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    p = json.loads(body)
    assert p["event"] == "order.notify" and p["order"]["status"] == "ordered" and p["order"]["buyer"] == "Hotel"


def test_failed_channel_never_blocks_the_order(env, monkeypatch):
    monkeypatch.setenv("SOUS_NTFY_TOPIC", "hotel-test")
    monkeypatch.setattr(httpx, "post", Calls(fail=True))
    mandate(notify_above_eur=1000, auto_approve_limit_eur=2000)
    o = S.place_order("a", "M", quote())
    wait()
    assert o["status"] == "ordered"
    [f] = events("notification.failed")
    assert f["data"]["channel"] == "ntfy" and "ConnectTimeout" in f["data"]["error"]


def test_broken_feed_never_blocks_the_order(env, monkeypatch):
    monkeypatch.setattr(notifications_store, "add", lambda n: 1 / 0)
    mandate(notify_above_eur=1000, auto_approve_limit_eur=2000)
    assert S.place_order("a", "M", quote())["status"] == "ordered"
    assert events("notification.failed")


def test_rest_routes(env, monkeypatch):
    from fastapi.testclient import TestClient

    from sous import api
    monkeypatch.setattr(config, "APPROVER_CODE", "1234")
    mandate()
    c = TestClient(api.app)
    assert c.post("/notifications/test", json={"mandate_id": "M"}).status_code == 401
    r = c.post("/notifications/test", json={"mandate_id": "M"}, headers={"X-Approver-Code": "1234"})
    assert r.status_code == 200 and r.json()["channels"] == ["feed"]
    assert c.get("/notifications", params={"buyer": "Hotel"}).json() == {"notifications": []}  # not public
    feed = c.get("/notifications", params={"buyer": "Hotel"}, headers={"X-Approver-Code": "1234"}).json()["notifications"]
    assert feed[0]["event"] == "test" and c.get("/notifications", params={"buyer": "Other"}).json() == {
        "notifications": []}
