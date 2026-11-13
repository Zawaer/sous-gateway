"""Persistence for mandates, quotes, orders and the audit log: MariaDB or SQLite.

With SOUS_DB_URL=mysql://user:pass@host:port/db the gateway uses MariaDB and
makes `mandate`, `purchase_order` and `audit_event` SYSTEM VERSIONED tables: the
database itself keeps every past version of every row. That gives three things the
challenge asks for, in plain SQL:

- "What did the mandate say when this order was decided?"
    SELECT ... FROM mandate FOR SYSTEM_TIME AS OF '<decision time>' WHERE id = ?
- The full history of an order (created -> held -> approved), row by row.
- A second, independent tamper check on the audit log: it is append-only, so any
  UPDATE or DELETE shows up as a closed historical row, on top of the hash chain.

Without a DB URL (development, tests) it falls back to a local SQLite file with the
same schema minus versioning; history then comes from the audit log.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

from . import config
from .signing import canonical

_lock = threading.RLock()
_conn = None


class Conflict(Exception):
    """A unique key (order id, idempotency key) is already taken."""

SQLITE_SCHEMA = """
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

MARIADB_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS mandate (
      id VARCHAR(64) PRIMARY KEY, data LONGTEXT NOT NULL, jws LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL
    ) WITH SYSTEM VERSIONING""",
    """CREATE TABLE IF NOT EXISTS quote (
      id VARCHAR(32) PRIMARY KEY, data LONGTEXT NOT NULL, jws LONGTEXT NOT NULL,
      created_at VARCHAR(40) NOT NULL, expires_at VARCHAR(40) NOT NULL, order_id VARCHAR(32))""",
    """CREATE TABLE IF NOT EXISTS purchase_order (
      id VARCHAR(32) PRIMARY KEY, quote_id VARCHAR(32) NOT NULL, mandate_id VARCHAR(64) NOT NULL,
      agent_id VARCHAR(128) NOT NULL, status VARCHAR(32) NOT NULL, total_ex_vat DOUBLE NOT NULL,
      data LONGTEXT NOT NULL, idempotency_key VARCHAR(191) UNIQUE,
      created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL,
      INDEX (mandate_id), INDEX (status)
    ) WITH SYSTEM VERSIONING""",
    """CREATE TABLE IF NOT EXISTS audit_event (
      seq BIGINT AUTO_INCREMENT PRIMARY KEY, ts VARCHAR(40) NOT NULL, actor VARCHAR(191) NOT NULL,
      actor_type VARCHAR(16) NOT NULL, event VARCHAR(64) NOT NULL, subject VARCHAR(64), buyer VARCHAR(191),
      data LONGTEXT NOT NULL, prev_hash CHAR(64) NOT NULL, hash CHAR(64) NOT NULL,
      INDEX (subject), INDEX (buyer)
    ) WITH SYSTEM VERSIONING""",
]


def backend() -> str:
    return "mariadb" if config.DB_URL else "sqlite"


def db():
    global _conn
    if _conn is None:
        if config.DB_URL:
            import pymysql

            u = urlparse(config.DB_URL)
            _conn = pymysql.connect(
                host=u.hostname, port=u.port or 3306, user=unquote(u.username or ""),
                password=unquote(u.password or ""), database=u.path.lstrip("/"), charset="utf8mb4",
                autocommit=True, cursorclass=pymysql.cursors.DictCursor,
                # re-applied by pymysql on every (re)connect, so ping(reconnect=True) keeps UTC
                init_command="SET time_zone = '+00:00'")
            with _conn.cursor() as c:
                for stmt in MARIADB_SCHEMA:
                    c.execute(stmt)
        else:
            config.STATE_DB.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(config.STATE_DB, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(SQLITE_SCHEMA)
    elif config.DB_URL:
        _conn.ping(reconnect=True)
    return _conn


def _exec(sql: str, args: tuple | list = ()) -> tuple[list[dict], int, int | None]:
    """Run one statement on either backend: (rows, rowcount, lastrowid)."""
    with _lock:
        conn = db()
        if config.DB_URL:
            with conn.cursor() as c:
                c.execute(sql.replace("?", "%s"), args)
                return list(c.fetchall()), c.rowcount, c.lastrowid
        cur = conn.execute(sql, args)
        return [dict(r) for r in cur.fetchall()], cur.rowcount, cur.lastrowid


def _upsert(table: str, cols: list[str], values: tuple) -> None:
    ph = ",".join("?" * len(cols))
    if config.DB_URL:
        # UPDATE in place (not REPLACE) so system versioning records a new version, not a delete
        updates = ",".join(f"{c}=VALUES({c})" for c in cols[1:])
        _exec(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph}) ON DUPLICATE KEY UPDATE {updates}", values)
    else:
        _exec(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({ph})", values)


def now_iso() -> str:
    return datetime.now(config.TZ).isoformat(timespec="milliseconds")


def _utc(ts: str) -> str:
    """ISO timestamp -> 'YYYY-MM-DD HH:MM:SS.ffffff' in UTC, the form FOR SYSTEM_TIME AS OF expects."""
    return datetime.fromisoformat(ts).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


# --- audit -----------------------------------------------------------------

def audit(event: str, actor: str, actor_type: str, subject: str | None, buyer: str | None, data: dict) -> dict:
    with _lock:
        last, _, _ = _exec("SELECT hash FROM audit_event ORDER BY seq DESC LIMIT 1")
        prev = last[0]["hash"] if last else "0" * 64
        rec = {"ts": now_iso(), "actor": actor, "actor_type": actor_type, "event": event,
               "subject": subject, "buyer": buyer, "data": data, "prev_hash": prev}
        h = hashlib.sha256(canonical(rec)).hexdigest()
        _, _, seq = _exec(
            "INSERT INTO audit_event (ts, actor, actor_type, event, subject, buyer, data, prev_hash, hash) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (rec["ts"], actor, actor_type, event, subject, buyer, json.dumps(data, ensure_ascii=False), prev, h))
        return {**rec, "seq": seq, "hash": h}


def _audit_row(r: dict) -> dict:
    d = {k: r[k] for k in ("seq", "ts", "actor", "actor_type", "event", "subject", "buyer", "prev_hash", "hash")}
    d["data"] = json.loads(r["data"])
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
    return [_audit_row(r) for r in _exec(q, args)[0]]


def verify_chain() -> dict:
    prev = "0" * 64
    n = 0
    result: dict = {"backend": backend()}
    for r in _exec("SELECT * FROM audit_event ORDER BY seq")[0]:
        d = _audit_row(r)
        rec = {k: d[k] for k in ("ts", "actor", "actor_type", "event", "subject", "buyer", "data", "prev_hash")}
        if d["prev_hash"] != prev or hashlib.sha256(canonical(rec)).hexdigest() != d["hash"]:
            result.update({"ok": False, "events": n, "broken_at_seq": d["seq"]})
            break
        prev = d["hash"]
        n += 1
    else:
        result.update({"ok": True, "events": n, "head": prev})
    if config.DB_URL:
        # Independent check from MariaDB's own history: an append-only table has no closed rows.
        rows, _, _ = _exec("SELECT seq, ROW_END FROM audit_event FOR SYSTEM_TIME ALL "
                           "WHERE ROW_END < TIMESTAMP'2038-01-19 03:14:07' ORDER BY seq")
        result["system_versioning"] = {"rows_changed_or_deleted": len(rows),
                                       "seqs": sorted({r["seq"] for r in rows})}
        result["ok"] = result["ok"] and not rows
    return result


# --- mandates ---------------------------------------------------------------

def put_mandate(m: dict, jws: str) -> None:
    existing = _exec("SELECT created_at FROM mandate WHERE id = ?", (m["id"],))[0]
    created = existing[0]["created_at"] if existing else now_iso()
    _upsert("mandate", ["id", "data", "jws", "created_at"], (m["id"], json.dumps(m, ensure_ascii=False), jws, created))


def get_mandate(mid: str) -> dict | None:
    r = _exec("SELECT data, jws FROM mandate WHERE id = ?", (mid,))[0]
    return {**json.loads(r[0]["data"]), "_jws": r[0]["jws"]} if r else None


def mandate_as_of(mid: str, ts: str) -> dict | None:
    """The mandate exactly as it was at `ts` (MariaDB system versioning)."""
    if not config.DB_URL:
        return None
    r = _exec("SELECT data FROM mandate FOR SYSTEM_TIME AS OF TIMESTAMP ? WHERE id = ?", (_utc(ts), mid))[0]
    return json.loads(r[0]["data"]) if r else None


def mandate_versions(mid: str) -> list[dict]:
    if not config.DB_URL:
        m = get_mandate(mid)
        return [{"valid_from": None, "valid_to": None, "mandate": {k: v for k, v in m.items() if k != "_jws"}}] if m else []
    rows = _exec("SELECT data, ROW_START, ROW_END FROM mandate FOR SYSTEM_TIME ALL WHERE id = ? ORDER BY ROW_START",
                 (mid,))[0]
    return [{"valid_from": _iso(r["ROW_START"]), "valid_to": _iso_end(r["ROW_END"]), "mandate": json.loads(r["data"])}
            for r in rows]


def list_mandates() -> list[dict]:
    return [json.loads(r["data"]) for r in _exec("SELECT data FROM mandate ORDER BY created_at")[0]]


# --- quotes -----------------------------------------------------------------

def put_quote(q: dict, jws: str) -> None:
    _exec("INSERT INTO quote (id, data, jws, created_at, expires_at) VALUES (?,?,?,?,?)",
          (q["id"], json.dumps(q, ensure_ascii=False), jws, q["issued_at"], q["expires_at"]))


def get_quote(qid: str) -> dict | None:
    r = _exec("SELECT * FROM quote WHERE id = ?", (qid,))[0]
    return {**json.loads(r[0]["data"]), "_jws": r[0]["jws"], "_order_id": r[0]["order_id"]} if r else None


def claim_quote(qid: str, order_id: str) -> bool:
    """Mark a quote used; False if another order already used it (no double orders)."""
    _, n, _ = _exec("UPDATE quote SET order_id = ? WHERE id = ? AND order_id IS NULL", (order_id, qid))
    return n == 1


def release_quote(qid: str, order_id: str) -> None:
    """Undo claim_quote when the order could not be stored."""
    _exec("UPDATE quote SET order_id = NULL WHERE id = ? AND order_id = ?", (qid, order_id))


# --- orders -----------------------------------------------------------------

ORDER_COLS = ["id", "quote_id", "mandate_id", "agent_id", "status", "total_ex_vat", "data",
              "idempotency_key", "created_at", "updated_at"]


def _order_row(o: dict) -> tuple:
    return (o["id"], o["quote_id"], o["mandate_id"], o["agent_id"], o["status"], o["total_ex_vat"],
            json.dumps(o, ensure_ascii=False), o.get("idempotency_key"), o["created_at"], o["updated_at"])


def insert_order(o: dict) -> None:
    """Store a NEW order. A clash on the id or idempotency key raises Conflict instead of overwriting."""
    try:
        _exec(f"INSERT INTO purchase_order ({','.join(ORDER_COLS)}) VALUES ({','.join('?' * len(ORDER_COLS))})",
              _order_row(o))
    except Exception as e:  # sqlite3.IntegrityError / pymysql.err.IntegrityError
        if type(e).__name__ == "IntegrityError":
            raise Conflict(str(e)) from e
        raise


def update_order(o: dict, expected_status: str | None = None) -> bool:
    """Write an order's new state; with `expected_status`, only if it still has that status (False otherwise)."""
    sql = "UPDATE purchase_order SET status = ?, total_ex_vat = ?, data = ?, updated_at = ? WHERE id = ?"
    args = [o["status"], o["total_ex_vat"], json.dumps(o, ensure_ascii=False), o["updated_at"], o["id"]]
    if expected_status is not None:
        sql += " AND status = ?"
        args.append(expected_status)
    _, n, _ = _exec(sql, args)
    return n == 1


