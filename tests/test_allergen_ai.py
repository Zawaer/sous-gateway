"""Gemini Decisions API on undeclared products (sous/allergen_ai.py): rule allergen.likely, never 'safe'. Offline."""

from datetime import date, datetime

import httpx
import pytest

from sous import allergen_ai as AI, catalog, config, quote as Q, service as S, signing, store
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
DAY = date(2026, 11, 26)


def product(pid, name, declared):
    return {
        "id": pid, "name": name, "names": {"fi": name, "en": "", "sv": ""}, "brand": "Testi", "vendor": "",
        "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
        "allowed_lot_size": 1.0, "temperature": "ambient", "labels": [], "claims": [],
        "allergens": {"declared": declared, "contains": [], "may_contain": [], "free_from": []},
        "size_from_name": (0.4, "KG"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
        "official_substitutes": [], "deleted": False, "ingredients_fi": "", "ingredients_en": "",
    }


PRODUCTS = {"SPREAD": product("SPREAD", "Hasselpähkinä-suklaalevite 400 g", False),
            "JAM": product("JAM", "Mansikkahillo 400 g", False),
            "SALT": product("SALT", "Merisuola 400 g", True)}
PRODUCTS["SALT"]["allergens"]["free_from"] = ["AM", "AN", "AP"]  # declared free of what the mandate avoids
calls: list[tuple[str, list[str]]] = []
ANSWERS = {"SPREAD": {"AN": 0.97, "AM": 0.9, "AP": 0.05}, "JAM": {"AN": 0.03, "AM": 0.02, "AP": 0.02}}


def fake_offers(product_ids, requested, need=None, need_unit=None):
    pr = {"catalogPrice": {"value": 2.0, "valueWithTax": 2.27, "validTo": None}, "comparisonPrice": {"value": 5.0},
          "comparisonUnit": "KG", "tax": {"taxRate": 13.5}, "error": False}
    av = {"logisticModel": "Stock", "closingTime": "2026-11-25T17:00:00+0200", "salesBlock": False,
          "scheduleLines": [{"salesBlock": False, "availableAmount": 999, "availableDate": "2026-11-26", "salesUnit": "ST"}]}
    return [Q.build_offer(PRODUCTS[p], pr, av, requested, need, need_unit, NOW) for p in product_ids], NOW


def fake_ask(text, codes, client=None):
    pid = next(k for k, p in PRODUCTS.items() if p["name"] in text)
    calls.append((pid, list(codes)))
    if pid == "BROKEN":
        raise httpx.ConnectError("down")
    return {c: {"p": ANSWERS[pid][c], "confidence": max(ANSWERS[pid][c], 1 - ANSWERS[pid][c]), "model": AI.MODEL}
            for c in codes}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(config, "APPROVER_CODE", "")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(pid))
    monkeypatch.setattr(Q, "offers", fake_offers)
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(S.notify, "send", lambda *a, **k: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("SOUS_ALLERGEN_AI", "1")
    monkeypatch.setattr(AI, "ask", fake_ask)
    monkeypatch.setattr(AI, "_calls", [])
    calls.clear()
    S.issue_mandate(Mandate(id="H", buyer="Hospital", agent_id="a", issued_by="chef", auto_approve_limit_eur=5000,
                            avoid_allergens=["AM", "AN", "AP"], unknown_allergens="ask", min_delivery_reliability=None))
    yield
    signing._key.cache_clear()


def order(pid):
    q = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="H", delivery_date=DAY, items=[
        S.QuoteItem(product_ids=[pid], amount=2, unit="SALES_UNIT")]))["quote"]
    return q, S.place_order("a", "H", q["id"])


def rules(o):
    return [f["rule"] for f in o["decision"]["findings"]]


def test_likely_allergen_refuses_an_undeclared_product():
    q, o = order("SPREAD")
    assert o["status"] == "refused" and rules(o) == ["allergen.likely"]
    msg = o["decision"]["findings"][0]["message"]
    assert "tree nuts (97%)" in msg and "milk (90%)" in msg and "gemini-decision-1" in msg
    assert q["mandate_preview"]["outcome"] == "block"
    assert calls == [("SPREAD", ["AM", "AN", "AP"])]  # one request, one yes/no per avoided allergen


def test_a_low_probability_never_makes_a_product_safe():
    _, o = order("JAM")
    assert o["status"] == "pending_approval" and rules(o) == ["allergen.unknown"]


def test_answers_are_cached_so_quote_order_and_approval_agree():
    order("SPREAD")
    order("SPREAD")
    assert len(calls) == 1
    assert AI.assess("SPREAD", ["AN"])["AN"]["p"] == 0.97 and len(calls) == 1


def test_declared_products_and_mandates_without_allergen_rules_make_no_call():
    _, o = order("SALT")
    assert o["status"] == "ordered" and calls == []
    assert AI.assess_lines([{"product_id": "SPREAD"}], []) == {} and calls == []


def test_failure_or_switch_off_means_todays_behaviour(monkeypatch):
    def down(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(AI, "ask", down)
    _, o = order("SPREAD")
    assert rules(o) == ["allergen.unknown"] and AI.stats["errors"] >= 1
    monkeypatch.setattr(AI, "ask", fake_ask)
    monkeypatch.setenv("SOUS_ALLERGEN_AI", "0")
    _, o = order("JAM")
    assert rules(o) == ["allergen.unknown"] and calls == []


def test_per_minute_cap(monkeypatch):
    monkeypatch.setattr(AI, "PER_MIN", 0)
    _, o = order("SPREAD")
    assert rules(o) == ["allergen.unknown"] and calls == []


def test_request_shape_and_response_parsing():
    seen = {}

    class Client:
        def post(self, url, json, headers, timeout):
            seen.update(url=url, body=json, headers=headers)
            return httpx.Response(200, request=httpx.Request("POST", url), json={"answers": {
                "AN": {"type": "noul", "noul": 0.93, "confidence": 0.93}}, "usage": {"total_tokens": 200}})

    from importlib import reload  # the real ask(), not the fixture's fake
    real = reload(AI).ask
    out = real("Hazelnut spread", ["AN"], client=Client())
    assert out == {"AN": {"p": 0.93, "confidence": 0.93, "model": "gemini-decision-1"}}
    assert seen["url"].endswith("/v1beta/decisions") and seen["headers"]["x-goog-api-key"] == "test-key"
    q = seen["body"]["questions"]["AN"]
    assert seen["body"]["model"] == "gemini-decision-1" and q["type"] == "noul" and "hazelnut" in q["instructions"]
