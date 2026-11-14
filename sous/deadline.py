"""Approve-by deadlines: a held order is only useful if the human decides before Valio's order cut-off.

Every `pending_approval` order carries `approve_by`, the earlier of
- the earliest order cut-off among its lines (Valio's closingTime) minus a safety margin
  (SOUS_APPROVE_MARGIN_MIN, default 15 minutes; the margin is dropped if the order was held inside it), and
- the start of its earliest delivery day (00:00 Helsinki), for lines without a cut-off, and
- the end of what may approve it: the mandate's valid_until (basis `mandate_expires`) and, for a sandbox order, the
  sandbox's expiry (basis `sandbox_expires`). An approval after either is refused anyway (the approval re-checks
  the mandate), so the deadline must not promise more time than that.

Once business time (clock.now(), so the demo clock applies) passes approve_by without a decision, the order
becomes `expired_cutoff` (terminal, nothing ordered) and the buyer is told the next delivery date to ask for
(the day after the lost one, as the quote's `next_date` option does). An approval after approve_by is refused
with `approval_deadline_passed`.

Standing orders (sous/standing.py) use estimate_cutoff() to renew early enough for a human to approve.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from . import config

MARGIN_MIN = float(config.env("APPROVE_MARGIN_MIN", "15"))
# Valio Aimo's Stock cut-off is 17:00 Helsinki the day before delivery (mock: Sun 15.11 17:00 for Mon 16.11).
DEFAULT_CUTOFF_HOUR = 17


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(config.TZ)


def day_start(d: date | str) -> datetime:
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return datetime.combine(d, time(0), config.TZ)


def delivery_days(order: dict) -> list[str]:
    """The days an order delivers on: its lines' own delivery dates (after a signed next_date option these are later
    than the date first asked for, which no longer matters), else the order's delivery date."""
    days = [l["delivery_date"] for l in order.get("lines") or [] if l.get("delivery_date")]
    return days or [d for d in (order.get("delivery_date") or order.get("requested_delivery_date"),) if d]


def compute(lines: list[dict], delivery_dates: list[str], now: datetime,
            limits: list[tuple[datetime, dict]] | None = None) -> dict | None:
    """approve_by for a held order: {at, basis, cutoff, product_id, margin_minutes, delivery_date}, or None.
    `limits`: (instant, {basis, ...}) ends of the approval authority (mandate_limits), no margin; on a tie the
    cut-off wins, so a cut-off-bound order reads exactly as before."""
    cands = []
    margin = timedelta(minutes=MARGIN_MIN)
    for l in lines:
        if l.get("order_cutoff"):
            cut = _dt(l["order_cutoff"])
            at, m = (cut - margin, MARGIN_MIN) if cut - margin > now else (cut, 0)
            cands.append((at, {"basis": "cutoff", "cutoff": cut.isoformat(), "product_id": l.get("product_id"),
                               "margin_minutes": m, "delivery_date": l.get("delivery_date")}))
    days = sorted({d for d in delivery_dates if d})
    if days:
        cands.append((day_start(days[0]), {"basis": "delivery_date", "cutoff": None, "product_id": None,
                                           "margin_minutes": 0, "delivery_date": days[0]}))
    if not cands:
        return None
    first_day = days[0] if days else None
    first_cut = min((c[1]["cutoff"] for c in cands if c[1]["basis"] == "cutoff"), default=None, key=_dt)
    for at, extra in limits or []:
        cands.append((at.astimezone(config.TZ), {"cutoff": first_cut, "product_id": None, "margin_minutes": 0,
                                                 "delivery_date": first_day, **extra}))
    at, basis = min(cands, key=lambda c: c[0])
    return {"at": at.isoformat(timespec="seconds"), **basis}


def mandate_limits(valid_until: datetime | None, mandate_id: str | None = None,
                   sandbox_id: str | None = None, sandbox_expires: datetime | None = None) -> list:
    """The limits compute() takes: the mandate's valid_until and the sandbox's expiry (already in business time).
    The sandbox comes first so it names the deadline when both are the same instant (a sandbox mandate's
    valid_until is its sandbox's expiry)."""
    out = []
    if sandbox_expires:
        out.append((sandbox_expires, {"basis": "sandbox_expires", "sandbox": sandbox_id, "mandate_id": mandate_id}))
    if valid_until:
        out.append((valid_until, {"basis": "mandate_expires", "mandate_id": mandate_id}))
    return out


# expiry.cause of an expired_cutoff order, by the basis that set its approve_by
CAUSE = {"cutoff": "cutoff", "delivery_date": "delivery_date", "mandate_expires": "mandate_expired",
         "sandbox_expires": "sandbox_expired"}
AUTHORITY = {"mandate_expires": "the mandate expires then", "sandbox_expires": "the sandbox and its mandate expire then"}


def authority_ends(order: dict) -> str | None:
    """`basis` if the order's approve_by is set by its mandate or sandbox ending, not by Valio's cut-off."""
    b = (order.get("approve_by_detail") or {}).get("basis")
    return b if b in AUTHORITY else None


def overdue(order: dict, now: datetime) -> bool:
    return order.get("status") == "pending_approval" and bool(order.get("approve_by")) and \
        now > _dt(order["approve_by"])


def lost_date(order: dict) -> str:
    return (order.get("approve_by_detail") or {}).get("delivery_date") or order.get("delivery_date") \
        or order.get("requested_delivery_date")


def next_date(order: dict) -> str:
    """The delivery day to ask for instead: the day after the latest one the order was for (as next_date does)."""
    days = [l["delivery_date"] for l in order.get("lines") or [] if l.get("delivery_date")]
    days += [d for d in (order.get("delivery_date"), order.get("requested_delivery_date")) if d]
    return (date.fromisoformat(max(days)) + timedelta(days=1)).isoformat()


def fmt_day(d: str) -> str:
    x = date.fromisoformat(d)
    return f"{x:%a} {x.day}.{x.month}"


def estimate_cutoff(delivery: date, reference: list[tuple[str, str]] | None = None,
                    source: str = "last renewal's") -> dict:
    """The order cut-off for a future delivery day, before any live quote: the offset between the latest real
    (cut-off, delivery date) pair we know, applied to `delivery`; else Valio's Stock rule (17:00 the day before)."""
    for cut, day in reference or []:
        if cut and day:
            off = day_start(day) - _dt(cut)
            return {"at": (day_start(delivery) - off).isoformat(timespec="seconds"), "estimated": True,
                    "basis": f"{source} Valio cut-off ({_dt(cut):%a %H:%M} for {fmt_day(day)})"}
    at = day_start(delivery) - timedelta(days=1) + timedelta(hours=DEFAULT_CUTOFF_HOUR)
    return {"at": at.isoformat(timespec="seconds"), "estimated": True,
            "basis": f"default: Valio Stock cut-off, {DEFAULT_CUTOFF_HOUR}:00 the day before delivery"}
