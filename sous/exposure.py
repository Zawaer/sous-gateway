"""Stock exposure policy: how much of Valio Aimo's stock level an outside agent sees.

A wholesaler doesn't want any agent (a competitor's included) to scrape exact stock levels from quotes.
SOUS_STOCK_EXPOSURE (sous/config.py) sets the policy:

  buckets         (default) agents see `stock_level` only: ample | limited | short | none | unknown;
                  `available_amount` is null and stock numbers are taken out of issue messages
  exact           today's behaviour: `available_amount` and "Only N in stock" messages as Valio reports them
  approver_exact  buckets, except for callers with the global approver code (X-Approver-Code), who see exact

Levels compare the stock with what the buyer asked for, in the product's sales units:
  ample    stock >= 3 x the request, or >= AMPLE_ABSOLUTE units, or Valio reports no practical limit
  limited  covers the request, but not 3 x
  short    doesn't cover the request (under a mandate the quote splits, and the line quantity shows what can be had)
  none     nothing available (out of stock)
  unknown  shipped from the supplier's warehouse (Terminal), or no availability data

The decision engine (quote.plan, options, the mandate) always works from the exact numbers; `view()` is
applied once, to what leaves the gateway (quote bodies before they are signed, substitutes, suggestions).
The quantities a buyer orders are never hidden: a split line's quantity is what can be supplied.
Only under a mandate (or for the approver, or in exact mode): a mandate-less quote needs no credential, so it is
not sized to stock at all (quote.plan(sized=False), service._request_quote; docs/PROTOCOL.md 5.2).
"""

from __future__ import annotations

import re

from . import config

MODES = ("buckets", "exact", "approver_exact")
AMPLE_FACTOR = 3
AMPLE_ABSOLUTE = 1000  # sales units: this much on hand is "ample" whatever the request
LEVELS = ("ample", "limited", "short", "none", "unknown")


def mode() -> str:
    """The configured mode; anything unrecognised falls back to the safe default (buckets)."""
    m = (config.STOCK_EXPOSURE or "").strip().lower()
    return m if m in MODES else "buckets"


def exact(approver: bool = False) -> bool:
    """Whether this caller sees exact stock numbers."""
    m = mode()
    return m == "exact" or (m == "approver_exact" and approver)


def is_approver(reader) -> bool:
    """A caller proved the global approver code (sous/access.py); sandbox and agent tokens don't count."""
    return reader is not None and "approver" in reader.kinds


def level(model: str | None, amount: float | None, requested: float | None, has_date: bool = True) -> str:
    """The stock bucket for an offer, from Valio Aimo's exact numbers (see the module docstring)."""
    from .quote import STOCK_PLENTY

    if model != "Stock":
        return "unknown"
    if not has_date or amount == 0:
        return "none"
    if amount is None:
        return "unknown"
    if amount >= STOCK_PLENTY:
        return "ample"
    if requested is not None and amount < requested:
        return "short"
    if amount >= AMPLE_ABSOLUTE or (requested is not None and amount >= AMPLE_FACTOR * requested):
        return "ample"
    return "limited"


# quote.py's PARTIAL_STOCK message: "Only 12 ST in stock; 40 needed."
_PARTIAL = re.compile(r"Only \S+ (\S+) in stock; (\S+) needed\.")


def _redact(x):
    if isinstance(x, dict):
        return {k: (None if k == "available_amount" else _redact(v)) for k, v in x.items()}
    if isinstance(x, list):
        return [_redact(v) for v in x]
    if isinstance(x, str):
        return _PARTIAL.sub(r"Not enough in stock for the \2 \1 needed (stock level: short).", x)
    return x


def view(obj, approver: bool = False):
    """What a caller may see of `obj` (a quote body, substitutes or suggestions response): unchanged when the
    caller sees exact stock, otherwise a copy without stock amounts. Never mutates `obj`."""
    return obj if exact(approver) else _redact(obj)
