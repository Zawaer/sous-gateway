"""Needs changed mid-contract: when a mandate update tightens the buyer's allergen or diet constraints, everything
already running on the old assumptions gets a visible control step.

Typical: a hospital kitchen's ward goes nut-free or gluten-free. The buyer's human updates the mandate
(POST /mandates, or a re-signed buyer credential) with a new avoided allergen (avoid_allergens), a new required
claim (required_claims, e.g. FREE_FROM_GLUTEN, VEGAN), may-contain no longer accepted (allow_may_contain false) or
missing allergen data now refused (unknown_allergens block). Sous then re-checks, for that mandate only:

  standing orders   every candidate product of every active standing order
  held orders       every pending_approval order (still waiting for a human)
  upcoming orders   ordered orders whose delivery day is today or later (already sent: flagged, not changed)
  remembered        approve-and-remember decisions (sous/remembered.py) and allergen clearances (sous/clearance.py)

A product conflicts when it fails the new constraint in a way it didn't fail the old one: it contains or may
contain a newly avoided allergen, has no allergen data for it, lacks a newly required claim, and so on. Then:

- a standing order with a conflicting product is paused (held) with a needs.changed finding; only a human resumes
  it (POST /standing-orders/{id}/resume), never Sous;
- a held order gets a needs.changed finding (ask) in its decision, so the approver sees what changed; approval
  re-checks the mandate as it is now anyway (an order with a newly avoided allergen is refused at approval);
- an ordered, not yet delivered order is listed for review (Sous never cancels or changes it silently);
- a remembered decision or clearance that would now be unsafe is revoked (audited remembered.revoked /
  allergen.clearance_revoked, authority needs.changed).

Nothing is silently changed or dropped, and nothing unaffected is touched. Where possible the review proposes a
compliant substitute from the substitute ranking (sous/substitutes.py), as a proposal only: the buyer's agent
quotes it with substitute_for, and the mandate decides as always. The result is one summary (fi/en): the
`needs_review` field of the mandate update's response, stored (GET /mandates/{id}/needs-reviews), audited
(needs.changed), and a buyer notification for the dashboard.

Loosening a constraint never expands anything automatically: held standing orders stay paused and revoked
decisions stay revoked until a human acts.
"""

from __future__ import annotations

import json
import secrets
from datetime import date, datetime, timedelta

from . import catalog, clearance, config, deadline, fi, remembered, standing_store as SS, store
from .mandate import Finding, Mandate, readable_name

RULE = "needs.changed"
MAX_SUBSTITUTES = 5  # live substitute lookups per review (each prices a shortlist at Valio Aimo)
CLAIM_EN = {"FREE_FROM_LACTOSE": "lactose-free", "LOW_LACTOSE": "low-lactose", "FREE_FROM_GLUTEN": "gluten-free",
            "VEGAN": "vegan", "VEGETARIAN": "vegetarian", "FREE_FROM_MILK": "milk-free", "FREE_FROM_EGGS": "egg-free",
            "FREE_FROM_NUTS": "nut-free"}

_SQLITE = """CREATE TABLE IF NOT EXISTS needs_review (
  id TEXT PRIMARY KEY, mandate_id TEXT NOT NULL, buyer TEXT, data TEXT NOT NULL, created_at TEXT NOT NULL)"""
_MARIADB = """CREATE TABLE IF NOT EXISTS needs_review (
  id VARCHAR(32) PRIMARY KEY, mandate_id VARCHAR(128) NOT NULL, buyer VARCHAR(191), data LONGTEXT NOT NULL,
  created_at VARCHAR(40) NOT NULL, INDEX (mandate_id)
) WITH SYSTEM VERSIONING"""
COLS = ["id", "mandate_id", "buyer", "data", "created_at"]
_ready_for = None


def _ensure() -> None:
    global _ready_for
    with store._lock:
        conn = store.db()
        if _ready_for is conn:
            return
        store._exec(_MARIADB if config.DB_URL else _SQLITE)
        _ready_for = conn


def _now() -> datetime:
    from . import service
    return service._now()


# --- what changed -------------------------------------------------------------------------------------------------

def _allergen_en(codes) -> str:
    return ", ".join(catalog.ALLERGENS.get(c, c) for c in sorted(codes))


def _claim_en(codes) -> str:
    return ", ".join(CLAIM_EN.get(c, c) for c in sorted(codes))


