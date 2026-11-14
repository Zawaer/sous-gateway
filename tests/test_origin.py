"""Country of origin: supplier-declared, shown on quote lines, restricted by the mandate, unknown never allowed."""

from datetime import date

import pytest
from pydantic import ValidationError

from sous import catalog, origin
from sous.mandate import Mandate, evaluate
from sous.rules import get as rule
from tests.test_core import NOW, product

FI = product(id="F1", name="Valio milk 1 l", country_of_origin="fi")
DE = product(id="D1", name="Arla milk 1 l", country_of_origin="de")
CN = product(id="C1", name="Import milk 1 l", country_of_origin="cn")
NONE = product(id="N1", name="Mystery milk 1 l", country_of_origin="")
ZZ = product(id="Z1", name="Placeholder milk 1 l", country_of_origin="zz")
BY_ID = {p["id"]: p for p in (FI, DE, CN, NONE, ZZ)}


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    monkeypatch.setattr(catalog, "get", lambda pid: BY_ID.get(pid))


def mandate(**kw):
    return Mandate(**{"id": "M", "buyer": "B", "agent_id": "a", "issued_by": "h", **kw})


def line(pid):
    return {"product_id": pid, "name": BY_ID[pid]["name"], "orderable": True, "line_total_ex_vat": 10.0,
            "quantity": 10, "sales_unit": "ST", "issues": [], "unit_price_ex_vat": 1.0,
            "country_of_origin": origin.code(BY_ID[pid]["country_of_origin"])}


def origin_findings(m, *pids):
    _, findings = evaluate(m, "a", [line(p) for p in pids], requested_date=date(2026, 11, 16), now=NOW)
    return [f for f in findings if f.rule.startswith("origin.")]


def test_no_restriction_changes_nothing():
    assert origin_findings(mandate(), "F1", "C1", "N1") == []


def test_groups_expand_and_check():
    assert origin.check("de", ["EU"], None) == "ok"
    assert origin.check("cn", ["EU"], None) == "outside"
    assert origin.check("se", ["NORDIC"], None) == "ok"
    assert origin.check("de", ["NORDIC"], None) == "outside"
    assert origin.check("fi", None, ["fi"]) == "avoided"
    assert origin.check("zz", ["EU"], None) == "unknown"
    assert origin.check("", None, ["cn"]) == "unknown"
    assert origin.check("cn", None, None) == "ok"


def test_allowed_origins_hold_outside_goods_by_default():
    f = origin_findings(mandate(allowed_origins=["EU"]), "F1", "D1", "C1")
    assert [(x.rule, x.outcome, x.product_id) for x in f] == [("origin.restricted", "ask", "C1")]
    assert "China" in f[0].message and "supplier-declared" in f[0].message and "EU" in f[0].message


def test_origin_violation_can_block():
    f = origin_findings(mandate(allowed_origins=["fi"], origin_violation="block"), "D1")
    assert f[0].outcome == "block"
    tier, _ = evaluate(mandate(allowed_origins=["fi"], origin_violation="block"), "a", [line("D1")],
                       requested_date=date(2026, 11, 16), now=NOW)
    assert tier == "block"


def test_avoid_origins():
    f = origin_findings(mandate(avoid_origins=["cn", "ru"]), "F1", "C1")
    assert [(x.rule, x.product_id) for x in f] == [("origin.restricted", "C1")]
    assert "avoids China, Russia" in f[0].message


def test_unknown_origin_is_never_treated_as_allowed():
    for pid in ("N1", "Z1"):
        f = origin_findings(mandate(allowed_origins=["fi"]), pid)
        assert [(x.rule, x.outcome) for x in f] == [("origin.unknown", "ask")]
    f = origin_findings(mandate(allowed_origins=["fi"], origin_unknown="block"), "N1")
    assert f[0].outcome == "block"
    assert origin_findings(mandate(), "N1") == []  # only when the mandate restricts origin


def test_invalid_origin_rejected_and_normalised():
    with pytest.raises(ValidationError):
        mandate(allowed_origins=["Atlantis"])
    assert mandate(allowed_origins=["FI", "eu", "se"]).allowed_origins == ["fi", "EU", "se"]


def test_summary_shares_by_value():
    s = origin.summary([line("F1"), line("F1"), line("D1"), line("N1")])
    assert s["domestic_share"] == 0.5 and s["unknown_share"] == 0.25
    assert [r["country"] for r in s["countries"]] == ["fi", "de", "unknown"]
    assert "not independently verified" in s["basis"]


def test_rules_are_registered_with_mandate_fields():
    r = rule("origin.restricted")
    assert r["owner"] == "buyer" and set(r["tiers"]) == {"ask", "block"}
    assert "allowed_origins" in r["mandate_field"]
    assert rule("origin.unknown")["tiers"] == ["notify", "ask", "block"]


def test_quote_line_carries_origin_and_its_basis():
    from sous import quote as Q
    from tests.test_core import PRICE, avail
    o = Q.build_offer(product(country_of_origin="de"), PRICE, avail(), date(2026, 11, 16), 1000, "L", NOW)
    assert o["country_of_origin"] == "de" and o["origin_name"] == "Germany"
    assert "supplier-declared" in o["origin_basis"]
    o = Q.build_offer(product(country_of_origin="zz"), PRICE, avail(), date(2026, 11, 16), 1000, "L", NOW)
    assert o["country_of_origin"] is None and o["origin_name"] is None
