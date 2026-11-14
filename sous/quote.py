"""Offers and quotes: Valio Aimo's price and availability merged into one verifiable view.

All arithmetic (pack sizes, quantities, lot sizes, totals, cut-offs) is done here
in plain code. Every problem found becomes an `Issue` with a stable code, a
severity and a sentence a human can read.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from typing import Annotated, Literal

from pydantic import BeforeValidator, WithJsonSchema

from . import aimo, catalog, clock, config, exposure, holds, origin

# --- need units: the one place they are normalised -----------------------------------------------------------
# Needs are in litres, kilos, sales units (SALES_UNIT, 'pack(s)') or one of Valio's own unit codes (ST = piece,
# KI = carton, ...). A unit code is taken literally when the product defines it: '20 ST' of a pesto sold per
# KI of 6 jars is 20 jars, i.e. 4 KI (24 ST) after rounding up to whole cartons. Only for a product without that
# code does it fall back to meaning its sales units. PCS / PC / KPL are spellings of ST. Every entry point
# (QuoteItem, standing orders, substitutes over REST and MCP) normalises here; build_offer, plan, options and
# substitutes convert with packs_per_sales_unit.
PACK_CODES = ("ST", "KI", "PAL", "KRS", "PAK", "BAG", "BOT", "PRK", "RAS", "CS", "SK", "KRT", "KLE", "ZML", "DOL",
              "KAN", "DSP", "KOR", "NIP", "PNT", "DZ", "MDO")
UNIT_ALIASES = {"SALES_UNIT": "SALES_UNIT", "PACK": "SALES_UNIT", "PACKS": "SALES_UNIT",
                "PCS": "ST", "PC": "ST", "KPL": "ST", "L": "L", "KG": "KG", **{c: c for c in PACK_CODES}}
UNIT_DOC = ("L or KG for volume/weight; SALES_UNIT (or pack, packs) for the product's sales units; a Valio unit code "
            "(ST = piece, also PCS / KPL; KI = carton, ...) counts that unit when the product has it (20 ST of a "
            "carton of 6 = 4 cartons, rounded up) and means sales units when it doesn't; any case")
NEED_UNITS = ("L", "KG", "SALES_UNIT") + PACK_CODES


def normalize_unit(unit):
    """'st' / 'PCS' / 'KPL' -> 'ST', 'pack(s)' -> 'SALES_UNIT', 'l' -> 'L', 'kg' -> 'KG', 'ki' -> 'KI'; anything else
    unchanged (and then rejected by the Unit type)."""
    if isinstance(unit, str):
        return UNIT_ALIASES.get(unit.strip().upper(), unit)
    return unit


def is_packs(unit: str | None) -> bool:
    """A need counted in packs (sales units or a Valio unit code), not in L or KG."""
    return unit is None or unit == "SALES_UNIT" or unit in PACK_CODES


# Valio's codes for one consumer piece (a jar, bottle, bag, can, box ...). A product sold per KI of 6 jars often
# has no ST at all, only PRK (purkki) at ratio 1: then '20 ST' means 20 of those jars, not 20 cartons.
PIECE_CODES = ("PRK", "PAK", "BOT", "BAG", "PSS", "PLO", "RS", "RAS", "TLK", "KAN", "DOL", "PNT", "ZML", "NIP")


def piece_unit(product: dict | None) -> str | None:
    """The product's one consumer-piece unit when it has no ST: exactly one unit other than the sales unit with
    ratio 1 to the base unit and a piece-type code (PRK on a KI of 6 jars), else the sales unit itself when that is
    a single piece (sold per PRK). None when there is no unambiguous piece."""
    if not product:
        return None
    units, su = product.get("units") or {}, product.get("sales_unit")
    if "ST" in units:
        return "ST"
    ones = [c for c, v in units.items() if c != su and v == 1]
    if len(ones) == 1 and ones[0] in PIECE_CODES and units.get(su):
        return ones[0]
    if not ones and su in PIECE_CODES and units.get(su) == 1:
        return su
    return None


def resolve_pack_unit(product: dict | None, unit: str | None) -> tuple[str | None, float, str]:
    """How a need in pack unit `unit` is read for this product: (the product's real unit, how many of it one sales
    unit is, how). how is 'sales' (SALES_UNIT / no unit / the sales unit), 'exact' (the product has that code),
    'piece' (ST read as the product's piece unit, e.g. PRK) or 'fallback' (no such unit: read as sales units)."""
    su = (product or {}).get("sales_unit")
    if not product or unit in (None, "SALES_UNIT") or unit not in PACK_CODES or unit == su:
        return su if product else unit, 1.0, "sales"
    units = product.get("units") or {}
    if units.get(unit) and units.get(su):
        return unit, units[su] / units[unit], "exact"
    if unit == "ST" and (piece := piece_unit(product)) and units.get(piece) and units.get(su):
        return piece, units[su] / units[piece], "piece"
    return su, 1.0, "fallback"


def packs_per_sales_unit(product: dict | None, unit: str | None) -> float:
    """How many `unit` one sales unit of the product is: 6 for ST on a KI of 6 jars (also when the jars are coded
    PRK and the product has no ST). 1 for SALES_UNIT, the sales unit itself, or a code the product doesn't define
    (then it means the sales unit, and pack_unit_note says so)."""
    return resolve_pack_unit(product, unit)[1]


def covers_unit(product: dict | None, unit: str | None) -> str | None:
    """The unit a pack need's `covers` is reported in: the product's real unit (PRK for ST read as jars, KI when
    ST fell back to sales units), never a unit the product doesn't have."""
    if unit not in PACK_CODES or not product:
        return unit
    return resolve_pack_unit(product, unit)[0]


def unit_note(asked: float, unit: str, qty: float, sales_unit: str, per: float) -> str:
    """'20 ST asked -> 4 KI (24 ST) ordered, rounded up to whole KI'."""
    got = round(qty * per, 3)
    return (f"{round(asked, 3):g} {unit} asked -> {qty:g} {sales_unit} ({got:g} {unit}) ordered"
            + (f", rounded up to whole {sales_unit}" if got > asked + 1e-9 else ""))


def pack_unit_note(product: dict, asked: float, unit: str | None, qty: float) -> str | None:
    """The unit_note for a need in a Valio pack code, or None when the reading is plain (sales units, 1:1)."""
    if unit not in PACK_CODES:
        return None
    real, per, how = resolve_pack_unit(product, unit)
    su = product.get("sales_unit")
    if how == "fallback":
        units, base = product.get("units") or {}, product.get("base_unit")
        inner = units.get(su, 0) / units[base] if base and base != su and units.get(base) else 0
        return (f"{unit} is not a unit of this product; read as {qty:g} {su}"
                + (f" = {qty * inner:g} {base}" if inner > 1 else "")
                + (f" (asked {round(asked, 3):g} {unit})" if abs(qty - asked) > 1e-9 else ""))
    if how == "piece":
        if per == 1:
            return f"{unit} read as {real}, this product's piece unit: {round(asked, 3):g} {unit} -> {qty:g} {su}"
        return f"{unit} read as {real} (this product's piece unit): " + unit_note(asked, real, qty, su, per)
    if how == "exact" and per != 1:
        return unit_note(asked, unit, qty, su, per)
    return None


# The schema lists the common choices (agents and LLMs see a short enum); the other Valio codes are accepted too.
Unit = Annotated[Literal[NEED_UNITS], BeforeValidator(normalize_unit),
                 WithJsonSchema({"type": "string", "enum": ["L", "KG", "SALES_UNIT", "ST"], "description": UNIT_DOC})]

STOCK_PLENTY = 1e9  # Stock lines report 9999999999 when there is effectively no limit
TERMINAL_UNKNOWN = 999999  # Terminal (supplier-shipped) lines always report 999999 = unknown
LOW_RELIABILITY = 0.9

BLOCKING = "blocking"  # the line cannot be ordered as quoted
WARNING = "warning"  # orderable, but the mandate may require a human to look
INFO = "info"  # for the reader only; no mandate rule acts on it


@dataclass
class Issue:
    code: str
    severity: str
    message: str


def _dt(s: str | None) -> datetime | None:
    """Parse Valio Aimo's timestamps; one without an offset is Helsinki local time."""
    if not s:
        return None
    d = datetime.fromisoformat(s.replace("+0200", "+02:00").replace("+0300", "+03:00"))
    return d if d.tzinfo else d.replace(tzinfo=config.TZ)


def _name_pack_count(product: dict) -> int | None:
    """The N in an "N x size" pack written in the product name, if any."""
    names = product.get("names") or {}
    for text in (names.get("fi"), names.get("en"), product.get("name")):
        for m in catalog._SIZE_RE.finditer(text or ""):
            if m.group(1):
                return int(m.group(1))
    return None


def content_per_sales_unit(product: dict, price: dict | None) -> tuple[float, str, str] | None:
    """How many L or KG one sales unit holds: (amount, unit, source).

    Two independent sources: the product name and pack hierarchy, and Valio
    Aimo's own price / comparison-price ratio. The name is exact when it agrees
    with the ratio (within 5 %); otherwise Valio Aimo's ratio wins.
    """
    from_name = None
    if product["size_from_name"]:
        size, unit = product["size_from_name"]
        units = product["units"]
        factor = units.get(product["sales_unit"], 1.0) / units.get(product["base_unit"], 1.0)
        # "4x1kg" sold per KI of 4 bags: the name already describes the whole sales unit; don't multiply again.
        if factor > 1 and _name_pack_count(product) == round(factor):
            factor = 1.0
        from_name = (size * factor, unit)
    from_price = None
    if price and not price.get("error") and price.get("comparisonUnit"):
        cmp_ = (price.get("comparisonPrice") or {}).get("value")
        cat = (price.get("catalogPrice") or {}).get("value")
        if cmp_ and cat:
            from_price = (cat / cmp_, price["comparisonUnit"])
    if product["sales_unit"] == "KG":
        return 1.0, "KG", "sold by weight"
    if from_name and from_price and from_name[1] == from_price[1]:
        if abs(from_name[0] - from_price[0]) <= 0.05 * from_price[0]:
            return round(from_name[0], 3), from_name[1], "pack size, confirmed by price ratio"
        return round(from_price[0], 2), from_price[1], "Valio Aimo comparison price ratio"
    if from_price:
        return round(from_price[0], 2), from_price[1], "Valio Aimo comparison price ratio"
    if from_name:
        return round(from_name[0], 3), from_name[1], "pack size from product name"
    return None


# Typical densities (kg per litre) of liquids kitchens order by weight ("40 kg kermaa"), keyed by the product type
# read from the product's own name (substitutes.product_type). Used only when that type is known; the line then says
# what it assumed (unit_note) and carries UNIT_INTERPRETED, so a human confirms (rule unit.interpreted, ask).
# Values: common dairy-technology and food-composition figures at fridge/room temperature (milk 1.028-1.035; cream
# falls with fat, ~1.00 at 35-40 %, ~1.02 at 10-20 %; yoghurt and soured milks ~1.03-1.04; vegetable oils 0.91-0.92;
# fruit juices ~1.04-1.05), rounded to two decimals. An assumption, not a measurement: never applied silently.
DENSITY_BASIS = "typical density for the product type (sous/quote.py DENSITY); an assumption a human confirms"
DENSITY: dict[str, float] = {
    "maito": 1.03, "maitojuoma": 1.03, "kahvimaito": 1.03, "piimä": 1.03, "kefiiri": 1.03, "viili": 1.03,
    "jogurtti": 1.03, "jogurttijuoma": 1.03, "kaakaojuoma": 1.04,
    "kerma": 1.02, "ruokakerma": 1.02, "kahvikerma": 1.02, "kuohukerma": 1.00,
    "öljy": 0.92, "mehu": 1.04, "mehujuoma": 1.04,
}


def density(product: dict | None) -> tuple[float, str] | None:
    """(kg per L, type label) for a product whose type has a typical density, else None. Plant-based drinks and
    creams (oat 'cream') are not dairy: no density is assumed for them."""
    if not product:
        return None
    from .substitutes import product_type  # substitutes imports this module
    t = product_type(product)
    if t.get("key") in DENSITY and not t.get("plant_based"):
        return DENSITY[t["key"]], t.get("label") or t["key"]
    return None


def need_factor(need_unit: str, content_unit: str, kg_per_l: float) -> float:
    """What one need unit is in the product's content unit: 1 kg of cream is 1/1.02 L; 1 L of oil is 0.92 kg."""
    if need_unit == content_unit:
        return 1.0
    return 1 / kg_per_l if need_unit == "KG" else kg_per_l


def need_content(o: dict, need_unit: str | None) -> float | None:
    """How much of the need (in L or KG) one sales unit of an offer covers, through its density when it has one."""
    c = o.get("content_per_sales_unit") or {}
    if not c.get("amount"):
        return None
    if c["unit"] == need_unit:
        return c["amount"]
    if (d := o.get("density")) and need_unit in ("L", "KG"):
        return c["amount"] / need_factor(need_unit, c["unit"], d["kg_per_l"])
    return None


def round_to_lot(qty: float, sales_unit: str, lot: float | None) -> float:
    if sales_unit != "KG":
        qty = math.ceil(qty - 1e-9)
    lot = lot or 0.01
    if lot >= 1 or sales_unit == "KG":
        qty = math.ceil(qty / lot - 1e-9) * lot
    return round(qty, 2)


def build_offer(
    product: dict,
    price: dict | None,
    avail: dict | None,
    requested: date,
    need: float | None,
    need_unit: str | None,
    now: datetime,
) -> dict:
    """One product's offer for a need. `need_unit` is 'L', 'KG' or None (= sales units)."""
    issues: list[Issue] = []
    su = product["sales_unit"]

    # --- content and quantity
    content = content_per_sales_unit(product, price)
    qty = None
    per = packs_per_sales_unit(product, need_unit)  # need_unit per sales unit when the need is in packs
    note = None
    if need is not None:
        if is_packs(need_unit):
            qty = round_to_lot(need / per, su, product["allowed_lot_size"])
            note = pack_unit_note(product, need, need_unit, qty)
            if resolve_pack_unit(product, need_unit)[2] == "fallback":
                # the product has no such unit (or no single piece unit for ST): the gateway is guessing what
                # the buyer meant, so a human confirms (mandate rule unit.interpreted, ask)
                issues.append(Issue("UNIT_INTERPRETED", WARNING, note + ". A human confirms the quantity."))
        elif content and content[1] == need_unit:
            qty = round_to_lot(need / content[0], su, product["allowed_lot_size"])
        elif content and (dens := density(product)) and {need_unit, content[1]} == {"L", "KG"}:
            # 40 kg of cream: read as litres at the product type's typical density, and a human confirms it
            factor = need_factor(need_unit, content[1], dens[0])
            qty = round_to_lot(need * factor / content[0], su, product["allowed_lot_size"])
            note = (f"{need:g} {need_unit.lower() if need_unit == 'KG' else need_unit} read as "
                    f"{round(need * factor, 1):g} {content[1] if content[1] == 'L' else 'kg'} of {dens[1]} at "
                    f"{dens[0]:g} kg/L (typical density, an assumption)")
            issues.append(Issue("UNIT_INTERPRETED", WARNING, note + ". A human confirms the quantity."))
        elif content:
            issues.append(Issue("UNIT_MISMATCH", BLOCKING,
                                f"Need is in {need_unit} but this product is measured in {content[1]}"
                                + (f"; ask in {content[1]} (or SALES_UNIT): no typical density is known for this "
                                   "product's type, so the gateway won't convert." if need_unit in ("L", "KG")
                                   and content[1] in ("L", "KG") else ".")))
        else:
            issues.append(Issue("CONTENT_UNKNOWN", BLOCKING,
                                f"Can't tell how many {need_unit} one {su} holds, so the quantity can't be computed."))
        if qty is not None and (product["allowed_lot_size"] or 0) >= 1:
            issues.append(Issue("LOT_SIZE", WARNING,
                                f"Sold in lots of {product['allowed_lot_size']:g} {su}; quantity rounded up to {qty:g}."))

    # --- price
    unit_ex = unit_inc = vat = None
    if not price or price.get("error") or not price.get("catalogPrice"):
        issues.append(Issue("NO_PRICE", BLOCKING, "Valio Aimo returned no price for this product."))
    else:
        cp = price["catalogPrice"]
        unit_ex, unit_inc = cp["value"], cp["valueWithTax"]
        vat = (price.get("tax") or {}).get("taxRate")
        valid_to = _dt(cp.get("validTo"))
        if valid_to and valid_to.date() < requested:
            issues.append(Issue("PRICE_EXPIRES", WARNING,
                                f"Price is valid until {valid_to.date()}, before the delivery date {requested}."))

    # --- availability
    a = avail or {}
    lines = a.get("scheduleLines") or []
    sl = lines[0] if lines else {}
    model = a.get("logisticModel")
    closing = _dt(a.get("closingTime"))
    available_amount = sl.get("availableAmount")
    available_date = date.fromisoformat(sl["availableDate"]) if sl.get("availableDate") else None
    # Valio Aimo (Tommi, 14.11): availableAmount >= 999999 on ANY line means "unknown, any quantity may be ordered"
    stock_known = model == "Stock" and available_amount is not None and available_amount < TERMINAL_UNKNOWN
    if closing and available_date and available_date < requested:
        # closingTime is the cut-off for the earliest date (availableDate), not for the later date asked for.
        closing = None
    if not avail:
        issues.append(Issue("NO_AVAILABILITY", BLOCKING, "Valio Aimo returned no availability for this product."))
    elif a.get("salesBlock") or sl.get("salesBlock"):
        issues.append(Issue("SALES_BLOCK", BLOCKING, "The product is sales-blocked and can't be ordered right now."))
    elif not lines:
        issues.append(Issue("UNDELIVERABLE", BLOCKING, "Valio Aimo can't deliver this product on any date."))
    elif not available_date or available_amount == 0:
        issues.append(Issue("OUT_OF_STOCK", BLOCKING, "Out of stock: no delivery date available."))
    else:
        if available_date < requested:
            issues.append(Issue("CUTOFF_UNKNOWN", WARNING,
                                f"Valio Aimo reported the order cut-off for {available_date} only; the cut-off for "
                                f"delivery on {requested} isn't known."))
        cutoff_passed = bool(closing and closing <= now)
        if cutoff_passed and available_date > requested:
            # Name the date the buyer asked for; the reported earliest date is unreachable too (its cut-off).
            issues.append(Issue("CUTOFF_PASSED", BLOCKING,
                                f"Delivery on {requested} can't be ordered: Valio Aimo's earliest date for it is "
                                f"{available_date}, and the order cut-off for that date was "
                                f"{closing.astimezone(config.TZ):%a %d.%m %H:%M}, which has already passed. "
                                "So the earliest possible date is later still: the quote's next_date option names it when Valio has one."))
        elif cutoff_passed:
            issues.append(Issue("CUTOFF_PASSED", BLOCKING,
                                f"The order cut-off for delivery on {requested} was "
                                f"{closing.astimezone(config.TZ):%a %d.%m %H:%M}, which has already passed. "
                                "The requested date can't be met even though the API still reports it."))
        if available_date > requested:
            issues.append(Issue("LATER_DATE", WARNING,
                                f"Delivery on {requested} isn't possible; the earliest date Valio Aimo reports is "
                                f"{available_date}{' (its cut-off has passed too)' if cutoff_passed else ''}."))
        if model == "Terminal":
            issues.append(Issue("STOCK_UNKNOWN", WARNING,
                                "Shipped from the supplier's warehouse (Terminal): the quantity in stock is unknown."))
        elif stock_known and qty is not None and available_amount < qty:
            issues.append(Issue("PARTIAL_STOCK", WARNING,
                                f"Only {available_amount:g} {sl.get('salesUnit', su)} in stock; {qty:g} needed."))
    # --- Valio's own hold on the product (recall, quality hold, sales stop: sous/holds.py)
    if hold := holds.active_for(product["id"]):
        issues.append(Issue("PRODUCT_HELD", BLOCKING, holds.message(hold)))
    # --- last-known data served because Valio Aimo couldn't be reached (see aimo.py)
    stale = {k: d["_stale_seconds"] for k, d in (("price", price), ("availability", avail))
             if d and d.get("_stale_seconds") is not None}
    if stale:
        why = ("Valio rate limit" if any((d or {}).get("_stale_reason") == "rate_limit" for d in (price, avail))
               else "Valio Aimo unreachable")
        mins = max(1, round(max(stale.values()) / 60))
        issues.append(Issue("DATA_STALE", WARNING,
                            f"Live {' and '.join(stale)} couldn't be refreshed ({why}); using data from "
                            f"{mins} min ago. The cut-off is still checked against now."))
    # --- a judge sandbox over our OWN call budget got a recent copy instead of a refusal (aimo._cached)
    cached = {k: d for k, d in (("price", price), ("availability", avail)) if d and d.get("_cached_seconds") is not None}
    cached_at = min((d["_cached_at"] for d in cached.values()), default=None)
    if cached and not stale:
        mins = max(1, round(max(d["_cached_seconds"] for d in cached.values()) / 60))
        issues.append(Issue("DATA_CACHED", INFO,
                            f"{' and '.join(cached).capitalize()} from the gateway's cache ({cached_at}, {mins} min "
                            "old, at most 10): the sandbox's live Valio budget for this minute was used up. Valio's "
                            "mock data is static; the cut-off is still checked against now."))
    rel = product["delivery_reliability"]
    if rel is not None and rel < LOW_RELIABILITY:
        issues.append(Issue("LOW_RELIABILITY", WARNING,
                            f"Historically delivered in full only {rel:.0%} of the time."))

    delivery_date = max(requested, available_date) if available_date else None
    covered = qty * content[0] if (qty is not None and content and content[1] == need_unit) else None
    dens = density(product) if (qty is not None and content and need_unit in ("L", "KG")
                                and content[1] != need_unit) else None
    if dens:
        covered = qty * content[0] / need_factor(need_unit, content[1], dens[0])
    co2 = None
    if product["co2_kg_per_kg_group_avg"] is not None and covered is not None:
        # L of a drink ~ 1 kg; flagged as a product-group estimate either way.
        co2 = round(product["co2_kg_per_kg_group_avg"] * covered, 1)
    if qty is not None and need_unit in PACK_CODES:
        covered = qty * per  # in the unit asked for: 4 KI cover 24 ST

    return {
        "product_id": product["id"],
        "name": product["name"],
        "brand": product["brand"],
        "sales_unit": su,
        "allowed_lot_size": product["allowed_lot_size"],
        "content_per_sales_unit": {"amount": content[0], "unit": content[1], "source": content[2]} if content else None,
        **({"density": {"kg_per_l": dens[0], "type": dens[1], "basis": DENSITY_BASIS}} if dens else {}),
        "quantity": qty,
        "covers": ({"amount": round(covered, 2), "unit": covers_unit(product, need_unit)}
                   if covered is not None else None),
        **({"unit_note": note} if note else {}),
        "unit_price_ex_vat": unit_ex,
        "unit_price_inc_vat": unit_inc,
        "vat_rate": vat,
        "comparison_price": {
            "ex_vat": (price.get("comparisonPrice") or {}).get("value"),
            "per": price.get("comparisonUnit"),
        } if price and not price.get("error") else None,
        "list_price_ex_vat": (price.get("listPrice") or {}).get("value") if price else None,
        "line_total_ex_vat": round(unit_ex * qty, 2) if unit_ex is not None and qty is not None else None,
        "line_total_inc_vat": round(unit_inc * qty, 2) if unit_inc is not None and qty is not None else None,
        "logistic_model": model,
        "available_amount": available_amount if stock_known else None,
        "stock": "known" if stock_known else ("plenty" if model == "Stock" else "unknown"),
        # what agents see under SOUS_STOCK_EXPOSURE=buckets (sous/exposure.py); available_amount may be withheld
        "stock_level": exposure.level(model, available_amount, qty, bool(available_date)),
        "available_date": available_date.isoformat() if available_date else None,
        "delivery_date": delivery_date.isoformat() if delivery_date else None,
        "order_cutoff": closing.isoformat() if closing else None,
        "delivery_reliability": rel,
        "co2_estimate_kg": co2,
        "co2_note": "product-group average x quantity, an estimate" if co2 is not None else None,
        "country_of_origin": origin.code(product.get("country_of_origin")),
        "origin_name": origin.name(origin.code(product.get("country_of_origin"))) if origin.code(product.get("country_of_origin")) else None,
        "origin_basis": origin.BASIS,
        "labels": product["labels"],
        "claims": product["claims"],
        "allergens": product["allergens"],
        "temperature": product["temperature"],
        **({"cached_at": cached_at} if cached else {}),
        "orderable": not any(i.severity == BLOCKING for i in issues),
        "issues": [asdict(i) for i in issues],
    }


def offers(
    product_ids: list[str],
    requested: date,
    need: float | None = None,
    need_unit: str | None = None,
) -> tuple[list[dict], datetime]:
    """Price + availability for a shortlist (two batched API calls at most)."""
    products = {pid: p for pid in product_ids if (p := catalog.get(pid))}
    ids = list(products)
    if not ids:
        return [], clock.now()
    prices = aimo.prices(ids, requested)
    avail = aimo.availability(ids, requested)
    now = clock.now()
    out = [build_offer(products[i], prices.get(i), avail.get(i), requested, need, need_unit, now) for i in ids]
    return out, now


def prefetch(product_ids: list[str], requested: date) -> None:
    """A basket: fetch every candidate's price and availability in one batched call each, so the per-item
    offers() calls are served from aimo's cache (2 Valio calls per basket, not 2 per item). Best effort: if
    it fails, offers() makes its own calls and handles the error as before."""
    ids = list(dict.fromkeys(pid for pid in product_ids if catalog.get(pid)))
    if not ids:
        return
    try:
        aimo.prices(ids, requested)
        aimo.availability(ids, requested)
    except aimo.AimoError:
        pass


def rank_key(o: dict):
    """Cheapest per L/KG first among orderable offers; reliability breaks ties."""
    cp = (o.get("comparison_price") or {}).get("ex_vat")
    per_unit = cp if cp is not None else (o["unit_price_ex_vat"] or math.inf)
    return (not o["orderable"], per_unit, -(o["delivery_reliability"] or 0.95))


STOCK_SIZING_WITHHELD = ("Stock is short for this quantity. The exact amount that can be supplied (and a split "
                         "across products) is shown only to an authenticated agent quoting under a mandate.")


def plan(need: float, need_unit: str | None, candidates: list[dict], sized: bool = True) -> tuple[list[dict], float]:
    """Fill a need from ranked orderable offers, splitting across products when stock runs short.

    sized=False (a mandate-less quote under the buckets stock policy, sous/exposure.py): the cheapest orderable
    offer takes the whole need, never capped at its stock, so the line quantity can't reveal the exact stock;
    a short line keeps PARTIAL_STOCK and gets STOCK_SIZING_WITHHELD.
    A need in L or KG that the cheapest packs overshoot by more than config.PACK_OVERSIZE_PCT (12 kg -> one 20 kg
    bag) is planned again with each candidate first and smallest packs first; the plan with the least surplus wins
    when it covers as much and costs at most config.PACK_OVERSIZE_PRICE_PCT more. A plan still over the threshold
    carries PACK_OVERSIZE (a warning; mandate rule quantity.pack_oversize, notify) on its lines.
    Returns (lines, uncovered amount in need_unit).
    """
    ranked = sorted(candidates, key=rank_key)
    lines, left = _greedy(need, need_unit, ranked, sized)
    if is_packs(need_unit) or not lines or _surplus(lines, need) <= need * config.PACK_OVERSIZE_PCT / 100 + 1e-9:
        return lines, left
    cost = _cost(lines)
    best = (_surplus(lines, need), cost, lines, left)
    usable = [o for o in ranked if o["orderable"] and o["quantity"] is not None]
    orders = [[o] + [x for x in ranked if x is not o] for o in usable[1:]]
    orders.append(sorted(usable, key=lambda o: ((o["content_per_sales_unit"] or {}).get("amount") or math.inf,
                                                rank_key(o))))
    for order in orders:
        alt, alt_left = _greedy(need, need_unit, order, sized)
        if not alt or alt_left > left + 1e-9 or _cost(alt) > cost * (1 + config.PACK_OVERSIZE_PRICE_PCT / 100) + 1e-9:
            continue
        if (s := _surplus(alt, need), c := _cost(alt)) < best[:2]:
            best = (s, c, alt, alt_left)
    surplus, _, lines, left = best
    if surplus > need * config.PACK_OVERSIZE_PCT / 100 + 1e-9:
        got, u = need + surplus, "kg" if need_unit == "KG" else "L"
        msg = (f"{round(got, 2):g} {u} for a {round(need, 2):g} {u} need (+{surplus / need:.0%}): the smallest orderable "
               f"packs that cover it; no other plan comes closer for at most {config.PACK_OVERSIZE_PRICE_PCT:g}% more.")
        for l in lines:
            l["issues"] = l["issues"] + [asdict(Issue("PACK_OVERSIZE", WARNING, msg))]
    return lines, left


def _surplus(lines: list[dict], need: float) -> float:
    return max(0.0, round(sum((l.get("covers") or {}).get("amount") or 0 for l in lines) - need, 3))


def _cost(lines: list[dict]) -> float:
    return round(sum(l["line_total_ex_vat"] or 0 for l in lines), 2)


def _greedy(need: float, need_unit: str | None, ranked: list[dict], sized: bool) -> tuple[list[dict], float]:
    """Fill the need from offers in the given order (see plan)."""
    remaining = need
    lines = []
    for o in ranked:
        if remaining <= 1e-9 or not o["orderable"] or o["quantity"] is None:
            continue
        product = catalog.get(o["product_id"])
        content = packs_per_sales_unit(product, need_unit) if is_packs(need_unit) else need_content(o, need_unit)
        if not content:
            continue
        lot = o.get("allowed_lot_size")
        want = round_to_lot(remaining / content, o["sales_unit"], lot)
        if sized and o["available_amount"] is not None:
            want = min(want, _floor_to_lot(o["available_amount"], o["sales_unit"], lot))
        if want <= 0:
            continue
        line = dict(o)
        line["quantity"] = want
        line["line_total_ex_vat"] = round(o["unit_price_ex_vat"] * want, 2)
        line["line_total_inc_vat"] = round(o["unit_price_inc_vat"] * want, 2)
        line["covers"] = {"amount": round(want * content, 2), "unit": covers_unit(product, need_unit)}
        if not o.get("density"):  # a density reading's note describes the whole need and stays
            line.pop("unit_note", None)
        if need_unit in PACK_CODES and product and (
                note := pack_unit_note(product, min(remaining, want * content), need_unit, want)):
            line["unit_note"] = note
        if sized:
            line["issues"] = [i for i in o["issues"] if i["code"] != "PARTIAL_STOCK"]
        else:
            line["stock_level"] = exposure.level(o["logistic_model"], o["available_amount"], want,
                                                 o["available_date"] is not None)
            if line["stock_level"] in ("short", "none"):
                line["issues"] = o["issues"] + [asdict(Issue("STOCK_SIZING_WITHHELD", WARNING, STOCK_SIZING_WITHHELD))]
        lines.append(line)
        remaining -= want * content
    return lines, max(0.0, round(remaining, 3))


def _floor_to_lot(stock: float, sales_unit: str, lot: float | None) -> float:
    """The most that can be ordered from `stock` in whole lots (rounding down, unlike round_to_lot)."""
    if lot and lot >= 1:
        return math.floor(stock / lot + 1e-9) * lot
    if sales_unit == "KG":
        step = lot or 0.01
        return round(math.floor(stock / step + 1e-9) * step, 2)
    return math.floor(stock)


def quote_expiry(now: datetime, lines: list[dict]) -> datetime:
    exp = now + timedelta(seconds=config.QUOTE_TTL_SECONDS)
    for l in lines:
        if l.get("order_cutoff"):
            exp = min(exp, datetime.fromisoformat(l["order_cutoff"]))
    return exp