def diff(old: Mandate, new: Mandate) -> dict:
    """{tightened: [...], loosened: [...]}: each {what, codes?, en, fi} about the allergen and diet constraints."""
    t, l = [], []
    if add := sorted(set(new.avoid_allergens) - set(old.avoid_allergens)):
        t.append({"what": "avoid_allergens", "codes": add, "en": f"now avoids {_allergen_en(add)}",
                  "fi": f"välttää nyt: {fi.allergens(add)}"})
    if gone := sorted(set(old.avoid_allergens) - set(new.avoid_allergens)):
        l.append({"what": "avoid_allergens", "codes": gone, "en": f"no longer avoids {_allergen_en(gone)}",
                  "fi": f"ei enää välttää: {fi.allergens(gone)}"})
    if add := sorted(set(new.required_claims) - set(old.required_claims)):
        t.append({"what": "required_claims", "codes": add, "en": f"must now be {_claim_en(add)}",
                  "fi": f"vaatii nyt: {fi.claims(add)}"})
    if gone := sorted(set(old.required_claims) - set(new.required_claims)):
        l.append({"what": "required_claims", "codes": gone, "en": f"no longer requires {_claim_en(gone)}",
                  "fi": f"ei enää vaadi: {fi.claims(gone)}"})
    if old.allow_may_contain and not new.allow_may_contain:
        t.append({"what": "allow_may_contain", "en": "no longer accepts 'may contain'",
                  "fi": "ei enää hyväksy 'voi sisältää' -merkintää"})
    elif new.allow_may_contain and not old.allow_may_contain:
        l.append({"what": "allow_may_contain", "en": "now accepts 'may contain'",
                  "fi": "hyväksyy nyt 'voi sisältää' -merkinnän"})
    if old.unknown_allergens == "ask" and new.unknown_allergens == "block":
        t.append({"what": "unknown_allergens", "en": "now refuses products without allergen data",
                  "fi": "estää nyt tuotteet, joilta puuttuvat allergeenitiedot"})
    elif old.unknown_allergens == "block" and new.unknown_allergens == "ask":
        l.append({"what": "unknown_allergens", "en": "asks again about products without allergen data",
                  "fi": "kysyy taas tuotteista, joilta puuttuvat allergeenitiedot"})
    return {"tightened": t, "loosened": l}


def _fails(p: dict, m: Mandate) -> dict[tuple[str, str], tuple[str, str]]:
    """{(kind, code): (english, finnish)} for every way product `p` fails the mandate's allergen and diet rules."""
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for c in sorted(set(m.required_claims) - set(p.get("claims") or [])):
        out[("claim", c)] = (f"is not marked {CLAIM_EN.get(c, c)}", f"ei ole merkitty: {fi.claims([c])}")
    if m.avoid_allergens and p.get("allergens"):
        for code in m.avoid_allergens:  # one code at a time, so the reason names the allergen that changed
            st = catalog.allergen_status(p, [code], m.allow_may_contain)
            if st["status"] == "contains":
                out[("contains", code)] = (f"contains {_allergen_en(st['codes'])}",
                                           f"sisältää: {fi.allergens(st['codes'])}")
            elif st["status"] == "may_contain":
                out[("may_contain", code)] = (f"may contain {_allergen_en(st['codes'])}",
                                              f"voi sisältää: {fi.allergens(st['codes'])}")
            elif st["status"] == "unknown":
                kind = "unknown_block" if m.unknown_allergens == "block" else "unknown"
                out[(kind, code)] = (f"has no allergen data, so it can't be confirmed free of {_allergen_en([code])}",
                                     f"allergeenitiedot puuttuvat, joten ei voida varmistaa, ettei siinä ole: "
                                     f"{fi.allergens([code])}")
    return out


def conflicts(pid: str, old: Mandate, new: Mandate) -> list[tuple[str, str]]:
    """(english, finnish) reasons product `pid` fails the new mandate but not the old one. Empty = unaffected.
    A product the catalog no longer knows can't be checked, so it conflicts (unknown is never safe)."""
    p = catalog.get(pid)
    if not p:
        return [("is no longer in the catalog, so it can't be checked", "ei ole enää tuoteluettelossa, joten sitä "
                 "ei voi tarkistaa")] if new.avoid_allergens or new.required_claims else []
    before = _fails(p, old)
    return [v for k, v in _fails(p, new).items() if k not in before]


# --- the review ---------------------------------------------------------------------------------------------------

def _day_en(d: datetime) -> str:
    return f"{d.day} {d:%b}"


def _finding(intro: tuple[str, str], what: tuple[str, str], reasons: list[tuple[str, str]],
             pid: str | None) -> Finding:
    """'Ward 3 is now nut-free since 14 Nov: the standing order Breakfast for Granola contains tree nuts.'"""
    en = f"{intro[0]}: {what[0]} " + "; ".join(r[0] for r in reasons) + "."
    fi_ = f"{intro[1]}: {what[1]} " + "; ".join(r[1] for r in reasons) + "."
    return Finding(rule=RULE, outcome="ask", message=en, product_id=pid, message_fi=fi_)


