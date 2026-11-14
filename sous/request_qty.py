"""The buyer's own words against the quote: did the agent read the quantities right?

An agent can misread its buyer: Gemma read the Finnish thousands spacing in "1 539 litraa" as 539 L, which shrank
an over-limit order under the limit (docs/REDTEAM.md). The mandate can't see that: it checks what the agent asked
for, not what the buyer said. So the agent may pass `buyer_request` (the buyer's message, verbatim, at most 2,000
characters) to request_quote / place_order. It is signed into the quote like the rationale, and the gateway reads
the quantities in it deterministically (no model), in Finnish and English:

  "1 539 litraa", "1 539 l" (space, no-break or thin-space grouping), "1.539,5 kg", "1,539.5 kg", "1,5 kg",
  "40 L", "5 dl", "2 x 10 l" (20 L), "tuhat litraa", "kaksi tuhatta kiloa", "24 kpl", "3 pkt", "2 ltk" ...

Each stated quantity is compared, in its unit family (litres, kilograms, packs/pieces), with what the quote has:
item amounts, what the lines cover, line quantities and their content (through a line's density when it has one),
per item and summed. It matches when one of those is from 2 % below to config.PACK_OVERSIZE_PCT above it (pack
rounding within the oversize rule). A stated quantity with no match is finding quantity.request_mismatch (ask):
the buyer's words quoted, the number as read. It joins the decision like any rule, so it can only raise the tier.

An ambiguous number ("1,539 l" / "1.539 l": a decimal or a thousands separator?) is checked with both readings:
silent when either matches, the finding (showing both readings) when neither does.

Not a finding, but listed in mandate_preview.request_quantities:
  * product sizes (status pack_size): when the request states another quantity too, one no larger than a sales
    unit's content ("coffee milk 5 dl"), under 1 L / kg ("370 g / 283 ml") or at most half of another in its
    family ("87 kiloa ... penne 3kg", also when the agent took a substitute with another pack). "10 l cream" alone
    is still checked as the amount. Known gap: "40 l milk and 10 l cream" with the cream dropped reads 10 l as a
    size, so no finding.
"""

from __future__ import annotations

import re

from . import config

MAX_CHARS = 2000
RULE = "quantity.request_mismatch"
BELOW = 0.02  # a stated amount still matches an item down to 2 % below it (the agent rounding 1 539 to 1 530)
MAX_FINDINGS = 3
DOC = ("The buyer's own request, verbatim (their message to the agent, at most 2,000 characters). Signed into the "
       "quote; the gateway reads the quantities in it and asks a person (rule quantity.request_mismatch) when the "
       "quote has none of them, e.g. 539 L for '1 539 litraa'. Optional; never used to lower a tier.")

_SP = "[    ]"
_NUM = rf"\d{{1,3}}(?:{_SP}\d{{3}}(?!\d))+(?:[.,]\d+)?|\d+(?:[.,]\d+)*"
_WORDS = {"yksi": 1, "kaksi": 2, "kolme": 3, "neljä": 4, "viisi": 5, "kuusi": 6, "seitsemän": 7, "kahdeksan": 8,
          "yhdeksän": 9, "kymmenen": 10, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "twenty": 20, "fifty": 50, "puoli": 0.5, "half": 0.5}
_SCALES = {"tuhatta": 1000, "tuhat": 1000, "thousand": 1000, "sataa": 100, "sata": 100, "hundred": 100}
# unit word -> (family, factor to the family's unit); longest alternatives first in the pattern
_UNITS: dict[str, tuple[str, float]] = {
    **{u: ("L", 1.0) for u in ("litraa", "litroja", "litran", "litrat", "litra", "litres", "liters", "litre",
                               "liter", "ltr", "l")},
    **{u: ("L", 0.1) for u in ("desilitraa", "desilitra", "dl")},
    **{u: ("L", 0.01) for u in ("cl",)},
    **{u: ("L", 0.001) for u in ("millilitraa", "ml")},
    **{u: ("KG", 1.0) for u in ("kilogrammaa", "kilogramma", "kilograms", "kilogram", "kilos", "kiloa", "kilo",
                                "kg")},
    **{u: ("KG", 0.001) for u in ("grammaa", "gramma", "grams", "gram", "g")},
    **{u: ("PACK", 1.0) for u in ("kappaletta", "kappale", "pakkausta", "pakkauksia", "pakkaus", "kpl", "pcs", "pieces", "piece", "pkt", "pakettia",
                                  "paketti", "packages", "package", "packs", "pack", "ltk", "laatikkoa", "laatikko",
                                  "boxes", "box", "cartons", "carton", "cases", "tlk", "tölkkiä", "tölkki", "prk",
                                  "purkkia", "purkki", "pss", "pussia", "pussi", "bottles", "bottle", "pulloa",
                                  "pullo", "sales units", "sales unit")},
}
_alt = lambda words: "|".join(sorted((re.escape(w) for w in words), key=len, reverse=True))  # noqa: E731
_QTY = re.compile(
    rf"(?<![\w.,])(?:(?P<mult>\d+)\s*[x×*]\s*)?"
    rf"(?:(?P<num>{_NUM})(?:\s+(?P<nscale>{_alt(_SCALES)}))?"
    rf"|(?:(?P<wn>{_alt(_WORDS)}|a)\s+)?(?P<scale>{_alt(_SCALES)})"
    rf"|(?P<word>{_alt(_WORDS)}))"
    rf"\s*(?P<unit>{_alt(_UNITS)})(?!\w)(?:\s*[x×*]\s*(?P<mult2>\d+)(?!\w|[.,]\d))?", re.I)
