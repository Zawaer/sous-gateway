"""Inventory must preserve the requested product's explicit salt specification."""
from datetime import date

import pytest

from agent.sous_agent import parse_need


@pytest.mark.parametrize("requested,stock", [
    ("unsalted", "salted"), ("salted", "unsalted"),
    ("reduced-salt", "salted"), ("salted", "reduced-salt"),
    ("unsalted", ""), ("", "salted"),
])
def test_conflicting_or_missing_salt_spec_does_not_reduce_need(requested, stock):
    need = parse_need(
        f"We need 14 kg {requested} butter in total; we have 4 kg {stock} butter in stock",
        date(2026, 11, 14), date(2026, 11, 16),
    )
    assert len(need.items) == 1
    assert need.items[0].amount == 14
    assert need.items[0].on_hand is None
    assert need.stock


@pytest.mark.parametrize("spec", ["unsalted", "salted", "reduced-salt", ""])
def test_matching_salt_spec_still_subtracts_stock(spec):
    need = parse_need(
        f"We need 14 kg {spec} butter in total; we have 4 kg {spec} butter in stock",
        date(2026, 11, 14), date(2026, 11, 16),
    )
    assert len(need.items) == 1
    assert need.items[0].amount == 10
    assert need.items[0].on_hand == 4
    assert not need.risks


def test_finnish_unsalted_need_keeps_salted_stock_separate():
    need = parse_need(
        "Tarvitsemme yhteensä 14 kg suolatonta voita; meillä on jo 4 kg normaalisuolaista voita",
        date(2026, 11, 14), date(2026, 11, 16),
    )
    assert len(need.items) == 1
    assert need.items[0].amount == 14
    assert need.items[0].on_hand is None


def test_finnish_unsalted_synonyms_still_match_stock():
    need = parse_need(
        "Tarvitsemme yhteensä 14 kg suolatonta voita; meillä on jo 4 kg suolaamatonta voita",
        date(2026, 11, 14), date(2026, 11, 16),
    )
    assert len(need.items) == 1
    assert need.items[0].amount == 10
    assert need.items[0].on_hand == 4
