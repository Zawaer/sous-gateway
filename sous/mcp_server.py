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
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from . import access, agent_auth, catalog, clock, config, exposure, signing, holds, offers, origin as origin_mod, oauth, remembered, rules, sandbox, service as S, standing, store
from .mandate import Finding, Mandate
from .standards import OrderDoc, QuoteDoc

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
You buy from Valio Aimo (Finnish food-service wholesaler) for a buyer, under the mandate that buyer signed. The
gateway's rules decide what you may order alone; you can't override them.

Core tools, in this order: my_mandates -> search_products -> request_quote (or can_i_order) -> place_order.
Every other tool is optional.

Worked example. The user (the hotel) says: "1,000 litres of lactose-free milk for Monday."
1. my_mandates() -> pick the hotel's mandate, e.g. "M-HOTEL-AURORA". Never ask the user for ids it gives you.
2. search_products(query="milk", claims=["FREE_FROM_LACTOSE"]) -> several plain lactose-free milk ids.
   Dietary needs go in the filters (claims, avoid_allergens), not only the text. Free; no prices here.
3. can_i_order(mandate_id="M-HOTEL-AURORA", delivery_date=<Monday, YYYY-MM-DD>,
   items=[{product_ids: [the ids], amount: 1000, unit: "L", product_type: "milk"}]) -> answer "yes" (order it),
   "needs_a_human" (ordering sends it to the buyer's human) or "no" (say why, with the rule), plus the quote_id.
   request_quote returns the same quote in full: read its mandate_preview, uncovered and options before ordering.
   If options are offered, show them or take one with request_quote(option_id=...); never drop a shortfall silently.
4. place_order(mandate_id="M-HOTEL-AURORA", quote_id=<quote_id>, idempotency_key=<unique, reused on retry>)
   -> ordered | pending_approval (give the user approval_url) | refused (tell them which rule).

Ask for amounts in L or KG with several candidate ids per need; mark substitutes with substitute_for
(suggest_substitutes finds them). Send your credential as "Authorization: Bearer <token>" and never print it.
An ordered order is placed with Sous; when erp.simulated is true, say the Valio hand-off is simulated in this demo.
On an error follow its "hint". Never state a price, stock level, date or allergen fact a tool didn't return:
say "unknown". Missing allergen data is not safe."""
if clock.is_demo():
    INSTRUCTIONS += (f"\n\nDEMO CLOCK: this gateway evaluates cut-offs, quote expiry and daily limits as if it were "
                     f"{clock.info()['configured']} ({'frozen' if not clock.info()['advancing'] else 'advancing in real time'}), "
                     "because Valio's mock API returns fixed dates. Quotes and orders carry clock.mode='demo'; "
                     "tell the user this is a demo clock if you mention dates or cut-offs.")

# MCP Apps (io.modelcontextprotocol/ui): quotes, orders, substitutes and renewals carry a card that chat hosts
# (Claude on web, desktop and phone) render under the tool call: web/mcp-card.html, served as ui://sous/card.html.
# The card gets the gateway's public keys in the result's _meta (hosts give _meta to the card, not to the model)
# and checks every signature itself. Clients without Apps support get the same JSON as before.
CARD_URI = "ui://sous/card.html"
CARD_META_KEY = "sous/card"
UI_TOOLS = {"request_quote", "place_order", "get_order", "suggest_substitutes", "renew_standing_order"}
_card_html = (config.ROOT / "web" / "mcp-card.html")
apps = Apps()
apps.add_html_resource(
    CARD_URI, _card_html.read_text(encoding="utf-8") if _card_html.is_file() else "<p>Sous card unavailable.</p>",
    name="sous-cards", title="Sous order cards",
    description="Quote, order, approval and substitute cards for the Sous gateway's tool results (MCP Apps).")

mcp = MCPServer(name="sous", title="Sous: Valio Aimo agent gateway", instructions=INSTRUCTIONS,
                version="0.2.0", extensions=[apps])


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
    content_per_sales_unit: dict[str, Any] | None = Field(
        None, description="{amount, unit, source, basis}: how much one sales unit holds (e.g. 3 L per KI of 6 x 0.5 l), "
        "from the catalog only; a quote confirms it against Valio's price ratio")


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


class CanIOrderOut(_Open):
    """can_i_order: the mandate's answer for this request, in one step, and the quote to order it with."""
    answer: Literal["yes", "needs_a_human", "no"] = Field(description="yes: place_order orders it (tier silent or "
        "notify); needs_a_human: place_order holds it for the buyer's human (ask); no: the mandate refuses it (block)")
    tier: str = Field(description="silent | notify | ask | block")
    why: list[str] = Field(description="The rules that decided, in plain words (empty when nothing stood out)")
    quote_id: str
    expires_at: str
    total_ex_vat: float
    lines: list[dict[str, Any]] = Field(description="What would be ordered: product_id, name, quantity, unit, total")
    uncovered: list[dict[str, Any]] = Field(description="Amounts the quote can't cover; see options")
    options: int = Field(description="How many alternatives the full quote offers (request_quote shows them)")
    next_step: str


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
    amendments: list[dict[str, Any]] | None = Field(
        None, description="Valio's post-order exceptions (short_pick, delay, substitution_at_pick, cancel_line), "
        "each {id, kind, line, reason, tier, findings[], status: applied|pending_buyer|accepted|rejected, effect, "
        "resolution, jws (typ sous-amendment+jws)}")
    credit_notes: list[dict[str, Any]] | None = Field(None, description="Credit (or extra charge) lines from amendments")
    erp: dict[str, Any] | None = Field(
        None, description="Hand-off to Valio Aimo's order system (unsigned): {adapter: stub|http, simulated, status: "
        "pending|sent|confirmed|simulated|failed, confirmation_id, note, ...}. simulated=true (status 'simulated', "
        "confirmation_id 'SIM-...'): no real order was sent to Valio Aimo (no order API in this demo); describe "
        "the order as 'placed with Sous; Valio hand-off simulated in this demo', never as confirmed by Valio.")


class MandateOut(_Open):
    """get_mandate: the signed mandate payload, its JWS (typ sous-mandate+jws), issuer and optional W3C VC."""
    mandate: Mandate
    jws: str
    issuer: dict[str, Any]
    credential: str | None = Field(None, description="vc+jwt credential when the buyer issued one")
    revocation: str | None = None
    remembered: list[dict[str, Any]] = Field(default_factory=list, description=(
        "Remembered decisions (approve and remember, sous/remembered.py): findings a human approved earlier that "
        "now notify instead of ask: {id, rule, product_id, original_product_id?, by, created_at, expires_at, "
        "max_quantity?, max_line_total_eur?, jws (sous-remembered+jws)}. Tell the buyer when one was used."))


class MandateCheckOut(_Open):
    outcome: Literal["silent", "notify", "ask", "block"]
    findings: list[Finding]


class MyMandateOut(_Open):
    mandate_id: str
    buyer: str
    label: str | None = Field(None, description="Scenario of a judge sandbox mandate: hotel, hospital, school")
    agent_id: str
    sandbox: str | None = None
    active: bool
    valid_until: str | None = None
    require_agent_auth: bool
    rules: str = Field(description="The mandate's active rules in one line (rules.mandate_summary)")
    remembered: list[dict[str, Any]] = Field(default_factory=list, description=(
        "Active remembered decisions (rule, product_id, by, expires_at): these findings notify instead of ask"))


class MyMandatesOut(_Open):
    """my_mandates: the mandates the caller's credential covers."""
    mandates: list[MyMandateOut]
    note: str


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


class OfferProductOut(_Open):
    product_id: str
    name: str
    price_ex_vat: float
    reference_price_ex_vat: float | None = None


class OfferOut(_Open):
    id: str
    sponsored_by: str
    disclosure: str
    status: str
    reason: str
    reason_text: str
    headline: str
    products: list[OfferProductOut]
    valid_until: str
    quantity_cap: float
    remaining: float
    how_to_accept: str
    jws: str


class OffersOut(_Open):
    mandate_id: str
    buyer: str
    sponsored_by: str
    disclosure: str
    offers: list[OfferOut]
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
    meta = {CARD_META_KEY: {"jwks": signing.jwks(), "gateway": config.PUBLIC_BASE_URL}} if name in UI_TOOLS else None
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))],
                          structured_content=data, _meta=meta)


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
        ui = {"ui": {"resourceUri": CARD_URI}, "ui/resourceUri": CARD_URI} if fn.__name__ in UI_TOOLS else None
        mcp.add_tool(wrapper, title=title, annotations=annotations, structured_output=False, meta=ui)
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