FAMILY_FI = {"L": "l", "KG": "kg", "PACK": "kpl"}
FAMILY_EN = {"L": "L", "KG": "kg", "PACK": "packs"}


def clean(text: str | None) -> str | None:
    """One line (any whitespace, no-break and thin spaces too, becomes one space), at most 2,000 characters."""
    text = " ".join((text or "").split())[:MAX_CHARS]
    return text or None


def _readings(s: str) -> list[float] | None:
    """The values a written number can mean: one, or two when '1,539' / '1.539' may be a decimal or grouping."""
    s = re.sub(_SP, " ", s)
    if " " in s:  # space grouping ("1 539", "1 539,5"): unambiguous, a separator after it is the decimal one
        head, _, dec = re.sub(r"[.,]", ".", s).partition(".")
        return [float(head.replace(" ", "") + ("." + dec if dec else ""))]
    seps = re.findall(r"[.,]", s)
    if not seps:
        return [float(s)]
    parts = re.split(r"[.,]", s)
    if len(set(seps)) == 2:  # "1.539,5" / "1,539.5": the last separator is the decimal one
        last = s[max(s.rfind("."), s.rfind(","))]
        head, dec = s.rsplit(last, 1)
        if any(len(g) != 3 for g in re.split(r"[.,]", head)[1:]):
            return None
        return [float(re.sub(r"[.,]", "", head) + "." + dec)]
    if len(seps) > 1:  # "1,234,567": grouping only
        return [float("".join(parts))] if all(len(g) == 3 for g in parts[1:]) else None
    head, dec = parts
    if len(dec) == 3 and head not in ("", "0"):  # "1,539": 1.539 or 1539
        return [float(f"{head}.{dec}"), float(head + dec)]
    return [float(f"{head}.{dec}")]


def parse(text: str | None) -> list[dict]:
    """Every quantity with a unit in the buyer's words: {text, family, values (one, or two when ambiguous),
    ambiguous, packs (the count in "2 x 10 l")}."""
    out = []
    for m in _QTY.finditer(clean(text) or ""):
        unit = _UNITS[re.sub(r"\s+", " ", m["unit"].lower())]
        if m["num"]:
            vals = _readings(m["num"])
            if vals is None:
                continue
            vals = [v * _SCALES.get((m["nscale"] or "").lower(), 1) for v in vals]
        elif m["scale"]:
            vals = [_WORDS.get((m["wn"] or "").lower(), 1) * _SCALES[m["scale"].lower()]]
        else:
            vals = [_WORDS[m["word"].lower()]]
        times = m["mult"] or m["mult2"]  # "2 x 10 l" / "10 l x 2": 20 L in 2 packs
        mult = int(times) if times else 1
        vals = [round(v * mult * unit[1], 6) for v in vals]
        if not any(v > 0 for v in vals):
            continue
        out.append({"text": m.group(0).strip(), "family": unit[0], "values": vals, "ambiguous": len(vals) > 1,
                    **({"packs": mult} if times else {})})
    return out


def _family(unit: str | None) -> str:
    return unit if unit in ("L", "KG") else "PACK"


