"""Buyer-side recipe planning: "Lasagne for 120 on Monday, we have 10 kg flour" -> one signed basket quote.

The chain, and who does what:

    request text --(LLM or recipe table: interpretation only)--> dishes, portions, per-portion ingredients, stock
                 --(code)--> scale by portions, convert to KG / L, subtract stock on hand, round up
                 --(code + gateway search)--> up to 5 catalog candidates per ingredient, filtered by the
                     mandate's allergen and claim rules (search filters) and a per-ingredient name check
                 --(gateway)--> ONE request_quote with items[]: the gateway picks pack sizes and the cheapest
                     orderable product per KG/L, reports what is uncovered, and previews the mandate tier
                 --(gateway)--> suggest_substitutes for what is uncovered or ruled out by the mandate
                 --(gateway, optional)--> place_order: the mandate engine decides silent/notify/ask/block

The LLM never sees prices or decides anything: it only turns the chef's sentence into strict JSON (validated
with pydantic, one retry). Without a model (or with --provider table) a curated table (agent/recipes.json)
does the interpretation, so the demo and the tests run offline. The plan says which path was used.

This is buyer-side intelligence: the gateway (sous/) stays free of LLM calls and sees an ordinary basket.

    uv run python -m agent.recipe --provider table --mandate M-HOTEL-AURORA --agent aurora-purchasing-agent \\
        --gateway local "Lasagne for 120 on Monday; we have 10 kg flour and plenty of salt"
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from .buyer import WEEKDAYS, helsinki_today, next_delivery_day

RECIPES_PATH = Path(__file__).with_name("recipes.json")
MAX_CANDIDATES = 5
MAX_SUBSTITUTE_CALLS = 4
SEARCH_LIMIT = 30

# Dietary notes on an ingredient -> catalog claims (search filter).
NOTE_CLAIMS = {
    "lactose-free": "FREE_FROM_LACTOSE", "lactose free": "FREE_FROM_LACTOSE", "laktoositon": "FREE_FROM_LACTOSE",
    "laktosfri": "FREE_FROM_LACTOSE", "gluten-free": "FREE_FROM_GLUTEN", "gluten free": "FREE_FROM_GLUTEN",
    "gluteeniton": "FREE_FROM_GLUTEN", "glutenfri": "FREE_FROM_GLUTEN", "vegan": "VEGAN", "vegaaninen": "VEGAN",
}
# Which table ingredients a menu-wide "lactose-free" / "gluten-free" applies to.
DIET_SCOPE = {
    "FREE_FROM_LACTOSE": {"maito", "voi", "kuohukerma", "ruokakerma", "juustoraaste"},
    "FREE_FROM_GLUTEN": {"lasagnelevy", "vehnajauho"},
}


# --- the interpretation schema (what the LLM must return) -------------------------------------------

class Ingredient(BaseModel):
    name_fi: str = Field(min_length=1)
    name_en: str = Field(min_length=1)
    amount_per_portion: float = Field(gt=0, le=5000)
    unit: Literal["g", "ml", "pcs"]
    notes: str | None = None
    key: str | None = None  # lexicon key, when known (the table sets it; LLM output is matched by name)


class Dish(BaseModel):
    name: str = Field(min_length=1)
    portions: int | None = Field(None, gt=0, le=100000)  # overrides the menu's portions for this dish
    ingredients: list[Ingredient] = Field(min_length=1, max_length=40)


class Stock(BaseModel):
    ingredient: str = Field(min_length=1)
    amount: float | None = Field(None, ge=0)
    unit: Literal["g", "kg", "ml", "dl", "l", "pcs"] | None = None
    plenty: bool = False  # "plenty of salt": everything this ingredient needs is on hand

    @field_validator("unit", mode="before")
    @classmethod
    def _lower(cls, v):
        return v.lower() if isinstance(v, str) else v


class MenuSpec(BaseModel):
    dishes: list[Dish] = Field(min_length=1, max_length=12)
    portions: int = Field(gt=0, le=100000)
    delivery_date: date | None = None
    stock_on_hand: list[Stock] = Field(default_factory=list)


class InterpretError(ValueError):
    pass


# --- lexicon --------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def table() -> dict:
    return json.loads(RECIPES_PATH.read_text(encoding="utf-8"))


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFC", (s or "").lower())
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s\-äöå]", " ", s)).strip()


def _lex_terms() -> list[tuple[str, str]]:
    """(term, key) pairs for every lexicon key, Finnish and English name and alias; longest first."""
    out = []
    for key, e in table()["ingredients"].items():
        for t in {key, e["fi"], e["en"], *e.get("aliases", [])}:
            out.append((_norm(t), key))
    return sorted(out, key=lambda x: -len(x[0]))


def match_ingredient(*names: str) -> str | None:
    """The lexicon key for an ingredient name (exact name first, then the longest term inside the name)."""
    terms = _lex_terms()
    normed = [_norm(n) for n in names if n]
    for n in normed:
        for t, key in terms:
            if n == t:
                return key
    for n in normed:
        for t, key in terms:
            # Finnish case endings: "sipulia", "jauhoja", "kermaa", "suolaa"
            if len(t) >= 3 and re.search(rf"(?<!\w){re.escape(t)}(?:a|ä|aa|ää|ta|tä|ja|jä|n)?(?!\w)", n):
                return key
    return None


# --- interpretation: table ------------------------------------------------------------------------

_WEEKDAY_WORDS = {
    0: ("monday", "maanantai", "måndag"), 1: ("tuesday", "tiistai", "tisdag"), 2: ("wednesday", "keskiviikko", "onsdag"),
    3: ("thursday", "torstai", "torsdag"), 4: ("friday", "perjantai", "fredag"), 5: ("saturday", "lauantai", "lördag"),
    6: ("sunday", "sunnuntai", "söndag"),
}
_UNIT_WORDS = {"kg": "kg", "kilo": "kg", "kilos": "kg", "kilogram": "kg", "kilograms": "kg", "kiloa": "kg",
               "g": "g", "gram": "g", "grams": "g", "grammaa": "g", "l": "l", "litre": "l", "litres": "l",
               "liter": "l", "liters": "l", "litraa": "l", "dl": "dl", "ml": "ml", "pcs": "pcs", "pieces": "pcs",
               "kpl": "pcs", "st": "pcs"}
_STOCK_START = re.compile(r"\b(we have|we've got|we got|have got|on hand|in stock|already have|meillä on|"
                          r"varastossa on|on jo|vi har|have)\b", re.I)
_PLENTY = re.compile(r"\b(?:plenty of|enough|lots of|a lot of|runsaasti|riittävästi|tarpeeksi|gott om)\s+"
                     r"([a-zäöå][\w\- ]*)", re.I)


def parse_date(text: str, today: date) -> date | None:
    t = text.lower()
    if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t):
        return date(int(m[1]), int(m[2]), int(m[3]))
    if m := re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})?", t):
        d = date(int(m[3]) if m[3] else today.year, int(m[2]), int(m[1]))
        return d if d > today or m[3] else date(today.year + 1, d.month, d.day)
    if re.search(r"\b(tomorrow|huomenna|i morgon)\b", t):
        return today + timedelta(days=1)
    for wd, words in _WEEKDAY_WORDS.items():
        if re.search(r"\b(" + "|".join(words) + r")\w*", t):
            ahead = (wd - today.weekday()) % 7 or 7
            return today + timedelta(days=ahead)
    return None


def parse_portions(text: str) -> int | None:
    t = text.lower().replace(",", "")
    m = (re.search(r"\b(\d{1,6})\s*(?:portions?|people|persons?|guests?|pax|covers|diners|servings|annosta|"
                   r"hengelle|henkilölle|hlö|ruokailijalle|personer|portioner)\b", t)
         or re.search(r"\bfor\s+(\d{1,6})\b", t) or re.search(r"\b(\d{1,6})\s*:lle\b", t))
    return int(m[1]) if m else None


def parse_stock(text: str) -> list[Stock]:
    out: list[Stock] = []
    for m in _PLENTY.finditer(text):
        name = re.split(r"\s+(?:and|ja|och)\s+|[,.;]", m[1])[0].strip()
        if name:
            out.append(Stock(ingredient=name, plenty=True))
    start = _STOCK_START.search(text)
    if not start:
        return out
    part = text[start.end():]
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*([a-zA-Z]+)\.?\s+(?:of\s+)?([a-zäöåA-ZÄÖÅ][\w\-]*(?:\s+[a-zäöå][\w\-]*)?)",
                         part):
        unit = _UNIT_WORDS.get(m[2].lower())
        if not unit:
            continue
        name = re.split(r"\s+(?:and|ja|och|plus)\b", m[3])[0].strip()
        out.append(Stock(ingredient=name, amount=float(m[1].replace(",", ".")), unit=unit))
    return out


def interpret_table(text: str, today: date) -> MenuSpec:
    """Deterministic interpretation from agent/recipes.json (no model): known dishes, portions, stock."""
    t = _norm(text)
    recipes = table()["recipes"]
    found: list[tuple[int, str]] = []
    for key, r in recipes.items():
        hits = [m.start() for a in r["aliases"] for m in re.finditer(rf"(?<!\w){re.escape(_norm(a))}", t)]
        if hits:
            found.append((min(hits), key))
    # "kinkku" inside "joulukinkku" etc.: keep each dish once, in the order they were mentioned
    keys = [k for _, k in sorted(found)]
    if not keys:
        raise InterpretError("No dish from the recipe table in the request "
                             f"(known: {', '.join(r['name_fi'] for r in recipes.values())}).")
    portions = parse_portions(text)
    if not portions:
        raise InterpretError("How many portions? Say e.g. 'for 120' or '120 portions'.")
    diet = {c for w, c in NOTE_CLAIMS.items() if w in t}
    lex = table()["ingredients"]
    dishes = []
    for k in keys:
        r = recipes[k]
        ings = []
        for i in r["ingredients"]:
            e = lex[i["key"]]
            notes = []
            notes += [n for n, c in NOTE_CLAIMS.items() if c in diet and i["key"] in DIET_SCOPE.get(c, ())
                      and n in ("lactose-free", "gluten-free")]
            ings.append(Ingredient(name_fi=e["fi"], name_en=e["en"], amount_per_portion=i["amount"], unit=i["unit"],
                                   notes="; ".join(notes) or None, key=i["key"]))
        dishes.append(Dish(name=f"{r['name_fi']} ({r['name_en']})" if r["name_fi"] != r["name_en"] else r["name_fi"],
                           ingredients=ings))
    return MenuSpec(dishes=dishes, portions=portions, delivery_date=parse_date(text, today),
                    stock_on_hand=parse_stock(text))


# --- interpretation: LLM --------------------------------------------------------------------------

def llm_prompt(today: date) -> str:
    days = ", ".join(f"{WEEKDAYS[d.weekday()]} {d.isoformat()}" for d in (today + timedelta(days=i) for i in range(1, 8)))
    names = ", ".join(sorted({e["fi"] for e in table()["ingredients"].values()}))
    return f"""You turn a professional chef's menu request into a structured ingredient plan. You only interpret: \
you do not choose products, prices or suppliers, and you do not judge budgets or allergen rules.

