"""The mandate (sous) and the deterministic rules engine that enforces it.

A mandate is what a human gives their purchasing agent: spend limits, allergen
and product requirements, and what the agent may decide alone. The engine checks
a quote against it and returns one of four tiers (silent / notify / ask / block), each with the rules that
fired, in plain language. No LLM is involved in this decision.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from . import catalog, config, holds

# Four tiers, from least to most human involvement:
#   silent: routine, the order goes through and is logged
#   notify: the order goes through and the human gets an FYI
#   ask:    held until a human approves (yes/no, with a deadline)
#   block:  never automatic; refused with the rule that fired
Tier = Literal["silent", "notify", "ask", "block"]


class Mandate(BaseModel):
    id: str
    buyer: str = Field(description="Organisation the agent buys for")
    agent_id: str = Field(description="The agent this mandate is issued to")
    issued_by: str = Field(description="The human who issued the mandate")
    valid_until: datetime | None = None
    revoked: bool = False
    require_agent_auth: bool = Field(False, description="Quotes and orders need the agent's bearer credential")
    notify_above_eur: float | None = Field(None, description="Orders above this (ex VAT) go through but the human gets an FYI")
    auto_approve_limit_eur: float = Field(500.0, description="Orders up to this (ex VAT) need no human")
    hard_limit_eur: float | None = Field(5000.0, description="Orders above this (ex VAT) are refused outright")
    daily_limit_eur: float | None = Field(None, description="Total per day before a human must approve")
    allowed_categories: list[str] | None = Field(None, description="Category codes; None = any")
    avoid_allergens: list[str] = Field(default_factory=list, description="GS1 allergen codes, e.g. AM (milk), AN (nuts)")
    allow_may_contain: bool = False
    unknown_allergens: Literal["ask", "block"] = "ask"  # unknown is never treated as safe
    required_claims: list[str] = Field(default_factory=list, description="e.g. FREE_FROM_LACTOSE")
    substitutions: Tier = "ask"
    terminal_products: Tier = "silent"
    min_delivery_reliability: float | None = 0.9
    later_delivery: Literal["notify", "ask", "block"] = "ask"
    max_comparison_price_eur: dict[str, float] = Field(default_factory=dict, description="e.g. {'L': 2.5}")
    demo_public: bool = Field(False, description="Curated demo data: readable without credentials (see sous/access.py). "
                              "Real buyers' mandates leave this off.")
    unusual_quantity_factor: float | None = Field(
        3.0, gt=1, description="Ask when a product family's quantity is more than this many times the median of the "
        "buyer's past orders of it (at least 3 past orders); None = off")
    sandbox: str | None = Field(None, description="Judge sandbox id (e.g. SBX-7Q2K) for a visitor's copy; see sous/sandbox.py")

    @field_validator("valid_until")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        """A valid_until without an offset is Helsinki local time (comparing naive and aware datetimes fails)."""
        return v.replace(tzinfo=config.TZ) if v is not None and v.tzinfo is None else v


class Finding(BaseModel):
    rule: str
    outcome: Tier
    message: str
    product_id: str | None = None
    rule_url: str | None = Field(None, description="This rule in the rule register (GET /rules/{rule})")

    def model_post_init(self, _ctx) -> None:
        if self.rule_url is None:
            self.rule_url = f"{config.PUBLIC_BASE_URL}/rules/{self.rule}"


# Fewer past orders than this and there is no "usual" quantity to compare with.
UNUSUAL_MIN_HISTORY = 3


_RANK = {"silent": 0, "notify": 1, "ask": 2, "block": 3}


def readable_name(name: str) -> str:
    """Catalog names are often ALL CAPS ('BRAVO CHEF 400G CHOPPED TOMATOES'); reasons read better in sentence case."""
    letters = [c for c in name or "" if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.8:
        return name[:1].upper() + name[1:].lower()
    return name


def evaluate(
    mandate: Mandate,
    agent_id: str,
    lines: list[dict],
    *,
    requested_date: date,
    substitute_for: dict[str, str] | None = None,
    uncovered: float | dict[str, float] = 0.0,
    need_unit: str | None = None,
    spent_today_eur: float = 0.0,
    now: datetime,
    extra_findings: list[Finding] | None = None,
    quantities: dict[tuple[str, str], dict] | None = None,
    usual_quantities: dict[tuple[str, str], list[float]] | None = None,
) -> tuple[Tier, list[Finding]]:
    """`extra_findings` are checks made outside the mandate (e.g. standing-order drift); they count like any rule.
    `quantities` is this order's amount per (product family, unit), `usual_quantities` the same buyer's past
    ordered amounts per key (both from sous/usual.py); they feed quantity.unusual."""
    f: list[Finding] = []

    def add(rule, outcome, message, pid=None):
        f.append(Finding(rule=rule, outcome=outcome, message=message, product_id=pid))

    # Who is asking, and is the mandate still in force?
    if mandate.revoked:
        add("mandate.revoked", "block", f"Mandate {mandate.id} has been revoked by {mandate.issued_by}.")
    if mandate.valid_until and now > mandate.valid_until:
        add("mandate.expired", "block", f"Mandate {mandate.id} expired on {mandate.valid_until:%d.%m.%Y}.")
    if agent_id != mandate.agent_id:
        add("mandate.agent", "block", f"Agent '{agent_id}' is not the holder of mandate {mandate.id}.")
    if not lines:
        add("order.empty", "block", "Nothing orderable was found for this request.")

    # An unorderable line already blocks the order (availability.*); it never counts towards spend.
    total = round(sum(l.get("line_total_ex_vat") or 0 for l in lines if l.get("orderable", True)), 2)
    if mandate.hard_limit_eur is not None and total > mandate.hard_limit_eur:
        add("spend.hard_limit", "block",
            f"Order total €{total:,.2f} exceeds the mandate's hard limit of €{mandate.hard_limit_eur:,.2f}.")
    elif total > mandate.auto_approve_limit_eur:
        add("spend.auto_limit", "ask",
            f"Order total €{total:,.2f} is above the €{mandate.auto_approve_limit_eur:,.2f} the agent may spend alone.")
    elif mandate.notify_above_eur is not None and total > mandate.notify_above_eur:
        add("spend.notify", "notify",
            f"Order total €{total:,.2f} is above €{mandate.notify_above_eur:,.2f}, so the buyer gets an FYI.")
    if mandate.daily_limit_eur is not None and spent_today_eur + total > mandate.daily_limit_eur:
        add("spend.daily_limit", "ask",
            f"With this order, today's spend would be €{spent_today_eur + total:,.2f}, "
            f"above the daily limit of €{mandate.daily_limit_eur:,.2f}.")
    # `uncovered` is either one amount in `need_unit`, or {unit: amount} when items use different units.
    missing: dict[str, float] = {}
    for u, a in (uncovered if isinstance(uncovered, dict) else {need_unit: uncovered}).items():
        if a and a > 0:
            u = "units" if u in (None, "SALES_UNIT") else u
            missing[u] = round(missing.get(u, 0.0) + a, 3)
    if missing:
        add("need.partial", "ask",
            "Only part of the need can be covered: "
            f"{' and '.join(f'{a:g} {u}' for u, a in missing.items())} still missing.")

    factor = mandate.unusual_quantity_factor
    for key, cur in (quantities or {}).items() if factor else ():
        past = sorted((usual_quantities or {}).get(key) or [])
        if len(past) < UNUSUAL_MIN_HISTORY:
            continue
        n = len(past)
        med = past[n // 2] if n % 2 else (past[n // 2 - 1] + past[n // 2]) / 2
        if med > 0 and cur["amount"] > factor * med:
            add("quantity.unusual", "ask",
                f"{cur['label']}: {cur['amount']:g} {key[1]} is {round(cur['amount'] / med, 1):g}× your usual {med:g} {key[1]} "
                f"(median of your last {n} orders; the mandate asks above {factor:g}×).", cur.get("product_id"))

    subs = substitute_for or {}
    for l in lines:
        pid, name = l["product_id"], readable_name(l["name"])
        p = catalog.get(pid) or {}

        # Valio's hold on the product: also for a quote issued before the hold (ordering, approving it later)
        held = next((i for i in l.get("issues", []) if i["code"] == "PRODUCT_HELD"), None)
        if held or (hold := holds.active_for(pid)):
            add("product.held", "block", f"{name}: {held['message'] if held else holds.message(hold)}", pid)
        for issue in l.get("issues", []):
            if issue["code"] == "PRODUCT_HELD":
                continue
            if issue["severity"] == "blocking":
                add(f"availability.{issue['code'].lower()}", "block", f"{name}: {issue['message']}", pid)
            elif issue["code"] == "LATER_DATE":
                add("delivery.later_date", mandate.later_delivery, f"{name}: {issue['message']}", pid)
            elif issue["code"] == "PARTIAL_STOCK":
                add("availability.partial_stock", "ask", f"{name}: {issue['message']}", pid)
            elif issue["code"] == "PRICE_EXPIRES":
                add("price.expires", "ask", f"{name}: {issue['message']}", pid)
            elif issue["code"] == "DATA_STALE":  # never order on last-known data without a human
                add("availability.data_stale", "ask", f"{name}: {issue['message']}", pid)
            elif issue["code"] == "UNIT_INTERPRETED":  # '20 ST' of a product without ST read as sales units
                add("unit.interpreted", "ask", f"{name}: {issue['message']}", pid)
            elif issue["code"] == "TYPE_MISMATCH":  # not the kind of product asked for (coffee milk for milk)
                add("product.type_mismatch", "ask", f"{name}: {issue['message']}", pid)

        if mandate.allowed_categories is not None and p.get("category") not in mandate.allowed_categories:
            add("product.category", "block",
                f"{name} is in category {p.get('category')}, which this mandate doesn't allow.", pid)

        missing = sorted(set(mandate.required_claims) - set(l.get("claims") or []))
        if missing:
            add("product.claims", "block", f"{name} lacks required claims: {', '.join(missing)}.", pid)

        if mandate.avoid_allergens:
            st = catalog.allergen_status(p, mandate.avoid_allergens, mandate.allow_may_contain)
            names = ", ".join(catalog.ALLERGENS.get(c, c) for c in st["codes"])
            if st["status"] == "contains":
                add("allergen.contains", "block", f"{name} contains {names}.", pid)
            elif st["status"] == "may_contain":
                add("allergen.may_contain", "block", f"{name} may contain {names}.", pid)
            elif st["status"] == "unknown":
                avoid = ", ".join(catalog.ALLERGENS.get(c, c) for c in mandate.avoid_allergens)
                add("allergen.unknown", mandate.unknown_allergens,
                    f"{name} has no allergen data, so it can't be confirmed free of {avoid}. "
                    "Unknown is treated as unsafe.", pid)

        if pid in subs:
            msg = {"block": "this mandate allows no substitutes", "ask": "substitutes need a human",
                   "notify": "the buyer will be told", "silent": "substitutes are allowed"}[mandate.substitutions]
            add("substitution", mandate.substitutions, f"{name} replaces {subs[pid]}; {msg}.", pid)

        if l.get("logistic_model") == "Terminal":
            if mandate.terminal_products != "silent":
                add("supply.terminal", mandate.terminal_products,
                    f"{name} ships from the supplier's warehouse, so its stock is unknown.", pid)

        rel = l.get("delivery_reliability")
        if mandate.min_delivery_reliability is not None and rel is not None and rel < mandate.min_delivery_reliability:
            add("supply.reliability", "ask",
                f"{name} has been delivered in full only {rel:.0%} of the time "
                f"(mandate minimum {mandate.min_delivery_reliability:.0%}).", pid)

        cp = l.get("comparison_price") or {}
        cap = mandate.max_comparison_price_eur.get(cp.get("per") or "")
        if cap is not None and cp.get("ex_vat") is not None and cp["ex_vat"] > cap:
            add("price.comparison_cap", "ask",
                f"{name} costs €{cp['ex_vat']:.2f}/{cp['per']}, above the €{cap:.2f} cap.", pid)

    f.extend(extra_findings or [])
    outcome: Tier = max((x.outcome for x in f), key=_RANK.__getitem__, default="silent")
    if outcome in ("silent", "notify"):
        add("mandate.ok", outcome, f"Within mandate {mandate.id}: €{total:,.2f} ex VAT, all checks passed.")
    return outcome, f
