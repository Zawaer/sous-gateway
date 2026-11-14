"""Reference buyer agent over HTTP (agent/server.py), offline: fake models, a fake catalog, mocked Valio Aimo.

Covers the three model-backed chains (menu -> Gemini, photo -> Nemotron, sentence -> buyer agent), the order
button, the guardrails (sandbox token or approver code, per-IP limit, daily budget, image checks) and /usage.
"""

import io
import json
import os
import subprocess
import sys
from datetime import date, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from agent import buyer as B, recipe as R, server as AS, vision as V
from agent.llm import ChatResponse, LLMConfig
from sous import aimo, api, catalog, config, notify, quote as Q, sandbox, service as S, signing, store

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
TODAY = date(2026, 11, 13)
CODE = "1234"


def product(pid, name, size, unit, claims=()):
    return {"id": pid, "name": name, "names": {"fi": name, "en": name, "sv": ""}, "brand": "", "vendor": "",
            "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST",
            "units": {"ST": 1.0}, "allowed_lot_size": 1.0, "temperature": "chilled", "labels": [],
            "claims": list(claims), "allergens": {"declared": True, "contains": [], "may_contain": [],
                                                  "free_from": ["AM", "AN", "AP", "AW", "AE"]},
            "size_from_name": (size, unit), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
            "official_substitutes": [], "deleted": False}


CATALOG = [(product("CREAM", "Whipping cream 1 l", 1.0, "L"), ("kerma",)),
           (product("ONION", "Onion peeled 1 kg", 1.0, "KG"), ("sipuli",)),
           (product("MILK", "Milk lactose free 1 l", 1.0, "L", ("FREE_FROM_LACTOSE",)), ("maito", "milk")),
           (product("BUTTER", "Butter 500 g", 0.5, "KG"), ("voi", "butter"))]
PRODUCTS = {p["id"]: p for p, _ in CATALOG}


def fake_search(query, *, claims=None, limit=20, **kw):
    q = query.lower()
    return [dict(p) for p, words in CATALOG if any(w in q for w in words)
            and (not claims or set(claims) <= set(p["claims"]))][:limit]


class Valio:
    def __init__(self):
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        ids = json.loads(request.content)["products"]
        if request.url.path.endswith("/price"):
            items = [{"productId": p, "catalogPrice": {"value": 1.0, "valueWithTax": 1.14, "validTo": None},
                      "comparisonPrice": {"value": 1.0}, "comparisonUnit": PRODUCTS[p]["size_from_name"][1], "tax": {"taxRate": 13.5},
                      "error": False} for p in ids]
        else:
            items = [{"productId": p, "logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200",
                      "salesBlock": False, "scheduleLines": [{"salesBlock": False, "availableAmount": 99999,
                                                              "availableDate": "2026-11-16", "salesUnit": "ST"}]}
                     for p in ids]
        return httpx.Response(200, json={"timeNow": "2026-11-13T21:00:00+0200", "products": items})


def offers_at_now(product_ids, requested, need=None, need_unit=None):
    products = {pid: p for pid in product_ids if (p := catalog.get(pid))}
    prices, avail = aimo.prices(list(products), requested), aimo.availability(list(products), requested)
    return [Q.build_offer(p, prices.get(i), avail.get(i), requested, need, need_unit, NOW)
            for i, p in products.items()], NOW