Today is {WEEKDAYS[today.weekday()]} {today.isoformat()}; the coming days: {days}.

Return ONLY one JSON object, no prose, no markdown, exactly this shape:
{{"dishes": [{{"name": str, "portions": int|null, "ingredients": [{{"name_fi": str, "name_en": str, \
"amount_per_portion": number, "unit": "g"|"ml"|"pcs", "notes": str|null}}]}}],
 "portions": int, "delivery_date": "YYYY-MM-DD"|null,
 "stock_on_hand": [{{"ingredient": str, "amount": number|null, "unit": "g"|"kg"|"ml"|"dl"|"l"|"pcs"|null, \
"plenty": bool}}]}}

Rules:
- Use a standard Finnish foodservice recipe for each dish. amount_per_portion is the raw ingredient for ONE \
portion (a lunch portion: soup ~350 ml, main course ~300-350 g in total). Weigh solids in g, liquids in ml; \
"pcs" only for things counted, like eggs.
- Include every ingredient a kitchen would order (main ingredients, dairy, stock, seasonings incl. salt); \
leave out water. Merge duplicates within a dish.
- name_fi is the plain Finnish product word a wholesaler would use; prefer one of these when it fits: {names}.
- notes: dietary needs the chef states for that ingredient ("lactose-free", "gluten-free"), else null.
- portions: the number of portions/people/guests the chef gives. A dish's own portions only if the chef gives \
a different number for it.
- delivery_date: the date the chef names (a weekday means the next such date in the list above), else null.
- stock_on_hand: what the chef says they already have. "plenty of X" or "enough X" -> plenty true, amount null.
- Optional values you don't know: null or leave the key out."""


def _extract_json(s: str) -> dict:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```\w*\s*|\s*```$", "", s)
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("no JSON object in the answer")
    return json.loads(s[i:j + 1])


@dataclass
class LLMUse:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors: list[str] = field(default_factory=list)
    structured: bool = False       # the provider took the JSON schema (structured output)
    schema_rejected: bool = False  # it answered HTTP 400 to response_format: plain JSON prompting instead


