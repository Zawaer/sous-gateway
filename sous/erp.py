"""Outbound order adapter: an order that went through is handed to Valio Aimo's order system (ERP).

When an order becomes `ordered` (silent/notify at placement, or approved by a human) it is mapped to an SAP IDoc
ORDERS05-shaped JSON document and sent to SOUS_ERP_URL. Default: an in-process stub that answers like an ERP
would (a sales order number), so the whole flow runs without any Valio system. Nothing here talks to a real Valio
system; the mapping is our best reading of a standard ORDERS05 inbound sales order and is to be checked with
Valio's integration team (docs/INTEGRATION.md has the mapping table and what we don't know).

Never loses an order: the order is written to the `erp_outbox` table first (one row per order id: idempotent),
then sent. A failed send stays in the outbox and is retried with backoff by the gateway's background sweep
(api._sweep_overdue -> process_due). The same order id always produces the same payload, carried as the
customer's purchase order number (E1EDK02 qualifier 001, BELNR) and as the Idempotency-Key header, so the ERP
can drop a duplicate.

Durability: the outbox row is written in the same database transaction as the order (service._place_order and
_decide; standing renewals and attack demos go through _place_order), so an order is never stored as `ordered`
without its row. As a second net, every sweep runs reconcile(): any ordered order without a row is queued
(order.erp_reconciled). Orders already `ordered` when the adapter was first activated (erp_meta.activated_at) get
a `legacy` row and are never sent.

State: erp_outbox row -> `erp` on the order (GET /orders/{id}): {status: pending|sent|confirmed|failed,
confirmation_id, payload_hash, endpoint, attempts, last_error, next_retry_at, sent_at, confirmed_at}. It is
unsigned operational metadata: signed receipts are never re-issued for it. Audit events: order.erp_sent,
order.erp_confirmed, order.erp_failed (each attempt), order.erp_gave_up (after MAX_ATTEMPTS).

Post-order amendments (sous/amendments.py) that change the lines add one ORDCHG change message each, keyed
'<order id>/<amendment id>' in the same outbox (same retries, Idempotency-Key = that key); the original row and
its payload are never rewritten.

Sandbox orders always go to the stub, whatever SOUS_ERP_URL says: a visitor's order never reaches a real ERP.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime

import httpx

from . import config, store
from .signing import canonical

ERP_URL = config.env("ERP_URL", "").strip()  # "" or "stub": in-process stub; "stub:fail": a stub that always fails
ERP_TIMEOUT = float(config.env("ERP_TIMEOUT", "5"))
SENDER = config.env("ERP_SENDER", "SOUS")  # logical system of the gateway (EDI_DC40 SNDPRN)
RECEIVER = config.env("ERP_RECEIVER", "VALIOAIMO")  # Valio's ERP logical system (EDI_DC40 RCVPRN); a placeholder
MAX_ATTEMPTS = 10
BACKOFF = [30, 60, 120, 300, 600, 1800, 3600]  # seconds before attempt 2, 3, ...; then hourly

_SQLITE = """CREATE TABLE IF NOT EXISTS erp_outbox (
  order_id TEXT PRIMARY KEY, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_ts REAL NOT NULL,
  payload TEXT NOT NULL, payload_hash TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL)"""
_MARIADB = """CREATE TABLE IF NOT EXISTS erp_outbox (
  order_id VARCHAR(32) PRIMARY KEY, status VARCHAR(16) NOT NULL, attempts INT NOT NULL DEFAULT 0,
  next_ts DOUBLE NOT NULL, payload LONGTEXT NOT NULL, payload_hash CHAR(64) NOT NULL, data LONGTEXT NOT NULL,
  created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL, INDEX (status, next_ts)
) WITH SYSTEM VERSIONING"""
_ready_for = None
_send_lock = threading.Lock()  # one send per order at a time (a retry and the sweep can't double-send)
_clock = time.time  # patched in tests


_META_SQLITE = "CREATE TABLE IF NOT EXISTS erp_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
_META_MARIADB = "CREATE TABLE IF NOT EXISTS erp_meta (k VARCHAR(64) PRIMARY KEY, v TEXT NOT NULL)"


def _ensure() -> None:
    """Create the tables, then activate once: the activation time is stored in erp_meta, and every order already
    `ordered` at that moment is marked `legacy` in the outbox (never sent: it was ordered before the adapter).
    From then on every ordered order must have an outbox row; reconcile() queues any that doesn't."""
    global _ready_for
    with store._lock:
        conn = store.db()
        if _ready_for is conn:
            return
        store._exec(_MARIADB if config.DB_URL else _SQLITE)
        store._exec(_META_MARIADB if config.DB_URL else _META_SQLITE)
        if not store._exec("SELECT v FROM erp_meta WHERE k = 'activated_at'")[0]:
            now = store.now_iso()
            with store.transaction():
                store._exec("INSERT INTO erp_meta (k, v) VALUES ('activated_at', ?)", (now,))
                store._exec("INSERT INTO erp_outbox (order_id, status, attempts, next_ts, payload, payload_hash, data, "
                            "created_at, updated_at) SELECT id, 'legacy', 0, 0, '{}', '', '{}', ?, ? "
                            "FROM purchase_order WHERE status = 'ordered' "
                            "AND id NOT IN (SELECT order_id FROM erp_outbox)", (now, now))
        _ready_for = conn