def _own_mandates(ctx: Context | None, agent_token: str | None) -> set[str]:
    """The mandates the caller's credential was issued for (Bearer header, which /connect/mcp fills from the OAuth
    token, the agent_token argument, or a sandbox approver token in X-Approver-Code)."""
    try:
        headers = ctx.headers if ctx is not None else None
    except ValueError:
        headers = None
    h = {k.lower(): v for k, v in (headers or {}).items()}
    return access.credential_mandates(agent_auth.bearer(h.get("authorization")), agent_token,
                                      h.get("x-approver-code"))


def _agent_for(agent_id: str | None, mandate_id: str | None, ctx: Context | None, agent_token: str | None) -> str:
    """agent_id as given, else the agent the mandate is issued to when the caller's credential covers that mandate
    (the agent never has to know its own id). Authentication is unchanged: the mandate still checks the token."""
    if agent_id:
        return agent_id
    if mandate_id and mandate_id in _own_mandates(ctx, agent_token):
        if m := store.get_mandate(mandate_id):
            return m["agent_id"]
    raise ValueError("agent_id is required unless your credential covers mandate_id: call my_mandates to see "
                     "your mandates and their agent_id.")


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

@_tool(SearchOut, title="Core 2/4: Search the catalog", annotations=READ_LOCAL)
def search_products(
    query: str,
    claims: list[str] | None = None,
    avoid_allergens: list[str] | None = None,
    labels: list[str] | None = None,
    temperature: str | None = None,
    limit: int = 15,
    agent_id: str = "anonymous-agent",
    food_only: bool | None = None,
    origin: list[str] | None = None,
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
    origin: keep only products declared from these countries or groups, e.g. ["fi"], ["EU"], ["NORDIC"]; the
      country is supplier-declared (not verified) and products without one drop out. Every card and quote line
      carries country_of_origin.
    Synonyms work across languages: "butter" also finds voi / smör.
    Returns {products: [card], note}: cards without price or stock; pass several of the ids to request_quote next.
    """
    try:
        origins = origin_mod.normalize(origin) if origin else None
    except ValueError as e:
        raise S.GatewayError("invalid_request", str(e), 422)
    return S.search(agent_id, query, claims=claims, avoid_allergens=avoid_allergens, labels=labels,
                    temperature=temperature, limit=limit, food_only=food_only, origins=origins)


@_tool(ProductOut, title="Optional: Product details", annotations=READ_LOCAL)
def get_product(product_id: str) -> dict:
    """Full static details of one product: names, pack sizes, allergens, claims, labels, reliability, CO2 estimate.

    No price or stock: those come only from request_quote.
    """
    p = catalog.get(product_id)
    if not p:
        raise S.GatewayError("product_not_found", f"No product {product_id}.", 404)
    return p


@_tool(QuoteOut, title="Core 3/4: Quote with live price, stock and the mandate check",
       annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                   open_world_hint=True))
def request_quote(
    agent_id: str | None = None,
    delivery_date: str | None = None,
    items: list[S.QuoteItem] | None = None,
    mandate_id: str | None = None,
    rationale: str | None = None,
    option_id: str | None = None,
    offer_id: str | None = None,
    agent_token: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Get a signed, expiring quote with live prices and availability from Valio Aimo. Places no order.

    delivery_date: YYYY-MM-DD.
    items: one per need: {product_ids: [...], amount, unit: 'L' | 'KG' | 'SALES_UNIT', substitute_for?}
    (pack, packs mean SALES_UNIT = cartons/packs as sold; 'ST' (PCS, KPL) counts pieces when the product has
    them: 20 ST of a carton of 6 = 4 cartons, rounded up, and the line says so in unit_note).
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
    offer_id: a Valio Aimo offer from list_offers (e.g. 'OF-1A2B3C4D'), only when the buyer wants it: name the
      offer's product_ids in items; they are priced at min(offer price, live price) and the quote carries `offer`.
      The mandate decides as always (rule offer.applied tells the buyer).
    agent_id: optional when your credential covers mandate_id (see my_mandates); required without a mandate.
    agent_token: only if you can't send the Authorization: Bearer header and the mandate requires agent auth.
    Returns {quote (schema /schemas/quote.json), jws (sous-quote+jws over quote)}.
    """
    agent_id = _agent_for(agent_id, mandate_id, ctx, agent_token)
    return S.request_quote(S.QuoteRequest(agent_id=agent_id, mandate_id=mandate_id,
                                          delivery_date=_date(delivery_date) if delivery_date else None,
                                          items=items or [], rationale=rationale, option_id=option_id,
                                          offer_id=offer_id),
                           agent_token=_agent_token(ctx, agent_token),
                           approver=exposure.is_approver(_reader(ctx, agent_token)))


@_tool(CanIOrderOut, title="Core 3/4 shortcut: Can I order this?",
       annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                   open_world_hint=True))
