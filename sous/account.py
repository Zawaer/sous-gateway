"""Customer account check: Valio Aimo's own facts about the buyer as a customer (credit, payment block, delivery days).

The mandate says what the BUYER allows its agent. This says what VALIO allows the customer, whoever orders:

  account.payment_block  block  the account has a payment (or delivery) block, e.g. overdue invoices
  account.credit_limit   ask    the order (incl. VAT) would take the account past its available credit
  account.delivery_day   block  the delivery date isn't one of the customer's delivery days, or its route's
                                cut-off for that date has passed
  account.unknown        block  only with SOUS_REQUIRE_ACCOUNT=1: no customer account for the mandate's buyer

Interface: `CustomerAccount` (lookup by customer number, and the buyer -> customer number mapping). The
implementation here is a MOCK: a `customer_account` table seeded per demo buyer and per sandbox copy from
DEMO_ACCOUNTS. In production the same interface is backed by Valio Aimo's customer master and credit management
(in SAP terms KNA1/KNVV for the customer and its sales area, the credit segment for limit and exposure, the
route/delivery schedule for days and cut-offs). None of that is integrated: see docs/INTEGRATION.md.

Which account: the mandate's optional `customer_number` (Valio's customer number for the buyer), else the
buyer name mapped in the account table. Credit used = the account's open balance (from the ERP) + Sous orders
for the same customer that went through and are still to be delivered (delivery date today or later; in the
ERP these are open orders the balance doesn't yet show).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, Field

from . import config, store

WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
REQUIRE_ACCOUNT = config.env("REQUIRE_ACCOUNT", "") in ("1", "true", "yes")


class Route(BaseModel):
    id: str = Field(description="Delivery route, e.g. HKI-03")
    cutoff_local: str = Field("20:00", pattern=r"^\d{2}:\d{2}$", description="Orders for a delivery date close at "
                              "this Helsinki time, cutoff_days_before days before it")
    cutoff_days_before: int = Field(1, ge=0, le=7)


class Account(BaseModel):
    customer_number: str = Field(description="Valio Aimo customer number (SAP KUNNR in production)")
    buyer: str = Field(description="The buyer organisation as named in mandates")
    name: str | None = None
    currency: str = "EUR"
    credit_limit_eur: float | None = Field(None, description="None = no credit limit")
    open_balance_eur: float = Field(0.0, description="Open receivables and open orders known to the ERP")
    payment_block: bool = False
    payment_block_reason: str | None = None
    delivery_days: list[str] = Field(default_factory=lambda: WEEKDAYS[:5])
    route: Route | None = None
    source: str = "mock"


class CustomerAccount(Protocol):
    """What the gateway needs from Valio's customer master. The mock below; SAP-backed in production."""

    def customer_number_for(self, buyer: str) -> str | None: ...
    def get(self, customer_number: str) -> Account | None: ...


# Demo buyers (scripts/seed.py). Numbers are made up, 10 digits like a SAP KUNNR. No rule fires on the demo path:
# credit is ample and Monday (the demo's delivery day, sandbox.next_delivery_day) is a delivery day for all.
DEMO_ACCOUNTS: list[dict] = [
    {"customer_number": "0000104711", "buyer": "Hotel Aurora, Helsinki", "name": "Hotel Aurora Oy",
     "credit_limit_eur": 60000, "open_balance_eur": 12400, "delivery_days": WEEKDAYS[:6],
     "route": {"id": "HKI-03", "cutoff_local": "20:00", "cutoff_days_before": 1}},
    {"customer_number": "0000207315", "buyer": "Meilahti hospital central kitchen", "name": "HUS Meilahti keittiö",
     "credit_limit_eur": 40000, "open_balance_eur": 8150, "delivery_days": WEEKDAYS,
     "route": {"id": "HKI-01", "cutoff_local": "20:00", "cutoff_days_before": 1}},
    {"customer_number": "0000309822", "buyer": "Espoo school kitchens", "name": "Espoon kaupunki, ruokapalvelut",
     "credit_limit_eur": 30000, "open_balance_eur": 4300, "delivery_days": WEEKDAYS[:5],
     "route": {"id": "ESP-02", "cutoff_local": "20:00", "cutoff_days_before": 1}},
    {"customer_number": "0000411208", "buyer": "Kaarna Catering Oy, Espoo", "name": "Kaarna Catering Oy",
     "credit_limit_eur": 15000, "open_balance_eur": 2200, "delivery_days": WEEKDAYS[:6],
     "route": {"id": "ESP-05", "cutoff_local": "20:00", "cutoff_days_before": 1}},
]
DEMO_CUSTOMER_NUMBERS = {a["buyer"]: a["customer_number"] for a in DEMO_ACCOUNTS}
_SANDBOX = re.compile(r"^(?P<base>.+) \(sandbox (?P<sid>SBX-[0-9A-Z]{4})\)$")

