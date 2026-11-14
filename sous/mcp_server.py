"""MCP interface: the same gateway operations as the REST API, as tools any agent can call.

Served over Streamable HTTP at /mcp by the main app (see api.py), or over stdio:
    uv run python -m sous.mcp_server

Trust: over HTTP every tool's read scope comes from the request's headers (sous/access.py). Over stdio there is
no request, so the process is the local operator and reads every mandate, quote and order in its database
(access.from_headers(None)); stock still follows SOUS_STOCK_EXPOSURE (stdio is not the approver). Run stdio only
where whoever starts the process may already read the database; never expose it to outside agents.

Every tool declares an outputSchema (MCP 2025-06-18 structured output) and returns its JSON object both as
structuredContent and, for older clients, as one text block with the same JSON. The schemas reuse the documentation
models served at /schemas/*.json (sous/standards.py). On failure the result has isError=true and the object is
{"error": <code>, "message": <what went wrong>, "hint": <what to do next>, "retryable": <bool>}.
"""

from __future__ import annotations

import functools
import json
import logging
from datetime import date
from typing import Any, Literal

import pydantic_core
from jsonschema.validators import validator_for
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from . import access, agent_auth, catalog, clock, exposure, rules, service as S, standing
from .mandate import Finding, Mandate
from .standards import OrderDoc, QuoteDoc

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
You buy from Valio Aimo (Finnish food-service wholesaler) for a buyer whose mandate (sous) you hold.
You need the mandate_id and your agent_id; the mandate is issued to exactly that agent_id.

