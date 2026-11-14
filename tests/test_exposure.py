"""Stock exposure policy (sous/exposure.py, SOUS_STOCK_EXPOSURE). Offline: test_options' catalog and Valio stubs."""

import json

import pytest
from fastapi.testclient import TestClient

from test_options import AVAIL, MON, fake_suggest, offer, offline  # noqa: F401  (autouse fixture)

from sous import access, config, exposure, signing, service as S, substitutes

DAY = MON.isoformat()
APPROVER = access.Reader(everything=True, kinds=["approver"])


def ask(product_ids, amount=100, approver=False, mandate="M-T"):
    return S.request_quote(S.QuoteRequest(agent_id="agent", mandate_id=mandate, delivery_date=MON,
                                          items=[S.QuoteItem(product_ids=product_ids, amount=amount, unit="L")]),
                           approver=approver)


def test_levels():
    L = exposure.level
    assert L("Stock", 300, 100) == "ample"            # >= 3 x the request
    assert L("Stock", 299, 100) == "limited"          # covers it, not 3 x
    assert L("Stock", 40, 100) == "short"
    assert L("Stock", 5000, 4000) == "ample"          # high absolute stock
    assert L("Stock", 3000, 4000) == "short"          # ... but never ample when it doesn't cover the request
    assert L("Stock", 9999999999, 10**6) == "ample"   # Valio's "no practical limit"
    assert L("Stock", 0, 10) == "none"
    assert L("Stock", 50, 10, has_date=False) == "none"
    assert L("Terminal", 999999, 10) == "unknown"
    assert L(None, None, 10) == "unknown"
    AVAIL["C"] = ("2026-11-16", 250, None)
    assert offer("C")["stock_level"] == "limited" and offer("A")["stock_level"] == "ample"
    AVAIL["C"] = None
    assert offer("C")["stock_level"] == "none"


def test_mode_default_and_fallback(monkeypatch):
    monkeypatch.setattr(config, "STOCK_EXPOSURE", "buckets")
    assert not exposure.exact() and not exposure.exact(approver=True)
    monkeypatch.setattr(config, "STOCK_EXPOSURE", "EXACT")
    assert exposure.exact()
    monkeypatch.setattr(config, "STOCK_EXPOSURE", "approver_exact")
    assert exposure.exact(approver=True) and not exposure.exact()
    monkeypatch.setattr(config, "STOCK_EXPOSURE", "everything-please")  # typo -> the safe default
    assert exposure.mode() == "buckets"


def test_partial_stock_message_redacted():
    m = {"issues": [{"code": "PARTIAL_STOCK", "message": "Only 40 ST in stock; 100 needed."}], "available_amount": 40}
    v = exposure.view(m)
    assert v == {"issues": [{"code": "PARTIAL_STOCK",
                             "message": "Not enough in stock for the 100 ST needed (stock level: short)."}],
                 "available_amount": None}
    assert m["available_amount"] == 40  # never mutates the input


def test_buckets_quote_is_signed_redacted_and_still_split_on_exact_stock(monkeypatch):
    monkeypatch.setattr(config, "STOCK_EXPOSURE", "buckets")
    AVAIL["C"] = ("2026-11-16", 40, None)  # cheapest, but only 40 of the 100 L
    r = ask(["A", "C"])
    q = r["quote"]
    by = {l["product_id"]: l for l in q["lines"]}
    # the engine used the exact 40: C is split off at 40, A covers the rest (the line quantity is what can be had)
    assert by["C"]["quantity"] == 40 and by["A"]["quantity"] == 60
    assert by["C"]["stock_level"] == "short" and by["A"]["stock_level"] == "ample"
    assert all(l["available_amount"] is None for l in q["lines"])
    assert signing.verify(r["jws"], "sous-quote+jws") == q  # the JWS carries the same redacted view
    # the stored quote is the signed one, and it orders normally
    o = S.place_order("agent", "M-T", q["id"])
    assert o["status"] in ("ordered", "pending_approval")


