"""Country of origin: what the catalog declares, and what a mandate may demand of it.

Valio Aimo's product master carries a `countryOfOrigin` (ISO 3166 alpha-2) for almost every product (99.9 % of the
17,546). It is a **supplier-declared** field: the gateway shows it on every quote line, labelled as such, and a
mandate can restrict it (`allowed_origins`, `avoid_origins`). It is never treated as verified: where the product
master has no usable country (empty, or the placeholder codes zz / zy / xs) the origin is *unknown*, and unknown is
never treated as allowed, same rule as for allergens.

A mandate lists countries (`fi`, `se`) and/or groups: EU, EEA, NORDIC, BALTIC, EUROPE. Groups expand here, once.
"""

from __future__ import annotations

NAMES = {
    "ae": "United Arab Emirates", "ar": "Argentina", "at": "Austria", "au": "Australia", "az": "Azerbaijan",
    "bd": "Bangladesh", "be": "Belgium", "bf": "Burkina Faso", "bg": "Bulgaria", "bo": "Bolivia", "br": "Brazil",
    "ca": "Canada", "ch": "Switzerland", "ci": "Côte d'Ivoire", "cl": "Chile", "cn": "China", "co": "Colombia",
    "cr": "Costa Rica", "cu": "Cuba", "cy": "Cyprus", "cz": "Czechia", "de": "Germany", "dk": "Denmark",
    "do": "Dominican Republic", "dz": "Algeria", "ec": "Ecuador", "ee": "Estonia", "eg": "Egypt", "es": "Spain",
    "et": "Ethiopia", "fi": "Finland", "fr": "France", "gb": "United Kingdom", "gh": "Ghana", "gl": "Greenland",
    "gr": "Greece", "gt": "Guatemala", "hr": "Croatia", "ht": "Haiti", "hu": "Hungary", "id": "Indonesia",
    "ie": "Ireland", "il": "Israel", "in": "India", "ir": "Iran", "is": "Iceland", "it": "Italy", "jm": "Jamaica",
    "jp": "Japan", "ke": "Kenya", "kh": "Cambodia", "kr": "South Korea", "kz": "Kazakhstan", "la": "Laos",
    "lb": "Lebanon", "li": "Liechtenstein", "lk": "Sri Lanka", "lt": "Lithuania", "lu": "Luxembourg",
    "lv": "Latvia", "ma": "Morocco", "mg": "Madagascar", "mk": "North Macedonia", "mm": "Myanmar", "mt": "Malta",
    "mu": "Mauritius", "mw": "Malawi", "mx": "Mexico", "my": "Malaysia", "nl": "Netherlands", "no": "Norway",
    "nz": "New Zealand", "pe": "Peru", "ph": "Philippines", "pk": "Pakistan", "pl": "Poland", "pt": "Portugal",
    "py": "Paraguay", "ro": "Romania", "ru": "Russia", "sc": "Seychelles", "se": "Sweden", "sg": "Singapore",
    "si": "Slovenia", "sk": "Slovakia", "sv": "El Salvador", "th": "Thailand", "tn": "Tunisia", "tr": "Türkiye",
    "tw": "Taiwan", "ua": "Ukraine", "ug": "Uganda", "us": "United States", "vc": "Saint Vincent and the Grenadines",
    "vn": "Vietnam", "za": "South Africa",
}

# Placeholder codes in the product master: not a country.
UNSPECIFIED = {"", "zz", "zy", "xs"}

_EU = {"at", "be", "bg", "hr", "cy", "cz", "dk", "ee", "fi", "fr", "de", "gr", "hu", "ie", "it", "lv", "lt", "lu",
       "mt", "nl", "pl", "pt", "ro", "sk", "si", "es", "se"}
GROUPS = {
    "EU": _EU,
    "EEA": _EU | {"is", "li", "no"},
    "NORDIC": {"fi", "se", "dk", "no", "is"},
    "BALTIC": {"ee", "lv", "lt"},
    "EUROPE": _EU | {"is", "li", "no", "ch", "gb", "ua", "ru", "mk"},
}

BASIS = "supplier-declared field of Valio Aimo's product master; not independently verified"


def code(raw: str | None) -> str | None:
    """The product's country code, lower case, or None when the master has no usable one."""
    c = (raw or "").strip().lower()
    return None if c in UNSPECIFIED else c


def name(c: str | None) -> str:
    return NAMES.get(c or "", (c or "").upper() or "unknown")


def normalize(tokens: list[str] | None) -> list[str] | None:
    """Mandate input to stored form: country codes lower case, groups upper case; raises ValueError on anything
    that is neither a known country (one that appears in the catalog) nor a group."""
    if tokens is None:
        return None
    out: list[str] = []
    for t in tokens:
        s = str(t).strip()
        if s.upper() in GROUPS:
            s = s.upper()
        elif s.lower() in NAMES:
            s = s.lower()
        else:
            raise ValueError(f"unknown origin '{t}': use a country code (fi, se, de ...) or one of "
                             f"{', '.join(GROUPS)}")
        if s not in out:
            out.append(s)
    return out


def expand(tokens: list[str] | None) -> set[str]:
    out: set[str] = set()
    for t in tokens or []:
        out |= GROUPS.get(t, {t})
    return out


def label(tokens: list[str] | None) -> str:
    """'Finland, Sweden, EU' for messages."""
    return ", ".join(t if t in GROUPS else name(t) for t in tokens or [])


def check(raw: str | None, allowed: list[str] | None, avoided: list[str] | None) -> str:
    """'ok', 'unknown' (no usable country while a restriction is set), 'avoided' or 'outside'."""
    if not allowed and not avoided:
        return "ok"
    c = code(raw)
    if c is None:
        return "unknown"
    if c in expand(avoided):
        return "avoided"
    if allowed and c not in expand(allowed):
        return "outside"
    return "ok"


def summary(lines: list[dict]) -> dict:
    """Where an order's goods come from, by value (ex VAT) of the orderable lines: {countries: [{country, name,
    share, lines}], domestic_share, unknown_share, basis}. Domestic = Finland."""
    total = 0.0
    by: dict[str, list[float]] = {}
    for l in lines:
        if not l.get("orderable", True):
            continue
        v = l.get("line_total_ex_vat") or 0.0
        c = code(l.get("country_of_origin")) or "unknown"
        by.setdefault(c, [0.0, 0])
        by[c][0] += v
        by[c][1] += 1
        total += v
    rows = sorted(({"country": c, "name": name(c) if c != "unknown" else "unknown", "value_ex_vat": round(v, 2),
                    "share": round(v / total, 3) if total else 0.0, "lines": n} for c, (v, n) in by.items()),
                  key=lambda r: -r["value_ex_vat"])
    share = lambda c: next((r["share"] for r in rows if r["country"] == c), 0.0)
    return {"countries": rows, "domestic_share": share("fi"), "unknown_share": share("unknown"), "basis": BASIS}
