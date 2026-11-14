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

Events: order.notify, order.ask, order.block (refused by the mandate: nothing ordered; to the buyer's human), order.decided (FYI to the agent
side after a human approves or rejects), product.held (Valio put a product on hold that the buyer has
open, standing or, for a recall, recently placed orders for; sous/holds.py), order.expired (a held order
passed its approve_by: delivery lost, next date suggested), order.amended / order.amendment_pending /
order.amendment_decided (Valio changed an ordered order after the fact: sous/amendments.py), test.
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

AUDIENCE = {"order.notify": "buyer", "order.ask": "buyer", "order.block": "buyer", "order.decided": "agent",
            "product.held": "buyer",
            "order.expired": "buyer",
            "order.amended": "buyer", "order.amendment_pending": "buyer", "order.amendment_decided": "buyer",
            "offer.received": "agent",
            "test": "buyer"}


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


def _day(d: str | None) -> str:
    if not d:
        return "the requested day"
    x = datetime.fromisoformat(d)
    return f"{x:%a} {x.day}.{x.month}"


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
        cut = order.get("approve_by") or earliest_cutoff(order)
        lost = (order.get("approve_by_detail") or {}).get("delivery_date") or order.get("delivery_date")
        by = (f"Approve by {fmt_time(cut)} or delivery on {_day(lost)} is lost" if cut and lost
              else f"Approve by {fmt_time(cut)}" if cut else "Approve or reject it")
        msg = (f"Approval needed: {reason}{more}. {by}. "
               f"Order: {what(order)} for {total} ex VAT. "
               "If nobody answers, it expires and nothing is ordered.")
        return {"title": f"Approval needed: {oid} ({total})", "message": msg,
                "url": order.get("approval_url") or url, "deadline": cut}
    if event == "order.block":
        fs = _findings(order, "block")
        reason = _sentence(fs[0]["message"]) if fs else "It is outside what the mandate allows"
        more = f" (+{len(fs) - 1} more reason{'s' if len(fs) > 2 else ''})" if len(fs) > 1 else ""
        return {"title": f"Refused: {oid}", "message":
                f"Your agent's order was refused and nothing was ordered: {reason}{more}. "
                f"Order: {what(order)}. Ask your agent for another product or date, or change the mandate.",
                "url": url}
    if event == "order.expired":
        x = order.get("expiry") or {}
        lost, nxt = x.get("lost_delivery_date"), x.get("next_delivery_date")
        msg = (f"Nobody approved {oid} by {fmt_time(x.get('approve_by') or order.get('approve_by'))}, the deadline "
               f"before Valio's order cut-off: delivery on {_day(lost)} is lost and nothing was ordered. "
               f"Order: {what(order)} for {total} ex VAT."
               + (f" Next option: ask your agent to quote it for {_day(nxt)}." if nxt else ""))
        return {"title": f"Expired: {oid} missed the cut-off", "message": msg, "url": url}
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
    if event == "product.held":
        return held_message(order)
    if event.startswith("order.amend"):
        return amendment_message(event, order)
    if event == "offer.received":
        o = order["offer"]
        names = ", ".join(f"{p['name']} €{p['price_ex_vat']:.2f}" for p in o["products"])
        return {"title": f"Valio Aimo offer: {o['headline']}",
                "message": f"Valio Aimo offers {names} ex VAT per sales unit, up to {o['quantity_cap']:g} units, "
                           f"until {fmt_time(o['valid_until'])} ({o['headline']}). Commercial offer: nothing is "
                           "ordered unless your agent quotes it and your mandate allows it.", "url": None}
    return {"title": "Sous test notification",
            "message": f"Test notification for {mandate.buyer} (mandate {mandate.id}). Channels work.", "url": url}


def _fi_reasons(order: dict, tier: str) -> list[str]:
    """Finnish twins (order.explanations_fi) of the findings _findings() picks, in the same order."""
    from . import fi
    ex = (order.get("explanations_fi") or {}).get("findings") or {}
    out = []
    for i, f in enumerate((order.get("decision") or {}).get("findings", [])):
        if f.get("outcome") == tier and f.get("rule") != "mandate.ok":
            out.append(ex.get(str(i)) or fi.fallback(f.get("rule") or "", f.get("product_id")))
    return out


