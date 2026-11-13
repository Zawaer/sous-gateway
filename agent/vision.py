"""Snap a photo of the order: NVIDIA Nemotron 3.5 Super VL reads a kitchen's paper, the agent turns it into a quote.

A kitchen's real inputs are often paper: a handwritten order list taped to the cold-room door, next week's
printed menu, a supplier's delivery note with shortfalls. The chain, and who does what:

    photo --(Nemotron VL: reading only)--> strict JSON: kind, lines as written + quantity/unit + confidence,
                                           dishes/portions, delivery date, what it could not read
          --(code)--> cross-check every line against its own text (the number and unit written on it);
                      a mismatch or low confidence -> (Nemotron, one targeted second look at just those lines)
          --(code)--> order_list:    lines -> catalog search (Finnish first) -> ONE basket quote
                      menu:          dishes + portions -> agent/recipe.py's planner -> ONE basket quote
                      delivery_note: ordered - delivered = shortfall -> ONE basket quote (+ substitutes)
          --(gateway)--> the mandate engine previews / decides: ordered, held for a human, or refused

Low-confidence and unreadable lines are never ordered: they are listed for a human to confirm. The model
never sees prices or decides anything; the mandate engine still decides everything else.

    uv run python -m agent.vision --provider nvidia --mandate M-HOTEL-AURORA --agent aurora-purchasing-agent \\
        evals/vision/images/01_order_handwritten_fi.jpg [--order]
    uv run python -m agent.vision --read-only photo.jpg            # just the reading, as JSON
    uv run python -m agent.vision --from-json read.json ...        # replay a reading (no model call)
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from . import recipe as R
from .buyer import WEEKDAYS, helsinki_today, next_delivery_day

CONFIRM_BELOW = 0.75        # lines read with less confidence go to a human, not into the basket
MAX_SIDE = 1600             # px; phone photos are downscaled before upload (latency, token cost)
MAX_SUBSTITUTE_CALLS = 3

# Written unit -> canonical unit. Finnish kitchen shorthand first (ltk = laatikko, plo = pullo, ...).
UNIT_ALIASES = {
    "kg": "kg", "kilo": "kg", "kiloa": "kg", "kilos": "kg", "kgs": "kg",
    "g": "g", "gr": "g", "grammaa": "g",
    "l": "l", "ltr": "l", "litra": "l", "litraa": "l", "liter": "l", "litre": "l", "litres": "l", "liters": "l",
    "lit": "l",
    "dl": "dl", "ml": "ml", "cl": "cl",
    "kpl": "kpl", "pcs": "kpl", "pc": "kpl", "st": "kpl", "stk": "kpl", "kappale": "kpl", "kappaletta": "kpl",
    "pieces": "kpl", "x": "kpl",
    "ltk": "ltk", "laatikko": "ltk", "laatikkoa": "ltk", "box": "ltk", "boxes": "ltk", "lda": "ltk", "låda": "ltk",
    "krt": "ltk", "kr": "ltk",
    "plo": "plo", "pullo": "plo", "pulloa": "plo", "bottle": "plo", "bottles": "plo", "fl": "plo", "flaska": "plo",
    "pkt": "pkt", "paketti": "pkt", "pakettia": "pkt", "pack": "pkt", "packs": "pkt", "pkg": "pkt", "fp": "pkt",
    "rs": "rs", "rasia": "rs", "rasiaa": "rs", "tray": "rs",
    "prk": "prk", "purkki": "prk", "purkkia": "prk", "tlk": "prk", "tölkki": "prk", "can": "prk", "tin": "prk",
    "ps": "ps", "pussi": "ps", "pussia": "ps", "bag": "ps", "påse": "ps",
    "sk": "sk", "säkki": "sk", "säkkiä": "sk", "sack": "sk", "säck": "sk",
    "pll": "pll", "lava": "pll", "pallet": "pll",
}
Unit = Literal["kg", "g", "l", "dl", "cl", "ml", "kpl", "ltk", "plo", "pkt", "rs", "prk", "ps", "sk", "pll"]
ARTICLE = {"order_list": "an order list", "menu": "a menu", "delivery_note": "a delivery note"}
PACK_UNITS = {"ltk", "plo", "pkt", "rs", "prk", "ps", "sk", "pll"}
_TO_GATEWAY = {"kg": ("KG", 1.0), "g": ("KG", 0.001), "l": ("L", 1.0), "dl": ("L", 0.1), "cl": ("L", 0.01),
               "ml": ("L", 0.001)}


def norm_unit(v) -> str | None:
    if v is None:
        return None
    u = str(v).strip().lower().rstrip(".")
    return UNIT_ALIASES.get(u) or (u if u in UNIT_ALIASES.values() else None)


# --- the reading schema (what the model must return) ----------------------------------------------

class ReadItem(BaseModel):
    text_as_written: str = Field(min_length=1, max_length=300)
    name_fi: str | None = None
    name_en: str | None = None
    quantity: float | None = Field(None, ge=0, le=1_000_000)          # ordered quantity on a delivery note
    unit: Unit | None = None
    delivered_quantity: float | None = Field(None, ge=0, le=1_000_000)  # delivery notes only
    confidence: float = Field(ge=0, le=1)
    note: str | None = None

    @field_validator("unit", mode="before")
    @classmethod
    def _unit(cls, v):
        return norm_unit(v) if v not in (None, "") else None

    @field_validator("quantity", "delivered_quantity", mode="before")
    @classmethod
    def _num(cls, v):
        if isinstance(v, str):  # "2,5" (Finnish decimal comma)
            v = v.strip().replace(",", ".")
            return float(v) if re.fullmatch(r"\d+(\.\d+)?", v) else None
        return v

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v):
        if isinstance(v, str):
            s = v.strip().lower().rstrip("%")
            v = {"high": 0.9, "medium": 0.6, "low": 0.3}.get(s, s)
            v = float(v) if re.fullmatch(r"\d+(\.\d+)?", v) else v
        if isinstance(v, (int, float)) and 1 < v <= 100:  # a percentage
            v = v / 100
        return v


class ReadDish(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    portions: int | None = Field(None, gt=0, le=100000)
    day: str | None = None
    confidence: float = Field(0.9, ge=0, le=1)


class ImageRead(BaseModel):
    kind: Literal["order_list", "menu", "delivery_note"]
    items: list[ReadItem] = Field(default_factory=list, max_length=80)
    dishes: list[ReadDish] = Field(default_factory=list, max_length=30)
    delivery_date: date | None = None
    notes: str | None = None
    unreadable: list[str] = Field(default_factory=list)

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, v):
        v = str(v or "").strip().lower().replace(" ", "_").replace("-", "_")
        return {"order": "order_list", "list": "order_list", "shopping_list": "order_list", "tilauslista": "order_list",
                "ruokalista": "menu", "delivery": "delivery_note", "lähete": "delivery_note",
                "packing_slip": "delivery_note", "invoice": "delivery_note"}.get(v, v)

    @field_validator("delivery_date", mode="before")
    @classmethod
    def _date(cls, v):
        return None if v in ("", "null") else v

    @field_validator("unreadable", mode="before")
    @classmethod
    def _unreadable(cls, v):
        return [str(x if not isinstance(x, dict) else x.get("text") or json.dumps(x, ensure_ascii=False))
                for x in (v or [])]


# --- prompts (versioned: scripts/vision_eval.py compares them) ------------------------------------

SCHEMA = """{"kind": "order_list" | "menu" | "delivery_note",
 "items": [{"text_as_written": str, "name_fi": str|null, "name_en": str|null, "quantity": number|null,
            "unit": "kg"|"g"|"l"|"dl"|"cl"|"ml"|"kpl"|"ltk"|"plo"|"pkt"|"rs"|"prk"|"ps"|"sk"|"pll"|null,
            "delivered_quantity": number|null, "confidence": number, "note": str|null}],
 "dishes": [{"name": str, "portions": int|null, "day": str|null, "confidence": number}],
 "delivery_date": "YYYY-MM-DD"|null, "notes": str|null, "unreadable": [str]}"""


def prompt_v1(today: date) -> str:
    """The baseline: what a first attempt looks like (kept for the eval's before/after)."""
    return f"""Read this image of a document from a restaurant kitchen and extract the order.
Return JSON with this shape: {SCHEMA}
confidence is between 0 and 1. Today is {today.isoformat()}."""


def prompt_v2(today: date) -> str:
    days = ", ".join(f"{WEEKDAYS[d.weekday()]} {d.isoformat()}" for d in (today + timedelta(days=i) for i in range(1, 8)))
    return f"""You read photos of paper from professional kitchens in Finland for a food wholesaler's purchasing \
agent. You only transcribe and structure what is written. You do not choose products, prices or suppliers, and \
you never add, merge or "fix" lines.

Today is {WEEKDAYS[today.weekday()]} {today.isoformat()}; the coming days: {days}.

1. kind: "order_list" (a list of goods to buy, often handwritten, e.g. "Tilaus", "Tilattavat", "Beställning"),
   "menu" (dishes, often per weekday, with portions/annosta/hlö/pax), or "delivery_note" (a supplier's
   lähete / följesedel / packing slip with ordered vs delivered columns, "Tilattu"/"Toimitettu", "Puuttuu").
2. items: one per written line with a product, top to bottom. Skip headings, signatures and crossed-out lines.
   - text_as_written: the line exactly as written (keep spelling, abbreviations and the number).
   - name_fi: the plain Finnish product name in base form (singular nominative), e.g. "maito laktoositon",
     "kananmuna", "peruna", "kuohukerma"; translate Swedish/English lines (mjölk -> maito, smör -> voi,
     grädde -> kerma). name_en: the English name ("lactose-free milk"). Keep qualifiers that change the
     product: laktoositon/laktosfri (lactose-free), gluteeniton (gluten-free), luomu (organic), pakaste (frozen).
   - quantity and unit: the number and unit written on that line. Finnish decimals use a comma: "2,5 kg" = 2.5.
     Units: kg, g, l (litra, ltr), dl, ml, kpl (pieces, st, stk, x), ltk (laatikko, box), plo (pullo, bottle),
     pkt (paketti, pack), rs (rasia, tray), prk (purkki, can, tlk), ps (pussi, bag), sk (säkki, sack).
     Do not convert units. No unit written -> unit null. No number written -> quantity null.
   - delivery_note only: quantity = ordered, delivered_quantity = delivered. If only a shortfall is
     written ("puuttuu 16 l"), set delivered_quantity = ordered - shortfall.
   - confidence (0-1): how sure you are of the WHOLE line (product, number and unit). Handwriting you can
     read clearly: 0.9+. A digit that could be another digit (1/7, 4/9, 0/6, 3/8), a smudged or ambiguous
     unit, a word you are guessing: below 0.6, and say what is unclear in note. Never guess silently: a
     low-confidence line is checked by a human, a wrong confident line is ordered.
3. unreadable: lines you cannot read at all (describe them, e.g. "line 6: smudged, maybe 'jogurtti'").
4. dishes (menus only): each dish with its portions (annosta, hlö, henkeä, pax, port.); day as written.
5. delivery_date: the delivery/order date written on the page ("toimitus", "leverans", "pvm"), as YYYY-MM-DD
   (a weekday means the next such date in the list above; "16.11." means this year); else null.
6. notes: anything else relevant on the page (e.g. "toimitus aamulla", allergy remarks), else null.

Return ONLY one JSON object, no prose, no markdown, exactly this shape:
{SCHEMA}"""


def prompt_v3(today: date) -> str:
    """v2 plus fixes for what Nemotron actually got wrong in eval run 1 (docs/NEMOTRON.md): headings and
    signatures listed as items, table rows transcribed without their numbers, a red-pen correction listed as
    its own line, menu portions written once in the heading, the "toimitettava" date on a menu."""
    days = ", ".join(f"{WEEKDAYS[d.weekday()]} {d.isoformat()}" for d in (today + timedelta(days=i) for i in range(1, 8)))
    return f"""You read photos of paper from professional kitchens in Finland for a food wholesaler's purchasing \
agent. You only transcribe and structure what is written. You do not choose products, prices or suppliers, and \
you never add, merge or "fix" lines.

Today is {WEEKDAYS[today.weekday()]} {today.isoformat()}; the coming days: {days}.

1. kind: "order_list" (a list of goods to buy, often handwritten, e.g. "Tilaus", "Tilattavat", "Beställning"),
   "menu" (dishes, often per weekday, with portions/annosta/hlö/pax), or "delivery_note" (a supplier's
   lähete / följesedel / packing slip with ordered vs delivered columns, "Tilattu"/"Toimitettu", "Puuttuu").
2. items: ONLY lines that name a product. Everything else stays out of items:
   - headings and titles ("Tilaus - toimitus ma 16.11.", "Kylmiö / tilattavat"), dates, signatures ("- Ville"),
     column headers ("Tuote", "Tilattu") and remarks go to notes;
   - a crossed-out (struck-through) line was cancelled: leave it out of items and say so in notes;
   - menus have no items (dishes go to dishes).
   For each product line, top to bottom:
   - text_as_written: the line exactly as written, including its number and unit. For a table row (delivery
     notes, printed orders with columns) write the whole row, cells joined with " | ", e.g.
     "Maito laktoositon 1 l | 40 | 24 | l". A pack size inside the product name ("1 l", "500 g") is part of
     the name, NOT the quantity: the quantity is in its own column.
   - name_fi: the plain Finnish product name in base form (singular nominative), e.g. "maito laktoositon",
     "kananmuna", "peruna", "kuohukerma"; translate Swedish/English lines (mjölk -> maito, smör -> voi,
     grädde -> kerma). name_en: the English name ("lactose-free milk"). Keep qualifiers that change the
     product: laktoositon/laktosfri (lactose-free), gluteeniton (gluten-free), luomu (organic), pakaste (frozen).
   - quantity and unit: the number and unit ordered on that line. Finnish decimals use a comma: "2,5 kg" = 2.5.
     Units: kg, g, l (litra, ltr), dl, ml, kpl (pieces, st, stk, x), ltk (laatikko, box), plo (pullo, bottle),
     pkt (paketti, pack), rs (rasia, tray), prk (purkki, can, tlk), ps (pussi, bag), sk (säkki, sack).
     Do not convert units. No unit written -> unit null. No number written -> quantity null.
   - delivery_note only: quantity = ordered ("Tilattu"), delivered_quantity = delivered ("Toimitettu").
     A handwritten correction next to a row (a struck-through number, "puuttuu 3 l" = 3 l missing) belongs to
     THAT row: apply it (delivered = ordered - missing), add it to that row's text_as_written after " | ",
     and do not list it as a line of its own.
   - confidence (0-1): how sure you are of the WHOLE line (product, number and unit). Clear print or
     handwriting: 0.9+. A digit that could be another digit (1/7, 4/9, 0/6, 3/8), a smudged or ambiguous
     unit, a word you are guessing: below 0.6, and say what is unclear in note. Never guess silently: a
     low-confidence line is checked by a human, a wrong confident line is ordered.
3. unreadable: product lines you cannot read at all (describe them, e.g. "line 7: smudged, maybe 'juusto'").
4. dishes (menus only): each dish with its portions (annosta, hlö, henkeä, pax, port.); day as written. A
   portion count written once for the whole menu (e.g. in the heading "Pikkujoulut la 21.11. - 60 hlö")
   applies to every dish that has no count of its own.
5. delivery_date as YYYY-MM-DD (a weekday means the next such date in the list above; "16.11." means this
   year): order lists and menus: the date the goods must arrive ("toimitus", "toimitettava", "mennessä",
   "leverans", "for Monday"); a menu with no such date: the event date (e.g. "la 21.11."). Delivery notes:
   the delivery date printed on the note. Else null.
6. notes: everything else relevant on the page (headings, signatures, crossed-out lines, remarks), else null.

Return ONLY one JSON object, no prose, no markdown, exactly this shape:
{SCHEMA}"""


PROMPTS = {"v1": prompt_v1, "v2": prompt_v2, "v3": prompt_v3}
DEFAULT_PROMPT = "v3"


def verify_prompt(lines: list[tuple[int, ReadItem, str]]) -> str:
    rows = "\n".join(f'- index {i}: "{it.text_as_written}" read as quantity={it.quantity} unit={it.unit}'
                     + (f" delivered={it.delivered_quantity}" if it.delivered_quantity is not None else "")
                     + f" (confidence {it.confidence:.2f}; why re-check: {why})" for i, it, why in lines)
    loose = next((why for _, _, why in lines if why.startswith("the page says")), None)
    pen = "" if not loose else f"""
A handwritten correction on the page ({loose.split("'")[1]}) is not applied to any row yet. Find where it is
written: next to which row, in which column, and whether a printed number there is struck through. Apply it to
THAT row only (delivered_quantity = ordered - missing) and add it to that row's text_as_written after " | ".
Leave the other rows as they are. If you cannot tell which row it belongs to, set confidence below 0.6.
"""
    return f"""Look at the same image again. These lines need a careful second reading:
{rows}
{pen}
For each one, read the line on the image again, character by character: the digits, the decimal comma, the
unit. Return ONLY one JSON object: {{"lines": [{{"index": int, "text_as_written": str, "quantity": number|null,
"unit": str|null, "delivered_quantity": number|null, "confidence": number, "note": str|null}}]}}
Lower the confidence below 0.6 if the line really is ambiguous; do not raise it unless you are now sure."""


# --- images -----------------------------------------------------------------------------------------

def _mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "image/heic"
    raise ValueError("Not a JPEG, PNG, WebP or HEIC image.")


def prepare_image(src: str | Path | bytes, max_side: int = MAX_SIDE) -> tuple[bytes, str]:
    """(bytes, mime) ready to send: EXIF-rotated, RGB, longest side <= max_side, JPEG (PNG kept when small)."""
    data = src if isinstance(src, (bytes, bytearray)) else Path(src).read_bytes()
    data = bytes(data)
    mime = _mime(data)
    try:
        from PIL import Image, ImageOps
    except ImportError:  # Pillow missing: send as is (JPEG/PNG only)
        if mime not in ("image/jpeg", "image/png"):
            raise ValueError(f"{mime} needs Pillow to convert") from None
        return data, mime
    img = Image.open(io.BytesIO(data))
    rotated = img.getexif().get(0x0112, 1) not in (None, 1)  # EXIF orientation: phone photos are often sideways
    if (not rotated and max(img.size) <= max_side and mime in ("image/jpeg", "image/png")
            and len(data) < 1_500_000):
        return data, mime
    img = ImageOps.exif_transpose(img)
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=88)
    return out.getvalue(), "image/jpeg"