def can_i_order(
    mandate_id: str,
    delivery_date: str,
    items: list[S.QuoteItem],
    agent_id: str | None = None,
    rationale: str | None = None,
    agent_token: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """What am I allowed to do? One step: quotes the request live under the mandate (same as request_quote) and
    answers yes / needs_a_human / no, with the rules that decided, the lines it would order and the quote_id.
    Places no order. If the answer is yes or needs_a_human, call place_order with quote_id (and an idempotency_key)
    before expires_at; if no, tell the user why. items as in request_quote (amount in L or KG, several ids)."""
    agent_id = _agent_for(agent_id, mandate_id, ctx, agent_token)
    r = S.request_quote(S.QuoteRequest(agent_id=agent_id, mandate_id=mandate_id, delivery_date=_date(delivery_date),
                                       items=items, rationale=rationale),
                        agent_token=_agent_token(ctx, agent_token),
                        approver=exposure.is_approver(_reader(ctx, agent_token)))
    q = r["quote"]
    prev = q.get("mandate_preview") or {}
    tier = prev.get("outcome") or "block"
    answer = {"silent": "yes", "notify": "yes", "ask": "needs_a_human"}.get(tier, "no")
    why = [f["message"] for f in prev.get("findings") or [] if f.get("outcome") in ("notify", "ask", "block")
           and f.get("rule") != "mandate.ok"]
    nxt = {"yes": "place_order with quote_id: it will be ordered" + (" and the buyer told" if tier == "notify" else ""),
           "needs_a_human": "place_order with quote_id holds it for the buyer's human; give them the approval_url",
           "no": "don't order; tell the user why. request_quote shows alternatives (options) when there are any"}[answer]
    if q.get("uncovered"):
        nxt += ". Part of the request can't be covered: see uncovered and request_quote's options"
    return {"answer": answer, "tier": tier, "why": why, "quote_id": q["id"], "expires_at": q["expires_at"],
            "total_ex_vat": q.get("total_ex_vat", 0.0),
            "lines": [{k: l.get(k) for k in ("product_id", "name", "quantity", "sales_unit", "line_total_ex_vat",
                                              "delivery_date", "orderable")} for l in q.get("lines") or []],
            "uncovered": q.get("uncovered") or [], "options": len(q.get("options") or []), "next_step": nxt}


@_tool(SubstitutesOut, title="Optional: Suggest substitutes", annotations=READ_LIVE)
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
    purpose: str | None = None,
    substitution: str | None = None,
) -> dict:
    """Good / better / best substitutes for a product that can't be ordered (or for any product).

    purpose: what the buyer uses the product for ('coffee', 'recipe: lasagne', 'allergy menu'); substitution
    exact | ask | allowed. A product the buyer needs exactly (by purpose or the mandate's product_purposes) has no
    substitutes; for 'ask', any substitute needs the buyer's yes. Pass the same purpose on request_quote items.

    Same category, measure and temperature class; keeps the original's dietary claims and the mandate's
    allergen rules (never relaxed). Each option lists its compromises in plain language and has a live offer.
    Tell the buyer the compromises, then request_quote for the chosen one with substitute_for=<original id>.
    Another kind of product is never offered: with nothing acceptable, no_safe_substitute says to ask a human and
    not_offered lists what was ruled out and why. delivery_date null = the next delivery day.
    With mandate_id it is read-scoped like get_mandate (send your Authorization: Bearer header or agent_token).
    """
    return S.suggest_substitutes(agent_id, product_id, _date(delivery_date) if delivery_date else None, amount, unit,
                                 mandate_id, avoid_allergens, limit, agent_token=_agent_token(ctx, agent_token),
                                 reader=_reader(ctx, agent_token), purpose=purpose, substitution=substitution)