_STR = {"type": "string"}
# The plan's JSON schema for structured output (Gemini and other OpenAI-compatible servers): plain types and
# enums only (no $ref / anyOf), optional fields simply not required. pydantic (MenuSpec) still validates.
MENU_SCHEMA = {
    "type": "object",
    "properties": {
        "dishes": {"type": "array", "items": {"type": "object", "properties": {
            "name": _STR,
            "portions": {"type": "integer", "description": "only if this dish has its own number of portions"},
            "ingredients": {"type": "array", "items": {"type": "object", "properties": {
                "name_fi": _STR, "name_en": _STR,
                "amount_per_portion": {"type": "number", "description": "raw amount for ONE portion"},
                "unit": {"type": "string", "enum": ["g", "ml", "pcs"]},
                "notes": {"type": "string", "description": "dietary need for this ingredient, e.g. lactose-free"}},
                "required": ["name_fi", "name_en", "amount_per_portion", "unit"]}}},
            "required": ["name", "ingredients"]}},
        "portions": {"type": "integer"},
        "delivery_date": {"type": "string", "description": "YYYY-MM-DD; omit if the chef names no day"},
        "stock_on_hand": {"type": "array", "items": {"type": "object", "properties": {
            "ingredient": _STR, "amount": {"type": "number"},
            "unit": {"type": "string", "enum": ["g", "kg", "ml", "dl", "l", "pcs"]},
            "plenty": {"type": "boolean"}}, "required": ["ingredient", "plenty"]}}},
    "required": ["dishes", "portions", "stock_on_hand"],
}
RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {"name": "menu_plan", "schema": MENU_SCHEMA}}


def _chat(llm, messages: list[dict], use: LLMUse):
    """One model call with structured output; if the server rejects response_format (HTTP 400), the same
    call without it (the prompt still asks for the JSON). Provider errors become InterpretError."""
    from .llm import LLMError
    try:
        if not use.schema_rejected:
            try:
                resp = llm.chat(messages, response_format=RESPONSE_FORMAT)
                use.structured = True
                return resp
            except LLMError as e:
                if "HTTP 400" not in str(e):
                    raise
                use.schema_rejected, use.structured = True, False
                use.errors.append(f"structured output not accepted: {str(e)[:200]}")
        return llm.chat(messages)
    except LLMError as e:
        raise InterpretError(f"model call failed: {e}") from None


def interpret_llm(text: str, today: date, llm, use: LLMUse | None = None) -> MenuSpec:
    """Ask the model for the plan as JSON (schema-constrained where the provider supports it); validate with
    pydantic; one retry with the validation error. Raises InterpretError."""
    use = use or LLMUse()
    messages = [{"role": "system", "content": llm_prompt(today)}, {"role": "user", "content": text}]
    for attempt in range(2):
        resp = _chat(llm, messages, use)
        use.calls += 1
        use.prompt_tokens += resp.usage.get("prompt_tokens", 0)
        use.completion_tokens += resp.usage.get("completion_tokens", 0)
        try:
            return MenuSpec.model_validate(_extract_json(resp.content))
        except (ValueError, ValidationError) as e:
            err = str(e)[:800]
            use.errors.append(err)
            messages += [{"role": "assistant", "content": resp.content or ""},
                         {"role": "user", "content": f"That was not valid: {err}\nReturn only the corrected JSON object."}]
    raise InterpretError(f"The model's answer was not valid JSON for the plan after 2 tries: {use.errors[-1][:300]}")


# --- arithmetic: scale, convert, subtract stock, round ---------------------------------------------

_TO_BASE = {"g": ("KG", 0.001), "kg": ("KG", 1.0), "ml": ("L", 0.001), "dl": ("L", 0.1), "l": ("L", 1.0),
            "pcs": ("PCS", 1.0)}


def round_up(amount: float, unit: str) -> float:
    """Round a purchase amount up to a sensible step (the gateway then rounds to whole packs)."""
    if amount <= 0:
        return 0.0
    if unit == "SALES_UNIT":
        return float(math.ceil(amount - 1e-9))
    step = 0.01 if amount < 1 else 0.1 if amount < 20 else 0.5
    return round(math.ceil(amount / step - 1e-9) * step, 2)


def _purpose(n) -> str:
    """What an ingredient is for, on its quote item: 'recipe: <dishes>'. The gateway reads a recipe purpose as
    substitution 'ask' (Valio Aimo: a substitute may not work the same in a recipe, so the buyer decides)."""
    dishes = ", ".join(dict.fromkeys(d.split(" (")[0] for d in (getattr(n, "dishes", None) or [])))
    return ("recipe: " + dishes)[:120] if dishes else "recipe"


@dataclass
class Need:
    key: str
    name_fi: str
    name_en: str
    dishes: list[str]
    unit: str                      # KG | L | SALES_UNIT (after conversion)
    needed: float                  # in `unit`, exact
    per_portion: list[str]         # e.g. "40 g x 120 (Lasagne)"
    claims: list[str]
    queries: list[str]
    must: list[str]
    exclude: list[str]
    notes: list[str] = field(default_factory=list)
    bulk_ok: bool = False          # concentrates (bouillon "1.5 kg/100 L"): big nominal packs are normal
    on_hand: float | None = None   # in `unit`; inf = plenty
    on_hand_text: str | None = None
    to_buy: float = 0.0
    status: str = "pending"        # covered_by_stock | quoted | uncovered | not_found | ruled_out_by_mandate
    candidates: list[dict] = field(default_factory=list)
    items: list[int] = field(default_factory=list)  # indexes into the quote's items
    substitute: dict | None = None

    def row(self) -> dict:
        return {"ingredient": self.key, "name_fi": self.name_fi, "name_en": self.name_en, "dishes": self.dishes,
                "per_portion": self.per_portion, "unit": self.unit, "needed": round(self.needed, 3),
                "on_hand": ("plenty" if self.on_hand == math.inf else None if self.on_hand is None
                            else round(self.on_hand, 3)),
                "to_buy": self.to_buy, "status": self.status, "claims": self.claims, "notes": self.notes,
                "candidates": [{k: c.get(k) for k in ("id", "name")} for c in self.candidates],
                "quote_items": self.items, "substitute": self.substitute}


