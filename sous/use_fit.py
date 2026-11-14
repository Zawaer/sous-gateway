"""Does the product the agent CHOSE fit what the kitchen will use it for?

sous/use_sensitivity.py judges a *substitute* against the original. This module judges the chosen product itself:
a request for "lactose-free milk for a dessert" that lands on skimmed UHT milk, or coffee cream for whipping, is a
product that fits the words of the request but not the dish. A person should look before it is ordered.

The requirements are few, written down here and documented in docs/PROTOCOL.md 5.1.2; no model decides them:

  use (sous/use_sensitivity.py USES)  product family  doesn't fit                         fits
  whipping (whipping or a dessert)    cream           coffee cream, cooking cream, any    whipping cream (kuohukerma
                                                      cream with a known fat % under 30   / vispikerma), cream >= 30 %
  whipping (a dessert)                milk            skimmed milk (fat % 0, 'rasvaton')  milk with any fat
  cooking (a sauce, cooking)          cream           coffee cream (it can split when     cooking cream, whipping cream
                                                      it boils)
  laminated_pastry                    spread          a spread / butter blend ('levite')  butter (bakery margarine is
                                                                                          made for it, so it passes)

Anything else is not judged: coffee takes anything of the type, and a product whose spec the name doesn't reveal
(no type, no fat %) gets no finding (don't nag on unknowns). A mismatch is the finding product.use_fit, at the
mandate's `use_fit` tier (default ask; silent turns it off).
"""

from __future__ import annotations

from . import use_sensitivity as US

WHIP_MIN_FAT = 30.0
SKIMMED_MAX_FAT = 0.5

# (English, Finnish) names for what was chosen and what would fit
_COFFEE_CREAM = ("Coffee cream", "Kahvikerma")
_COOKING_CREAM = ("Cooking cream", "Ruokakerma")
_SKIMMED = ("Skimmed milk", "Rasvaton maito")
_SPREAD = ("A spread", "Levite")
_WANT_WHIP = ("whipping cream (at least 30 % fat)", "kuohukerma (vähintään 30 % rasvaa)")
_WANT_MILK = ("milk with some fat (light, semi-skimmed or whole milk)", "rasvaa sisältävä maito (kevyt-, ykkös- "
              "tai täysmaito)")
_WANT_SAUCE = ("cooking cream or whipping cream", "ruokakerma tai kuohukerma")
_WANT_BUTTER = ("butter", "voi")


def _ptype(p: dict | None) -> dict:
    return US._ptype(p)


def _verdict(use_class: str, t: dict) -> tuple[bool | None, dict | None]:
    """(fits, details) of a product type for one canonical use: True fits, False doesn't (details say why and
    what would), None: no requirement for this use and kind, or the spec is unknown."""
    key, fam, fat = t.get("key"), t.get("family"), t.get("fat_pct")
    if use_class == "whipping" and fam == "cream":
        if key == "kahvikerma":
            return False, {"chosen": _COFFEE_CREAM, "want": _WANT_WHIP,
                           "why": ("coffee cream (about 10-12 % fat) doesn't whip, so the dessert won't set",
                                   "kahvikerma (noin 10-12 % rasvaa) ei vaahtoudu, joten jälkiruoka ei jähmety")}
        if key == "ruokakerma":
            return False, {"chosen": _COOKING_CREAM, "want": _WANT_WHIP,
                           "why": ("cooking cream is too lean to whip and is made to be heated, not whipped",
                                   "ruokakerma on liian vähärasvaista vaahdotettavaksi ja tehty kuumennettavaksi")}
        if key in ("kuohukerma", "kerma"):
            if fat is not None and fat < WHIP_MIN_FAT:
                return False, {"chosen": (f"Cream with {fat:g} % fat", f"Kerma, jossa {fat:g} % rasvaa"),
                               "want": _WANT_WHIP,
                               "why": (f"cream under {WHIP_MIN_FAT:g} % fat doesn't whip to a stable foam",
                                       f"alle {WHIP_MIN_FAT:g} % kerma ei vaahtoudu kestäväksi vaahdoksi")}
            if key == "kuohukerma" or fat is not None:
                return True, None
        return None, None
    if use_class == "whipping" and fam == "milk" and key in ("maito", "maitojuoma"):
        if fat is None:
            return None, None
        if fat < SKIMMED_MAX_FAT:
            return False, {"chosen": _SKIMMED, "want": _WANT_MILK,
                           "why": ("a fat-free milk changes the texture (the dessert turns thin and less creamy)",
                                   "rasvaton maito muuttaa rakennetta (jälkiruoasta tulee vetinen ja vähemmän "
                                   "kermainen)")}
        return True, None
    if use_class == "cooking" and fam == "cream":
        if key == "kahvikerma":
            return False, {"chosen": _COFFEE_CREAM, "want": _WANT_SAUCE,
                           "why": ("coffee cream can split when it boils in a sauce",
                                   "kahvikerma voi juoksettua, kun kastike kiehuu")}
        if key in ("ruokakerma", "kuohukerma"):
            return True, None
        return None, None
    if use_class == "laminated_pastry" and fam in ("spread", "butter"):
        if key == "levite":
            return False, {"chosen": _SPREAD, "want": _WANT_BUTTER,
                           "why": ("a spread has more water and less fat than butter, so the layers don't hold",
                                   "levitteessä on enemmän vettä ja vähemmän rasvaa kuin voissa, joten kerrokset "
                                   "eivät pysy")}
        return True, None  # butter, or bakery margarine
    return None, None


def fits(product: dict | None, use: str | None) -> bool | None:
    """True when every requirement the use sets on the product is met with a known spec, False when one is not,
    None when nothing is known or nothing is required."""
    t = _ptype(product)
    seen = None
    for c in US.classify(use):
        ok, _ = _verdict(c, t)
        if ok is False:
            return False
        if ok:
            seen = True
    return seen


def _phrase(use: str | None, use_class: str, t: dict, lang: str) -> str:
    """'for a dessert' for milk in the whipping class (milk is not whipped); otherwise as use_sensitivity says."""
    if use_class == "whipping" and t.get("family") == "milk" and not US._dish(use):
        return "jälkiruokaan" if lang == "fi" else "for a dessert"
    return US.phrase(use, use_class, lang)


def check(product: dict | None, use: str | None) -> dict | None:
    """None when the product fits (or nothing is known); else the first unmet requirement as
    {use, use_class, reason, reason_fi, want, want_fi}: the reason is what a kitchen manager reads at a glance,
    'Skimmed milk for a dessert: a fat-free milk changes the texture (...)'."""
    if not product:
        return None
    t = _ptype(product)
    for c in US.classify(use):
        ok, d = _verdict(c, t)
        if ok is False and d:
            return {"use": use, "use_class": c,
                    "reason": f"{d['chosen'][0]} {_phrase(use, c, t, 'en')}: {d['why'][0]}",
                    "reason_fi": f"{d['chosen'][1]} {_phrase(use, c, t, 'fi')}: {d['why'][1]}",
                    "want": d["want"][0], "want_fi": d["want"][1]}
    return None


def suggest(chosen: dict | None, use: str | None, candidates: list[dict]) -> dict | None:
    """A product of the same search (`candidates`: product master records, in the order to prefer them) that is of
    the chosen product's family and fits the use with a known spec: {product_id, name}. No supplier call: the
    candidates are the offers the quote already has."""
    fam = _ptype(chosen).get("family")
    if not fam:
        return None
    for p in candidates:
        if p and _ptype(p).get("family") == fam and fits(p, use) is True:
            return {"product_id": p.get("id"), "name": p.get("name")}
    return None