def review(old: Mandate | None, new: Mandate, *, by: str | None = None, note: str | None = None,
           now: datetime | None = None) -> dict | None:
    """Re-check everything running under `new.id` after a mandate update. None when the update doesn't touch the
    allergen and diet constraints (or it is the first version). Holds, flags and revokes as the module docstring
    says; returns the stored summary. Substitutes are added afterwards (add_substitutes), outside any lock."""
    if old is None or old.id != new.id:
        return None
    change = diff(old, new)
    if not change["tightened"] and not change["loosened"]:
        return None
    now = now or _now()
    by = by or new.issued_by
    since_en, since_fi = _day_en(now), fi.day(now)
    what_en = "; ".join(c["en"] for c in change["tightened"])
    what_fi = "; ".join(c["fi"] for c in change["tightened"])
    intro = ((f"{note.strip()} since {since_en}" if note and note.strip()
              else f"{new.buyer}'s mandate {what_en} since {since_en}"),
             (f"{note.strip()} {since_fi} alkaen" if note and note.strip()
              else f"{new.buyer}: valtakirja {what_fi} {since_fi} alkaen"))
    out = {"id": "NR-" + secrets.token_hex(4).upper(), "mandate_id": new.id, "buyer": new.buyer, "by": by,
           "note": note, "created_at": now.isoformat(timespec="seconds"), "change": change,
           "held": [], "flagged": [], "revoked": [], "unaffected": [], "substitutes": []}
    if change["tightened"]:
        _standing(out, old, new, intro, now)
        _orders(out, old, new, intro, now)
        _remembered(out, old, new, intro, by, now)
    out["summary"], out["summary_fi"] = _summary(out, intro, what_en, what_fi)
    _put(out)
    store.audit(RULE, by, "human", new.id, new.buyer,
                {k: out[k] for k in ("id", "mandate_id", "note", "change", "held", "flagged", "revoked",
                                     "unaffected", "summary")})
    if change["tightened"]:
        try:
            from . import notifications_store
            notifications_store.add({"event": RULE, "audience": "buyer", "buyer": new.buyer, "order_id": None,
                                     "tier": "ask" if out["held"] or out["flagged"] else "notify",
                                     "title": "Your needs changed: running orders re-checked",
                                     "message": out["summary"], "url": "/ui/buyer.html#/today",
                                     "data": {"needs_review_id": out["id"], "mandate_id": new.id,
                                              "summary_fi": out["summary_fi"]}})
        except Exception:  # the feed is a convenience; the review itself is stored and audited
            pass
    return out


def _standing(out: dict, old: Mandate, new: Mandate, intro, now: datetime) -> None:
    for so in SS.list_(new.id):
        if not so.get("active"):
            continue
        findings, hits = [], []
        for it in so["items"]:
            for pid in it["product_ids"]:  # any candidate: the weekly pick may land on it
                if rs := conflicts(pid, old, new):
                    name = readable_name((catalog.get(pid) or {}).get("name") or pid)
                    findings.append(_finding(intro, (f"the standing order {so['name']} for {name}",
                                                     f"vakiotilauksen {so['name']} tuote {name}"), rs, pid))
                    hits.append({"product_id": pid, "name": name, "amount": it["amount"], "unit": it["unit"]})
        if not findings:
            out["unaffected"].append({"kind": "standing_order", "id": so["id"], "name": so["name"]})
            continue
        so = {**so, "active": False, "updated_at": now.isoformat(timespec="milliseconds"),
              "needs_hold": {"review_id": out["id"], "at": now.isoformat(timespec="seconds"),
                             "findings": [f.model_dump() for f in findings]}}
        SS.put(so)
        store.audit("standing.paused", out["by"], "human", so["id"], so["buyer"],
                    {"reason": RULE, "needs_review_id": out["id"], "findings": [f.message for f in findings]})
        out["held"].append({"kind": "standing_order", "id": so["id"], "name": so["name"], "products": hits,
                            "findings": [_fd(f) for f in findings],
                            "next_step": f"Remove or replace the product, then resume (POST /standing-orders/"
                                         f"{so['id']}/resume)."})


def _fd(f: Finding) -> dict:
    return {**f.model_dump(), "message_fi": f.message_fi}


