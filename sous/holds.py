"""Valio-side product holds: recalls, quality holds and sales stops that agents can't buy past.

The seller's own control over what agents may order, next to the buyer's mandate. A hold is global (every
buyer, every sandbox) and needs the global approver code (POST /holds, DELETE /holds/{id}); the list of
active holds is public (GET /holds, MCP resource sous://holds) because buyers' agents need it.

Effects (docs/PROTOCOL.md, "Product holds"):
- quote.build_offer marks a held product unorderable with the blocking issue PRODUCT_HELD, so the planner,
  response options, substitutes and suggestions all skip it;
- mandate.evaluate fires rule `product.held` (block) for every held line, also on a quote issued before the
  hold, so placing an order and approving a held order (service._approval_blocker) are blocked too;
- creating a hold notifies the buyer of every open pending_approval order and active standing order with
  the product (one audit event `product.held` per affected order); a recall also lists the orders placed
  in the last RECALL_LOOKBACK_DAYS days with the product, so the buyer can act on delivered goods.

Stored like the other side tables (created lazily). On MariaDB `product_hold` is SYSTEM VERSIONED, so a
lifted hold keeps its full history: "was this product held when that order was decided?" is one
FOR SYSTEM_TIME AS OF query.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from . import config, store

Kind = Literal["recall", "quality_hold", "sales_stop"]
KIND_TEXT = {"recall": "recalled", "quality_hold": "on quality hold", "sales_stop": "under a sales stop"}
RECALL_LOOKBACK_DAYS = 14

_SQLITE = """CREATE TABLE IF NOT EXISTS product_hold (
  id TEXT PRIMARY KEY, product_id TEXT NOT NULL, kind TEXT NOT NULL, active INTEGER NOT NULL,
  data TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
_MARIADB = """CREATE TABLE IF NOT EXISTS product_hold (
  id VARCHAR(32) PRIMARY KEY, product_id VARCHAR(64) NOT NULL, kind VARCHAR(16) NOT NULL, active TINYINT NOT NULL,
  data LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL,
  INDEX (product_id)
) WITH SYSTEM VERSIONING"""
COLS = ["id", "product_id", "kind", "active", "data", "created_at", "updated_at"]

_ready_for = None  # the connection the table was last ensured on (tests swap connections)
_cache: dict = {"conn": None, "at": 0.0, "map": {}}
CACHE_SECONDS = 2.0  # search/quotes; placing and approving an order always read the database (invalidate())


class HoldIn(BaseModel):
    product_id: str = Field(min_length=1, max_length=64)
    kind: Kind = Field(description="recall (product withdrawn; recent orders are listed to the buyer), "
                                   "quality_hold (pending a quality check) or sales_stop (not sold for now)")
    reason: str = Field(min_length=1, max_length=500, description="Shown to buyers and their agents as written")
    until: datetime | None = Field(None, description="Optional end; without it the hold lasts until lifted")
    by: str = Field("valio", max_length=100, description="Who at Valio placed the hold")


def _ensure() -> None:
    global _ready_for
    with store._lock:
        conn = store.db()
        if _ready_for is conn:
            return
        store._exec(_MARIADB if config.DB_URL else _SQLITE)
        _ready_for = conn


def _now() -> datetime:
    return datetime.now(config.TZ)


def _is_active(h: dict, now: datetime) -> bool:
    return bool(h.get("active")) and not (h.get("until") and datetime.fromisoformat(h["until"]) <= now)


def public(h: dict) -> dict:
    """What buyers and their agents see of a hold (no Valio staff names)."""
    return {"id": h["id"], "product_id": h["product_id"], "name": h.get("name"), "kind": h["kind"],
            "reason": h["reason"], "since": h["created_at"], "until": h.get("until")}


def _put(h: dict) -> None:
    _ensure()
    store._upsert("product_hold", COLS, (h["id"], h["product_id"], h["kind"], int(bool(h["active"])),
                                         json.dumps(h, ensure_ascii=False), h["created_at"], h["updated_at"]))
    invalidate()


def get(hold_id: str) -> dict | None:
    _ensure()
    r = store._exec("SELECT data FROM product_hold WHERE id = ?", (hold_id,))[0]
    return json.loads(r[0]["data"]) if r else None


def list_active() -> list[dict]:
    _ensure()
    now = _now()
    rows = store._exec("SELECT data FROM product_hold WHERE active = 1 ORDER BY created_at")[0]
    return [h for h in (json.loads(r["data"]) for r in rows) if _is_active(h, now)]


def _active_map() -> dict[str, dict]:
    """{product_id: hold} of active holds, cached briefly. No state database yet means no holds (pure unit tests
    of quote.build_offer never create one)."""
    conn = store._conn
    if conn is None and not config.DB_URL and not config.STATE_DB.exists():
        return {}
    if _cache["conn"] is conn and conn is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
        m = _cache["map"]
    else:
        m = {}
        for h in list_active():
            m.setdefault(h["product_id"], h)  # oldest first; recall wins below
            if h["kind"] == "recall":
                m[h["product_id"]] = h
        _cache.update(conn=store._conn, at=time.monotonic(), map=m)
    now = _now()
    return {pid: h for pid, h in m.items() if _is_active(h, now)}


def active_for(product_id: str) -> dict | None:
    """The active hold on a product (a recall if there are several), or None."""
    return _active_map().get(product_id)


def message(h: dict) -> str:
    until = f" until {h['until'][:16].replace('T', ' ')}" if h.get("until") else ""
    return f"Valio Aimo has put this product {KIND_TEXT[h['kind']]}{until}: {h['reason']}"


def card_info(product_id: str) -> dict | None:
    """The `held` entry for a search card."""
    h = active_for(product_id)
    return {k: v for k, v in public(h).items() if k in ("id", "kind", "reason", "since", "until")} if h else None


# --- create / lift ------------------------------------------------------------

def invalidate() -> None:
    """Forget the cached hold map, so the next check reads the database (other workers' holds included)."""
    _cache["conn"] = None


def _order_lock():
    """service._order_lock: a hold is written and its affected orders listed while no order is being placed or
    approved, so an order either completes before the hold (and is listed as affected) or sees the hold."""
    from . import service as S
    return S._order_lock


def create(req: HoldIn, name: str | None = None) -> dict:
    with _order_lock():
        return _create(req, name)


def _create(req: HoldIn, name: str | None) -> dict:
    now = _now().isoformat(timespec="milliseconds")
    until = req.until
    if until is not None and until.tzinfo is None:
        until = until.replace(tzinfo=config.TZ)
    h = {"id": "H-" + secrets.token_hex(4).upper(), "product_id": req.product_id, "name": name, "kind": req.kind,
         "reason": req.reason.strip(), "until": until.isoformat(timespec="seconds") if until else None,
         "active": True, "by": req.by, "created_at": now, "updated_at": now}
    _put(h)
    store.audit("hold.created", req.by, "human", h["id"], None, {"hold": public(h), "by": req.by})
    h["affected"] = _notify_affected(h)
    return {**public(h), "affected": h["affected"]}


def lift(hold_id: str, by: str = "valio") -> dict | None:
    with _order_lock():
        return _lift(hold_id, by)


def _lift(hold_id: str, by: str) -> dict | None:
    h = get(hold_id)
    if not h:
        return None
    if h["active"]:
        h["active"] = False
        h["updated_at"] = _now().isoformat(timespec="milliseconds")
        h["lifted_by"] = by
        _put(h)
        store.audit("hold.lifted", by, "human", hold_id, None, {"hold": public(h), "by": by})
    return {**public(h), "active": False, "lifted_at": h["updated_at"]}


# --- who is affected ----------------------------------------------------------

def _affected(product_id: str, kind: str) -> dict[str, dict]:
    """Per mandate: open pending_approval orders, active standing orders and (recalls) recently placed orders
    containing the product."""
    from . import standing_store

    out: dict[str, dict] = {}

    def slot(mid: str, buyer: str | None) -> dict:
        return out.setdefault(mid, {"buyer": buyer, "pending": [], "standing": [], "recent": []})

    def has(o: dict) -> bool:
        return any(l.get("product_id") == product_id for l in o.get("lines") or [])

    for o in store.list_orders(status="pending_approval"):
        if has(o):
            slot(o["mandate_id"], o.get("buyer"))["pending"].append(o)
    for so in standing_store.list_():
        if so.get("active") and any(product_id in it.get("product_ids", []) for it in so.get("items") or []):
            slot(so["mandate_id"], so.get("buyer"))["standing"].append(so)
    if kind == "recall":
        since = (_now() - timedelta(days=RECALL_LOOKBACK_DAYS)).isoformat()
        for o in store.list_orders(status="ordered"):
            if has(o) and datetime.fromisoformat(o["created_at"]) >= datetime.fromisoformat(since):
                slot(o["mandate_id"], o.get("buyer"))["recent"].append(o)
    return out


def _notify_affected(h: dict) -> list[dict]:
    from . import notify, service as S

    summary = []
    for mid, g in _affected(h["product_id"], h["kind"]).items():
        try:
            mandate = S._mandate(mid)
        except Exception:  # noqa: BLE001 - a broken mandate must not stop the hold
            mandate = None
        buyer = g["buyer"] or (mandate.buyer if mandate else None)
        ref = lambda o, role: {k: v for k, v in {  # noqa: E731
            "id": o["id"], "role": role, "status": o.get("status"), "name": o.get("name"),
            "created_at": o.get("created_at"), "delivery_date": o.get("delivery_date")}.items() if v is not None}
        refs = ([ref(o, "pending_approval") for o in g["pending"]] + [ref(s, "standing_order") for s in g["standing"]]
                + [ref(o, "recently_ordered") for o in g["recent"]])
        for r in refs:
            store.audit("product.held", "valio", "system", r["id"], buyer,
                        {"hold": public(h), "role": r["role"], "mandate_id": mid})
        if mandate is not None:
            notify.send("product.held", {"id": refs[0]["id"], "buyer": buyer, "mandate_id": mid,
                                         "hold": public(h), "affected": refs}, mandate)
        summary.append({"mandate_id": mid, "buyer": buyer, "orders": refs})
    return summary
