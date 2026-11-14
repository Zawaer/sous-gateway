"""Finnish explanations (sous/fi.py): every rule has text_fi, findings and approval reasons have Finnish twins in
unsigned fields, English output and signatures are unchanged."""

from datetime import datetime

from fastapi.testclient import TestClient

from sous import config, fi, notify, rules, service as S, signing, store
from sous.mandate import Mandate, evaluate
from tests.test_amendments import MON, NOW, env, issue  # noqa: F401  offline setup: EUR 1.00 per unit


def _line(pid="OAT", total=1234.5, **kw):
    return {"product_id": pid, "name": "OAT DRINK 1 L", "quantity": 10, "sales_unit": "ST", "unit_price_ex_vat": 1.0,
            "line_total_ex_vat": total, "issues": [], "claims": [], "orderable": True, **kw}


def _mandate(**kw):
    return Mandate(**{"id": "M-FI", "buyer": "Kahvila", "agent_id": "a", "issued_by": "Anna", **kw})


# --- formatting --------------------------------------------------------------------------------------------------

def test_finnish_money_and_dates():
    assert fi.eur(1234.5) == "1 234,50 €"
    assert fi.eur(500, whole=True) == "500 €" and fi.eur(2.5, whole=True) == "2,50 €"
    assert fi.pct(20) == "20 %" and fi.ratio(0.9) == "90 %"
    assert fi.day("2026-11-15") == "15.11.2026"
    assert fi.when("2026-11-15T16:45:00+02:00") == "15.11.2026 klo 16.45"
    assert fi.when(datetime(2026, 11, 15, 14, 45, tzinfo=config.TZ)) == "15.11.2026 klo 14.45"


# --- the rule register -------------------------------------------------------------------------------------------

def test_every_rule_has_finnish_text():
    for r in rules.register():
        assert r["text_fi"], r["id"]
        assert "{" not in r["text_fi"] and r["text_fi"] != r["text"], r["id"]


def test_rules_endpoint_in_finnish_and_english_unchanged():
    c = TestClient(__import__("sous.api", fromlist=["app"]).app)
    en = c.get("/rules").json()
    assert en["owners"] == rules.OWNERS and "lang" not in en
    hard = next(r for r in en["rules"] if r["id"] == "spend.hard_limit")
    assert hard["text"].startswith("An order above the hard limit") and hard["text_fi"].startswith("Ehdottoman")
    fi_doc = c.get("/rules?lang=fi").json()
    assert fi_doc["lang"] == "fi" and fi_doc["owners"] == fi.OWNERS and fi_doc["count"] == en["count"]
    one = c.get("/rules/spend.hard_limit?lang=fi").json()
    assert one["text"] == hard["text_fi"] and one["text_en"] == hard["text"]
    assert c.get("/rules/spend.hard_limit", headers={"Accept-Language": "fi-FI,fi;q=0.9"}).json()["text"] == hard["text_fi"]
    assert c.get("/rules/spend.hard_limit").json()["text"] == hard["text"]
    # numbers from code are in the Finnish text too
    assert "15 %" in rules.get("standing.quantity_creep")["text_fi"]


def test_mandate_summary_lines_have_finnish():
    m = _mandate(notify_above_eur=200, daily_limit_eur=1500, avoid_allergens=["AM"], allowed_origins=["fi", "EU"],
                 max_comparison_price_eur={"L": 2.5}, valid_until=datetime(2026, 11, 15, 16, 45, tzinfo=config.TZ))
    s = rules.mandate_summary(m)
    by = {x["rule"]: x["text_fi"] for x in s}
    assert all(x["text_fi"] != x["text"] for x in s)
    assert by["spend.notify"] == "ilmoitus, kun tilaus on yli 200 €"
    assert by["spend.daily_limit"] == "yli 1 500 € päivässä vaatii hyväksynnän"
    assert by["price.comparison_cap"].startswith("hintakatto: kaikki litrahintaiset enintään 2,50 €/l")
    assert "maito" in by["allergen.contains"] and "Suomi, EU" in by["origin.restricted"]
    assert by["mandate.expired"] == "voimassa 15.11.2026 klo 16.45 asti"
    assert s[0]["text"] == "FYI above €200 an order"  # English unchanged


# --- findings ----------------------------------------------------------------------------------------------------