@_tool(OrderOut, title="Core 4/4: Place the order",
       # idempotent: a retry with the same idempotency_key returns the same order instead of ordering again.
       annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True,
                                   open_world_hint=True))
def place_order(mandate_id: str, quote_id: str, agent_id: str | None = None, idempotency_key: str | None = None,
                rationale: str | None = None, agent_token: str | None = None,
                ctx: Context | None = None) -> dict:
    """Order a quote under a mandate. This is the only call that commits the buyer to a purchase.

    The mandate engine decides, not you: status ordered (silent/notify), pending_approval (a human must approve
    at approval_url; poll get_order) or refused (decision.findings say which rule fired).
    Always send an idempotency_key and reuse it on retries: the same key never orders twice (the retry returns
    the original order); reusing a key for a different quote fails with idempotency_key_reused.
    Returns the order (schema /schemas/order.json): id (PO-...), status, decision {outcome, findings}, lines,
    totals, approval_url when pending, receipt_jws (signed receipt) and, once ordered, erp (the hand-off to
    Valio Aimo's order system). In this demo erp.status is "simulated" with a SIM- confirmation_id: tell the
    buyer the order is placed with Sous and the Valio hand-off is simulated, not that Valio confirmed it.
    rationale: one sentence on why the buyer needs this order; the approver sees it next to the mandate's findings.
    agent_id: optional when your credential covers mandate_id (see my_mandates).
    agent_token: only if you can't send the Authorization: Bearer header and the mandate requires agent auth.
    """
    agent_id = _agent_for(agent_id, mandate_id, ctx, agent_token)
    return S.place_order(agent_id, mandate_id, quote_id, idempotency_key,
                         agent_token=_agent_token(ctx, agent_token), rationale=rationale)


