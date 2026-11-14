"""The rule register: every rule the gateway can fire, who owns it, what it does and what sets it.

Served at GET /rules (and /rules/{id}) and as the MCP resource sous://rules; every decision finding carries
`rule_url` pointing here. Three owners:

  buyer    the buyer's mandate: the buyer sets it (the mandate field is named), the gateway enforces it
  valio    Valio Aimo's own facts and policy (stock, cut-offs, sales blocks, price validity): not negotiable
  gateway  the gateway's safety rules: they protect every buyer the same way and no mandate turns them off

Kinds: a `finding` joins an order decision and moves it to a tier (silent / notify / ask / block); a
`refusal` stops the call itself with an error code and HTTP status before any decision is made.

Generated from code where the code knows the answer: tiers a buyer can choose come from the Mandate field's
type, defaults and field descriptions from the Mandate model, standing-order thresholds from standing.py.
tests/test_adopt.py scans the source for every rule id the engine emits and fails if one is missing here.
"""

from __future__ import annotations

import typing

from . import config
from .mandate import UNUSUAL_MIN_HISTORY, Mandate

OWNERS = {
    "buyer": "Buyer's mandate: set by the buyer's human, enforced by the gateway",
    "valio": "Valio Aimo: live availability, cut-off and price facts; not negotiable by a mandate",
    "gateway": "Gateway safety: the same for every buyer; no mandate field turns it off",
}
ALL_TIERS = ["silent", "notify", "ask", "block"]


def _field_tiers(field: str) -> list[str]:
    """The tiers a buyer can choose for a Tier/Literal mandate field, from its type."""
    args = typing.get_args(Mandate.model_fields[field].annotation)
    return [t for t in ALL_TIERS if t in args]


def _r(id, kind, tiers, owner, text, field=None, *, status=None, condition=None) -> dict:
    out = {"id": id, "kind": kind, "tiers": tiers, "owner": owner, "text": text, "mandate_field": field}
    if field:
        fields = field if isinstance(field, list) else [field]
        out["mandate_field_default"] = {f: Mandate.model_fields[f].default for f in fields
                                        if not Mandate.model_fields[f].is_required()
                                        and Mandate.model_fields[f].default_factory is None}
        if desc := "; ".join(d for f in fields if (d := Mandate.model_fields[f].description)):
            out["mandate_field_description"] = desc
    if status:
        out["http_status"] = status
    if condition:
        out["condition"] = condition
    return out


# Valio availability issues that block a line (quote.py Issue(..., BLOCKING)): rule availability.<code>.
_VALIO_BLOCKING = {
    "NO_PRICE": "Valio Aimo returned no price for the product, so it can't be ordered.",
    "NO_AVAILABILITY": "Valio Aimo returned no availability for the product.",
    "SALES_BLOCK": "Valio Aimo has sales-blocked the product.",
    "UNDELIVERABLE": "Valio Aimo can't deliver the product on any date.",
    "OUT_OF_STOCK": "Out of stock: Valio Aimo has no delivery date for it.",
    "CUTOFF_PASSED": "The order cut-off for the delivery date has passed (checked against the clock, again at "
                     "order time and at approval time).",
}
_GATEWAY_BLOCKING = {
    "UNIT_MISMATCH": "The need is in litres but the product is sold by weight (or the reverse): the gateway "
                     "won't guess a conversion.",
    "CONTENT_UNKNOWN": "The gateway can't tell how much one pack holds, so it can't compute the quantity.",
}


