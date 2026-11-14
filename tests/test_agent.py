"""LLM buyer agent (agent/), offline: a scripted fake LLM drives the real in-process MCP server.

No model API keys, no Valio calls, no data files: catalog, prices and availability are stubbed.
"""

import json
from datetime import date, datetime

import httpx
import pytest

from agent import buyer as B
from agent.llm import ChatClient, ChatResponse, LLMConfig, LLMError, config_from_env
from sous import aimo, catalog, config, service as S, signing, store
from sous.mandate import Mandate
from sous.mcp_server import mcp

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)


def product(pid, name, cp, contains=("AM",), claims=("FREE_FROM_LACTOSE",)):
    return {
        "id": pid, "name": name, "names": {"fi": name, "en": name, "sv": ""}, "brand": "Valio", "vendor": "",
        "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
        "allowed_lot_size": 1.0, "temperature": "chilled", "labels": [], "claims": list(claims),
        "allergens": {"declared": True, "contains": list(contains), "may_contain": [], "free_from": []},
        "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": 1.4,
        "official_substitutes": [], "deleted": False, "_cp": cp,
    }


PRODUCTS = {p["id"]: p for p in [product("464201", "Valio lactose-free semi-skimmed milk 1 l", 1.20),
                                  product("464202", "Arla lactose-free milk 1 l", 1.30)]}


def fake_prices(ids, requested):
    return {i: {"catalogPrice": {"value": PRODUCTS[i]["_cp"], "valueWithTax": round(PRODUCTS[i]["_cp"] * 1.135, 2),
                                 "validTo": "2026-12-31T00:00:00+0200"},
                "comparisonPrice": {"value": PRODUCTS[i]["_cp"]}, "comparisonUnit": "L",
                "tax": {"taxRate": 13.5}, "error": False} for i in ids}


def fake_availability(ids, requested):
    return {i: {"logisticModel": "Stock", "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": False,
                "scheduleLines": [{"salesBlock": False, "availableAmount": 100000, "availableDate": "2026-11-16",
                                   "salesUnit": "ST"}]} for i in ids}


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(pid))
    monkeypatch.setattr(catalog, "search", lambda q, **kw: list(PRODUCTS.values()) if "milk" in q else [])
    monkeypatch.setattr(aimo, "prices", fake_prices)
    monkeypatch.setattr(aimo, "availability", fake_availability)
    monkeypatch.setattr(S, "_now", lambda: NOW)
    S.issue_mandate(Mandate(id="M-HOTEL", buyer="Hotel", agent_id="hotel-agent", issued_by="chef",
                            auto_approve_limit_eur=500, hard_limit_eur=5000, required_claims=["FREE_FROM_LACTOSE"]))
    S.issue_mandate(Mandate(id="M-SCHOOL", buyer="School", agent_id="school-agent", issued_by="cook",
                            auto_approve_limit_eur=100, hard_limit_eur=200, avoid_allergens=["AM"]))
    yield
    signing._key.cache_clear()


@pytest.fixture(scope="module")
def gw():
    with B.MCPGateway(mcp) as g:
        yield g


