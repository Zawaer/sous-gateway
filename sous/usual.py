"""What a buyer usually orders: per product family and unit, from that mandate's past orders.

Feeds the mandate rule quantity.unusual (sous/mandate.py): an order far above the buyer's usual amount of
the same kind of product (e.g. 960 L of milk when the last orders were around 120 L) is held for a human.
It catches an agent's slip (an extra zero, litres read as packs) before it becomes a pallet at the door.

History is the mandate's own orders that went through (status ordered), standing-order renewals included
(they are orders under the same mandate), from the last HISTORY_DAYS days, newest MAX_ORDERS. Amounts come
from each order's quote: what the lines cover in the need's unit (L, KG) or else the packs ordered.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import catalog, store

HISTORY_DAYS = 90
MAX_ORDERS = 50


def _family(pid: str) -> tuple[str, str]:
    """(key, label) for the product's family: 'milk', 'cream'... from its name; else its category code."""
    p = catalog.get(pid) or {}
    try:
        from .substitutes import product_type  # lazy: substitutes imports mandate

        t = product_type(p) if p else {}
    except Exception:  # an unreadable name never breaks an order
        t = {}
    fam = t.get("family") or t.get("key")
    if fam:
        return fam, t.get("label") or fam
    cat = p.get("category") or "?"
    return f"category {cat}", f"Category {cat}"


def profile(lines: list[dict]) -> dict[tuple[str, str], dict]:
    """{(family, unit): {amount, label, product_id}} for the orderable lines of a quote."""
    out: dict[tuple[str, str], dict] = {}
    for l in lines:
        if not l.get("orderable", True):
            continue
        cov = l.get("covers") or {}
        if cov.get("unit") and cov.get("amount") is not None:
            unit, amount = cov["unit"], float(cov["amount"])
        elif l.get("quantity") is not None:
            unit, amount = l.get("sales_unit") or "packs", float(l["quantity"])
        else:
            continue
        fam, label = _family(l["product_id"])
        cur = out.setdefault((fam, unit), {"amount": 0.0, "label": label, "product_id": l["product_id"]})
        cur["amount"] = round(cur["amount"] + amount, 3)
    return out


def history(mandate_id: str, now: datetime) -> dict[tuple[str, str], list[float]]:
    """Past ordered amounts per (family, unit) under this mandate, one value per order."""
    since = (now - timedelta(days=HISTORY_DAYS)).isoformat()
    out: dict[tuple[str, str], list[float]] = {}
    orders = [o for o in store.list_orders(status="ordered", mandate_id=mandate_id) if o["created_at"] >= since]
    for o in orders[:MAX_ORDERS]:
        q = store.get_quote(o["quote_id"])
        if not q:
            continue
        for key, cur in profile(q["lines"]).items():
            out.setdefault(key, []).append(cur["amount"])
    return out