def _orders(out: dict, old: Mandate, new: Mandate, intro, now: datetime) -> None:
    from . import service as S
    today = now.astimezone(config.TZ).date().isoformat()
    for o in store.list_orders(mandate_id=new.id):
        status = o.get("status")
        if status == "ordered":
            days = deadline.delivery_days(o)
            if not days or max(days) < today:
                continue  # delivered already: nothing left to stop
        elif status != "pending_approval":
            continue
        what = (("the held order", "odottava tilaus") if status == "pending_approval" else
                (f"the order for delivery on {min(days)}", f"toimitukselle {fi.day(min(days))} tehty tilaus"))
        findings = []
        for l in o.get("lines") or []:
            if l.get("cancelled") or not l.get("product_id"):
                continue
            if rs := conflicts(l["product_id"], old, new):
                name = readable_name(l.get("name") or l["product_id"])
                findings.append(_finding(intro, (f"{what[0]} {o['id']} for {name}",
                                                 f"{what[1]} {o['id']} tuote {name}"), rs, l["product_id"]))
        if not findings:
            out["unaffected"].append({"kind": "order", "id": o["id"], "status": status})
            continue
        entry = {"kind": "order", "id": o["id"], "status": status, "findings": [_fd(f) for f in findings],
                 "products": [{"product_id": l["product_id"], "name": l.get("name"), "quantity": l.get("quantity"),
                               "unit": "SALES_UNIT"} for l in o["lines"]
                              if l.get("product_id") in {f.product_id for f in findings}]}
        if status == "pending_approval":
            with S._order_lock:
                cur = store.get_order(o["id"])
                if not cur or cur.get("status") != "pending_approval":
                    continue
                dec = cur["decision"]
                cur = {**cur, "updated_at": now.isoformat(timespec="milliseconds"),
                       "decision": {**dec, "outcome": "block" if dec.get("outcome") == "block" else "ask",
                                    "findings": dec.get("findings", []) + [f.model_dump() for f in findings]}}
                cur["receipt_jws"] = S._receipt(cur)
                if not store.update_order(cur, expected_status="pending_approval"):
                    continue
            entry["next_step"] = "Reject it, or approve it if the product is fine for everyone it feeds."
            out["held"].append(entry)
        else:
            entry["next_step"] = ("Already sent to Valio Aimo: ask Valio Aimo to change or cancel the line, or keep "
                                  "it away from the ward concerned. Sous doesn't change it on its own.")
            out["flagged"].append(entry)
        store.audit(RULE, out["by"], "human", o["id"], o.get("buyer"),
                    {"needs_review_id": out["id"], "order_id": o["id"], "status": status,
                     "findings": [f.message for f in findings]})


def _remembered(out: dict, old: Mandate, new: Mandate, intro, by: str, now: datetime) -> None:
    for r in remembered.list_for(new.id, now=now):
        rs = conflicts(r["product_id"], old, new)  # the product the remembered decision lets through
        if not rs:
            out["unaffected"].append({"kind": "remembered", "id": r["id"], "name": r.get("name")})
            continue
        remembered.revoke(new.id, r["id"], by, RULE)
        name = readable_name(r.get("name") or r["product_id"])
        f = _finding(intro, (f"the remembered approval for {name} is revoked: it",
                             f"muistettu hyväksyntä tuotteelle {name} on peruttu: tuote"), rs, r["product_id"])
        out["revoked"].append({"kind": "remembered", "id": r["id"], "rule": r["rule"], "product_id": r["product_id"],
                               "findings": [_fd(f)]})
    if not new.avoid_allergens:
        return
    for c in clearance.list_for(new.id):
        # a clearance never covers what was added after it (clearance._valid), so it can only mislead: revoke it
        # when the product can't be confirmed free of the new constraint (unknown data, or it declares it)
        rs = conflicts(c["product_id"], old, new)
        if not rs:
            out["unaffected"].append({"kind": "clearance", "id": c["id"], "name": c.get("name")})
            continue
        clearance.revoke(c["id"], by, RULE)
        name = readable_name(c.get("name") or c["product_id"])
        f = _finding(intro, (f"the allergen clearance for {name} is revoked: it",
                             f"allergeenivarmistus tuotteelle {name} on peruttu: tuote"), rs, c["product_id"])
        out["revoked"].append({"kind": "clearance", "id": c["id"], "product_id": c["product_id"],
                               "findings": [_fd(f)]})


