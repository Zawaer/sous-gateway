"""Use-aware substitution: whether a substitute is acceptable depends on what the kitchen uses the product for.

Two cartons of whipping cream from two brands can look identical and still differ in ways that matter: the
stabilisers (lecithin, carrageenan) change how the cream whips and how long the foam holds, so swapping brands can
break a dessert. In coffee the brand barely matters. The same holds for butter in laminated pastry (water content,
how pliable it stays cold) versus butter in a sauce, or milk served to drink versus milk in a béchamel.

So a substitute is judged against the product's *use*, at one of three levels:

  exact      the same product only: any substitute (a brand change counts) needs a person, even when the
             mandate lets substitutes through on their own (rule substitution.use_sensitive, ask)
  same_spec  the same kind of product with the same fat % and the original's claims; any brand will do
  type       the same kind (type family) of product will do

The table below is small and explicit on purpose (no model decides it). A product type or use that is not in the
table keeps the gateway's ordinary substitution behaviour (rule substitution, set by the mandate). Valio Aimo or
the buyer can override any cell per mandate with `substitution_by_use` ({type key | family | category | "*":
{use: level}}), which is signed with the mandate like every other field.

Where the use comes from: QuoteItem.use (free text or the vocabulary in USES), the recipe planner (each ingredient
line knows its dish: 'ingredient-in-recipe:Tiramisu'), or the buyer's history (a standing order or a past order of
the same product that stated a use). Otherwise it is unknown and nothing changes.
"""

from __future__ import annotations

import re
import unicodedata

Level = str  # "exact" | "same_spec" | "type"
LEVELS = ("type", "same_spec", "exact")  # loosest first
_RANK = {lv: i for i, lv in enumerate(LEVELS)}
RECIPE_PREFIX = "ingredient-in-recipe:"

# canonical use -> (English phrase, Finnish phrase, keywords in free text / dish names, en + fi)
USES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "coffee": ("for coffee", "kahviin", ("coffee", "kahvi", "latte", "cappuccino", "espresso")),
    "whipping": ("for whipping or a dessert", "vaahdotukseen tai jälkiruokaan",
                 ("whip", "dessert", "cake", "mousse", "tiramisu", "panna cotta", "pannacotta", "pavlova", "trifle",
                  "cheesecake", "vaahdo", "vatkat", "jälkiruoka", "jalkiruoka", "kakku", "kermavaahto", "rahkavaahto",
                  "jälkkäri", "jalkkari", "torttu")),
    "laminated_pastry": ("for laminated pastry", "voitaikinaan tai kerrostaikinaan",
                         ("laminated", "croissant", "puff pastry", "danish", "voitaikina", "lehtitaikina",
                          "wienerleipä", "wienerleipa", "kerrostaikina")),
    "baking": ("for baking", "leivontaan",
               ("bak", "bread", "bun", "cookie", "biscuit", "muffin", "leivo", "leivon", "pulla", "leipä",
                "leipa", "keksi", "piirakka", "piiras", "taikina")),
    "melting": ("for melting (gratin, pizza)", "sulatettavaksi (gratiinit, pizza)",
                ("melt", "gratin", "pizza", "toast", "lasagn", "au gratin", "gratiini", "gratinoi", "sulat",
                 "uunijuusto")),
    "cooking": ("for cooking or a sauce", "ruoanlaittoon tai kastikkeeseen",
                ("cook", "sauce", "soup", "stew", "casserole", "curry", "risotto", "pasta", "béchamel", "bechamel",
                 "kastike", "keitto", "ruoanlaitto", "ruoanvalmistus", "pata", "paisti", "laatikko", "muhennos",
                 "lasagn")),
    "drinking": ("for drinking", "juotavaksi",
                 ("drink", "breakfast", "lunch milk", "juoma", "juotava", "aamiai", "ruokajuoma", "maitolasi")),
}
# vocabulary words that are names for a canonical use
_ALIASES = {"dessert": "whipping", "whipping/dessert": "whipping", "sauce": "cooking", "sauce/cooking": "cooking",
            "laminated": "laminated_pastry", "pastry": "laminated_pastry"}


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", (s or "").strip().lower())