def image_part(data: bytes, mime: str) -> dict:
    """An OpenAI-style image content part (NVIDIA's API takes base64 data URLs)."""
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}


# --- reading -------------------------------------------------------------------------------------------

class VisionError(ValueError):
    pass


@dataclass
class VisionRead:
    read: ImageRead | None
    prompt_version: str
    model: str
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    repaired: bool = False                                  # the first answer was invalid JSON, the retry fixed it
    rechecked: list[dict] = field(default_factory=list)     # lines the second look re-read (before -> after)
    flags: list[dict] = field(default_factory=list)         # cross-check findings left after the second look
    errors: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)        # headings/signatures the model listed as lines
    raw: str = ""

    def to_dict(self) -> dict:
        return {"read": self.read.model_dump(mode="json") if self.read else None,
                "prompt_version": self.prompt_version, "model": self.model, "calls": self.calls,
                "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "latency_ms": self.latency_ms, "repaired": self.repaired, "rechecked": self.rechecked,
                "flags": self.flags, "ignored": self.ignored, "errors": self.errors}


def extract_json(s: str) -> dict:
    s = re.sub(r"<think>.*?</think>", "", s or "", flags=re.S).strip()
    if s.startswith("```"):
        s = re.sub(r"^```\w*\s*|\s*```$", "", s)
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("no JSON object in the answer")
    return json.loads(s[i:j + 1])