@_tool(OrderOut, title="Optional: Order status", annotations=READ_LOCAL)
def get_order(order_id: str, agent_token: str | None = None, ctx: Context | None = None) -> dict:
    """Current status of an order (ordered, pending_approval, refused, rejected, expired, expired_cutoff) and any human decision.
    Only orders under your own mandate: send your Authorization: Bearer header (or agent_token).
    Returns the order (schema /schemas/order.json) with approval {by, approved, note, at} once a human decided,
    a fresh receipt_jws, and agent_claim (the rationale, labelled unverified). After delivery problems it carries
    amendments[] from Valio (short pick, delay, substitution at pick, cancelled line; status pending_buyer waits
    for the buyer, ask-tier for their human, not you) and credit_notes[]. erp: the hand-off to Valio Aimo's order
    system; erp.simulated / status "simulated" / SIM- id means placed with Sous, Valio hand-off simulated in this
    demo (no real order reached Valio)."""
    access.check_order(_reader(ctx, agent_token), order_id)
    return access.label_order(S.get_order(order_id))


@_tool(MyMandatesOut, title="Core 1/4: Which mandates am I acting under?", annotations=READ_LOCAL)
def my_mandates(agent_token: str | None = None, ctx: Context | None = None) -> dict:
    """Call this first. The mandates your credential (Authorization: Bearer agent token, or the OAuth sign-in of
    this connector) was issued for: mandate_id, buyer, label (e.g. hotel / hospital / school), agent_id and a
    one-line summary of the rules. Pick the mandate by the buyer the user names ("the hotel"); pass its mandate_id
    to request_quote / place_order (agent_id can then be left out). active=false: revoked, expired or its sandbox
    closed. Empty without a credential: ask the user for the agent token or a mandate_id; never guess one."""
    out = []
    labels = {}
    idx = sandbox.index()
    for mid in sorted(_own_mandates(ctx, agent_token)):
        m = store.get_mandate(mid)
        if not m:
            continue
        sid = idx["mandates"].get(mid)
        if sid and sid not in labels:
            row = sandbox._row(sid)
            labels[sid] = {v: k for k, v in (row["data"]["mandates"] if row else {}).items()}
        mandate = S._mandate(mid)  # the signed mandate
        out.append({"mandate_id": mid, "buyer": m["buyer"], "label": labels.get(sid, {}).get(mid) if sid else None,
                    "agent_id": m["agent_id"], "sandbox": sid, "active": oauth._mandate_live(mid),
                    "valid_until": str(m["valid_until"]) if m.get("valid_until") else None, "require_agent_auth": bool(m.get("require_agent_auth")),
                    "rules": "; ".join(r["text"] for r in rules.mandate_summary(mandate)),
                    "remembered": [{k: r.get(k) for k in ("id", "rule", "product_id", "original_product_id", "name",
                                                          "by", "expires_at")} for r in remembered.list_for(mid)]})
    note = ("Pick the mandate whose buyer the user means; pass its mandate_id. Read the full rules with get_mandate."
            if out else "No credential: connect with 'Authorization: Bearer <agent token>' (or sign in to the "
                        "connector with it). Ask the user; don't guess a mandate_id.")
    return {"mandates": out, "note": note}