def classify(use: str | None) -> list[str]:
    """The canonical uses a stated use names (several for a dish: lasagne is melting and cooking). [] = unknown."""
    text = _norm(use or "")
    if not text or text == "unknown":
        return []
    for prefix in (RECIPE_PREFIX, "recipe:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    word = text.replace(" ", "_")
    if word in USES:
        return [word]
    if text in _ALIASES:
        return [_ALIASES[text]]
    return [u for u, (_, _, keys) in USES.items() if any(k in text for k in keys)]


def use_key(use: str | None) -> str | None:
    """A stable key for comparing two stated uses (remembered decisions): the canonical uses, sorted."""
    c = classify(use)
    return "+".join(sorted(c)) if c else None


def recipe(name: str | None) -> str | None:
    """The `use` of an ingredient line in a recipe: 'ingredient-in-recipe:<dish>'."""
    name = " ".join((name or "").split())[:180]
    return RECIPE_PREFIX + name if name else None


def _dish(use: str | None) -> str | None:
    u = (use or "").strip()
    for prefix in (RECIPE_PREFIX, "recipe:"):  # 'recipe: <dishes>' is the purpose the recipe planner sets
        if u.lower().startswith(prefix):
            return u[len(prefix):].strip() or None
    return None


_WORDS = {"dessert": ("for a dessert", "jälkiruokaan"), "whipping": ("for whipping", "vaahdotukseen"),
          "sauce": ("for a sauce", "kastikkeeseen"), "cooking": ("for cooking", "ruoanlaittoon")}


def phrase(use: str | None, canonical: str, lang: str = "en") -> str:
    """'for a dessert' / 'for whipping or a dessert' / 'in the dish Tiramisu'."""
    en, fi_, _ = USES[canonical]
    dish = _dish(use)
    if dish:
        return f"ruokalajiin {dish}" if lang == "fi" else f"in the dish {dish}"
    word = _WORDS.get(_norm(use or ""))
    if word:
        return word[1] if lang == "fi" else word[0]
    return fi_ if lang == "fi" else en


# --- the table -----------------------------------------------------------------------------------------------
# (type key or family) -> use -> (level, why in English, why in Finnish). The why says what a kitchen would see go
# wrong; it is shown to the person who confirms. Type keys come from sous/substitutes.py (_TYPES).
_WHIP = ("exact", "a different cream can whip differently (stabilisers such as lecithin or carrageenan change how "
         "it whips and how long the foam holds)",
         "toinen kerma voi vaahtoutua eri tavalla (stabilointiaineet, kuten lesitiini tai karrageeni, "
         "vaikuttavat vaahdotukseen ja vaahdon kestoon)")
_CREAM_COOK = ("same_spec", "in cooking the brand matters little, but the fat % does (lower-fat cream can split "
               "when it boils)",
               "ruoanlaitossa merkillä ei ole juuri väliä, mutta rasvaprosentilla on (vähärasvaisempi kerma voi "
               "juoksettua keitettäessä)")
_CREAM_BAKE = ("same_spec", "in baking the fat % changes the result; any brand with the same fat % will do",
               "leivonnassa rasvaprosentti vaikuttaa lopputulokseen; mikä tahansa merkki samalla rasvaprosentilla käy")
_CREAM_COFFEE = ("type", "in coffee the brand barely matters; any cream of the same kind will do",
                 "kahvissa merkillä ei ole juuri väliä; mikä tahansa samanlainen kerma käy")
_CREAM = {"whipping": _WHIP, "cooking": _CREAM_COOK, "baking": _CREAM_BAKE, "coffee": _CREAM_COFFEE}
_MILK = {
    "drinking": ("same_spec", "milk served to drink must be the same kind and fat % (guests notice); any brand",
                 "juotavaksi tarjottavan maidon pitää olla samanlaista ja saman rasvaisuista (asiakkaat huomaavat); "
                 "merkillä ei ole väliä"),
    "coffee": ("type", "in coffee any milk of the same kind will do", "kahvissa mikä tahansa samanlainen maito käy"),
    "baking": ("type", "in baking any milk of the same kind will do", "leivonnassa mikä tahansa samanlainen maito käy"),
    "cooking": ("type", "in cooking any milk of the same kind will do",
                "ruoanlaitossa mikä tahansa samanlainen maito käy"),
}
TABLE: dict[str, dict[str, tuple[Level, str, str]]] = {
    "kuohukerma": _CREAM,
    "kerma": _CREAM,
    "kahvikerma": {"coffee": _CREAM_COFFEE,
                   "cooking": ("type", "in cooking any cream of the same kind will do",
                               "ruoanlaitossa mikä tahansa samanlainen kerma käy")},
    "milk": _MILK,  # family: milk and milk drink
    "voi": {
        "laminated_pastry": ("exact", "laminated pastry depends on the butter's water content and how pliable it "
                             "stays when cold, which differ between brands",
                             "kerrostaikina riippuu voin vesipitoisuudesta ja siitä, miten notkeana se pysyy "
                             "kylmänä, ja ne vaihtelevat merkeittäin"),
        "baking": ("same_spec", "in baking the fat and salt content matter; any brand with the same spec will do",
                   "leivonnassa rasva- ja suolapitoisuus ratkaisevat; mikä tahansa merkki samoilla tiedoilla käy"),
        "cooking": ("type", "in cooking any butter will do", "ruoanlaitossa mikä tahansa voi käy"),
    },
    "cheese": {  # family
        "melting": ("same_spec", "in a gratin or on a pizza the cheese must melt the same way: the same kind and "
                    "fat %, any brand",
                    "gratiinissa tai pizzassa juuston pitää sulaa samalla tavalla: sama juustotyyppi ja "
                    "rasvaprosentti, merkillä ei ole väliä"),
        "cooking": ("type", "in cooking any cheese of the same kind will do",
                    "ruoanlaitossa mikä tahansa samanlainen juusto käy"),
    },
}
# The why for a mandate override (no table text): by level
_GENERIC = {
    "exact": ("only the same product will do for this use", "tähän käyttöön käy vain sama tuote"),
    "same_spec": ("the substitute must be the same kind, with the same fat % and claims (any brand)",
                  "korvaavan tuotteen pitää olla samanlainen, samalla rasvaprosentilla ja samoilla merkinnöillä "
                  "(merkillä ei ole väliä)"),
    "type": ("any product of the same kind will do", "mikä tahansa samanlainen tuote käy"),
}


def _ptype(p: dict | None) -> dict:
    if not p:
        return {}
    try:
        from .substitutes import product_type  # lazy: substitutes imports mandate
        return product_type(p) or {}
    except Exception:  # an unreadable name never breaks a quote
        return {}


def lookup(product: dict | None, use: str | None, mandate=None) -> dict | None:
    """The sensitivity of `product` for `use`: {level, use_class, source ('mandate' | 'table'), why, why_fi, key},
    the strictest among the canonical uses the stated use names. None: unknown use, or not in the table (the
    ordinary substitution behaviour applies)."""
    classes = classify(use)
    if not classes or not product:
        return None
    t = _ptype(product)
    keys = [k for k in (t.get("key"), t.get("family"), product.get("category"), "*") if k]
    override = getattr(mandate, "substitution_by_use", None) or {}
    best = None
    for c in classes:
        hit = None
        for k in keys:  # the most specific key wins; the mandate before the table
            if c in (override.get(k) or {}):
                lv = override[k][c]
                hit = {"level": lv, "use_class": c, "source": "mandate", "key": k,
                       "why": _GENERIC[lv][0], "why_fi": _GENERIC[lv][1]}
                break
            if c in TABLE.get(k, {}):
                lv, why, why_fi = TABLE[k][c]
                hit = {"level": lv, "use_class": c, "source": "table", "key": k, "why": why, "why_fi": why_fi}
                break
        if hit and (best is None or _RANK[hit["level"]] > _RANK[best["level"]]):
            best = hit
    return best


def _label(t: dict, p: dict) -> tuple[str, str]:
    """(English label, Finnish label) of the product's kind: 'Whipping cream' / 'Kuohukerma'."""
    en = (t.get("label") or (p.get("name") or "the product")).strip()
    fi_ = (t.get("key") or "").strip() or en
    return en[:1].upper() + en[1:], fi_[:1].upper() + fi_[1:]


def _gaps(level: Level, o: dict, s: dict, ot: dict, st: dict) -> tuple[list[str], list[str]]:
    """What keeps the substitute from meeting `level` (English, Finnish); [] = it meets it."""
    en, fi_ = [], []
    if level == "exact":
        if o.get("id") != s.get("id"):
            ob, sb = (o.get("brand") or "").strip(), (s.get("brand") or "").strip()
            if ob and sb and ob.lower() != sb.lower():
                en.append(f"a different brand: {sb} instead of {ob}")
                fi_.append(f"eri merkki: {sb} eikä {ob}")
            else:
                en.append("a different product")
                fi_.append("eri tuote")
        return en, fi_
    if ot.get("family") and st.get("family") != ot.get("family"):
        en.append(f"another kind of product ({st.get('label') or 'unknown'} instead of {ot.get('label')})")
        fi_.append(f"toisenlainen tuote ({st.get('key') or 'tuntematon'} eikä {ot.get('key')})")
        return en, fi_
    if level == "type":
        return en, fi_
    if ot.get("key") and st.get("key") != ot.get("key"):
        en.append(f"{st.get('label') or 'unknown kind'} instead of {ot.get('label')}")
        fi_.append(f"{st.get('key') or 'tuntematon tyyppi'} eikä {ot.get('key')}")
    of, sf = ot.get("fat_pct"), st.get("fat_pct")
    if of is not None and (sf is None or abs(of - sf) > 0.05):
        en.append(f"fat {'unknown' if sf is None else f'{sf:g}%'} vs {of:g}%")
        fi_.append(f"rasva {'tuntematon' if sf is None else f'{sf:g} %'} eikä {of:g} %")
    if ot.get("flavour") != st.get("flavour"):
        en.append(f"flavour {st.get('flavour') or 'plain'} vs {ot.get('flavour') or 'plain'}")
        fi_.append("eri maku")
    lost = sorted(set(o.get("claims") or []) - set(s.get("claims") or []))
    if lost:
        from . import fi as FI
        en.append("not " + ", ".join(c.replace("_", " ").lower() for c in lost) + " (the original is)")
        fi_.append("puuttuu merkintä: " + FI.claims(lost))
    return en, fi_


def judge(original: dict | None, substitute: dict | None, use: str | None, mandate=None) -> dict | None:
    """Is `substitute` acceptable in place of `original` for `use`? None when the use is unknown or the table (and
    the mandate) say nothing about it: the ordinary substitution rules apply. Otherwise
    {use, use_class, use_sensitivity, source, ok, reason, reason_fi, compromise}: the reason says the why in words
    a kitchen manager reads at a glance."""
    if not original or not substitute:
        return None
    s = lookup(original, use, mandate)
    if s is None:
        return None
    ot, st = _ptype(original), _ptype(substitute)
    gaps, gaps_fi = _gaps(s["level"], original, substitute, ot, st)
    en_label, fi_label = _label(ot, original)
    what, what_fi = phrase(use, s["use_class"]), phrase(use, s["use_class"], "fi")
    by = " (the buyer's mandate says so)" if s["source"] == "mandate" else ""
    by_fi = " (ostajan valtakirja määrää näin)" if s["source"] == "mandate" else ""
    if gaps:
        reason = (f"{en_label} {what}: {s['why']}{by}, so a person should confirm the substitute "
                  f"({'; '.join(gaps)}).")
        reason_fi = (f"{fi_label} {what_fi}: {s['why_fi']}{by_fi}, joten ihmisen pitää vahvistaa korvaava tuote "
                     f"({'; '.join(gaps_fi)}).")
    else:
        ok_en = {"type": "the substitute is the same kind, so it is fine for this use",
                 "same_spec": "the substitute is the same kind, fat % and claims, so it is fine for this use",
                 "exact": "it is the same product"}[s["level"]]
        ok_fi = {"type": "korvaava tuote on samanlainen, joten se käy tähän käyttöön",
                 "same_spec": "korvaava tuote on samanlainen, samalla rasvaprosentilla ja merkinnöillä, joten se "
                              "käy tähän käyttöön",
                 "exact": "se on sama tuote"}[s["level"]]
        reason = f"{en_label} {what}: {s['why']}{by}; {ok_en}."
        reason_fi = f"{fi_label} {what_fi}: {s['why_fi']}{by_fi}; {ok_fi}."
    return {"use": use, "use_class": s["use_class"], "use_sensitivity": s["level"], "source": s["source"],
            "ok": not gaps, "reason": reason, "reason_fi": reason_fi,
            "compromise": None if not gaps else f"{what}: needs a person ({'; '.join(gaps)})"}


# --- where the use comes from when the request doesn't say -----------------------------------------------------

def from_history(mandate_id: str | None, product_ids: list[str]) -> tuple[str, str] | None:
    """(use, source) stated earlier for one of these products under the mandate: a standing order's item, else the
    newest past order's quote line (or item). None when no history states one."""
    if not mandate_id or not product_ids:
        return None
    ids = set(product_ids)
    try:
        from . import standing_store
        for so in standing_store.list_(mandate_id):
            for it in so.get("items") or []:
                said = it.get("use") or it.get("purpose")
                if said and ids & set(it.get("product_ids") or []):
                    return said, f"history: standing order {so.get('id')}"
    except Exception:  # no standing-order table yet: no history from it
        pass
    from . import store
    try:
        orders = store.list_orders(status="ordered", mandate_id=mandate_id)
    except Exception:
        return None
    for o in orders[:50]:
        q = store.get_quote(o.get("quote_id")) if o.get("quote_id") else None
        for l in (q or {}).get("lines") or []:
            said = l.get("use") or l.get("purpose")
            if said and (l.get("product_id") in ids or l.get("substitute_for") in ids):
                return said, f"history: order {o.get('id')}"
        for it in (q or {}).get("items") or []:
            said = it.get("use") or it.get("purpose")
            if said and ids & set((it.get("product_ids") or []) + [it.get("substitute_for")]):
                return said, f"history: order {o.get('id')}"
    return None


def validate_override(v: dict) -> dict:
    """Mandate field substitution_by_use: {key: {use: level}} with canonical uses and known levels."""
    out: dict[str, dict[str, str]] = {}
    for key, per in (v or {}).items():
        if not isinstance(per, dict):
            raise ValueError("substitution_by_use maps a product type, family or category to {use: level}")
        for use, lv in per.items():
            c = classify(use)
            if len(c) != 1 or re.fullmatch(r"[a-z_/ ]+", _norm(use)) is None:
                raise ValueError(f"unknown use '{use}'; one of {', '.join(USES)}")
            if lv not in LEVELS:
                raise ValueError(f"unknown level '{lv}'; one of {', '.join(LEVELS)}")
            out.setdefault(str(key), {})[c[0]] = lv
    return out