def test_options_lines_carry_levels_not_amounts(monkeypatch):
    monkeypatch.setattr(config, "STOCK_EXPOSURE", "buckets")
    AVAIL["C"] = ("2026-11-16", 40, None)
    AVAIL["A"] = ("2026-11-18", None, None)  # the rest only later: the quote offers options
    AVAIL["S"] = ("2026-11-16", 70, None)
    monkeypatch.setattr(substitutes, "suggest", fake_suggest(
        [("S", "better", "compatible", ["Only 70 ST in stock; 100 needed."])]))
    q = ask(["A", "C"])["quote"]
    assert q.get("options") and any(o["substitute"] for o in q["options"])
    lines = [l for o in q["options"] for l in o["lines"]]
    assert all(l["available_amount"] is None and l["stock_level"] in exposure.LEVELS for l in lines)
    assert "in stock;" not in json.dumps(q)


def test_exact_mode_keeps_todays_behaviour(monkeypatch):
    monkeypatch.setattr(config, "STOCK_EXPOSURE", "exact")
    AVAIL["C"] = ("2026-11-16", 40, None)
    q = ask(["A", "C"])["quote"]
    assert {l["product_id"]: l["available_amount"] for l in q["lines"]} == {"C": 40, "A": None}


def test_approver_exact(monkeypatch):
    monkeypatch.setattr(config, "STOCK_EXPOSURE", "approver_exact")
    AVAIL["C"] = ("2026-11-16", 40, None)
    agent = {l["product_id"]: l["available_amount"] for l in ask(["A", "C"])["quote"]["lines"]}
    seller = {l["product_id"]: l["available_amount"] for l in ask(["A", "C"], approver=True)["quote"]["lines"]}
    assert agent["C"] is None and seller["C"] == 40


def test_rest_approver_code_decides(monkeypatch):
    from sous.api import app
    monkeypatch.setattr(config, "STOCK_EXPOSURE", "approver_exact")
    monkeypatch.setattr(config, "APPROVER_CODE", "s3cret-test")
    AVAIL["C"] = ("2026-11-16", 40, None)
    body = {"agent_id": "agent", "mandate_id": "M-T", "delivery_date": DAY,
            "items": [{"product_ids": ["A", "C"], "amount": 100, "unit": "L"}]}
    c = TestClient(app)
    amounts = lambda r: {l["product_id"]: l["available_amount"] for l in r.json()["quote"]["lines"]}  # noqa: E731
    assert amounts(c.post("/quotes", json=body))["C"] is None
    assert amounts(c.post("/quotes", json=body, headers={"X-Approver-Code": "wrong"}))["C"] is None
    assert amounts(c.post("/quotes", json=body, headers={"X-Approver-Code": "s3cret-test"}))["C"] == 40


def test_mcp_quote_is_redacted(monkeypatch):
    from sous import mcp_server
    monkeypatch.setattr(config, "STOCK_EXPOSURE", "buckets")
    AVAIL["C"] = ("2026-11-16", 40, None)
    r = mcp_server.request_quote(agent_id="agent", mandate_id="M-T", delivery_date=DAY,
                                 items=[S.QuoteItem(product_ids=["A", "C"], amount=100, unit="L")])
    q = r["quote"] if isinstance(r, dict) else r.structuredContent["quote"]
    assert all(l["available_amount"] is None for l in q["lines"])
    assert {l["stock_level"] for l in q["lines"]} == {"short", "ample"}


def test_substitutes_and_suggestions_are_redacted(monkeypatch):
    from sous import suggest
    monkeypatch.setattr(config, "STOCK_EXPOSURE", "approver_exact")
    AVAIL["S"] = ("2026-11-16", 70, None)
    monkeypatch.setattr(substitutes, "suggest", lambda *a, **k: {
        **fake_suggest([("S", "better", "compatible", ["Only 70 ST in stock; 100 needed."])])(*a, **k),
        "constraints": {}, "no_safe_substitute": None})
    r = S.suggest_substitutes("agent", "A", MON, 100, "L", "M-T")
    assert r["options"][0]["offer"]["available_amount"] is None
    assert r["options"][0]["offer"]["stock_level"] == "short"
    assert "70" not in json.dumps(r["options"][0]["compromises"])
    seller = S.suggest_substitutes("agent", "A", MON, 100, "L", "M-T", reader=APPROVER)
    assert seller["options"][0]["offer"]["available_amount"] == 70

    monkeypatch.setattr(suggest, "_suggestions", lambda *a: {"suggestions": [{"offer": offer("S")}]})
    assert suggest.suggestions("M-T", "agent", MON)["suggestions"][0]["offer"]["available_amount"] is None
    assert suggest.suggestions("M-T", "agent", MON, reader=APPROVER)["suggestions"][0]["offer"]["available_amount"] == 70
