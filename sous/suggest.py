"""Valio Aimo's own selling agent: proactive suggestions to a buyer's agent, with guardrails.

The other side of the conversation. Valio Aimo spots something the buyer may want and offers it:

  alternative: a better deal on something the buyer already orders (same category, measure and
               temperature, a similar name): cheaper per L/KG, delivered in full more often, a
               certification label (Hyvää Suomesta, MSC, organic...) or a lower CO2 estimate, at a
               small premium at most.
  seasonal:    a seasonal product for the delivery date, found by name keywords (Christmas: joulu,
               glögi, rosolli, juhlakinkku...). The dataset has no launch or season dates, so this is
               a documented proxy.
  new:         a recently (re)listed product in a category the buyer orders from: its Synkka product
               record was updated in the last 30 days of the dataset. Synkka has no launch date, so
               this too is a proxy, and every suggestion says so.

Guardrails, all deterministic (no LLM):
  * A suggestion is an offer, never an order: nothing here creates a quote or an order.
  * Everything is checked against the buyer's signed mandate first. Anything the mandate would block
    (allergens contained or possibly contained, missing required claims, disallowed category, over a
    comparison-price cap) or that can't be delivered is never shown. Products with unknown allergen
    data are not pushed to a buyer with allergen rules.
  * Each suggestion carries mandate_check: the tier (silent / notify / ask) it would get if ordered at
    the buyer's usual quantity, from the same mandate engine that decides real orders.
  * Each is labelled sponsored_by "Valio Aimo" with a disclosure: it is the seller's commercial
    suggestion, not neutral advice.
  * Every batch is written to the audit log (suggestions.offered), so both sides can see what was pushed.

Live price and availability: one price call and one availability call for all candidates together.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import date, timedelta

from . import access, aimo, catalog, config, quote as Q, sandbox, service as S, store
from .mandate import Mandate, evaluate

SPONSOR = "Valio Aimo"
DISCLOSURE = ("Commercial suggestion from Valio Aimo, the seller: not neutral advice. It is an offer only; "
              "nothing is quoted or ordered unless your agent asks for a quote and places an order under "
              "your mandate.")
ACTOR = "valio-aimo-suggestions"

MAX_ORIGINALS = 6  # products from the buyer's history to look for better deals on
ALTS_PER_ORIGINAL = 4
MAX_SEASONAL = 16
MAX_NEW = 4
MAX_LIVE = 50  # products priced live, in one price + one availability call
ORDERS_CONSIDERED = 10
SMALL_PREMIUM = 0.10  # a label / reliability / CO2 gain may cost at most this much more per L/KG
MIN_SAVING = 0.02
NEW_WINDOW_DAYS = 30

LABEL_NAMES = {
    "GOODS_FROM_FINLAND_BLUE_SWAN": "Hyvää Suomesta",
    "MADE_IN_FINLAND_FLAG_WITH_KEY": "Avainlippu (made in Finland)",
    "FINNISH_HEART_SYMBOL": "Sydänmerkki (heart symbol)",
    "EU_ORGANIC_FARMING": "EU organic",
    "MARINE_STEWARDSHIP_COUNCIL_LABEL": "MSC",
    "AQUACULTURE_STEWARDSHIP_COUNCIL": "ASC",
    "FAIR_TRADE_MARK": "Fairtrade",
    "RAINFOREST_ALLIANCE": "Rainforest Alliance",
    "KRAV_MARK": "KRAV organic",
    "OFFICIAL_ECO_LABEL_SUN": "Finnish organic (Luomu sun)",
    "SOIL_ASSOCIATION_ORGANIC_SYMBOL": "Soil Association organic",
    "PUHTAASTI_KOTIMAINEN": "Puhtaasti kotimainen",
}
DIETARY_CLAIMS = {"FREE_FROM_LACTOSE", "FREE_FROM_GLUTEN", "VEGAN", "VEGETARIAN", "LOW_LACTOSE",
                  "FREE_FROM_MILK", "FREE_FROM_EGGS", "FREE_FROM_NUTS"}

# (name, first day (month, day), last day (month, day), FTS terms, confirming regex)
SEASONS = [
    ("Christmas", (11, 1), (12, 26),
     ["joulu", "christmas", "glögi", "glögg", "glogg", "piparkak", "pepparkak", "gingerbread", "rosolli",
      "rosoll", "juhlakinkku", "julskinka", "laatikko", "lipeäkala", "lutfisk", "lutefish"],
     r"joulu|christmas|glögi|glögg|glogg|piparkak|pepparkak|gingerbread|rosoll|juhlakinkku|julskinka|"
     r"lanttulaatikko|porkkanalaatikko|perunalaatikko|lipeäkala|lutfisk|lutefish"),
    ("autumn / Halloween", (9, 15), (11, 5), ["kurpitsa", "pumpkin", "halloween"],
     r"(?<!kesä)kurpitsa|pumpkin|halloween"),
    ("Easter", (3, 1), (4, 25), ["pääsiäi", "påsk", "easter", "mämmi", "memma"],
     r"pääsiäi|påsk|easter|mämmi|memma"),
]

DEFINITIONS = {
    "seasonal": "Name contains a seasonal keyword for the delivery date (e.g. Christmas 1.11-26.12: joulu, "
                "christmas, glögi, piparkakku, rosolli, juhlakinkku, laatikko). The dataset has no season "
                "or launch dates, so this keyword rule is our assumption.",
    "new": f"The product's Synkka record was updated within the last {NEW_WINDOW_DAYS} days of the dataset "
           "(synkkaData.lastUpdated). Synkka has no launch date, so this is a proxy for 'new or relaunched'.",
    "co2": "CO2 is Valio Aimo's product-group average (kg CO2e per kg), an estimate, not a product measurement.",
    "usual_quantity": "The quantity of the product in the buyer's most recent order; 1 sales unit when there "
                      "is no history.",
}


# --- static data ------------------------------------------------------------

def _in_category(category: str) -> list[dict]:
    rows = catalog._db().execute(
        "SELECT data FROM product WHERE json_extract(data, '$.category') = ?", (category,)).fetchall()
    return [json.loads(d) for (d,) in rows]


def _name_matches(terms: list[str]) -> list[dict]:
    q = "{name_fi name_en name_sv} : (" + " OR ".join(f'"{t}"' for t in terms) + ")"
    rows = catalog._db().execute(
        "SELECT p.data FROM product_fts f JOIN product p ON p.id = f.id WHERE product_fts MATCH ? LIMIT 3000",
        (q,)).fetchall()
    return [json.loads(d) for (d,) in rows]


DATES_FILE = config.DATA_DIR / "product_dates.json"
_dates_cache: dict | None = None
_dates_lock = threading.Lock()


def build_dates_index(path=None) -> int:
    """Side index {product id: synkka lastUpdated} from products.json (not in the compact catalog)."""
    path = path or DATES_FILE
    products = json.load(open(config.PRODUCTS_JSON))
    out = {p["id"]: (p.get("synkkaData") or {}).get("lastUpdated") for p in products}
    out = {k: v for k, v in out.items() if v}
    path.write_text(json.dumps(out))
    return len(out)


def _dates() -> dict[str, str]:
    """Product id -> Synkka lastUpdated. Built on first use when products.json is there; {} otherwise."""
    global _dates_cache
    with _dates_lock:
        if _dates_cache is None:
            try:
                if not DATES_FILE.exists() and config.PRODUCTS_JSON.exists():
                    build_dates_index()
                _dates_cache = json.loads(DATES_FILE.read_text()) if DATES_FILE.exists() else {}
            except (OSError, ValueError):
                _dates_cache = {}
        return _dates_cache


def _new_ids() -> set[str]:
    d = _dates()
    if not d:
        return set()
    parsed = {k: Q._dt(v) for k, v in d.items()}
    newest = max(parsed.values())
    return {k for k, v in parsed.items() if v >= newest - timedelta(days=NEW_WINDOW_DAYS)}


# --- helpers ----------------------------------------------------------------

_WORD = re.compile(r"[a-zåäö]{3,}")
_STOP = {"the", "and", "with", "ltd", "pack", "pcs", "uht"}


def _words(p: dict) -> set[str]:
    brand = (p.get("brand") or "").lower()
    text = " ".join(v for v in p["names"].values() if v).lower()
    return {w for w in _WORD.findall(text) if w not in _STOP and w not in brand}


def _sim(a: dict, b: dict) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / max(1, len(wa | wb))


def _static_block(p: dict, m: Mandate) -> str | None:
    """Why the mandate rules this product out before any live call, or None."""
    if p.get("deleted"):
        return "deleted"
    if m.allowed_categories is not None and p.get("category") not in m.allowed_categories:
        return "category"
    if not set(m.required_claims) <= set(p.get("claims") or []):
        return "claims"
    if m.avoid_allergens:
        st = catalog.allergen_status(p, m.avoid_allergens, m.allow_may_contain)["status"]
        if st in ("contains", "may_contain"):
            return "allergen"
        if st == "unknown":
            return "unknown_allergens"
    return None


def _season(d: date) -> list[tuple]:
    md = (d.month, d.day)
    return [s for s in SEASONS if s[1] <= md <= s[2]]


def _label(code: str) -> str:
    return LABEL_NAMES.get(code, code.replace("_", " ").title())


def _pct(x: float) -> str:
    return f"{abs(x) * 100:.1f}%"


def _allergen_phrase(alt: dict, orig: dict) -> str | None:
    a, o = set(alt["allergens"]["contains"]), set(orig["allergens"]["contains"])
    if not alt["allergens"]["declared"] or not orig["allergens"]["declared"]:
        return None
    if a == o:
        return "same allergens"
    if a < o:
        return "declares fewer allergens (no " + ", ".join(catalog.ALLERGENS.get(c, c) for c in sorted(o - a)) + ")"
    return None


# --- the engine -------------------------------------------------------------

def suggestions(mandate_id: str, agent_id: str, delivery_date: date, basis: list[str] | None = None,
                limit: int = 5, agent_token: str | None = None, reader: access.Reader | None = None) -> dict:
    access.check_live(reader, mandate_id)  # first: no existence oracle (sous/access.py)
    m = S._mandate(mandate_id)
    if not access.skips_agent_check(reader, m):
        sandbox.authorize(m, agent_id, agent_token, "get_suggestions")
    if agent_id != m.agent_id:
        raise S.GatewayError("agent_mismatch", f"Agent '{agent_id}' is not the holder of mandate {m.id}.", 403)
    limit = max(1, min(limit, 10))
    now = S._now()
    out = {
        "mandate_id": m.id, "buyer": m.buyer, "agent_id": agent_id, "delivery_date": delivery_date.isoformat(),
        "sponsored_by": SPONSOR, "disclosure": DISCLOSURE, "suggestions": [],
    }
    if m.revoked or (m.valid_until and now > m.valid_until):
        out["note"] = f"Mandate {m.id} is not in force, so Valio Aimo suggests nothing."
        _audit(m, agent_id, delivery_date, out, {})
        return out

    # 1. what the buyer usually orders: newest order first, usual quantity = the latest one
    orders = [o for o in store.list_orders(mandate_id=m.id) if o["status"] in ("ordered", "pending_approval")]
    orders = orders[:ORDERS_CONSIDERED]
    usual: dict[str, dict] = {}
    for o in orders:
        for l in o["lines"]:
            if l.get("product_id") and l.get("quantity") and l["product_id"] not in usual:
                usual[l["product_id"]] = {"quantity": l["quantity"], "order_id": o["id"],
                                          "ordered_at": o["created_at"][:10]}
    originals: list[dict] = []
    for pid in list(basis or []) + list(usual):
        p = catalog.get(pid)
        if p and pid not in {x["id"] for x in originals}:
            originals.append(p)
    originals = originals[:MAX_ORIGINALS]
    known = {p["id"] for p in originals} | set(usual)
    excluded: dict[str, int] = {}

    def skip(reason: str):
        excluded[reason] = excluded.get(reason, 0) + 1

    # 2. static candidates (no live calls yet)
    alt_for: dict[str, str] = {}  # candidate id -> original id
    cands: dict[str, dict] = {}
    for orig in originals:
        measure = (orig.get("size_from_name") or [None, None])[1]
        keep = set(orig["claims"]) & DIETARY_CLAIMS
        pool = []
        for c in _in_category(orig["category"]):
            if c["id"] in known or c["id"] in cands:
                continue
            if orig["temperature"] and c["temperature"] and c["temperature"] != orig["temperature"]:
                continue
            if measure and c.get("size_from_name") and c["size_from_name"][1] != measure:
                continue
            if not keep <= set(c["claims"]):
                continue
            if orig["allergens"]["declared"] and not set(c["allergens"]["contains"]) <= set(orig["allergens"]["contains"]):
                continue  # never adds an allergen the buyer's usual product doesn't have
            sim = _sim(orig, c)
            if sim < 0.25 and c["id"] not in orig.get("official_substitutes", []):
                continue  # not the same kind of product
            if (why := _static_block(c, m)):
                skip(why)
                continue
            gain = len((set(c["labels"]) - set(orig["labels"])) & set(LABEL_NAMES))
            pool.append((3 * sim + gain + (0.5 if c["id"] in orig.get("official_substitutes", []) else 0), c))
        pool.sort(key=lambda x: -x[0])
        for _, c in pool[:ALTS_PER_ORIGINAL]:
            cands[c["id"]] = c
            alt_for[c["id"]] = orig["id"]

    history_prefixes = {p["category"][:2] for p in originals}
    seasons = _season(delivery_date)
    seasonal: dict[str, tuple[str, str]] = {}  # id -> (season, keyword)
    for name, _, _, terms, pattern in seasons:
        rx = re.compile(pattern, re.IGNORECASE)
        ranked = []
        for c in _name_matches(terms):
            if c["id"] in known or c["id"] in cands or not catalog.is_food(c):
                continue
            hit = rx.search(" ".join(v for v in c["names"].values() if v))
            if not hit:
                continue
            if (why := _static_block(c, m)):
                skip(why)
                continue
            score = (2 if c["category"][:2] in history_prefixes else 0) + \
                (0.5 if set(c["labels"]) & set(LABEL_NAMES) else 0) + \
                (0.5 if (c.get("delivery_reliability") or 0) >= 0.95 else 0) + (0.3 if c["allergens"]["declared"] else 0)
            ranked.append((score, hit.group(0).lower(), c))
        ranked.sort(key=lambda x: (-x[0], x[2]["id"]))
        per_kw: dict[str, int] = {}
        for _, kw, c in ranked:
            if len(seasonal) >= MAX_SEASONAL:
                break
            if per_kw.get(kw, 0) >= 2:
                continue  # variety: not five hams
            per_kw[kw] = per_kw.get(kw, 0) + 1
            cands[c["id"]] = c
            seasonal[c["id"]] = (name, kw)

    new: set[str] = set()
    if originals:
        cats = {p["category"] for p in originals}
        for pid in sorted(_new_ids()):
            if len(new) >= MAX_NEW:
                break
            if pid in known or pid in cands:
                continue
            c = catalog.get(pid)
            if not c or c["category"] not in cats:
                continue
            if (why := _static_block(c, m)):
                skip(why)
                continue
            cands[pid] = c
            new.add(pid)

    # 3. live price + availability: originals and candidates together, one call each
    ids = ([p["id"] for p in originals] + list(cands))[:MAX_LIVE]
    before = dict(aimo.stats)
    try:
        with sandbox.metered(m):
            prices = aimo.prices(ids, delivery_date) if ids else {}
            avail = aimo.availability(ids, delivery_date) if ids else {}
    except aimo.AimoError as e:
        raise S.valio_error(e)
    now = S._now()

    orig_offer: dict[str, dict] = {}
    for p in originals:
        q = (usual.get(p["id"]) or {}).get("quantity") or 1
        orig_offer[p["id"]] = Q.build_offer(p, prices.get(p["id"]), avail.get(p["id"]), delivery_date, q, None, now)

    spent = store.spent_today(m.id, now.date().isoformat())
    picked: list[dict] = []
    for cid in ids:
        if cid not in cands:
            continue
        c = cands[cid]
        oid = alt_for.get(cid)
        orig = next((p for p in originals if p["id"] == oid), None) if oid else None
        oo = orig_offer.get(oid) if oid else None
        need, unit, qty_basis = 1, None, "1 sales unit (a product you have not ordered before)"
        if orig and oo:
            content = oo.get("content_per_sales_unit")
            q_orig = oo.get("quantity") or 1
            if content:
                need, unit = q_orig * content["amount"], content["unit"]
            elif c["sales_unit"] == orig["sales_unit"]:
                need = q_orig
            u = usual.get(oid)
            qty_basis = (f"your usual {q_orig:g} {orig['sales_unit']} of {orig['name']}"
                         + (f" (order {u['order_id']}, {u['ordered_at']})" if u else " (from basis)"))
        offer = Q.build_offer(c, prices.get(cid), avail.get(cid), delivery_date, need, unit, now)
        if not offer["orderable"] or offer["quantity"] is None:
            skip("not_orderable")
            continue

        reasons: list[str] = []
        kind, score = None, 0.0
        if orig and oo:
            cp_a = (offer.get("comparison_price") or {}).get("ex_vat")
            cp_o = (oo.get("comparison_price") or {}).get("ex_vat")
            per = (offer.get("comparison_price") or {}).get("per")
            if not (cp_a and cp_o) or per != (oo.get("comparison_price") or {}).get("per"):
                skip("not_comparable")
                continue
            d = (cp_a - cp_o) / cp_o
            gained = sorted((set(c["labels"]) - set(orig["labels"])) & set(LABEL_NAMES))
            ra, ro = c.get("delivery_reliability"), orig.get("delivery_reliability")
            rel_gain = (ra - ro) if (ra is not None and ro is not None) else 0.0
            ca, co = c.get("co2_kg_per_kg_group_avg"), orig.get("co2_kg_per_kg_group_avg")
            co2_drop = (co - ca) / co if (ca is not None and co) else 0.0
            if d <= -MIN_SAVING:
                kind, score = "cheaper_alternative", -d * 100
                reasons.append(f"{_pct(d)} cheaper per {per} (€{cp_a:.2f} vs €{cp_o:.2f} ex VAT)")
                saving = (cp_o - cp_a) * (offer["covers"] or {}).get("amount", 0) if offer.get("covers") else None
                if saving:
                    reasons.append(f"saves about €{saving:.2f} on {offer['covers']['amount']:g} {per}")
            elif d <= SMALL_PREMIUM and (gained or rel_gain >= 0.05 or co2_drop >= 0.10):
                price_txt = (f"{_pct(d)} more per {per}" if d > 0.0005 else f"same price per {per}" if d > -0.0005
                             else f"{_pct(d)} cheaper per {per}") + f" (€{cp_a:.2f} vs €{cp_o:.2f})"
                reasons.append(price_txt)
            else:
                skip("no_advantage")
                continue
            if gained:
                kind = kind or "certified_alternative"
                score += 3 * len(gained)
                reasons.append(" and ".join(f"'{_label(g)}' label" for g in gained))
            if rel_gain >= 0.05:
                kind = kind or "more_reliable_alternative"
                score += rel_gain * 50
                reasons.append(f"delivered in full {ra:.0%} of the time vs {ro:.0%}")
            if co2_drop >= 0.10:
                kind = kind or "lower_co2_alternative"
                score += co2_drop * 10
                reasons.append(f"lower CO2 estimate {ca:.2f} vs {co:.2f} kg CO2e/kg (product-group average, "
                               "an estimate)")
            if (al := _allergen_phrase(c, orig)):
                reasons.append(al)
            reason = f"Instead of {orig['name']}: " + "; ".join(reasons) + "."
        elif cid in seasonal:
            season, kw = seasonal[cid]
            kind, score = "seasonal", 4 + (1 if c["category"][:2] in history_prefixes else 0)
            reasons.append(f"seasonal for {season} (name contains '{kw}'; keyword rule, an assumption)")
            reasons.append(f"€{offer['unit_price_ex_vat']:.2f} ex VAT per {offer['sales_unit']}, delivery "
                           f"{offer['delivery_date']}")
            if offer.get("comparison_price") and offer["comparison_price"].get("ex_vat"):
                reasons.append(f"€{offer['comparison_price']['ex_vat']:.2f}/{offer['comparison_price']['per']}")
            gained = sorted(set(c["labels"]) & set(LABEL_NAMES))
            if gained:
                reasons.append(" and ".join(f"'{_label(g)}' label" for g in gained))
            reason = "New for the season: " + "; ".join(reasons) + "."
        else:
            kind, score = "new", 3
            reasons.append(f"Synkka record updated {_dates().get(cid, '?')[:10]} (proxy for new; no launch date "
                           "in the data)")
            reasons.append(f"in category {c['category']}, which you already order from")
            reasons.append(f"€{offer['unit_price_ex_vat']:.2f} ex VAT per {offer['sales_unit']}")
            reason = "New or updated listing: " + "; ".join(reasons) + "."

        # the mandate engine, in preview: what would happen if the buyer ordered this at the usual quantity
        outcome, findings = evaluate(m, agent_id, [offer], requested_date=delivery_date,
                                     substitute_for={cid: oid} if oid else None, spent_today_eur=spent, now=now)
        if outcome == "block":
            skip("mandate_block")
            continue
        if any(f.rule == "price.comparison_cap" for f in findings):
            skip("price_cap")
            continue
        picked.append({
            "kind": kind, "score": round(score, 2),
            "sponsored_by": SPONSOR, "disclosure": DISCLOSURE,
            "product": catalog.card(c),
            "instead_of": {"product_id": orig["id"], "name": orig["name"],
                           "comparison_price": oo.get("comparison_price")} if orig and oo else None,
            "reason": reason,
            "reasons": reasons,
            "usual_quantity": {"quantity": offer["quantity"], "sales_unit": offer["sales_unit"], "basis": qty_basis},
            "offer": offer,
            "mandate_check": {
                "outcome": outcome,
                "findings": [f.model_dump() for f in findings],
                "note": "Preview only, at the usual quantity: no quote or order was created. The mandate decides "
                        "again on the real quote.",
            },
            "how_to_accept": (f"request_quote with product_ids ['{cid}']"
                              + (f" and substitute_for '{oid}'" if oid else "")
                              + ", show the buyer the quote, then place_order if they agree."),
        })

    # 4. variety: alternatives and seasonal first, then the rest by score
    by_kind: dict[str, list[dict]] = {}
    for s in sorted(picked, key=lambda s: -s["score"]):
        by_kind.setdefault("seasonal" if s["kind"] == "seasonal" else "new" if s["kind"] == "new" else "alt",
                           []).append(s)
    chosen: list[dict] = []
    for k in ["alt", "seasonal", "alt", "new", "seasonal", "alt", "alt", "seasonal", "new", "alt"]:
        if len(chosen) >= limit:
            break
        if by_kind.get(k):
            chosen.append(by_kind[k].pop(0))
    rest = sorted((s for v in by_kind.values() for s in v), key=lambda s: -s["score"])
    chosen += rest[:limit - len(chosen)]
    for i, s in enumerate(chosen, 1):
        s["id"] = f"S{i}"

    out["suggestions"] = chosen
    out["basis"] = {"orders_considered": len(orders), "products": [p["id"] for p in originals],
                    "seasons": [s[0] for s in seasons]}
    out["considered"] = {"candidates": len(cands), "priced_live": len(ids), "excluded": excluded}
    out["definitions"] = DEFINITIONS
    out["valio_api_calls"] = {k: aimo.stats[k] - before[k] for k in aimo.stats}
    if not chosen:
        out["note"] = "Nothing to suggest that fits this mandate and can be delivered on that date."
    _audit(m, agent_id, delivery_date, out, out["valio_api_calls"])
    return out


def _audit(m: Mandate, agent_id: str, delivery_date: date, out: dict, calls: dict) -> None:
    store.audit("suggestions.offered", ACTOR, "system", m.id, m.buyer, {
        "agent_id": agent_id, "delivery_date": delivery_date.isoformat(), "sponsored_by": SPONSOR,
        "suggestions": [{"product_id": s["product"]["id"], "kind": s["kind"],
                         "instead_of": (s["instead_of"] or {}).get("product_id"),
                         "mandate_outcome": s["mandate_check"]["outcome"], "reason": s["reason"]}
                        for s in out["suggestions"]],
        "valio_api_calls": calls,
    })


if __name__ == "__main__":
    print("indexed", build_dates_index(), "products ->", DATES_FILE)
