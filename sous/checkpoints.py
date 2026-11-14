"""Signed audit checkpoints: so deleting the newest audit events can't go unnoticed.

The audit log is a SHA-256 hash chain (sous/store.py): editing or removing an event in the middle breaks it. What
a chain alone can't show is a cut tail: drop the last N events and what is left is a shorter, perfectly valid chain.
Checkpoints close that:

- Every CHECKPOINT_EVERY_S seconds (default 300, the API's background sweep) and on GET /audit/checkpoints when
  the chain has moved on, the gateway signs `{seq, head, events, at}` for the newest event (Ed25519 JWS, typ
  sous-checkpoint+jws, the same key as quotes and receipts) and keeps it.
- Every signed receipt carries `audit_head` ({seq, hash} of the chain when it was signed), so every buyer holding a
  receipt holds a point of the chain.
- GET /audit/verify checks every stored checkpoint against the chain (the event at its seq must still have that
  hash; the chain must still reach it), and `?checkpoint=<jws>` checks one a buyer or Valio saved: its signature
  and that the chain still contains it. A tail cut below any saved checkpoint or receipt fails.

Stored checkpoints are checked by their signed payload (the row's columns must match it). Someone with database
access could still delete stored checkpoints together with the tail; what they can't do is make a checkpoint or
receipt that a buyer or Valio saved stop failing. Anchoring heads outside the gateway (a transparency log, a
timestamping service) is the production next step.
"""

from __future__ import annotations

import time

from . import config, signing, store

TYP = "sous-checkpoint+jws"
EVERY_S = float(config.env("CHECKPOINT_EVERY_S", "300"))

_SQLITE = """CREATE TABLE IF NOT EXISTS audit_checkpoint (
  seq INTEGER PRIMARY KEY, head TEXT NOT NULL, events INTEGER NOT NULL, at TEXT NOT NULL, jws TEXT NOT NULL)"""
_MARIADB = """CREATE TABLE IF NOT EXISTS audit_checkpoint (
  seq BIGINT PRIMARY KEY, head CHAR(64) NOT NULL, events BIGINT NOT NULL, at VARCHAR(40) NOT NULL,
  jws TEXT NOT NULL) WITH SYSTEM VERSIONING"""
_ready_for = None


def _ensure() -> None:
    global _ready_for
    with store._lock:
        conn = store.db()
        if _ready_for is conn:
            return
        store._exec(_MARIADB if config.DB_URL else _SQLITE)
        _ready_for = conn


def head() -> dict | None:
    """{seq, hash} of the newest audit event, or None for an empty log."""
    rows = store._exec("SELECT seq, hash FROM audit_event ORDER BY seq DESC LIMIT 1")[0]
    return {"seq": int(rows[0]["seq"]), "hash": rows[0]["hash"]} if rows else None


def latest() -> dict | None:
    _ensure()
    rows = store._exec("SELECT seq, head, events, at, jws FROM audit_checkpoint ORDER BY seq DESC LIMIT 1")[0]
    return dict(rows[0]) if rows else None


def make(force: bool = False) -> dict | None:
    """Sign a checkpoint of the current head unless the newest one already covers it (or is younger than
    EVERY_S, unless `force`). Returns the newest checkpoint either way."""
    _ensure()
    h = head()
    last = latest()
    if h is None:
        return last
    if last and int(last["seq"]) >= h["seq"]:
        return last
    if last and not force and time.time() - _epoch(last["at"]) < EVERY_S:
        return last
    events = int(store._exec("SELECT COUNT(*) AS n FROM audit_event")[0][0]["n"])
    body = {"seq": h["seq"], "head": h["hash"], "events": events, "at": store.now_iso()}
    jws = signing.sign(body, TYP)
    store._upsert("audit_checkpoint", ["seq", "head", "events", "at", "jws"],
                  (body["seq"], body["head"], body["events"], body["at"], jws))
    return {**body, "jws": jws}


def _epoch(iso: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(iso).timestamp()


def listing(limit: int = 20) -> dict:
    make()  # a reader always gets one that covers the chain as it is now (at most EVERY_S old)
    rows = store._exec("SELECT seq, head, events, at, jws FROM audit_checkpoint ORDER BY seq DESC LIMIT ?",
                       (limit,))[0]
    return {"typ": TYP, "keys": "/.well-known/jwks.json", "every_s": EVERY_S,
            "checkpoints": [dict(r) for r in rows],
            "how_to_use": "Save any checkpoint's jws. Later, GET /audit/verify?checkpoint=<jws> proves the chain "
                          "still contains that event: nothing up to it was cut or rewritten."}


def check(hashes: dict[int, str], saved: str | None = None) -> dict:
    """Stored checkpoints (and an optional saved one) against the chain's {seq: hash}."""
    _ensure()
    problems = []
    rows = store._exec("SELECT seq, head, jws FROM audit_checkpoint ORDER BY seq")[0]
    for r in rows:
        # The signed payload is the checkpoint; the row's seq/head columns are only an index and must match it
        # (otherwise a database writer could point a validly signed checkpoint at an earlier head).
        body = signing.verify(r["jws"], TYP)
        if body is None:
            problems.append({"seq": int(r["seq"]), "why": "checkpoint signature does not verify"})
        elif int(body.get("seq", -1)) != int(r["seq"]) or body.get("head") != r["head"]:
            problems.append({"seq": int(r["seq"]), "why": "the stored row doesn't match what the checkpoint signs"})
        elif hashes.get(int(body["seq"])) != body["head"]:
            problems.append({"seq": int(body["seq"]), "why": "the chain no longer has this checkpoint's event"})
    out: dict = {"stored": len(rows), "ok": not problems, "problems": problems,
                 "latest_seq": int(rows[-1]["seq"]) if rows else None}
    if saved:
        body = signing.verify(saved, TYP)
        if body is None:
            out["saved"] = {"ok": False, "why": "not a checkpoint signed by this gateway's key"}
        else:
            found = hashes.get(int(body["seq"])) == body["head"]
            out["saved"] = {"ok": found, "seq": body["seq"], "at": body.get("at"),
                            **({} if found else {"why": "the chain no longer contains this event: cut or rewritten"})}
        out["ok"] = out["ok"] and out["saved"]["ok"]
    return out

