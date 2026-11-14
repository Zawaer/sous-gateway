"""Gemini Decisions API (gemini-decision-1) on products with no allergen declaration: extra caution, never less.

About 30 % of Valio Aimo's food products declare no allergens at all. The mandate engine already treats them as
unknown (rule allergen.unknown: ask or block, never safe). Many of those still say what they are made of in their
name or key ingredients ("Hasselpähkinä-suklaalevite", "Voi-munataikina"). For such a product under a mandate that
avoids allergens, the gateway asks the Decisions API one yes/no question per avoided allergen and gets back the
probability that the product contains it.

- p >= LIKELY (0.8): rule allergen.likely, block, with the probability in the message. A human can't approve it
  into an order: the product very likely contains what the buyer must avoid.
- anything else: nothing changes. The product stays allergen.unknown (ask or block per the mandate). A low
  probability never makes a product safe: no model output removes a check.

The answer is cached per (product, allergen code, model) in table `allergen_ai`, so the same product gets the same
answer at quote, order and approval time, and a quote costs at most one API call per undeclared product. Off
without GEMINI_API_KEY or with SOUS_ALLERGEN_AI=0; a failed or slow call (SOUS_ALLERGEN_AI_TIMEOUT_S, default 4 s)
means no assessment, i.e. today's behaviour. Calls are capped per minute (SOUS_ALLERGEN_AI_PER_MIN, default 60).

Input: the product's names (fi, sv, en), brand and key ingredients from the static catalog; no buyer data.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import catalog, config, store

MODEL = "gemini-decision-1"
URL = "https://generativelanguage.googleapis.com/v1beta/decisions"
LIKELY = 0.8

TIMEOUT_S = float(config.env("ALLERGEN_AI_TIMEOUT_S", "4"))
PER_MIN = int(config.env("ALLERGEN_AI_PER_MIN", "60"))

# What each GS1 code means in a question. A group code (milk, tree nuts, gluten cereals) asks about the family.
_FAMILY = {
    "AM": "milk or any milk product (butter, cream, cheese, whey, yoghurt, milk powder, lactose)",
    "AN": "tree nuts (almond, hazelnut, walnut, cashew, pecan, brazil nut, pistachio, macadamia)",
    "AW": "cereals containing gluten (wheat, rye, barley, oats, spelt, kamut)",
    "AP": "peanuts", "AE": "egg", "AF": "fish", "AC": "crustaceans (shrimp, crab, lobster)",
    "UM": "molluscs (mussels, squid, snails)", "AY": "soy", "AS": "sesame", "BM": "mustard", "BC": "celery",
    "NL": "lupin", "AU": "sulphites (sulphur dioxide, E220-E228)",
}

_SQLITE = """CREATE TABLE IF NOT EXISTS allergen_ai (
  product_id TEXT NOT NULL, code TEXT NOT NULL, model TEXT NOT NULL, p REAL NOT NULL, confidence REAL NOT NULL,
  created_at TEXT NOT NULL, PRIMARY KEY (product_id, code, model))"""
_MARIADB = """CREATE TABLE IF NOT EXISTS allergen_ai (
  product_id VARCHAR(64) NOT NULL, code VARCHAR(8) NOT NULL, model VARCHAR(64) NOT NULL, p DOUBLE NOT NULL,
  confidence DOUBLE NOT NULL, created_at VARCHAR(40) NOT NULL, PRIMARY KEY (product_id, code, model))"""
COLS = ["product_id", "code", "model", "p", "confidence", "created_at"]

_ready_for = None
_calls: list[float] = []  # monotonic times of recent API calls (per-minute cap)
_calls_lock = threading.Lock()
stats = {"api_calls": 0, "cache_hits": 0, "errors": 0, "capped": 0}


def enabled() -> bool:
    return bool(os.getenv("GEMINI_API_KEY")) and config.env("ALLERGEN_AI", "1") not in ("0", "false", "no")


def _ensure() -> None:
    global _ready_for
    with store._lock:
        conn = store.db()
        if _ready_for is conn:
            return
        store._exec(_MARIADB if config.DB_URL else _SQLITE)
        _ready_for = conn


def question(code: str) -> str:
    what = _FAMILY.get(code) or catalog.ALLERGENS.get(code, code)
    return (f"Does this food product contain {what} as an ingredient? Judge only from its name, brand and listed "
            "ingredients. Answer yes if any of them is or is made from it; a mere possible trace is not an ingredient.")


def product_text(p: dict) -> str:
    names = "; ".join(f"{lang}: {n}" for lang, n in (p.get("names") or {}).items() if n) or p.get("name", "")
    parts = [f"Food product in a Finnish food-service wholesale catalog. Names: {names}."]
    if p.get("brand"):
        parts.append(f"Brand: {p['brand']}.")
    ingredients = " / ".join(x for x in (p.get("ingredients_fi"), p.get("ingredients_en")) if x)
    if ingredients:
        parts.append(f"Key ingredients: {ingredients}.")
    return " ".join(parts)


def _cached(pid: str, codes: list[str]) -> dict[str, dict]:
    _ensure()
    marks = ",".join("?" for _ in codes)
    rows = store._exec(f"SELECT code, p, confidence FROM allergen_ai WHERE product_id = ? AND model = ? "
                       f"AND code IN ({marks})", (pid, MODEL, *codes))[0]
    return {r["code"]: {"p": float(r["p"]), "confidence": float(r["confidence"]), "model": MODEL} for r in rows}


def _take_slot() -> bool:
    now = time.monotonic()
    with _calls_lock:
        while _calls and now - _calls[0] > 60:
            _calls.pop(0)
        if len(_calls) >= PER_MIN:
            return False
        _calls.append(now)
        return True


def ask(text: str, codes: list[str], *, client: httpx.Client | None = None) -> dict[str, dict]:
    """One Decisions API request: {code: {p, confidence}} for each code. Raises on HTTP or schema errors."""
    body = {"model": MODEL, "input": text,
            "questions": {c: {"type": "noul", "instructions": question(c),
                              "criteria": {"true_description": "It is, or contains, this as an ingredient.",
                                           "false_description": "None of its ingredients is this."}}
                          for c in codes}}
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"}
    http = client or httpx
    r = http.post(URL, json=body, headers=headers, timeout=TIMEOUT_S)
    r.raise_for_status()
    answers = r.json()["answers"]
    return {c: {"p": float(answers[c]["noul"]), "confidence": float(answers[c]["confidence"]), "model": MODEL}
            for c in codes}


def assess(pid: str, codes: list[str]) -> dict[str, dict]:
    """{code: {p, confidence, model}} for the product, from the cache or one API call; {} when off or failing."""
    codes = sorted({c for c in codes if c in _FAMILY or c in catalog.ALLERGENS} - {"X99"})
    p = catalog.get(pid)
    if not codes or not p or not enabled():
        return {}
    got = _cached(pid, codes)
    missing = [c for c in codes if c not in got]
    stats["cache_hits"] += len(codes) - len(missing)
    if not missing:
        return got
    if not _take_slot():
        stats["capped"] += 1
        return got
    try:
        stats["api_calls"] += 1
        fresh = ask(product_text(p), missing)
    except Exception:  # noqa: BLE001 - no assessment is today's behaviour, never an error for the buyer
        stats["errors"] += 1
        return got
    now = store.now_iso()
    for c, a in fresh.items():
        store._upsert("allergen_ai", COLS, (pid, c, MODEL, a["p"], a["confidence"], now))
    return {**got, **fresh}


def assess_lines(lines: list[dict], avoid: list[str]) -> dict[str, dict[str, dict]]:
    """{product_id: {code: assessment}} for the lines whose product declares no allergens at all."""
    if not avoid or not enabled():
        return {}
    pids = sorted({l["product_id"] for l in lines
                   if catalog.allergen_status(catalog.get(l["product_id"]) or {}, avoid, False)["status"] == "unknown"})
    if not pids:
        return {}
    with ThreadPoolExecutor(max_workers=min(4, len(pids))) as ex:
        return {pid: a for pid, a in zip(pids, ex.map(lambda x: assess(x, avoid), pids)) if a}


def likely(assessment: dict[str, dict]) -> list[tuple[str, dict]]:
    """The (code, answer) pairs at or above LIKELY, highest first."""
    return sorted(((c, a) for c, a in assessment.items() if a["p"] >= LIKELY), key=lambda x: -x[1]["p"])