class ScriptedLLM:
    """Plays a list of turns. A turn is a final text (str), or a list of (tool, args), or a callable
    taking the tool results so far (list of dicts) and returning either."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.cfg = LLMConfig(provider="fake", base_url="", model="scripted")
        self.seen_tools = None
        self.messages = []

    def chat(self, messages, tools=None):
        self.seen_tools = tools
        self.messages = messages
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        turn = self.turns.pop(0) if self.turns else "done"
        if callable(turn):
            turn = turn(results)
        if isinstance(turn, str):
            return ChatResponse({"role": "assistant", "content": turn}, "stop",
                                {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}, 5, 0)
        calls = [{"id": f"c{len(messages)}_{i}", "type": "function",
                  "function": {"name": n, "arguments": a if isinstance(a, str) else json.dumps(a)}}
                 for i, (n, a) in enumerate(turn)]
        return ChatResponse({"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls",
                            {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}, 5, 0)


def cfg(mandate="M-HOTEL", agent="hotel-agent", **kw):
    return B.BuyerConfig(mandate_id=mandate, agent_id=agent, today=date(2026, 11, 13), **kw)


def happy_turns(quote_args=None):
    return [
        [("search_products", {"query": "lactose free milk", "claims": ["FREE_FROM_LACTOSE"]})],
        lambda res: [("request_quote", quote_args or {
            "delivery_date": "2026-11-16",
            "items": [{"product_ids": [p["id"] for p in res[-1]["products"]], "amount": 200, "unit": "L"}]})],
        lambda res: [("place_order", {"quote_id": res[-1]["quote"]["id"]})],
        "Ordered 200 L of lactose-free milk for Monday 16.11.",
    ]


def test_tool_schemas_hide_runtime_bound_arguments(gw):
    tools = {t["function"]["name"]: t["function"] for t in B.openai_tools(gw.list_tools())}
    assert set(tools) == set(B.BUYER_TOOLS)
    for f in tools.values():
        props = f["parameters"]["properties"]
        assert not set(props) & {"agent_id", "mandate_id", "idempotency_key", "agent_token", "rationale"}, f["name"]
        assert "$ref" not in json.dumps(f) and "anyOf" not in json.dumps(f)
    item = tools["request_quote"]["parameters"]["properties"]["items"]["items"]
    assert item["properties"]["unit"]["enum"] == ["L", "KG", "SALES_UNIT", "ST"]
    assert tools["get_mandate"]["parameters"]["properties"] == {}
    assert "Place an order" in tools["place_order"]["description"] or "commits" in tools["place_order"]["description"]


def test_happy_path_search_quote_order(gw):
    llm = ScriptedLLM(happy_turns())
    run = B.BuyerAgent(llm, gw, cfg()).run("200 litres of lactose-free milk for Monday")
    s = run.summary()
    assert run.stop_reason == "answered" and s["explanation"].startswith("Ordered")
    assert s["order"]["status"] == "ordered" and s["order"]["tier"] == "silent"
    assert s["order"]["lines"][0]["product_id"] == "464201"  # cheapest per L
    assert s["tool_calls"] == 3 and s["llm_calls"] == 4 and s["tokens"]["prompt"] == 400
    # the system prompt carries the mandate, the agent, today's date and the default delivery day
    system = llm.messages[0]["content"]
    assert "M-HOTEL" in system and "hotel-agent" in system and "2026-11-13" in system
    assert "Monday 2026-11-16" in system
    assert [t["tool"] for t in run.transcript()] == ["search_products", "request_quote", "place_order"]


def test_runtime_binds_identity_the_model_cannot_switch_mandate(gw):
    """The model passes another mandate and agent: ignored, the runtime's identity is used."""
    turns = happy_turns()
    turns[2] = lambda res: [("place_order", {"quote_id": res[-1]["quote"]["id"], "mandate_id": "M-SCHOOL",
                                             "agent_id": "school-agent", "idempotency_key": "x"})]
    run = B.BuyerAgent(ScriptedLLM(turns), gw, cfg()).run("milk")
    o = run.orders[0]
    assert o["mandate_id"] == "M-HOTEL" and o["agent_id"] == "hotel-agent"
    assert o["idempotency_key"] == f"hotel-agent:{o['quote_id']}"


def test_repeated_place_order_never_orders_twice(gw):
    turns = happy_turns()
    turns.insert(3, lambda res: [("place_order", {"quote_id": res[-1]["quote_id"]})])
    run = B.BuyerAgent(ScriptedLLM(turns), gw, cfg()).run("milk")
    placed = run.results("place_order")
    assert len(placed) == 2 and placed[0][1]["id"] == placed[1][1]["id"]
    assert len(run.orders) == 1


def test_gateway_decides_block_is_recorded_not_overridden(gw):
    """School mandate avoids milk: the agent orders as asked and the gateway refuses with the rule."""
    run = B.BuyerAgent(ScriptedLLM(happy_turns()), gw, cfg("M-SCHOOL", "school-agent")).run("200 L milk")
    s = run.summary()
    assert s["order"]["status"] == "refused" and s["order"]["tier"] == "block"
    assert "allergen.contains" in s["order"]["rules"]


def test_bad_arguments_and_unknown_tools_go_back_to_the_model(gw):
    llm = ScriptedLLM([[("search_products", "{not json")], [("delete_everything", {})],
                       [("get_order", {"order_id": "PO-NOPE"})], "Nothing ordered."])
    run = B.BuyerAgent(llm, gw, cfg()).run("milk")
    errs = [s.result["error"] for s in run.steps]
    assert errs == ["invalid_argument", "unknown_tool", "order_not_found"]
    assert run.tool_errors == 3 and run.stop_reason == "answered" and run.orders == []