Typical flow, 3-4 calls:
1. search_products: put dietary needs in filters, not just the query text: claims=["FREE_FROM_LACTOSE"],
   avoid_allergens=["AM"] (GS1 codes; resource sous://allergens). No price or stock here, and it is free.
2. request_quote with mandate_id: one item per need, with SEVERAL candidate product_ids and the amount in
   unit "L" or "KG" (e.g. 1000 L of milk), not pack counts. The gateway prices live, picks pack sizes,
   checks cut-off and stock, splits across products, and returns quote.mandate_preview.
   Read mandate_preview (outcome silent | notify | ask | block, with findings) and quote.uncovered BEFORE ordering.
   If quote.options is present (an item can't be covered as asked), present them to the buyer or pick per the
   mandate, then request_quote(option_id=...); never silently drop the uncovered amount.
   If a line is not orderable or the preview blocks it, call suggest_substitutes and re-quote with
   substitute_for=<original id>.
3. place_order(quote_id, mandate_id, idempotency_key=<unique string you reuse on retry>).
   Result status: ordered | pending_approval (a human decides; give them approval_url, check get_order later)
   | refused (tell the buyer which rule fired; you cannot override it).

If the mandate has require_agent_auth, quotes, orders and renewals need your agent credential: connect with
the header "Authorization: Bearer <token>" (or, where headers are impossible, pass agent_token). Never print it.

On an error, follow its "hint" field. Never state a price, stock level, delivery date or allergen fact
that a tool did not return; say "unknown" instead. Allergen data marked 'unknown' is not safe."""
if clock.is_demo():
    INSTRUCTIONS += (f"\n\nDEMO CLOCK: this gateway evaluates cut-offs, quote expiry and daily limits as if it were "
                     f"{clock.info()['configured']} ({'frozen' if not clock.info()['advancing'] else 'advancing in real time'}), "
                     "because Valio's mock API returns fixed dates. Quotes and orders carry clock.mode='demo'; "
                     "tell the user this is a demo clock if you mention dates or cut-offs.")

mcp = MCPServer(name="sous", title="Sous: Valio Aimo agent gateway", instructions=INSTRUCTIONS,
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
    "agent_auth_required": ("This mandate requires agent authentication. Reconnect with the header "
                            "'Authorization: Bearer <agent token>' from the buyer (or pass agent_token); "
                            "don't retry without it.", False),
    "agent_auth_invalid": ("Your agent credential is wrong, revoked or for another mandate. Ask the buyer for a "
                           "new one (they rotate it); don't guess or retry.", False),
    "sandbox_limit": ("This judge sandbox is out of live Valio lookups for now. Tell the user; retry after "
                      "retry_after seconds, or reuse an earlier quote. Don't loop.", False),
    "sandbox_expired": ("This sandbox has expired or was closed. The user can create a new one (POST /sandbox).",
                        False),
    "option_not_found": ("Use an options[].id (Q-...O<n>) from your own latest quote, or request a new quote.", False),
    "invalid_request": ("Pass delivery_date and items, or option_id from a previous quote's options[].", False),
    "invalid_argument": ("Fix the argument named in the message and call the tool again.", False),
    "internal_error": ("Unexpected gateway error. Retry once; if it persists, tell the buyer and stop.", True),
}


def _error(code: str, message: str, retry_after: int | None = None) -> CallToolResult:
    hint, retryable = HINTS.get(code, ("Read the message and adjust the request.", False))
    body = {"error": code, "message": message, "hint": hint, "retryable": retryable}
    if retry_after is not None:
        body["retry_after"] = retry_after
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(body, ensure_ascii=False))],
                          structured_content=body, is_error=True)


# --- output schemas -----------------------------------------------------------
# Each tool's outputSchema. Quotes, orders and mandates reuse the models behind /schemas/*.json
# (sous/standards.py), so REST and MCP describe one shape. All objects are open (additionalProperties allowed):
# new fields don't break clients that validate structuredContent.

class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class ProductCardOut(_Open):
    """A catalog card: static data only, no price or stock."""
    id: str
    name: str
    brand: str | None = None
    sales_unit: str | None = None
    size_from_name: list[Any] | None = Field(None, description="[amount, unit] parsed from the name, e.g. [1.0, 'L']")
    temperature: str | None = None
    claims: list[str] = []
    labels: list[str] = []
    allergens: dict[str, Any] | None = Field(None, description="{declared, contains[], may_contain[], free_from[]}")
    allergen_check: Any = Field(None, description="Present when avoid_allergens was given: per-product verdict")
    delivery_reliability: float | None = None
    co2_kg_per_kg_group_avg: float | None = None
    category: str | None = None


class SearchOut(_Open):
    products: list[ProductCardOut]
    note: str


class ProductOut(_Open):
    """Full static product record (names, units, allergens, claims, labels, reliability, CO2)."""
    id: str
    name: str


class QuoteOptionOut(_Open):
    """A complete alternative for one item that can't be covered as asked (take it with option_id)."""
    id: str = Field(description="Pass as option_id to request_quote, e.g. Q-1A2B3C4D.O2")
    rank: int
    item_index: int
    kind: str
    tradeoff: str
    lines: list[dict[str, Any]]
    total_ex_vat: float
    mandate_preview: dict[str, Any] | None = Field(None, description="{outcome, rules[]} if a mandate was given")


class QuoteBodyOut(QuoteDoc):
    options: list[QuoteOptionOut] | None = None
    chosen_option: str | None = None


class QuoteOut(_Open):
    """request_quote: the quote payload and its JWS (typ sous-quote+jws). Schema of `quote`: /schemas/quote.json."""
    quote: QuoteBodyOut
    jws: str = Field(description="Compact JWS over `quote`, verifiable with /.well-known/jwks.json")


class SubstituteOptionOut(_Open):
    tier: str = Field(description="good | better | best")
    product_id: str
    compromises: list[Any]


class SubstitutesOut(_Open):
    original: dict[str, Any]
    constraints: dict[str, Any]
    considered: dict[str, Any]
    options: list[SubstituteOptionOut]
    no_safe_substitute: str | None
    next_step: str | None = None


class AgentClaim(_Open):
    text: str
    verified: Literal[False]
    note: str
    suspicious: bool


class OrderOut(OrderDoc):
    """An order with its signed receipt. Schema: /schemas/order.json."""
    agent_claim: AgentClaim | None = Field(None, description="get_order: the agent's rationale, labelled unverified")


class MandateOut(_Open):
    """get_mandate: the signed mandate payload, its JWS (typ sous-mandate+jws), issuer and optional W3C VC."""
    mandate: Mandate
    jws: str
    issuer: dict[str, Any]
    credential: str | None = Field(None, description="vc+jwt credential when the buyer issued one")
    revocation: str | None = None


class MandateCheckOut(_Open):
    outcome: Literal["silent", "notify", "ask", "block"]
    findings: list[Finding]


class SuggestionOut(_Open):
    id: str
    kind: str
    sponsored_by: str
    disclosure: str
    product: ProductCardOut
    reason: str
    offer: dict[str, Any]
    mandate_check: MandateCheckOut
    how_to_accept: str


class SuggestionsOut(_Open):
    mandate_id: str
    buyer: str
    agent_id: str
    delivery_date: str
    sponsored_by: str
    disclosure: str
    suggestions: list[SuggestionOut]
    note: str | None = None


class StandingOrderOut(_Open):
    id: str = Field(description="SO-XXXXXXXX")
    mandate_id: str
    agent_id: str
    buyer: str
    name: str
    items: list[dict[str, Any]]
    cadence: dict[str, Any]
    active: bool
    created_at: str
    updated_at: str


class RenewalOut(_Open):
    renewal: dict[str, Any]
    order: OrderOut
    drift: dict[str, Any] = Field(description="{findings[], summary, baseline}")


class StandingDetailOut(_Open):
    standing_order: StandingOrderOut
    renewals: list[dict[str, Any]]
    baseline: Any
    next_delivery_date: str
    versions: list[Any]
    rules: dict[str, Any]


def output_schema(model: type[BaseModel]) -> dict:
    """The JSON Schema (2020-12) of a tool's structuredContent; $refs stay inside the document ($defs)."""
    return model.model_json_schema()


_validators: dict[str, Any] = {}


def _result(name: str, body: Any) -> CallToolResult:
    """Success: the object as structuredContent plus the same JSON as text (pre-2025-06-18 clients)."""
    data = pydantic_core.to_jsonable_python(body, fallback=str)
    if (v := _validators.get(name)) is not None and (err := next(v.iter_errors(data), None)) is not None:
        # Clients may reject the result; log it so the schema gets fixed, but never hide the result itself.
        log.warning("MCP tool %s: structuredContent does not match its outputSchema: %s at %s",
                    name, err.message, "/".join(map(str, err.absolute_path)))
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))],
                          structured_content=data)


