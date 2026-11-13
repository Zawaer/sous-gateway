"""Persistence for mandates, quotes, orders and the audit log.

SQLite for development; the schema is plain SQL so it ports to MariaDB, where the
audit and order tables become SYSTEM VERSIONED. The audit log is append-only and
hash-chained: each event includes the hash of the previous one, so any edit or
deletion breaks the chain and `verify_chain()` reports where.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime

from . import config
from .signing import canonical

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS mandate (
  id TEXT PRIMARY KEY, data TEXT NOT NULL, jws TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quote (
  id TEXT PRIMARY KEY, data TEXT NOT NULL, jws TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, order_id TEXT);
CREATE TABLE IF NOT EXISTS purchase_order (
  id TEXT PRIMARY KEY, quote_id TEXT NOT NULL, mandate_id TEXT NOT NULL, agent_id TEXT NOT NULL,
  status TEXT NOT NULL, total_ex_vat REAL NOT NULL, data TEXT NOT NULL,
  idempotency_key TEXT UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_event (
  seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL, actor_type TEXT NOT NULL,
  event TEXT NOT NULL, subject TEXT, buyer TEXT, data TEXT NOT NULL,
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
"""


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        config.STATE_DB.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(config.STATE_DB, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
    return _conn


def now_iso() -> str:
    return datetime.now(config.TZ).isoformat(timespec="seconds")


# --- audit -----------------------------------------------------------------

def audit(event: str, actor: str, actor_type: str, subject: str | None, buyer: str | None, data: dict) -> dict:
    with _lock:
        c = db()
        last = c.execute("SELECT hash FROM audit_event ORDER BY seq DESC LIMIT 1").fetchone()
        prev = last["hash"] if last else "0" * 64
        rec = {"ts": now_iso(), "actor": actor, "actor_type": actor_type, "event": event,
               "subject": subject, "buyer": buyer, "data": data, "prev_hash": prev}
        h = hashlib.sha256(canonical(rec)).hexdigest()
        cur = c.execute(
            "INSERT INTO audit_event (ts, actor, actor_type, event, subject, buyer, data, prev_hash, hash) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (rec["ts"], actor, actor_type, event, subject, buyer, json.dumps(data, ensure_ascii=False), prev, h))
        c.commit()
        return {**rec, "seq": cur.lastrowid, "hash": h}


def _audit_row(r) -> dict:
    d = dict(r)
    d["data"] = json.loads(d["data"])
    return d


def audit_events(subject: str | None = None, buyer: str | None = None, limit: int = 500) -> list[dict]:
    q, args = "SELECT * FROM audit_event WHERE 1=1", []
    if subject:
        q += " AND (subject = ? OR data LIKE ?)"
        args += [subject, f'%"{subject}"%']
    if buyer:
        q += " AND buyer = ?"
        args.append(buyer)
    q += " ORDER BY seq DESC LIMIT ?"
    args.append(limit)
    with _lock:
        return [_audit_row(r) for r in db().execute(q, args).fetchall()]


def verify_chain() -> dict:
    prev = "0" * 64
    n = 0
    with _lock:
        rows = db().execute("SELECT * FROM audit_event ORDER BY seq").fetchall()
    for r in rows:
        d = _audit_row(r)
        rec = {k: d[k] for k in ("ts", "actor", "actor_type", "event", "subject", "buyer", "data", "prev_hash")}
        if d["prev_hash"] != prev or hashlib.sha256(canonical(rec)).hexdigest() != d["hash"]:
            return {"ok": False, "events": n, "broken_at_seq": d["seq"]}
        prev = d["hash"]
        n += 1
    return {"ok": True, "events": n, "head": prev}


# --- mandates, quotes, orders ------------------------------------------------

def put_mandate(m: dict, jws: str) -> None:
    with _lock:
        db().execute("INSERT OR REPLACE INTO mandate VALUES (?,?,?,?)",
                     (m["id"], json.dumps(m, ensure_ascii=False), jws, now_iso()))
        db().commit()


def get_mandate(mid: str) -> dict | None:
    with _lock:
        r = db().execute("SELECT data, jws FROM mandate WHERE id = ?", (mid,)).fetchone()
    return {**json.loads(r["data"]), "_jws": r["jws"]} if r else None


def list_mandates() -> list[dict]:
    with _lock:
        return [json.loads(r["data"]) for r in db().execute("SELECT data FROM mandate ORDER BY created_at")]


def put_quote(q: dict, jws: str) -> None:
    with _lock:
        db().execute("INSERT INTO quote (id, data, jws, created_at, expires_at) VALUES (?,?,?,?,?)",
                     (q["id"], json.dumps(q, ensure_ascii=False), jws, q["issued_at"], q["expires_at"]))
        db().commit()


def get_quote(qid: str) -> dict | None:
    with _lock:
        r = db().execute("SELECT * FROM quote WHERE id = ?", (qid,)).fetchone()
    return {**json.loads(r["data"]), "_jws": r["jws"], "_order_id": r["order_id"]} if r else None


def claim_quote(qid: str, order_id: str) -> bool:
    """Mark a quote used; False if another order already used it (no double orders)."""
    with _lock:
        cur = db().execute("UPDATE quote SET order_id = ? WHERE id = ? AND order_id IS NULL", (order_id, qid))
        db().commit()
        return cur.rowcount == 1


def put_order(o: dict) -> None:
    with _lock:
        db().execute(
            "INSERT OR REPLACE INTO purchase_order VALUES (?,?,?,?,?,?,?,?,?,?)",
            (o["id"], o["quote_id"], o["mandate_id"], o["agent_id"], o["status"], o["total_ex_vat"],
             json.dumps(o, ensure_ascii=False), o.get("idempotency_key"), o["created_at"], o["updated_at"]))
        db().commit()


def get_order(oid: str) -> dict | None:
    with _lock:
        r = db().execute("SELECT data FROM purchase_order WHERE id = ?", (oid,)).fetchone()
    return json.loads(r["data"]) if r else None


def order_by_idempotency_key(key: str) -> dict | None:
    with _lock:
        r = db().execute("SELECT data FROM purchase_order WHERE idempotency_key = ?", (key,)).fetchone()
    return json.loads(r["data"]) if r else None


def list_orders(status: str | None = None, mandate_id: str | None = None) -> list[dict]:
    q, args = "SELECT data FROM purchase_order WHERE 1=1", []
    if status:
        q += " AND status = ?"
        args.append(status)
    if mandate_id:
        q += " AND mandate_id = ?"
        args.append(mandate_id)
    with _lock:
        return [json.loads(r["data"]) for r in db().execute(q + " ORDER BY created_at DESC", args)]


def spent_today(mandate_id: str, day: str) -> float:
    with _lock:
        r = db().execute(
            "SELECT COALESCE(SUM(total_ex_vat), 0) FROM purchase_order "
            "WHERE mandate_id = ? AND status = 'ordered' AND substr(created_at, 1, 10) = ?",
            (mandate_id, day)).fetchone()
    return float(r[0])
