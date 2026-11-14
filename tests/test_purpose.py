"""Purpose per item (Valio Aimo's feedback): coffee cream can be any brand, whipping cream for a recipe may not whip
the same, and in an allergy recipe only one exact product works. exact refuses a substitute and Valio proposes
none; ask makes a substitute the buyer's call; an agent can only make an item stricter than the buyer's mandate."""

from datetime import date

import pytest
from test_standards import MON, env, issue  # noqa: F401  (env is autouse)

from sous import amendments as AM, purpose as P, service as S, substitutes
from sous.mandate import Mandate, evaluate
from tests.test_core import NOW


def test_purpose_words_imply_a_level():
    assert P.infer("coffee") == "allowed" and P.infer("kahviin") == "allowed"
    assert P.infer("recipe: lasagne") == "ask" and P.infer("kuohukerma jälkiruokaan") == "ask"
    assert P.infer("allergy menu (gluten-free)") == "exact" and P.infer("vain tämä tuote") == "exact"
    assert P.infer("weekly stock") is None and P.infer(None) is None


def test_strictest_wins_and_an_agent_cannot_loosen_the_mandate():
    m = Mandate(id="M", buyer="B", agent_id="a", issued_by="h",
                product_purposes={"C1": {"purpose": "allergy menu", "substitution": "exact"}})
    assert P.for_product(m, "C1", ("coffee", "allowed")) == ("exact", "allergy menu")
    assert P.for_product(m, "X", ("recipe: soup", None)) == ("ask", "recipe: soup")
    assert P.for_product(None, "X", ("coffee", None)) == ("allowed", "coffee")


def _line(pid, **kw):
    return {"product_id": pid, "name": f"Product {pid}", "orderable": True, "line_total_ex_vat": 10.0, "quantity": 1,
            "sales_unit": "ST", "issues": [], "unit_price_ex_vat": 10.0, **kw}


def _rules(m, line, orig="ORIG"):
    _, fs = evaluate(m, "a", [line], requested_date=date(2026, 11, 16), now=NOW, substitute_for={line["product_id"]: orig})
    return {f.rule: f.outcome for f in fs}


def test_substitute_for_an_exact_product_is_refused_and_ask_is_held():
    m = Mandate(id="M", buyer="B", agent_id="a", issued_by="h", substitutions="silent",
                product_purposes={"ORIG": {"purpose": "allergy recipe", "substitution": "exact"}})
    assert _rules(m, _line("SUB"))["purpose.exact"] == "block"
    m2 = Mandate(id="M", buyer="B", agent_id="a", issued_by="h", substitutions="silent")
    r = _rules(m2, _line("SUB", purpose="recipe: whipped cream dessert"))
    assert r["purpose.ask"] == "ask"  # even though the mandate lets substitutes through silently
    r = _rules(m2, _line("SUB", purpose="coffee"))
    assert "purpose.ask" not in r and "purpose.exact" not in r  # coffee cream: any brand


def test_substitutes_suggest_none_for_an_exact_product_without_live_calls(monkeypatch):
    monkeypatch.setattr(substitutes.catalog, "get", lambda pid: {"id": pid, "name": "Cream 2 dl"})
    monkeypatch.setattr(substitutes.Q, "offers", lambda *a, **k: pytest.fail("no live lookup for an exact product"))
    m = Mandate(id="M", buyer="B", agent_id="a", issued_by="h",
                product_purposes={"C": {"purpose": "coeliac menu", "substitution": "exact"}})
    r = substitutes.suggest("C", date(2026, 11, 16), 1, None, m)
    assert r["options"] == [] and "only this product works" in r["no_safe_substitute"]
    r = substitutes.suggest("C", date(2026, 11, 16), 1, None, None, purpose="allergy menu")
    assert r["options"] == [] and r["constraints"]["substitution"] == "exact"


def test_quote_lines_carry_the_items_purpose_and_order_lines_keep_it():
    issue()
    q = S.request_quote(S.QuoteRequest(agent_id="kide-agent", mandate_id="M-STD", delivery_date=MON, items=[
        S.QuoteItem(product_ids=["MILK"], amount=10, unit="L", purpose="coffee")]))["quote"]
    (l,) = q["lines"]
    assert l["purpose"] == "coffee" and l["substitution"] == "allowed"
    o = S.place_order("kide-agent", "M-STD", q["id"])
    assert o["lines"][0]["purpose"] == "coffee"


def test_mandate_product_purpose_reaches_the_quote_line():
    issue(product_purposes={"MILK": {"purpose": "recipe: rice pudding"}})
    q = S.request_quote(S.QuoteRequest(agent_id="kide-agent", mandate_id="M-STD", delivery_date=MON, items=[
        S.QuoteItem(product_ids=["MILK"], amount=10, unit="L", purpose="coffee", substitution="allowed")]))["quote"]
    assert q["lines"][0]["substitution"] == "ask" and q["lines"][0]["purpose"] == "recipe: rice pudding"


def test_valio_substitution_at_pick_respects_an_exact_purpose(monkeypatch):
    from test_standards import MILK
    from sous import catalog
    monkeypatch.setattr(catalog, "get", lambda pid: {**MILK, "id": pid} if pid in ("MILK", "MILK2") else None)
    issue(product_purposes={"MILK": {"purpose": "allergy menu", "substitution": "exact"}})
    q = S.request_quote(S.QuoteRequest(agent_id="kide-agent", mandate_id="M-STD", delivery_date=MON, items=[
        S.QuoteItem(product_ids=["MILK"], amount=10, unit="L")]))["quote"]
    o = S.place_order("kide-agent", "M-STD", q["id"])
    a = AM.report(o["id"], AM.AmendmentIn(kind="substitution_at_pick", line=0, product_id="MILK2", quantity=10,
                                          reason="out at the warehouse"), "global approver code")
    am = (a.get("amendments") or [a.get("amendment") or a])[-1]
    assert am["status"] == "pending_buyer" and am["tier"] == "block"
    assert "purpose.exact" in {f["rule"] for f in am["findings"]}
