"""A fresh price check must not discard the buyer's words or unresolved uncertainty."""
from datetime import timedelta

import pytest

from sous import quote as Q, service as S, signing, store
from test_rules_gaps import env, issue, clock, PRICE, MON


@pytest.mark.parametrize("quote_risks,order_risks", [
    (["Quantity needs confirmation"], None),
    (None, ["Quantity needs confirmation"]),
    (["Quantity needs confirmation"], ["Check suitability", "Quantity needs confirmation"]),
])
def test_requote_retains_uncertainty_and_stays_held(monkeypatch, quote_risks, order_risks):
    monkeypatch.setenv("SOUS_APPROVAL_RECHECK", "1")
    monkeypatch.setattr(Q, "prefetch", lambda *a, **k: None)
    issue(auto_approve_limit_eur=5000)
    q = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=MON,
        buyer_request="10 l milk", declared_risks=quote_risks,
        items=[S.QuoteItem(product_ids=["MILK"], amount=10, unit="L")]))["quote"]
    original = S.place_order("a", "M", q["id"], declared_risks=order_risks)
    assert original["status"] == "pending_approval"
    clock["now"] += timedelta(hours=2)
    PRICE["eur"] = 1.3
    expired = S.decide(original["id"], "chef", True)
    assert expired["status"] == "expired"
    fresh = store.get_quote(expired["requote"]["quote_id"])
    expected = list(dict.fromkeys((quote_risks or []) + (order_risks or [])))
    assert fresh.get("declared_risks") == expected
    assert fresh.get("buyer_request") == "10 l milk"
    assert fresh["mandate_preview"]["outcome"] == "ask"
    assert signing.verify(fresh["_jws"], "sous-quote+jws")["declared_risks"] == expected
    replacement = S.place_order("a", "M", fresh["id"])
    assert replacement["status"] == "pending_approval"


def test_requote_prefers_the_words_given_when_ordering(monkeypatch):
    monkeypatch.setenv("SOUS_APPROVAL_RECHECK", "1")
    monkeypatch.setattr(Q, "prefetch", lambda *a, **k: None)
    issue(auto_approve_limit_eur=50)
    q = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=MON,
        buyer_request="60 l milk", items=[S.QuoteItem(product_ids=["MILK"], amount=60, unit="L")]))["quote"]
    original = S.place_order("a", "M", q["id"], buyer_request="60 litres of milk for Monday")
    clock["now"] += timedelta(hours=2)
    PRICE["eur"] = 1.3
    expired = S.decide(original["id"], "chef", True)
    fresh = store.get_quote(expired["requote"]["quote_id"])
    assert fresh.get("buyer_request") == "60 litres of milk for Monday"
    assert fresh["items"] == q["items"]
