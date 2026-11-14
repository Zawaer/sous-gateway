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

from . import catalog, config, deadline, fi, origin as origins, purpose as purposes, reservations
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
    out = {"id": id, "kind": kind, "tiers": tiers, "owner": owner, "text": text, "text_fi": fi.rule_text(id),
           "mandate_field": field}
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
    "OFFER_VAT_UNKNOWN": "A line quoted on a Valio Aimo offer has no VAT rate from Valio and no tax-inclusive price to "
                         "derive it from: the gateway never assumes 0 %, so the offer line is refused (sous/offers.py).",
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
        _r("autonomy.advise", "finding", ["ask"], "buyer",
           "The mandate lets the agent advise only: it may search and quote, and every order waits for a human.",
           "autonomy"),
        _r("autonomy.reorder", "finding", ["ask"], "buyer",
           "The mandate lets the agent reorder alone only the buyer's usual products (ordered under this mandate "
           "before, or in usual_products); any other product waits for a human.", ["autonomy", "usual_products"]),
        _r("autonomy.capped", "finding", ["ask"], "valio",
           "Valio Aimo caps this buyer's agents (or every buyer's) at a lower autonomy level than the mandate's "
           "(GET /autonomy-caps): the order waits for a human, and the finding names Valio's reason. A cap never "
           "raises autonomy or lets through what another rule holds.", None),
        _r("ordering.paused", "finding", ["block"], "valio",
           "Valio Aimo has paused agent ordering for every buyer or for this buyer (GET /agent-ordering): no "
           "order goes through and no held order can be approved until Valio Aimo resumes it. Search and "
           "quotes still work.", None),
        # --- the customer account (sous/account.py; mock customer master, SAP KNA1/KNVV + credit in production)
        _r("account.payment_block", "finding", ["block"], "valio",
           "Valio Aimo has put a payment block on the customer account (e.g. overdue invoices): no order goes "
           "through and no held order can be approved into an order until it is lifted.", "customer_number"),
        _r("account.credit_limit", "finding", ["ask"], "valio",
           "The order (incl. VAT) is more than the credit left on the customer account: the credit limit minus the "
           "open balance and the account's Sous orders still to be delivered. A human confirms it.",
           "customer_number"),
        _r("account.delivery_day", "finding", ["block"], "valio",
           "The delivery date isn't one of the customer's delivery days, or the cut-off of the customer's delivery "
           "route for that date has passed.", "customer_number"),
        _r("account.unknown", "finding", ["block"], "gateway",
           "No customer account for the mandate's buyer. Only when the gateway runs with SOUS_REQUIRE_ACCOUNT=1 "
           "(production); otherwise a buyer without an account gets no account checks.", "customer_number"),
        # --- money
        _r("spend.hard_limit", "finding", ["block"], "buyer",
           "An order above the hard limit (ex VAT) is refused outright.", "hard_limit_eur"),
        _r("spend.auto_limit", "finding", ["ask"], "buyer",
           "An order above what the agent may spend alone waits for a human.", "auto_approve_limit_eur"),
        _r("spend.notify", "finding", ["notify"], "buyer",
           "An order above this amount goes through and the buyer gets an FYI.", "notify_above_eur"),
        _r("spend.weekly_limit", "finding", ["ask"], "buyer",
           "This calendar week's ordered total (Monday to Sunday, Helsinki time) plus this order may not pass "
           "the weekly limit without a human.", "weekly_limit_eur"),
        _r("spend.split_orders", "finding", ["ask"], "buyer",
           "Orders that each stay under the auto-approve limit but together pass it within the window wait for "
           "a human: an agent can't slice one big order into small ones to avoid approval. Orders that went "
           "through in the window count; ones that waited for a human already had one.", "split_window_minutes"),
        _r("spend.daily_limit", "finding", ["ask"], "buyer",
           "Today's ordered total plus this order may not pass the daily limit without a human. Splitting a "
           "large order into small ones (a slice attack) doesn't get around it: the order that crosses the "
           "limit waits for approval.", "daily_limit_eur"),
        _r("quantity.unusual", "finding", ["ask"], "buyer",
           f"An order far above the buyer's usual amount of the same kind of product (more than the factor × "
           f"the median of their past orders in the same unit, needing at least {UNUSUAL_MIN_HISTORY} past orders) "
           "waits for a human: it catches an extra zero or packs read as litres.", "unusual_quantity_factor"),
        _r("price.changed", "finding", ["ask"], "buyer",
           "A product whose unit price (ex VAT) differs by more than the mandate's percentage, up or down, from the "
           "last price paid for it under this mandate waits for a human: a price rise, or a different pack read as "
           "the usual one.", "max_price_change_pct"),
        _r("price.comparison_changed", "finding", ["ask"], "buyer",
           "A product priced per litre or kilo more than the mandate's percentage above what the buyer last paid per "
           "litre or kilo for the same kind of product (a pricier brand, pack or variant than usual) waits for a human. "
           "The same product's own price is checked by price.changed.", "max_price_change_pct"),
        _r("price.comparison_cap", "finding", ["ask"], "buyer",
           "A product priced per litre or kilo above the buyer's cap for that kind of product waits for a human. "
           "Caps can be scoped to a product type, family or catalog category (e.g. milk at €2.50/L), so cream is "
           "not held by a milk cap; the most specific cap wins. A cap per unit alone (e.g. €2.50 for every product "
           "sold by the litre) applies to every product without a scoped cap.",
           ["comparison_price_caps", "max_comparison_price_eur"]),
        # --- what is bought
        _r("product.first_time", "finding", ["notify", "ask", "block"], "buyer",
           "A product never ordered under this mandate before, and not in the buyer's usual_products, gets the tier the buyer chose (FYI, approval or "
           "refusal), so a new product reaches the kitchen only when a human wants it to.", ["first_time_products", "usual_products"]),
        _r("product.category", "finding", ["block"], "buyer",
           "The product's category is outside the categories the mandate allows.", "allowed_categories"),
        _r("origin.restricted", "finding", ["ask", "block"], "buyer",
           "The product's country of origin is outside the countries the mandate allows, or is one it avoids. The "
           "country is the supplier-declared countryOfOrigin of Valio Aimo's product master, not independently "
           "verified. The buyer chooses whether a human decides or the order is refused.",
           ["allowed_origins", "avoid_origins", "origin_violation"]),
        _r("origin.unknown", "finding", ["notify", "ask", "block"], "buyer",
           "The product master gives no usable country of origin (empty or a placeholder code) while the mandate "
           "restricts origin: unknown is never treated as allowed.", ["allowed_origins", "avoid_origins", "origin_unknown"]),
        _r("product.claims", "finding", ["block"], "buyer",
           "The product lacks a claim the mandate requires (e.g. lactose-free).", "required_claims"),
        _r("allergen.contains", "finding", ["block"], "buyer",
           "The product contains an allergen the mandate avoids.", "avoid_allergens"),
        _r("allergen.may_contain", "finding", ["block"], "buyer",
           "The product may contain (traces of) an avoided allergen, and the mandate doesn't allow that.",
           ["avoid_allergens", "allow_may_contain"]),
        _r("allergen.likely", "finding", ["block"], "gateway",
           "The product declares no allergens, but the Gemini Decisions API (gemini-decision-1), asked one yes/no "
           "question per avoided allergen from the product's names, brand and key ingredients, puts one at 80 % or "
           "more: refused, and a human can't approve it into an order. Below 80 % the product stays "
           "allergen.unknown; a model answer never makes a product safe.", "avoid_allergens"),
        _r("allergen.unknown", "finding", _field_tiers("unknown_allergens"), "buyer",
           "The product has no allergen data, so it can't be confirmed free of an avoided allergen. Unknown "
           "is never treated as safe.", "unknown_allergens"),
        _r("allergen.cleared", "finding", ["notify"], "buyer",
           "The product has no allergen data, but a human of this buyer already confirmed it free of everything the "
           "mandate avoids when approving an earlier order and asked the gateway to remember it (sous/clearance.py: "
           "one mandate and product, voided when the product data or the avoided allergens change, expires after "
           "90 days, revocable). Ordered, and the buyer is told whose clearance was used.", "avoid_allergens"),
        # --- approve and remember (sous/remembered.py): a human's earlier answer to the same soft hold
        _r("remembered.substitution", "finding", ["notify"], "buyer",
           "The line is the same substitute for the same original product that a human of this buyer approved "
           "earlier and asked the gateway to remember: notify instead of ask. Per mandate and product pair, voided "
           "when either product's data changes, above its optional quantity or amount cap, after it expires (30 "
           "days by default, at most 90) or when revoked (DELETE /mandates/{id}/remembered/{rid}). A mandate whose "
           "substitutions is block is never softened.", "substitutions"),
        _r("remembered.first_time", "finding", ["notify"], "buyer",
           "A product never ordered under the mandate that a human of this buyer already approved and asked the "
           "gateway to remember: notify instead of ask, under the same limits as remembered.substitution. "
           "first_time_products: block is never softened.", "first_time_products"),
        _r("remembered.unit_interpreted", "finding", ["notify"], "buyer",
           "The gateway read the need's unit the same way (20 ST as KI) for the same product that a human of this "
           "buyer approved and asked it to remember: notify instead of ask. A different reading, changed product "
           "units, a quantity or amount over the cap, expiry or revocation ask again.", None),
        _r("remembered.type_mismatch", "finding", ["notify"], "buyer",
           "The same other kind of product (oat drink for milk) that a human of this buyer approved and asked the "
           "gateway to remember: notify instead of ask, under the same limits as remembered.unit_interpreted.", None),
        _r("purpose.exact", "finding", ["block"], "buyer",
           "A substitute for a product the buyer uses where only that exact product works (e.g. an allergy recipe): "
           "refused, and Valio Aimo proposes no alternative for it. Set per product in the mandate (product_purposes) or "
           "per item (purpose / substitution = exact); an agent can only make an item stricter.",
           "product_purposes"),
        _r("purpose.ask", "finding", ["ask"], "buyer",
           "A substitute for a product the buyer uses in a recipe (whipping cream may not whip the same): the buyer "
           "decides, whatever the substitutions tier says. Coffee cream, by contrast, can be marked allowed.",
           "product_purposes"),
        _r("substitution", "finding", _field_tiers("substitutions"), "buyer",
           "A line replaces the product the buyer asked for; the mandate says how much say the human gets.",
           "substitutions"),
        _r("product.held", "finding", ["block"], "valio",
           "Valio Aimo has put the product on hold (recall, quality hold or sales stop; GET /holds): it can't be "
           "quoted as orderable, ordered, or approved into an order, also on a quote issued before the hold.", None),
        # --- post-order amendments (sous/amendments.py): Valio changes an ordered order after the fact
        _r("amendment.short_pick", "finding", ["notify"], "valio",
           "Valio Aimo picked fewer units than ordered: applied at once, the total drops and the re-signed receipt "
           "carries a credit note line. The buyer is told.", None, condition="post-order amendment"),
        _r("amendment.line_cancelled", "finding", ["notify"], "valio",
           "Valio Aimo can't deliver a line at all: it is cancelled with a credit note line. The buyer is told.",
           None, condition="post-order amendment"),
        _r("amendment.delay", "finding", ["notify"], "buyer",
           "Valio Aimo moves the delivery to a later date and the mandate's later_delivery is notify: applied, "
           "the buyer is told.", "later_delivery", condition="post-order amendment"),
        _r("amendment.delay_beyond_mandate", "finding", ["ask", "block"], "buyer",
           "Valio Aimo moves the delivery to a later date and the mandate's later_delivery is ask or block: the "
           "buyer's human must accept it (block: nobody can accept until the mandate allows it); rejecting cancels "
           "the line(s) with a credit.", "later_delivery", condition="post-order amendment"),
        _r("amendment.substitution", "finding", ["notify"], "buyer",
           "Valio Aimo picked another product and the mandate, checked on the substitute line, lets it through: "
           "applied, the buyer is told.", "substitutions", condition="post-order amendment"),
        _r("amendment.substitution_not_allowed", "finding", ["ask", "block"], "buyer",
           "Valio Aimo picked another product that the mandate doesn't let through on its own (substitutions tier, "
           "allergens, claims, category, holds ... re-checked on the substitute line): it waits for the buyer; "
           "the agent can't accept it, a block can't be accepted at all. Rejecting cancels the line with a credit.",
           "substitutions", condition="post-order amendment"),
        _r("amendment.price_increase", "finding", ["ask"], "gateway",
           "A substitute picked by Valio Aimo would cost more than the line ordered: a human decides.", None,
           condition="post-order amendment"),
        # --- Valio Aimo offers (sous/offers.py)
        _r("offer.applied", "finding", ["notify"], "gateway",
           "A line is priced on a Valio Aimo offer (request_quote with offer_id): the agent took a seller's "
           "commercial offer, so the buyer is told. Every other rule still applies; an offer never bypasses limits, "
           "allergens, categories or first-time products.", None),
        _r("offer.expired", "finding", ["block"], "valio",
           "The Valio Aimo offer a quote was priced on has expired or was withdrawn before the order was placed or "
           "approved: ask for a new quote.", None),
        _r("offer.signature_invalid", "finding", ["block"], "gateway",
           "The stored Valio Aimo offer a quote was priced on no longer matches its signed JWS (tampered or "
           "corrupted): the order is refused.", None),
        _r("offer.over_cap", "finding", ["block"], "valio",
           "Orders on the Valio Aimo offer would exceed its quantity cap (all buyers together; orders placed or "
           "waiting for approval count).", None),
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
        _r("availability.allocated", "finding", ["block"], "valio",
           "Valio Aimo's stock for the product and delivery date is already reserved by orders placed or held "
           "through Sous (sous/reservations.py): the order asks for more than is left, so it is refused and its "
           "options offer what's left. Under SOUS_ALLOCATION=pro_rata (set by Valio, not by a mandate), when demand "
           "exceeds stock each buyer is capped at a share in proportion to their last 4 weeks' orders.", None,
           condition=f"SOUS_ALLOCATION={reservations.mode()}"),
        _r("price.expires", "finding", ["ask"], "valio",
           "Valio Aimo's price is valid only until before the delivery date.", None),
        _r("availability.data_stale", "finding", ["ask"], "gateway",
           "Valio Aimo couldn't be reached, so the quote uses last-known price or stock: a human decides.", None),
        _r("product.type_mismatch", "finding", ["ask"], "gateway",
           "Only another kind of product than the one asked for can be delivered (coffee milk for milk): it is "
           "quoted, but a human confirms it.", None),
        _r("unit.interpreted", "finding", ["ask"], "gateway",
           "The need's unit isn't one of the product's units (20 ST of a product sold per KI with no single piece "
           "unit), so it was read as sales units: the line says so in unit_note and a human confirms the "
           "quantity. A unit the product defines, or ST on its one piece unit (PRK, BOT, ...), stays silent. Also "
           "fires when kg of a liquid (40 kg of cream) is read as litres, or the reverse, at the product type's "
           "typical density.", None),
        _r("quantity.pack_oversize", "finding", ["notify"], "gateway",
           f"The planned packs exceed a need in L or KG by more than {config.PACK_OVERSIZE_PCT:g}% (12 kg -> one "
           f"20 kg bag, +67%) and no plan with less surplus costs at most {config.PACK_OVERSIZE_PRICE_PCT:g}% more: "
           "the order goes through and the buyer is told.", None),
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
        _r("passkey_required", "refusal", ["block"], "buyer", "The mandate requires passkey approval: a held order "
           "can only be approved with a registered approver's passkey, signed over this exact order (id, quote, "
           "total, lines, approve_by). The shared approver code can still reject it.", "approval_method",
           status=403),
        _r("passkey_invalid", "refusal", ["block"], "gateway", "A passkey approval that doesn't verify: wrong "
           "signature, a challenge issued for another order or action, a challenge already used (replay), an "
           "expired challenge, an order that changed since the challenge, or a passkey not registered for this "
           "order's mandate or buyer.", None, status=403),
        _r("passkey_not_registered", "refusal", ["block"], "gateway", "Passkey approval was asked for an order "
           "whose mandate and buyer have no registered passkey; register one first (POST "
           "/approvers/register/options, with the approver code or the sandbox's approver token).", None,
           status=409),
        _r("agent_cannot_accept", "refusal", ["block"], "gateway", "The buyer's agent tried to accept a Valio "
           "amendment that the mandate, re-checked at that moment, would not let through without a human (ask or "
           "block tier). Refused and audited (agent.amendment_accept_refused); the agent may still reject it.",
           None, status=403),
        _r("amendment_blocked", "refusal", ["block"], "buyer", "Accepting a Valio amendment that the mandate as it "
           "stands blocks (e.g. a substitute with an avoided allergen): nobody can accept it; reject it, or change "
           "the mandate first.", None, status=409),
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
        _r("offer_not_found", "refusal", ["block"], "gateway", "No such offer for this mandate (an offer made for "
           "another buyer answers the same).", None, status=404),
        _r("offer_expired", "refusal", ["block"], "valio", "The offer has expired, was withdrawn or is sold out.",
           None, status=409),
        _r("offer_not_applicable", "refusal", ["block"], "gateway", "None of the offer's products is an orderable "
           "line of the quote.", None, status=422),
        _r("offer_signature_invalid", "refusal", ["block"], "gateway", "The stored offer differs from what Valio "
           "Aimo signed (prices, cap, validity, reason or targets): it can't be quoted. Terms are only ever taken from "
           "the verified JWS.", None, status=409),
        _r("offer_over_cap", "refusal", ["block"], "valio", "The quote asks for more of the offer than is left "
           "under its quantity cap.", None, status=409),
        _r("offer_requires_mandate", "refusal", ["block"], "gateway", "An offer is quoted only under the mandate it "
           "was made for (pass mandate_id).", None, status=422),
        _r("offer_above_list", "refusal", ["block"], "gateway", "Valio side: an offer price above Valio's current "
           "price for the product is refused; an offer may only lower a price.", None, status=422),
        _r("offer_no_targets", "refusal", ["block"], "gateway", "Valio side: no target mandate is left after the "
           "guardrails (mandate rules such as avoided allergens, or the limit of active offers per buyer).", None,
           status=422),
        _r("too_many_candidates", "refusal", ["block"], "gateway",
           f"At most {config.MAX_CANDIDATES_PER_QUOTE} candidate products per quote.", None, status=400),
        _r("sandbox_limit", "refusal", ["block"], "gateway",
           "A judge sandbox has used its share of the live Valio key.", None, status=429),
        _r("sandbox_expired", "refusal", ["block"], "gateway",
           "The judge sandbox has expired or was closed.", None, status=410),
        _r("approval_deadline_passed", "refusal", ["block"], "gateway",
           "A held order can be approved only until its approve_by: the earliest order cut-off among its lines "
           f"minus {deadline.MARGIN_MIN:g} minutes (or the start of the delivery day), or the mandate's or sandbox's expiry if that comes "
           "first (approve_by_detail.basis says which). After that it is "
           "expired_cutoff, nothing is ordered, and the buyer is told the next delivery date to ask for.",
           None, status=409),
    ]
    return R