def _fi_what(order: dict) -> str:
    from . import fi
    lines = order.get("lines") or []
    if not lines:
        return "ei mitään"
    first = lines[0]
    s = f"{fi.num(float(first.get('quantity') or 0))} × {first.get('name') or first.get('product_id')}"
    return s if len(lines) == 1 else f"{s} ja {len(lines) - 1} muu{'ta' if len(lines) > 2 else ''} rivi{'ä' if len(lines) > 2 else ''}"


def compose_fi(event: str, order: dict, mandate: Mandate) -> dict | None:
    """Finnish title and message for the order events (None for the others): the same facts as compose(), with the
    reasons from order.explanations_fi. Never sent instead of the English; carried beside it (title_fi, message_fi)."""
    from . import fi
    oid, total = order.get("id"), fi.eur(order.get("total_ex_vat"), whole=True)
    sent = lambda s: s.strip().rstrip(".")  # noqa: E731
    if event == "order.notify":
        fs = _fi_reasons(order, "notify")
        if any(f["rule"] == "spend.notify" for f in _findings(order, "notify")) and mandate.notify_above_eur is not None:
            why = f" (yli {fi.eur(mandate.notify_above_eur, whole=True)} ilmoitusrajasi)."
        elif fs:
            why = f". Tiedoksi: {sent(fs[0])}."
        else:
            why = "."
        return {"title": f"Tilattu: {oid} ({total})", "message": f"Tilattu {_fi_what(order)}, {total} (alv 0 %){why}"}
    if event in ("order.ask", "order.block"):
        tier = "ask" if event == "order.ask" else "block"
        fs = _fi_reasons(order, tier)
        more = f" (+{len(fs) - 1} muu{'ta' if len(fs) > 2 else ''} syy{'tä' if len(fs) > 2 else ''})" if len(fs) > 1 else ""
        if tier == "block":
            reason = sent(fs[0]) if fs else "Tilaus ei mahdu valtakirjan rajoihin"
            return {"title": f"Estetty: {oid}", "message":
                    f"Agenttisi tilaus estettiin, eikä mitään tilattu: {reason}{more}. Tilaus: {_fi_what(order)}. "
                    "Pyydä agentilta toinen tuote tai päivä, tai muuta valtakirjaa."}
        reason = sent(fs[0]) if fs else "Tilaus on enemmän kuin agentti saa tehdä itse"
        cut = order.get("approve_by") or earliest_cutoff(order)
        lost = (order.get("approve_by_detail") or {}).get("delivery_date") or order.get("delivery_date")
        by = (f"Hyväksy viimeistään {fi.when(cut)}, tai toimitus {fi.day(lost)} menetetään" if cut and lost
              else f"Hyväksy viimeistään {fi.when(cut)}" if cut else "Hyväksy tai hylkää se")
        return {"title": f"Hyväksyntä tarvitaan: {oid} ({total})", "message":
                f"Hyväksyntä tarvitaan: {reason}{more}. {by}. Tilaus: {_fi_what(order)}, {total} (alv 0 %). "
                "Jos kukaan ei vastaa, tilaus vanhenee eikä mitään tilata."}
    if event == "order.expired":
        x = order.get("expiry") or {}
        lost, nxt = x.get("lost_delivery_date"), x.get("next_delivery_date")
        return {"title": f"Vanhentunut: {oid} ei ehtinyt tilausaikaan", "message":
                f"Kukaan ei hyväksynyt tilausta {oid} viimeistään {fi.when(x.get('approve_by') or order.get('approve_by'))}, "
                f"Valion tilausaikaa ennen: toimitus {fi.day(lost) if lost else 'pyydettynä päivänä'} menetettiin, "
                f"eikä mitään tilattu. Tilaus: {_fi_what(order)}, {total} (alv 0 %)."
                + (f" Seuraava vaihtoehto: pyydä agentilta tarjous päivälle {fi.day(nxt)}." if nxt else "")}
    if event == "order.decided":
        a = order.get("approval") or {}
        who, status, note = a.get("by") or "Ihminen", order.get("status"), a.get("note")
        st = {"ordered": "tilattu", "rejected": "hylätty", "refused": "estetty", "expired": "vanhentunut"}.get(
            status, status)
        if status == "ordered":
            msg = f"{who} hyväksyi tilauksen {oid}: tilattu {_fi_what(order)}, {total} (alv 0 %)."
        elif status == "rejected":
            msg = f"{who} hylkäsi tilauksen {oid}" + (f": {sent(note)}" if note else "") + ". Mitään ei tilattu."
        else:
            why = (order.get("explanations_fi") or {}).get("result_reason") or a.get("result_reason") or ""
            msg = f"{who} hyväksyi tilauksen {oid}, mutta se on {st}: {sent(why)}. Mitään ei tilattu."
        return {"title": f"{oid} {st}", "message": msg}
    return None


