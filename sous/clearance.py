"""A human's allergen clearance, remembered per buyer and product.

Nearly half of Valio Aimo's catalog declares no allergens, so a kitchen that avoids allergens sees the same
product held for `allergen.unknown` again and again. When the buyer's human approves such an order they may tick
"remember": the gateway then records a clearance for that mandate and product. The next order of the product
under the same mandate gets `allergen.cleared` (notify: ordered, and the buyer is told whose clearance it used)
instead of being held again.

A clearance is narrow on purpose:
- one mandate (one buyer and its rules) and one product, never a product family or another buyer;
- it covers the allergens the mandate avoided when the human cleared it: a mandate that now avoids more is held
  again;
- it is tied to the product data it was given on (a fingerprint of names, allergen data and claims): when Valio
  changes the product, it no longer applies;
- it expires (CLEARANCE_DAYS) and can be revoked (DELETE /clearances/{id});
- it only softens `allergen.unknown`. A product that declares an avoided allergen, or that the Gemini check finds
  likely to contain one (allergen.likely), is refused as before; a mandate whose unknown_allergens is `block`
  never has a human approving such a line, so it never gets a clearance.

Every clearance and revocation is audited (allergen.cleared_recorded / allergen.clearance_revoked).
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta

from . import catalog, config, store

CLEARANCE_DAYS = 90

_SQLITE = """CREATE TABLE IF NOT EXISTS allergen_clearance (
  id TEXT PRIMARY KEY, mandate_id TEXT NOT NULL, product_id TEXT NOT NULL, active INTEGER NOT NULL,
  data TEXT NOT NULL, created_at TEXT NOT NULL)"""
_MARIADB = """CREATE TABLE IF NOT EXISTS allergen_clearance (
  id VARCHAR(32) PRIMARY KEY, mandate_id VARCHAR(128) NOT NULL, product_id VARCHAR(64) NOT NULL,
  active TINYINT NOT NULL, data LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL,
  INDEX (mandate_id, product_id)
) WITH SYSTEM VERSIONING"""
COLS = ["id", "mandate_id", "product_id", "active", "data", "created_at"]
_ready_for = None


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


def fingerprint(product: dict | None) -> str | None:
    """What the human looked at: the product's names, allergen data and claims. None for an unknown product."""
    if not product:
        return None
    seen = {k: product.get(k) for k in ("name", "names", "brand", "allergens", "claims")}
    return hashlib.sha256(json.dumps(seen, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def _put(c: dict) -> None:
    _ensure()
    store._upsert("allergen_clearance", COLS, (c["id"], c["mandate_id"], c["product_id"], int(bool(c["active"])),
                                               json.dumps(c, ensure_ascii=False), c["created_at"]))


def get(cid: str) -> dict | None:
    _ensure()
    r = store._exec("SELECT data FROM allergen_clearance WHERE id = ?", (cid,))[0]
    return json.loads(r[0]["data"]) if r else None


def list_for(mandate_id: str | None = None, active_only: bool = True) -> list[dict]:
    _ensure()
    sql, args = "SELECT data FROM allergen_clearance", []
    where = []
    if mandate_id:
        where.append("mandate_id = ?"); args.append(mandate_id)
    if active_only:
        where.append("active = 1")
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = store._exec(sql + " ORDER BY created_at DESC", tuple(args))[0]
    out = [json.loads(r["data"]) for r in rows]
    now = _now()
    return [c for c in out if not active_only or datetime.fromisoformat(c["expires_at"]) > now]


def _valid(c: dict, mandate, now: datetime) -> bool:
    if not c.get("active") or datetime.fromisoformat(c["expires_at"]) <= now:
        return False
    if not set(mandate.avoid_allergens) <= set(c.get("allergens") or []):
        return False  # the mandate now avoids something the human didn't clear
    return fingerprint(catalog.get(c["product_id"])) == c.get("fingerprint")


def for_lines(mandate, lines: list[dict], now: datetime | None = None) -> dict[str, dict]:
    """{product_id: clearance} for the lines' products that this mandate's human has cleared and that still hold.
    Empty when the mandate avoids no allergens or there is no state database yet."""
    if not mandate.avoid_allergens or not lines:
        return {}
    if store._conn is None and not config.DB_URL and not config.STATE_DB.exists():
        return {}
    now = now or _now()
    pids = {l.get("product_id") for l in lines}
    return {c["product_id"]: c for c in reversed(list_for(mandate.id)) if c["product_id"] in pids and _valid(c, mandate, now)}


def record_from_order(order: dict, mandate, by: str, authority: str | None, note: str | None = None) -> list[dict]:
    """After a human approved `order` into an order: one clearance per product it was held for allergen.unknown.
    Supersedes an older clearance of the same mandate and product. Returns the new clearances."""
    if order.get("status") != "ordered" or not mandate.avoid_allergens:
        return []
    pids = sorted({f.get("product_id") for f in (order.get("decision") or {}).get("findings", [])
                   if f.get("rule") == "allergen.unknown" and f.get("product_id")})
    now, out = _now(), []
    for pid in pids:
        product = catalog.get(pid)
        for old in list_for(mandate.id):
            if old["product_id"] == pid:
                _put({**old, "active": False, "superseded_at": now.isoformat(timespec="seconds")})
        line = next((l for l in order.get("lines") or [] if l.get("product_id") == pid), {})
        c = {"id": "CLR-" + secrets.token_hex(4).upper(), "mandate_id": mandate.id, "buyer": order.get("buyer"),
             "product_id": pid, "name": line.get("name") or (product or {}).get("name") or pid,
             "allergens": sorted(mandate.avoid_allergens), "fingerprint": fingerprint(product),
             "by": by, "authority": authority, "order_id": order["id"], "note": note, "active": True,
             "created_at": now.isoformat(timespec="seconds"),
             "expires_at": (now + timedelta(days=CLEARANCE_DAYS)).isoformat(timespec="seconds")}
        _put(c)
        store.audit("allergen.clearance_recorded", by, "human", order["id"], order.get("buyer"),
                    {k: c[k] for k in ("id", "mandate_id", "product_id", "name", "allergens", "authority",
                                       "expires_at", "fingerprint")})
        out.append(c)
    return out


def revoke(cid: str, by: str, authority: str | None = None) -> dict | None:
    c = get(cid)
    if not c:
        return None
    if c.get("active"):
        c = {**c, "active": False, "revoked_at": _now().isoformat(timespec="seconds"), "revoked_by": by}
        _put(c)
        store.audit("allergen.clearance_revoked", by, "human", c["order_id"], c.get("buyer"),
                    {"id": cid, "mandate_id": c["mandate_id"], "product_id": c["product_id"], "authority": authority})
    return c


def message(name: str, c: dict, avoid: str) -> str:
    return (f"{name} has no allergen data; {c['by']} confirmed it free of {avoid} for this buyer on "
            f"{c['created_at'][8:10]}.{c['created_at'][5:7]} (order {c['order_id']}). Remembered until "
            f"{c['expires_at'][8:10]}.{c['expires_at'][5:7]}; revoke it at any time.")
