"""MCP interface: the same gateway operations as the REST API, as tools any agent can call.

Served over Streamable HTTP at /mcp by the main app (see api.py), or over stdio:
    uv run python -m valtakirja.mcp_server
"""

from __future__ import annotations

from datetime import date

from mcp.server.mcpserver import MCPServer

from . import catalog, service as S

INSTRUCTIONS = """\
You are buying from Valio Aimo, a Finnish food-service wholesaler, on behalf of a buyer
whose mandate (valtakirja) you hold. Flow:
1. search_products: find candidates in the static catalog (fi/sv/en names; filter by claims
   like FREE_FROM_LACTOSE and allergens to avoid). This costs no live API calls.
2. request_quote: pass candidate product ids with the amount needed (L, KG or packs) and the
   delivery date. The gateway prices them live, picks pack sizes, checks cut-offs and stock,
   splits across products if needed, and previews the result against your mandate.
   If a product can't be ordered (cut-off passed, out of stock...), call suggest_substitutes.
3. place_order with the quote id. The mandate decides: silent/notify = ordered,
   ask = waits for a human, block = refused. You can't override it; explain the reasons to
   the buyer. Check later with get_order.
Never invent prices, stock or allergen facts: only report what the tools return."""

mcp = MCPServer(name="valtakirja", title="Valtakirja: Valio Aimo agent gateway", instructions=INSTRUCTIONS,
                version="0.1.0")


@mcp.tool()
def search_products(
    query: str,
    claims: list[str] | None = None,
    avoid_allergens: list[str] | None = None,
    labels: list[str] | None = None,
    temperature: str | None = None,
    limit: int = 15,
    agent_id: str = "anonymous-agent",
) -> dict:
    """Search Valio Aimo's catalog (17.5k products) by name, brand or ingredient in Finnish, Swedish or English.

    claims: required nutritional claims, e.g. FREE_FROM_LACTOSE, FREE_FROM_GLUTEN, VEGAN.
    avoid_allergens: GS1 codes to exclude, e.g. AM (milk), AN (tree nuts), AP (peanuts), AW (gluten), AE (egg).
      Products that contain them are dropped; products with no allergen data are kept but marked 'unknown'.
    labels: e.g. GOODS_FROM_FINLAND_BLUE_SWAN, EU_ORGANIC_FARMING, MARINE_STEWARDSHIP_COUNCIL_LABEL.
    temperature: ambient, chilled or frozen.
    Returns product cards without price or stock; ask for a quote next.
    """
    return S.search(agent_id, query, claims=claims, avoid_allergens=avoid_allergens, labels=labels,
                    temperature=temperature, limit=limit)


@mcp.tool()
def get_product(product_id: str) -> dict:
    """Full static details of one product: names, pack sizes, allergens, claims, labels, reliability, CO2 estimate."""
    p = catalog.get(product_id)
    if not p:
        return {"error": f"No product {product_id}"}
    return p


@mcp.tool()
def request_quote(
    agent_id: str,
    delivery_date: str,
    items: list[S.QuoteItem],
    mandate_id: str | None = None,
) -> dict:
    """Get a signed, expiring quote with live prices and availability from Valio Aimo.

    delivery_date: YYYY-MM-DD.
    items: each {product_ids: [...], amount, unit: 'L' | 'KG' | 'SALES_UNIT', substitute_for?}. Give several
      candidate ids for one need and the gateway picks the cheapest orderable one per L/KG, splitting across
      products when stock is short. Mark substitutes with substitute_for (the product they replace).
    With mandate_id, the quote includes mandate_preview: what will happen if you order it, and why.
    """
    return S.request_quote(S.QuoteRequest(agent_id=agent_id, mandate_id=mandate_id,
                                          delivery_date=date.fromisoformat(delivery_date), items=items))


@mcp.tool()
def suggest_substitutes(
    agent_id: str,
    product_id: str,
    delivery_date: str,
    amount: float,
    unit: str = "SALES_UNIT",
    mandate_id: str | None = None,
    avoid_allergens: list[str] | None = None,
    limit: int = 3,
) -> dict:
    """Good / better / best substitutes for a product that can't be ordered (or for any product).

    Same category, measure and temperature class; keeps the original's dietary claims and the mandate's
    allergen rules (never relaxed). Each option lists its compromises in plain language and has a live offer.
    Then request a quote for the chosen one with substitute_for=<original id>.
    """
    try:
        return S.suggest_substitutes(agent_id, product_id, date.fromisoformat(delivery_date), amount, unit,
                                     mandate_id, avoid_allergens, limit)
    except S.GatewayError as e:
        return {"error": e.code, "message": e.message}


@mcp.tool()
def place_order(agent_id: str, mandate_id: str, quote_id: str, idempotency_key: str | None = None) -> dict:
    """Order a quote under a mandate. Returns status ordered, pending_approval or refused, with the rules that fired.

    Use an idempotency_key so a retry never orders twice.
    """
    try:
        return S.place_order(agent_id, mandate_id, quote_id, idempotency_key)
    except S.GatewayError as e:
        return {"error": e.code, "message": e.message}


@mcp.tool()
def get_order(order_id: str) -> dict:
    """Current status of an order, including a human's approval or rejection."""
    try:
        return S.get_order(order_id)
    except S.GatewayError as e:
        return {"error": e.code, "message": e.message}


@mcp.tool()
def get_mandate(mandate_id: str) -> dict:
    """The mandate you act under (limits, allergen rules, what needs a human), with its signature."""
    try:
        return S.get_mandate(mandate_id)
    except S.GatewayError as e:
        return {"error": e.code, "message": e.message}


if __name__ == "__main__":
    mcp.run()
