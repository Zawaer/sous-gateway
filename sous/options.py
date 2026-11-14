"""Response options: what the buyer can do when an item can't be covered as asked.

When a quote can't cover an item in full by the requested date (stock short, earliest date later
than asked, cut-off passed, undeliverable...), the quote lists `options[]` for that item. Each option
is a complete alternative with its own lines, totals, delivery dates and a plain-language trade-off:

  on_time                 another of the item's candidates covers everything by the requested date
  partial_now_rest_later  what can arrive on the requested date, the rest on a later date (two deliveries)
  all_later               everything from products that arrive later, complete by one later date
  split_with_substitute   what the original covers on time, the gap from a compatible substitute
  substitute              a substitute (good / better / best from substitutes.py) covers everything

A need in packs (unit None) is met by a substitute with equal content, not an equal number of its own
packs: 40 x 400 g is 16 kg, so 2 x 10 kg, not 40 x 10 kg (the same conversion as substitutes.suggest).
Such options carry `covers_content` ({amount, unit} in L or KG) and say it in the trade-off; their `covers`
stays in the original's packs (content / one original pack). Option lines are in the substitute's own
packs, which is what a re-quote with option_id orders (SALES_UNIT).

Ranking: no substitute before substitute; then fully on time > part on time + rest later > later;
then the substitute tier (best > better > good); then price. Each option carries a mandate preview
(the tier the whole quote would get with this option in place of the item's lines).

Honest about availability: Valio Aimo reports only each product's earliest date and the amount
available then. Stock on a later date isn't known, so the remainder of a short product is never
assumed to arrive later from the same product; it comes from other products whose own earliest
date and amount cover it. Plain code, no LLM.
"""

from __future__ import annotations

from datetime import date
from typing import Callable

from . import catalog
from . import quote as Q

MAX_PER_ITEM = 5
_TIER_RANK = {"best": 0, "better": 1, "good": 2}
_MATCH_RANK = {"same": 0, "compatible": 1, "unknown": 2, "different": 3}
_COMPACT = ("product_id", "name", "quantity", "sales_unit", "unit_price_ex_vat", "line_total_ex_vat",
            "line_total_inc_vat", "covers", "available_amount", "delivery_date", "order_cutoff", "logistic_model",
            "substitute_for")


def trouble(lines: list[dict], uncovered: float, requested: date) -> list[str]:
    """Why an item can't be covered as asked (empty: it can)."""
    why = []
    if uncovered > 0:
        why.append("uncovered")
    if any(not l["orderable"] for l in lines):
        why.append("unorderable")
    codes = {i["code"] for l in lines for i in l["issues"]}
    if "PARTIAL_STOCK" in codes:
        why.append("partial_stock")
    if "LATER_DATE" in codes or any(l.get("delivery_date") and l["delivery_date"] > requested.isoformat()
                                    for l in lines if l["orderable"]):
        why.append("later_date")
    return why


def _late(o: dict, requested: date) -> bool:
    return (o.get("delivery_date") or "9999") > requested.isoformat()


def _fill(amount: float, unit: str | None, pool: list[dict], substitute_for: str | None = None) -> tuple[list[dict], float]:
    lines, left = Q.plan(amount, unit, pool)
    if substitute_for:
        for l in lines:
            l["substitute_for"] = substitute_for
    return lines, left


def _covered(lines: list[dict], unit: str | None) -> float:
    if unit is None:
        return round(sum(l["quantity"] for l in lines), 3)
    return round(sum((l.get("covers") or {}).get("amount") or 0 for l in lines), 3)


def _content_of(original_id: str | None, offers: list[dict]) -> tuple[float, str] | None:
    """What one pack of the original holds (amount, 'L' or 'KG'): from its offer, else its catalog entry."""
    o = next((o for o in offers if o["product_id"] == original_id), None)
    c = (o or {}).get("content_per_sales_unit")
    if c and c.get("amount"):
        return c["amount"], c["unit"]
    p = catalog.get(original_id) if original_id else None
    c2 = Q.content_per_sales_unit(p, None) if p else None
    return (c2[0], c2[1]) if c2 else None


def _packs(lines: list[dict]) -> str:
    return " + ".join(f"{l['quantity']:g} {l['sales_unit']}" for l in lines)