def put_order(o: dict) -> None:
    """Insert or update an order by id (UPDATE in place on MariaDB, so versioning keeps history)."""
    _upsert("purchase_order", ORDER_COLS, _order_row(o))


def get_order(oid: str) -> dict | None:
    r = _exec("SELECT data FROM purchase_order WHERE id = ?", (oid,))[0]
    return json.loads(r[0]["data"]) if r else None


def order_versions(oid: str) -> list[dict]:
    """Every stored version of an order, oldest first (MariaDB); SQLite has only the current one."""
    if not config.DB_URL:
        o = get_order(oid)
        return [{"valid_from": None, "valid_to": None, "status": o["status"], "order": o}] if o else []
    rows = _exec("SELECT data, ROW_START, ROW_END FROM purchase_order FOR SYSTEM_TIME ALL WHERE id = ? "
                 "ORDER BY ROW_START", (oid,))[0]
    out = []
    for r in rows:
        o = json.loads(r["data"])
        out.append({"valid_from": _iso(r["ROW_START"]), "valid_to": _iso_end(r["ROW_END"]),
                    "status": o["status"], "order": o})
    return out


def order_by_idempotency_key(key: str) -> dict | None:
    r = _exec("SELECT data FROM purchase_order WHERE idempotency_key = ?", (key,))[0]
    return json.loads(r[0]["data"]) if r else None


def list_orders(status: str | None = None, mandate_id: str | None = None) -> list[dict]:
    q, args = "SELECT data FROM purchase_order WHERE 1=1", []
    if status:
        q += " AND status = ?"
        args.append(status)
    if mandate_id:
        q += " AND mandate_id = ?"
        args.append(mandate_id)
    return [json.loads(r["data"]) for r in _exec(q + " ORDER BY created_at DESC", args)[0]]


def spent_today(mandate_id: str, day: str) -> float:
    r = _exec("SELECT COALESCE(SUM(total_ex_vat), 0) AS s FROM purchase_order "
              "WHERE mandate_id = ? AND status = 'ordered' AND substr(created_at, 1, 10) = ?", (mandate_id, day))[0]
    return float(r[0]["s"])


def _iso(dt) -> str | None:
    return dt.replace(tzinfo=timezone.utc).astimezone(config.TZ).isoformat(timespec="milliseconds") if dt else None


def _iso_end(dt) -> str | None:
    return None if dt is None or dt.year >= 2038 else _iso(dt)