def compute_needs(spec: MenuSpec) -> list[Need]:
    lex = table()["ingredients"]
    needs: dict[str, Need] = {}
    for dish in spec.dishes:
        portions = dish.portions or spec.portions
        short = dish.name.split(" (")[0]
        for ing in dish.ingredients:
            key = ing.key or match_ingredient(ing.name_fi, ing.name_en) or _norm(ing.name_fi)
            e = lex.get(key) or {"fi": ing.name_fi, "en": ing.name_en, "queries": [ing.name_fi, ing.name_en],
                                 "must": [], "exclude": []}
            base, factor = _TO_BASE[ing.unit]
            amount = ing.amount_per_portion * factor * portions
            notes = []
            if base == "L" and e.get("concentrate_g_per_l"):  # stock bought as concentrate, by weight
                base, amount = "KG", amount * e["concentrate_g_per_l"] / 1000
                notes.append(f"{_fmt(ing.amount_per_portion * factor * portions, 'L')} made from concentrate "
                             f"at {e['concentrate_g_per_l']:g} g per litre")
            if base == "PCS":
                if e.get("pcs_g"):
                    base, amount = "KG", amount * e["pcs_g"] / 1000
                    notes.append(f"{ing.amount_per_portion:g} pcs/portion counted as {e['pcs_g']:g} g each")
                else:
                    base = "SALES_UNIT"
                    notes.append("counted in pieces: check the pack size")
            claims = sorted({c for w, c in NOTE_CLAIMS.items() if ing.notes and w in ing.notes.lower()}
                            | set(e.get("claims", [])))
            n = needs.get(key)
            if n is None:
                n = needs[key] = Need(key=key, name_fi=e["fi"], name_en=e["en"], dishes=[], unit=base, needed=0.0,
                                      per_portion=[], claims=claims, queries=list(e.get("queries") or [e["fi"]]),
                                      must=list(e.get("must", [])), exclude=list(e.get("exclude", [])),
                                      bulk_ok=bool(e.get("bulk_ok") or e.get("concentrate_g_per_l")))
                if key not in lex:
                    n.queries = list(dict.fromkeys([ing.name_fi, ing.name_en]))
            elif n.unit != base:  # same ingredient in g in one dish and ml in another: water-like density
                notes.append(f"{base} and {n.unit} added at 1 kg = 1 L")
            n.claims = sorted(set(n.claims) | set(claims))
            n.needed += amount
            n.dishes.append(short)
            n.per_portion.append(f"{ing.amount_per_portion:g} {ing.unit} x {portions} ({short})")
            n.notes += [x for x in notes + ([ing.notes] if ing.notes else []) if x not in n.notes]
    for s in spec.stock_on_hand:
        key = match_ingredient(s.ingredient)
        n = needs.get(key) if key else None
        if n is None:
            continue
        if s.plenty or s.amount is None:
            n.on_hand, n.on_hand_text = math.inf, f"plenty ({s.ingredient})"
            continue
        base, factor = _TO_BASE[s.unit or "pcs"]
        amount = s.amount * factor
        if base == "PCS" and n.unit == "KG" and (pcs := table()["ingredients"].get(n.key, {}).get("pcs_g")):
            base, amount = "KG", amount * pcs / 1000
        if base != n.unit and {base, n.unit} == {"KG", "L"}:
            n.notes.append("stock in kg vs need in L (or back) taken at 1 kg = 1 L")
        elif base != n.unit and not (base == "PCS" and n.unit == "SALES_UNIT"):
            n.notes.append(f"stock '{s.amount:g} {s.unit}' not comparable with the need; ignored")
            continue
        n.on_hand = (n.on_hand or 0) + amount
        n.on_hand_text = f"{s.amount:g} {s.unit} {s.ingredient}"
    for n in needs.values():
        rest = n.needed - (n.on_hand or 0)
        n.to_buy = round_up(rest, n.unit) if rest > 1e-9 else 0.0
        if n.to_buy == 0:
            n.status = "covered_by_stock"
    return list(needs.values())


# --- gateway steps ----------------------------------------------------------------------------------

@dataclass
class Trace:
    steps: list[dict] = field(default_factory=list)
    valio_api_calls: int = 0

    def add(self, tool: str, args: dict, is_error: bool, body: dict) -> None:
        v = (body.get("quote") or body).get("valio_api_calls") or {}
        n = (v.get("price_calls") or 0) + (v.get("availability_calls") or 0) if isinstance(v, dict) else 0
        self.valio_api_calls += n
        brief = {"error": body.get("error"), "message": body.get("message")} if is_error else (
            {"ids": [p["id"] for p in body.get("products", [])][:8]} if tool == "search_products" else
            {"quote_id": body["quote"]["id"], "total_ex_vat": body["quote"]["total_ex_vat"]} if "quote" in body else
            {"options": [o["product_id"] for o in body.get("options", [])]} if tool == "suggest_substitutes" else
            {"id": body.get("id"), "status": body.get("status")})
        self.steps.append({"tool": tool, "args": {k: v for k, v in args.items() if k not in ("agent_id", "mandate_id")},
                           "is_error": is_error, "valio_api_calls": n, "result": brief})


BUDGET_WAIT_S = 15.0


def _call(gw, trace: Trace, tool: str, args: dict, sleep=None) -> tuple[bool, dict]:
    """One gateway tool call. The gateway's own per-minute cap on live Valio calls is waited out (up to 5
    times), like agent.buyer does; every other error goes back to the caller."""
    import time
    for attempt in range(6):
        is_error, body = gw.call(tool, args)
        if not (is_error and body.get("error") == "valio_unavailable" and "budget" in str(body.get("message"))
                and attempt < 5):
            break
        (sleep or time.sleep)(BUDGET_WAIT_S)
    trace.add(tool, args, is_error, body)
    return is_error, body


def _unit_ok(card: dict, unit: str) -> bool:
    if unit == "SALES_UNIT":
        return True
    size = card.get("size_from_name")
    if unit == "KG" and card.get("sales_unit") == "KG":
        return True
    return bool(size) and size[1] == unit


# Prepared foods that merely mention an ingredient ("beans in onion sauce"): never a raw ingredient.
PREPARED = ("sauce", "soup", "pizza", "flammkuchen", "beans", "sandwich", "pie ", "patty", "casserole",
            "balls", "chips", "crisps", "snack", "calzone", "lasagne 3kg", "stew", "salad", "hartshorn", "cake",
            "margarine", "ice cream", " ice ", "muffin", "cookie", "biscuit", "candy")


def _name_ok(card: dict, n: Need) -> bool:
    name = (card.get("name") or "").lower()
    if any(x in name for x in n.exclude) or any(x in name and x not in " ".join(n.must) for x in PREPARED):
        return False
    if n.must:  # whole words: "butter" is not "butterbeans", but "milk free" is still excluded below
        return any(re.search(rf"(?<!\w){re.escape(m)}(?:s|es)?(?!\w)", name) for m in n.must) and not re.search(
            r"\b(?:" + "|".join(re.escape(m) for m in n.must) + r")[\s-]*free\b", name)
    # an ingredient outside the lexicon: the product name should mention it (English name words or the
    # Finnish stem); the catalog's own ranking decides otherwise
    words = [w for w in re.findall(r"[a-zäöå]{4,}", f"{n.name_en} {n.name_fi}".lower())]
    return any(w[:6] in name for w in words) if words else True


def _search(gw, trace: Trace, n: Need, query: str, mandate: dict, agent_id: str, filtered: bool) -> list[dict]:
    args = {"query": query, "limit": SEARCH_LIMIT, "food_only": True, "agent_id": agent_id}
    claims = sorted(set(n.claims) | set(mandate.get("required_claims") or []))
    if filtered:
        if claims:
            args["claims"] = claims
        if mandate.get("avoid_allergens"):
            args["avoid_allergens"] = mandate["avoid_allergens"]
    is_error, body = _call(gw, trace, "search_products", args)
    return [] if is_error else body.get("products", [])