_SQLITE = """CREATE TABLE IF NOT EXISTS customer_account (
  customer_number TEXT PRIMARY KEY, buyer TEXT NOT NULL, data TEXT NOT NULL, updated_at TEXT NOT NULL)"""
_MARIADB = """CREATE TABLE IF NOT EXISTS customer_account (
  customer_number VARCHAR(64) PRIMARY KEY, buyer VARCHAR(191) NOT NULL, data LONGTEXT NOT NULL,
  updated_at VARCHAR(40) NOT NULL, INDEX (buyer)) WITH SYSTEM VERSIONING"""
_ready_for = None


def _ensure() -> None:
    global _ready_for
    with store._lock:
        conn = store.db()
        if _ready_for is conn:
            return
        store._exec(_MARIADB if config.DB_URL else _SQLITE)
        _ready_for = conn


def sandbox_number(base: str, sandbox_id: str) -> str:
    """A sandbox copy's own account (its own credit), so visitors never share or use the demo buyer's."""
    return f"{base}-{sandbox_id}"


def _template(customer_number: str | None, buyer: str | None) -> dict | None:
    """Seed data for a demo buyer or a sandbox copy of one, by customer number or buyer name."""
    sid = None
    if buyer and (m := _SANDBOX.match(buyer)):
        buyer, sid = m["base"], m["sid"]
    if customer_number and (m := re.match(r"^(?P<base>\d+)-(?P<sid>SBX-[0-9A-Z]{4})$", customer_number)):
        customer_number, sid = m["base"], m["sid"]
    t = next((a for a in DEMO_ACCOUNTS if a["customer_number"] == customer_number or a["buyer"] == buyer), None)
    if t is None:
        return None
    if sid:
        t = {**t, "customer_number": sandbox_number(t["customer_number"], sid),
             "buyer": f"{t['buyer']} (sandbox {sid})", "name": f"{t['name']} (sandbox {sid})"}
    return t


class MockAccounts:
    """CustomerAccount backed by the customer_account table; demo buyers and sandbox copies are seeded on first use."""

    def put(self, a: Account) -> Account:
        _ensure()
        store._upsert("customer_account", ["customer_number", "buyer", "data", "updated_at"],
                      (a.customer_number, a.buyer, a.model_dump_json(), store.now_iso()))
        return a

    def _row(self, sql: str, arg: str) -> Account | None:
        _ensure()
        r = store._exec(sql, (arg,))[0]
        return Account(**json.loads(r[0]["data"])) if r else None

    def get(self, customer_number: str) -> Account | None:
        a = self._row("SELECT data FROM customer_account WHERE customer_number = ?", customer_number)
        if a is None and (t := _template(customer_number, None)):
            a = self.put(Account(**t))
        return a

    def customer_number_for(self, buyer: str) -> str | None:
        a = self._row("SELECT data FROM customer_account WHERE buyer = ? ORDER BY customer_number LIMIT 1", buyer)
        if a is None and (t := _template(None, buyer)):
            a = self.put(Account(**t))
        return a.customer_number if a else None


backend: CustomerAccount = MockAccounts()


def for_mandate(mandate) -> Account | None:
    num = getattr(mandate, "customer_number", None) or backend.customer_number_for(mandate.buyer)
    return backend.get(num) if num else None


def _mandate_ids(account: Account) -> set[str]:
    """Mandates whose orders count against this account."""
    out = set()
    for m in store.list_mandates():
        num = m.get("customer_number")
        if num == account.customer_number or (not num and m.get("buyer") == account.buyer):
            out.add(m["id"])
    return out


