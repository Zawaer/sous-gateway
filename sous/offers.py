"""Valio Aimo's proactive offers: the seller pushes a priced offer to buyers' agents, the buyer's mandate decides.

The "proactive selling agent" scenario, made safe for both sides. Unlike suggestions (sous/suggest.py: computed
on demand, catalogue prices), an offer is a commitment Valio makes on purpose: near best-before stock, a seasonal
product, a replacement for a recalled one, a new product. POST /offers (Valio side: the global approver code, or a
judge sandbox's sbxa_ token for its own buyers) creates it:

  {target, product_ids, price_ex_vat | discount_pct, quantity_cap, valid_until, reason, reason_text}

and the gateway signs it (JWS typ sous-offer+jws). Delivery: GET /mandates/{id}/offers (read-scoped like the
mandate), MCP list_offers, and an offer.received notification in each target buyer's feed.

Valio-side guardrails (at creation, deterministic):
  * the price is at or below Valio's current price for the product (catalogPrice, or listPrice when lower):
    an offer can only lower a price (offer_above_list otherwise);
  * a target mandate never sees a product its mandate rules out: avoided allergens (contained, may contain or
    unknown), a category it doesn't allow, missing required claims. Such products are excluded for that mandate,
    with the reason, in the offer's `excluded` list (Valio side only);
  * at most MAX_ACTIVE_PER_BUYER active offers per buyer, so agents aren't spammed (rate_limited exclusion);
  * audit events offer.created / offer.offered (per mandate) / offer.accepted (an order placed on it) /
    offer.expired / offer.withdrawn.

Buyer side: request_quote(..., offer_id=...) prices the offer's products at min(offer price, live price) and
references the offer in the signed quote (line `offer`, quote `offer`). The mandate decides as always: limits,
allergens, categories and first-time products still apply, and rule offer.applied (notify) tells the buyer the
agent took a seller's offer. Offer problems are refusals before a quote (offer_not_found, offer_expired,
offer_target_mismatch, offer_not_applicable, offer_over_cap) and findings at order or approval time (offer.expired,
offer.over_cap: block), so a quote can't outlive its offer.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from . import catalog, config, signing, store
from .mandate import Finding, Mandate

TYP = "sous-offer+jws"
SPONSOR = "Valio Aimo"
DISCLOSURE = ("Commercial offer from Valio Aimo, the seller: not neutral advice. Nothing is quoted or ordered "
              "unless your agent asks for a quote with this offer_id and places an order under your mandate, "
              "which decides as always.")
MAX_ACTIVE_PER_BUYER = 3
MAX_VALID_DAYS = 30
ACTOR = "valio-aimo-offers"

Reason = Literal["near_best_before", "seasonal", "recall_replacement", "new_product", "surplus", "other"]
REASON_TEXT = {"near_best_before": "Near best-before", "seasonal": "Seasonal", "new_product": "New product",
               "recall_replacement": "Replacement for a recalled product", "surplus": "Surplus stock",
               "other": "Valio Aimo offer"}

_SQLITE = """CREATE TABLE IF NOT EXISTS offer (
  id TEXT PRIMARY KEY, sandbox TEXT, status TEXT NOT NULL, valid_until TEXT NOT NULL,
  data TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
_MARIADB = """CREATE TABLE IF NOT EXISTS offer (
  id VARCHAR(32) PRIMARY KEY, sandbox VARCHAR(16), status VARCHAR(16) NOT NULL, valid_until VARCHAR(40) NOT NULL,
  data LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL, INDEX (status)
) WITH SYSTEM VERSIONING"""
COLS = ["id", "sandbox", "status", "valid_until", "data", "created_at", "updated_at"]
_ready_for = None


class OfferTarget(BaseModel):
    """Who gets the offer: named mandates, every mandate of a buyer, a segment (mandates ordering in a catalogue
    category), or (nothing set) every active mandate in scope."""
    mandate_ids: list[str] | None = Field(None, max_length=50)
    buyer: str | None = Field(None, max_length=200)
    category: str | None = Field(None, max_length=20, description="Catalogue category code, e.g. '10401' (cream): "
                                 "mandates that allow it and whose usual products include one in it")


class OfferIn(BaseModel):
    target: OfferTarget = Field(default_factory=OfferTarget)
    product_ids: list[str] = Field(min_length=1, max_length=10)
    price_ex_vat: float | None = Field(None, gt=0, description="Offer price per sales unit, ex VAT (one price for "
                                       "every product); or give discount_pct")
    discount_pct: float | None = Field(None, gt=0, le=90, description="Percent off Valio's current price")
    quantity_cap: float = Field(gt=0, le=100000, description="Sales units sold at the offer price, all buyers "
                                "together")
    valid_until: datetime
    reason: Reason
    reason_text: str = Field(min_length=1, max_length=300, description="Shown to buyers as written, e.g. "
                             "'best before 20.11.'")
    by: str = Field("valio", max_length=100, description="Who at Valio made the offer")

    @model_validator(mode="after")
    def _one_price(self):
        if (self.price_ex_vat is None) == (self.discount_pct is None):
            raise ValueError("give exactly one of price_ex_vat or discount_pct")
        return self


def _err(code: str, message: str, status: int):
    from . import service as S
    return S.GatewayError(code, message, status)


def _now() -> datetime:
    from . import service as S
    return S._now()


def _ensure() -> None:
    global _ready_for
    with store._lock:
        conn = store.db()
        if _ready_for is conn:
            return
        store._exec(_MARIADB if config.DB_URL else _SQLITE)
        _ready_for = conn


def _put(o: dict) -> None:
    _ensure()
    store._upsert("offer", COLS, (o["id"], o.get("sandbox"), o["status"], o["valid_until"],
                                  json.dumps(o, ensure_ascii=False), o["created_at"], o["updated_at"]))


def get(offer_id: str) -> dict | None:
    _ensure()
    r = store._exec("SELECT data FROM offer WHERE id = ?", (offer_id,))[0]
    return _expire(json.loads(r[0]["data"])) if r else None


def _all(sandbox: str | None = "*") -> list[dict]:
    """Offers, newest first: sandbox "*" every one, None the curated (non-sandbox) ones, "SBX-..." one sandbox's."""
    _ensure()
    q, args = "SELECT data FROM offer", ()
    if sandbox is None:
        q += " WHERE sandbox IS NULL"
    elif sandbox != "*":
        q, args = q + " WHERE sandbox = ?", (sandbox,)
    return [_expire(json.loads(r["data"])) for r in store._exec(q + " ORDER BY created_at DESC", args)[0]]


def _expire(o: dict) -> dict:
    """Lazily mark an offer past valid_until expired (audited once)."""
    if o["status"] == "active" and datetime.fromisoformat(o["valid_until"]) <= _now():
        o["status"], o["updated_at"] = "expired", _now().isoformat(timespec="milliseconds")
        _put(o)
        store.audit("offer.expired", ACTOR, "system", o.get("sandbox") or o["id"], None,
                    {"offer_id": o["id"], "valid_until": o["valid_until"], "used": used(o["id"])})
    return o


# --- usage ------------------------------------------------------------------

def used(offer_id: str, excluding_quote: str | None = None, mandate_ids: list[str] | None = None) -> float:
    """Sales units of the offer in orders placed or waiting for approval (refused/expired/rejected ones don't count)."""
    if mandate_ids is None:
        o = _raw(offer_id)
        mandate_ids = list((o or {}).get("mandates") or {})
    total = 0.0
    for mid in mandate_ids:
        for order in store.list_orders(mandate_id=mid):
            if order["status"] not in ("ordered", "pending_approval") or order.get("quote_id") == excluding_quote:
                continue
            total += sum(l.get("quantity") or 0 for l in order["lines"] if l.get("offer_id") == offer_id)
    return round(total, 3)


def _raw(offer_id: str) -> dict | None:
    _ensure()
    r = store._exec("SELECT data FROM offer WHERE id = ?", (offer_id,))[0]
    return json.loads(r[0]["data"]) if r else None


def remaining(o: dict) -> float:
    return max(0.0, round(o["quantity_cap"] - used(o["id"], mandate_ids=list(o["mandates"])), 3))


def _active(o: dict) -> bool:
    return o["status"] == "active" and datetime.fromisoformat(o["valid_until"]) > _now()


def active_for_buyer(buyer: str, sandbox: str | None) -> list[dict]:
    return [o for o in _all(sandbox) if _active(o) and buyer in {t["buyer"] for t in o["mandates"].values()}
            and remaining(o) > 0]


# --- create -----------------------------------------------------------------

def _static_block(p: dict | None, m: Mandate) -> str | None:
    """Why the mandate rules this product out, in words, or None (the same static checks as suggestions)."""
    if p is None:
        return None
    if m.allowed_categories is not None and p.get("category") not in m.allowed_categories:
        return f"category {p.get('category')} is not allowed by the mandate"
    if missing := sorted(set(m.required_claims) - set(p.get("claims") or [])):
        return f"lacks required claims {', '.join(missing)}"
    if m.avoid_allergens:
        st = catalog.allergen_status(p, m.avoid_allergens, m.allow_may_contain)
        if st["status"] in ("contains", "may_contain"):
            return f"{st['status'].replace('_', ' ')} an allergen the buyer avoids ({', '.join(m.avoid_allergens)})"
        if st["status"] == "unknown":
            return "allergen data unknown and the buyer avoids allergens"
    return None


def _candidates(t: OfferTarget, scope: str | None) -> list[Mandate]:
    """Active mandates the target names, within the issuer's scope (None = global: curated mandates, plus
    explicitly named ones anywhere; "SBX-..." = that sandbox's mandates only)."""
    from . import service as S
    now = _now()
    out = []
    for d in store.list_mandates():
        if d.get("sandbox") != scope and not (scope is None and t.mandate_ids and d["id"] in t.mandate_ids):
            continue
        if t.mandate_ids is not None and d["id"] not in t.mandate_ids:
            continue
        if t.buyer is not None and d.get("buyer") != t.buyer:
            continue
        try:
            m = S._mandate(d["id"])
        except Exception:  # noqa: BLE001 - an unverifiable mandate gets no offers
            continue
        if m.revoked or (m.valid_until and m.valid_until <= now):
            continue
        if t.category is not None:
            if m.allowed_categories is not None and t.category not in m.allowed_categories:
                continue
            if m.allowed_categories is None and not any(
                    (catalog.get(pid) or {}).get("category") == t.category for pid in m.usual_products):
                continue
        out.append(m)
    return out


def _price_ceiling(product_ids: list[str], day: date, metered_by: Mandate | None) -> dict[str, float]:
    """Valio's current price per sales unit ex VAT (catalogPrice, or listPrice when lower), one live price call."""
    from . import aimo, sandbox, service as S
    try:
        with sandbox.metered(metered_by):
            prices = aimo.prices(product_ids, day)
    except aimo.AimoError as e:
        raise S.valio_error(e)
    out = {}
    for pid in product_ids:
        p = prices.get(pid) or {}
        vals = [v for v in ((p.get("catalogPrice") or {}).get("value"), (p.get("listPrice") or {}).get("value"))
                if v is not None]
        if p.get("error") or not vals:
            raise _err("offer_no_list_price", f"Valio Aimo has no current price for {pid}, so an offer price can't "
                       "be checked against it.", 422)
        out[pid] = min(vals)
    return out


def create(req: OfferIn, scope: str | None, *, reference_prices: dict[str, float] | None = None,
           seeded: dict | None = None) -> dict:
    """`scope`: None for the global approver, a sandbox id for a sandbox approver. `reference_prices` replaces the
    live price lookup (sandbox seed only; the offer says so in `seeded`)."""
    now = _now()
    until = req.valid_until if req.valid_until.tzinfo else req.valid_until.replace(tzinfo=config.TZ)
    if until <= now or until > now + timedelta(days=MAX_VALID_DAYS):
        raise _err("invalid_request", f"valid_until must be in the future and at most {MAX_VALID_DAYS} days ahead.",
                   422)
    ids = list(dict.fromkeys(req.product_ids))
    products = {pid: catalog.get(pid) for pid in ids}
    if not reference_prices and (unknown := [pid for pid, p in products.items() if not p]):
        raise _err("product_not_found", f"No product {', '.join(unknown)}.", 404)
    cands = _candidates(req.target, scope)
    if not cands:
        raise _err("offer_no_targets", "No active mandate matches the target in your scope.", 422)
    ceiling = reference_prices or _price_ceiling(ids, now.date(), cands[0] if scope else None)
    priced = []
    for pid in ids:
        price = req.price_ex_vat if req.price_ex_vat is not None else ceiling[pid] * (1 - req.discount_pct / 100)
        price = round(price, 2)
        if price > ceiling[pid] + 1e-9:
            raise _err("offer_above_list", f"Offer price €{price:.2f} for {pid} is above Valio's current price "
                       f"€{ceiling[pid]:.2f}: an offer may only lower a price.", 422)
        p = products[pid] or {}
        priced.append({"product_id": pid, "name": p.get("name") or (seeded or {}).get("names", {}).get(pid) or pid,
                       "sales_unit": p.get("sales_unit") or "ST", "price_ex_vat": price,
                       "reference_price_ex_vat": round(ceiling[pid], 2),
                       "discount_pct": round((1 - price / ceiling[pid]) * 100, 1) if ceiling[pid] else None,
                       "reference_basis": "seeded reference price (no Valio call)" if reference_prices
                       else "Valio Aimo current price at creation"})
    oid = "OF-" + secrets.token_hex(4).upper()
    core = {"id": oid, "issuer": SPONSOR, "issued_at": now.isoformat(timespec="milliseconds"),
            "valid_until": until.isoformat(timespec="seconds"), "currency": "EUR", "products": priced,
            "quantity_cap": req.quantity_cap, "quantity_unit": "SALES_UNIT",
            "reason": req.reason, "reason_text": req.reason_text.strip(), "sandbox": scope,
            **({"seeded": seeded["note"]} if seeded else {})}
    mandates, excluded = {}, []
    for m in cands:
        why = {pid: w for pid in ids if (w := _static_block(products[pid], m))}
        ok = [pid for pid in ids if pid not in why]
        if not ok:
            excluded.append({"mandate_id": m.id, "buyer": m.buyer, "reason": "mandate_rules",
                             "products": [{"product_id": pid, "why": w} for pid, w in why.items()]})
            continue
        if len(active_for_buyer(m.buyer, scope)) >= MAX_ACTIVE_PER_BUYER:
            excluded.append({"mandate_id": m.id, "buyer": m.buyer, "reason": "rate_limited",
                             "why": f"{m.buyer} already has {MAX_ACTIVE_PER_BUYER} active offers (the limit)."})
            continue
        mandates[m.id] = {"buyer": m.buyer, "product_ids": ok,
                          "excluded_products": [{"product_id": pid, "why": w} for pid, w in why.items()]}
    if not mandates:
        raise _err("offer_no_targets", "Every target mandate was excluded: " + "; ".join(
            f"{e['mandate_id']} ({e['reason']})" for e in excluded), 422)
    jws = signing.sign(core, TYP)
    o = {**core, "status": "active", "jws": jws, "target": req.target.model_dump(exclude_none=True),
         "mandates": mandates, "excluded": excluded, "by": req.by,
         "created_at": core["issued_at"], "updated_at": core["issued_at"]}
    _put(o)
    store.audit("offer.created", req.by, "human" if not seeded else "system", scope or oid, None,
                {"offer": core, "target": o["target"], "mandates": list(mandates), "excluded": excluded})
    _deliver(o, notify_feed=not seeded)  # a seeded offer is reported in the sandbox response instead
    return valio_view(o)


def _deliver(o: dict, notify_feed: bool = True) -> None:
    from . import notify, service as S
    for mid, t in o["mandates"].items():
        store.audit("offer.offered", ACTOR, "system", o["id"], t["buyer"],
                    {"offer_id": o["id"], "mandate_id": mid, "product_ids": t["product_ids"],
                     "excluded_products": t["excluded_products"]})
        if not notify_feed:
            continue
        try:
            m = S._mandate(mid)
        except Exception:  # noqa: BLE001
            continue
        notify.send("offer.received", {"id": o["id"], "buyer": t["buyer"], "mandate_id": mid,
                                       "offer": buyer_view(o, mid, remaining_qty=o["quantity_cap"])}, m)


def withdraw(offer_id: str, by: str, scope: str | None) -> dict | None:
    o = get(offer_id)
    if not o or (scope is not None and o.get("sandbox") != scope):
        return None
    if o["status"] == "active":
        o["status"], o["updated_at"], o["withdrawn_by"] = "withdrawn", _now().isoformat(timespec="milliseconds"), by
        _put(o)
        store.audit("offer.withdrawn", by, "human", o.get("sandbox") or o["id"], None, {"offer_id": o["id"], "by": by})
    return valio_view(o)


# --- views ------------------------------------------------------------------

def valio_view(o: dict) -> dict:
    """What Valio (the issuer) sees: targets and exclusions too."""
    return {**{k: v for k, v in o.items() if k != "jws"}, "used": used(o["id"], mandate_ids=list(o["mandates"])),
            "remaining": remaining(o), "jws": o["jws"]}


def list_valio(scope: str | None) -> list[dict]:
    return [valio_view(o) for o in _all("*" if scope == "*" else scope)]


def buyer_view(o: dict, mandate_id: str, remaining_qty: float | None = None) -> dict:
    """What one target mandate's agent sees: no other buyers, only the products its mandate allows."""
    t = o["mandates"][mandate_id]
    core = signing.verify(o["jws"], TYP) or {}
    return {
        "id": o["id"], "sponsored_by": SPONSOR, "disclosure": DISCLOSURE, "status": o["status"],
        "reason": o["reason"], "reason_text": o["reason_text"],
        "headline": f"{REASON_TEXT[o['reason']]}: {o['reason_text']}",
        "products": [p for p in core.get("products", o["products"]) if p["product_id"] in t["product_ids"]],
        "valid_until": o["valid_until"], "quantity_cap": o["quantity_cap"],
        "remaining": remaining(o) if remaining_qty is None else remaining_qty,
        "quantity_unit": "SALES_UNIT",
        **({"seeded": o["seeded"]} if o.get("seeded") else {}),
        "how_to_accept": (f"request_quote with mandate_id={mandate_id}, offer_id={o['id']} and items naming the "
                          "offer's product_ids (amount in SALES_UNIT, L or KG); the quote prices them at the offer "
                          "price and your mandate decides as always. Only if the buyer wants it."),
        "jws": o["jws"],
    }


def for_mandate(mandate_id: str, include_inactive: bool = False) -> dict:
    """GET /mandates/{id}/offers and MCP list_offers. The caller's read access was checked already."""
    from . import service as S
    m = S._mandate(mandate_id)
    offers = []
    for o in _all("*"):
        if mandate_id not in o["mandates"]:
            continue
        if not include_inactive and not (_active(o) and remaining(o) > 0):
            continue
        offers.append(buyer_view(o, mandate_id))
    return {"mandate_id": m.id, "buyer": m.buyer, "sponsored_by": SPONSOR, "disclosure": DISCLOSURE,
            "offers": offers, "note": None if offers else "No active Valio Aimo offers for this mandate."}


# --- quoting and ordering ---------------------------------------------------

def for_quote(offer_id: str, mandate: Mandate | None) -> dict:
    """The offer a quote request names, after the refusal checks that need no prices."""
    if mandate is None:
        raise _err("offer_requires_mandate", "An offer is quoted under the mandate it was made for: pass mandate_id.",
                   422)
    o = get(offer_id)
    if not o or mandate.id not in o["mandates"]:
        # an offer for another buyer answers like one that doesn't exist: no oracle over other buyers' offers
        raise _err("offer_not_found", f"No offer {offer_id} for mandate {mandate.id} (GET /mandates/{mandate.id}/"
                   "offers lists yours).", 404)
    if o["status"] != "active" or not _active(o):
        raise _err("offer_expired", f"Offer {offer_id} is {o['status'] if o['status'] != 'active' else 'expired'} "
                   f"(valid until {o['valid_until']}); ask for a quote at the current price instead.", 409)
    if signing.verify(o["jws"], TYP) is None:
        raise _err("offer_signature", f"Offer {offer_id} does not verify.", 409)
    return o


def apply(o: dict, mandate: Mandate, lines: list[dict]) -> dict:
    """Price the quote's lines for the offer's products at min(offer price, live price). Raises when the offer
    covers no line or the quote asks for more than is left. Returns the quote's `offer` reference."""
    allowed = set(o["mandates"][mandate.id]["product_ids"])
    price = {p["product_id"]: p["price_ex_vat"] for p in o["products"] if p["product_id"] in allowed}
    hit = [l for l in lines if l["product_id"] in price and l.get("orderable") and l.get("unit_price_ex_vat")]
    if not hit:
        raise _err("offer_not_applicable", f"Offer {o['id']} covers {', '.join(sorted(price))}; none of them is an "
                   "orderable line of this quote. Name the offer's product_ids in items.", 422)
    qty = round(sum(l["quantity"] or 0 for l in hit), 3)
    left = remaining(o)
    if qty > left + 1e-9:
        raise _err("offer_over_cap", f"Offer {o['id']} has {left:g} sales units left; this quote needs {qty:g}. "
                   f"Ask for at most {left:g}.", 409)
    for l in hit:
        live, vat = l["unit_price_ex_vat"], l.get("vat_rate") or 0
        new = min(price[l["product_id"]], live)
        ratio = new / live
        l["unit_price_ex_vat"] = new
        l["unit_price_inc_vat"] = round(new * (1 + vat / 100), 4)
        l["line_total_ex_vat"] = round(new * l["quantity"], 2)
        l["line_total_inc_vat"] = round(l["unit_price_inc_vat"] * l["quantity"], 2)
        if (cp := l.get("comparison_price")) and cp.get("ex_vat") is not None:
            l["comparison_price"] = {**cp, "ex_vat": round(cp["ex_vat"] * ratio, 2)}
        l["offer_id"] = o["id"]
        l["offer"] = {"id": o["id"], "price_ex_vat": new, "live_price_ex_vat": live, "reason": o["reason"],
                      "reason_text": o["reason_text"], "valid_until": o["valid_until"]}
    return {"id": o["id"], "sponsored_by": SPONSOR, "reason": o["reason"], "reason_text": o["reason_text"],
            "valid_until": o["valid_until"], "quantity": qty, "remaining_before": left,
            "offer_jws_sha256": hashlib.sha256(o["jws"].encode()).hexdigest()}


def findings(q: dict, now: datetime) -> list[Finding]:
    """Rules for a quote's offer lines, at quote, order and approval time: offer.applied (notify), and the offer's
    own validity (offer.expired, offer.over_cap: block). Orders on this quote don't count against its own cap."""
    out: list[Finding] = []
    by_offer: dict[str, list[dict]] = {}
    for l in q.get("lines") or []:
        if l.get("offer_id") and l.get("orderable", True):
            by_offer.setdefault(l["offer_id"], []).append(l)
    for oid, ls in by_offer.items():
        o = _raw(oid)
        first = ls[0]
        out.append(Finding(rule="offer.applied", outcome="notify", product_id=first["product_id"], message=(
            f"Bought on Valio Aimo's offer {oid} ({first['offer']['reason_text']}): "
            f"€{first['offer']['price_ex_vat']:,.2f} instead of €{first['offer']['live_price_ex_vat']:,.2f} per "
            f"{first.get('sales_unit') or 'unit'}; the buyer is told the agent took a seller's offer.")))
        if not o or o["status"] == "withdrawn" or datetime.fromisoformat(o["valid_until"]) <= now:
            out.append(Finding(rule="offer.expired", outcome="block", product_id=first["product_id"], message=(
                f"Valio Aimo's offer {oid} is no longer valid (valid until {(o or {}).get('valid_until')}"
                f"{', withdrawn' if o and o['status'] == 'withdrawn' else ''}): ask for a new quote.")))
            continue
        qty = sum(l.get("quantity") or 0 for l in ls)
        left = o["quantity_cap"] - used(oid, excluding_quote=q.get("id"), mandate_ids=list(o["mandates"]))
        if qty > left + 1e-9:
            out.append(Finding(rule="offer.over_cap", outcome="block", product_id=first["product_id"], message=(
                f"Valio Aimo's offer {oid} has {max(0, left):g} sales units left; this order needs {qty:g}.")))
    return out


def on_order(order: dict) -> None:
    """Audit offer.accepted when an order with offer lines went through or is waiting for approval."""
    if order["status"] not in ("ordered", "pending_approval"):
        return
    qty: dict[str, float] = {}
    for l in order["lines"]:
        if l.get("offer_id"):
            qty[l["offer_id"]] = qty.get(l["offer_id"], 0) + (l.get("quantity") or 0)
    for oid, n in qty.items():
        store.audit("offer.accepted", order["agent_id"], "agent", oid, order["buyer"],
                    {"offer_id": oid, "order_id": order["id"], "status": order["status"], "quantity": n,
                     "mandate_id": order["mandate_id"]})


# --- sandbox seed -----------------------------------------------------------

SEED_OFFER = {"product_id": "464194", "name": "Valio whipping cream 1 l UHT lactose free",
              "reference_price": 4.20, "price": 2.95, "cap": 60}
SEED_NOTE = ("Seeded demo offer: its reference price is stored in the gateway, not a live Valio Aimo price. A quote "
             "on it is live and pays min(offer price, live price).")


def seed(sid: str, mandate_id: str) -> dict | None:
    """One near-best-before cream offer for a new sandbox's hotel (no Valio call). Never fails the sandbox."""
    import logging
    try:
        until = (_now() + timedelta(days=2)).replace(hour=23, minute=59, second=0, microsecond=0)
        s = SEED_OFFER
        req = OfferIn(target=OfferTarget(mandate_ids=[mandate_id]), product_ids=[s["product_id"]],
                      price_ex_vat=s["price"], quantity_cap=s["cap"], valid_until=until, reason="near_best_before",
                      reason_text=f"best before {(until + timedelta(days=3)):%d.%m.}, cream for sauces and desserts",
                      by="sandbox seed")
        o = create(req, sid, reference_prices={s["product_id"]: s["reference_price"]},
                   seeded={"note": SEED_NOTE, "names": {s["product_id"]: s["name"]}})
        return {"offer_id": o["id"], "mandate_id": mandate_id, "product_id": s["product_id"],
                "price_ex_vat": s["price"], "valid_until": o["valid_until"], "note": SEED_NOTE}
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("sandbox %s: offer seed failed", sid)
        return None