def find_candidates(gw, trace: Trace, n: Need, mandate: dict, agent_id: str) -> tuple[list[dict], dict | None]:
    """Up to 5 candidates for a need that pass the mandate's allergen/claim filters, the unit and the name check.

    When none pass only because of the mandate, also returns the best unfiltered match (to ask for a
    substitute that the gateway checks against the mandate)."""
    picked: list[dict] = []
    for q in n.queries:
        for c in _search(gw, trace, n, q, mandate, agent_id, filtered=True):
            if (n.bulk_ok or _unit_ok(c, n.unit)) and _name_ok(c, n) and c["id"] not in {p["id"] for p in picked}:
                picked.append(c)
        if len(picked) >= MAX_CANDIDATES:
            break
    if mandate.get("avoid_allergens") and picked:
        # Products with no allergen data would send the whole basket to a human: use them only if
        # nothing with declared allergens fits.
        known = [c for c in picked if (c.get("allergen_check") or {}).get("status") == "safe"]
        if known:
            if len(known) < len(picked):
                n.notes.append(f"{len(picked) - len(known)} candidate(s) without allergen data left out")
            picked = known
    if picked:
        return _fit_packs(picked, n)[:MAX_CANDIDATES], None
    if mandate.get("avoid_allergens") or mandate.get("required_claims") or n.claims:
        for q in n.queries:
            for c in _search(gw, trace, n, q, {}, agent_id, filtered=False):
                if _unit_ok(c, n.unit) and _name_ok(c, n):
                    return [], c
    return [], None


def _pack(card: dict) -> float | None:
    size = card.get("size_from_name")
    return size[0] if size and size[0] else None


def _fit_packs(cards: list[dict], n: Need) -> list[dict]:
    """Leave out packs far bigger than the need (3 kg of pepper for 30 g) when a smaller pack exists.

    The gateway picks the cheapest per kg/L among the candidates; for small needs that would be the bulk pack."""
    if n.unit == "SALES_UNIT" or n.bulk_ok:
        return cards
    sizes = [x for c in cards if (x := _pack(c))]
    if not sizes:
        return cards
    limit = max(4 * n.to_buy, min(sizes))
    kept = [c for c in cards if (_pack(c) or 0) <= limit]
    if len(kept) < 3:  # keep the smallest few, so the gateway still has an alternative if one can't be delivered
        kept = sorted(cards, key=lambda c: _pack(c) or 0)[:min(3, len(cards))]
        kept = [c for c in cards if c in kept]  # back in search order
    if len(kept) < len(cards):
        n.notes.append(f"{len(cards) - len(kept)} bulk pack(s) much bigger than the need left out")
    return kept


def _item_cost(q: dict, idx: int) -> tuple[float, bool]:
    """(total ex VAT of the item's lines, fully covered and orderable)."""
    lines = [l for l in q["lines"] if l.get("item_index") == idx]
    ok = bool(lines) and all(l["orderable"] for l in lines) and not any(
        g.get("item_index") == idx for g in q["uncovered"])
    return round(sum(l["line_total_ex_vat"] or 0 for l in lines), 2), ok


def _trim_waste(gateway, p: "Plan", items: list[dict], dd: str, rounds: int = 2) -> dict | None:
    """The gateway picks the cheapest per kg/L, which for a small need can be a big pack: 4 kg bought for
    0.4 kg needed. Re-quote such items without that product and keep the change only if the item then costs
    less (live prices; the Valio calls are served from the gateway's cache). Updates `items` in place and
    returns the final quote, or None if nothing changed."""
    q = p.quote
    final = None
    frozen: set[int] = set()
    for _ in range(rounds):
        trial = [dict(i) for i in items]
        tried = []
        for idx, item in enumerate(items):
            if idx in frozen or item.get("substitute_for") or item["unit"] == "SALES_UNIT":
                continue
            lines = [l for l in q["lines"] if l.get("item_index") == idx]
            covers = sum((l.get("covers") or {}).get("amount") or 0 for l in lines)
            chosen = {l["product_id"] for l in lines}
            rest = [i for i in item["product_ids"] if i not in chosen]
            if lines and rest and covers > max(1.5 * item["amount"], item["amount"] + 0.3):
                trial[idx]["product_ids"] = rest
                tried.append(idx)
        if not tried:
            break
        err, r = _call(gateway, p.trace, "request_quote", {"agent_id": p.agent_id, "mandate_id": p.mandate_id,
                                                           "delivery_date": dd, "items": trial})
        if err:
            break
        q2 = r["quote"]
        kept = 0
        for idx in tried:
            old_cost, _ = _item_cost(q, idx)
            new_cost, ok = _item_cost(q2, idx)
            if ok and new_cost < old_cost:
                items[idx] = trial[idx]
                kept += 1
            else:
                frozen.add(idx)
        if kept == len(tried):
            q = final = q2
            continue
        if kept == 0:
            break
        # some trims kept, some reverted: one more quote for exactly the kept basket
        err, r = _call(gateway, p.trace, "request_quote", {"agent_id": p.agent_id, "mandate_id": p.mandate_id,
                                                           "delivery_date": dd, "items": items})
        if err:
            break
        q = final = r["quote"]
    return final


def _first_substitute(r: dict, mandate: dict, need: float | None = None) -> dict | None:
    """The gateway's best-ranked option that can be delivered, preferring declared-safe allergens and a pack
    that fits the need (not 3 kg of oregano for 20 g)."""
    safe = [o for o in r.get("options", []) if (o.get("offer") or {}).get("orderable")]
    safe = [o for o in safe if o.get("use_ok") is not False] or safe  # one that suits the dish, if any
    if mandate.get("avoid_allergens"):
        known = [o for o in safe if (o.get("allergen_check") or {}).get("status") == "safe"]
        safe = known or safe
    if need:
        def fits(o):
            c = ((o.get("offer") or {}).get("content_per_sales_unit") or {}).get("amount")
            return c is None or c <= max(4 * need, need + 0.3)
        safe = [o for o in safe if fits(o)] or safe
    return safe[0] if safe else None


def _sub_view(orig_id: str, orig_name: str, o: dict, amount: float, unit: str) -> dict:
    return {"for_product_id": orig_id, "for_name": orig_name, "product_id": o["product_id"], "name": o["name"],
            "tier": o["tier"], "compromises": o["compromises"], "amount": amount, "unit": unit,
            **({"use_reason": o["use_reason"]} if o.get("use_reason") else {})}


# --- the plan ---------------------------------------------------------------------------------------