class FakeLLM:
    """Scripted model: answers are dicts/strings, or callables (messages -> message dict) for tool calling."""

    def __init__(self, provider, model, answers, price=(1.5, 7.5)):
        self.cfg = LLMConfig(provider=provider, base_url="", model=model, price_in=price[0], price_out=price[1])
        self.answers = list(answers)

    def chat(self, messages, tools=None, response_format=None):
        a = self.answers.pop(0)
        msg = a(messages) if callable(a) else {"role": "assistant",
                                               "content": a if isinstance(a, str) else json.dumps(a)}
        return ChatResponse(msg, "stop", {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
                            7, 0)


SOUP = {"dishes": [{"name": "Kermainen sipulikeitto", "ingredients": [
    {"name_fi": "kuohukerma", "name_en": "whipping cream", "amount_per_portion": 50, "unit": "ml"},
    {"name_fi": "sipuli", "name_en": "onion", "amount_per_portion": 100, "unit": "g"}]}],
    "portions": 40, "delivery_date": "2026-11-16", "stock_on_hand": []}


def item(text, name, q, unit, conf=0.95, **kw):
    return {"text_as_written": text, "name_fi": name, "name_en": None, "quantity": q, "unit": unit,
            "confidence": conf, **kw}


PAGE = {"kind": "order_list", "delivery_date": "2026-11-16", "notes": None, "unreadable": [], "dishes": [],
        "items": [item("maito laktoositon 40 l", "maito laktoositon", 40, "l"), item("voi 5 kg", "voi", 5, "kg"),
                  item("jauheliha 1? kg", "jauheliha", 12, "kg", 0.4, note="second digit unclear")]}

LLMS: dict = {}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(config, "REQUIRE_AGENT_AUTH", False)
    monkeypatch.setattr(config, "APPROVER_CODE", CODE)
    monkeypatch.setattr(config, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(config, "SANDBOX_PER_IP_HOUR", 50)
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(pid))
    monkeypatch.setattr(catalog, "search", fake_search)
    from scripts.seed import MANDATES  # the fake products count as the demo buyers' usual ones (first-time rule)
    for m in MANDATES:
        monkeypatch.setattr(m, "usual_products", m.usual_products + list(PRODUCTS))
    monkeypatch.setattr(Q, "offers", offers_at_now)
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(sandbox, "_clock", lambda: NOW.timestamp())
    monkeypatch.setattr(sandbox, "_last_sweep", 0.0)
    monkeypatch.setattr(AS, "_clock", lambda: NOW.timestamp())
    monkeypatch.setattr(notify, "_deliver", lambda *a, **k: None)
    for mod in (R, V, B):
        monkeypatch.setattr(mod, "helsinki_today", lambda: TODAY)
    monkeypatch.setattr(AS, "PER_IP_HOUR", 10)
    monkeypatch.setattr(AS, "DAILY_USD", 5.0)
    monkeypatch.setattr(AS, "make_llm", lambda provider: LLMS.get(provider))
    LLMS.clear()
    monkeypatch.setattr(aimo, "_client", httpx.Client(base_url="https://aimo.test/api",
                                                      transport=httpx.MockTransport(Valio())))
    aimo.reset()
    api._hits.clear()
    yield
    api._hits.clear()
    aimo.reset()
    signing._key.cache_clear()


@pytest.fixture
def c():
    return TestClient(AS.app)


@pytest.fixture
def sbx():
    s = sandbox.create("10.0.0.1", "https://sous.test")
    hotel = next(m for m in s["mandates"] if m["scenario"] == "hotel")
    return {"token": s["agent_token"], "mandate_id": hotel["mandate_id"], "agent_id": hotel["agent_id"],
            "headers": {"Authorization": f"Bearer {s['agent_token']}"}}


def jpeg() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (400, 300), (250, 250, 240)).save(b, "JPEG")
    return b.getvalue()


# --- menu -> Gemini -> quote -------------------------------------------------------------------------

def test_plan_menu_shows_the_model_the_chain_and_the_mandate_preview(c, sbx):
    LLMS["gemini"] = FakeLLM("gemini", "gemini-4-hydrogen", [SOUP])
    r = c.post("/plan-menu", headers=sbx["headers"], json={
        "text": "Onion soup for 40 on Monday", "mandate_id": sbx["mandate_id"], "agent_id": sbx["agent_id"],
        "delivery_date": "2026-11-16"})
    assert r.status_code == 200, r.text
    d = r.json()
    it = d["interpretation"]
    assert it["used_model"] and it["by"] == "Gemini 4 Hydrogen" and it["structured_output"]
    assert it["model"]["prompt_tokens"] == 1000 and it["model"]["cost_usd"] > 0 and "latency_ms" in it["model"]
    assert it["spec"]["portions"] == 40 and it["spec"]["dishes"][0]["name"] == "Kermainen sipulikeitto"
    assert {i["name_fi"] for i in d["ingredients"]} == {"kuohukerma", "sipuli"}
    q = d["quote"]
    assert q["id"].startswith("Q") and {l["product_id"] for l in q["lines"]} == {"CREAM", "ONION"}
    assert d["mandate_preview"]["outcome"] in ("silent", "notify") and d["summary"]
    u = c.get("/usage").json()
    [p] = u["providers"]
    assert p["provider"] == "gemini" and p["requests"] == 1 and p["model_calls"] == 1
    assert p["prompt_tokens"] == 1000 and u["today"]["spent_usd"] == pytest.approx(0.003)


