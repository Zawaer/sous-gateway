"""MCP interface: the same gateway operations as the REST API, as tools any agent can call.

Served over Streamable HTTP at /mcp by the main app (see api.py), or over stdio:
    uv run python -m valtakirja.mcp_server

Every tool returns a JSON object. On failure the result has isError=true and the object is
{"error": <code>, "message": <what went wrong>, "hint": <what to do next>, "retryable": <bool>}.
"""

from __future__ import annotations

import functools
import json
import logging
from datetime import date

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, TextContent, ToolAnnotations

from . import catalog, service as S, standing

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
You buy from Valio Aimo (Finnish food-service wholesaler) for a buyer whose mandate (valtakirja) you hold.
You need the mandate_id and your agent_id; the mandate is issued to exactly that agent_id.

Typical flow, 3-4 calls:
1. search_products: put dietary needs in filters, not just the query text: claims=["FREE_FROM_LACTOSE"],
   avoid_allergens=["AM"] (GS1 codes; resource valtakirja://allergens). No price or stock here, and it is free.
2. request_quote with mandate_id: one item per need, with SEVERAL candidate product_ids and the amount in
   unit "L" or "KG" (e.g. 1000 L of milk), not pack counts. The gateway prices live, picks pack sizes,
   checks cut-off and stock, splits across products, and returns quote.mandate_preview.
   Read mandate_preview (outcome silent | notify | ask | block, with findings) and quote.uncovered BEFORE ordering.
   If a line is not orderable or the preview blocks it, call suggest_substitutes and re-quote with
   substitute_for=<original id>.
3. place_order(quote_id, mandate_id, idempotency_key=<unique string you reuse on retry>).
   Result status: ordered | pending_approval (a human decides; give them approval_url, check get_order later)
   | refused (tell the buyer which rule fired; you cannot override it).

On an error, follow its "hint" field. Never state a price, stock level, delivery date or allergen fact
that a tool did not return; say "unknown" instead. Allergen data marked 'unknown' is not safe."""

mcp = MCPServer(name="valtakirja", title="Valtakirja: Valio Aimo agent gateway", instructions=INSTRUCTIONS,
                version="0.2.0")


# --- errors -----------------------------------------------------------------

HINTS: dict[str, tuple[str, bool]] = {
    # code: (what the agent should do next, retryable as-is)
    "mandate_not_found": ("Check the mandate_id with the buyer (ids look like M-HOTEL-AURORA); don't guess.", False),
    "product_not_found": ("Use a product id returned by search_products.", False),
    "quote_not_found": ("Use the quote.id returned by request_quote, or request a new quote.", False),
    "quote_expired": ("Prices and cut-offs may have changed: call request_quote again, show the buyer the new "
                      "total, then place_order with the new quote id.", False),
    "quote_used": ("This quote already has an order: call get_order with that order, or request a new quote "
                   "for a new order.", False),
    "quote_signature": ("The stored quote was tampered with or corrupted. Request a new quote.", False),
    "order_not_found": ("Use the order id (PO-...) returned by place_order.", False),
    "not_pending": ("The order is no longer waiting for approval; call get_order for its current status.", False),
    "too_many_candidates": ("Pass fewer candidate product_ids (the best 3-6 per need from search_products).", False),
    "valio_unavailable": ("Valio's live price/stock API failed. Wait a few seconds and retry once; if it fails "
                          "again, tell the buyer and don't order.", True),
    "standing_not_found": ("Use a standing order id (SO-...) from create_standing_order.", False),
    "standing_paused": ("The buyer paused this standing order; ask them before ordering it another way.", False),
    "already_renewed": ("This week is already renewed: call get_order with the order id in the message.", False),
    "invalid_argument": ("Fix the argument named in the message and call the tool again.", False),
    "internal_error": ("Unexpected gateway error. Retry once; if it persists, tell the buyer and stop.", True),
}


def _error(code: str, message: str) -> CallToolResult:
    hint, retryable = HINTS.get(code, ("Read the message and adjust the request.", False))
    body = {"error": code, "message": message, "hint": hint, "retryable": retryable}
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(body, ensure_ascii=False))],
                          structured_content=body, is_error=True)


def _tool(fn):
    """Turn gateway errors (and anything unexpected) into one structured error result."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except S.GatewayError as e:
            return _error(e.code, e.message)
        except ValueError as e:
            return _error("invalid_argument", str(e))
        except Exception as e:  # noqa: BLE001 - the agent gets a clean error, the log gets the trace
            log.exception("MCP tool %s failed", fn.__name__)
            return _error("internal_error", f"{type(e).__name__} in {fn.__name__}.")

    return wrapper


def _date(value: str, field: str = "delivery_date") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be YYYY-MM-DD, got {value!r}.") from None


READ_LOCAL = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
READ_LIVE = ToolAnnotations(read_only_hint=True, idempotent_hint=False, open_world_hint=True)


# --- tools ------------------------------------------------------------------

@mcp.tool(title="Search the catalog", annotations=READ_LOCAL)
@_tool
def search_products(
    query: str,
    claims: list[str] | None = None,
    avoid_allergens: list[str] | None = None,
    labels: list[str] | None = None,
    temperature: str | None = None,
    limit: int = 15,
    agent_id: str = "anonymous-agent",
    food_only: bool | None = None,
) -> dict:
    """Search Valio Aimo's catalog (17.5k products) by name, brand or ingredient in Finnish, Swedish or English.

    claims: required nutritional claims, e.g. FREE_FROM_LACTOSE, FREE_FROM_GLUTEN, VEGAN.
    avoid_allergens: GS1 codes to exclude, e.g. AM (milk), AN (tree nuts), AP (peanuts), AW (gluten), AE (egg).
      Group codes cover the family (AN = all tree nuts). Products that contain them are dropped; products with
      no allergen data are kept but marked 'unknown'. Full list: resource valtakirja://allergens.
    labels: e.g. GOODS_FROM_FINLAND_BLUE_SWAN, EU_ORGANIC_FARMING, MARINE_STEWARDSHIP_COUNCIL_LABEL.
    temperature: ambient, chilled or frozen.
    food_only: default ranks food first (equipment first only when the query names it, e.g. "milk jug");
      true drops non-food (tableware, cleaning, disposables); false = no preference.
    Synonyms work across languages: "butter" also finds voi / smör.
    Returns product cards without price or stock; pass several of the ids to request_quote next.
    """
    return S.search(agent_id, query, claims=claims, avoid_allergens=avoid_allergens, labels=labels,
                    temperature=temperature, limit=limit, food_only=food_only)


@mcp.tool(title="Product details", annotations=READ_LOCAL)
@_tool
def get_product(product_id: str) -> dict:
    """Full static details of one product: names, pack sizes, allergens, claims, labels, reliability, CO2 estimate.

    No price or stock: those come only from request_quote.
    """
    p = catalog.get(product_id)
    if not p:
        raise S.GatewayError("product_not_found", f"No product {product_id}.", 404)
    return p


@mcp.tool(title="Request a quote (live price and stock)",
          annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                      open_world_hint=True))