def _numbers(text: str) -> list[float]:
    return [float(m.replace(",", ".")) for m in re.findall(r"(?<![\w,.])\d+(?:[.,]\d+)?", text)]


def _written_units(text: str) -> set[str]:
    """The unit tokens written right after a number on the line ("40 l", "5kg", "2 ltk")."""
    return {u for m in re.finditer(r"\d[\d.,]*\s*([a-zA-ZäöåÄÖÅ]{1,10})\.?(?![a-zA-ZäöåÄÖÅ])", text)
            if (u := norm_unit(m[1]))}


def cross_check(it: ReadItem, kind: str) -> str | None:
    """Code checks the model's numbers against its own transcription of the line. Returns why it disagrees."""
    nums = _numbers(it.text_as_written)
    if it.quantity is None:
        return "no quantity read"
    if nums and not any(abs(n - it.quantity) < 1e-6 for n in nums):
        return f"quantity {it.quantity:g} is not a number written on the line"
    wu = _written_units(it.text_as_written)
    if it.unit is None:
        return "no unit read"
    words = {norm_unit(w) for w in re.findall(r"[a-zA-ZäöåÄÖÅ]+", it.text_as_written)}
    if wu and it.unit not in wu and it.unit not in words:  # "Voi 500 g | 5 | 5 | kg": the unit has its own column
        return f"unit {it.unit} but the line says {'/'.join(sorted(wu))}"
    if kind == "delivery_note":
        if it.delivered_quantity is None:
            return "delivered quantity missing"
        if it.delivered_quantity > it.quantity * 1.5 + 1:
            return "delivered much more than ordered"
    if it.unit in ("kg", "l") and it.quantity > 2000 or it.unit == "kpl" and it.quantity > 20000:
        return "implausibly large quantity"
    return None


