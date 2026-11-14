"""How much a buyer's agent may do alone: the buyer's choice in the mandate, capped by Valio Aimo.

Levels, from least to most autonomy:

  advise   the agent searches, quotes and proposes; every order waits for a human (rule autonomy.advise)
  reorder  the agent orders alone only products the buyer has bought before (ordered under the mandate or in its
           usual_products); anything else waits for a human (rule autonomy.reorder)
  order    the agent orders alone whatever the other rules allow

The buyer sets `autonomy` in the mandate (default order). Valio Aimo can cap it per buyer, or for every buyer, with
the global approver code: POST /autonomy-caps {buyer?, max, reason, by}, DELETE /autonomy-caps?buyer=. The agent
then acts at the lower of the two, and every order that the cap holds back names it (rule autonomy.capped). A cap
never raises autonomy, and it never lets an order through that another rule holds or refuses.

Like pauses (sous/pause.py), GET /autonomy-caps is public but says only the every-buyer cap and how many single
buyers are capped; which buyer and why needs the global approver code. Table autonomy_cap (SYSTEM VERSIONED on
MariaDB), audited as autonomy.capped / autonomy.uncapped.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from . import config, store

Level = Literal["advise", "reorder", "order"]
LEVELS: tuple[str, ...] = ("advise", "reorder", "order")
ALL = "*"

TEXT = {"advise": "advise only: every order waits for a human",
        "reorder": "reorder the buyer's usual products alone; anything new waits for a human",
        "order": "order alone whatever the other rules allow"}

_SQLITE = """CREATE TABLE IF NOT EXISTS autonomy_cap (
  scope TEXT PRIMARY KEY, active INTEGER NOT NULL, data TEXT NOT NULL, updated_at TEXT NOT NULL)"""
_MARIADB = """CREATE TABLE IF NOT EXISTS autonomy_cap (
  scope VARCHAR(191) PRIMARY KEY, active TINYINT NOT NULL, data LONGTEXT NOT NULL, updated_at VARCHAR(40) NOT NULL
) WITH SYSTEM VERSIONING"""
COLS = ["scope", "active", "data", "updated_at"]
_ready_for = None


class CapIn(BaseModel):
    buyer: str | None = Field(None, max_length=191, description="Cap only this buyer's agents; omit for every buyer")
    max: Level = Field(description="The most autonomy this buyer's agents get, whatever their mandate says")
    reason: str = Field(min_length=1, max_length=500, description="Shown to the buyer's agent in the finding")
    by: str = Field("valio", max_length=100)


def lower(a: str, b: str) -> str:
    return a if LEVELS.index(a) <= LEVELS.index(b) else b


def _ensure() -> None:
    global _ready_for
    with store._lock:
        conn = store.db()
        if _ready_for is conn:
            return
        store._exec(_MARIADB if config.DB_URL else _SQLITE)
        _ready_for = conn


def _rows() -> list[dict]:
    if store._conn is None and not config.DB_URL and not config.STATE_DB.exists():
        return []
    _ensure()
    return [json.loads(r["data"]) for r in store._exec("SELECT data FROM autonomy_cap WHERE active = 1")[0]]


def cap_for(buyer: str | None) -> dict | None:
    """The tightest active cap on this buyer (every-buyer or their own), or None."""
    caps = [c for c in _rows() if c["scope"] == ALL or (buyer and c["scope"] == buyer)]
    return min(caps, key=lambda c: LEVELS.index(c["max"])) if caps else None


def effective(mandate_level: str, buyer: str | None) -> tuple[str, dict | None]:
    """(level the agent acts at, the cap if it lowered the mandate's level)."""
    cap = cap_for(buyer)
    if cap and LEVELS.index(cap["max"]) < LEVELS.index(mandate_level):
        return cap["max"], cap
    return mandate_level, None


def _now() -> str:
    return datetime.now(config.TZ).isoformat(timespec="milliseconds")


def _order_lock():
    from . import service as S
    return S._order_lock


def set_cap(req: CapIn) -> dict:
    _ensure()
    scope, now = req.buyer or ALL, _now()
    c = {"scope": scope, "max": req.max, "reason": req.reason, "since": now, "by": req.by}
    with _order_lock():
        store._upsert("autonomy_cap", COLS, (scope, 1, json.dumps(c, ensure_ascii=False), now))
    store.audit("autonomy.capped", req.by, "human", scope, None if scope == ALL else scope,
                {"scope": scope, "max": req.max, "reason": req.reason, "by": req.by})
    return status(full=True)


def lift(buyer: str | None, by: str) -> dict | None:
    _ensure()
    scope = buyer or ALL
    cur = next((c for c in _rows() if c["scope"] == scope), None)
    if cur is None:
        return None
    now = _now()
    with _order_lock():
        store._upsert("autonomy_cap", COLS, (scope, 0, json.dumps({**cur, "lifted_at": now, "lifted_by": by},
                                                                   ensure_ascii=False), now))
    store.audit("autonomy.uncapped", by, "human", scope, None if scope == ALL else scope,
                {"scope": scope, "was": cur["max"], "by": by})
    return status(full=True)


def status(full: bool = False) -> dict:
    rows = _rows()
    everyone = next((c for c in rows if c["scope"] == ALL), None)
    out = {"levels": {k: TEXT[k] for k in LEVELS},
           "everyone": {"max": everyone["max"], "reason": everyone["reason"], "since": everyone["since"]}
           if everyone else None,
           "buyer_caps": sum(1 for c in rows if c["scope"] != ALL)}
    if full:
        out["caps"] = [{"scope": c["scope"], "buyer": None if c["scope"] == ALL else c["scope"], "max": c["max"],
                        "reason": c["reason"], "since": c["since"]} for c in rows]
    return out
