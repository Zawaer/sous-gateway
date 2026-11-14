"""Valio-side pause of agent ordering: the seller's kill switch, for every buyer or for one buyer.

Next to product holds (sous/holds.py, one product) this stops agents from ordering anything at all, e.g. during
an incident, a suspected runaway agent or a buyer's unpaid invoices. It needs the global approver code
(POST /agent-ordering/pause and /resume); the current state is public (GET /agent-ordering) because buyers'
agents need to know why they are refused.

Effects: mandate.evaluate fires rule `ordering.paused` (block) on every order under an affected mandate, so a
quote's mandate preview shows it, placing an order is refused, and approving a held order is refused too
(service._approval_blocker re-runs the rules): while paused, nothing reaches Valio Aimo. Searching and quoting
still work. Resuming affects new decisions only; refused orders stay refused.

Scope: `buyer` None pauses every buyer (sandboxes included); a buyer name pauses that buyer's mandates only.
Stored in table agent_pause (SYSTEM VERSIONED on MariaDB, so "was ordering paused when that order was decided?"
is one FOR SYSTEM_TIME AS OF query); audited as ordering.paused / ordering.resumed.
"""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import BaseModel, Field

from . import config, store

ALL = "*"

_SQLITE = """CREATE TABLE IF NOT EXISTS agent_pause (
  scope TEXT PRIMARY KEY, active INTEGER NOT NULL, data TEXT NOT NULL, updated_at TEXT NOT NULL)"""
_MARIADB = """CREATE TABLE IF NOT EXISTS agent_pause (
  scope VARCHAR(191) PRIMARY KEY, active TINYINT NOT NULL, data LONGTEXT NOT NULL, updated_at VARCHAR(40) NOT NULL
) WITH SYSTEM VERSIONING"""
COLS = ["scope", "active", "data", "updated_at"]

_ready_for = None  # the connection the table was last ensured on (tests swap connections)


class PauseIn(BaseModel):
    buyer: str | None = Field(None, max_length=191, description="Pause only this buyer's mandates; omit for every buyer")
    reason: str = Field(min_length=1, max_length=500, description="Shown to buyers and their agents as written")
    by: str = Field("valio", max_length=100, description="Who at Valio paused ordering")


class ResumeIn(BaseModel):
    buyer: str | None = Field(None, max_length=191, description="The scope to resume; omit for the every-buyer pause")
    by: str = Field("valio", max_length=100)


def _ensure() -> None:
    global _ready_for
    with store._lock:
        conn = store.db()
        if _ready_for is conn:
            return
        store._exec(_MARIADB if config.DB_URL else _SQLITE)
        _ready_for = conn


def _now() -> str:
    return datetime.now(config.TZ).isoformat(timespec="milliseconds")


def _rows() -> list[dict]:
    if store._conn is None and not config.DB_URL and not config.STATE_DB.exists():
        return []  # no state database yet: nothing can be paused (pure unit tests of the engine)
    _ensure()
    return [json.loads(r["data"]) for r in store._exec("SELECT data FROM agent_pause WHERE active = 1")[0]]


def active() -> list[dict]:
    """Every active pause: [{scope, buyer, reason, since}], the every-buyer one first."""
    return sorted((public(p) for p in _rows()), key=lambda p: (p["scope"] != ALL, p["scope"]))


def active_for(buyer: str | None) -> dict | None:
    """The pause that stops this buyer's agents (the every-buyer one wins), or None."""
    by_scope = {p["scope"]: p for p in _rows()}
    return by_scope.get(ALL) or (by_scope.get(buyer) if buyer else None)


def public(p: dict) -> dict:
    return {"scope": p["scope"], "buyer": None if p["scope"] == ALL else p["scope"], "reason": p["reason"],
            "since": p["since"]}


def message(p: dict) -> str:
    who = "every buyer" if p["scope"] == ALL else p["scope"]
    return (f"Valio Aimo has paused agent ordering for {who} since {p['since'][:16].replace('T', ' ')}: "
            f"{p['reason']} Nothing can be ordered or approved until Valio Aimo resumes it.")


def _order_lock():
    """service._order_lock: a pause is written while no order is being placed or approved, so an order either
    completes before the pause or is evaluated after it (the engine reads pauses from the database, uncached)."""
    from . import service as S
    return S._order_lock


def pause(req: PauseIn) -> dict:
    _ensure()
    scope = req.buyer or ALL
    now = _now()
    p = {"scope": scope, "active": True, "reason": req.reason, "since": now, "by": req.by}
    with _order_lock():
        store._upsert("agent_pause", COLS, (scope, 1, json.dumps(p, ensure_ascii=False), now))
    store.audit("ordering.paused", req.by, "human", scope, None if scope == ALL else scope,
                {"pause": public(p), "by": req.by})
    return status()


def resume(req: ResumeIn) -> dict | None:
    """None when that scope wasn't paused."""
    _ensure()
    scope = req.buyer or ALL
    cur = next((p for p in _rows() if p["scope"] == scope), None)
    if cur is None:
        return None
    now = _now()
    p = {**cur, "active": False, "resumed_at": now, "resumed_by": req.by}
    with _order_lock():
        store._upsert("agent_pause", COLS, (scope, 0, json.dumps(p, ensure_ascii=False), now))
    store.audit("ordering.resumed", req.by, "human", scope, None if scope == ALL else scope,
                {"scope": scope, "paused_since": cur["since"], "by": req.by})
    return status()


def status() -> dict:
    act = active()
    return {"paused_for_everyone": any(p["scope"] == ALL for p in act), "pauses": act}