def drop_non_items(rd: ImageRead) -> list[str]:
    """Remove lines that are not products (headings, dates, signatures, column headers): no product name, no
    quantity, no unit, and no "number + unit" in the text. Anything that might be a product ("jogurtti 4 kg"
    without a name) stays, and goes to a human. Returns the removed texts (shown, never silently lost)."""
    keep, dropped = [], []
    for it in rd.items:
        if (it.name_fi is None and it.quantity is None and it.unit is None
                and not _written_units(it.text_as_written)):
            dropped.append(it.text_as_written)
        else:
            keep.append(it)
    rd.items = keep
    return dropped


_SHORTFALL = re.compile(r"\b(puuttuu|puuttui|puuttuvat|vajaa|missing|short|saknas|fattas)\b", re.I)
# a shortfall WITH an amount ("puuttuu 3 l", "3 l missing"); a printed "Puuttuvat tuotteet toimitetaan
# erikseen" footer is not one (eval run 3 flagged that page before this)
_SHORTFALL_AMOUNT = re.compile(
    r"\b(?:puuttuu|puuttui|vajaa|missing|short|saknas|fattas)\W{0,3}\d+(?:[.,]\d+)?\s*[a-zäöå]{0,4}"
    r"|\d+(?:[.,]\d+)?\s*[a-zäöå]{0,4}\W{0,3}(?:puuttuu|puuttui|missing|short|saknas|fattas)\b", re.I)


def page_check(rd: ImageRead, ignored: list[str]) -> dict[int, str]:
    """Page-level check for delivery notes: a shortfall written on the page ("puuttuu 3 l!" in red pen) that
    the model put in notes instead of applying it to a row. Every row that looks delivered in full is then
    suspect (code can't tell which row the pen meant), so it gets the second look, and a human if unresolved.
    Seen in eval run 2: the same page was read correctly in one run and like this in the next."""
    if rd.kind != "delivery_note":
        return {}
    rows = [it for it in rd.items if it.quantity is not None and it.delivered_quantity is not None]
    # where a loose note can hide: page notes, unreadable/ignored lines, and "lines" that are not rows
    texts = [rd.notes or "", *rd.unreadable, *ignored,
             *(f"{it.text_as_written} {it.note or ''}" for it in rd.items if not any(it is r for r in rows))]
    loose = [m.group(0).strip() for t in texts for m in _SHORTFALL_AMOUNT.finditer(t)]
    if not loose or any(_SHORTFALL.search(f"{it.text_as_written} {it.note or ''}") for it in rows):
        return {}
    why = f"the page says '{loose[0]}' but no row shows it: which row is short?"
    return {i: why for i, it in enumerate(rd.items)
            if it.quantity is not None and it.delivered_quantity is not None and it.delivered_quantity >= it.quantity}


class _Use:
    def __init__(self, vr: VisionRead):
        self.vr = vr

    def __call__(self, llm, messages):
        t0 = time.perf_counter()
        resp = llm.chat(messages)
        self.vr.calls += 1
        self.vr.latency_ms += round((time.perf_counter() - t0) * 1000)
        self.vr.prompt_tokens += resp.usage.get("prompt_tokens", 0)
        self.vr.completion_tokens += resp.usage.get("completion_tokens", 0)
        return resp


def read_order_image(src: str | Path | bytes, kind_hint: str | None = None, llm=None, *,
                     prompt_version: str = DEFAULT_PROMPT, today: date | None = None,
                     verify: bool = True) -> VisionRead:
    """Image -> validated ImageRead. One repair retry on invalid JSON; then code cross-checks every line and,
    if any line disagrees with itself or is low-confidence, ONE second look at just those lines.

    llm: a ChatClient (agent.llm) for a vision model, or anything with chat(messages) -> .content/.usage.
    Raises VisionError when the model's answer is invalid twice (the VisionRead is attached as .result).
    """
    if llm is None:
        raise VisionError("No vision model: set NVIDIA_API_KEY (provider nvidia) or pass --from-json.")
    today = today or helsinki_today()
    data, mime = prepare_image(src)
    label = getattr(getattr(llm, "cfg", None), "label", "llm")
    vr = VisionRead(read=None, prompt_version=prompt_version, model=label)
    use = _Use(vr)
    user: list[dict] = [{"type": "text", "text": "Read this page." + (
        f" The kitchen says it is a {kind_hint.replace('_', ' ')}." if kind_hint else "")}, image_part(data, mime)]
    messages = [{"role": "system", "content": PROMPTS[prompt_version](today)}, {"role": "user", "content": user}]
    for attempt in range(2):
        resp = use(llm, messages)
        vr.raw = resp.content
        try:
            vr.read = ImageRead.model_validate(extract_json(resp.content))
            vr.repaired = attempt > 0
            break
        except (ValueError, ValidationError) as e:
            err = str(e)[:800]
            vr.errors.append(err)
            messages += [{"role": "assistant", "content": resp.content or ""},
                         {"role": "user", "content": f"That was not valid: {err}\n"
                                                     "Return only the corrected JSON object, exactly the shape asked."}]
    if vr.read is None:
        e = VisionError(f"The model's answer was not valid JSON after 2 tries: {vr.errors[-1][:300]}")
        e.result = vr  # type: ignore[attr-defined]
        raise e
    rd = vr.read
    if kind_hint in ("order_list", "menu", "delivery_note") and rd.kind != kind_hint:
        rd.notes = ((rd.notes + " ") if rd.notes else "") + f"(read as {rd.kind}; the kitchen said {kind_hint})"

    vr.ignored += drop_non_items(rd)
    page = page_check(rd, vr.ignored)
    suspicious = [(i, it, why) for i, it in enumerate(rd.items)
                  if (why := cross_check(it, rd.kind) or page.get(i)
                      or ("low confidence" if it.confidence < CONFIRM_BELOW else None))]
    if verify and suspicious:
        msgs = [{"role": "system", "content": PROMPTS[prompt_version](today)},
                {"role": "user", "content": [{"type": "text", "text": verify_prompt(suspicious)}, image_part(data, mime)]}]
        try:
            resp = use(llm, msgs)
            fixes = {int(x["index"]): x for x in extract_json(resp.content).get("lines", []) if "index" in x}
        except Exception as e:  # noqa: BLE001 - the second look is best effort; the flags stay
            vr.errors.append(f"second look failed: {str(e)[:200]}")
            fixes = {}
        for i, it, why in suspicious:
            fx = fixes.get(i)
            if not fx:
                continue
            before = it.model_dump(include={"text_as_written", "quantity", "unit", "delivered_quantity", "confidence"})
            try:
                new = ReadItem.model_validate({**it.model_dump(), **{k: v for k, v in fx.items() if k != "index"}})
            except ValidationError as e:
                vr.errors.append(f"second look, line {i}: {str(e)[:200]}")
                continue
            # the second look may not make a line look safer than code can check: an unresolved mismatch stays
            rd.items[i] = new
            vr.rechecked.append({"index": i, "why": why, "before": before,
                                 "after": new.model_dump(include=set(before))})
    # the page's loose shortfall counts as applied once the second look moved it onto a row
    # (the changed row must also say where it saw the note, so a guess at a row is not taken for a fix)
    applied = any(c["after"].get("delivered_quantity") != c["before"].get("delivered_quantity")
                  and _SHORTFALL.search(c["after"].get("text_as_written") or "") for c in vr.rechecked)
    had_loose = bool(page)
    page = {} if applied else page_check(rd, vr.ignored)
    if had_loose and not applied:
        # a row the second look changed for the loose note without showing the note on it is a guess
        for c in vr.rechecked:
            if c["after"].get("delivered_quantity") != c["before"].get("delivered_quantity"):
                page[c["index"]] = "delivered changed for a loose page note without showing it on the row"
    for i, it in enumerate(rd.items):
        why = cross_check(it, rd.kind) or page.get(i)
        if why:
            it.confidence = min(it.confidence, 0.5)
            it.note = "; ".join(x for x in (it.note, f"check: {why}") if x)
            vr.flags.append({"index": i, "text_as_written": it.text_as_written, "why": why})
    return vr