@_tool(MandateOut, title="Optional: Read the mandate", annotations=READ_LOCAL)
def get_mandate(mandate_id: str, agent_token: str | None = None, ctx: Context | None = None) -> dict:
    """The mandate you act under (spend limits, allergen and claim rules, what needs a human), with its signature.
    Only your own mandate (send your Authorization: Bearer header or agent_token) or a public demo mandate.
    Returns {mandate (schema /schemas/mandate.json), jws (sous-mandate+jws), issuer, credential?, revocation?}."""
    access.check_mandate(_reader(ctx, agent_token), mandate_id)
    return {**S.get_mandate(mandate_id),
            "remembered": [remembered.summary(r) for r in remembered.list_for(mandate_id)]}


@_tool(SuggestionsOut, title="Optional: Valio Aimo's suggestions (sponsored)", annotations=READ_LIVE)
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


@_tool(OffersOut, title="Optional: Valio Aimo's offers (sponsored)", annotations=READ_LOCAL)
def list_offers(mandate_id: str, agent_token: str | None = None, ctx: Context | None = None) -> dict:
    """Valio Aimo's active COMMERCIAL offers made to your buyer: signed (jws, typ sous-offer+jws) prices below the
    current one for a reason (near best-before, seasonal, a replacement for a recalled product, a new product),
    with a quantity cap and valid_until. Products the mandate rules out (avoided allergens, categories, claims)
    were never offered. Nothing is ordered: present each to the buyer as "Valio Aimo offers...", with its
    headline and disclosure, and only if the buyer wants it call request_quote with offer_id as how_to_accept
    says; the mandate decides as always (a first-time product may still need a human).
    Read-scoped like get_mandate: send your Authorization: Bearer header (or agent_token)."""
    access.check_mandate(_reader(ctx, agent_token), mandate_id)
    return offers.for_mandate(mandate_id)