def test_tool_call_cap(gw):
    llm = ScriptedLLM([[("search_products", {"query": "milk"})]] * 30)
    run = B.BuyerAgent(llm, gw, cfg(max_tool_calls=4)).run("milk")
    assert run.tool_calls == 4 and run.stop_reason == "tool_call_cap"
    assert run.llm_calls <= 7


def test_waits_out_the_valio_call_budget():
    class Gw:
        n = 0

        def list_tools(self):
            return [type("T", (), {"name": "get_order", "description": "Order status", "title": "",
                                   "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}}})]

        def call(self, name, args):
            self.n += 1
            if self.n == 1:
                return True, {"error": "valio_unavailable", "message": "Valio API call budget used up for this minute"}
            return False, {"id": args["order_id"], "status": "ordered"}

    slept = []
    run = B.BuyerAgent(ScriptedLLM([[("get_order", {"order_id": "PO-1"})], "ok"]), Gw(), cfg(),
                       sleep=slept.append).run("status?")
    assert slept == [15.0] and run.waited_s == 15.0 and not run.steps[0].is_error


# --- the chat client ---------------------------------------------------------------------------

def test_presets_and_key_hygiene(monkeypatch):
    env = {"GEMINI_API_KEY": "sk-secret-123"}
    c = config_from_env("gemini", env=env)
    assert c.base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert c.model == "gemini-4-hydrogen" and c.api_key == "sk-secret-123" and c.cost_usd(1_000_000, 0) == 1.5
    assert "sk-secret" not in repr(c)
    v = config_from_env("verda", env={"VERDA_INFERENCE_API_KEY": "k"})
    assert v.base_url == "https://inference.verda.com/v1" and v.reasoning_effort == "low"
    assert v.cost_usd(1, 1) is None  # Verda's prices are only in its console
    loc = config_from_env("local", env={"LLM_BASE_URL": "http://gpu:8000/v1/", "LLM_MODEL": "qwen"})
    assert loc.base_url == "http://gpu:8000/v1" and loc.api_key == ""
    with pytest.raises(ValueError):
        config_from_env("local", env={})


def test_chat_retries_429_then_succeeds_and_never_leaks_the_key():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        body = json.loads(req.content)
        assert body["model"] == "m" and body["tools"][0]["function"]["name"] == "t"
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"message": "slow down"}})
        if len(calls) == 2:
            return httpx.Response(503, json={"error": {"message": "busy"}})
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function", "function": {
                "name": "t", "arguments": "{}"}, "extra_content": {"google": {"thought_signature": "sig"}}}]}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}})

    slept = []
    cfg_ = LLMConfig(provider="x", base_url="http://llm/v1", model="m", api_key="sk-secret-123")
    c = ChatClient(cfg_, http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=slept.append)
    r = c.chat([{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "t", "parameters": {}}}])
    assert len(calls) == 3 and slept[0] == 2.0 and r.retries == 2
    assert calls[0].headers["authorization"] == "Bearer sk-secret-123"
    assert r.tool_calls[0]["extra_content"]["google"]["thought_signature"] == "sig"  # echoed back verbatim
    assert r.usage == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}

    bad = ChatClient(cfg_, http=httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(401, json={"error": {"message": "bad key"}}))), sleep=slept.append)
    with pytest.raises(LLMError) as e:
        bad.chat([{"role": "user", "content": "hi"}])
    assert "401" in str(e.value) and "sk-secret" not in str(e.value)

    # a 200 with an empty body (seen from Verda) is retried, then a clean LLMError, never a JSONDecodeError
    empty = ChatClient(cfg_, http=httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=b""))), sleep=slept.append)
    with pytest.raises(LLMError) as e:
        empty.chat([{"role": "user", "content": "hi"}])
    assert "non-JSON" in str(e.value)


def test_min_interval_paces_requests():
    """The nvidia preset spaces requests out (free tier ~40/min); the wait is measured from the last start."""
    assert config_from_env("nvidia", env={"NVIDIA_API_KEY": "k"}).min_interval_s == 1.6
    assert config_from_env("nvidia", env={"NVIDIA_API_KEY": "k", "LLM_MIN_INTERVAL_S": "0"}).min_interval_s == 0
    assert config_from_env("gemini", env={"GEMINI_API_KEY": "k"}).min_interval_s == 0
    now, slept = [100.0], []

    def sleep(s):
        slept.append(s)
        now[0] += s
    ok = httpx.MockTransport(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]}))
    c = ChatClient(LLMConfig(provider="nvidia", base_url="http://llm/v1", model="m", min_interval_s=1.6),
                   http=httpx.Client(transport=ok), sleep=sleep, clock=lambda: now[0])
    c.chat([{"role": "user", "content": "a"}])
    now[0] += 0.5
    c.chat([{"role": "user", "content": "b"}])
    now[0] += 5
    c.chat([{"role": "user", "content": "c"}])
    assert slept == [pytest.approx(1.1)]