# --- from the reading to a quote -------------------------------------------------------------------------

@dataclass
class Line:
    index: int
    item: ReadItem
    status: str                    # to_order | needs_confirmation | no_shortfall | quoted | not_found |
                                   # ruled_out_by_mandate | uncovered | quoted_with_substitute
    reason: str | None = None
    amount: float | None = None    # what goes to the gateway, in `unit`
    unit: str | None = None        # KG | L | SALES_UNIT
    need: R.Need | None = None
    quote_items: list[int] = field(default_factory=list)
    substitute: dict | None = None

    def to_dict(self) -> dict:
        return {"index": self.index, "text_as_written": self.item.text_as_written, "name_fi": self.item.name_fi,
                "quantity": self.item.quantity, "unit_written": self.item.unit,
                "delivered_quantity": self.item.delivered_quantity, "confidence": self.item.confidence,
                "status": self.status, "reason": self.reason, "amount": self.amount, "unit": self.unit,
                "candidates": [{"id": c["id"], "name": c["name"]} for c in (self.need.candidates if self.need else [])],
                "quote_items": self.quote_items, "substitute": self.substitute}


@dataclass
class PhotoOrder:
    vision: VisionRead
    mandate_id: str
    agent_id: str
    lines: list[Line] = field(default_factory=list)
    delivery_date: date | None = None
    quote: dict | None = None
    order: dict | None = None
    plan: R.Plan | None = None     # menus: the recipe planner's plan (its own quote and order)
    to_confirm: list[str] = field(default_factory=list)
    error: str | None = None
    trace: R.Trace = field(default_factory=R.Trace)

    @property
    def kind(self) -> str | None:
        return self.vision.read.kind if self.vision.read else None

    @property
    def final_quote(self) -> dict | None:
        return self.plan.quote if self.plan else self.quote

    @property
    def final_order(self) -> dict | None:
        return self.plan.order if self.plan else self.order

    def to_dict(self) -> dict:
        q = self.final_quote
        o = self.final_order
        return {
            "vision": self.vision.to_dict(), "kind": self.kind, "mandate_id": self.mandate_id,
            "agent_id": self.agent_id, "delivery_date": self.delivery_date.isoformat() if self.delivery_date else None,
            "lines": [l.to_dict() for l in self.lines],
            "to_confirm": self.to_confirm,
            "quote": None if not q else {
                "id": q["id"], "expires_at": q["expires_at"], "total_ex_vat": q["total_ex_vat"],
                "total_inc_vat": q["total_inc_vat"],
                "lines": [{"item_index": l.get("item_index"), "product_id": l["product_id"], "name": l["name"],
                           "quantity": l["quantity"], "sales_unit": l["sales_unit"],
                           "line_total_ex_vat": l["line_total_ex_vat"], "orderable": l["orderable"],
                           "substitute_for": l.get("substitute_for")} for l in q["lines"]],
                "uncovered": q["uncovered"], "mandate_preview": q.get("mandate_preview")},
            "order": None if not o else {"id": o["id"], "status": o["status"], "tier": o["decision"]["outcome"],
                                         "total_ex_vat": o["total_ex_vat"], "approval_url": o.get("approval_url")},
            "recipe_plan": self.plan.to_dict() if self.plan else None,
            "error": self.error, "summary": self.summary(),
            "valio_api_calls": self.trace.valio_api_calls + (self.plan.trace.valio_api_calls if self.plan else 0),
        }

    def summary(self) -> str:
        if self.error and not self.final_quote:
            return f"No quote: {self.error}" + (f" To confirm: {'; '.join(self.to_confirm)}." if self.to_confirm else "")
        parts = []
        if self.kind == "menu" and self.plan:
            parts.append(self.plan.summary())
        else:
            n = sum(1 for l in self.lines if l.quote_items)
            what = "shortfall line(s)" if self.kind == "delivery_note" else "line(s)"
            parts.append(f"Read {ARTICLE[self.kind]}: {len(self.lines)} lines, {n} {what} quoted.")
            q = self.quote
            if q:
                prev = q.get("mandate_preview") or {}
                parts.append(f"Quote {q['id']}: €{q['total_ex_vat']:,.2f} ex VAT, mandate preview: "
                             f"{prev.get('outcome', 'n/a')}.")
            if self.order:
                parts.append(f"Order {self.order['id']}: {self.order['status']}"
                             + (f", approve at {self.order['approval_url']}" if self.order.get("approval_url") else "")
                             + ".")
        if self.to_confirm:
            parts.append(f"Not ordered, please confirm: {'; '.join(self.to_confirm)}.")
        return " ".join(parts)


