"""MCP structured tool output (spec 2025-06-18): every tool declares an outputSchema, returns structuredContent
that validates against it, and keeps the same JSON as a text block for older clients.

Offline, with test_sandbox.py's fixtures: one catalog product, Valio Aimo mocked, no data/ needed. The in-process
client (mcp.Client) itself re-validates structuredContent against the listed outputSchema, so a mismatch fails here.
"""

import asyncio
import json

from jsonschema.validators import validator_for
from mcp import Client

from test_sandbox import env, seed_curated  # noqa: F401  (pytest fixture)

from sous import mcp_server, standards
from sous.mcp_server import mcp

DAY = "2026-11-16"
AGENT, MANDATE = "aurora-purchasing-agent", "M-HOTEL-AURORA"


def run(coro_fn):
    async def go():
        async with Client(mcp) as c:
            return await coro_fn(c)
    return asyncio.run(go())


def tools():
    return {t.name: t for t in run(lambda c: c.list_tools()).tools}


def check(result, schema):
    """Not an error, structuredContent valid against the schema, and the text block is the same JSON."""
    assert not result.is_error, result.content[0].text
    data = result.structured_content
    validator_for(schema)(schema).validate(data)
    assert json.loads(result.content[0].text) == data
    return data


def test_every_tool_declares_an_object_output_schema_and_all_hints():
    for name, t in tools().items():
        s = t.output_schema
        assert s and s["type"] == "object" and s["required"], name
        validator_for(s).check_schema(s)
        assert t.description and len(t.description) > 80, name
        a = t.annotations
        assert None not in (a.read_only_hint, a.destructive_hint, a.idempotent_hint, a.open_world_hint), name
    t = tools()
    assert t["place_order"].annotations.read_only_hint is False
    assert t["place_order"].annotations.idempotent_hint is True
    assert t["place_order"].annotations.destructive_hint is True
    assert {"products", "note"} <= set(t["search_products"].output_schema["required"])
    assert set(t["request_quote"].output_schema["required"]) == {"quote", "jws"}
    assert {"mandate", "jws"} <= set(t["get_mandate"].output_schema["required"])


def test_schemas_reuse_the_published_json_schemas():
    """One source of truth: what /schemas/{quote,order,mandate}.json require, the tool schemas require too."""
    t = tools()
    defs = t["request_quote"].output_schema["$defs"]
    quote = standards.json_schema("quote", "http://x")
    assert set(quote["required"]) <= set(defs["QuoteBodyOut"]["required"])
    assert set(quote["properties"]) <= set(defs["QuoteBodyOut"]["properties"])
    order = standards.json_schema("order", "http://x")
    for name in ("place_order", "get_order"):
        s = t[name].output_schema
        assert set(order["required"]) == set(s["required"]) and set(order["properties"]) <= set(s["properties"])
    mandate = standards.json_schema("mandate", "http://x")
    assert set(mandate["properties"]) == set(t["get_mandate"].output_schema["$defs"]["Mandate"]["properties"])


def test_search_quote_order_and_reads_return_valid_structured_content():
    seed_curated()
    schemas = {n: t.output_schema for n, t in tools().items()}

    async def calls(c):
        s = await c.call_tool("search_products", {"query": "oat", "agent_id": AGENT})
        m = await c.call_tool("get_mandate", {"mandate_id": MANDATE})
        q = await c.call_tool("request_quote", {
            "agent_id": AGENT, "mandate_id": MANDATE, "delivery_date": DAY, "rationale": "breakfast oat drink",
            "items": [{"product_ids": ["OAT"], "amount": 10, "unit": "L"}]})
        qid = q.structured_content["quote"]["id"]
        args = {"agent_id": AGENT, "mandate_id": MANDATE, "quote_id": qid, "idempotency_key": "k-structured-1"}
        o1 = await c.call_tool("place_order", args)
        o2 = await c.call_tool("place_order", args)  # retry with the same key: same order, no second purchase
        g = await c.call_tool("get_order", {"order_id": o1.structured_content["id"]})
        return s, m, q, o1, o2, g

    s, m, q, o1, o2, g = run(calls)
    assert [p["id"] for p in check(s, schemas["search_products"])["products"]] == ["OAT"]
    assert check(m, schemas["get_mandate"])["mandate"]["id"] == MANDATE
    quote = check(q, schemas["request_quote"])["quote"]
    assert quote["mandate_preview"]["outcome"] in ("silent", "notify", "ask", "block")
    order = check(o1, schemas["place_order"])
    assert order["status"] in ("ordered", "pending_approval", "refused") and order["receipt_jws"]
    assert check(o2, schemas["place_order"])["id"] == order["id"]
    got = check(g, schemas["get_order"])
    assert got["id"] == order["id"] and got["agent_claim"]["verified"] is False


def test_errors_stay_unstructured_by_schema_but_carry_the_error_object():
    r = run(lambda c: c.call_tool("get_order", {"order_id": "PO-NOPE"}))
    assert r.is_error and r.structured_content["error"] == "order_not_found"
    assert json.loads(r.content[0].text) == r.structured_content


def test_python_callers_still_get_plain_dicts():
    """tests and scripts call the tool functions directly: success is the dict, not a CallToolResult."""
    seed_curated()
    assert mcp_server.get_mandate(MANDATE)["mandate"]["id"] == MANDATE
    assert mcp_server.get_order("PO-NOPE").is_error
