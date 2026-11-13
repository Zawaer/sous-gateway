"""In-app notification feed, stored next to the rest of the state (SQLite or MariaDB).

The table is created lazily on first use, so store.py's schema stays untouched.
"""

from __future__ import annotations

import json

from . import config, store

_SQLITE = """CREATE TABLE IF NOT EXISTS notification (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, event TEXT NOT NULL, audience TEXT NOT NULL,
  buyer TEXT, order_id TEXT, tier TEXT, title TEXT NOT NULL, message TEXT NOT NULL, url TEXT,
  data TEXT NOT NULL)"""
_MARIADB = """CREATE TABLE IF NOT EXISTS notification (
  seq BIGINT AUTO_INCREMENT PRIMARY KEY, ts VARCHAR(40) NOT NULL, event VARCHAR(64) NOT NULL,
  audience VARCHAR(16) NOT NULL, buyer VARCHAR(191), order_id VARCHAR(32), tier VARCHAR(16),
  title VARCHAR(255) NOT NULL, message TEXT NOT NULL, url VARCHAR(512), data LONGTEXT NOT NULL,
  INDEX (buyer), INDEX (order_id))"""

_ready_for = None  # the connection the table was last ensured on (tests swap connections)


def _ensure() -> None:
    global _ready_for
    with store._lock:
        conn = store.db()
        if _ready_for is conn:
            return
        store._exec(_MARIADB if config.DB_URL else _SQLITE)
        _ready_for = conn


def add(n: dict) -> dict:
    """Store one notification; returns it with its seq and timestamp."""
    _ensure()
    rec = {"ts": store.now_iso(), **n}
    _, _, seq = store._exec(
        "INSERT INTO notification (ts, event, audience, buyer, order_id, tier, title, message, url, data) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rec["ts"], rec["event"], rec["audience"], rec.get("buyer"), rec.get("order_id"), rec.get("tier"),
         rec["title"][:255], rec["message"], rec.get("url"), json.dumps(rec.get("data") or {}, ensure_ascii=False)))
    return {**rec, "seq": seq}


def list_(buyer: str | None = None, limit: int = 50, order_id: str | None = None,
          sandbox: str | None = None) -> list[dict]:
    """Newest first. `sandbox`: see store.sandbox_where."""
    _ensure()
    q, args = store.sandbox_where(sandbox, ("buyer",))
    q = "SELECT * FROM notification WHERE 1=1" + q
    if buyer:
        q += " AND buyer = ?"
        args.append(buyer)
    if order_id:
        q += " AND order_id = ?"
        args.append(order_id)
    q += " ORDER BY seq DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    out = []
    for r in store._exec(q, args)[0]:
        r = dict(r)
        r["data"] = json.loads(r["data"])
        out.append(r)
    return out