def _need_for(it: ReadItem, amount: float, unit: str) -> R.Need:
    """A recipe-planner Need for one written line, so it gets the planner's lexicon-aware catalog matching."""
    key = R.match_ingredient(*(x for x in (it.name_fi, it.name_en) if x))
    lex = R.table()["ingredients"]
    e = lex.get(key) if key else None
    text = " ".join(x for x in (it.text_as_written, it.name_fi, it.name_en, it.note) if x).lower()
    claims = sorted({c for w, c in R.NOTE_CLAIMS.items() if w in text} | set((e or {}).get("claims", [])))
    name_fi = it.name_fi or it.text_as_written
    name_en = it.name_en or name_fi
    if e:
        queries = list(dict.fromkeys([*(e.get("queries") or [e["fi"]]), name_fi]))
        return R.Need(key=key, name_fi=e["fi"], name_en=e["en"], dishes=[], unit=unit, needed=amount, per_portion=[],
                      claims=claims, queries=queries, must=list(e.get("must", [])),
                      exclude=list(e.get("exclude", [])), bulk_ok=bool(e.get("bulk_ok")), to_buy=amount,
                      status="pending")
    queries = list(dict.fromkeys(x for x in (name_fi, name_en) if x))
    return R.Need(key=R._norm(name_fi), name_fi=name_fi, name_en=name_en, dishes=[], unit=unit, needed=amount,
                  per_portion=[], claims=claims, queries=queries, must=[], exclude=[], to_buy=amount, status="pending")


def gateway_amount(it: ReadItem, qty: float) -> tuple[float, str, str | None]:
    """(amount, KG|L|SALES_UNIT, note) for a written quantity and unit."""
    if it.unit in _TO_GATEWAY:
        base, f = _TO_GATEWAY[it.unit]
        return R.round_up(qty * f, base), base, None
    if it.unit == "kpl":
        key = R.match_ingredient(*(x for x in (it.name_fi, it.name_en) if x))
        pcs_g = (R.table()["ingredients"].get(key) or {}).get("pcs_g") if key else None
        if pcs_g:
            return R.round_up(qty * pcs_g / 1000, "KG"), "KG", f"{qty:g} pcs counted as {pcs_g:g} g each"
        return float(qty), "SALES_UNIT", "counted in pieces: the gateway quotes sales units, check the pack size"
    return float(qty), "SALES_UNIT", f"written as {qty:g} {it.unit}: quoted as sales units, check the pack size"


def classify(rd: ImageRead) -> list[Line]:
    """Which lines may be ordered automatically, which need a human, which need nothing (delivered in full).

    Doubt comes first: a line that is low-confidence, incomplete or fails the code cross-check goes to a human
    whatever it says, also an apparent full delivery (a misread "5 / 5" could hide a shortfall). The
    cross-check runs here too, so callers that build a reading themselves get the same guard."""
    out = []
    for i, it in enumerate(rd.items):
        ln = Line(index=i, item=it, status="to_order")
        why = cross_check(it, rd.kind)
        if it.confidence < CONFIRM_BELOW:
            ln.status = "needs_confirmation"
            ln.reason = f"read with confidence {it.confidence:.2f}" + (f" ({it.note})" if it.note else "")
        elif it.quantity is None or it.unit is None:
            ln.status, ln.reason = "needs_confirmation", "no quantity or unit read"
        elif rd.kind == "delivery_note" and it.delivered_quantity is None:
            ln.status, ln.reason = "needs_confirmation", "delivered quantity not read"
        elif why:
            ln.status, ln.reason = "needs_confirmation", f"check: {why}"
        if ln.status != "to_order":
            out.append(ln)
            continue
        qty = it.quantity
        if rd.kind == "delivery_note":
            qty = round(it.quantity - it.delivered_quantity, 6)
            if qty <= 0:
                ln.status, ln.reason = "no_shortfall", "delivered in full"
                out.append(ln)
                continue
        ln.amount, ln.unit, note = gateway_amount(it, qty)
        if note:
            ln.reason = note
        out.append(ln)
    return out


def _menu_request(rd: ImageRead) -> tuple[str, list[str]]:
    """The recipe planner's request text for the dishes we may plan, and what needs a human."""
    confirm, ok = [], []
    for d in rd.dishes:
        if d.portions is None:
            confirm.append(f"{d.name}: no portions read")
        elif d.confidence < CONFIRM_BELOW:
            confirm.append(f"{d.name} for {d.portions}: read with confidence {d.confidence:.2f}")
        else:
            ok.append(d)
    if not ok:
        return "", confirm
    if len({d.portions for d in ok}) == 1:
        text = f"{', '.join(d.name for d in ok)} for {ok[0].portions} portions"
    else:
        text = "Menu: " + "; ".join(f"{d.name} for {d.portions} portions" for d in ok)
    if rd.delivery_date:
        text += f", delivery {rd.delivery_date.isoformat()}"
    return text, confirm