def amounts(q: dict) -> tuple[dict[str, set[float]], dict[str, set[float]], dict[str, list[float]], dict]:
    """What the quote has, per family: (amounts an order quantity can match, pack contents, item amounts, slots).
    slots[family][item index] are the amounts one item holds, slots[family]["ALL"] the whole quote's: each stated
    quantity needs a slot of its own (check), so "25 l milk and 25 l cream" can't both match one 25 L item."""
    have: dict[str, set[float]] = {"L": set(), "KG": set(), "PACK": set()}
    slots: dict[str, dict] = {"L": {}, "KG": {}, "PACK": {}}
    packs: dict[str, set[float]] = {"L": set(), "KG": set(), "PACK": set()}
    items: dict[str, list[float]] = {"L": [], "KG": [], "PACK": []}
    for i, it in enumerate(q.get("items") or []):
        if it.get("amount") is not None:
            items[_family(it.get("unit"))].append(float(it["amount"]))
            slots[_family(it.get("unit"))].setdefault(i, set()).add(round(float(it["amount"]), 6))
    sums: dict[tuple, float] = {}  # per (item, kind, family) and per (kind, family): split lines add up

    def add(fam: str, v: float | None, i: int | None, kind: str) -> None:
        if v:
            have[fam].add(round(v, 6))
            for key in ((i, kind, fam), (kind, fam)):
                sums[key] = sums.get(key, 0.0) + v

    for fam, vals in items.items():
        have[fam] |= {round(v, 6) for v in vals}
        if vals:
            have[fam].add(round(sum(vals), 6))
            slots[fam].setdefault("ALL", set()).add(round(sum(vals), 6))
    for l in q.get("lines") or []:
        i = l.get("item_index")
        if (c := l.get("covers")) and c.get("amount"):
            add(_family(c.get("unit")), c["amount"], i, "covers")
        qty = l.get("quantity")
        add("PACK", qty, i, "qty")
        cont = l.get("content_per_sales_unit") or {}
        if cont.get("amount") and cont.get("unit") in ("L", "KG"):
            fams = {cont["unit"]: cont["amount"]}
            if (d := (l.get("density") or {}).get("kg_per_l")):
                other = "KG" if cont["unit"] == "L" else "L"
                fams[other] = cont["amount"] * d if other == "KG" else cont["amount"] / d
            for fam, a in fams.items():
                packs[fam].add(round(a, 6))
                if qty:
                    add(fam, qty * a, i, "content")
    for key, v in sums.items():
        have[key[-1]].add(round(v, 6))
        slots[key[-1]].setdefault(key[0] if len(key) == 3 else "ALL", set()).add(round(v, 6))
    return have, packs, items, slots


def _near(stated: float, vals: set[float]) -> bool:
    hi = stated * (1 + config.PACK_OVERSIZE_PCT / 100) + 1e-6
    return any(stated * (1 - BELOW) - 1e-6 <= v <= hi for v in vals)


def _size_like(s: dict, stated: list[dict], packs: dict[str, set[float]]) -> bool:
    """A product's size, not an amount, when the request states another quantity too and this one is at most one
    sales unit's content ("coffee milk 5 dl", sold 10 x 5 dl per carton), under one litre / kilo ("370 g / 283 ml"),
    or at most half of another quantity in its family ("87 kiloa ... penne 3kg", the named product's size even when
    the agent took a substitute with another pack)."""
    others = [o for o in stated if o is not s]
    if not others:  # "30x125g", "4x1kg" next to an amount are sizes too
        return False
    v, fam = min(s["values"]), s["family"]
    return ((bool(packs[fam]) and v <= max(packs[fam]) * (1 + BELOW)) or (fam != "PACK" and v < 1)
            or any(o["family"] == fam and max(o["values"]) >= 2 * v for o in others))


def check(q: dict, text: str | None = None) -> list[dict]:
    """The stated quantities, each with status matched | pack_size | mismatch (an ambiguous number
    matches when either reading does)."""
    stated = parse(text if text is not None else q.get("buyer_request"))
    if not stated:
        return []
    have, packs, _, slots = amounts(q)
    hits = [any(_near(v, have[s["family"]]) for v in s["values"]) for s in stated]
    alone = {id(s) for s in stated if s.get("packs") is not None and _near(s["packs"], have["PACK"])}  # "2 x 10 l"
    got: set[int] = set(alone)  # each with a slot of its own (or sharing one whose amount is their sum)
    for fam in slots:
        fallback = {id(s) for s, h in zip(stated, hits) if h}
        got |= _assign([s for s in stated if s["family"] == fam and id(s) not in alone], slots[fam], fallback)
    taken = {id(s) for s, h in zip(stated, hits) if h} - got  # near a line, but it went to another quantity
    out = []
    for s, hit in zip(stated, hits):
        if id(s) in got:
            status = "matched"
        elif _size_like(s, stated, packs):
            status = "pack_size"
        else:
            status = "mismatch"  # an ambiguous number too, when neither reading matches
        out.append({**s, "status": status, **({"shared": True} if id(s) in taken and status == "mismatch" else {})})
    return out


MAX_ASSIGN = 60  # stated x slots above this (or over 6 stated): each quantity is only checked on its own


