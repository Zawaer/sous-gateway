"""MCP server over the in-process transport: tool listing, annotations, calls, structured errors.

Offline: the catalog and state DB are replaced, no Valio API calls are made.
"""

import asyncio
import json

import pytest
from mcp import Client

from valtakirja import catalog, config, service as S, signing, store
from valtakirja.mandate import Mandate
from valtakirja.mcp_server import mcp

MILK = {
    "id": "6409100", "name": "Valio Eila 1 l laktoositon maitojuoma", "names": {"fi": "Valio Eila", "en": "", "sv": ""},
    "brand": "Valio", "vendor": "Valio", "category": "10400", "country_of_origin": "fi", "sales_unit": "ST",
    "base_unit": "ST", "units": {"ST": 1.0}, "allowed_lot_size": 1.0, "temperature": "chilled", "labels": [],
    "claims": ["FREE_FROM_LACTOSE"], "allergens": {"declared": True, "contains": ["AM"], "may_contain": [],
                                                  "free_from": []},
    "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": 1.4,
    "official_substitutes": [], "deleted": False,
}


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    seen = {}

    def search(query, **kw):
        seen.update(query=query, **kw)
        return [MILK] if "maito" in query or "milk" in query else []

    monkeypatch.setattr(catalog, "search", search)
    monkeypatch.setattr(catalog, "get", lambda pid: MILK if pid == MILK["id"] else None)
    yield seen
    signing._key.cache_clear()


def run(coro_fn):
    async def go():
        async with Client(mcp) as c:
            return await coro_fn(c)
    return asyncio.run(go())


def test_tools_and_annotations():
    tools = {t.name: t for t in run(lambda c: c.list_tools()).tools}
    assert set(tools) == {"search_products", "get_product", "request_quote", "suggest_substitutes",
                          "place_order", "get_order", "get_mandate", "get_suggestions",
                          "create_standing_order", "renew_standing_order", "get_standing_order"}
    for name in ("search_products", "get_product", "get_order", "get_mandate", "suggest_substitutes",
                 "get_suggestions"):
        assert tools[name].annotations.read_only_hint is True, name
    assert tools["request_quote"].annotations.read_only_hint is False
    assert tools["request_quote"].annotations.destructive_hint is False
    assert tools["place_order"].annotations.destructive_hint is True
    assert tools["place_order"].annotations.read_only_hint is False
    assert tools["search_products"].annotations.open_world_hint is False
    assert tools["request_quote"].annotations.open_world_hint is True


def test_search_and_get_product(_offline):
    async def calls(c):
        s = await c.call_tool("search_products", {"query": "maito", "claims": ["FREE_FROM_LACTOSE"],
                                                  "avoid_allergens": ["AN"], "agent_id": "test-agent"})
        p = await c.call_tool("get_product", {"product_id": MILK["id"]})
        return s, p

    s, p = run(calls)
    assert not s.is_error
    body = json.loads(s.content[0].text)
    assert [x["id"] for x in body["products"]] == [MILK["id"]]
    assert "price" not in body["products"][0]
    assert _offline["claims"] == ["FREE_FROM_LACTOSE"] and _offline["avoid_allergens"] == ["AN"]
    assert not p.is_error and json.loads(p.content[0].text)["claims"] == ["FREE_FROM_LACTOSE"]


def test_errors_are_structured():
    async def calls(c):
        return (await c.call_tool("get_product", {"product_id": "nope"}),
                await c.call_tool("get_order", {"order_id": "PO-NOPE"}),
                await c.call_tool("request_quote", {"agent_id": "a", "delivery_date": "tomorrow",
                                                    "items": [{"product_ids": [MILK["id"]], "amount": 1}]}))

    for r, code in zip(run(calls), ("product_not_found", "order_not_found", "invalid_argument")):
        assert r.is_error
        assert r.structured_content["error"] == code
        assert r.structured_content["message"] and r.structured_content["hint"]
        assert json.loads(r.content[0].text) == r.structured_content


def test_place_order_unknown_quote_hint():
    S.issue_mandate(Mandate(id="M-T", buyer="Test", agent_id="a", issued_by="human"))
    r = run(lambda c: c.call_tool("place_order", {"agent_id": "a", "mandate_id": "M-T", "quote_id": "Q-NOPE",
                                                  "idempotency_key": "k1"}))
    assert r.is_error and r.structured_content["error"] == "quote_not_found"
    assert "request_quote" in r.structured_content["hint"]


def test_resources_and_prompt():
    S.issue_mandate(Mandate(id="M-T", buyer="Test", agent_id="a", issued_by="human", avoid_allergens=["AN"]))

    async def calls(c):
        a = await c.read_resource("valtakirja://allergens")
        m = await c.read_resource("valtakirja://mandates/M-T")
        p = await c.get_prompt("purchase", {"request": "1000 L lactose-free milk", "mandate_id": "M-T",
                                            "agent_id": "a"})
        return a, m, p

    a, m, p = run(calls)
    allergens = json.loads(a.contents[0].text)
    assert allergens["codes"]["AM"] == "milk" and "SA" in allergens["groups"]["AN"]
    assert json.loads(m.contents[0].text)["mandate"]["avoid_allergens"] == ["AN"]
    assert "place_order" in p.messages[0].content.text