def _sig(lines: list[dict]) -> tuple:
    return tuple(sorted((l["product_id"], l["quantity"]) for l in lines))


def _say(phrases: list[str]) -> str:
    return "; ".join(x.rstrip(". ") for x in phrases)


def _names(lines: list[dict]) -> str:
    return ", ".join(dict.fromkeys(l["name"] for l in lines))


def build(
    item_index: int,
    amount: float,
    unit: str | None,
    offers: list[dict],
    chosen: list[dict],
    requested: date,
    substitutes: list[dict] | None = None,
    original_id: str | None = None,
) -> list[dict]:
    """Options for one item, best first. `offers` are the item's own candidates (quote.offers);
    `substitutes` are substitutes.suggest()['options'] for `original_id` (or None when not looked up).
    Each option keeps its full lines under `_lines` (for the mandate preview); `public()` drops them."""
    u = "units" if unit is None else unit
    req = requested.isoformat()
    usable = [o for o in offers if o["orderable"] and o["quantity"] is not None]
    on_time = [o for o in usable if not _late(o, requested)]
    later = [o for o in usable if _late(o, requested)]
    quoted = _sig(chosen) if chosen and all(l["orderable"] for l in chosen) else None
    seen: set[tuple] = set()
    out: list[dict] = []

    def add(kind: str, lines: list[dict], tradeoff: str, substitute: dict | None = None,
            covers: float | None = None, content: dict | None = None):
        if not lines or _sig(lines) in seen:
            return
        seen.add(_sig(lines))
        dates = sorted({l["delivery_date"] for l in lines if l.get("delivery_date")})
        late = [l for l in lines if _late(l, requested)]
        timing = 0 if not late else (1 if len(late) < len(lines) else 2)
        total = round(sum(l["line_total_ex_vat"] or 0 for l in lines), 2)
        out.append({
            "item_index": item_index, "kind": kind, "tradeoff": f"{tradeoff} Total €{total:,.2f} ex VAT.",
            "covers": {"amount": _covered(lines, unit) if covers is None else covers, "unit": unit or "SALES_UNIT"},
            **({"covers_content": content} if content else {}),
            "on_time": timing == 0, "delivery_dates": dates,
            "total_ex_vat": total, "total_inc_vat": round(sum(l["line_total_inc_vat"] or 0 for l in lines), 2),
            "substitute": substitute, "same_as_quote": _sig(lines) == quoted, "_timing": timing, "_lines": lines,
        })

    # 1. another candidate covers everything on time
    lines, left = _fill(amount, unit, on_time)
    if lines and left <= 0:
        add("on_time", lines, f"All {amount:g} {u} on {req} from {_names(lines)}.")

    # 2. what arrives on time, the rest from products whose own earliest date is later
    now_lines, gap = _fill(amount, unit, on_time)
    if now_lines and gap > 0:
        used = {l["product_id"] for l in now_lines}
        rest, left = _fill(gap, unit, [o for o in later if o["product_id"] not in used])
        if rest and left <= 0:
            last = max(l["delivery_date"] for l in rest)
            add("partial_now_rest_later", now_lines + rest,
                f"{_covered(now_lines, unit):g} {u} arrive on {req} ({_names(now_lines)}); the remaining "
                f"{_covered(rest, unit):g} {u} arrive by {last} ({_names(rest)}). Two deliveries.")

    # 3. everything from later products, complete by the earliest date that covers it all
    for d in sorted({o["delivery_date"] for o in later}):
        lines, left = _fill(amount, unit, [o for o in later if o["delivery_date"] <= d])
        if lines and left <= 0:
            days = (date.fromisoformat(d) - requested).days
            add("all_later", lines, f"All {amount:g} {u} by {d}, {days} day{'s' * (days != 1)} later than asked "
                                    f"({_names(lines)}).")
            break

    # 4. substitutes: fill the gap next to the original, or replace it outright. A need in the original's
    # packs becomes what those packs hold, so a substitute in other packs covers the same content.
    subs = [s for s in substitutes or [] if s["offer"]["orderable"] and s["offer"]["quantity"] is not None]
    per_pack = _content_of(original_id, offers) if unit is None and subs else None
    if per_pack:
        subs = [s for s in subs if (s["offer"].get("content_per_sales_unit") or {}).get("unit") == per_pack[1]]
    s_unit = per_pack[1] if per_pack else unit

    def s_fill(need: float, pool: list[dict]) -> tuple[list[dict], float, float]:
        """Substitute lines for `need` (in the item's unit); returns (lines, left, content covered in s_unit)."""
        lines, left = _fill(round(need * per_pack[0], 3) if per_pack else need, s_unit, pool, original_id)
        return lines, left, _covered(lines, s_unit)

    def in_packs(content: float) -> float:  # content back in the original's packs
        return round(content / per_pack[0], 3)

    def what(need: float, lines: list[dict]) -> str:
        if not per_pack:
            return f"{need:g} {u}"
        return (f"{need:g} {u} ({need * per_pack[0]:g} {s_unit}) as {_packs(lines)}, "
                f"{_covered(lines, s_unit):g} {s_unit}, of")

    if subs and original_id:
        by_id = {s["product_id"]: s for s in subs}
        compatible = [s["offer"] for s in subs if (s.get("type") or {}).get("match") != "different"]
        if now_lines and gap > 0:
            sub_lines, left, got = s_fill(gap, [o for o in compatible if not _late(o, requested)])
            if sub_lines and left <= 0:
                s0 = by_id[sub_lines[0]["product_id"]]
                now_n = _covered(now_lines, unit)
                add("split_with_substitute", now_lines + sub_lines,
                    f"{now_n:g} {u} of {_names(now_lines)} on {req}; the remaining "
                    f"{what(gap, sub_lines) if per_pack else f'{got:g} {u} from'} {_names(sub_lines)}, a substitute"
                    f"{': ' + _say(s0['compromises']) if s0['compromises'] else ' with no compromises'}.",
                    {"tier": s0["tier"], "for": original_id, "compromises": s0["compromises"],
                     "match": (s0.get("type") or {}).get("match")},
                    round(now_n + in_packs(got), 3) if per_pack else None,
                    {"amount": round(now_n * per_pack[0] + got, 3), "unit": s_unit} if per_pack else None)
        for s in subs:
            lines, left, got = s_fill(amount, [s["offer"]])
            if lines and left <= 0:
                when = "on " + req if not _late(lines[0], requested) else "on " + lines[0]["delivery_date"]
                add("substitute", lines,
                    f"{s['tier'].capitalize()} substitute: all {what(amount, lines) if per_pack else f'{amount:g} {u} of'} "
                    f"{s['name']} instead, arriving {when}; "
                    f"{_say(s['compromises']) if s['compromises'] else 'no compromises'}.",
                    {"tier": s["tier"], "for": original_id, "compromises": s["compromises"],
                     "match": (s.get("type") or {}).get("match")},
                    in_packs(got) if per_pack else None, {"amount": got, "unit": s_unit} if per_pack else None)

    out.sort(key=lambda o: (o["substitute"] is not None, o["_timing"], o["kind"] != "split_with_substitute",
                            _TIER_RANK.get((o["substitute"] or {}).get("tier"), 0),
                            _MATCH_RANK.get((o["substitute"] or {}).get("match"), 0),
                            len((o["substitute"] or {}).get("compromises") or []), o["total_ex_vat"]))
    return out[:MAX_PER_ITEM]


def finish(options: list[dict], quote_id: str, preview: Callable[[dict], tuple[str, list[str]]] | None) -> list[dict]:
    """Add the mandate preview and compact lines, move options the mandate would block to the end of their
    item, then number them: ids '<quote id>.O<n>', `rank` 1 = best within the item."""
    out = []
    for o in options:
        lines = o.pop("_lines")
        o.pop("_timing")
        if preview:
            tier, rules = preview(o | {"_lines": lines})
            o["mandate_preview"] = {"outcome": tier, "rules": rules}
        o["lines"] = [{k: l.get(k) for k in _COMPACT} | {"issues": [i["code"] for i in l["issues"]]} for l in lines]
        out.append(o)
    out.sort(key=lambda o: (o["item_index"], (o.get("mandate_preview") or {}).get("outcome") == "block"))
    ranks: dict[int, int] = {}
    for n, o in enumerate(out, 1):
        ranks[o["item_index"]] = ranks.get(o["item_index"], 0) + 1
        out[n - 1] = {"id": f"{quote_id}.O{n}", "rank": ranks[o["item_index"]], **o}
    return out