def _tool(output: type[BaseModel], *, title: str, annotations: ToolAnnotations):
    """Register a tool with an outputSchema; gateway errors (and anything unexpected) become one structured error."""

    def decorator(fn):
        @functools.wraps(fn)
        def safe(*args, **kwargs):
            """The tool as a Python function: the object on success, an error CallToolResult on failure."""
            try:
                return fn(*args, **kwargs)
            except S.GatewayError as e:
                return _error(e.code, e.message, e.retry_after)
            except ValueError as e:
                return _error("invalid_argument", str(e))
            except Exception as e:  # noqa: BLE001 - the agent gets a clean error, the log gets the trace
                log.exception("MCP tool %s failed", fn.__name__)
                return _error("internal_error", f"{type(e).__name__} in {fn.__name__}.")

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            out = safe(*args, **kwargs)
            return out if isinstance(out, CallToolResult) else _result(fn.__name__, out)

        # structured_output=False: the SDK neither derives a schema from the annotation nor validates; we publish
        # our own schema (FuncMetadata.output_schema is read live) and return CallToolResult ourselves.
        mcp.add_tool(wrapper, title=title, annotations=annotations, structured_output=False)
        schema = output_schema(output)
        mcp._tool_manager.get_tool(fn.__name__).fn_metadata.output_schema = schema  # noqa: SLF001
        _validators[fn.__name__] = validator_for(schema)(schema)
        return safe

    return decorator


def _date(value: str, field: str = "delivery_date") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be YYYY-MM-DD, got {value!r}.") from None


def _agent_token(ctx: Context | None, agent_token: str | None) -> str | None:
    """The agent credential: the HTTP request's Authorization: Bearer header (Streamable HTTP), else the argument."""
    try:
        headers = ctx.headers if ctx is not None else None
    except ValueError:  # no request context (e.g. called outside a request)
        headers = None
    return agent_auth.bearer((headers or {}).get("authorization")) or agent_token or None


def _reader(ctx: Context | None, agent_token: str | None = None) -> access.Reader:
    """Read scope (sous/access.py) from the HTTP request's X-Approver-Code / Authorization headers. Over stdio
    there is no request: the local operator reads everything."""
    try:
        headers = ctx.headers if ctx is not None else None
    except ValueError:
        headers = None
    if headers is None:
        return access.from_headers(None)
    h = {k.lower(): v for k, v in headers.items()}
    if agent_token and not agent_auth.bearer(h.get("authorization")):
        h["authorization"] = f"Bearer {agent_token}"
    return access.from_headers(h)


