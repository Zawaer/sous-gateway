"""Notifications for the notify and ask tiers, and for human decisions.

`send(event, order, mandate)` always writes the in-app feed (GET /notifications), then
delivers to the external channels in a background thread with a short timeout:

- ntfy.sh push to the buyer's phone (free, no account): SOUS_NTFY_TOPIC, optional
  SOUS_NTFY_SERVER (default https://ntfy.sh) and SOUS_NTFY_TOKEN. Ask-tier
  pushes carry the approval link as a click action.
- Generic webhook (POST JSON): SOUS_NOTIFY_WEBHOOK_URL. With
  SOUS_NOTIFY_WEBHOOK_SECRET the body is signed:
  X-Sous-Signature: sha256=<hex HMAC-SHA256 of the raw body>.

A mandate may name its own channels (attributes `notify_webhook_url`, `notify_ntfy_topic`);
they win over the environment. A failed notification never blocks or fails an order: each
delivery is audited as notification.sent or notification.failed.

Events: order.notify, order.ask (to the buyer's human), order.decided (FYI to the agent
side after a human approves or rejects), test.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
from datetime import datetime

import httpx

from . import config, notifications_store, store
from .mandate import Mandate

log = logging.getLogger(__name__)

AUDIENCE = {"order.notify": "buyer", "order.ask": "buyer", "order.decided": "agent", "test": "buyer"}


def _env(name: str, default: str = "") -> str:
    if name.startswith("SOUS_"):  # pre-rename VALTAKIRJA_* names still work
        v = os.getenv(name)
        if v is None:
            v = os.getenv("VALTAKIRJA_" + name[5:], default)
        return (v or "").strip()
    return os.getenv(name, default).strip()


def _timeout() -> float:
    try:
        return float(_env("SOUS_NOTIFY_TIMEOUT", "5"))
    except ValueError:
        return 5.0


# --- plain-language messages -------------------------------------------------

def eur(x: float | None) -> str:
    x = float(x or 0)
    return f"€{x:,.0f}" if x == int(x) else f"€{x:,.2f}"


def _qty(q) -> str:
    q = float(q or 0)
    return f"{q:,.0f}" if q == int(q) else f"{q:,.2f}".rstrip("0").rstrip(".")


def what(order: dict) -> str:
    lines = order.get("lines") or []
    if not lines:
        return "nothing"
    first = lines[0]
    s = f"{_qty(first.get('quantity'))} × {first.get('name') or first.get('product_id')}"
    return s if len(lines) == 1 else f"{s} and {len(lines) - 1} more line{'s' if len(lines) > 2 else ''}"


def fmt_time(ts: str) -> str:
    dt = datetime.fromisoformat(ts).astimezone(config.TZ)
    return f"{dt:%a} {dt.day}.{dt.month} {dt:%H:%M}"


def earliest_cutoff(order: dict) -> str | None:
    cuts = [l["order_cutoff"] for l in order.get("lines") or [] if l.get("order_cutoff")]
    return min(cuts, key=lambda c: datetime.fromisoformat(c)) if cuts else None


def _findings(order: dict, tier: str) -> list[dict]:
    return [f for f in (order.get("decision") or {}).get("findings", [])
            if f.get("outcome") == tier and f.get("rule") != "mandate.ok"]


def _sentence(s: str) -> str:
    return s.strip().rstrip(".")


def compose(event: str, order: dict, mandate: Mandate) -> dict:
    """Title, message and link for one event, in plain language."""
    oid, total = order.get("id"), eur(order.get("total_ex_vat"))
    url = order.get("order_url")
    if event == "order.notify":
        fs = _findings(order, "notify")
        if any(f["rule"] == "spend.notify" for f in fs) and mandate.notify_above_eur is not None:
            why = f" (above your {eur(mandate.notify_above_eur)} FYI threshold)."
        elif fs:
            why = f". FYI: {_sentence(fs[0]['message'])}."
        else:
            why = "."
        return {"title": f"Ordered: {oid} ({total})",
                "message": f"Ordered {what(order)} for {total} ex VAT{why}", "url": url}
    if event == "order.ask":
        fs = _findings(order, "ask")
        reason = _sentence(fs[0]["message"]) if fs else "This order is outside what the agent may do alone"
        more = f" (+{len(fs) - 1} more reason{'s' if len(fs) > 2 else ''})" if len(fs) > 1 else ""
        cut = earliest_cutoff(order)
        by = f"Approve by {fmt_time(cut)} (cut-off)" if cut else "Approve or reject it"
        msg = (f"Approval needed: {reason}{more}. {by}. "
               f"Order: {what(order)} for {total} ex VAT. "
               "If nobody answers, it expires and nothing is ordered.")
        return {"title": f"Approval needed: {oid} ({total})", "message": msg,
                "url": order.get("approval_url") or url, "deadline": cut}
    if event == "order.decided":
        a = order.get("approval") or {}
        who, status, note = a.get("by") or "A human", order.get("status"), a.get("note")
        if status == "ordered":
            msg = f"{who} approved {oid}: ordered {what(order)} for {total} ex VAT."
        elif status == "rejected":
            msg = f"{who} rejected {oid}" + (f": {_sentence(note)}" if note else "") + ". Nothing was ordered."
        else:
            msg = f"{who} approved {oid}, but it is {status}: {_sentence(a.get('result_reason') or '')}. " \
                  "Nothing was ordered."
        return {"title": f"{oid} {status}", "message": msg, "url": url}
    return {"title": "Sous test notification",
            "message": f"Test notification for {mandate.buyer} (mandate {mandate.id}). Channels work.", "url": url}


# --- channels -----------------------------------------------------------------

def channels(mandate: Mandate, audience: str) -> dict[str, str]:
    out = {}
    hook = getattr(mandate, "notify_webhook_url", None) or _env("SOUS_NOTIFY_WEBHOOK_URL")
    if hook:
        out["webhook"] = hook
    topic = getattr(mandate, "notify_ntfy_topic", None) or _env("SOUS_NTFY_TOPIC")
    if topic and audience == "buyer":  # the phone belongs to the buyer's human
        out["ntfy"] = topic
    return out


def _webhook(url: str, payload: dict) -> int:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "X-Sous-Event": payload["event"]}
    if secret := _env("SOUS_NOTIFY_WEBHOOK_SECRET"):
        headers["X-Sous-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    r = httpx.post(url, content=body, headers=headers, timeout=_timeout())
    r.raise_for_status()
    return r.status_code


def _ntfy(topic: str, n: dict) -> int:
    server = _env("SOUS_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    ask = n["event"] == "order.ask"
    msg = {"topic": topic, "title": n["title"], "message": n["message"], "priority": 4 if ask else 3,
           "tags": ["warning"] if ask else ["shopping_cart"]}
    if (url := n.get("url")) and url.startswith("http"):
        msg["click"] = url
        if ask:
            msg["actions"] = [{"action": "view", "label": "Review and approve", "url": url, "clear": True}]
    headers = {"Authorization": f"Bearer {tok}"} if (tok := _env("SOUS_NTFY_TOKEN")) else {}
    # JSON publishing (POST to the server root) keeps € and ä intact, unlike header-based publishing
    r = httpx.post(server, json=msg, headers=headers, timeout=_timeout())
    r.raise_for_status()
    return r.status_code


def _deliver(n: dict, chans: dict[str, str], order: dict) -> None:
    payload = {"event": n["event"], "audience": n["audience"], "title": n["title"], "message": n["message"],
               "url": n.get("url"), "deadline": n.get("deadline"), "ts": n.get("ts"),
               "order": {k: order.get(k) for k in ("id", "buyer", "mandate_id", "agent_id", "status",
                                                    "total_ex_vat", "delivery_date", "order_url", "approval_url")}}
    for name, target in chans.items():
        try:
            code = _webhook(target, payload) if name == "webhook" else _ntfy(target, n)
            ev, data = "notification.sent", {"channel": name, "event": n["event"], "status_code": code}
        except Exception as e:  # noqa: BLE001 - any failure is audited, never raised
            ev, data = "notification.failed", {"channel": name, "event": n["event"],
                                               "error": f"{type(e).__name__}: {e}"[:300]}
        try:
            store.audit(ev, "notifier", "system", order.get("id"), order.get("buyer"), data)
        except Exception:  # noqa: BLE001
            log.exception("could not audit notification")


def send(event: str, order: dict, mandate: Mandate) -> threading.Thread | None:
    """Record the notification in the feed and deliver it in the background. Never raises."""
    try:
        audience = AUDIENCE.get(event, "buyer")
        n = {"event": event, "audience": audience, "buyer": order.get("buyer") or mandate.buyer,
             "order_id": order.get("id"), "tier": (order.get("decision") or {}).get("outcome"),
             **compose(event, order, mandate)}
        chans = channels(mandate, audience)
        n["data"] = {"channels": sorted(chans), "deadline": n.pop("deadline", None), "status": order.get("status")}
        n = notifications_store.add(n)
        store.audit("notification.sent", "notifier", "system", order.get("id"), n["buyer"],
                    {"channel": "feed", "event": event, "notification_seq": n["seq"]})
        n["deadline"] = n["data"]["deadline"]
        if not chans:
            return None
        t = threading.Thread(target=_deliver, args=(n, chans, order), name=f"notify-{order.get('id')}", daemon=True)
        t.start()
        return t
    except Exception:  # noqa: BLE001 - notifications must never break ordering
        log.exception("notification %s for %s failed", event, order.get("id"))
        try:
            store.audit("notification.failed", "notifier", "system", order.get("id"), order.get("buyer"),
                        {"channel": "feed", "event": event})
        except Exception:  # noqa: BLE001
            pass
        return None