def _summary(out: dict, intro, what_en: str, what_fi: str) -> tuple[str, str]:
    held, flagged, revoked, ok = (len(out[k]) for k in ("held", "flagged", "revoked", "unaffected"))
    loosened = out["change"]["loosened"]
    if not out["change"]["tightened"]:
        lo_en = "; ".join(c["en"] for c in loosened)
        lo_fi = "; ".join(c["fi"] for c in loosened)
        return (f"The mandate {lo_en}. Nothing was expanded automatically: anything held or revoked earlier stays "
                "that way until a human resumes or approves it.",
                f"Valtakirja {lo_fi}. Mitään ei laajennettu automaattisesti: aiemmin pysäytetty tai peruttu pysyy "
                "sellaisena, kunnes ihminen jatkaa tai hyväksyy sen.")
    en = (f"{intro[0]}. Re-checked everything running under mandate {out['mandate_id']}: {held} held for you, "
          f"{flagged} already-sent order{'s' if flagged != 1 else ''} to review, {revoked} remembered "
          f"decision{'s' if revoked != 1 else ''} revoked, {ok} unaffected and still running.")
    fi_ = (f"{intro[1]}. Kaikki valtakirjalla {out['mandate_id']} käynnissä oleva tarkistettiin: {held} pysäytetty "
           f"odottamaan päätöstäsi, {flagged} jo lähetettyä tilausta tarkistettavaksi, {revoked} muistettua päätöstä "
           f"peruttu, {ok} ei muutosta ja jatkuu.")
    first = next((x["findings"][0] for k in ("held", "flagged", "revoked") for x in out[k]), None)
    if first:
        en += " For example: " + first["message"]
        fi_ += " Esimerkiksi: " + (first.get("message_fi") or first["message"])
    if loosened:
        en += " Loosened rules (" + "; ".join(c["en"] for c in loosened) + ") expand nothing automatically."
        fi_ += " Lievennetyt säännöt (" + "; ".join(c["fi"] for c in loosened) + ") eivät laajenna mitään itsestään."
    return en, fi_


# --- substitutes (proposals only) ---------------------------------------------------------------------------------

def _substitute(pid: str, mandate: Mandate, when: date, amount: float, unit: str | None) -> dict | None:
    """The best-ranked compliant substitute for one product under the new mandate, or None."""
    from . import aimo, substitutes
    with aimo.background():
        r = substitutes.suggest(pid, when, amount, unit, mandate=mandate, limit=1)
    opt = (r.get("options") or [None])[0]
    if not opt:
        return None
    return {k: opt.get(k) for k in ("product_id", "name", "tier", "compromises", "total_ex_vat")}


def add_substitutes(rev: dict | None, mandate: Mandate) -> dict | None:
    """Add one proposed compliant substitute per held or flagged product (at most MAX_SUBSTITUTES lookups). A
    proposal only: nothing is quoted or ordered. Updates the stored review."""
    if not rev or not (rev["held"] or rev["flagged"] or rev["revoked"]):
        return rev
    seen, when = set(), (_now() + timedelta(days=2)).date()
    for x in rev["held"] + rev["flagged"]:
        for p in x.get("products") or []:
            pid = p["product_id"]
            if pid in seen or len(seen) >= MAX_SUBSTITUTES:
                continue
            seen.add(pid)
            try:
                sub = _substitute(pid, mandate, when, float(p.get("amount") or p.get("quantity") or 1),
                                  p.get("unit") or "SALES_UNIT")
            except Exception:  # no live data now: the hold stands, the agent can ask for substitutes later
                sub = None
            rev["substitutes"].append({"for_product_id": pid, "for": x["id"], "proposal": sub,
                                       "next_step": ("Quote it with substitute_for=" + pid + "; the mandate decides."
                                                     if sub else "No safe substitute found now; ask the agent "
                                                     "(GET /products/{id}/substitutes) later.")})
    _put(rev)
    return rev


# --- storage ------------------------------------------------------------------------------------------------------

def _put(r: dict) -> None:
    _ensure()
    store._upsert("needs_review", COLS, (r["id"], r["mandate_id"], r.get("buyer"),
                                         json.dumps(r, ensure_ascii=False), r["created_at"]))


def list_for(mandate_id: str, limit: int = 20) -> list[dict]:
    """Newest first."""
    if store._conn is None and not config.DB_URL and not config.STATE_DB.exists():
        return []
    _ensure()
    rows = store._exec("SELECT data FROM needs_review WHERE mandate_id = ? ORDER BY created_at DESC LIMIT ?",
                       (mandate_id, limit))[0]
    return [json.loads(r["data"]) for r in rows]


def previous(stored: dict | None) -> Mandate | None:
    """The mandate as stored before an update (store.get_mandate row), or None."""
    if not stored:
        return None
    try:
        return Mandate(**{k: v for k, v in stored.items() if not k.startswith("_")})
    except Exception:
        return None