def _assign(cands: list[dict], slots: dict, fallback: set[int]) -> set[int]:
    """The largest set of stated quantities (ids) that each get a slot of their own: an item, or the whole quote
    ("ALL", then no item on its own), where several may share a slot when their sum is near it ("25 l milk and
    25 l cream" in one 50 L item). One-to-one, so two equal amounts need two items or one holding their sum."""
    if not cands:
        return set()
    if len(cands) * len(slots) > MAX_ASSIGN or len(cands) > 6:
        return {id(s) for s in cands} & fallback
    keys = list(slots)
    tops = {k: max(slots[k]) * (1 + BELOW) + 1e-6 for k in keys}
    best: list = [set()]

    def ok(load: dict) -> bool:
        if "ALL" in load and len(load) > 1:
            return False
        return all(_near(sum(vs), slots[k]) for k, vs in load.items())

    def walk(k: int, load: dict, got: set) -> None:
        if len(got) + len(cands) - k <= len(best[0]):
            return
        if k == len(cands):
            if ok(load):
                best[0] = set(got)
            return
        s = cands[k]
        for key in keys:
            for v in s["values"]:
                if sum(load.get(key, [])) + v > tops[key]:
                    continue
                load.setdefault(key, []).append(v)
                walk(k + 1, load, got | {id(s)})
                load[key].pop()
                if not load[key]:
                    del load[key]
                if len(best[0]) == len(cands):
                    return
        walk(k + 1, load, got)

    walk(0, {}, set())
    return best[0]


def _fmt(x: float) -> str:
    return f"{round(x, 3):,g}"


def _num_fi(x: float) -> str:
    from .fi import num
    return num(round(x, 3))


def _has(q: dict, fam: str) -> tuple[str, str]:
    """What the quote's items ask for in this family, in English and Finnish ('539 L', '539 l')."""
    _, _, items, _ = amounts(q)
    vals = items[fam]
    if not vals:
        return f"nothing in {FAMILY_EN[fam]}", f"ei mitään yksikössä {FAMILY_FI[fam]}"
    return (" + ".join(f"{_fmt(v)} {FAMILY_EN[fam]}" for v in vals),
            " + ".join(f"{_num_fi(v)} {FAMILY_FI[fam]}" for v in vals))


def findings(q: dict, texts: list[str | None] | None = None) -> list:
    """quantity.request_mismatch (ask) for each stated quantity the quote has nothing near (at most 3 per text)."""
    from .mandate import Finding
    out, seen = [], set()
    for text in texts if texts is not None else [q.get("buyer_request")]:
        n = 0
        for s in check(q, text):
            key = (s["text"], s["family"], tuple(s["values"]))
            if s["status"] != "mismatch" or key in seen or n >= MAX_FINDINGS:
                continue
            seen.add(key)
            n += 1
            fam = s["family"]
            en, fi_ = _has(q, fam)
            if s["ambiguous"]:  # "1,539 l": 1,539 L or 1.539 L, and the quote has neither
                big, small = max(s["values"]), min(s["values"])
                read_en = f"could mean {_fmt(big)} {FAMILY_EN[fam]} or {_fmt(small)} {FAMILY_EN[fam]}"
                read_fi = f"voi tarkoittaa {_num_fi(big)} {FAMILY_FI[fam]} tai {_num_fi(small)} {FAMILY_FI[fam]}"
                near_en, near_fi = "neither is near any item or line of this quote", "kumpaakaan ei vastaa mikään tarjouksen määrä"
            else:
                v = s["values"][0]
                read_en, read_fi = f"read as {_fmt(v)} {FAMILY_EN[fam]}", f"luettu: {_num_fi(v)} {FAMILY_FI[fam]}"
                near_en, near_fi = "no item or line of this quote is near it", "sitä ei vastaa mikään tarjouksen määrä"
            if s.get("shared"):  # "25 l milk and 25 l cream", one 25 L item
                near_en = ("the only item or line of this quote near it is already taken by another quantity the "
                           "buyer stated")
                near_fi = "ainoa sitä vastaava tarjouksen määrä on jo käytetty toiseen ostajan mainitsemaan määrään"
            out.append(Finding(
                rule="quantity.request_mismatch", outcome="ask",
                message=(f'The buyer wrote "{s["text"]}" ({read_en}), but {near_en} (the quote asks for {en}): '
                         "the agent may have misread the buyer, so a person checks the quantity."),
                message_fi=(f'Ostaja kirjoitti "{s["text"]}" ({read_fi}), mutta {near_fi} '
                            f"(tarjouksessa {fi_}). Agentti on voinut lukea pyynnön väärin, joten ihminen "
                            "tarkistaa määrän.")))
    return out