_TIER_SAYS = {"silent": "go through", "notify": "go through with an FYI", "ask": "need a human",
              "block": "are refused"}


def mandate_summary(m: Mandate) -> list[dict]:
    """Every active rule of a mandate in plain language, one {rule, text} per rule, most important first.
    Built from the mandate's fields (an unset rule is left out), so a summary never hides a rule that can fire."""
    from . import catalog
    out: list[dict] = []

    seen: dict = {}

    def add(rule, text):
        n = seen[rule] = seen.get(rule, -1) + 1
        out.append({"rule": rule, "text": text, "text_fi": fi.summary_text(m, rule, n) or text})

    def eur(x):
        return f"€{x:,.2f}".replace(".00", "")

    if m.notify_above_eur is not None:
        add("spend.notify", f"FYI above {eur(m.notify_above_eur)} an order")
    add("spend.auto_limit", f"auto-approve up to {eur(m.auto_approve_limit_eur)} an order, a human approves above")
    if m.hard_limit_eur is not None:
        add("spend.hard_limit", f"refused above {eur(m.hard_limit_eur)} an order")
    if m.daily_limit_eur is not None:
        add("spend.daily_limit", f"a human approves past {eur(m.daily_limit_eur)} a day")
    if m.weekly_limit_eur is not None:
        add("spend.weekly_limit", f"a human approves past {eur(m.weekly_limit_eur)} a week")
    if m.split_window_minutes:
        add("spend.split_orders", f"small orders within {m.split_window_minutes} min are added up against the "
                                  "auto-approve limit")
    caps = [f"{k} at {eur(v)} per L or kg" for k, v in m.comparison_price_caps.items()]
    caps += [f"anything else sold per {u} at {eur(v)}/{u}" if m.comparison_price_caps
             else f"everything sold per {u} at {eur(v)}/{u}" for u, v in m.max_comparison_price_eur.items()]
    if caps:
        add("price.comparison_cap", "price cap: " + ", ".join(caps) + ", a human approves above")
    if m.max_price_change_pct:
        add("price.changed", f"a price more than {m.max_price_change_pct:g}% off the last one paid needs a human")
    if m.unusual_quantity_factor:
        add("quantity.unusual", f"more than {m.unusual_quantity_factor:g}× the usual quantity needs a human")
    if m.avoid_allergens:
        add("allergen.contains", "avoids " + ", ".join(catalog.ALLERGENS.get(a, a) for a in m.avoid_allergens)
            + ("" if m.allow_may_contain else " (may-contain too)")
            + f"; no allergen data: {'refused' if m.unknown_allergens == 'block' else 'a human decides'}")
    if m.required_claims:
        add("product.claims", "only products claiming " + ", ".join(
            c.replace("_", " ").lower() for c in m.required_claims))
    if m.allowed_origins:
        add("origin.restricted", f"only goods from {origins.label(m.allowed_origins)}; "
            f"{'refused' if m.origin_violation == 'block' else 'a human decides'} otherwise (supplier-declared)")
    if m.avoid_origins:
        add("origin.restricted", f"no goods from {origins.label(m.avoid_origins)}")
    if m.allowed_origins or m.avoid_origins:
        add("origin.unknown", f"unknown origin {_TIER_SAYS[m.origin_unknown]}")
    if m.allowed_categories is not None:
        add("product.category", "only categories " + ", ".join(m.allowed_categories))
    if m.first_time_products:
        known = f" (beyond {len(m.usual_products)} usual products)" if m.usual_products else ""
        add("product.first_time", f"new products{known} {_TIER_SAYS[m.first_time_products]}")
    add("substitution", f"substitutes {_TIER_SAYS[m.substitutions]}")
    for pid, pp in (m.product_purposes or {}).items():
        lvl = purposes.resolve(pp.purpose, pp.substitution)
        if lvl in ("exact", "ask"):
            add(f"purpose.{lvl}", f"{(catalog.get(pid) or {}).get('name') or pid} ({pp.purpose}): {purposes.LABEL[lvl]}")
    if m.terminal_products != "silent":
        add("supply.terminal", f"supplier-shipped products {_TIER_SAYS[m.terminal_products]}")
    if m.min_delivery_reliability is not None:
        add("supply.reliability", f"products delivered in full under {m.min_delivery_reliability:.0%} of the time "
                                  "need a human")
    add("delivery.later_date", f"deliveries later than asked {_TIER_SAYS[m.later_delivery]}")
    if days := getattr(m, "delivery_days", None):  # if a mandate field for delivery days is added
        add(None, "delivery only on " + ", ".join(map(str, days)))
    if m.require_agent_auth:
        add("agent_auth_required", "the agent must present its token")
    if m.valid_until:
        add("mandate.expired", f"valid until {m.valid_until:%d.%m.%Y %H:%M}")
    return out