@_tool
def request_quote(
    agent_id: str,
    delivery_date: str,
    items: list[S.QuoteItem],
    mandate_id: str | None = None,
) -> dict:
    """Get a signed, expiring quote with live prices and availability from Valio Aimo. Places no order.

    delivery_date: YYYY-MM-DD.
    items: one per need: {product_ids: [...], amount, unit: 'L' | 'KG' | 'SALES_UNIT', substitute_for?}.
      Give several candidate ids per need and an amount in L or KG: the gateway picks the cheapest orderable
      one per L/KG, splitting across products when stock is short. Mark substitutes with substitute_for.
    Always pass mandate_id: the quote then includes mandate_preview (silent | notify | ask | block, and why).
    Check quote.lines[].orderable, quote.uncovered and mandate_preview, then place_order with quote.id
    before quote.expires_at.
    """
    return S.request_quote(S.QuoteRequest(agent_id=agent_id, mandate_id=mandate_id,
                                          delivery_date=_date(delivery_date), items=items))


@mcp.tool(title="Suggest substitutes", annotations=READ_LIVE)
@_tool
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
    Tell the buyer the compromises, then request_quote for the chosen one with substitute_for=<original id>.
    """
    return S.suggest_substitutes(agent_id, product_id, _date(delivery_date), amount, unit,
                                 mandate_id, avoid_allergens, limit)


@mcp.tool(title="Place an order under the mandate",
          annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                      open_world_hint=True))
@_tool
def place_order(agent_id: str, mandate_id: str, quote_id: str, idempotency_key: str | None = None) -> dict:
    """Order a quote under a mandate. This is the only call that commits the buyer to a purchase.

    The mandate engine decides, not you: status ordered (silent/notify), pending_approval (a human must approve
    at approval_url; poll get_order) or refused (decision.findings say which rule fired).
    Always send an idempotency_key and reuse it on retries: the same key never orders twice.
    """
    return S.place_order(agent_id, mandate_id, quote_id, idempotency_key)


@mcp.tool(title="Order status", annotations=READ_LOCAL)
@_tool
def get_order(order_id: str) -> dict:
    """Current status of an order (ordered, pending_approval, refused, rejected, expired) and any human decision."""
    return S.get_order(order_id)


@mcp.tool(title="Read the mandate", annotations=READ_LOCAL)
@_tool
def get_mandate(mandate_id: str) -> dict:
    """The mandate you act under (spend limits, allergen and claim rules, what needs a human), with its signature."""
    return S.get_mandate(mandate_id)


@mcp.tool(title="Create a weekly standing order",
          annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                      open_world_hint=False))
@_tool
def create_standing_order(mandate_id: str, agent_id: str, name: str, items: list[standing.StandingItem],
                          weekday: str = "mon") -> dict:
    """Save the buyer's recurring weekly order (e.g. milk + butter + cream every Monday). Places no order.

    items: [{product_ids: [...candidates], amount, unit: 'L' | 'KG' | 'SALES_UNIT', label: 'milk'}].
    weekday: mon..sun, the usual delivery day. Renew it each week with renew_standing_order.
    """
    return standing.create(standing.StandingOrderIn(mandate_id=mandate_id, agent_id=agent_id, name=name,
                                                    items=items, weekday=weekday))


@mcp.tool(title="Renew a standing order (with drift check)",
          annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                      open_world_hint=True))
@_tool
def renew_standing_order(standing_id: str, agent_id: str, delivery_date: str | None = None,
                         amounts: list[float] | None = None) -> dict:
    """Renew this week's standing order: live quote, drift check against past renewals, then the order.

    amounts: this week's amount per item, in item order, if it differs from the usual (omit for the usual).
    The gateway compares with the median of recent renewals: quantity creep, cost or price drift, a different
    product, new Terminal or reliability issues. Any drift puts the order on hold for a human (pending_approval);
    tell the buyer drift.summary and share order.approval_url. delivery_date defaults to the next delivery day.
    """
    return standing.renew(standing_id, _date(delivery_date) if delivery_date else None, agent_id=agent_id,
                          amounts=amounts)


@mcp.tool(title="Standing order, history and drift baseline", annotations=READ_LOCAL)
@_tool
def get_standing_order(standing_id: str) -> dict:
    """A standing order with its renewals (newest first, simulated ones marked) and the drift baseline."""
    return standing.get(standing_id)


# --- resources --------------------------------------------------------------

@mcp.resource("valtakirja://allergens", title="GS1 allergen codes", mime_type="application/json")
def allergens() -> dict:
    """GS1 allergen codes for avoid_allergens, and the group codes that cover a whole family."""
    return {"codes": catalog.ALLERGENS,
            "groups": {k: sorted(v) for k, v in catalog.ALLERGEN_GROUPS.items()},
            "note": "Avoiding a group code (AM, AN, AW) also avoids every code in its group."}


@mcp.resource("valtakirja://mandates/{mandate_id}", title="Mandate", mime_type="application/json")
def mandate_resource(mandate_id: str) -> dict:
    """A mandate (valtakirja): limits and rules the agent acts under, with its JWS signature."""
    return S.get_mandate(mandate_id)


# --- prompts ----------------------------------------------------------------

@mcp.prompt(title="Buy something under a mandate")
def purchase(request: str, mandate_id: str, agent_id: str, delivery_date: str = "") -> str:
    """Walk through search -> quote -> order for one purchasing request."""
    when = delivery_date or "the earliest sensible delivery date (ask the buyer if it matters)"
    return f"""Purchasing request: {request}
Mandate: {mandate_id}. Act as agent_id {agent_id}. Delivery: {when}.

1. get_mandate {mandate_id}: note avoid_allergens, required_claims and the spend limits.
2. search_products for each need, passing the mandate's required_claims as claims and its avoid_allergens
   (plus any the request implies) as avoid_allergens. Shortlist 3-6 suitable ids per need.
3. request_quote with mandate_id, one item per need, all shortlisted ids, amount in L or KG where possible.
4. Show the buyer: chosen products, quantities, total ex VAT, delivery date, anything in quote.uncovered,
   and mandate_preview (outcome and each finding). If a line is not orderable or blocked, use
   suggest_substitutes and re-quote with substitute_for.
5. place_order with the quote id and a fresh idempotency_key. Report the status exactly as returned:
   ordered, pending_approval (share approval_url) or refused (quote the rule).
Use only facts the tools return."""


if __name__ == "__main__":
    mcp.run()
