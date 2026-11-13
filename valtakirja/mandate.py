"""The mandate (valtakirja) and the deterministic rules engine that enforces it.

A mandate is what a human gives their purchasing agent: spend limits, allergen
and product requirements, and what the agent may decide alone. The engine checks
a quote against it and returns one of three outcomes, each with the rules that
fired, in plain language. No LLM is involved in this decision.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from . import catalog

Outcome = Literal["order", "approval", "refuse"]
Policy = Literal["auto", "approval", "never"]


class Mandate(BaseModel):
    id: str
    buyer: str = Field(description="Organisation the agent buys for")
    agent_id: str = Field(description="The agent this mandate is issued to")
    issued_by: str = Field(description="The human who issued the mandate")
    valid_until: datetime | None = None
    revoked: bool = False
    auto_approve_limit_eur: float = Field(500.0, description="Orders up to this (ex VAT) need no human")
    hard_limit_eur: float | None = Field(5000.0, description="Orders above this (ex VAT) are refused outright")
    daily_limit_eur: float | None = Field(None, description="Total per day before a human must approve")
    allowed_categories: list[str] | None = Field(None, description="Category codes; None = any")
    avoid_allergens: list[str] = Field(default_factory=list, description="GS1 allergen codes, e.g. AM (milk), AN (nuts)")
    allow_may_contain: bool = False
    unknown_allergens: Literal["approval", "refuse"] = "approval"
    required_claims: list[str] = Field(default_factory=list, description="e.g. FREE_FROM_LACTOSE")
    substitutions: Policy = "approval"
    terminal_products: Policy = "auto"
    min_delivery_reliability: float | None = 0.9
    later_delivery: Literal["approval", "refuse"] = "approval"
    max_comparison_price_eur: dict[str, float] = Field(default_factory=dict, description="e.g. {'L': 2.5}")


class Finding(BaseModel):
    rule: str
    outcome: Outcome
    message: str
    product_id: str | None = None


_RANK = {"order": 0, "approval": 1, "refuse": 2}


def evaluate(
    mandate: Mandate,
    agent_id: str,
    lines: list[dict],
    *,
    requested_date: date,
    substitute_for: dict[str, str] | None = None,
    uncovered: float = 0.0,
    need_unit: str | None = None,
    spent_today_eur: float = 0.0,
    now: datetime,
) -> tuple[Outcome, list[Finding]]:
    f: list[Finding] = []

    def add(rule, outcome, message, pid=None):
        f.append(Finding(rule=rule, outcome=outcome, message=message, product_id=pid))

    # Who is asking, and is the mandate still in force?
    if mandate.revoked:
        add("mandate.revoked", "refuse", f"Mandate {mandate.id} has been revoked by {mandate.issued_by}.")
    if mandate.valid_until and now > mandate.valid_until:
        add("mandate.expired", "refuse", f"Mandate {mandate.id} expired on {mandate.valid_until:%d.%m.%Y}.")
    if agent_id != mandate.agent_id:
        add("mandate.agent", "refuse", f"Agent '{agent_id}' is not the holder of mandate {mandate.id}.")
    if not lines:
        add("order.empty", "refuse", "Nothing orderable was found for this request.")

    total = round(sum(l.get("line_total_ex_vat") or 0 for l in lines), 2)
    if mandate.hard_limit_eur is not None and total > mandate.hard_limit_eur:
        add("spend.hard_limit", "refuse",
            f"Order total €{total:,.2f} exceeds the mandate's hard limit of €{mandate.hard_limit_eur:,.2f}.")
    elif total > mandate.auto_approve_limit_eur:
        add("spend.auto_limit", "approval",
            f"Order total €{total:,.2f} is above the €{mandate.auto_approve_limit_eur:,.2f} the agent may spend alone.")
    if mandate.daily_limit_eur is not None and spent_today_eur + total > mandate.daily_limit_eur:
        add("spend.daily_limit", "approval",
            f"With this order, today's spend would be €{spent_today_eur + total:,.2f}, "
            f"above the daily limit of €{mandate.daily_limit_eur:,.2f}.")
    if uncovered > 0:
        add("need.partial", "approval",
            f"Only part of the need can be covered: {uncovered:g} {need_unit or 'units'} is still missing.")

    subs = substitute_for or {}
    for l in lines:
        pid, name = l["product_id"], l["name"]
        p = catalog.get(pid) or {}

        for issue in l.get("issues", []):
            if issue["severity"] == "blocking":
                add(f"availability.{issue['code'].lower()}", "refuse", f"{name}: {issue['message']}", pid)
            elif issue["code"] == "LATER_DATE":
                add("delivery.later_date", mandate.later_delivery, f"{name}: {issue['message']}", pid)
            elif issue["code"] == "PRICE_EXPIRES":
                add("price.expires", "approval", f"{name}: {issue['message']}", pid)

        if mandate.allowed_categories is not None and p.get("category") not in mandate.allowed_categories:
            add("product.category", "refuse",
                f"{name} is in category {p.get('category')}, which this mandate doesn't allow.", pid)

        missing = sorted(set(mandate.required_claims) - set(l.get("claims") or []))
        if missing:
            add("product.claims", "refuse", f"{name} lacks required claims: {', '.join(missing)}.", pid)

        if mandate.avoid_allergens:
            st = catalog.allergen_status(p, mandate.avoid_allergens, mandate.allow_may_contain)
            names = ", ".join(catalog.ALLERGENS.get(c, c) for c in st["codes"])
            if st["status"] == "contains":
                add("allergen.contains", "refuse", f"{name} contains {names}.", pid)
            elif st["status"] == "may_contain":
                add("allergen.may_contain", "refuse", f"{name} may contain {names}.", pid)
            elif st["status"] == "unknown":
                avoid = ", ".join(catalog.ALLERGENS.get(c, c) for c in mandate.avoid_allergens)
                add("allergen.unknown", mandate.unknown_allergens,
                    f"{name} has no allergen data, so it can't be confirmed free of {avoid}. "
                    "Unknown is treated as unsafe.", pid)

        if pid in subs:
            if mandate.substitutions == "never":
                add("substitution.never", "refuse", f"{name} substitutes {subs[pid]}; this mandate allows no substitutes.", pid)
            elif mandate.substitutions == "approval":
                add("substitution.approval", "approval", f"{name} is a substitute for {subs[pid]}; substitutes need a human.", pid)

        if l.get("logistic_model") == "Terminal":
            if mandate.terminal_products == "never":
                add("supply.terminal", "refuse", f"{name} ships from the supplier (stock unknown); not allowed.", pid)
            elif mandate.terminal_products == "approval":
                add("supply.terminal", "approval", f"{name} ships from the supplier and its stock is unknown.", pid)

        rel = l.get("delivery_reliability")
        if mandate.min_delivery_reliability is not None and rel is not None and rel < mandate.min_delivery_reliability:
            add("supply.reliability", "approval",
                f"{name} has been delivered in full only {rel:.0%} of the time "
                f"(mandate minimum {mandate.min_delivery_reliability:.0%}).", pid)

        cp = l.get("comparison_price") or {}
        cap = mandate.max_comparison_price_eur.get(cp.get("per") or "")
        if cap is not None and cp.get("ex_vat") is not None and cp["ex_vat"] > cap:
            add("price.comparison_cap", "approval",
                f"{name} costs €{cp['ex_vat']:.2f}/{cp['per']}, above the €{cap:.2f} cap.", pid)

    outcome: Outcome = max((x.outcome for x in f), key=_RANK.__getitem__, default="order")
    if outcome == "order":
        add("mandate.ok", "order", f"Within mandate {mandate.id}: €{total:,.2f} ex VAT, all checks passed.")
    return outcome, f