# All four hints on every tool. open_world: the call reaches Valio Aimo's live API (prices, stock, orders).
READ_LOCAL = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
READ_LIVE = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=False, open_world_hint=True)


# --- tools ------------------------------------------------------------------

@_tool(SearchOut, title="Search the catalog", annotations=READ_LOCAL)
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
      no allergen data are kept but marked 'unknown'. Full list: resource sous://allergens.
    labels: e.g. GOODS_FROM_FINLAND_BLUE_SWAN, EU_ORGANIC_FARMING, MARINE_STEWARDSHIP_COUNCIL_LABEL.
    temperature: ambient, chilled or frozen.
    food_only: default ranks food first (equipment first only when the query names it, e.g. "milk jug");
      true drops non-food (tableware, cleaning, disposables); false = no preference.
    Synonyms work across languages: "butter" also finds voi / smör.
    Returns {products: [card], note}: cards without price or stock; pass several of the ids to request_quote next.
    """
    return S.search(agent_id, query, claims=claims, avoid_allergens=avoid_allergens, labels=labels,
                    temperature=temperature, limit=limit, food_only=food_only)


@_tool(ProductOut, title="Product details", annotations=READ_LOCAL)
def get_product(product_id: str) -> dict:
    """Full static details of one product: names, pack sizes, allergens, claims, labels, reliability, CO2 estimate.

    No price or stock: those come only from request_quote.
    """
    p = catalog.get(product_id)
    if not p:
        raise S.GatewayError("product_not_found", f"No product {product_id}.", 404)
    return p


@_tool(QuoteOut, title="Request a quote (live price and stock)",
       annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                   open_world_hint=True))
def request_quote(
    agent_id: str,
    delivery_date: str | None = None,
    items: list[S.QuoteItem] | None = None,
    mandate_id: str | None = None,
    rationale: str | None = None,
    option_id: str | None = None,
    agent_token: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Get a signed, expiring quote with live prices and availability from Valio Aimo. Places no order.

    delivery_date: YYYY-MM-DD.
    items: one per need: {product_ids: [...], amount, unit: 'L' | 'KG' | 'SALES_UNIT', substitute_for?}
    (ST, PCS, KPL, pack, packs also mean SALES_UNIT).
      Give several candidate ids per need and an amount in L or KG: the gateway picks the cheapest orderable
      one per L/KG, splitting across products when stock is short. Mark substitutes with substitute_for.
    Always pass mandate_id: the quote then includes mandate_preview (silent | notify | ask | block, and why).
    Check quote.lines[].orderable, quote.uncovered and mandate_preview, then place_order with quote.id
    before quote.expires_at.
    If quote.options is present, an item can't be covered as asked (stock short, later date, cut-off passed,
      undeliverable). Each option is a complete alternative for that item (kind, lines, total, delivery_dates,
      tradeoff, substitute, mandate_preview), best first. Present them to the buyer, or pick per the mandate
      (an option whose mandate_preview is silent/notify needs no human). Never silently drop the uncovered amount.
    option_id: to take an option, call request_quote again with only agent_id, mandate_id and option_id
      (e.g. 'Q-1A2B3C4D.O2'): you get a fresh signed quote with that item's lines replaced; then place_order it.
    rationale: one sentence on the buyer's business need (e.g. "breakfast milk for 400 guests"); it is signed into
      the quote and shown to the humans who audit or approve. State the need, not your reasoning steps.
    agent_token: only if you can't send the Authorization: Bearer header and the mandate requires agent auth.
    Returns {quote (schema /schemas/quote.json), jws (sous-quote+jws over quote)}.
    """
    return S.request_quote(S.QuoteRequest(agent_id=agent_id, mandate_id=mandate_id,
                                          delivery_date=_date(delivery_date) if delivery_date else None,
                                          items=items or [], rationale=rationale, option_id=option_id),
                           agent_token=_agent_token(ctx, agent_token),
                           approver=exposure.is_approver(_reader(ctx, agent_token)))