@dataclass
class Plan:
    request: str
    interpretation: str                # "table" | "llm:<provider>:<model>" | "table (LLM answer invalid)"
    mandate_id: str
    agent_id: str
    spec: MenuSpec | None = None
    needs: list[Need] = field(default_factory=list)
    quote: dict | None = None          # the final quote (after substitutes)
    first_quote: dict | None = None    # set when substitutes led to a revised quote
    substitutes: list[dict] = field(default_factory=list)
    order: dict | None = None
    error: str | None = None
    trace: Trace = field(default_factory=Trace)
    llm: LLMUse = field(default_factory=LLMUse)

    @property
    def delivery_date(self) -> date | None:
        return self.spec.delivery_date if self.spec else None

    def to_dict(self) -> dict:
        q = self.quote
        return {
            "request": self.request,
            "interpretation": self.interpretation,
            "mandate_id": self.mandate_id, "agent_id": self.agent_id,
            "error": self.error,
            "portions": self.spec.portions if self.spec else None,
            "delivery_date": self.delivery_date.isoformat() if self.delivery_date else None,
            "dishes": [{"name": d.name, "portions": d.portions or self.spec.portions,
                        "ingredients": [i.model_dump(exclude={"key"}) for i in d.ingredients]}
                       for d in self.spec.dishes] if self.spec else [],
            "stock_on_hand": [s.model_dump() for s in self.spec.stock_on_hand] if self.spec else [],
            "ingredients": [n.row() for n in self.needs],
            "quote": None if not q else {
                "id": q["id"], "expires_at": q["expires_at"], "total_ex_vat": q["total_ex_vat"],
                "total_inc_vat": q["total_inc_vat"],
                "lines": [{"item_index": l.get("item_index"), "product_id": l["product_id"], "name": l["name"],
                           "quantity": l["quantity"], "sales_unit": l["sales_unit"], "covers": l.get("covers"),
                           "line_total_ex_vat": l["line_total_ex_vat"], "orderable": l["orderable"],
                           "substitute_for": l.get("substitute_for"),
                           "issues": [i["code"] for i in l["issues"]]} for l in q["lines"]],
                "uncovered": [{"item_index": g["item_index"], "uncovered": g["uncovered"], "unit": g["unit"]}
                              for g in q["uncovered"]],
                "mandate_preview": q.get("mandate_preview")},
            "first_quote_id": self.first_quote["id"] if self.first_quote else None,
            "substitutes": self.substitutes,
            "order": None if not self.order else {
                "id": self.order["id"], "status": self.order["status"], "tier": self.order["decision"]["outcome"],
                "total_ex_vat": self.order["total_ex_vat"], "approval_url": self.order.get("approval_url"),
                "findings": [f["message"] for f in self.order["decision"]["findings"] if f["outcome"] != "silent"]},
            "summary": self.summary(),
            "valio_api_calls": self.trace.valio_api_calls,
            "llm": {"calls": self.llm.calls, "prompt_tokens": self.llm.prompt_tokens,
                    "completion_tokens": self.llm.completion_tokens, "errors": self.llm.errors},
            "steps": self.trace.steps,
        }

    def summary(self) -> str:
        if self.error:
            return f"No plan: {self.error}"
        s = self.spec
        dd = self.delivery_date
        parts = [f"{' + '.join(d.name.split(' (')[0] for d in s.dishes)} for {s.portions}"
                 + (f", delivery {WEEKDAYS[dd.weekday()]} {dd:%d.%m.%Y}" if dd else "") + "."]
        covered = [n for n in self.needs if n.status == "covered_by_stock"]
        if covered:
            parts.append("Already on hand: " + ", ".join(f"{n.name_en} ({_fmt(n.needed, n.unit)} needed)"
                                                          for n in covered) + ".")
        q = self.quote
        if q:
            prev = q.get("mandate_preview") or {}
            fired = [f["message"] for f in prev.get("findings", []) if f["outcome"] != "silent"]
            parts.append(f"Quote {q['id']}: {len(q['lines'])} lines, €{q['total_ex_vat']:,.2f} ex VAT. "
                         f"Mandate preview: {prev.get('outcome', 'n/a')}"
                         + (f" ({'; '.join(fired[:3])})" if fired and prev.get("outcome") != "silent" else "") + ".")
        for sub in self.substitutes:
            parts.append(f"Substitute: {sub['name']} for {sub['for_name']}"
                         + (f" ({'; '.join(sub['compromises'][:2])})" if sub["compromises"] else "") + ".")
        missing = [n for n in self.needs if n.status in ("not_found", "ruled_out_by_mandate", "uncovered",
                                                         "not_orderable")
                   and not n.substitute]
        if missing:
            parts.append("Not covered: " + ", ".join(f"{n.name_en} ({n.status.replace('_', ' ')})"
                                                    for n in missing) + ".")
        if self.order:
            o = self.order
            st = {"ordered": "ordered", "pending_approval": "waiting for human approval", "refused": "refused"}.get(
                o["status"], o["status"])
            parts.append(f"Order {o['id']}: {st} (tier {o['decision']['outcome']})"
                         + (f", approve at {o['approval_url']}" if o.get("approval_url") else "") + ".")
        return " ".join(parts)


def _fmt(amount: float, unit: str) -> str:
    if amount == math.inf:
        return "plenty"
    u = {"KG": "kg", "L": "L", "SALES_UNIT": "pcs"}.get(unit, unit)
    digits = 3 if amount < 0.1 else 2
    return f"{amount:,.{digits}f}".rstrip("0").rstrip(".") + f" {u}"


def interpret(text: str, today: date, llm=None, use: LLMUse | None = None) -> tuple[MenuSpec, str]:
    """(spec, which path). With an LLM: the model; if its answer is invalid twice, the table if it knows the dish."""
    if llm is None:
        return interpret_table(text, today), "table"
    label = getattr(getattr(llm, "cfg", None), "label", "llm")
    try:
        return interpret_llm(text, today, llm, use), f"llm:{label}"
    except InterpretError as e:
        try:
            return interpret_table(text, today), f"table (LLM {label} answer invalid: {str(e)[:120]})"
        except InterpretError:
            raise e from None