def order_from_read(vr: VisionRead, *, gateway, mandate_id: str, agent_id: str, recipe_llm=None,
                    delivery_date: date | None = None, today: date | None = None, place: bool = False,
                    substitutes: bool = True) -> PhotoOrder:
    """A validated reading -> ONE basket quote (-> order under the mandate). Lines that need a human are left out."""
    today = today or helsinki_today()
    po = PhotoOrder(vision=vr, mandate_id=mandate_id, agent_id=agent_id)
    rd = vr.read
    if rd is None:
        po.error = "nothing was read"
        return po
    po.to_confirm += [f"unreadable: {u}" for u in rd.unreadable]
    dd = delivery_date or (rd.delivery_date if rd.delivery_date and rd.delivery_date > today else None) \
        or next_delivery_day(today)
    po.delivery_date = dd

    if rd.kind == "menu":
        text, confirm = _menu_request(rd)
        po.to_confirm += confirm
        if not text:
            po.error = "no dish with portions to plan"
            return po
        if recipe_llm is None and len({d.portions for d in rd.dishes if d.portions}) > 1:
            po.to_confirm.append("the recipe table plans every dish for the first portion count; "
                                 "use a model (--recipe-provider) for different portions per dish")
        po.plan = R.plan(text, gateway=gateway, mandate_id=mandate_id, agent_id=agent_id, llm=recipe_llm,
                         delivery_date=dd, today=today, place=place, substitutes=substitutes)
        po.error = po.plan.error
        return po

    po.lines = classify(rd)
    for ln in po.lines:
        if ln.status == "needs_confirmation":
            po.to_confirm.append(f'"{ln.item.text_as_written}": {ln.reason}')
    todo = [ln for ln in po.lines if ln.status == "to_order"]
    if not todo:
        po.error = ("everything on the delivery note was delivered" if rd.kind == "delivery_note"
                    and all(l.status == "no_shortfall" for l in po.lines) else "no line can be ordered automatically")
        return po
    is_error, m = R._call(gateway, po.trace, "get_mandate", {"mandate_id": mandate_id})
    if is_error:
        po.error = f"mandate: {m.get('message')}"
        return po
    mandate = m["mandate"]
    items: list[dict] = []
    for ln in todo:
        ln.need = n = _need_for(ln.item, ln.amount, ln.unit)
        n.candidates, blocked = R.find_candidates(gateway, po.trace, n, mandate, agent_id)
        if not n.candidates:
            ln.status = "ruled_out_by_mandate" if blocked else "not_found"
            ln.reason = (f"closest match {blocked['name']} is ruled out by the mandate" if blocked
                         else "no catalog product matched")
            po.to_confirm.append(f'"{ln.item.text_as_written}": {ln.reason}')
            continue
        ln.status = "quoted"
        ln.quote_items = [len(items)]
        note = ln.item.text_as_written[:80] + (" (shortfall)" if rd.kind == "delivery_note" else "")
        items.append({"product_ids": [c["id"] for c in n.candidates], "amount": ln.amount, "unit": ln.unit,
                      "note": note})
    if not items:
        po.error = "no catalog product matched any line"
        return po
    args = {"agent_id": agent_id, "mandate_id": mandate_id, "delivery_date": dd.isoformat(), "items": list(items)}
    is_error, r = R._call(gateway, po.trace, "request_quote", args)
    if is_error:
        po.error = f"quote: {r.get('message')}"
        return po
    po.quote = r["quote"]

    # what the quote can't cover (short stock: typical for a delivery-note shortfall): one substitute each
    if substitutes:
        calls, changed = 0, False
        for ln in [l for l in po.lines if l.quote_items]:
            idx = ln.quote_items[0]
            lines = [l for l in po.quote["lines"] if l.get("item_index") == idx]
            gap = next((g for g in po.quote["uncovered"] if g.get("item_index") == idx), None)
            if not gap and lines and all(l["orderable"] for l in lines):
                continue
            ln.status = "uncovered"
            if calls >= MAX_SUBSTITUTE_CALLS:
                continue
            calls += 1
            orig = lines[0]["product_id"] if lines else ln.need.candidates[0]["id"]
            missing = R.round_up(gap["uncovered"] if gap else ln.amount, ln.unit)
            err, s = R._call(gateway, po.trace, "suggest_substitutes", {
                "agent_id": agent_id, "mandate_id": mandate_id, "product_id": orig, "delivery_date": dd.isoformat(),
                "amount": missing, "unit": ln.unit})
            opts = [] if err else [o for o in s.get("options", []) if (o.get("offer") or {}).get("orderable")]
            if not opts:
                po.to_confirm.append(f'"{ln.item.text_as_written}": not available, no safe substitute')
                continue
            o = opts[0]
            ln.substitute = {"for_product_id": orig, "product_id": o["product_id"], "name": o["name"],
                             "tier": o.get("tier"), "compromises": o.get("compromises", []), "amount": missing}
            ok = [l for l in lines if l["orderable"]]
            covered = sum((l.get("covers") or {}).get("amount") or 0 for l in ok)
            if ok and covered > 0:
                items[idx] = {**items[idx], "product_ids": [l["product_id"] for l in ok], "amount": round(covered, 3)}
            else:
                items[idx] = None  # type: ignore[call-overload]
            items.append({"product_ids": [o["product_id"]], "amount": missing, "unit": ln.unit,
                          "substitute_for": orig, "note": f"{ln.item.text_as_written[:60]} (substitute)"})
            ln.quote_items = ([idx] if items[idx] else []) + [len(items) - 1]
            ln.status = "quoted_with_substitute"
            changed = True
        if changed:
            keep = [i for i, it in enumerate(items) if it is not None]
            remap = {old: new for new, old in enumerate(keep)}
            for ln in po.lines:
                ln.quote_items = [remap[i] for i in ln.quote_items if i in remap]
            err, r2 = R._call(gateway, po.trace, "request_quote", {**args, "items": [items[i] for i in keep]})
            if not err:
                po.quote = r2["quote"]

    if place:
        qid = po.quote["id"]
        is_error, o = R._call(gateway, po.trace, "place_order", {"agent_id": agent_id, "mandate_id": mandate_id,
                                                                 "quote_id": qid,
                                                                 "idempotency_key": f"{agent_id}:{qid}"})
        if is_error:
            po.error = f"order: {o.get('message')}"
        else:
            po.order = o
    return po


def order_from_image(src: str | Path | bytes, *, gateway, mandate_id: str, agent_id: str, llm,
                     kind_hint: str | None = None, recipe_llm=None, delivery_date: date | None = None,
                     today: date | None = None, place: bool = False, prompt_version: str = DEFAULT_PROMPT,
                     verify: bool = True) -> PhotoOrder:
    """Photo -> Nemotron VL reading -> ONE basket quote (-> order). The function for an upload box."""
    try:
        vr = read_order_image(src, kind_hint, llm, prompt_version=prompt_version, today=today, verify=verify)
    except VisionError as e:
        vr = getattr(e, "result", None) or VisionRead(read=None, prompt_version=prompt_version, model="?")
        po = PhotoOrder(vision=vr, mandate_id=mandate_id, agent_id=agent_id, error=str(e))
        return po
    return order_from_read(vr, gateway=gateway, mandate_id=mandate_id, agent_id=agent_id, recipe_llm=recipe_llm,
                           delivery_date=delivery_date, today=today, place=place)


# --- rendering ------------------------------------------------------------------------------------------

