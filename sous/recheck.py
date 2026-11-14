"""Live re-check at approval: a held order is only ordered at the price and stock Valio Aimo reports now.

An order held for a human can wait hours. Approving it used to re-run the mandate's rules on the signed quote, but
the quote's price and stock were from the moment it was issued. Now, when the human approves, the gateway asks
Valio Aimo again (price and availability for exactly the order's lines and delivery dates, two batched calls at
most, cached answers reused) and compares, line by line:

- a line that is no longer orderable as quoted (out of stock, less stock than ordered, sales block, cut-off passed,
  no price) -> the order becomes `expired` with that reason: nothing is ordered, the agent asks for a new quote;
- a unit price that moved by more than the mandate's `max_price_change_pct` (or SOUS_APPROVAL_PRICE_PCT, default
  20, when the mandate has none), up or down -> `expired` too: the human approved one price, Valio would charge
  another;
- a smaller move is accepted and recorded.

The result is kept in the order's `approval.recheck` ({at, lines: [{product_id, quoted, now, change_pct,
orderable}]}) and so signed into the receipt. If Valio Aimo can't be reached, the approval fails with a retryable
`valio_unavailable` and the order stays pending: an order is never placed on prices nobody could check.
A seeded sandbox quote (reference prices, no live quote behind it) is not compared; the record says so.
Off with SOUS_APPROVAL_RECHECK=0.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from . import config

DEFAULT_PCT = float(config.env("APPROVAL_PRICE_PCT", "20"))


def enabled() -> bool:
    return config.env("APPROVAL_RECHECK", "1") not in ("0", "false", "no")


def _pct(now: float, was: float) -> float:
    return float((Decimal(str(now)) - Decimal(str(was))) * 100 / Decimal(str(was))) if was else 0.0


def check(o: dict, q: dict, mandate, now: datetime) -> tuple[tuple[str, str, str] | None, dict]:
    """(blocker as (status, reason, reason_fi) or None, the record for approval.recheck). Raises aimo.AimoError
    (the caller turns it into a retryable error) when Valio Aimo can't be asked."""
    from . import quote as Q, sandbox
    record: dict = {"at": now.isoformat(timespec="milliseconds"), "lines": []}
    if q.get("seeded"):
        record["skipped"] = "seeded sandbox quote: reference prices, no live quote to compare with"
        return None, record
    if not enabled():
        record["skipped"] = "live re-check switched off (SOUS_APPROVAL_RECHECK=0)"
        return None, record
    limit = mandate.max_price_change_pct if getattr(mandate, "max_price_change_pct", None) else DEFAULT_PCT
    record["price_limit_pct"] = limit
    by_day: dict[str, list[dict]] = {}
    for l in o.get("lines") or []:
        day = l.get("delivery_date") or o.get("requested_delivery_date") or q.get("requested_delivery_date")
        by_day.setdefault(day, []).append(l)
    problems: list[tuple[str, str]] = []
    with sandbox.metered(mandate):
        for day, lines in by_day.items():
            when = date.fromisoformat(day)
            Q.prefetch([l["product_id"] for l in lines], when)
            for l in lines:
                offers, _ = Q.offers([l["product_id"]], when, float(l.get("quantity") or 0), None)
                off = offers[0] if offers else None
                quoted, fresh = l.get("unit_price_ex_vat"), (off or {}).get("unit_price_ex_vat")
                change = _pct(fresh, quoted) if quoted and fresh is not None else None
                blocking = [i for i in (off or {}).get("issues", []) if i.get("severity") == "blocking"]
                row = {"product_id": l["product_id"], "quoted_ex_vat": quoted, "now_ex_vat": fresh,
                       "change_pct": round(change, 1) if change is not None else None,
                       "orderable": bool(off and off.get("orderable") and not blocking)}
                record["lines"].append(row)
                name = l.get("name") or l["product_id"]
                if not row["orderable"]:
                    why = blocking[0]["message"] if blocking else "Valio Aimo no longer offers it"
                    problems.append((f"{name}: {why}", f"{name}: {why}"))
                elif change is not None and abs(Decimal(str(fresh)) - Decimal(str(quoted))) * 100 > \
                        Decimal(str(quoted)) * Decimal(str(limit)):
                    problems.append((
                        f"{name}: €{quoted:,.2f} when quoted, €{fresh:,.2f} now ({change:+.0f}%, the limit is "
                        f"{limit:g}%)",
                        f"{name}: {quoted:.2f} € tarjouksessa, nyt {fresh:.2f} € ({change:+.0f} %, raja {limit:g} %)"))
    if not problems:
        return None, record
    record["result"] = "expired"
    reason = ("Checked with Valio Aimo at approval: " + "; ".join(p[0] for p in problems)
              + ". Nothing was ordered at the old terms: ask for a new quote.")
    reason_fi = ("Tarkistettu Valio Aimolta hyväksyttäessä: " + "; ".join(p[1] for p in problems)
                 + ". Mitään ei tilattu vanhoilla ehdoilla: pyydä uusi tarjous.")
    return ("expired", reason, reason_fi), record