def plan(request_text: str, *, gateway, mandate_id: str, agent_id: str, llm=None,
         delivery_date: date | None = None, today: date | None = None, place: bool = False,
         substitutes: bool = True) -> Plan:
    """Request text -> recipe -> ingredient quantities -> catalog candidates -> ONE basket quote (-> order).

    gateway: anything with call(tool, args) -> (is_error, body), e.g. agent.buyer.MCPGateway.
    llm: a ChatClient (or anything with chat(messages) -> response with .content and .usage); None = table.
    """
    today = today or helsinki_today()
    p = Plan(request=request_text, interpretation="", mandate_id=mandate_id, agent_id=agent_id)
    try:
        p.spec, p.interpretation = interpret(request_text, today, llm, p.llm)
    except InterpretError as e:
        p.interpretation = "table" if llm is None else f"llm:{getattr(getattr(llm, 'cfg', None), 'label', 'llm')}"
        p.error = str(e)
        return p
    if delivery_date:
        p.spec.delivery_date = delivery_date
    elif not p.spec.delivery_date or p.spec.delivery_date <= today:
        p.spec.delivery_date = next_delivery_day(today)
    dd = p.spec.delivery_date.isoformat()

    is_error, m = _call(gateway, p.trace, "get_mandate", {"mandate_id": mandate_id})
    if is_error:
        p.error = f"mandate: {m.get('message')}"
        return p
    mandate = m["mandate"]
    p.needs = compute_needs(p.spec)

    # catalog candidates per ingredient (local search, no Valio calls)
    items: list[dict] = []
    sub_calls = 0
    for n in p.needs:
        if n.to_buy <= 0:
            continue
        n.candidates, blocked = find_candidates(gateway, p.trace, n, mandate, agent_id)
        if n.candidates:
            n.items = [len(items)]
            n.status = "quoted"
            items.append({"product_ids": [c["id"] for c in n.candidates], "amount": n.to_buy, "unit": n.unit,
                          "note": n.name_fi, "purpose": _purpose(n)})
        elif blocked and substitutes and sub_calls < MAX_SUBSTITUTE_CALLS:
            # nothing meets the mandate's allergen/claim rules: ask the gateway for a substitute it has checked
            n.status = "ruled_out_by_mandate"
            sub_calls += 1
            err, r = _call(gateway, p.trace, "suggest_substitutes", {
                "agent_id": agent_id, "mandate_id": mandate_id, "product_id": blocked["id"], "delivery_date": dd,
                "amount": n.to_buy, "unit": n.unit, "purpose": _purpose(n)})
            o = None if err else _first_substitute(r, mandate, n.to_buy)
            if o:
                n.substitute = _sub_view(blocked["id"], blocked["name"], o, n.to_buy, n.unit)
                p.substitutes.append(n.substitute)
                n.items = [len(items)]
                items.append({"product_ids": [o["product_id"]], "amount": n.to_buy, "unit": n.unit,
                              "substitute_for": blocked["id"], "note": n.name_fi, "purpose": _purpose(n)})
            else:
                n.notes.append(f"no product meets the mandate; closest match {blocked['name']} is ruled out")
        else:
            n.status = "ruled_out_by_mandate" if blocked else "not_found"
    if not items:
        p.error = "Nothing to buy" if all(n.status == "covered_by_stock" for n in p.needs) else \
            "No catalog product matched any ingredient."
        return p

    is_error, r = _call(gateway, p.trace, "request_quote",
                        {"agent_id": agent_id, "mandate_id": mandate_id, "delivery_date": dd, "items": items})
    if is_error:
        p.error = f"quote: {r.get('message')}"
        return p
    p.quote = r["quote"]
    p.quote = _trim_waste(gateway, p, items, dd) or p.quote

    # uncovered (short stock, nothing orderable): a substitute for the rest, then one revised quote
    if substitutes:
        entries: list[tuple[Need, dict]] = []  # the revised basket
        changed = False
        owner = {i: n for n in p.needs for i in n.items}
        for idx, item in enumerate(items):
            n = owner[idx]
            lines = [l for l in p.quote["lines"] if l.get("item_index") == idx]
            gap = next((g for g in p.quote["uncovered"] if g.get("item_index") == idx), None)
            if not gap and lines and all(l["orderable"] for l in lines):
                entries.append((n, item))
                continue
            n.status = "uncovered"
            undeliverable = bool(lines) and not any(l["orderable"] for l in lines) and not gap

            def keep_or_drop():
                nonlocal changed
                if undeliverable:
                    # Nothing to buy for this ingredient on this date; leaving it in would block the whole
                    # basket. It is listed as not orderable for the human; the rest of the basket goes ahead.
                    reasons = sorted({i["message"] for l in lines for i in l["issues"] if i["severity"] == "blocking"})
                    n.status = "not_orderable"
                    n.notes.append("left out of the basket: " + "; ".join(reasons)[:300])
                    changed = True
                else:
                    entries.append((n, item))

            if item.get("substitute_for") or sub_calls >= MAX_SUBSTITUTE_CALLS:
                keep_or_drop()
                continue
            ok = [l for l in lines if l["orderable"]]
            covered = sum((l.get("covers") or {}).get("amount") or 0 for l in ok)
            missing = round_up(gap["uncovered"] if gap else item["amount"] - covered, item["unit"])
            orig = (lines[0]["product_id"], lines[0]["name"]) if lines else (n.candidates[0]["id"], n.candidates[0]["name"])
            sub_calls += 1
            err, s = _call(gateway, p.trace, "suggest_substitutes", {
                "agent_id": agent_id, "mandate_id": mandate_id, "product_id": orig[0], "delivery_date": dd,
                "amount": missing, "unit": item["unit"], "purpose": _purpose(n)})
            o = None if err else _first_substitute(s, mandate, missing)
            if not o:
                if not undeliverable:
                    n.notes.append("no safe substitute; the gap stays in the quote for a human")
                keep_or_drop()
                continue
            n.substitute = _sub_view(orig[0], orig[1], o, missing, item["unit"])
            p.substitutes.append(n.substitute)
            changed = True
            if ok and covered > 0:  # keep what the original products cover, substitute only the rest
                entries.append((n, {**item, "product_ids": [l["product_id"] for l in ok], "amount": round(covered, 3)}))
            entries.append((n, {"product_ids": [o["product_id"]], "amount": missing, "unit": item["unit"],
                                "substitute_for": orig[0], "note": f"{n.name_fi} (substitute)",
                                "purpose": _purpose(n)}))
        if changed and entries:
            kept = [it for _, it in entries]
            err, r2 = _call(gateway, p.trace, "request_quote",
                            {"agent_id": agent_id, "mandate_id": mandate_id, "delivery_date": dd, "items": kept})
            if not err:
                p.first_quote, p.quote = p.quote, r2["quote"]
                for n in p.needs:
                    n.items = [i for i, (owner_n, _) in enumerate(entries) if owner_n is n]
                    if n.substitute and n.status == "uncovered":
                        n.status = "quoted_with_substitute"

    if place:
        qid = p.quote["id"]
        is_error, o = _call(gateway, p.trace, "place_order", {"agent_id": agent_id, "mandate_id": mandate_id,
                                                              "quote_id": qid, "idempotency_key": f"{agent_id}:{qid}"})
        if is_error:
            p.error = f"order: {o.get('message')}"
        else:
            p.order = o
    return p


# --- rendering ----------------------------------------------------------------------------------------