def test_plan_menu_without_a_key_falls_back_to_the_table_and_costs_nothing(c, sbx):
    r = c.post("/plan-menu", headers=sbx["headers"], json={
        "text": "Lasagne for 120 on Monday, we have 10 kg flour", "mandate_id": sbx["mandate_id"],
        "agent_id": sbx["agent_id"], "delivery_date": "2026-11-16"})
    assert r.status_code == 200, r.text
    it = r.json()["interpretation"]
    assert not it["used_model"] and "No Gemini key" in it["note"] and it["by"].startswith("Curated recipe table")
    assert c.get("/usage").json()["providers"] == []


# --- guardrails ----------------------------------------------------------------------------------------

def test_only_sandbox_token_or_approver_code(c, sbx):
    body = {"text": "Lasagne for 120", "mandate_id": sbx["mandate_id"], "agent_id": sbx["agent_id"],
            "provider": "table", "delivery_date": "2026-11-16"}
    assert c.post("/plan-menu", json=body).json()["error"] == "agent_auth_required"
    assert c.post("/plan-menu", json=body, headers={"Authorization": "Bearer nope"}).json()["error"] == \
        "agent_auth_invalid"
    assert c.post("/plan-menu", json={**body, "agent_id": "someone-else"},
                  headers=sbx["headers"]).json()["error"] == "agent_mismatch"
    S.issue_mandate(S.Mandate(id="M-REAL", buyer="Real", agent_id="real-agent", issued_by="x",
                              auto_approve_limit_eur=500))
    real = {**body, "mandate_id": "M-REAL", "agent_id": "real-agent"}
    r = c.post("/plan-menu", json=real)
    assert r.status_code == 403 and r.json()["error"] == "sandbox_or_approver_required"
    assert c.post("/plan-menu", json=real, headers={"X-Approver-Code": "wrong"}).status_code == 403
    assert c.post("/plan-menu", json=real, headers={"X-Approver-Code": CODE}).status_code == 200
    r = c.post("/ask", json={"text": "100 l milk", "mandate_id": "M-REAL", "agent_id": "real-agent"},
               headers={"X-Approver-Code": CODE})
    assert r.status_code == 403 and r.json()["error"] == "sandbox_required"
    assert c.post("/plan-menu", json={**real, "mandate_id": "M-NOPE"}).status_code == 404


def test_per_ip_limit_and_daily_budget(c, sbx, monkeypatch):
    monkeypatch.setattr(AS, "PER_IP_HOUR", 2)
    body = {"text": "Onion soup for 40", "mandate_id": sbx["mandate_id"], "agent_id": sbx["agent_id"],
            "delivery_date": "2026-11-16"}
    LLMS["gemini"] = FakeLLM("gemini", "gemini-4-hydrogen", [SOUP, SOUP, SOUP])
    assert c.post("/plan-menu", headers=sbx["headers"], json=body).status_code == 200
    assert c.post("/plan-menu", headers=sbx["headers"], json=body).status_code == 200
    r = c.post("/plan-menu", headers=sbx["headers"], json=body)
    assert r.status_code == 429 and r.json()["error"] == "agent_rate_limited" and r.headers["Retry-After"]
    # the table needs no model, so it is not limited
    assert c.post("/plan-menu", headers=sbx["headers"], json={**body, "text": "Lasagne for 120",
                                                               "provider": "table"}).status_code == 200
    monkeypatch.setattr(AS, "PER_IP_HOUR", 100)
    monkeypatch.setattr(AS, "DAILY_USD", 0.005)  # two calls spent $0.006
    r = c.post("/plan-menu", headers=sbx["headers"], json=body)
    assert r.status_code == 429 and r.json()["error"] == "agent_budget_exhausted"