def sous_open_eur(account: Account, today: date) -> float:
    """Sous orders for the customer that went through and are still to be delivered (incl. VAT), in cents."""
    ids = _mandate_ids(account)
    cents = sum(round((o.get("total_inc_vat") or o.get("total_ex_vat") or 0) * 100)
                for o in store.list_orders(status="ordered") if o["mandate_id"] in ids
                and (o.get("delivery_date") or "") >= today.isoformat())
    return cents / 100


def credit(account: Account, today: date) -> dict:
    used = round(account.open_balance_eur + sous_open_eur(account, today), 2)
    avail = None if account.credit_limit_eur is None else round(account.credit_limit_eur - used, 2)
    return {"credit_limit_eur": account.credit_limit_eur, "open_balance_eur": account.open_balance_eur,
            "sous_open_orders_eur": round(used - account.open_balance_eur, 2), "available_eur": avail}


def _route_cutoff(route: Route, day: date) -> datetime:
    h, m = (int(x) for x in route.cutoff_local.split(":"))
    d = day - timedelta(days=route.cutoff_days_before)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=config.TZ)


def findings(mandate, q: dict, now: datetime) -> list:
    """account.* findings for a quote (preview, order, approval). Empty when the buyer has no account (unless
    SOUS_REQUIRE_ACCOUNT=1) so a gateway without customer data behaves as before."""
    from .mandate import Finding
    a = for_mandate(mandate)
    if a is None:
        if REQUIRE_ACCOUNT:
            return [Finding(rule="account.unknown", outcome="block",
                            message=f"No Valio Aimo customer account for {mandate.buyer}: set the mandate's "
                                    "customer_number.")]
        return []
    out = []
    if a.payment_block:
        out.append(Finding(rule="account.payment_block", outcome="block",
                           message=f"Customer {a.customer_number} has a payment block"
                                   + (f" ({a.payment_block_reason})" if a.payment_block_reason else "")
                                   + ": Valio Aimo takes no new orders on the account until it is lifted."))
    lines = [l for l in q.get("lines") or [] if l.get("orderable", True)]
    total = round(sum(l.get("line_total_inc_vat") or l.get("line_total_ex_vat") or 0 for l in lines), 2)
    c = credit(a, now.astimezone(config.TZ).date())
    if c["available_eur"] is not None and total > 0 and round(total * 100) > round(c["available_eur"] * 100):
        out.append(Finding(rule="account.credit_limit", outcome="ask",
                           message=f"This order (€{total:,.2f} incl. VAT) is more than the €{max(c['available_eur'], 0):,.2f} "
                                   f"credit left on customer {a.customer_number} (limit €{a.credit_limit_eur:,.2f}, "
                                   f"open balance €{a.open_balance_eur:,.2f}, open Sous orders "
                                   f"€{c['sous_open_orders_eur']:,.2f}). A human confirms it; Valio Aimo's credit "
                                   "control may still hold it."))
    days = sorted({l["delivery_date"] for l in lines if l.get("delivery_date")}) or [q["requested_delivery_date"]]
    for d in days:
        day = date.fromisoformat(d[:10])
        wd = WEEKDAYS[day.weekday()]
        if wd not in a.delivery_days:
            out.append(Finding(rule="account.delivery_day", outcome="block",
                               message=f"{day:%a %d.%m.%Y} is not a delivery day for customer {a.customer_number} "
                                       f"(delivers {', '.join(a.delivery_days)}"
                                       + (f", route {a.route.id}" if a.route else "") + ")."))
        elif a.route and now >= (cut := _route_cutoff(a.route, day)):
            out.append(Finding(rule="account.delivery_day", outcome="block",
                               message=f"Route {a.route.id}'s cut-off for {day:%d.%m.%Y} was "
                                       f"{cut:%a %d.%m. %H:%M}: it has passed."))
    return out


def summary(mandate, now: datetime | None = None) -> dict | None:
    """The account as the UI and GET /accounts show it, with credit worked out."""
    a = for_mandate(mandate)
    if a is None:
        return None
    now = now or datetime.now(config.TZ)
    return {**a.model_dump(mode="json"), "credit": credit(a, now.astimezone(config.TZ).date())}
