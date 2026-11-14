"""The mandate (sous) and the deterministic rules engine that enforces it.

A mandate is what a human gives their purchasing agent: spend limits, allergen
and product requirements, and what the agent may decide alone. The engine checks
a quote against it and returns one of four tiers (silent / notify / ask / block), each with the rules that
fired, in plain language. No language model makes this decision; the one model input is the Gemini Decisions API's
probability for products that declare no allergens (sous/allergen_ai.py), which can only add allergen.likely.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from . import autonomy, catalog, config, fi, holds, origin as origins, pause, purpose as purposes

# Four tiers, from least to most human involvement:
#   silent: routine, the order goes through and is logged
#   notify: the order goes through and the human gets an FYI
#   ask:    held until a human approves (yes/no, with a deadline)
#   block:  never automatic; refused with the rule that fired
Tier = Literal["silent", "notify", "ask", "block"]


class Mandate(BaseModel):
    id: str
    buyer: str = Field(description="Organisation the agent buys for")
    customer_number: str | None = Field(
        None, max_length=64, description="Valio Aimo customer number of the buyer (customer master; SAP KUNNR in "
        "production): which customer account's credit, payment block and delivery days apply (sous/account.py). "
        "None = looked up by buyer name")
    agent_id: str = Field(description="The agent this mandate is issued to")
    issued_by: str = Field(description="The human who issued the mandate")
    valid_until: datetime | None = None
    revoked: bool = False
    require_agent_auth: bool = Field(False, description="Quotes and orders need the agent's bearer credential")
    notify_above_eur: float | None = Field(None, description="Orders above this (ex VAT) go through but the human gets an FYI")
    auto_approve_limit_eur: float = Field(500.0, description="Orders up to this (ex VAT) need no human")
    hard_limit_eur: float | None = Field(5000.0, description="Orders above this (ex VAT) are refused outright")
    daily_limit_eur: float | None = Field(None, description="Total per day before a human must approve")
    weekly_limit_eur: float | None = Field(
        None, description="Total per calendar week (Mon-Sun, Helsinki) before a human must approve; None = off")
    split_window_minutes: int | None = Field(
        None, ge=1, le=1440, description="Ask when orders placed within this many minutes, each under the "
        "auto-approve limit, add up to more than it (an agent slicing one big order into small ones); None = off")
    first_time_products: Literal["notify", "ask", "block"] | None = Field(
        None, description="Tier for a product never ordered under this mandate before; None = off")
    autonomy: Literal["advise", "reorder", "order"] = Field(
        "order", description="How much the agent may do alone: advise (every order waits for a human), reorder (only "
        "the buyer's usual products alone) or order (whatever the other rules allow). Valio Aimo can cap it lower "
        "per buyer (GET /autonomy-caps)")
    usual_products: list[str] = Field(
        default_factory=list, description="Product ids the buyer has bought before (e.g. imported from their Valio "
        "order history); with first_time_products they count as known, like products ordered under this mandate")
    max_price_change_pct: float | None = Field(
        20.0, gt=0, description="Ask when a product's unit price (ex VAT) differs by more than this many percent, up "
        "or down, from the last price paid for it under this mandate; None = off")
    allowed_categories: list[str] | None = Field(None, description="Category codes; None = any")
    avoid_allergens: list[str] = Field(default_factory=list, description="GS1 allergen codes, e.g. AM (milk), AN (nuts)")
    allow_may_contain: bool = False
    unknown_allergens: Literal["ask", "block"] = "ask"  # unknown is never treated as safe
    required_claims: list[str] = Field(default_factory=list, description="e.g. FREE_FROM_LACTOSE")
    product_purposes: dict[str, purposes.ProductPurpose] = Field(
        default_factory=dict, description="What the buyer uses a product for, set by the buyer's human, keyed by "
        "product id, e.g. {'464411': {'purpose': 'recipe: whipped desserts', 'substitution': 'ask'}}. substitution "
        "exact = only this product (a substitute is refused, and Valio Aimo proposes none), ask = a substitute "
        "always needs the buyer's yes, allowed = the substitutions tier applies; default read from the purpose")
    allowed_origins: list[str] | None = Field(
        None, description="Countries (fi, se, de ...) and/or groups (EU, EEA, NORDIC, BALTIC, EUROPE) the goods may "
        "come from; a product from anywhere else gets origin_violation. The country is the supplier-declared "
        "countryOfOrigin of Valio Aimo's product master, not independently verified. None = any country")
    avoid_origins: list[str] = Field(
        default_factory=list, description="Countries and/or groups the goods must not come from (same codes as "
        "allowed_origins); a product from one gets origin_violation")
    origin_violation: Literal["ask", "block"] = Field(
        "ask", description="Tier for a product from outside allowed_origins or from an avoided origin")
    origin_unknown: Literal["notify", "ask", "block"] = Field(
        "ask", description="Tier for a product whose product master has no usable country of origin while "
        "allowed_origins or avoid_origins is set: unknown is never treated as allowed")
    substitutions: Tier = "ask"
    terminal_products: Tier = "silent"
    min_delivery_reliability: float | None = 0.9
    later_delivery: Literal["notify", "ask", "block"] = "ask"
    max_comparison_price_eur: dict[str, float] = Field(
        default_factory=dict, description="Cap on price per comparison unit (EUR ex VAT) for every product sold by "
        "that unit, e.g. {'L': 2.5}, unless comparison_price_caps has a cap for the product's kind; above it, held")
    comparison_price_caps: dict[str, float] = Field(
        default_factory=dict, description="Caps on price per L or KG (EUR ex VAT, in the product's own comparison "
        "unit) for one kind of product, keyed by product type ('kahvikerma'), product family ('milk', 'cream', "
        "'coffee milk'; sous/substitutes.py) or catalog category code, e.g. {'milk': 2.5}; the most specific match "
        "wins and replaces max_comparison_price_eur for that product; above it, held")
    demo_public: bool = Field(False, description="Curated demo data: readable without credentials (see sous/access.py). "
                              "Real buyers' mandates leave this off.")
    unusual_quantity_factor: float | None = Field(
        3.0, gt=1, description="Ask when a product family's quantity is more than this many times the median of the "
        "buyer's past orders of it (at least 3 past orders); None = off")
    approval_method: Literal["any", "passkey"] = Field(
        "any", description="How a held order may be approved: 'any' (approver code, sandbox approver token or a "
        "passkey) or 'passkey' (only a registered approver's passkey, bound to the exact order; rejecting still "
        "works with the code)")
    sandbox: str | None = Field(None, description="Judge sandbox id (e.g. SBX-7Q2K) for a visitor's copy; see sous/sandbox.py")

    @field_validator("max_comparison_price_eur", "comparison_price_caps")
    @classmethod
    def _positive_caps(cls, v: dict[str, float]) -> dict[str, float]:
        if any(x <= 0 for x in v.values()):
            raise ValueError("price caps must be above 0")
        return v

    @field_validator("allowed_origins", "avoid_origins")
    @classmethod
    def _origins(cls, v: list[str] | None) -> list[str] | None:
        return origins.normalize(v)

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
    # Finnish twin of `message` (sous/fi.py), built from the same values. Never dumped: signed quotes and receipts
    # keep exactly the English findings; Finnish goes only into unsigned explanations_fi fields.
    message_fi: str | None = Field(None, exclude=True)

    def model_post_init(self, _ctx) -> None:
        if self.rule_url is None:
            self.rule_url = f"{config.PUBLIC_BASE_URL}/rules/{self.rule}"


# Fewer past orders than this and there is no "usual" quantity to compare with.
UNUSUAL_MIN_HISTORY = 3


_RANK = {"silent": 0, "notify": 1, "ask": 2, "block": 3}


def _cents(eur: float) -> int:
    """Money compared in whole cents: 1.10 + 2.20 is exactly a €3.30 limit, not 3.3000000000000003."""
    return round(Decimal(str(eur)) * 100)


def _likely(assessment: dict[str, dict] | None) -> list[tuple[str, dict]]:
    """Avoided allergens the Decisions API puts at or above allergen_ai.LIKELY, highest first."""
    from .allergen_ai import likely
    return likely(assessment) if assessment else []


def _over_pct(now: float, was: float, pct: float) -> bool:
    """|now - was| / was > pct %, decided in exact decimals (a change of exactly the limit passes)."""
    n, w = Decimal(str(now)), Decimal(str(was))
    return abs(n - w) * 100 > w * Decimal(str(pct))


def comparison_cap(mandate: Mandate, pid: str, per: str | None) -> tuple[float, str] | None:
    """(cap in EUR per `per`, what it covers) for one product: comparison_price_caps by product type, then family,
    then catalog category (the most specific wins), else max_comparison_price_eur[per] (every product in that
    unit). None when no cap applies."""
    p = catalog.get(pid) or {}
    if mandate.comparison_price_caps and p:
        try:
            from .substitutes import product_type  # lazy: substitutes imports mandate
            t = product_type(p) or {}
        except Exception:  # an unreadable name never breaks an order: fall back to the category
            t = {}
        for key, what in ((t.get("key"), t.get("label")), (t.get("family"), t.get("family")),
                          (p.get("category"), f"category {p.get('category')}")):
            if key and key in mandate.comparison_price_caps:
                return mandate.comparison_price_caps[key], str(what or key)
    if per and per in mandate.max_comparison_price_eur:
        return mandate.max_comparison_price_eur[per], f"everything priced per {per}"
    return None


def readable_name(name: str | None) -> str:
    """Catalog names are often ALL CAPS ('BRAVO CHEF 400G CHOPPED TOMATOES'); reasons read better in sentence case."""
    name = "" if name is None else str(name)
    letters = [c for c in name if c.isalpha()]
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
    spent_week_eur: float = 0.0,
    recent_orders: list[dict] | None = None,
    ordered_products: set[str] | None = None,
    last_prices: dict[str, dict] | None = None,
    families: dict[str, tuple[str, str]] | None = None,
    last_comparison_prices: dict[tuple[str, str], dict] | None = None,
    allergen_ai: dict[str, dict[str, dict]] | None = None,
    allergen_clearances: dict[str, dict] | None = None,
    remembered: list[dict] | None = None,
) -> tuple[Tier, list[Finding]]:
    """`extra_findings` are checks made outside the mandate (e.g. standing-order drift); they count like any rule.
    `quantities` is this order's amount per (product family, unit), `usual_quantities` the same buyer's past
    ordered amounts per key (both from sous/usual.py); they feed quantity.unusual.
    `spent_week_eur` is this calendar week's ordered total (spend.weekly_limit), `recent_orders` the orders that
    went through within split_window_minutes as [{id, total_ex_vat}] (spend.split_orders), `ordered_products`
    every product id ever ordered under the mandate (product.first_time), `last_prices` the last unit price paid
    per product under the mandate as {product_id: {unit_price_ex_vat, order_id, at}} (price.changed), `families`
    each line's product family and `last_comparison_prices` the last price per L/KG paid per (family, per) unit
    (price.comparison_changed). Times are when orders went through (store.ordered_at).
    `allergen_ai` is the Gemini Decisions API's {product_id: {code: {p, confidence, model}}} for products that
    declare no allergens (sous/allergen_ai.py): it can turn allergen.unknown into allergen.likely, never into safe.
    `allergen_clearances` is {product_id: clearance} from sous/clearance.py: a human of this buyer already confirmed
    the product free of everything the mandate avoids, so allergen.unknown becomes allergen.cleared (notify).
    `remembered` are the mandate's remembered decisions from sous/remembered.py: a matching one turns an ask of
    substitution / product.first_time / unit.interpreted / product.type_mismatch into its remembered.* notify twin;
    nothing else (and no block) is touched."""
    f: list[Finding] = []

    def add(rule, outcome, message, pid=None, message_fi=None):
        f.append(Finding(rule=rule, outcome=outcome, message=message, product_id=pid, message_fi=message_fi))

    # Who is asking, and is the mandate still in force?
    if mandate.revoked:
        add("mandate.revoked", "block", f"Mandate {mandate.id} has been revoked by {mandate.issued_by}.",
            message_fi=f"{mandate.issued_by} on perunut valtakirjan {mandate.id}.")
    if mandate.valid_until and now > mandate.valid_until:
        add("mandate.expired", "block", f"Mandate {mandate.id} expired on {mandate.valid_until:%d.%m.%Y}.",
            message_fi=f"Valtakirja {mandate.id} vanheni {fi.day(mandate.valid_until)}.")
    if agent_id != mandate.agent_id:
        add("mandate.agent", "block", f"Agent '{agent_id}' is not the holder of mandate {mandate.id}.",
            message_fi=f"Agentti '{agent_id}' ei ole valtakirjan {mandate.id} haltija.")
    if not lines:
        add("order.empty", "block", "Nothing orderable was found for this request.",
            message_fi="Pyyntöön ei löytynyt mitään tilattavaa.")
    # How much the agent may do alone: the mandate's level, capped by Valio Aimo (sous/autonomy.py).
    level, cap = autonomy.effective(mandate.autonomy, mandate.buyer)
    capped = f" Valio Aimo limits this buyer's agents to '{level}': {cap['reason']}" if cap else ""
    capped_fi = f" Valio Aimo rajaa tämän ostajan agentit tasolle '{level}': {cap['reason']}" if cap else ""
    if level == "advise" and lines:
        add("autonomy.capped" if cap else "autonomy.advise", "ask",
            f"The agent may advise only: every order waits for a human.{capped}",
            message_fi="Agentti saa vain neuvoa: jokainen tilaus odottaa ihmisen hyväksyntää." + capped_fi)
    # Valio Aimo's kill switch: no agent orders (nor approvals) while ordering is paused for this buyer.
    if paused := pause.active_for(mandate.buyer):
        add("ordering.paused", "block", pause.message(paused), message_fi=fi.pause(paused))

    # An unorderable line already blocks the order (availability.*); it never counts towards spend.
    total = round(sum(l.get("line_total_ex_vat") or 0 for l in lines if l.get("orderable", True)), 2)
    if mandate.hard_limit_eur is not None and total > mandate.hard_limit_eur:
        add("spend.hard_limit", "block",
            f"Order total €{total:,.2f} exceeds the mandate's hard limit of €{mandate.hard_limit_eur:,.2f}.",
            message_fi=f"Tilauksen summa {fi.eur(total)} ylittää valtakirjan ehdottoman ylärajan "
                       f"{fi.eur(mandate.hard_limit_eur)}.")
    elif total > mandate.auto_approve_limit_eur:
        add("spend.auto_limit", "ask",
            f"Order total €{total:,.2f} is above the €{mandate.auto_approve_limit_eur:,.2f} the agent may spend alone.",
            message_fi=f"Tilauksen summa {fi.eur(total)} on yli {fi.eur(mandate.auto_approve_limit_eur)}, jonka "
                       "agentti saa käyttää itse.")
    elif mandate.notify_above_eur is not None and total > mandate.notify_above_eur:
        add("spend.notify", "notify",
            f"Order total €{total:,.2f} is above €{mandate.notify_above_eur:,.2f}, so the buyer gets an FYI.",
            message_fi=f"Tilauksen summa {fi.eur(total)} on yli {fi.eur(mandate.notify_above_eur)}, joten ostaja "
                       "saa ilmoituksen.")
    if mandate.weekly_limit_eur is not None and _cents(spent_week_eur) + _cents(total) > _cents(mandate.weekly_limit_eur):
        add("spend.weekly_limit", "ask",
            f"With this order, this week's spend would be €{(_cents(spent_week_eur) + _cents(total)) / 100:,.2f}, "
            f"above the weekly limit of €{mandate.weekly_limit_eur:,.2f}.",
            message_fi=f"Tämän tilauksen kanssa viikon ostot olisivat "
                       f"{fi.eur((_cents(spent_week_eur) + _cents(total)) / 100)}, yli viikkorajan "
                       f"{fi.eur(mandate.weekly_limit_eur)}.")
    # Slicing: orders that each stay under the auto-approve limit but together pass it within a short window.
    auto = _cents(mandate.auto_approve_limit_eur)
    small = [o for o in recent_orders or () if _cents(o.get("total_ex_vat") or 0) <= auto]
    if (mandate.split_window_minutes and small and 0 < _cents(total) <= auto
            and sum(_cents(o["total_ex_vat"]) for o in small) + _cents(total) > auto):
        together = (sum(_cents(o["total_ex_vat"]) for o in small) + _cents(total)) / 100
        add("spend.split_orders", "ask",
            f"This order and {len(small)} more in the last {mandate.split_window_minutes} min add up to "
            f"€{together:,.2f}, above the €{mandate.auto_approve_limit_eur:,.2f} the agent may spend alone: "
            "small orders in a row are checked together.",
            message_fi=f"Tämä tilaus ja {len(small)} muuta viimeisen {mandate.split_window_minutes} minuutin ajalta "
                       f"ovat yhteensä {fi.eur(together)}, yli {fi.eur(mandate.auto_approve_limit_eur)}, jonka "
                       "agentti saa käyttää itse: peräkkäiset pienet tilaukset tarkistetaan yhdessä.")
    if mandate.daily_limit_eur is not None and _cents(spent_today_eur) + _cents(total) > _cents(mandate.daily_limit_eur):
        add("spend.daily_limit", "ask",
            f"With this order, today's spend would be €{(_cents(spent_today_eur) + _cents(total)) / 100:,.2f}, "
            f"above the daily limit of €{mandate.daily_limit_eur:,.2f}.",
            message_fi=f"Tämän tilauksen kanssa päivän ostot olisivat "
                       f"{fi.eur((_cents(spent_today_eur) + _cents(total)) / 100)}, yli päivärajan "
                       f"{fi.eur(mandate.daily_limit_eur)}.")
    # `uncovered` is either one amount in `need_unit`, or {unit: amount} when items use different units.
    missing: dict[str, float] = {}
    for u, a in (uncovered if isinstance(uncovered, dict) else {need_unit: uncovered}).items():
        if a and a > 0:
            u = "units" if u in (None, "SALES_UNIT") else u
            missing[u] = round(missing.get(u, 0.0) + a, 3)
    if missing:
        add("need.partial", "ask",
            "Only part of the need can be covered: "
            f"{' and '.join(f'{a:g} {u}' for u, a in missing.items())} still missing.",
            message_fi="Tarpeesta voidaan kattaa vain osa: vielä puuttuu "
                       f"{' ja '.join(f'{fi.num(a)} {fi.unit(u)}' for u, a in missing.items())}.")

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
                f"(median of your last {n} orders; the mandate asks above {factor:g}×).", cur.get("product_id"),
                f"{cur['label']}: {fi.num(cur['amount'])} {fi.unit(key[1])} on "
                f"{fi.num(round(cur['amount'] / med, 1))}-kertainen tavalliseen {fi.num(med)} {fi.unit(key[1])} "
                f"verrattuna (viimeisten {n} tilauksesi mediaani; valtakirja vaatii hyväksynnän yli "
                f"{fi.num(factor)}-kertaisesta).")

    subs = substitute_for or {}
    for l in lines:
        pid = l["product_id"]
        name = readable_name(l.get("name") or str(pid))  # a missing display name never crashes a quote
        p = catalog.get(pid) or {}

        # Valio's hold on the product: also for a quote issued before the hold (ordering, approving it later)
        held = next((i for i in l.get("issues", []) if i["code"] == "PRODUCT_HELD"), None)
        if held or (hold := holds.active_for(pid)):
            add("product.held", "block", f"{name}: {held['message'] if held else holds.message(hold)}", pid,
                f"{name}: {fi.hold(hold) if not held and hold else fi.rule_text('product.held')}")
        def issue_fi(issue, l=l, name=name):
            return f"{name}: {fi.issue(issue['code'], l, requested_date)}"

        for issue in l.get("issues", []):
            if issue["code"] == "PRODUCT_HELD":
                continue
            if issue["severity"] == "blocking":
                add(f"availability.{issue['code'].lower()}", "block", f"{name}: {issue['message']}", pid, issue_fi(issue))
            elif issue["code"] == "LATER_DATE":
                add("delivery.later_date", mandate.later_delivery, f"{name}: {issue['message']}", pid, issue_fi(issue))
            elif issue["code"] == "PARTIAL_STOCK":
                add("availability.partial_stock", "ask", f"{name}: {issue['message']}", pid, issue_fi(issue))
            elif issue["code"] == "PRICE_EXPIRES":
                add("price.expires", "ask", f"{name}: {issue['message']}", pid, issue_fi(issue))
            elif issue["code"] == "DATA_STALE":  # never order on last-known data without a human
                add("availability.data_stale", "ask", f"{name}: {issue['message']}", pid, issue_fi(issue))
            elif issue["code"] == "UNIT_INTERPRETED":  # '20 ST' of a product without ST read as sales units
                add("unit.interpreted", "ask", f"{name}: {issue['message']}", pid, issue_fi(issue))
            elif issue["code"] == "TYPE_MISMATCH":  # not the kind of product asked for (coffee milk for milk)
                add("product.type_mismatch", "ask", f"{name}: {issue['message']}", pid, issue_fi(issue))
            elif issue["code"] == "PACK_OVERSIZE":  # 12 kg needed, one 20 kg bag planned: the buyer is told
                add("quantity.pack_oversize", "notify", f"{name}: {issue['message']}", pid, issue_fi(issue))

        if level == "reorder" and l.get("orderable", True) and pid not in (ordered_products or set()) \
                and pid not in mandate.usual_products:
            add("autonomy.capped" if cap else "autonomy.reorder", "ask",
                f"{name} is not one of the buyer's usual products, and the agent may only reorder those alone."
                + capped, pid,
                f"{name} ei ole ostajan tavallisia tuotteita, ja agentti saa tilata itse vain niitä." + capped_fi)

        if (mandate.first_time_products and ordered_products is not None
                and pid not in ordered_products and pid not in mandate.usual_products):
            add("product.first_time", mandate.first_time_products,
                f"{name} has never been ordered under this mandate before.", pid,
                f"{name}: tätä tuotetta ei ole ennen tilattu tällä valtakirjalla.")

        last = (last_prices or {}).get(pid) if mandate.max_price_change_pct else None
        now_price = l.get("unit_price_ex_vat")
        if last and now_price is not None and last.get("unit_price_ex_vat"):
            was = last["unit_price_ex_vat"]
            change = (now_price - was) / was * 100  # for the message only; the decision is exact
            if _over_pct(now_price, was, mandate.max_price_change_pct):
                unit = l.get("sales_unit") or "unit"
                add("price.changed", "ask",
                    f"{name}: €{now_price:,.2f} per {unit} now, {abs(change):.0f}% {'above' if change > 0 else 'below'} "
                    f"the €{was:,.2f} paid on {last['at'][8:10]}.{last['at'][5:7]} (order {last['order_id']}); the "
                    f"mandate asks above {mandate.max_price_change_pct:g}%.", pid,
                    f"{name}: nyt {fi.eur(now_price)}/{fi.unit(unit)}, {fi.pct(round(abs(change)))} "
                    f"{'kalliimpi' if change > 0 else 'halvempi'} kuin {fi.short_day(last['at'])} maksettu "
                    f"{fi.eur(was)} (tilaus {last['order_id']}); valtakirja vaatii hyväksynnän, kun muutos on yli "
                    f"{fi.pct(mandate.max_price_change_pct)}.")

        # Price per L / KG against what the buyer last paid for the same kind of product (a pricier brand or pack)
        cp_now = l.get("comparison_price") or {}
        fam = (families or {}).get(pid)
        prev = (last_comparison_prices or {}).get((fam[0], cp_now.get("per"))) if fam and mandate.max_price_change_pct else None
        if (prev and prev["product_id"] != pid and cp_now.get("ex_vat") is not None and prev.get("ex_vat")
                and cp_now["ex_vat"] > prev["ex_vat"] and _over_pct(cp_now["ex_vat"], prev["ex_vat"], mandate.max_price_change_pct)):
            per, up = cp_now["per"], (cp_now["ex_vat"] - prev["ex_vat"]) / prev["ex_vat"] * 100
            add("price.comparison_changed", "ask",
                f"{name}: €{cp_now['ex_vat']:,.2f}/{per}, {up:.0f}% above the €{prev['ex_vat']:,.2f}/{per} paid for "
                f"{fam[1].lower()} on {prev['at'][8:10]}.{prev['at'][5:7]} ({readable_name(prev.get('name') or prev['product_id'])}, "
                f"order {prev['order_id']}); the mandate asks above {mandate.max_price_change_pct:g}%.", pid,
                f"{name}: {fi.eur(cp_now['ex_vat'])}/{fi.unit(per)}, {fi.pct(round(up))} kalliimpi kuin "
                f"{fi.short_day(prev['at'])} maksettu {fi.eur(prev['ex_vat'])}/{fi.unit(per)} "
                f"({readable_name(prev.get('name') or prev['product_id'])}, tilaus {prev['order_id']}); "
                f"valtakirja vaatii hyväksynnän, kun nousu on yli {fi.pct(mandate.max_price_change_pct)}.")

        if mandate.allowed_origins or mandate.avoid_origins:
            oc = origins.check(p.get("country_of_origin"), mandate.allowed_origins, mandate.avoid_origins)
            if oc == "unknown":
                add("origin.unknown", mandate.origin_unknown,
                    f"{name}: the product master gives no country of origin, so it can't be checked against the "
                    f"mandate ({origins.label(mandate.allowed_origins) or 'avoiding ' + origins.label(mandate.avoid_origins)}).", pid,
                    f"{name}: tuotetiedoissa ei ole alkuperämaata, joten sitä ei voi tarkistaa valtakirjaa vasten "
                    f"({'sallitut: ' + fi.origins(mandate.allowed_origins) if mandate.allowed_origins else 'vältettävät: ' + fi.origins(mandate.avoid_origins)}).")
            elif oc in ("avoided", "outside"):
                where = origins.name(origins.code(p.get("country_of_origin")))
                rule = (f"avoids {origins.label(mandate.avoid_origins)}" if oc == "avoided"
                        else f"allows only {origins.label(mandate.allowed_origins)}")
                add("origin.restricted", mandate.origin_violation,
                    f"{name} comes from {where} (supplier-declared); the mandate {rule}.", pid,
                    f"{name}: alkuperämaa {fi.country(origins.code(p.get('country_of_origin')))} (toimittajan "
                    "ilmoittama); valtakirja "
                    + (f"välttää alkuperämaita {fi.origins(mandate.avoid_origins)}." if oc == "avoided"
                       else f"sallii vain alkuperämaat {fi.origins(mandate.allowed_origins)}."))

        if mandate.allowed_categories is not None and p.get("category") not in mandate.allowed_categories:
            add("product.category", "block",
                f"{name} is in category {p.get('category')}, which this mandate doesn't allow.", pid,
                f"{name}: tuoteryhmä {p.get('category')} ei kuulu valtakirjan sallimiin.")

        missing = sorted(set(mandate.required_claims) - set(l.get("claims") or []))
        if missing:
            add("product.claims", "block", f"{name} lacks required claims: {', '.join(missing)}.", pid,
                f"{name}: puuttuu vaadittu merkintä: {fi.claims(missing)}.")

        if mandate.avoid_allergens:
            st = catalog.allergen_status(p, mandate.avoid_allergens, mandate.allow_may_contain)
            names = ", ".join(catalog.ALLERGENS.get(c, c) for c in st["codes"])
            if st["status"] == "contains":
                add("allergen.contains", "block", f"{name} contains {names}.", pid,
                    f"{name} sisältää vältettävää allergeenia: {fi.allergens(st['codes'])}.")
            elif st["status"] == "may_contain":
                add("allergen.may_contain", "block", f"{name} may contain {names}.", pid,
                    f"{name} voi sisältää vältettävää allergeenia: {fi.allergens(st['codes'])}.")
            elif st["status"] == "unknown" and (hits := _likely((allergen_ai or {}).get(pid))):
                said = ", ".join(f"{catalog.ALLERGENS.get(c, c)} ({a['p']:.0%})" for c, a in hits)
                add("allergen.likely", "block",
                    f"{name} declares no allergens, but from its name and ingredients it very likely contains "
                    f"{said}, by Gemini Decisions ({hits[0][1].get('model', 'gemini-decision-1')}). Refused: an "
                    "avoided allergen is too likely to approve.", pid,
                    f"{name} ei ilmoita allergeeneja, mutta nimen ja raaka-aineiden perusteella siinä on hyvin "
                    "todennäköisesti: "
                    + ", ".join(f"{fi.ALLERGENS.get(c, c)} ({fi.ratio(a['p'])})" for c, a in hits)
                    + f" (Gemini Decisions, {hits[0][1].get('model', 'gemini-decision-1')}). Estetty: vältettävä "
                    "allergeeni on liian todennäköinen hyväksyttäväksi.")
            elif st["status"] == "unknown" and (clr := (allergen_clearances or {}).get(pid)) \
                    and mandate.unknown_allergens != "block":
                from .clearance import message as _cleared
                avoid = ", ".join(catalog.ALLERGENS.get(c, c) for c in mandate.avoid_allergens)
                add("allergen.cleared", "notify", _cleared(name, clr, avoid), pid,
                    fi.clearance(name, clr, mandate.avoid_allergens))
            elif st["status"] == "unknown":
                avoid = ", ".join(catalog.ALLERGENS.get(c, c) for c in mandate.avoid_allergens)
                add("allergen.unknown", mandate.unknown_allergens,
                    f"{name} has no allergen data, so it can't be confirmed free of {avoid}. "
                    "Unknown is treated as unsafe.", pid,
                    f"{name}: allergeenitiedot puuttuvat, joten ei voida varmistaa, ettei siinä ole: "
                    f"{fi.allergens(mandate.avoid_allergens)}. Tuntematonta ei pidetä turvallisena.")

        if pid in subs:
            lvl, why = purposes.for_product(mandate, subs[pid], (l.get("purpose"), l.get("substitution")))
            orig_name = readable_name((catalog.get(subs[pid]) or {}).get("name") or subs[pid])
            if lvl == "exact":
                add("purpose.exact", "block",
                    f"{name} would replace {orig_name}, but the buyer uses {orig_name} for {why or 'a purpose'} "
                    "where only that exact product works.", pid,
                    f"{name} korvaisi tuotteen {orig_name}, mutta ostaja käyttää sitä ({why or 'käyttötarkoitus'}) "
                    "niin, että vain juuri se tuote käy.")
            elif lvl == "ask":
                add("purpose.ask", "ask",
                    f"{name} would replace {orig_name}, which the buyer uses for {why or 'a recipe'}: a substitute "
                    "may not work the same there, so the buyer decides.", pid,
                    f"{name} korvaisi tuotteen {orig_name}, jota ostaja käyttää: {why or 'resepti'}. Korvaava ei "
                    "välttämättä toimi samoin, joten ostaja päättää.")
            msg = {"block": "this mandate allows no substitutes", "ask": "substitutes need a human",
                   "notify": "the buyer will be told", "silent": "substitutes are allowed"}[mandate.substitutions]
            msg_fi = {"block": "valtakirja ei salli korvaavia tuotteita",
                      "ask": "korvaava tuote vaatii hyväksynnän", "notify": "ostajalle kerrotaan",
                      "silent": "korvaavat tuotteet ovat sallittuja"}[mandate.substitutions]
            add("substitution", mandate.substitutions, f"{name} replaces {subs[pid]}; {msg}.", pid,
                f"{name} korvaa tuotteen {subs[pid]}; {msg_fi}.")

        if l.get("logistic_model") == "Terminal":
            if mandate.terminal_products != "silent":
                add("supply.terminal", mandate.terminal_products,
                    f"{name} ships from the supplier's warehouse, so its stock is unknown.", pid,
                    f"{name} toimitetaan toimittajan varastosta, joten sen saldoa ei tiedetä.")

        rel = l.get("delivery_reliability")
        if mandate.min_delivery_reliability is not None and rel is not None and rel < mandate.min_delivery_reliability:
            add("supply.reliability", "ask",
                f"{name} has been delivered in full only {rel:.0%} of the time "
                f"(mandate minimum {mandate.min_delivery_reliability:.0%}).", pid,
                f"{name} on toimitettu täysimääräisenä vain {fi.ratio(rel)} kerroista (valtakirjan vähimmäistaso "
                f"{fi.ratio(mandate.min_delivery_reliability)}).")

        cp = l.get("comparison_price") or {}
        # not `capped`: that is the autonomy-cap suffix above, needed again for the next line (R05 crashed on it)
        cp_cap = comparison_cap(mandate, pid, cp.get("per")) if cp.get("ex_vat") is not None else None
        if cp_cap and _cents(cp["ex_vat"]) > _cents(cp_cap[0]):
            add("price.comparison_cap", "ask",
                f"{name} costs €{cp['ex_vat']:.2f}/{cp['per']}, above the mandate's €{cp_cap[0]:.2f}/{cp['per']} "
                f"cap for {cp_cap[1]}.", pid,
                f"{name} maksaa {fi.eur(cp['ex_vat'])}/{fi.unit(cp['per'])}, yli valtakirjan hintakaton "
                f"{fi.eur(cp_cap[0])}/{fi.unit(cp['per'])} ("
                + (f"kaikki {fi.unit(cp['per'])}-hintaiset" if cp_cap[1].startswith("everything priced per")
                   else cp_cap[1].replace("category", "tuoteryhmä")) + ").")

    f.extend(extra_findings or [])
    if remembered:
        from .remembered import soften
        f[:] = soften(f, lines, remembered, now)
    outcome: Tier = max((x.outcome for x in f), key=_RANK.__getitem__, default="silent")
    if outcome in ("silent", "notify"):
        add("mandate.ok", outcome, f"Within mandate {mandate.id}: €{total:,.2f} ex VAT, all checks passed.",
            message_fi=f"Valtakirjan {mandate.id} rajoissa: {fi.eur(total)} (alv 0 %), kaikki tarkistukset "
                       "kunnossa.")
    return outcome, f


# Rules that refuse a product for what it is, whatever the quantity, date or total: the planner leaves such a
# candidate out when another one can be ordered (sous/service.py), so one bad cheapest product doesn't block a
# quote that a safe candidate could fill. The full evaluate() still runs on the chosen lines.
PRODUCT_RULES = ("product.held", "product.category", "product.claims",
                 "allergen.contains", "allergen.may_contain", "allergen.unknown")


def product_refusals(mandate: Mandate, line: dict, now: datetime) -> list[Finding]:
    """The findings with which `mandate` would block this product on its own (PRODUCT_RULES at tier block), decided
    by evaluate() itself so the planner and the check can't disagree. Allergen messages also say what the mandate
    avoids: 'Bread contains wheat (UW); Test school avoids cereals containing gluten (AW).'"""
    _, found = evaluate(mandate, mandate.agent_id, [line], requested_date=now.date(), now=now)
    out = []
    for x in found:
        if x.outcome != "block" or x.rule not in PRODUCT_RULES:
            continue
        if x.rule in ("allergen.contains", "allergen.may_contain"):
            st = catalog.allergen_status(catalog.get(line["product_id"]) or {}, mandate.avoid_allergens,
                                         mandate.allow_may_contain)
            what = ", ".join(f"{catalog.ALLERGENS.get(c, c)} ({c})" for c in st["codes"])
            avoid = ", ".join(f"{catalog.ALLERGENS.get(c, c)} ({c})" for c in mandate.avoid_allergens)
            verb = "contains" if x.rule == "allergen.contains" else "may contain"
            verb_fi = "sisältää" if x.rule == "allergen.contains" else "voi sisältää"
            what_fi = ", ".join(f"{fi.ALLERGENS.get(c, c)} ({c})" for c in st["codes"])
            avoid_fi = ", ".join(f"{fi.ALLERGENS.get(c, c)} ({c})" for c in mandate.avoid_allergens)
            x = x.model_copy(update={"message": f"{readable_name(line['name'])} {verb} {what}; "
                                                f"{mandate.buyer} avoids {avoid}.",
                                     "message_fi": f"{readable_name(line['name'])} {verb_fi}: {what_fi}; "
                                                   f"{mandate.buyer} välttää: {avoid_fi}."})
        out.append(x)
    return out