def test_image_checks(c, sbx, monkeypatch):
    form = {"mandate_id": sbx["mandate_id"], "agent_id": sbx["agent_id"]}
    r = c.post("/read-photo", headers=sbx["headers"], data=form, files={"image": ("a.gif", b"GIF89a....", "image/gif")})
    assert r.status_code == 415 and r.json()["error"] == "unsupported_image"
    monkeypatch.setattr(AS, "MAX_IMAGE_BYTES", 1000)
    r = c.post("/read-photo", headers=sbx["headers"], data=form, files={"image": ("a.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 413 and r.json()["error"] == "image_too_large"
    monkeypatch.setattr(AS, "MAX_IMAGE_BYTES", 4 * 1024 * 1024)
    r = c.post("/read-photo", headers=sbx["headers"], data=form, files={"image": ("a.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 503 and r.json()["error"] == "vision_unavailable"


# --- photo -> Nemotron -> quote; order --------------------------------------------------------------------

def test_read_photo_flags_doubtful_lines_and_the_order_button_is_idempotent(c, sbx):
    fix = {"lines": [{"index": 2, "quantity": 12, "unit": "kg", "confidence": 0.45}]}
    LLMS["nvidia"] = FakeLLM("nvidia", "nvidia/nemotron-3.5-super-vl-120b-a12b", [PAGE, fix], price=(None, None))
    r = c.post("/read-photo", headers=sbx["headers"], data={"mandate_id": sbx["mandate_id"],
               "agent_id": sbx["agent_id"]}, files={"image": ("list.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 200, r.text
    d = r.json()
    rd = d["reading"]
    assert rd["by"] == "NVIDIA Nemotron 3.5 Super VL" and rd["kind"] == "order_list" and rd["model"]["calls"] == 2
    assert rd["model"]["cost_estimated"] and rd["model"]["cost_usd"] > 0
    flagged = [l for l in d["lines"] if l["needs_human"]]
    assert [l["name_fi"] for l in flagged] == ["jauheliha"] and any("jauheliha" in t for t in d["to_confirm"])
    q = d["quote"]
    assert {l["product_id"] for l in q["lines"]} == {"MILK", "BUTTER"} and d["mandate_preview"]["outcome"]

    o1 = c.post("/order", headers=sbx["headers"], json={"quote_id": q["id"], "mandate_id": sbx["mandate_id"],
                                                         "agent_id": sbx["agent_id"]})
    assert o1.status_code == 200, o1.text
    o = o1.json()["order"]
    assert o["status"] in ("ordered", "pending_approval") and o["tier"] and o["findings"]
    o2 = c.post("/order", headers=sbx["headers"], json={"quote_id": q["id"], "mandate_id": sbx["mandate_id"],
                                                         "agent_id": sbx["agent_id"]}).json()["order"]
    assert o2["id"] == o["id"]
    assert c.post("/order", json={"quote_id": q["id"], "mandate_id": sbx["mandate_id"],
                                  "agent_id": sbx["agent_id"]}).status_code == 401
    u = c.get("/usage").json()["providers"]
    assert [(p["provider"], p["model_calls"], p["cost_estimated"]) for p in u] == [("nvidia", 2, True)]


def test_read_photo_prefers_nemotron_on_verda(c, sbx):
    LLMS["nvidia"] = FakeLLM("nvidia", "nvidia/nemotron-3.5-super-vl-120b-a12b", [])  # must not be called
    LLMS["verda-nemotron"] = FakeLLM("verda-nemotron", "nemotron-3.5-super-vl", [PAGE, {"lines": []}], price=(0, 0))
    r = c.post("/read-photo", headers=sbx["headers"], data={"mandate_id": sbx["mandate_id"],
               "agent_id": sbx["agent_id"]}, files={"image": ("list.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 200, r.text
    rd = r.json()["reading"]
    assert rd["by"] == "NVIDIA Nemotron 3.5 Super VL (on Verda)" and rd["model"]["provider"] == "verda-nemotron"
    assert rd["model"]["calls"] == 2 and LLMS["verda-nemotron"].answers == []


# --- a sentence -> the LLM buyer agent ------------------------------------------------------------------------

def _tool(name, args):
    def step(messages):
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{len(messages)}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
    return step


def _order_last_quote(messages):
    qid = next(json.loads(m["content"])["quote"]["id"] for m in reversed(messages)
               if m["role"] == "tool" and m["name"] == "request_quote")
    return _tool("place_order", {"quote_id": qid})(messages)


@pytest.mark.parametrize("agent", [None, "generic"])  # None: the default, the Sous agent
def test_ask_runs_the_buyer_agent_with_the_gateway_tools(c, sbx, agent):
    LLMS["gemini"] = FakeLLM("gemini", "gemini-4-hydrogen", [
        _tool("search_products", {"query": "milk lactose free"}),
        _tool("request_quote", {"delivery_date": "2026-11-16",
                                "items": [{"product_ids": ["MILK"], "amount": 100, "unit": "L"}]}),
        _order_last_quote, "Ordered 100 L of lactose-free milk for Monday."])
    body = {"text": "100 litres of lactose-free milk by Monday", "mandate_id": sbx["mandate_id"],
            "agent_id": sbx["agent_id"], **({"agent": agent} if agent else {})}
    r = c.post("/ask", headers=sbx["headers"], json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["agent"] == ("generic" if agent else "sous-agent")
    assert (d["agent_notes"] is None) == bool(agent) and d["declared_risks"] == []
    assert d["by"] == "Gemini 4 Hydrogen" and d["model"]["calls"] == 4
    assert [s["tool"] for s in d["transcript"]] == ["search_products", "request_quote", "place_order"]
    assert not any(s["is_error"] for s in d["transcript"]), d["transcript"]
    assert d["order"]["status"] == "ordered" and d["order"]["findings"][0]["rule"] and d["quote"]["lines"][0]["product_id"] == "MILK"
    assert d["answer"].startswith("Ordered") and d["stop_reason"] == "answered"


def test_ask_follow_up_answers_the_agents_question_in_place(c, sbx):
    seen = []

    def quote_after_reply(messages):
        seen.append([(m["role"], m.get("content")) for m in messages[1:]])
        return _tool("request_quote", {"delivery_date": "2026-11-16", "items": [
            {"product_ids": ["MILK"], "amount": 40, "unit": "L", "purpose": "dessert"}]})(messages)

    LLMS["gemini"] = FakeLLM("gemini", "gemini-4-hydrogen", ["Which fat level do you want for the dessert?"])
    body = {"text": "40 L lactose-free milk for a dessert", "mandate_id": sbx["mandate_id"],
            "agent_id": sbx["agent_id"], "allow_order": False, "agent": "generic"}
    d = c.post("/ask", headers=sbx["headers"], json=body).json()
    assert d["needs_reply"] is True and d["quote"] is None and d["follow_up"] == []
    LLMS["gemini"] = FakeLLM("gemini", "gemini-4-hydrogen", [quote_after_reply, "Quoted 40 L for the dessert."])
    turn = [{"question": d["answer"], "answer": "Full-fat, please"}]
    r = c.post("/ask", headers=sbx["headers"], json={**body, "follow_up": turn})
    assert r.status_code == 200, r.text
    d2 = r.json()
    assert seen[0] == [("user", body["text"]), ("assistant", d["answer"]), ("user", "Full-fat, please")]
    assert d2["needs_reply"] is False and d2["quote"]["lines"][0]["product_id"] == "MILK"
    assert d2["follow_up"] == turn and d2["request"] == body["text"]


def test_ask_sous_agent_declares_an_assumed_amount_and_the_gateway_holds_it(c, sbx):
    """'for 40 people' with no amount: the Sous agent assumes servings (PORTIONS), declares it on the quote, and
    the gateway holds the order for a human; the buyer's own words reach the gateway (buyer_request)."""
    LLMS["gemini"] = FakeLLM("gemini", "gemini-4-hydrogen", [
        _tool("request_quote", {"delivery_date": "2026-11-17",
                                "items": [{"product_ids": ["MILK"], "amount": 40, "unit": "SALES_UNIT", "note": "#1"}]}),
        _order_last_quote, "Quoted 1.2 L of milk for coffee for 40 people; waiting for your approval."])
    text = "Lactose-free milk for coffee for 40 people on Monday"
    r = c.post("/ask", headers=sbx["headers"], json={"text": text, "mandate_id": sbx["mandate_id"],
                                                      "agent_id": sbx["agent_id"]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["agent"] == "sous-agent"
    rq = d["transcript"][0]["args"]
    assert rq["delivery_date"] == "2026-11-16" and rq["items"][0]["amount"] == 1.2 and rq["items"][0]["unit"] == "L"
    assert "buyer_request" not in rq and any("40 people" in x for x in d["declared_risks"])
    assert d["agent_notes"]["parsed_need"]["headcount"] == 40
    assert d["mandate_preview"]["outcome"] == "ask"
    assert any(f["rule"] == "agent.declared_risk" for f in d["mandate_preview"]["findings"])
    assert d["order"]["status"] == "pending_approval"
    assert store.get_quote(d["quote"]["id"])["buyer_request"] == text


def test_ask_sous_agent_asks_then_quotes_with_the_follow_up(c, sbx):
    LLMS["gemini"] = FakeLLM("gemini", "gemini-4-hydrogen", [
        _tool("request_quote", {"items": [{"product_ids": ["ONION"], "amount": 40, "unit": "SALES_UNIT"}]}),
        "How many kilos of onions do you need for the 40 people?"])
    body = {"text": "Onions for 40 people on Monday", "mandate_id": sbx["mandate_id"], "agent_id": sbx["agent_id"],
            "allow_order": False}
    d = c.post("/ask", headers=sbx["headers"], json=body).json()
    assert d["transcript"][0]["result"]["error"] == "need_clarification" and d["quote"] is None
    assert d["needs_reply"] is True and d["answer"].startswith("How many")
    LLMS["gemini"] = FakeLLM("gemini", "gemini-4-hydrogen", [
        _tool("request_quote", {"items": [{"product_ids": ["ONION"], "amount": 8, "unit": "KG", "note": "#1"}]}),
        "Quoted 8 kg of onions for Monday."])
    turn = [{"question": d["answer"], "answer": "8 kg"}]
    d2 = c.post("/ask", headers=sbx["headers"], json={**body, "follow_up": turn}).json()
    assert d2["needs_reply"] is False and d2["quote"]["lines"][0]["product_id"] == "ONION"
    assert d2["follow_up"] == turn and d2["order"] is None
    assert "place_order" not in {s["tool"] for s in d2["transcript"]}
    assert store.get_quote(d2["quote"]["id"])["buyer_request"].split() == ["Onions", "for", "40", "people", "on", "Monday", "8", "kg"]


def _order_planned(messages):
    qid = next(json.loads(m["content"])["quote_id"] for m in reversed(messages)
               if m["role"] == "tool" and m["name"] == "plan_menu")
    return _tool("place_order", {"quote_id": qid})(messages)


def test_ask_sous_agent_sends_a_menu_through_the_recipe_planner(c, sbx):
    LLMS["gemini"] = FakeLLM("gemini", "gemini-4-hydrogen", [
        _tool("plan_menu", {}), SOUP, _order_planned, "Quoted cream and onions for the soup; waiting for you."])
    r = c.post("/ask", headers=sbx["headers"], json={"text": "Onion soup for 40 on Monday, order the ingredients",
                                                      "mandate_id": sbx["mandate_id"], "agent_id": sbx["agent_id"]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [s["tool"] for s in d["transcript"]] == ["request_quote", "plan_menu", "place_order"]  # planner quote first
    assert {l["product_id"] for l in d["quote"]["lines"]} == {"CREAM", "ONION"}
    assert d["declared_risks"] and d["mandate_preview"]["outcome"] == "ask"
    assert d["order"]["status"] == "pending_approval" and d["model"]["calls"] == 4


def test_ask_needs_a_model_key(c, sbx):
    r = c.post("/ask", headers=sbx["headers"], json={"text": "100 l milk", "mandate_id": sbx["mandate_id"],
                                                      "agent_id": sbx["agent_id"]})
    assert r.status_code == 503 and r.json()["error"] == "llm_unavailable"


# --- mounting ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("flag,mounted", [("1", True), ("", False)])
def test_mounted_only_with_the_flag(flag, mounted):
    code = ("from sous import api; import sys; "
            "sys.exit(0 if any(type(r).__name__ == 'Mount' and r.path == '/agent' for r in api.app.routes) else 3)")
    env = {**os.environ, "SOUS_BUYER_AGENT": flag}
    rc = subprocess.run([sys.executable, "-c", code], env=env, cwd=config.ROOT).returncode
    assert rc == (0 if mounted else 3)


def test_openapi_has_the_banner(c):
    assert AS.BANNER in c.get("/openapi.json").json()["info"]["description"]
