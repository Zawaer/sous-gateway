"""Three buyer controls on top of the mandate engine (docs/PROTOCOL.md "Buyer controls").

1. Substitute price premium cap (`max_substitute_premium_pct`): a substitute may cost at most this many percent
   more per comparable unit (L / KG / piece, Valio's comparison price) than the product it replaces. Above it, or
   when the replaced product's price can't be compared, rule substitution.premium holds the order.
2. Agent-declared doubt (`declared_risks` on request_quote / place_order): the agent's own "I'm not sure about
   this" lifts the order to at least ask (rule agent.declared_risk). It can never lower a tier.
3. Four-eyes (`separate_approver`): the human who approves a held order must not be the identity recorded as
   having requested it (order.requested_by). Refused with 403 separate_approver_required.

Kept apart from sous/mandate.py so the engine's own rules stay where they are; service._evaluate adds these
findings next to the mandate's (extra findings count like any rule; remembered / earned autonomy never touch them).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from . import agent_auth, aimo, catalog, fi
from .mandate import Finding, Mandate, readable_name

MAX_RISKS = 5
MAX_RISK_CHARS = 200

RISKS_DOC = (f"Optional, at most {MAX_RISKS} entries of at most {MAX_RISK_CHARS} characters: anything the agent "
             "itself is unsure about in this request (e.g. 'not sure 10 L covers Saturday', 'the chef asked for "
             "another brand'). Any entry holds the order for a human (rule agent.declared_risk, at least ask) with "
             "the agent's own words; it can never lower a tier. Signed into the quote and the order receipt.")

_PER = {"L": ("litre", "litralta"), "KG": ("kilo", "kilolta"), "ST": ("piece", "kappaleelta"),
        "KPL": ("piece", "kappaleelta")}


# --- 2. declared risks ---------------------------------------------------------------------------------------

def clean_risks(risks: list[str] | None) -> list[str]:
    """One line each, empty entries dropped, duplicates once, in the order given."""
    out: list[str] = []
    for r in risks or []:
        r = " ".join(str(r or "").split())[:MAX_RISK_CHARS]
        if r and r not in out:
            out.append(r)
    return out


def check_risks(risks: list[str] | None) -> list[str] | None:
    """Pydantic validator body: at most MAX_RISKS entries of at most MAX_RISK_CHARS characters (refused, not cut)."""
    if risks is None:
        return None
    if len(risks) > MAX_RISKS:
        raise ValueError(f"declared_risks: at most {MAX_RISKS} entries")
    if any(len(r) > MAX_RISK_CHARS for r in risks):
        raise ValueError(f"declared_risks: each entry at most {MAX_RISK_CHARS} characters")
    return risks


def risk_findings(risks: list[str] | None) -> list[Finding]:
    return [Finding(rule="agent.declared_risk", outcome="ask",
                    message=f"The agent itself flagged a doubt: \"{r}\" (the agent's own words, unverified). "
                            "A human decides.",
                    message_fi=f"Agentti itse ilmoitti epävarmuuden: \"{r}\" (agentin omat sanat, tarkistamatta). "
                               "Ihminen päättää.")
            for r in clean_risks(risks)]


# --- 1. substitute price premium ------------------------------------------------------------------------------

def _ref(pid: str, offer: dict | None, source: str) -> dict:
    if not offer:
        return {"product_id": pid, "comparison_price": None, "unit_price_ex_vat": None, "source": "unavailable"}
    return {"product_id": pid, "comparison_price": offer.get("comparison_price"),
            "unit_price_ex_vat": offer.get("unit_price_ex_vat"), "sales_unit": offer.get("sales_unit"),
            "source": source}


def annotate_replaced(mandate: Mandate | None, lines: list[dict], known: dict[str, dict], day: date) -> None:
    """Give every substitute line `replaced_price`: the replaced product's price per comparison unit, from this
    quote's own offers when it was a candidate ('quote'), else one batched price/availability lookup ('looked up'),
    else unavailable. Only when the mandate caps the premium, so other quotes keep their shape. Signed with the
    quote, so placing and approving it decide on the same numbers."""
    if mandate is None or mandate.max_substitute_premium_pct is None:
        return
    subs = [l for l in lines if l.get("substitute_for") and "replaced_price" not in l]
    if not subs:
        return
    need = sorted({l["substitute_for"] for l in subs if l["substitute_for"] not in known})
    looked: dict[str, dict] = {}
    if need:
        from . import quote as Q
        try:
            looked = {o["product_id"]: o for o in Q.offers(need, day)[0]}
        except aimo.BudgetLimit:  # our own per-minute budget: a retryable wait, like any quote call
            raise
        except aimo.AimoError:
            looked = {}
    for l in subs:
        orig = l["substitute_for"]
        l["replaced_price"] = (_ref(orig, known[orig], "quote") if orig in known
                               else _ref(orig, looked.get(orig), "looked up"))


def premium_findings(mandate: Mandate, lines: list[dict]) -> list[Finding]:
    cap = mandate.max_substitute_premium_pct
    if cap is None:
        return []
    out = []
    for l in lines:
        orig = l.get("substitute_for")
        if not orig or not l.get("orderable", True):
            continue
        pid = l["product_id"]
        name = readable_name(l.get("name") or pid)
        orig_name = readable_name((catalog.get(orig) or {}).get("name") or orig)
        ref = l.get("replaced_price") or {}
        now, was = l.get("comparison_price") or {}, ref.get("comparison_price") or {}
        if now.get("ex_vat") is None or not was.get("ex_vat") or now.get("per") != was.get("per"):
            why = ("its price could not be looked up" if not ref or ref.get("source") == "unavailable" or not was
                   else "the two are priced per different units"
                   if now.get("per") and was.get("per") and now.get("per") != was.get("per")
                   else "a comparison price is missing")
            why_fi = ("sen hintaa ei saatu" if not ref or ref.get("source") == "unavailable" or not was
                      else "tuotteilla on eri vertailuyksikkö"
                      if now.get("per") and was.get("per") and now.get("per") != was.get("per")
                      else "vertailuhinta puuttuu")
            out.append(Finding(
                rule="substitution.premium", outcome="ask", product_id=pid,
                message=f"{name} replaces {orig_name}, but {why}, so the mandate's {cap:g} % cap on a substitute's "
                        "extra cost can't be checked. A human decides.",
                message_fi=f"{name} korvaa tuotteen {orig_name}, mutta {why_fi}, joten valtakirjan {fi.pct(cap)} "
                           "katto korvaavan tuotteen lisähinnalle ei ole tarkistettavissa. Ihminen päättää."))
            continue
        n, w = Decimal(str(now["ex_vat"])), Decimal(str(was["ex_vat"]))
        if (n - w) * 100 <= w * Decimal(str(cap)):  # exactly the cap passes; cheaper always passes
            continue
        up = float((n - w) * 100 / w)
        per = now["per"]
        en, fin = _PER.get(per, (per, f"/{fi.unit(per)}"))
        out.append(Finding(
            rule="substitution.premium", outcome=mandate.substitute_premium, product_id=pid,
            message=f"Substitute {name} costs {up:.0f} % more per {en} than {orig_name}, the product it replaces "
                    f"(€{n:.2f}/{per} vs €{w:.2f}/{per}; cap {cap:g} %).",
            message_fi=f"Korvaava tuote {name} maksaa {fi.pct(round(up))} enemmän {fin} kuin korvattava tuote "
                       f"{orig_name} ({fi.eur(float(n))}/{fi.unit(per)} vs {fi.eur(float(w))}/{fi.unit(per)}; "
                       f"katto {fi.pct(cap)})."))
    return out


def findings(mandate: Mandate, q: dict) -> list[Finding]:
    """This module's findings for a quote (or the order being placed / approved from it)."""
    return premium_findings(mandate, q.get("lines") or []) + risk_findings(q.get("declared_risks"))