def activated_at() -> str | None:
    _ensure()
    r = store._exec("SELECT v FROM erp_meta WHERE k = 'activated_at'")[0]
    return r[0]["v"] if r else None


# --- mapping: Sous order -> ORDERS05-shaped JSON -------------------------------------------------------------

def _d(iso: str | None) -> str | None:
    """ISO date/time -> SAP DATUM (YYYYMMDD)."""
    return iso[:10].replace("-", "") if iso else None


def _qty(x) -> str:
    return f"{float(x):g}" if x is not None else "0"


def to_orders05(order: dict, customer_number: str | None) -> dict:
    """The order as an ORDERS05 IDoc in JSON (segment and field names as in SAP; values as strings like an IDoc).
    Only orderable lines with a quantity are sent. Prices are the list prices Sous quoted, for reference: the
    ERP prices the order with the customer's own conditions."""
    ordered_at = store.ordered_at(order)
    lines = [l for l in order.get("lines") or [] if l.get("quantity") and l.get("unit_price_ex_vat") is not None]
    partner = customer_number or ""
    return {
        "EDI_DC40": {"IDOCTYP": "ORDERS05", "MESTYP": "ORDERS", "DIRECT": "2", "SNDPRT": "LS", "SNDPRN": SENDER,
                     "RCVPRT": "LS", "RCVPRN": RECEIVER, "REFINT": order["id"]},
        "E1EDK01": {"ACTION": "000", "CURCY": "EUR", "BELNR": order["id"], "RECIPNT_NO": partner},
        "E1EDK14": [{"QUALF": "012", "ORGID": "ZAGT"}],  # sales document type: placeholder, Valio's to define
        "E1EDK03": [{"IDDAT": "002", "DATUM": _d(order.get("requested_delivery_date"))},  # requested delivery
                    {"IDDAT": "022", "DATUM": _d(ordered_at)}],  # purchase order date
        "E1EDKA1": [{"PARVW": "AG", "PARTN": partner, "NAME1": order.get("buyer")},  # sold-to
                    {"PARVW": "WE", "PARTN": partner, "NAME1": order.get("buyer")}],  # ship-to
        "E1EDK02": [{"QUALF": "001", "BELNR": order["id"], "DATUM": _d(ordered_at)}],  # customer PO = Sous order id
        "E1EDKT1": [{"TDID": "Z001", "TSSPRAS_ISO": "EN",
                     "E1EDKT2": [{"TDLINE": f"Sous order {order['id']}, mandate {order['mandate_id']}, "
                                            f"quote {order['quote_id']}"[:70]}]}],
        "E1EDP01": [{
            "POSEX": f"{(i + 1) * 10:06d}", "MENGE": _qty(l["quantity"]), "MENEE": l.get("sales_unit") or "",
            "VPREI": f"{l['unit_price_ex_vat']:.2f}", "PEINH": "1", "NETWR": f"{(l.get('line_total_ex_vat') or 0):.2f}",
            "E1EDP19": [{"QUALF": "002", "IDTNR": str(l["product_id"]), "KTEXT": (l.get("name") or "")[:40]}],
            "E1EDP20": [{"WMENG": _qty(l["quantity"]),
                         "EDATU": _d(l.get("delivery_date") or order.get("delivery_date"))}],
        } for i, l in enumerate(lines)],
        "E1EDS01": [{"SUMID": "002", "SUMME": f"{order.get('total_ex_vat') or 0:.2f}", "WAERQ": "EUR"}],
    }


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(canonical(payload)).hexdigest()


# --- transport -------------------------------------------------------------------------------------------------

class SendError(Exception):
    pass