def test_spend_findings_in_finnish_with_the_same_numbers():
    m = _mandate(auto_approve_limit_eur=500, weekly_limit_eur=2000)
    _, found = evaluate(m, "a", [_line()], requested_date=MON, now=NOW, spent_week_eur=1000)
    auto = next(f for f in found if f.rule == "spend.auto_limit")
    assert auto.message == "Order total €1,234.50 is above the €500.00 the agent may spend alone."
    assert auto.message_fi == "Tilauksen summa 1 234,50 € on yli 500,00 €, jonka agentti saa käyttää itse."
    week = next(f for f in found if f.rule == "spend.weekly_limit")
    assert "2 234,50 €" in week.message_fi and "2 000,00 €" in week.message_fi
    assert "message_fi" not in auto.model_dump()  # never dumped: signed payloads stay English


def test_allergen_and_price_findings_in_finnish(monkeypatch):
    from sous import catalog
    monkeypatch.setattr(catalog, "get", lambda pid: {"id": pid, "name": "Kerma 1 l", "category": "10400",
                        "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": []}})
    m = _mandate(avoid_allergens=["AM"])
    _, found = evaluate(m, "a", [_line(total=10)], requested_date=MON, now=NOW,
                        last_prices={"OAT": {"unit_price_ex_vat": 0.8, "order_id": "PO-1", "at": "2026-11-02T10:00:00"}})
    al = next(f for f in found if f.rule == "allergen.contains")
    assert al.message_fi == "Oat drink 1 l sisältää vältettävää allergeenia: maito."
    pc = next(f for f in found if f.rule == "price.changed")
    assert pc.message_fi.startswith("Oat drink 1 l: nyt 1,00 €/kpl, 25 % kalliimpi kuin 2.11. maksettu 0,80 €")
    assert "20 %" in pc.message_fi


# --- unsigned fields next to signed payloads ---------------------------------------------------------------------

def test_quote_and_order_carry_finnish_outside_the_signatures():
    issue(auto_approve_limit_eur=5, hard_limit_eur=50)
    q = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=MON,
                                       items=[S.QuoteItem(product_ids=["OAT"], amount=10, unit="SALES_UNIT")]))
    signed = signing.verify(q["jws"], "sous-quote+jws")
    assert signed == q["quote"] and "explanations_fi" not in signed
    assert "message_fi" not in str(signed) and "Tilauksen summa" not in str(signed)
    findings = q["quote"]["mandate_preview"]["findings"]
    i = next(n for n, f in enumerate(findings) if f["rule"] == "spend.auto_limit")
    assert findings[i]["message"] == "Order total €10.00 is above the €5.00 the agent may spend alone."
    assert q["explanations_fi"]["findings"][str(i)] == ("Tilauksen summa 10,00 € on yli 5,00 €, jonka agentti saa "
                                                        "käyttää itse.")

    o = S.place_order("a", "M", q["quote"]["id"])
    assert o["status"] == "pending_approval"
    receipt = signing.verify(o["receipt_jws"], "sous-order+jws")
    assert receipt and "explanations_fi" not in receipt and receipt["decision"] == o["decision"]
    j = next(n for n, f in enumerate(o["decision"]["findings"]) if f["rule"] == "spend.auto_limit")
    assert o["explanations_fi"]["findings"][str(j)].startswith("Tilauksen summa 10,00 €")
    assert store.get_order(o["id"])["explanations_fi"] == o["explanations_fi"]

    ask = notify.compose_fi("order.ask", o, S._mandate("M"))
    assert ask["message"].startswith("Hyväksyntä tarvitaan: Tilauksen summa 10,00 € on yli 5,00 €")
    assert "klo" in ask["message"]


def test_approval_reason_in_finnish():
    issue(auto_approve_limit_eur=5, hard_limit_eur=50)
    q = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=MON,
                                       items=[S.QuoteItem(product_ids=["OAT"], amount=10, unit="SALES_UNIT")]))
    o = S.place_order("a", "M", q["quote"]["id"])
    S.issue_mandate(Mandate(**{**S._mandate("M").model_dump(), "revoked": True}))
    d = S.decide(o["id"], "Anna", True)
    assert d["status"] == "refused" and d["approval"]["result_reason"] == "Mandate was revoked before approval."
    assert d["explanations_fi"]["result_reason"] == "Valtakirja peruttiin ennen hyväksyntää."
    assert signing.verify(d["receipt_jws"], "sous-order+jws")["approval"]["result_reason"] == d["approval"]["result_reason"]