@_tool(StandingOrderOut, title="Optional: Create a weekly standing order",
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


@_tool(RenewalOut, title="Optional: Renew a standing order (with drift check)",
       annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                   open_world_hint=True))
def renew_standing_order(standing_id: str, agent_id: str | None = None, delivery_date: str | None = None,
                         amounts: list[float] | None = None, rationale: str | None = None,
                         agent_token: str | None = None, ctx: Context | None = None) -> dict:
    """Renew this week's standing order: live quote, drift check against past renewals, then the order.

    amounts: this week's amount per item, in item order, if it differs from the usual (omit for the usual).
    The gateway compares with the median of recent renewals: quantity creep, cost or price drift, a different
    product, new Terminal or reliability issues. Any drift puts the order on hold for a human (pending_approval);
    tell the buyer drift.summary and share order.approval_url. delivery_date defaults to the next delivery day.
    agent_id: optional when your credential covers the standing order's mandate (see my_mandates).
    agent_token: only if you can't send the Authorization: Bearer header and the mandate requires agent auth.
    """
    if not agent_id:
        so = standing.get(standing_id)["standing_order"]
        agent_id = _agent_for(None, so["mandate_id"], ctx, agent_token)
    return standing.renew(standing_id, _date(delivery_date) if delivery_date else None, agent_id=agent_id,
                          amounts=amounts, agent_token=_agent_token(ctx, agent_token),
                          rationale=rationale)


@_tool(StandingDetailOut, title="Optional: Standing order, history and drift baseline", annotations=READ_LOCAL)
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
    """Every rule the gateway can fire: tiers, owner (buyer mandate / Valio / gateway), text (and text_fi, Finnish),
    mandate field.
    Decision findings carry rule_url pointing at the same entries (GET /rules/{id})."""
    return rules.document()


@mcp.resource("sous://holds", title="Valio product holds", mime_type="application/json")
def holds_resource() -> dict:
    """Products Valio Aimo has on hold right now (recall, quality hold, sales stop): they can't be ordered.
    Search cards of held products carry `held`; quotes mark them PRODUCT_HELD (rule product.held)."""
    return {"holds": [holds.public(h) for h in holds.list_active()]}


@mcp.resource("sous://stats/lost-demand", title="Valio's lost demand (last 7 days)", mime_type="application/json")
def lost_demand_resource() -> dict:
    """What buyers' agents wanted from Valio Aimo and didn't get, and why: supply problems vs buyer rules vs approval
    too late, uncovered quantities and why candidates were passed over. Totals only (no buyer names)."""
    from . import lost_demand
    return lost_demand.report(7)


@mcp.resource("sous://agent-ordering", title="Is agent ordering paused?", mime_type="application/json")
def agent_ordering_resource() -> dict:
    """Whether Valio Aimo has paused agent ordering for every buyer (and why), and how many single buyers are paused.
    While a pause covers your buyer, every order is refused with rule ordering.paused, whose message says why;
    searching and quoting still work."""
    from . import pause
    return pause.status()


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
3. can_i_order with mandate_id, one item per need, all shortlisted ids, amount in L or KG where possible: it
   answers yes / needs_a_human / no with the rules that decided and a quote_id. (request_quote gives the full
   quote, with options when part of the request can't be covered.)
4. Show the buyer: products, quantities, total ex VAT, delivery date, the answer and why, anything uncovered.
   If a line is not orderable or refused, use suggest_substitutes and re-quote with substitute_for.
5. place_order with the quote id and a fresh idempotency_key. Report the status exactly as returned:
   ordered, pending_approval (share approval_url) or refused (quote the rule).
Use only facts the tools return."""


if __name__ == "__main__":
    log.warning("MCP over stdio: local operator scope, every read is allowed "
                "(docs/PROTOCOL.md section 22). Don't expose this process to outside agents.")
    mcp.run()
