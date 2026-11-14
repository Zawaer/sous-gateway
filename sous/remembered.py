"""Approve and remember: a human's answer to a soft, repetitive hold, remembered per mandate and product.

The same held line comes back every week (the usual substitute while the usual product is out, a product that is
still "first time" until it is ordered, 20 ST read as KI, oat drink for milk). When the buyer's human approves such
an order they may tick "remember" per finding; the gateway then records a remembered decision, and the next time
the same finding fires under that mandate it is a notify ("previously approved by X on DATE, until DATE") instead
of an ask.

Narrow on purpose (the pattern of sous/clearance.py, which does the same for allergen.unknown):
- only these ask-tier findings: substitution (original -> substitute), product.first_time, unit.interpreted,
  product.type_mismatch (REMEMBERABLE). Nothing else is ever softened: allergens, hard/daily/weekly/split limits,
  holds, payment block, revoked or expired mandates, autonomy and every block-tier finding stay as they are, and a
  finding the mandate itself sets to block (substitutions: block) is never touched;
- one mandate and the exact product(s): a substitution is remembered for that original -> substitute pair only,
  and for what it is used for (the line's `use`, sous/use_sensitivity.py): a substitute approved for coffee asks
  again in a dessert; substitution.use_sensitive itself is never softened;
- tied to the product data the human saw (a fingerprint of names, units and category per product) and, for unit
  and type findings, to the gateway's reading (its message without the numbers): when either changes, ask again;
- expires (DEFAULT_DAYS, at most MAX_DAYS), optional caps on the line's quantity and total, revocable;
- recorded only from a human approval (approver code or passkey) that turned the order into "ordered": agents
  can't approve, so they can't remember either;
- signed (typ sous-remembered+jws) and verified before every use; audited (remembered.recorded / .revoked).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from . import catalog, config, signing, store
from .use_sensitivity import use_key

DEFAULT_DAYS = 30
MAX_DAYS = 90
TYP = "sous-remembered+jws"
# finding rule -> the notify rule it becomes when a remembered decision matches
REMEMBERABLE = {"substitution": "remembered.substitution", "product.first_time": "remembered.first_time",
                "unit.interpreted": "remembered.unit_interpreted", "product.type_mismatch": "remembered.type_mismatch"}
_READING = ("unit.interpreted", "product.type_mismatch")  # also tied to what the gateway assumed

_SQLITE = """CREATE TABLE IF NOT EXISTS remembered_decision (
  id TEXT PRIMARY KEY, mandate_id TEXT NOT NULL, product_id TEXT NOT NULL, active INTEGER NOT NULL,
  data TEXT NOT NULL, created_at TEXT NOT NULL)"""
_MARIADB = """CREATE TABLE IF NOT EXISTS remembered_decision (
  id VARCHAR(32) PRIMARY KEY, mandate_id VARCHAR(128) NOT NULL, product_id VARCHAR(64) NOT NULL,
  active TINYINT NOT NULL, data LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL,
  INDEX (mandate_id, product_id)
) WITH SYSTEM VERSIONING"""
COLS = ["id", "mandate_id", "product_id", "active", "data", "created_at"]
_ready_for = None


class RememberIn(BaseModel):
    """One finding of the held order to remember (approve only)."""
    rule: str = Field(description="The finding's rule: " + ", ".join(REMEMBERABLE))
    product_id: str = Field(description="The finding's product_id (for a substitution: the substitute)")
    days: int = Field(DEFAULT_DAYS, ge=1, le=MAX_DAYS, description=f"How long (default {DEFAULT_DAYS}, max {MAX_DAYS})")
    max_quantity: float | None = Field(None, gt=0, description="Ask again above this many sales units on the line")
    max_line_total_eur: float | None = Field(None, gt=0, description="Ask again above this line total (EUR ex VAT)")


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
    """What the human looked at: the product's names, brand, category and units. None for an unknown product."""
    if not product:
        return None
    seen = {k: product.get(k) for k in ("name", "names", "brand", "category", "sales_unit", "base_unit", "units",
                                         "size_from_name")}
    return hashlib.sha256(json.dumps(seen, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:24]


def reading(message: str | None) -> str:
    """The gateway's reading in a unit/type finding, without its numbers (20 ST and 40 ST read the same way)."""
    return hashlib.sha256(re.sub(r"\d+(?:[.,]\d+)?", "#", message or "").encode()).hexdigest()[:16]


def _put(r: dict) -> None:
    _ensure()
    store._upsert("remembered_decision", COLS, (r["id"], r["mandate_id"], r["product_id"], int(bool(r["active"])),
                                                json.dumps(r, ensure_ascii=False), r["created_at"]))


def _sign(r: dict) -> dict:
    body = {k: v for k, v in r.items() if k != "jws"}
    return {**body, "jws": signing.sign(body, TYP)}


def get(rid: str) -> dict | None:
    _ensure()
    r = store._exec("SELECT data FROM remembered_decision WHERE id = ?", (rid,))[0]
    return json.loads(r[0]["data"]) if r else None


def list_for(mandate_id: str, active_only: bool = True, now: datetime | None = None) -> list[dict]:
    """Newest first. active_only: not revoked, superseded or expired."""
    if store._conn is None and not config.DB_URL and not config.STATE_DB.exists():
        return []
    _ensure()
    sql = ("SELECT data, mandate_id, product_id FROM remembered_decision WHERE mandate_id = ?"
           + (" AND active = 1" if active_only else ""))
    out = []
    for row in store._exec(sql + " ORDER BY created_at DESC", (mandate_id,))[0]:
        r = json.loads(row["data"])
        # the signed body names its own mandate and product: a row copied under another mandate (or product) column
        # is not this mandate's decision
        if r.get("mandate_id") == mandate_id == row["mandate_id"] and r.get("product_id") == row["product_id"]:
            out.append(r)
    now = now or _now()
    return [r for r in out if not active_only or datetime.fromisoformat(r["expires_at"]) > now]


def summary(r: dict) -> dict:
    """The short form for GET /mandates/{id}/remembered and MCP my_mandates / get_mandate."""
    return {k: r.get(k) for k in ("id", "rule", "product_id", "original_product_id", "use", "name", "by", "authority",
                                  "order_id", "created_at", "expires_at", "max_quantity", "max_line_total_eur",
                                  "active", "revoked_at", "revoked_by", "jws")}


def for_lines(mandate, lines: list[dict], now: datetime | None = None) -> list[dict]:
    """The active remembered decisions of this mandate for the lines' products (checked in detail by soften)."""
    if not lines:
        return []
    pids = {l.get("product_id") for l in lines}
    return [r for r in list_for(mandate.id, now=now) if r["product_id"] in pids]


def _valid(r: dict, now: datetime, mandate_id: str | None = None) -> bool:
    if not r.get("active") or datetime.fromisoformat(r["expires_at"]) <= now:
        return False
    if mandate_id is not None and r.get("mandate_id") != mandate_id:
        return False  # signed for another mandate (checked again below against the signature)
    signed = signing.verify(r.get("jws") or "", TYP)
    if signed != {k: v for k, v in r.items() if k != "jws"}:
        return False  # the stored row was changed after it was signed
    return all(fingerprint(catalog.get(pid)) == fp for pid, fp in (r.get("fingerprints") or {}).items())


def _matches(r: dict, f, lines: list[dict]) -> bool:
    """`lines`: every line of the quote with the finding's product. Each must be what was remembered, and the caps
    apply to them together (two lines of 200 L + 10 L are 210 L against a 50 L cap, not 10 L)."""
    if r["rule"] != f.rule or r["product_id"] != f.product_id or not lines:
        return False
    for line in lines:
        if f.rule == "substitution" and r.get("original_product_id") != line.get("substitute_for"):
            return False
        # what the substitute is used for (sous/use_sensitivity.py): one approved for coffee is not one for a dessert
        if f.rule == "substitution" and r.get("use") != use_key(line.get("use")):
            return False
    if f.rule in _READING and r.get("reading") != reading(f.message):
        return False
    quantity = sum(line.get("quantity") or 0 for line in lines)
    total = sum(round((line.get("line_total_ex_vat") or 0) * 100) for line in lines)
    if r.get("max_quantity") is not None and quantity > r["max_quantity"] + 1e-9:
        return False
    if r.get("max_line_total_eur") is not None and total > round(r["max_line_total_eur"] * 100):
        return False
    return True


def soften(findings: list, lines: list[dict], decisions: list[dict] | None, now: datetime,
           mandate_id: str | None = None) -> list:
    """`findings` with each ask of a REMEMBERABLE rule that a valid remembered decision of `mandate_id` covers
    turned into its notify twin (remembered.*). Every other finding, and any block, is returned unchanged."""
    if not decisions:
        return findings
    from . import fi
    by_pid: dict = {}
    for l in lines:
        by_pid.setdefault(l.get("product_id"), []).append(l)
    out = []
    for f in findings:
        same = by_pid.get(f.product_id) or []
        hit = None
        if f.outcome == "ask" and f.rule in REMEMBERABLE and same:
            hit = next((r for r in decisions if _matches(r, f, same) and _valid(r, now, mandate_id)), None)
        if hit is None:
            out.append(f)
            continue
        out.append(f.model_copy(update={
            "rule": REMEMBERABLE[f.rule], "outcome": "notify", "rule_url": None,
            "message": f"{f.message} " + message(hit),
            "message_fi": f"{fi.finding(f)} " + fi.remembered(hit)}))
        out[-1].model_post_init(None)  # rule_url for the new rule id
    return out


def message(r: dict) -> str:
    return (f"Previously approved by {r['by']} on {r['created_at'][8:10]}.{r['created_at'][5:7]}. (order "
            f"{r['order_id']}) and remembered until {r['expires_at'][8:10]}.{r['expires_at'][5:7]}. ({r['id']}; "
            "revocable): ordered without asking again.")


def eligible(order: dict) -> list[dict]:
    """The findings of a held order that a human may ask to remember: ask-tier, of a REMEMBERABLE rule."""
    return [f for f in (order.get("decision") or {}).get("findings", [])
            if f.get("outcome") == "ask" and f.get("rule") in REMEMBERABLE and f.get("product_id")]


def check(order: dict, picks: list[RememberIn]) -> None:
    """Raise unless every pick names an eligible finding of this held order (before anything is decided)."""
    from .service import GatewayError
    ok = {(f["rule"], f["product_id"]) for f in eligible(order)}
    bad = [f"{p.rule} {p.product_id}" for p in picks if (p.rule, p.product_id) not in ok]
    if bad:
        raise GatewayError("not_rememberable",
                           f"Order {order.get('id')} has no ask-tier finding to remember for: {', '.join(bad)}. Only "
                           f"{', '.join(REMEMBERABLE)} findings that held this order can be remembered "
                           f"(GET /orders/{order.get('id')}: decision.findings).", 422)


def record(order: dict, mandate, picks: list[RememberIn], by: str, authority: str | None) -> list[dict]:
    """After a human approved `order` into an order: one signed remembered decision per pick. Supersedes an older
    one for the same mandate, rule and product(s). Returns the new decisions."""
    if order.get("status") != "ordered" or not picks:
        return []
    now, out = _now(), []
    findings = {(f["rule"], f["product_id"]): f for f in eligible(order)}
    # order lines don't carry substitute_for: the original product comes from the order's signed quote
    row = store.get_quote(order["quote_id"]) if order.get("quote_id") else None
    q = signing.verify(row["_jws"], "sous-quote+jws") if row else None
    originals = {l["product_id"]: l.get("substitute_for") for l in (q or {}).get("lines", [])}
    uses = {l["product_id"]: l.get("use") for l in (q or {}).get("lines", [])}
    for p in picks:
        f = findings.get((p.rule, p.product_id))
        if not f:
            continue
        line = next((l for l in order.get("lines") or [] if l.get("product_id") == p.product_id), {})
        original = (line.get("substitute_for") or originals.get(p.product_id)) if p.rule == "substitution" else None
        if p.rule == "substitution" and not original:
            continue  # without the original the pair can't be pinned: nothing is remembered
        pids = [p.product_id] + ([original] if original else [])
        for old in list_for(mandate.id, now=now):
            if (old["rule"], old["product_id"], old.get("original_product_id")) == (p.rule, p.product_id, original):
                _put(_sign({**old, "active": False, "superseded_at": now.isoformat(timespec="seconds")}))
        r = {"id": "REM-" + secrets.token_hex(4).upper(), "mandate_id": mandate.id, "buyer": order.get("buyer"),
             "rule": p.rule, "product_id": p.product_id, "original_product_id": original,
             "name": line.get("name") or (catalog.get(p.product_id) or {}).get("name") or p.product_id,
             "fingerprints": {pid: fingerprint(catalog.get(pid)) for pid in pids},
             "reading": reading(f.get("message")) if p.rule in _READING else None,
             **({"use": use_key(line.get("use") or uses.get(p.product_id))} if p.rule == "substitution" else {}),
             "approved_line": {k: line.get(k) for k in ("quantity", "sales_unit", "line_total_ex_vat", "unit_note")},
             "max_quantity": p.max_quantity, "max_line_total_eur": p.max_line_total_eur,
             "by": by, "authority": authority, "order_id": order["id"], "active": True,
             "created_at": now.isoformat(timespec="seconds"),
             "expires_at": (now + timedelta(days=p.days)).isoformat(timespec="seconds")}
        r = _sign(r)
        _put(r)
        store.audit("remembered.recorded", by, "human", order["id"], order.get("buyer"),
                    {k: r[k] for k in ("id", "mandate_id", "rule", "product_id", "original_product_id", "authority",
                                       "expires_at", "max_quantity", "max_line_total_eur", "fingerprints")})
        out.append(r)
    return out


def revoke(mandate_id: str, rid: str, by: str, authority: str | None = None) -> dict | None:
    r = get(rid)
    if not r or r["mandate_id"] != mandate_id:
        return None
    if r.get("active"):
        r = _sign({**r, "active": False, "revoked_at": _now().isoformat(timespec="seconds"), "revoked_by": by})
        _put(r)
        store.audit("remembered.revoked", by, "human", r["order_id"], r.get("buyer"),
                    {"id": rid, "mandate_id": mandate_id, "rule": r["rule"], "product_id": r["product_id"],
                     "authority": authority})
    return r