_AMEND_KIND = {"short_pick": "short pick", "delay": "delay", "substitution_at_pick": "substitution",
               "cancel_line": "line cancelled"}


def amendment_message(event: str, order: dict) -> dict:
    """order.amended / order.amendment_pending / order.amendment_decided: `order["amendment"]` is the amendment
    (sous/amendments.py)."""
    a, oid, url = order.get("amendment") or {}, order.get("id"), order.get("order_url")
    kind = _AMEND_KIND.get(a.get("kind"), a.get("kind"))
    why = _sentence(next((f["message"] for f in a.get("findings") or [] if f.get("outcome") == a.get("tier")),
                         a.get("reason") or ""))
    total = f" New total {eur(order.get('total_ex_vat'))} ex VAT."
    credit = (a.get("effect") or {}).get("credit_ex_vat")
    if credit:
        total += f" Credit {eur(-credit)}." if credit < 0 else f" Extra charge {eur(credit)}."
    if event == "order.amendment_pending":
        return {"title": f"Valio changed {oid}: {kind}, your answer needed",
                "message": f"Valio Aimo reports a {kind} on {oid}: {why}. Accept or reject it "
                           f"(amendment {a.get('id')}); rejecting cancels the line(s) concerned.", "url": url}
    if event == "order.amendment_decided":
        r = a.get("resolution") or {}
        return {"title": f"{oid}: {kind} {a.get('status')}",
                "message": f"{r.get('by') or 'The buyer'} {a.get('status')} the {kind} on {oid} "
                           f"(amendment {a.get('id')}).{total}", "url": url}
    return {"title": f"Valio changed {oid}: {kind}", "message": f"Valio Aimo reports a {kind} on {oid}: {why}.{total}",
            "url": url}


def held_message(n: dict) -> dict:
    """product.held: `n` carries the public hold and the buyer's affected orders (sous/holds.py)."""
    from .holds import KIND_TEXT, RECALL_LOOKBACK_DAYS

    h, refs = n["hold"], n.get("affected") or []
    name = h.get("name") or h["product_id"]
    ids = lambda role: ", ".join(r["id"] for r in refs if r["role"] == role)  # noqa: E731
    parts = []
    if p := ids("pending_approval"):
        parts.append(f"Waiting for approval: {p} (approving is now blocked).")
    if s := ids("standing_order"):
        parts.append(f"Standing order {s}: renewals skip it or are blocked.")
    if r := ids("recently_ordered"):
        parts.append(f"Already ordered in the last {RECALL_LOOKBACK_DAYS} days: {r}. Check deliveries and stock on hand.")
    title = ("Recall: " if h["kind"] == "recall" else "Product hold: ") + name
    msg = f"Valio Aimo has put {name} ({h['product_id']}) {KIND_TEXT[h['kind']]}: {_sentence(h['reason'])}."
    return {"title": title, "message": " ".join([msg, *parts]), "url": None}


# --- channels -----------------------------------------------------------------

def channels(mandate: Mandate, audience: str) -> dict[str, str]:
    out = {}
    if getattr(mandate, "sandbox", None):  # judge sandboxes: in-app feed only, never the demo phone or webhook
        return out
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
           "tags": ["warning"] if ask else ["no_entry"] if n["event"] == "order.block" else ["shopping_cart"]}
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
    fi_n = (n.get("data") or {}).get("fi") or {}
    payload = {"event": n["event"], "audience": n["audience"], "title": n["title"], "message": n["message"],
               **({"title_fi": fi_n.get("title"), "message_fi": fi_n.get("message")} if fi_n else {}),
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
        try:
            if fi_n := compose_fi(event, order, mandate):  # Finnish beside the English: data.fi {title, message}
                n["data"]["fi"] = fi_n
        except Exception:  # noqa: BLE001 - a Finnish text never stops the English notification
            log.exception("Finnish notification text for %s failed", order.get("id"))
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
