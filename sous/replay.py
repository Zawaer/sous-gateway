"""Offline Valio Aimo replay: serve price and availability from a recorded snapshot, never from the network.

For large offline evaluations (scripts/redteam_run.py) that must not touch the shared, rate-limited Valio Aimo
key. Switched on with SOUS_VALIO_REPLAY=<snapshot.json> (or replay.enable(path)); sous/aimo.py then answers
every prices()/availability() call from here and makes no HTTP call at all.

Snapshot file (scripts/redteam_snapshot.py writes it from ONE batched live call per kind):

    {"recorded_at": "2026-11-14T12:05:00+02:00", "requested_date": "2026-11-16", "source": "...",
     "price": {"<productId>": {...item as returned...}}, "availability": {"<productId>": {...}}}

Ids missing from the snapshot are answered with a deterministic synthetic offer (labelled: the item carries
`_replay: "synthetic"`, and stats() counts it), so an agent that quotes a product outside the recorded subset
still gets a plausible answer instead of a hole. Recorded items carry `_replay: "recorded"`. Valio's mock
returns fixed dates whatever the requested date, so one recorded date serves every requested date.

The label for reports is label(): "Valio prices replayed from snapshot <date>".
"""

from __future__ import annotations

import hashlib
import json
import os
import statistics
import threading
from pathlib import Path

_lock = threading.Lock()
_snap: dict | None = None
_path: str | None = None
_counts = {"recorded": 0, "synthetic": 0}
_median_cmp: dict[str, float] = {}  # comparison unit -> median comparison price ex VAT (for synthetic items)


def enable(path: str | os.PathLike) -> None:
    global _snap, _path
    data = json.loads(Path(path).read_text())
    for k in ("price", "availability", "recorded_at"):
        if k not in data:
            raise ValueError(f"replay snapshot {path} has no {k!r}")
    by_unit: dict[str, list[float]] = {}
    for item in data["price"].values():
        v = (item.get("comparisonPrice") or {}).get("value")
        if v and item.get("comparisonUnit"):
            by_unit.setdefault(item["comparisonUnit"], []).append(float(v))
    with _lock:
        _snap, _path = data, str(path)
        _median_cmp.clear()
        _median_cmp.update({u: statistics.median(vs) for u, vs in by_unit.items()})
        _counts.update(recorded=0, synthetic=0)


def disable() -> None:
    global _snap, _path
    with _lock:
        _snap, _path = None, None


def active() -> bool:
    return _snap is not None


def label() -> str | None:
    if _snap is None:
        return None
    return f"Valio prices replayed from snapshot {_snap['recorded_at'][:10]}"


def info() -> dict | None:
    if _snap is None:
        return None
    return {"label": label(), "recorded_at": _snap["recorded_at"], "requested_date": _snap.get("requested_date"),
            "recorded_products": len(_snap["price"]), "source": _snap.get("source"),
            "served": dict(_counts)}


def stats() -> dict:
    with _lock:
        return dict(_counts)


def _h(pid: str, salt: str) -> float:
    """Deterministic 0..1 from a product id."""
    return int(hashlib.sha256(f"{salt}:{pid}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def _template(kind: str) -> dict:
    """A recorded item to copy dates/structure from (the first Stock item for availability)."""
    items = _snap[kind]
    if kind == "availability":
        for it in items.values():
            if it.get("logisticModel") == "Stock" and it.get("scheduleLines"):
                return it
    return next(iter(items.values()))


def _synthetic_price(pid: str) -> dict:
    from . import catalog

    p = catalog.get(pid) or {}
    size = p.get("size_from_name") or None
    unit = size[1] if size else None
    per = _median_cmp.get(unit or "", 5.0) * (0.6 + 0.8 * _h(pid, "price"))
    content = float(size[0]) if size else 1.0
    units = p.get("units") or {}
    try:  # pack hierarchy: a carton of 6 x 1 l holds 6 l
        content *= float(units.get(p.get("sales_unit"), 1.0)) / float(units.get(p.get("base_unit"), 1.0) or 1.0)
    except (TypeError, ValueError):
        pass
    value = round(max(0.2, per * content), 2)
    per = value / content if content else per
    t = _template("price")
    item = {**{k: v for k, v in t.items() if k in ("tax", "_timeNow")},
            "productId": pid, "error": False,
            "catalogPrice": {**(t.get("catalogPrice") or {}), "value": value, "valueWithTax": round(value * 1.14, 2)},
            "_replay": "synthetic"}
    if unit:
        item["comparisonPrice"] = {"value": round(per, 4)}
        item["comparisonUnit"] = unit
    return item


def _synthetic_availability(pid: str) -> dict:
    from . import catalog

    p = catalog.get(pid) or {}
    t = _template("availability")
    sl = dict((t.get("scheduleLines") or [{}])[0])
    r = _h(pid, "stock")
    amount = 0.0 if r < 0.05 else round(5 + 995 * r)  # ~5 % out of stock, else 5..1000 sales units
    sl.update(availableAmount=amount, salesUnit=p.get("sales_unit") or sl.get("salesUnit"), salesBlock=False)
    return {**t, "productId": pid, "scheduleLines": [sl], "salesBlock": False, "_replay": "synthetic"}


def lookup(kind: str, ids: list[str]) -> dict[str, dict]:
    """kind: 'price' | 'availability'. Every id gets an item: recorded when the snapshot has it, else synthetic."""
    if _snap is None:
        raise RuntimeError("replay is not enabled")
    out: dict[str, dict] = {}
    rec = syn = 0
    for pid in dict.fromkeys(ids):
        item = _snap[kind].get(pid)
        if item is not None:
            out[pid] = {**item, "_replay": "recorded"}
            rec += 1
        else:
            out[pid] = _synthetic_price(pid) if kind == "price" else _synthetic_availability(pid)
            syn += 1
    with _lock:
        _counts["recorded"] += rec
        _counts["synthetic"] += syn
    return out


if os.getenv("SOUS_VALIO_REPLAY"):
    enable(os.environ["SOUS_VALIO_REPLAY"])