def _stub(payload: dict, fail: bool) -> dict:
    """What an ERP answers: a sales order number (VBELN-like, 10 digits), deterministic per order id, so a
    resend gets the same number (idempotent)."""
    if fail:
        raise SendError("stub ERP configured to fail (SOUS_ERP_URL=stub:fail)")
    oid = payload["E1EDK01"]["BELNR"]
    return {"status": "confirmed",
            "confirmation_id": f"{int(hashlib.sha256(oid.encode()).hexdigest()[:12], 16) % 10**8 + 10**9:010d}"}


def _http(url: str, payload: dict, order_id: str) -> dict:
    try:
        r = httpx.post(url, json=payload, timeout=ERP_TIMEOUT,
                       headers={"Idempotency-Key": order_id, "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        raise SendError(f"{type(e).__name__}: {e}"[:300]) from None
    if r.status_code >= 300:
        raise SendError(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        body = r.json()
    except ValueError:
        body = {}
    cid = body.get("confirmation_id") or body.get("sales_order") if isinstance(body, dict) else None
    return {"status": "confirmed" if cid else "sent", "confirmation_id": cid}


def endpoint_for(order: dict) -> str:
    """'stub', 'stub:fail' or the URL. Sandbox orders always go to the stub."""
    if order.get("sandbox") or _is_sandbox(order):
        return "stub"
    return ERP_URL or "stub"


def _is_sandbox(order: dict) -> bool:
    m = store.get_mandate(order["mandate_id"]) or {}
    return bool(m.get("sandbox"))


def _send(endpoint: str, payload: dict, order_id: str) -> dict:
    if endpoint in ("stub", "stub:fail"):
        return _stub(payload, endpoint == "stub:fail")
    return _http(endpoint, payload, order_id)


# --- outbox ----------------------------------------------------------------------------------------------------

def _get(order_id: str) -> dict | None:
    _ensure()
    r = store._exec("SELECT * FROM erp_outbox WHERE order_id = ?", (order_id,))[0]
    if not r:
        return None
    row = dict(r[0])
    row["data"] = json.loads(row["data"])
    return row


def _save(row: dict) -> None:
    store._exec("UPDATE erp_outbox SET status = ?, attempts = ?, next_ts = ?, data = ?, updated_at = ? "
                "WHERE order_id = ?", (row["status"], row["attempts"], row["next_ts"], json.dumps(row["data"]),
                                       store.now_iso(), row["order_id"]))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, config.TZ).isoformat(timespec="seconds")


def prepare(order: dict, mandate=None) -> tuple | None:
    """The outbox row for an ordered order, built BEFORE the transaction that stores the order (it reads the
    customer account, which may seed a mock row: no writes of its own inside that transaction). None if the
    order isn't ordered."""
    if order.get("status") != "ordered":
        return None
    _ensure()
    from . import account, service as S
    try:
        acct = account.for_mandate(mandate or S._mandate(order["mandate_id"]))
    except S.GatewayError:
        acct = None
    num = acct.customer_number if acct else None
    payload = to_orders05(order, num)
    endpoint = endpoint_for(order)
    data = {"endpoint": "stub" if endpoint.startswith("stub") else "http", "customer_number": num,
            "confirmation_id": None, "last_error": None, "sent_at": None, "confirmed_at": None,
            "buyer": order.get("buyer"), "target": endpoint}
    now = store.now_iso()
    return (order["id"], "pending", 0, _clock(), json.dumps(payload, ensure_ascii=False), payload_hash(payload),
            json.dumps(data), now, now)


def write(row: tuple | None) -> None:
    """Insert a prepared outbox row; called inside the order's own transaction (service._place_order, _decide)."""
    if row is not None:
        store._exec("INSERT INTO erp_outbox (order_id, status, attempts, next_ts, payload, payload_hash, data, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)", row)


def enqueue(order: dict) -> dict | None:
    """Put an ordered order in the outbox if it isn't there yet (idempotent by order id). Returns the outbox row."""
    if order.get("status") != "ordered":
        return None
    _ensure()
    if (row := _get(order["id"])) is not None:
        return row
    try:
        write(prepare(order))
    except Exception as e:  # another thread enqueued it first
        if type(e).__name__ != "IntegrityError":
            raise
    return _get(order["id"])


def reconcile(limit: int = 200) -> int:
    """Queue every ordered order that has no outbox row (a crash or failure between storing an order and queueing
    it). Orders from before activation have `legacy` rows, so they are never picked up. Returns how many."""
    _ensure()
    rows = store._exec("SELECT id FROM purchase_order WHERE status = 'ordered' "
                       "AND id NOT IN (SELECT order_id FROM erp_outbox) LIMIT ?", (limit,))[0]
    n = 0
    for r in rows:
        o = store.get_order(r["id"])
        if o and enqueue(o) is not None:
            n += 1
            store.audit("order.erp_reconciled", "erp-adapter", "system", o["id"], o.get("buyer"),
                        {"order_id": o["id"], "reason": "ordered but not in the ERP outbox: queued by the sweep"})
    return n


# --- post-order amendments (sous/amendments.py): one ORDCHG change message per amendment ----------------------

def change_key(order_id: str, amendment_id: str) -> str:
    """Outbox key of an amendment's change message: '<order id>/<amendment id>' (also its Idempotency-Key)."""
    return f"{order_id}/{amendment_id}"


def to_ordchg(order: dict, amendment: dict, customer_number: str | None) -> dict:
    """The amended order as an ORDERS05 change (MESTYP ORDCHG, E1EDK01 ACTION 002), every line at its original
    position: changed lines ACTION 002, cancelled ones 003 with quantity 0. Same BELNR as the original order."""
    doc = to_orders05({**order, "lines": []}, customer_number)
    doc["EDI_DC40"] |= {"MESTYP": "ORDCHG", "REFINT": change_key(order["id"], amendment["id"])}
    doc["E1EDK01"]["ACTION"] = "002"
    doc["E1EDKT1"][0]["E1EDKT2"].append({"TDLINE": f"Amendment {amendment['id']} {amendment['kind']} "
                                                   f"{amendment['status']}"[:70]})
    items = []
    for i, l in enumerate(order.get("lines") or []):
        if l.get("unit_price_ex_vat") is None:
            continue
        action = "003" if l.get("cancelled") else "002" if l.get("amended_by") else "000"
        items.append({"POSEX": f"{(i + 1) * 10:06d}", "ACTION": action, "MENGE": _qty(l.get("quantity")),
                      "MENEE": l.get("sales_unit") or "", "VPREI": f"{l['unit_price_ex_vat']:.2f}", "PEINH": "1",
                      "NETWR": f"{(l.get('line_total_ex_vat') or 0):.2f}",
                      "E1EDP19": [{"QUALF": "002", "IDTNR": str(l["product_id"]),
                                   "KTEXT": (l.get("name") or "")[:40]}],
                      "E1EDP20": [{"WMENG": _qty(l.get("quantity")),
                                   "EDATU": _d(l.get("delivery_date") or order.get("delivery_date"))}]})
    doc["E1EDP01"] = items
    return doc


def enqueue_change(order: dict, amendment: dict) -> dict | None:
    """Queue the change message for an amendment that changed the lines, once per amendment (idempotent), only
    when the order itself went through the outbox (an order the ERP never got has nothing to change)."""
    _ensure()
    base = _get(order["id"])
    if base is None or base["status"] == "legacy":
        return None
    key = change_key(order["id"], amendment["id"])
    if (row := _get(key)) is not None:
        return row
    payload = to_ordchg(order, amendment, base["data"].get("customer_number"))
    data = {**{k: base["data"].get(k) for k in ("endpoint", "customer_number", "buyer", "target")},
            "confirmation_id": None, "last_error": None, "sent_at": None, "confirmed_at": None,
            "change_of": order["id"], "amendment_id": amendment["id"]}
    try:
        store._exec("INSERT INTO erp_outbox (order_id, status, attempts, next_ts, payload, payload_hash, data, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (key, "pending", 0, _clock(), json.dumps(payload, ensure_ascii=False), payload_hash(payload),
                     json.dumps(data), store.now_iso(), store.now_iso()))
    except Exception as e:  # another thread enqueued it first
        if type(e).__name__ != "IntegrityError":
            raise
    return _get(key)


def submit_change(order: dict, amendment: dict) -> dict | None:
    """Queue and try to send an amendment's change message now; a failure stays queued for the sweep."""
    try:
        if enqueue_change(order, amendment) is not None:
            return attempt(change_key(order["id"], amendment["id"]))
    except Exception as e:  # noqa: BLE001
        store.audit("order.erp_failed", "erp-adapter", "system", order["id"], order.get("buyer"),
                    {"order_id": order["id"], "amendment_id": amendment["id"], "stage": "enqueue_change",
                     "error": f"{type(e).__name__}: {e}"[:300]})
    return None


def attempt(order_id: str) -> dict | None:
    """Send one queued order now (if it's due or never tried). Idempotent; failures stay queued."""
    with _send_lock:
        row = _get(order_id)
        if row is None or row["status"] in ("sent", "confirmed", "legacy") or row["data"].get("gave_up"):
            return row
        payload = json.loads(row["payload"])
        d, buyer, now = row["data"], row["data"].get("buyer"), _clock()
        row["attempts"] += 1
        ev = {"order_id": order_id, "payload_hash": row["payload_hash"], "endpoint": d["endpoint"],
              "attempt": row["attempts"], "customer_number": d.get("customer_number")}
        try:
            res = _send(d.get("target") or "stub", payload, order_id)
        except SendError as e:
            d["last_error"] = str(e)
            gave_up = row["attempts"] >= MAX_ATTEMPTS
            row["status"], d["gave_up"] = "failed", gave_up
            row["next_ts"] = now + BACKOFF[min(row["attempts"] - 1, len(BACKOFF) - 1)]
            d["next_retry_at"] = None if gave_up else _iso(row["next_ts"])
            _save(row)
            store.audit("order.erp_gave_up" if gave_up else "order.erp_failed", "erp-adapter", "system", order_id,
                        buyer, {**ev, "error": d["last_error"], "next_retry_at": d["next_retry_at"]})
            return row
        d.update(last_error=None, next_retry_at=None, sent_at=_iso(now), confirmation_id=res.get("confirmation_id"))
        row["status"] = "sent"
        store.audit("order.erp_sent", "erp-adapter", "system", order_id, buyer, ev)
        if res["status"] == "confirmed":
            row["status"], d["confirmed_at"] = "confirmed", _iso(_clock())
            store.audit("order.erp_confirmed", "erp-adapter", "system", order_id, buyer,
                        {**ev, "confirmation_id": d["confirmation_id"]})
        _save(row)
        return row


def submit(order: dict) -> dict:
    """After an order became `ordered`: queue it and try to send it once now. Never raises (the order stands
    whatever the ERP does); returns the order with `erp` attached."""
    try:
        if enqueue(order) is not None:
            attempt(order["id"])
    except Exception as e:  # noqa: BLE001 - the outbox row (if written) is retried by the sweep
        store.audit("order.erp_failed", "erp-adapter", "system", order["id"], order.get("buyer"),
                    {"order_id": order["id"], "error": f"{type(e).__name__}: {e}"[:300], "stage": "enqueue"})
    return attach(order)


def process_due(limit: int = 50) -> int:
    """Queue what is missing (reconcile), then send or retry what is due (the background sweep calls this at
    startup and every minute). Returns how many were attempted."""
    reconcile()
    rows = store._exec("SELECT order_id, data FROM erp_outbox WHERE status IN ('pending', 'failed') AND next_ts <= ? "
                       "ORDER BY next_ts LIMIT ?", (_clock(), limit))[0]
    n = 0
    for r in rows:
        if json.loads(r["data"]).get("gave_up"):
            continue
        attempt(r["order_id"])
        n += 1
    return n


def status(order_id: str) -> dict | None:
    """The `erp` block for an order, or None when it was never queued (not ordered, or ordered before the adapter)."""
    row = _get(order_id)
    if row is None or row["status"] == "legacy":
        return None
    d = row["data"]
    return {"status": row["status"], "confirmation_id": d.get("confirmation_id"), "payload_hash": row["payload_hash"],
            "format": "ORDERS05 (JSON)", "endpoint": d["endpoint"], "customer_number": d.get("customer_number"),
            "attempts": row["attempts"], "last_error": d.get("last_error"), "next_retry_at": d.get("next_retry_at"),
            "gave_up": bool(d.get("gave_up")), "sent_at": d.get("sent_at"), "confirmed_at": d.get("confirmed_at"),
            "note": "Operational metadata, not signed. The stub stands in for Valio Aimo's ERP; see docs/INTEGRATION.md."
            if d["endpoint"] == "stub" else "Operational metadata, not signed."}


def attach(order: dict) -> dict:
    if order and order.get("status") == "ordered":
        try:
            order = {**order, "erp": status(order["id"])}
        except Exception:  # noqa: BLE001 - never fail an order read over the outbox
            pass
    return order


def document(order_id: str) -> dict | None:
    """The exact payload sent (or queued) for the order, with its status."""
    row = _get(order_id)
    return None if row is None or row["status"] == "legacy" else {"order_id": order_id, "erp": status(order_id), "payload": json.loads(row["payload"])}