def _lang(r: dict, lang: str | None) -> dict:
    """lang='fi': `text` is Finnish and the English moves to text_en; otherwise unchanged (English, plus text_fi)."""
    return {**r, "text": r["text_fi"], "text_en": r["text"]} if lang == "fi" and r.get("text_fi") else r


def register(lang: str | None = None) -> list[dict]:
    base = config.PUBLIC_BASE_URL
    return [_lang({**r, "url": f"{base}/rules/{r['id']}"}, lang) for r in _build()]


def get(rule_id: str, lang: str | None = None) -> dict | None:
    return next((r for r in register(lang) if r["id"] == rule_id), None)


def document(lang: str | None = None) -> dict:
    rules = register(lang)
    if lang == "fi":
        return {"lang": "fi", "owners": fi.OWNERS, "tiers": fi.TIERS, "kinds": fi.KINDS, "count": len(rules),
                "rules": rules}
    return {"owners": OWNERS, "tiers": {
                "silent": "ordered, logged", "notify": "ordered, the buyer gets an FYI",
                "ask": "held until a human approves (pending_approval)", "block": "refused"},
            "kinds": {"finding": "joins the order decision; the highest tier among findings wins",
                      "refusal": "stops the call with an error code before any decision"},
            "count": len(rules), "rules": rules}
