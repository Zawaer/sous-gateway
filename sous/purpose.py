"""What a product is for, and so how strictly it may be substituted.

A kitchen's everyday reality: coffee cream can be any brand, but whipping cream for a recipe may not
whip the same (additives like lecithin change it), and in an allergy recipe often only one exact product works.
So each item can say what it is for:

  purpose       free text: "coffee", "recipe: lasagne", "allergy menu (gluten-free)"
  substitution  exact   only the product(s) asked for; no substitute is proposed or ordered (refused)
                ask     a substitute may be proposed, but always needs the buyer's yes
                allowed the mandate's own `substitutions` tier applies (the default)

Where it comes from, strictest wins:
  - the buyer's mandate, `product_purposes` {product_id: {purpose, substitution}}: set by the buyer's human, so it
    binds every agent and Valio Aimo's side (suggestions, offers, substitution at pick);
  - a standing order's item (set by the buyer's human);
  - the quote item itself (set by the agent or the recipe planner). Without `substitution`, it is read from the
    purpose: recipe / dish words -> ask; allergy / "only this" words -> exact; coffee / "any brand" -> allowed.

An agent can only make an item stricter: `allowed` never loosens the mandate's substitutions tier.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

Substitution = Literal["exact", "ask", "allowed"]
RANK = {"allowed": 0, "ask": 1, "exact": 2}

_EXACT = re.compile(r"\b(allerg\w*|keliak\w*|coeliac|celiac|only this|exact|vain tämä|juuri tämä|ei korvaavaa|"
                    r"no substitut\w*)", re.IGNORECASE)
_ASK = re.compile(r"\b(recipe|resepti\w*|recept\w*|dish|ruokalaji|bak\w*|leivon\w*|whip\w*|kuohu\w*|"
                  r"menu|ruokalista|lasagne|lasagna|keitto|soup|kastike|sauce|dessert|jälkiruo\w*)", re.IGNORECASE)
_ALLOWED = re.compile(r"\b(coffee|kahvi\w*|tea|tee\b|any brand|mikä tahansa|whatever|general|yleis\w*|"
                      r"buffet|aamiai\w*|breakfast)", re.IGNORECASE)

LABEL = {"exact": "only this product", "ask": "substitutes need the buyer's yes", "allowed": "substitutes allowed"}


class ProductPurpose(BaseModel):
    purpose: str = Field(max_length=120, description="What the product is for, e.g. 'recipe: lasagne', 'coffee'")
    substitution: Substitution | None = Field(
        None, description="exact | ask | allowed; default read from the purpose")


def infer(purpose: str | None) -> Substitution | None:
    """The strictness a purpose implies, or None when it says nothing about it."""
    if not purpose:
        return None
    if _EXACT.search(purpose):
        return "exact"
    if _ASK.search(purpose):
        return "ask"
    if _ALLOWED.search(purpose):
        return "allowed"
    return None


def resolve(purpose: str | None, substitution: str | None) -> Substitution | None:
    return substitution or infer(purpose)  # type: ignore[return-value]


def strictest(*pairs: tuple[str | None, str | None] | None) -> tuple[Substitution | None, str | None]:
    """(substitution, purpose) from several (purpose, substitution) sources: the strictest substitution wins and
    brings its purpose; a purpose without a level still counts as context."""
    best: tuple[Substitution | None, str | None] = (None, None)
    for pair in pairs:
        if not pair:
            continue
        p, s = pair
        level = resolve(p, s)
        if level and (best[0] is None or RANK[level] > RANK[best[0]]):
            best = (level, p)
        elif best[1] is None and p:
            best = (best[0], p)
    return best


def for_product(mandate, product_id: str | None, item: tuple[str | None, str | None] | None = None,
                standing: tuple[str | None, str | None] | None = None) -> tuple[Substitution | None, str | None]:
    """Effective (substitution, purpose) for one original product: mandate, standing item and quote item."""
    pp = (getattr(mandate, "product_purposes", None) or {}).get(product_id or "") if mandate else None
    m = (pp.purpose, pp.substitution) if pp else None
    return strictest(m, standing, item)


def locked(mandate) -> dict[str, ProductPurpose]:
    """The mandate's products whose substitution is exact or ask (Valio's side must respect these)."""
    out = {}
    for pid, pp in (getattr(mandate, "product_purposes", None) or {}).items():
        if resolve(pp.purpose, pp.substitution) in ("exact", "ask"):
            out[pid] = pp
    return out