def _build() -> list[dict]:
    from . import standing  # lazy: standing imports service

    pct = lambda x: f"{x:.0%}"  # noqa: E731
    R = [
        # --- who is asking, and is the mandate in force
        _r("mandate.revoked", "finding", ["block"], "buyer", "The buyer revoked the mandate: nothing more is "
           "ordered under it, and held orders can no longer be approved into orders.", "revoked"),
        _r("mandate.expired", "finding", ["block"], "buyer", "The mandate's validity has ended.", "valid_until"),
        _r("mandate.agent", "finding", ["block"], "buyer",
           "Only the agent the mandate was issued to may act under it.", "agent_id"),
        _r("mandate.ok", "finding", ["silent", "notify"], "buyer",
           "Every check passed: the order goes through (with an FYI to the buyer if a notify rule fired).", None),
        _r("order.empty", "finding", ["block"], "gateway", "Nothing orderable was found for the request.", None),
        # --- money
        _r("spend.hard_limit", "finding", ["block"], "buyer",
           "An order above the hard limit (ex VAT) is refused outright.", "hard_limit_eur"),
        _r("spend.auto_limit", "finding", ["ask"], "buyer",
           "An order above what the agent may spend alone waits for a human.", "auto_approve_limit_eur"),
        _r("spend.notify", "finding", ["notify"], "buyer",
           "An order above this amount goes through and the buyer gets an FYI.", "notify_above_eur"),
        _r("spend.daily_limit", "finding", ["ask"], "buyer",
           "Today's ordered total plus this order may not pass the daily limit without a human. Splitting a "
           "large order into small ones (a slice attack) doesn't get around it: the order that crosses the "
           "limit waits for approval.", "daily_limit_eur"),
        _r("quantity.unusual", "finding", ["ask"], "buyer",
           f"An order far above the buyer's usual amount of the same kind of product (more than the factor × "
           f"the median of their past orders in the same unit, needing at least {UNUSUAL_MIN_HISTORY} past orders) "
           "waits for a human: it catches an extra zero or packs read as litres.", "unusual_quantity_factor"),
        _r("price.comparison_cap", "finding", ["ask"], "buyer",
           "A product above the buyer's cap on comparison price (e.g. € per litre) waits for a human.",
           "max_comparison_price_eur"),
        # --- what is bought
        _r("product.category", "finding", ["block"], "buyer",
           "The product's category is outside the categories the mandate allows.", "allowed_categories"),
        _r("product.claims", "finding", ["block"], "buyer",
           "The product lacks a claim the mandate requires (e.g. lactose-free).", "required_claims"),
        _r("allergen.contains", "finding", ["block"], "buyer",
           "The product contains an allergen the mandate avoids.", "avoid_allergens"),
        _r("allergen.may_contain", "finding", ["block"], "buyer",
           "The product may contain (traces of) an avoided allergen, and the mandate doesn't allow that.",
           ["avoid_allergens", "allow_may_contain"]),
        _r("allergen.unknown", "finding", _field_tiers("unknown_allergens"), "buyer",
           "The product has no allergen data, so it can't be confirmed free of an avoided allergen. Unknown "
           "is never treated as safe.", "unknown_allergens"),
        _r("substitution", "finding", _field_tiers("substitutions"), "buyer",
           "A line replaces the product the buyer asked for; the mandate says how much say the human gets.",
           "substitutions"),
        # --- supply
        _r("supply.terminal", "finding", _field_tiers("terminal_products"), "buyer",
           "The product ships from the supplier's warehouse (Terminal), so its stock is unknown.",
           "terminal_products"),
        _r("supply.reliability", "finding", ["ask"], "buyer",
           "The product's delivered-in-full record is below the mandate's minimum.", "min_delivery_reliability"),
        _r("delivery.later_date", "finding", _field_tiers("later_delivery"), "buyer",
           "Valio Aimo can deliver only after the requested date.", "later_delivery"),
        _r("need.partial", "finding", ["ask"], "gateway",
           "Only part of the need can be covered (stock or pack sizes): never silently short-shipped.", None),
        _r("availability.partial_stock", "finding", ["ask"], "valio",
           "Valio Aimo has less in stock than the quantity needed.", None),
        _r("price.expires", "finding", ["ask"], "valio",
           "Valio Aimo's price is valid only until before the delivery date.", None),
        _r("availability.data_stale", "finding", ["ask"], "gateway",
           "Valio Aimo couldn't be reached, so the quote uses last-known price or stock: a human decides.", None),
        _r("product.type_mismatch", "finding", ["ask"], "gateway",
           "Only another kind of product than the one asked for can be delivered (coffee milk for milk): it is "
           "quoted, but a human confirms it.", None),
    ]
    R += [_r(f"availability.{c.lower()}", "finding", ["block"], "valio", t) for c, t in _VALIO_BLOCKING.items()]
    R += [_r(f"availability.{c.lower()}", "finding", ["block"], "gateway", t) for c, t in _GATEWAY_BLOCKING.items()]
    # --- standing-order drift (standing.py): each holds the renewal for a human
    n = standing.HISTORY_WEEKS
    R += [
        _r("standing.quantity_creep", "finding", ["ask"], "gateway",
           f"A standing-order item's amount is more than {pct(standing.QTY_CREEP)} above the median of the last "
           f"{n} renewals.", None, condition="standing-order renewal"),
        _r("standing.quantity_trend", "finding", ["ask"], "gateway",
           f"A standing-order item has grown {standing.TREND_RUN} renewals in a row.", None,
           condition="standing-order renewal"),
        _r("standing.price_drift", "finding", ["ask"], "gateway",
           f"An item's price per unit is more than {pct(standing.COST_DRIFT)} above the median of the last {n} "
           "renewals.", None, condition="standing-order renewal"),
        _r("standing.cost_drift", "finding", ["ask"], "gateway",
           f"The renewal's total is more than {pct(standing.COST_DRIFT)} above the median of the last {n} "
           "renewals.", None, condition="standing-order renewal"),
        _r("standing.product_switched", "finding", ["ask"], "gateway",
           "The gateway picked a different product than last time.", None, condition="standing-order renewal"),
        _r("standing.new_terminal", "finding", ["ask"], "gateway",
           "An item now ships from the supplier's warehouse (stock unknown); last time it didn't.", None,
           condition="standing-order renewal"),
        _r("standing.new_reliability", "finding", ["ask"], "gateway",
           "An item's product now has a poor delivery record; last time's didn't.", None,
           condition="standing-order renewal"),
    ]
    # --- refusals: the call is stopped before any decision
    R += [
        _r("agent_auth_required", "refusal", ["block"], "buyer", "The mandate requires the agent to prove who it "
           "is (Authorization: Bearer <agent token>); a call without the token is refused and audited "
           "(agent.auth_failed).", "require_agent_auth", status=401),
        _r("agent_auth_invalid", "refusal", ["block"], "buyer", "The agent's token is wrong, revoked or for another "
           "mandate; refused and audited (agent.auth_failed).", "require_agent_auth", status=403),
        _r("agent_cannot_approve", "refusal", ["block"], "gateway", "An agent credential was presented to approve "
           "or reject an order. Only a human decides held orders; the attempt is refused and audited "
           "(agent.approval_attempt_refused). The MCP server has no approve tool at all.", None, status=403),
        _r("quote_used", "refusal", ["block"], "gateway", "A quote can be ordered once: a second order on it (a "
           "retry with a new key, or many agents racing) is refused. Concurrent orders are serialised.",
           None, status=409),
        _r("quote_expired", "refusal", ["block"], "gateway",
           "The quote's price and availability are too old to order on; ask for a new quote.", None, status=409),
        _r("quote_mismatch", "refusal", ["block"], "gateway",
           "The quote was issued to another agent or mandate.", None, status=409),
        _r("quote_signature", "refusal", ["block"], "gateway",
           "The stored quote doesn't match its signature (tampered or corrupted).", None, status=409),
        _r("mandate_signature", "refusal", ["block"], "gateway",
           "The mandate (or the buyer's credential behind it) doesn't verify.", None, status=409),
        _r("idempotency_key_reused", "refusal", ["block"], "gateway",
           "The agent already used this idempotency key for a different order; a same-key retry returns the "
           "original order. Keys are scoped per agent.",
           None, status=409),
        _r("too_many_candidates", "refusal", ["block"], "gateway",
           f"At most {config.MAX_CANDIDATES_PER_QUOTE} candidate products per quote.", None, status=400),
        _r("sandbox_limit", "refusal", ["block"], "gateway",
           "A judge sandbox has used its share of the live Valio key.", None, status=429),
        _r("sandbox_expired", "refusal", ["block"], "gateway",
           "The judge sandbox has expired or was closed.", None, status=410),
    ]
    return R


def register() -> list[dict]:
    base = config.PUBLIC_BASE_URL
    return [{**r, "url": f"{base}/rules/{r['id']}"} for r in _build()]


def get(rule_id: str) -> dict | None:
    return next((r for r in register() if r["id"] == rule_id), None)


def document() -> dict:
    rules = register()
    return {"owners": OWNERS, "tiers": {
                "silent": "ordered, logged", "notify": "ordered, the buyer gets an FYI",
                "ask": "held until a human approves (pending_approval)", "block": "refused"},
            "kinds": {"finding": "joins the order decision; the highest tier among findings wins",
                      "refusal": "stops the call with an error code before any decision"},
            "count": len(rules), "rules": rules}
