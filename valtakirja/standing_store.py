"""Storage for standing orders and their renewals (SQLite or MariaDB), next to the rest of the state.

Tables are created lazily on first use, so store.py's schema stays untouched. On MariaDB
`standing_order` is SYSTEM VERSIONED: every edit (pause, resume, new amounts) keeps the old
row, so "what did the standing order say on <date>?" is one FOR SYSTEM_TIME AS OF query.
Renewals are append-only rows; synthetic history for demos is stored with simulated = 1.
"""

from __future__ import annotations

import json

from . import config, store

_SQLITE = [
    """CREATE TABLE IF NOT EXISTS standing_order (
      id TEXT PRIMARY KEY, mandate_id TEXT NOT NULL, agent_id TEXT NOT NULL, active INTEGER NOT NULL,
      data TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS standing_renewal (
      id TEXT PRIMARY KEY, standing_id TEXT NOT NULL, delivery_date TEXT NOT NULL, simulated INTEGER NOT NULL,
      order_id TEXT, data TEXT NOT NULL, created_at TEXT NOT NULL)""",
]
_MARIADB = [
    """CREATE TABLE IF NOT EXISTS standing_order (
      id VARCHAR(32) PRIMARY KEY, mandate_id VARCHAR(64) NOT NULL, agent_id VARCHAR(128) NOT NULL,
      active TINYINT NOT NULL, data LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL,
      updated_at VARCHAR(40) NOT NULL, INDEX (mandate_id)
    ) WITH SYSTEM VERSIONING""",
    """CREATE TABLE IF NOT EXISTS standing_renewal (
      id VARCHAR(32) PRIMARY KEY, standing_id VARCHAR(32) NOT NULL, delivery_date VARCHAR(10) NOT NULL,
      simulated TINYINT NOT NULL, order_id VARCHAR(32), data LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL,
      INDEX (standing_id))""",
]

_ready_for = None  # the connection the tables were last ensured on (tests swap connections)


def _ensure() -> None:
    global _ready_for
    with store._lock:
        conn = store.db()
        if _ready_for is conn:
            return
        for stmt in _MARIADB if config.DB_URL else _SQLITE:
            store._exec(stmt)
        _ready_for = conn


COLS = ["id", "mandate_id", "agent_id", "active", "data", "created_at", "updated_at"]


def put(so: dict) -> None:
    """Insert or update a standing order (UPDATE in place on MariaDB, so versioning keeps every version)."""
    _ensure()
    store._upsert("standing_order", COLS, (so["id"], so["mandate_id"], so["agent_id"], int(bool(so["active"])),
                                           json.dumps(so, ensure_ascii=False), so["created_at"], so["updated_at"]))


def get(sid: str) -> dict | None:
    _ensure()
    r = store._exec("SELECT data FROM standing_order WHERE id = ?", (sid,))[0]
    return json.loads(r[0]["data"]) if r else None


def list_(mandate_id: str | None = None) -> list[dict]:
    _ensure()
    q, args = "SELECT data FROM standing_order", []
    if mandate_id:
        q += " WHERE mandate_id = ?"
        args.append(mandate_id)
    return [json.loads(r["data"]) for r in store._exec(q + " ORDER BY created_at", args)[0]]


def versions(sid: str) -> list[dict]:
    """Every stored version of a standing order, oldest first (MariaDB); SQLite has only the current one."""
    _ensure()
    if not config.DB_URL:
        so = get(sid)
        return [{"valid_from": None, "valid_to": None, "standing_order": so}] if so else []
    rows = store._exec("SELECT data, ROW_START, ROW_END FROM standing_order FOR SYSTEM_TIME ALL WHERE id = ? "
                       "ORDER BY ROW_START", (sid,))[0]
    return [{"valid_from": store._iso(r["ROW_START"]), "valid_to": store._iso_end(r["ROW_END"]),
             "standing_order": json.loads(r["data"])} for r in rows]


def add_renewal(r: dict) -> None:
    _ensure()
    store._exec("INSERT INTO standing_renewal (id, standing_id, delivery_date, simulated, order_id, data, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (r["id"], r["standing_id"], r["delivery_date"], int(bool(r.get("simulated"))), r.get("order_id"),
                 json.dumps(r, ensure_ascii=False), r["created_at"]))


def renewals(sid: str) -> list[dict]:
    """All renewals of a standing order, oldest delivery first."""
    _ensure()
    rows = store._exec("SELECT data FROM standing_renewal WHERE standing_id = ? ORDER BY delivery_date, created_at",
                       (sid,))[0]
    return [json.loads(r["data"]) for r in rows]