def render(p: Plan) -> str:
    """The reasoning chain for a human: recipe -> ingredients -> products -> quote -> mandate -> order."""
    out = [f"Request: {p.request}", f"Interpreted by: {p.interpretation}"
           + (" (curated recipe table, no model)" if p.interpretation == "table" else "")]
    if p.spec is None:
        out.append(f"No plan: {p.error}")
        return "\n".join(out)
    s = p.spec
    out.append(f"\n1. Recipe: {s.portions} portions, delivery {s.delivery_date.isoformat() if s.delivery_date else '?'}")
    for d in s.dishes:
        out.append(f"   {d.name} x {d.portions or s.portions}: "
                   + ", ".join(f"{i.name_fi} {i.amount_per_portion:g} {i.unit}" for i in d.ingredients))
    if s.stock_on_hand:
        out.append("   On hand: " + ", ".join("plenty of " + x.ingredient if x.plenty or x.amount is None
                                              else f"{x.amount:g} {x.unit} {x.ingredient}" for x in s.stock_on_hand))
    out.append("\n2. Ingredients (scaled, minus stock, rounded up)")
    out.append(f"   {'ingredient':<26}{'needed':>11}{'on hand':>11}{'to buy':>11}  status")
    for n in p.needs:
        oh = "-" if n.on_hand is None else _fmt(n.on_hand, n.unit)
        out.append(f"   {n.name_fi[:25]:<26}{_fmt(n.needed, n.unit):>11}{oh:>11}{_fmt(n.to_buy, n.unit):>11}  "
                   f"{n.status}" + (f" [{', '.join(n.claims)}]" if n.claims else ""))
    out.append("\n3. Products (catalog candidates per ingredient; the gateway picks the cheapest orderable per kg/L)")
    q = p.quote
    lines_by_item: dict[int, list[dict]] = {}
    for l in (q or {}).get("lines", []):
        lines_by_item.setdefault(l.get("item_index"), []).append(l)
    for n in p.needs:
        if n.status == "covered_by_stock":
            continue
        chosen = [l for i in n.items for l in lines_by_item.get(i, [])]
        picks = ", ".join("{:g} {} {} (€{:,.2f})".format(l["quantity"], l["sales_unit"], l["name"],
                                                         l["line_total_ex_vat"] or 0) for l in chosen)
        out.append(f"   {n.name_fi}: {len(n.candidates)} candidate(s)" + (f" -> {picks}" if picks else ""))
        if n.substitute:
            sub = n.substitute
            out.append(f"      substitute: {sub['name']} for {sub['for_name']} [{sub['tier']}]"
                       + (f": {'; '.join(sub['compromises'][:2])}" if sub["compromises"] else ""))
        for note in n.notes:
            out.append(f"      note: {note}")
    if q:
        prev = q.get("mandate_preview") or {}
        out.append(f"\n4. Quote {q['id']}: €{q['total_ex_vat']:,.2f} ex VAT (€{q['total_inc_vat']:,.2f} incl.), "
                   f"{len(q['lines'])} lines, expires {q['expires_at'][11:16]}"
                   + (f" (revised from {p.first_quote['id']} with substitutes)" if p.first_quote else ""))
        for g in q["uncovered"]:
            out.append(f"   uncovered: item {g['item_index']} ({(g.get('item') or {}).get('note')}): "
                       f"{g['uncovered']:g} {g['unit']}")
        out.append(f"\n5. Mandate preview ({p.mandate_id}): {prev.get('outcome')}")
        for f in prev.get("findings", []):
            if f["outcome"] != "silent" or f["rule"] == "mandate.ok":
                out.append(f"   [{f['outcome']}] {f['rule']}: {f['message']}")
    if p.order:
        o = p.order
        out.append(f"\n6. Order {o['id']}: {o['status']} (tier {o['decision']['outcome']})"
                   + (f"\n   approve at {o['approval_url']}" if o.get("approval_url") else ""))
    elif q:
        out.append("\n6. Not ordered (run with --order to place it under the mandate)")
    if p.error:
        out.append(f"\nError: {p.error}")
    out.append(f"\nSummary: {p.summary()}")
    out.append(f"Valio API calls: {p.trace.valio_api_calls}; gateway tool calls: {len(p.trace.steps)}"
               + (f"; LLM calls: {p.llm.calls}" if p.llm.calls else ""))
    return "\n".join(out)


# --- CLI ------------------------------------------------------------------------------------------------

def looks_like_menu(text: str) -> bool:
    """A request about dishes and portions (for agent.buyer to route here) rather than named products."""
    t = _norm(text)
    if any(re.search(rf"(?<!\w){re.escape(_norm(a))}", t) for r in table()["recipes"].values() for a in r["aliases"]):
        return parse_portions(text) is not None
    return bool(re.search(r"\b(menu|recipe|resepti|ruokalista|dish|dishes)\b", t)) and parse_portions(text) is not None


def make_llm(provider: str | None, model: str | None = None):
    """A ChatClient for the provider, or None for the table (also when no key is configured)."""
    if provider == "table":
        return None
    from .llm import ChatClient, config_from_env, key_present
    cfg = config_from_env(provider, model)
    if cfg.provider != "local" and not key_present(cfg.provider):
        print(f"No API key for {cfg.provider}: using the recipe table instead.", file=sys.stderr)
        return None
    return ChatClient(cfg)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Plan a menu into one basket quote (and optionally order it).")
    ap.add_argument("request", help='e.g. "Lasagne for 120 on Monday; we have 10 kg flour"')
    ap.add_argument("--mandate", required=True)
    ap.add_argument("--agent", required=True)
    ap.add_argument("--provider", default=None,
                    help="table | gemini | verda | local (default: $LLM_PROVIDER, or table without a key)")
    ap.add_argument("--model")
    ap.add_argument("--gateway", default="local",
                    help="'local' (in-process gateway on this machine's data) or a gateway base URL")
    ap.add_argument("--date", help="delivery date YYYY-MM-DD (overrides the request)")
    ap.add_argument("--order", action="store_true", help="place the order under the mandate")
    ap.add_argument("--no-substitutes", action="store_true")
    ap.add_argument("--json", action="store_true", help="print the plan as JSON")
    a = ap.parse_args(argv)
    import os
    # Gemini is the default brain when its key is set; the curated table otherwise (offline demos, tests)
    provider = a.provider or os.environ.get("LLM_PROVIDER") or ("gemini" if os.environ.get("GEMINI_API_KEY")
                                                                else "table")
    llm = make_llm(provider, a.model)
    from .buyer import MCPGateway
    if a.gateway == "local":
        from sous.mcp_server import mcp
        target = mcp
    else:
        target = a.gateway
    with MCPGateway(target) as gw:
        p = plan(a.request, gateway=gw, mandate_id=a.mandate, agent_id=a.agent, llm=llm,
                 delivery_date=date.fromisoformat(a.date) if a.date else None, place=a.order,
                 substitutes=not a.no_substitutes)
    print(json.dumps(p.to_dict(), ensure_ascii=False, indent=1, default=str) if a.json else render(p))
    return 1 if p.error and p.spec is None else 0


if __name__ == "__main__":
    sys.exit(main())