def render(po: PhotoOrder, source: str = "photo") -> str:
    vr = po.vision
    out = [f"Image: {source}", f"Read by: {vr.model} (prompt {vr.prompt_version}; {vr.calls} call(s), "
           f"{vr.latency_ms / 1000:.1f} s{', JSON repaired once' if vr.repaired else ''})"]
    rd = vr.read
    if rd is None:
        out.append(f"Nothing read: {po.error}")
        return "\n".join(out)
    out.append(f"\n1. Read: {rd.kind.replace('_', ' ')}"
               + (f", delivery {rd.delivery_date.isoformat()}" if rd.delivery_date else "")
               + (f"  [{rd.notes}]" if rd.notes else ""))
    for i, it in enumerate(rd.items):
        q = "?" if it.quantity is None else f"{it.quantity:g}"
        d = "" if it.delivered_quantity is None else f" / delivered {it.delivered_quantity:g}"
        mark = "ok " if it.confidence >= CONFIRM_BELOW else "?? "
        out.append(f"   {mark}{it.confidence:.2f}  {it.text_as_written!r:<38} -> {it.name_fi or '?'}: {q} {it.unit or '?'}{d}"
                   + (f"  ({it.note})" if it.note else ""))
    for d in rd.dishes:
        out.append(f"   dish: {d.name} x {d.portions or '?'}" + (f" ({d.day})" if d.day else ""))
    for u in rd.unreadable:
        out.append(f"   unreadable: {u}")
    for t in vr.ignored:
        out.append(f"   not a product line (ignored): {t!r}")
    for c in vr.rechecked:
        b, a = c["before"], c["after"]
        out.append(f"   second look, line {c['index']} ({c['why']}): {b['quantity']} {b['unit']} -> "
                   f"{a['quantity']} {a['unit']} (confidence {b['confidence']:.2f} -> {a['confidence']:.2f})")
    if po.plan:
        out.append("\n2. Menu -> recipe planner (agent/recipe.py)\n")
        out.append("\n".join("   " + x for x in R.render(po.plan).splitlines()))
    else:
        out.append("\n2. Lines -> products" + (" (shortfall = ordered - delivered)" if rd.kind == "delivery_note" else ""))
        q = po.quote
        by_item: dict[int, list[dict]] = {}
        for l in (q or {}).get("lines", []):
            by_item.setdefault(l.get("item_index"), []).append(l)
        for ln in po.lines:
            amt = f"{ln.amount:g} {ln.unit}" if ln.amount is not None else ""
            out.append(f"   [{ln.status}] {ln.item.text_as_written}" + (f" -> {amt}" if amt else "")
                       + (f"  ({ln.reason})" if ln.reason else ""))
            for i in ln.quote_items:
                for l in by_item.get(i, []):
                    out.append(f"      {l['quantity']:g} {l['sales_unit']} {l['name']} "
                               f"(€{l['line_total_ex_vat'] or 0:,.2f}){'' if l['orderable'] else ' NOT ORDERABLE'}")
            if ln.substitute:
                out.append(f"      substitute: {ln.substitute['name']} [{ln.substitute['tier']}]"
                           + (f": {'; '.join(ln.substitute['compromises'][:2])}" if ln.substitute['compromises'] else ""))
        if q:
            prev = q.get("mandate_preview") or {}
            out.append(f"\n3. Quote {q['id']}: €{q['total_ex_vat']:,.2f} ex VAT (€{q['total_inc_vat']:,.2f} incl.), "
                       f"{len(q['lines'])} lines, delivery {po.delivery_date}, expires {q['expires_at'][11:16]}")
            out.append(f"\n4. Mandate preview ({po.mandate_id}): {prev.get('outcome')}")
            for f in prev.get("findings", []):
                if f["outcome"] != "silent" or f["rule"] == "mandate.ok":
                    out.append(f"   [{f['outcome']}] {f['rule']}: {f['message']}")
            if po.order:
                o = po.order
                out.append(f"\n5. Order {o['id']}: {o['status']} (tier {o['decision']['outcome']})"
                           + (f"\n   approve at {o['approval_url']}" if o.get("approval_url") else ""))
            else:
                out.append("\n5. Not ordered (run with --order to place it under the mandate)")
    if po.to_confirm:
        out.append("\nNeeds a human (not ordered):")
        out += [f"   - {c}" for c in po.to_confirm]
    if po.error:
        out.append(f"\nError: {po.error}")
    out.append(f"\nSummary: {po.summary()}")
    return "\n".join(out)


# --- CLI ------------------------------------------------------------------------------------------------

def make_llm(provider: str | None, model: str | None = None):
    from .llm import ChatClient, config_from_env, key_present
    cfg = config_from_env(provider, model)
    if cfg.provider != "local" and not key_present(cfg.provider):
        return None
    cfg.max_tokens = 4096
    return ChatClient(cfg)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Photo of an order list, menu or delivery note -> one basket quote.")
    ap.add_argument("image", nargs="?", help="JPEG/PNG/WebP photo or scan")
    ap.add_argument("--mandate", default="M-HOTEL-AURORA")
    ap.add_argument("--agent", default="aurora-purchasing-agent")
    ap.add_argument("--provider", default="nvidia", help="vision model provider (default nvidia: Nemotron 3.5 Super VL)")
    ap.add_argument("--model")
    ap.add_argument("--kind", choices=["order_list", "menu", "delivery_note"], help="hint what the page is")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT, choices=sorted(PROMPTS))
    ap.add_argument("--no-verify", action="store_true", help="skip the targeted second look")
    ap.add_argument("--recipe-provider", default=None,
                    help="menus: table | nvidia | gemini | ... (default: the vision provider, else the table)")
    ap.add_argument("--gateway", default="local", help="'local' (in-process) or a gateway base URL")
    ap.add_argument("--date", help="delivery date YYYY-MM-DD (overrides the page)")
    ap.add_argument("--order", action="store_true", help="place the order under the mandate")
    ap.add_argument("--read-only", action="store_true", help="only read the image; print the reading as JSON")
    ap.add_argument("--from-json", help="replay a saved reading (ImageRead JSON) instead of calling the model")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    a = ap.parse_args(argv)
    if a.from_json:
        raw = json.loads(Path(a.from_json).read_text(encoding="utf-8"))
        rec = raw.get("recorded") or {}  # evals/vision/samples/: real Nemotron readings recorded in the CI eval
        vr = VisionRead(read=ImageRead.model_validate(raw.get("read", raw)),
                        prompt_version=f"replay of {rec['variant']}" if rec else "replay",
                        model=f"{rec['model']}, recorded {rec['ran_at'][:16]}" if rec else
                        f"replay:{Path(a.from_json).name}", calls=rec.get("calls", 0),
                        latency_ms=rec.get("latency_ms", 0), rechecked=rec.get("second_look", []))
        for i, it in enumerate(vr.read.items):
            if why := cross_check(it, vr.read.kind):
                it.confidence = min(it.confidence, 0.5)
                vr.flags.append({"index": i, "text_as_written": it.text_as_written, "why": why})
    else:
        if not a.image:
            ap.error("give an image (or --from-json)")
        llm = make_llm(a.provider, a.model)
        if llm is None:
            print(f"No API key for {a.provider} (NVIDIA_API_KEY for nvidia). Use --from-json to replay a reading.",
                  file=sys.stderr)
            return 2
        try:
            vr = read_order_image(a.image, a.kind, llm, prompt_version=a.prompt, verify=not a.no_verify)
        except VisionError as e:
            print(str(e), file=sys.stderr)
            return 1
    if a.read_only:
        print(json.dumps(vr.to_dict(), ensure_ascii=False, indent=1))
        return 0
    rp = a.recipe_provider or ("table" if a.from_json else a.provider)
    recipe_llm = None if rp == "table" else make_llm(rp, a.model if rp == a.provider else None)
    from .buyer import MCPGateway
    if a.gateway == "local":
        from sous.mcp_server import mcp
        target = mcp
    else:
        target = a.gateway
    with MCPGateway(target) as gw:
        po = order_from_read(vr, gateway=gw, mandate_id=a.mandate, agent_id=a.agent, recipe_llm=recipe_llm,
                             delivery_date=date.fromisoformat(a.date) if a.date else None, place=a.order)
    print(json.dumps(po.to_dict(), ensure_ascii=False, indent=1, default=str) if a.json
          else render(po, a.image or a.from_json))
    return 1 if po.error and not po.final_quote else 0


if __name__ == "__main__":
    sys.exit(main())