# --- the LLM eval's harness logic --------------------------------------------------------------

def test_llm_eval_applicability_and_need_checks():
    from scripts import llm_eval as E

    assert E.applicability({"id": "H15", "flow": ["order", "order_again"]})
    assert E.applicability({"id": "X1", "flow": ["order", "requote_order"]})
    assert E.applicability({"id": "H05", "flow": ["order", "reject"]}) is None
    assert E.applicability({"id": "X2", "scripted_only": "fixed candidates"}) == "fixed candidates"
    import yaml
    cases = {c["id"]: c for c in yaml.safe_load((E.ROOT / "evals" / "cases.yaml").read_text())["cases"]}
    assert E.applicability(cases["H20"]) and E.applicability(cases["H15"]) and E.applicability(cases["H01"]) is None
    case = {"id": "H08", "request": "500 litres of the milk we always buy (464119).", "amount": 500, "unit": "L",
            "expect": {}}
    good = {"_quote": {"requested_delivery_date": "2026-11-16"}, "orders": [], "stop_reason": "answered",
            "_quote_args": {"items": [{"product_ids": ["464119"], "amount": 500, "unit": "L"}]}}
    assert all(ok for _, ok, _ in E.llm_checks(case, good, {"delivery_date": "2026-11-16"}))
    wrong = {**good, "_quote": {"requested_delivery_date": "2026-11-17"},
             "_quote_args": {"items": [{"product_ids": ["464201"], "amount": 500, "unit": "SALES_UNIT"}]}}
    failed = {n.split(" ")[0] for n, ok, _ in E.llm_checks(case, wrong, {"delivery_date": "2026-11-16"}) if not ok}
    assert failed == {"asked", "delivery", "quoted"}


def test_runtime_records_the_buyers_words_as_rationale(gw):
    """The model can't write the rationale: the runtime sends the buyer's request, signed into the quote."""
    turns = happy_turns()
    turns[2] = lambda res: [("place_order", {"quote_id": res[-1]["quote"]["id"], "rationale": "model made this up"})]
    run = B.BuyerAgent(ScriptedLLM(turns), gw, cfg()).run("200 litres of  lactose-free milk\nfor Monday")
    o = run.orders[0]
    assert o["agent_rationale"] == "200 litres of lactose-free milk for Monday"
    assert S.get_order(o["id"])["agent_rationale"] == o["agent_rationale"]
    assert all("rationale" not in t["args"] for t in run.transcript())


def test_mcp_gateway_adds_the_agent_token_below_the_model(monkeypatch):
    """With an agent token, MCPGateway sends it on the money-spending tools; the transcript never shows it."""
    from sous import config
    monkeypatch.setattr(config, "REQUIRE_AGENT_AUTH", True)
    token = S.agent_credential("M-HOTEL", "test")["agent_credential"]["token"]
    with B.MCPGateway(mcp) as bare:
        run = B.BuyerAgent(ScriptedLLM(happy_turns()[:2] + ["no token"]), bare, cfg()).run("milk")
        assert run.results("request_quote", ok_only=False)[0][1]["error"] == "agent_auth_required"
    with B.MCPGateway(mcp, agent_token=token) as authed:
        run = B.BuyerAgent(ScriptedLLM(happy_turns()), authed, cfg()).run("milk")
        assert run.orders[0]["status"] == "ordered"
        assert token not in json.dumps(run.transcript()) and token not in json.dumps(run.summary())


def test_eval_cases_do_not_share_stock_reservations(monkeypatch):
    """Each eval case gets its own reservation key space: shared Valkey hashes outlast a case (TTL)."""
    from scripts.llm_eval import isolate_reservations
    from sous import reservations

    monkeypatch.setattr(reservations, "_key", reservations._key)  # restored after the test
    isolate_reservations("H04")
    a = reservations._key("464119", "2026-11-16")
    isolate_reservations("H04")
    b = reservations._key("464119", "2026-11-16")
    assert a != b and ":H04:" in a and a.endswith("resv:464119:2026-11-16")
