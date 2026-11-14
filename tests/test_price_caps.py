"""Comparison-price caps scoped to a kind of product (comparison_price_caps) next to the old per-unit cap."""

from datetime import date

import pytest
from pydantic import ValidationError

from sous import catalog
from sous.mandate import Mandate, comparison_cap, evaluate
from sous.rules import get as rule
from tests.test_core import NOW, product

MILK = product(id="M1", name="Valio semi skimmed milk 1 l lactose free", category="10400",
               names={"fi": "Valio laktoositon kevytmaito 1 l", "en": "Valio semi skimmed milk 1 l lactose free"})
CREAM = product(id="C1", name="Valio coffee cream 1 l lactose free", category="10401",
                names={"fi": "Valio laktoositon kahvikerma 1 l", "en": "Valio coffee cream 1 l lactose free"})


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    monkeypatch.setattr(catalog, "get", lambda pid: {"M1": MILK, "C1": CREAM}.get(pid))


def mandate(**kw):
    return Mandate(**{"id": "M", "buyer": "B", "agent_id": "a", "issued_by": "h", **kw})


def line(pid, per_l):
    return {"product_id": pid, "name": {"M1": MILK, "C1": CREAM}[pid]["name"], "orderable": True,
            "line_total_ex_vat": 10 * per_l, "quantity": 10, "sales_unit": "ST", "issues": [],
            "unit_price_ex_vat": per_l, "comparison_price": {"ex_vat": per_l, "per": "L"}}


def caps(m, *lines):
    out, findings = evaluate(m, "a", list(lines), requested_date=date(2026, 11, 16), now=NOW)
    return [f for f in findings if f.rule == "price.comparison_cap"]


def test_milk_cap_holds_milk_but_not_cream():
    m = mandate(comparison_price_caps={"milk": 2.5})
    assert not caps(m, line("C1", 2.72))  # cream is not milk: the hotel's milk cap leaves it alone
    held = caps(m, line("M1", 2.6))
    assert len(held) == 1 and held[0].product_id == "M1"
    assert "€2.50/L cap for milk" in held[0].message
    assert not caps(m, line("M1", 2.5))  # exactly the cap passes


def test_old_per_unit_cap_still_applies_to_everything_sold_by_the_litre():
    m = mandate(max_comparison_price_eur={"L": 2.5})
    assert [f.product_id for f in caps(m, line("M1", 2.6), line("C1", 2.72))] == ["M1", "C1"]
    assert "for everything priced per L" in caps(m, line("C1", 2.72))[0].message


def test_most_specific_cap_wins():
    m = mandate(max_comparison_price_eur={"L": 2.5}, comparison_price_caps={"cream": 6.0, "kahvikerma": 3.0})
    assert comparison_cap(m, "C1", "L") == (3.0, "coffee cream")  # product type before family
    assert not caps(m, line("C1", 2.72))
    assert caps(m, line("C1", 3.5))
    assert caps(m, line("M1", 2.6))  # milk has no scoped cap: the per-unit cap still holds it
    by_category = mandate(comparison_price_caps={"10401": 2.0})
    assert comparison_cap(by_category, "C1", "L") == (2.0, "category 10401")
    assert comparison_cap(by_category, "M1", "L") is None


def test_caps_must_be_positive_and_are_in_the_rule_register():
    with pytest.raises(ValidationError):
        mandate(comparison_price_caps={"milk": 0})
    r = rule("price.comparison_cap")
    assert r["mandate_field"] == ["comparison_price_caps", "max_comparison_price_eur"]
    assert "milk" in r["text"] and "product family" in r["mandate_field_description"]


def test_seeded_hotel_mandates_cap_milk_only():
    from scripts.seed import MANDATES
    hotels = [m for m in MANDATES if m.id.startswith("M-HOTEL-AURORA")]
    assert hotels and all(m.comparison_price_caps == {"milk": 2.5} and not m.max_comparison_price_eur
                          for m in hotels)