# --- 3. four-eyes ---------------------------------------------------------------------------------------------

def _norm(name: str | None) -> str:
    return " ".join((name or "").split()).casefold()


def requested_by(mandate: Mandate) -> dict:
    """Who the order is recorded as requested by: the human who minted the agent's current credential (the person
    who set this agent to work), else the mandate's issuer. An identity string, not a verified person."""
    row = agent_auth._row(mandate.id)
    if row and row.get("created_by"):
        return {"identity": row["created_by"], "source": "agent_credential",
                "credential_last4": row.get("last4")}
    return {"identity": mandate.issued_by, "source": "mandate_issuer"}


def check_separate_approver(mandate: Mandate, order: dict, approver: str | None) -> None:
    """Raise GatewayError 403 separate_approver_required when the mandate wants four eyes and the approver is (by
    name, case and spacing ignored) the identity the order was requested by, or gives no name at all."""
    if not mandate.separate_approver:
        return
    from .service import GatewayError
    req = (order.get("requested_by") or requested_by(mandate)).get("identity")
    if not _norm(approver):
        raise GatewayError("separate_approver_required", f"Mandate {mandate.id} needs an approver other than the "
                           "requester: give the approver's name.", 403)
    if _norm(approver) == _norm(req):
        raise GatewayError("separate_approver_required",
                           f"Mandate {mandate.id} requires a separate approver: {order.get('kind', 'order')} {order.get('id')} was "
                           f"requested by '{req}', who can't also approve it. Another approver must approve it "
                           "(rejecting it is still allowed).", 403)


def check_approver_role(mandate: Mandate, order: dict, approver: str | None) -> None:
    """Role routing (mandate.approvers_for): a held order whose findings match a key may only be approved by one of
    that key's names (e.g. allergen holds only by the dietitian). Raise GatewayError 403 approver_not_authorized."""
    routes = getattr(mandate, "approvers_for", None) or {}
    if not routes:
        return
    from .service import GatewayError
    fired = [f.get("rule", "") for f in (order.get("decision") or {}).get("findings") or []
             if f.get("outcome") in ("ask", "block")]
    for key, names in routes.items():
        hit = [r for r in fired if r == key or (key.endswith(".") and r.startswith(key))]
        if hit and _norm(approver) not in {_norm(n) for n in names}:
            raise GatewayError("approver_not_authorized",
                               f"{order.get('id')} was held for {', '.join(sorted(set(hit)))}; mandate {mandate.id} lets "
                               f"only {', '.join(names)} approve that (approver given: '{approver or ''}'). They "
                               "must approve it; rejecting is still allowed.", 403)

