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

import contextlib
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

from . import clock, config
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
  idempotency_key TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
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
      data LONGTEXT NOT NULL, idempotency_key VARCHAR(191),
      created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL,
      INDEX (mandate_id), INDEX (status), UNIQUE KEY agent_idempotency (agent_id, idempotency_key)
    ) WITH SYSTEM VERSIONING""",
    """CREATE TABLE IF NOT EXISTS audit_event (
      seq BIGINT AUTO_INCREMENT PRIMARY KEY, ts VARCHAR(40) NOT NULL, actor VARCHAR(191) NOT NULL,
      actor_type VARCHAR(16) NOT NULL, event VARCHAR(64) NOT NULL, subject VARCHAR(64), buyer VARCHAR(191),
      data LONGTEXT NOT NULL, prev_hash CHAR(64) NOT NULL, hash CHAR(64) NOT NULL,
      INDEX (subject), INDEX (buyer)
    ) WITH SYSTEM VERSIONING""",
]


IDEM_INDEX = "agent_idempotency"  # idempotency keys are unique per agent: UNIQUE (agent_id, idempotency_key)


def _migrate_sqlite(conn) -> None:
    """Idempotency keys used to be globally UNIQUE (a column constraint SQLite can't drop): rebuild the
    table once without it. Then make sure the per-agent unique index exists. Safe to run on every start."""
    old = [r for r in conn.execute("PRAGMA index_list(purchase_order)").fetchall()
           if r["unique"] and r["origin"] == "u"
           and [c["name"] for c in conn.execute(f"PRAGMA index_info('{r['name']}')")] == ["idempotency_key"]]
    if old:
        cols = ",".join(ORDER_COLS)
        conn.executescript(f"""
          BEGIN;
          CREATE TABLE purchase_order_v2 (
            id TEXT PRIMARY KEY, quote_id TEXT NOT NULL, mandate_id TEXT NOT NULL, agent_id TEXT NOT NULL,
            status TEXT NOT NULL, total_ex_vat REAL NOT NULL, data TEXT NOT NULL,
            idempotency_key TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
          INSERT INTO purchase_order_v2 ({cols}) SELECT {cols} FROM purchase_order;
          DROP TABLE purchase_order;
          ALTER TABLE purchase_order_v2 RENAME TO purchase_order;
          COMMIT;""")
    conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {IDEM_INDEX} ON purchase_order (agent_id, idempotency_key)")


def _migrate_mariadb(c) -> None:
    """Existing databases: add the per-agent unique index, then drop the old global UNIQUE(idempotency_key)
    index (named after its column). Index changes only, no data rewritten; safe to run on every start."""
    c.execute("SELECT INDEX_NAME, COLUMN_NAME, NON_UNIQUE FROM information_schema.STATISTICS "
              "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'purchase_order'")
    idx: dict[str, list] = {}
    for r in c.fetchall():
        idx.setdefault(r["INDEX_NAME"], []).append((r["COLUMN_NAME"], int(r["NON_UNIQUE"])))
    old = [n for n, cols in idx.items() if n not in ("PRIMARY", IDEM_INDEX)
           and [col for col, _ in cols] == ["idempotency_key"] and cols[0][1] == 0]
    if IDEM_INDEX in idx and not old:
        return
    c.execute("SET SESSION system_versioning_alter_history = KEEP")  # ALTER on a SYSTEM VERSIONED table
    if IDEM_INDEX not in idx:
        c.execute(f"ALTER TABLE purchase_order ADD UNIQUE KEY {IDEM_INDEX} (agent_id, idempotency_key)")
    for n in old:
        c.execute(f"ALTER TABLE purchase_order DROP INDEX `{n}`")


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
                _migrate_mariadb(c)
        else:
            config.STATE_DB.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(config.STATE_DB, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(SQLITE_SCHEMA)
            _migrate_sqlite(_conn)
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


_tx_depth = 0


@contextlib.contextmanager
def transaction():
    """Run several statements as one database transaction (both backends): all commit or none do. Holds the store
    lock throughout, so other threads' statements can't interleave on the shared connection. Nested use joins the
    outer transaction. No DDL inside (MariaDB commits implicitly on CREATE TABLE)."""
    global _tx_depth
    with _lock:
        conn = db()
        if _tx_depth:
            _tx_depth += 1
            try:
                yield
            finally:
                _tx_depth -= 1
            return
        if config.DB_URL:
            conn.begin()
        else:
            conn.execute("BEGIN IMMEDIATE")
        _tx_depth = 1
        try:
            yield
        except BaseException:
            _tx_depth = 0
            conn.rollback() if config.DB_URL else conn.execute("ROLLBACK")
            raise
        _tx_depth = 0
        conn.commit() if config.DB_URL else conn.execute("COMMIT")


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
    data = {**data, "clock": clock.audit_tag()}  # the event's ts stays real time; this says which clock decided
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


def audit_events(subject: str | None = None, buyer: str | None = None, limit: int = 500,
                 where: tuple[str, list] | None = None) -> list[dict]:
    """`where`: an extra (" AND ...", args) condition, e.g. sandbox.where() for the judge-sandbox views."""
    q, args = "SELECT * FROM audit_event WHERE 1=1", []
    if where:
        q, args = q + where[0], args + list(where[1])
    if subject:
        q += " AND (subject = ? OR data LIKE ?)"
        args += [subject, f'%"{subject}"%']
    if buyer:
        q += " AND buyer = ?"
        args.append(buyer)
    q += " ORDER BY seq DESC LIMIT ?"
    args.append(limit)
    return [_audit_row(r) for r in _exec(q, args)[0]]


def verify_chain(head: str | None = None, checkpoint: str | None = None) -> dict:
    """Recompute the hash chain. `head`: a head hash the caller saved earlier. A consistent chain alone can't show
    that its newest events were deleted (what's left is a shorter valid chain), so a saved head must still be one
    of the chain's hashes, else the chain fails."""
    prev = "0" * 64
    n = 0
    result: dict = {"backend": backend()}
    head_seq = 0 if head == prev else None  # the empty chain's head is the genesis hash
    hashes: dict[int, str] = {}  # seq -> hash, for the signed checkpoints (sous/checkpoints.py)
    for r in _exec("SELECT * FROM audit_event ORDER BY seq")[0]:
        d = _audit_row(r)
        rec = {k: d[k] for k in ("ts", "actor", "actor_type", "event", "subject", "buyer", "data", "prev_hash")}
        if d["prev_hash"] != prev or hashlib.sha256(canonical(rec)).hexdigest() != d["hash"]:
            result.update({"ok": False, "events": n, "broken_at_seq": d["seq"]})
            break
        prev = d["hash"]
        hashes[int(d["seq"])] = d["hash"]
        n += 1
        if head and d["hash"] == head:
            head_seq = d["seq"]
    else:
        result.update({"ok": True, "events": n, "head": prev})
    if head:
        result["saved_head"] = {"found": head_seq is not None, "seq": head_seq}
        result["ok"] = result["ok"] and head_seq is not None
    from . import checkpoints  # signed checkpoints: a cut tail below any of them fails
    result["checkpoints"] = checkpoints.check(hashes, checkpoint)
    result["ok"] = result["ok"] and result["checkpoints"]["ok"]
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


def order_by_idempotency_key(agent_id: str, key: str) -> dict | None:
    """Idempotency keys belong to the agent that sent them: another agent's identical key is unrelated."""
    r = _exec("SELECT data FROM purchase_order WHERE agent_id = ? AND idempotency_key = ?", (agent_id, key))[0]
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


def ordered_at(o: dict) -> str:
    """When the order went through: the human's approval time for an approved order, else when it was placed.
    Period totals, split detection and last-paid prices all count an order from this moment, so an order held on
    Sunday and approved on Monday is Monday's spend."""
    a = o.get("approval") or {}
    return a["at"] if o.get("status") == "ordered" and a.get("approved") and a.get("at") else o["created_at"]


def ordered_orders(mandate_id: str) -> list[dict]:
    """The mandate's orders that went through, newest ordered_at first."""
    return sorted(list_orders(status="ordered", mandate_id=mandate_id),
                  key=lambda o: datetime.fromisoformat(ordered_at(o)), reverse=True)


def _cents_total(orders) -> float:
    return sum(round((o.get("total_ex_vat") or 0) * 100) for o in orders) / 100


def spent_today(mandate_id: str, day: str) -> float:
    """Ordered total of a Helsinki calendar day (YYYY-MM-DD), by ordered_at, summed in cents."""
    return _cents_total(o for o in list_orders(status="ordered", mandate_id=mandate_id)
                        if datetime.fromisoformat(ordered_at(o)).astimezone(config.TZ).date().isoformat() == day)


def spent_since(mandate_id: str, since_iso: str) -> float:
    """Ordered total under the mandate since a timestamp, by ordered_at, summed in cents."""
    since = datetime.fromisoformat(since_iso)
    return _cents_total(o for o in list_orders(status="ordered", mandate_id=mandate_id)
                        if datetime.fromisoformat(ordered_at(o)) >= since)


def ordered_since(mandate_id: str, since_iso: str, *, auto_only: bool = True) -> list[dict]:
    """[{id, total_ex_vat}] of orders that went through since a timestamp. auto_only: only those the agent ordered
    alone (no human approval), which is what spend.split_orders sums: an approved order already had a human."""
    since = datetime.fromisoformat(since_iso)
    return [{"id": o["id"], "total_ex_vat": float(o["total_ex_vat"])} for o in reversed(ordered_orders(mandate_id))
            if datetime.fromisoformat(ordered_at(o)) >= since and not (auto_only and o.get("approval"))]


def last_unit_prices(mandate_id: str) -> dict[str, dict]:
    """{product_id: {unit_price_ex_vat, order_id, at}}: the price in the order that most recently went through."""
    out: dict[str, dict] = {}
    for o in ordered_orders(mandate_id):
        for l in o.get("lines") or []:
            if l.get("unit_price_ex_vat") is not None and l["product_id"] not in out:
                out[l["product_id"]] = {"unit_price_ex_vat": l["unit_price_ex_vat"], "order_id": o["id"],
                                        "at": ordered_at(o)}
    return out


def ordered_product_ids(mandate_id: str) -> set[str]:
    """Every product id in an order that went through under the mandate."""
    return {l["product_id"] for o in list_orders(status="ordered", mandate_id=mandate_id) for l in o.get("lines") or []}


def _iso(dt) -> str | None:
    return dt.replace(tzinfo=timezone.utc).astimezone(config.TZ).isoformat(timespec="milliseconds") if dt else None


def _iso_end(dt) -> str | None:
    return None if dt is None or dt.year >= 2038 else _iso(dt)