@_tool(SubstitutesOut, title="Suggest substitutes", annotations=READ_LIVE)
def suggest_substitutes(
    agent_id: str,
    product_id: str,
    delivery_date: str | None,
    amount: float,
    unit: str = "SALES_UNIT",
    mandate_id: str | None = None,
    avoid_allergens: list[str] | None = None,
    limit: int = 3,
    agent_token: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Good / better / best substitutes for a product that can't be ordered (or for any product).

    Same category, measure and temperature class; keeps the original's dietary claims and the mandate's
    allergen rules (never relaxed). Each option lists its compromises in plain language and has a live offer.
    Tell the buyer the compromises, then request_quote for the chosen one with substitute_for=<original id>.
    Another kind of product is never offered: with nothing acceptable, no_safe_substitute says to ask a human and
    not_offered lists what was ruled out and why. delivery_date null = the next delivery day.
    With mandate_id it is read-scoped like get_mandate (send your Authorization: Bearer header or agent_token).
    """
    return S.suggest_substitutes(agent_id, product_id, _date(delivery_date) if delivery_date else None, amount, unit,
                                 mandate_id, avoid_allergens, limit, agent_token=_agent_token(ctx, agent_token),
                                 reader=_reader(ctx, agent_token))


@_tool(OrderOut, title="Place an order under the mandate",
       # idempotent: a retry with the same idempotency_key returns the same order instead of ordering again.
       annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True,
                                   open_world_hint=True))
def place_order(agent_id: str, mandate_id: str, quote_id: str, idempotency_key: str | None = None,
                rationale: str | None = None, agent_token: str | None = None,
                ctx: Context | None = None) -> dict:
    """Order a quote under a mandate. This is the only call that commits the buyer to a purchase.

    The mandate engine decides, not you: status ordered (silent/notify), pending_approval (a human must approve
    at approval_url; poll get_order) or refused (decision.findings say which rule fired).
    Always send an idempotency_key and reuse it on retries: the same key never orders twice (the retry returns
    the original order); reusing a key for a different quote fails with idempotency_key_reused.
    Returns the order (schema /schemas/order.json): id (PO-...), status, decision {outcome, findings}, lines,
    totals, approval_url when pending, and receipt_jws (signed receipt).
    rationale: one sentence on why the buyer needs this order; the approver sees it next to the mandate's findings.
    agent_token: only if you can't send the Authorization: Bearer header and the mandate requires agent auth.
    """
    return S.place_order(agent_id, mandate_id, quote_id, idempotency_key,
                         agent_token=_agent_token(ctx, agent_token), rationale=rationale)


@_tool(OrderOut, title="Order status", annotations=READ_LOCAL)
def get_order(order_id: str, agent_token: str | None = None, ctx: Context | None = None) -> dict:
    """Current status of an order (ordered, pending_approval, refused, rejected, expired) and any human decision.
    Only orders under your own mandate: send your Authorization: Bearer header (or agent_token).
    Returns the order (schema /schemas/order.json) with approval {by, approved, note, at} once a human decided,
    a fresh receipt_jws, and agent_claim (the rationale, labelled unverified)."""
    access.check_order(_reader(ctx, agent_token), order_id)
    return access.label_order(S.get_order(order_id))


@_tool(MandateOut, title="Read the mandate", annotations=READ_LOCAL)
def get_mandate(mandate_id: str, agent_token: str | None = None, ctx: Context | None = None) -> dict:
    """The mandate you act under (spend limits, allergen and claim rules, what needs a human), with its signature.
    Only your own mandate (send your Authorization: Bearer header or agent_token) or a public demo mandate.
    Returns {mandate (schema /schemas/mandate.json), jws (sous-mandate+jws), issuer, credential?, revocation?}."""
    access.check_mandate(_reader(ctx, agent_token), mandate_id)
    return S.get_mandate(mandate_id)


@_tool(SuggestionsOut, title="Valio Aimo's suggestions (sponsored)", annotations=READ_LIVE)
def get_suggestions(mandate_id: str, agent_id: str, delivery_date: str, limit: int = 5,
                    basis: list[str] | None = None, agent_token: str | None = None,
                    ctx: Context | None = None) -> dict:
    """Valio Aimo's own COMMERCIAL suggestions for your buyer: the seller promoting products, not neutral advice.

    Better-value alternatives to what the buyer usually orders (cheaper per L/KG, a certification label,
    more reliable delivery or a lower CO2 estimate), seasonal products for delivery_date, and recently listed
    ones. basis: optional product ids (e.g. the current cart) to find alternatives for, besides order history.
    Everything was pre-checked against the mandate (nothing it would block is shown) and has a live offer and
    mandate_check (the tier it would get at the usual quantity). Nothing is ordered.
    Present each one to the buyer as "Valio Aimo suggests...", with its reason and disclosure; never as your own
    recommendation, and never order one unless the buyer asks: then request_quote as how_to_accept says.
    Read-scoped like get_mandate: send your Authorization: Bearer header (or agent_token) unless the mandate is a
    public demo; without one the error is agent_auth_required.
    """
    from . import suggest
    return suggest.suggestions(mandate_id, agent_id, _date(delivery_date), basis, limit,
                               agent_token=_agent_token(ctx, agent_token), reader=_reader(ctx, agent_token))


@_tool(StandingOrderOut, title="Create a weekly standing order",
       annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                   open_world_hint=False))
def create_standing_order(mandate_id: str, agent_id: str, name: str, items: list[standing.StandingItem],
                          weekday: str = "mon") -> dict:
    """Save the buyer's recurring weekly order (e.g. milk + butter + cream every Monday). Places no order.

    items: [{product_ids: [...candidates], amount, unit: 'L' | 'KG' | 'SALES_UNIT', label: 'milk'}].
    weekday: mon..sun, the usual delivery day. Renew it each week with renew_standing_order.
    """
    return standing.create(standing.StandingOrderIn(mandate_id=mandate_id, agent_id=agent_id, name=name,
                                                    items=items, weekday=weekday))


@_tool(RenewalOut, title="Renew a standing order (with drift check)",
       annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                   open_world_hint=True))
def renew_standing_order(standing_id: str, agent_id: str, delivery_date: str | None = None,
                         amounts: list[float] | None = None, rationale: str | None = None,
                         agent_token: str | None = None, ctx: Context | None = None) -> dict:
    """Renew this week's standing order: live quote, drift check against past renewals, then the order.

    amounts: this week's amount per item, in item order, if it differs from the usual (omit for the usual).
    The gateway compares with the median of recent renewals: quantity creep, cost or price drift, a different
    product, new Terminal or reliability issues. Any drift puts the order on hold for a human (pending_approval);
    tell the buyer drift.summary and share order.approval_url. delivery_date defaults to the next delivery day.
    agent_token: only if you can't send the Authorization: Bearer header and the mandate requires agent auth.
    """
    return standing.renew(standing_id, _date(delivery_date) if delivery_date else None, agent_id=agent_id,
                          amounts=amounts, agent_token=_agent_token(ctx, agent_token),
                          rationale=rationale)


@_tool(StandingDetailOut, title="Standing order, history and drift baseline", annotations=READ_LOCAL)
def get_standing_order(standing_id: str, agent_token: str | None = None, ctx: Context | None = None) -> dict:
    """A standing order with its renewals (newest first, simulated ones marked) and the drift baseline.
    Returns {standing_order, renewals, baseline, next_delivery_date, versions, rules}."""
    access.check_standing(_reader(ctx, agent_token), standing_id)
    return standing.get(standing_id)


# --- resources --------------------------------------------------------------

@mcp.resource("sous://allergens", title="GS1 allergen codes", mime_type="application/json")
def allergens() -> dict:
    """GS1 allergen codes for avoid_allergens, and the group codes that cover a whole family."""
    return {"codes": catalog.ALLERGENS,
            "groups": {k: sorted(v) for k, v in catalog.ALLERGEN_GROUPS.items()},
            "note": "Avoiding a group code (AM, AN, AW) also avoids every code in its group."}


@mcp.resource("sous://rules", title="Rule register", mime_type="application/json")
def rules_resource() -> dict:
    """Every rule the gateway can fire: tiers, owner (buyer mandate / Valio / gateway), text, mandate field.
    Decision findings carry rule_url pointing at the same entries (GET /rules/{id})."""
    return rules.document()


@mcp.resource("sous://mandates/{mandate_id}", title="Mandate", mime_type="application/json")
def mandate_resource(mandate_id: str, ctx: Context | None = None) -> dict:
    """A mandate (sous): limits and rules the agent acts under, with its JWS signature (yours or a public demo one)."""
    access.check_mandate(_reader(ctx), mandate_id)
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
    log.warning("MCP over stdio: local operator scope, every read is allowed "
                "(docs/PROTOCOL.md section 22). Don't expose this process to outside agents.")
    mcp.run()
