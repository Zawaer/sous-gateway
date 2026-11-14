"""Offline tests for the deterministic core: no Valio API calls, no data files needed."""

from datetime import date, datetime, timedelta

import pytest

from sous import catalog, config, quote as Q, signing, store
from sous.mandate import Mandate, evaluate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)


def product(**kw):
    p = {
        "id": "1", "name": "Test milk 1 l lactose free", "names": {"fi": "Testimaito 1 l", "en": "", "sv": ""},
        "brand": "", "vendor": "", "category": "10400", "country_of_origin": "fi",
        "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0, "KI": 10.0}, "allowed_lot_size": 0.01,
        "temperature": "chilled", "labels": [], "claims": ["FREE_FROM_LACTOSE"],
        "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": []},
        "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": 1.4,
        "official_substitutes": [], "deleted": False,
    }
    p.update(kw)
    return p


PRICE = {"catalogPrice": {"value": 1.4, "valueWithTax": 1.59, "validTo": "2026-12-31T00:00:00+0200"},
         "comparisonPrice": {"value": 1.4}, "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}


def avail(model="Stock", amount=500.0, closing="2026-11-15T17:00:00+0200", day="2026-11-16", lines=True):
    return {"logisticModel": model, "closingTime": closing, "salesBlock": False,
            "scheduleLines": [{"salesBlock": False, "availableAmount": amount, "availableDate": day,
                               "salesUnit": "ST"}] if lines else []}


# --- units and quantities ------------------------------------------------------

def test_parse_size():
    assert catalog.parse_size("Valio Eila 3 % maitojuoma 1 l laktoositon") == (1.0, "L")
    assert catalog.parse_size("Arla portion milk 100x2 cl") == (2.0, "L")
    assert catalog.parse_size("Valio voi 500 g") == (0.5, "KG")
    assert catalog.parse_size("Paper napkins") is None


def test_content_uses_pack_hierarchy():
    p = product(sales_unit="KI")
    price = {**PRICE, "catalogPrice": {**PRICE["catalogPrice"], "value": 14.0}}
    assert Q.content_per_sales_unit(p, price)[:2] == (10.0, "L")


def test_content_falls_back_to_price_ratio_when_name_disagrees():
    p = product(size_from_name=(5.0, "L"))
    assert Q.content_per_sales_unit(p, PRICE)[:2] == (1.0, "L")


def test_lot_size_rounding():
    assert Q.round_to_lot(7.2, "ST", 0.01) == 8
    assert Q.round_to_lot(7, "KI", 3.0) == 9
    assert Q.round_to_lot(2.345, "KG", 0.01) == 2.35


# --- exceptions ----------------------------------------------------------------

def codes(o):
    return {i["code"] for i in o["issues"]}


def test_happy_path_offer():
    o = Q.build_offer(product(), PRICE, avail(), date(2026, 11, 16), 1000, "L", NOW)
    assert o["orderable"] and o["quantity"] == 1000 and o["line_total_ex_vat"] == 1400.0


def test_cutoff_passed_blocks_even_if_api_still_reports_date():
    o = Q.build_offer(product(), PRICE, avail("Terminal", 999999, "2026-11-12T15:00:00+0200"),
                      date(2026, 11, 16), 10, "L", NOW)
    assert not o["orderable"] and {"CUTOFF_PASSED", "STOCK_UNKNOWN"} <= codes(o)


def test_undeliverable_and_out_of_stock():
    assert "UNDELIVERABLE" in codes(Q.build_offer(product(), PRICE, avail(lines=False), date(2026, 11, 16), 1, "L", NOW))
    o = Q.build_offer(product(), PRICE, avail(amount=0.0, day=None), date(2026, 11, 16), 1, "L", NOW)
    assert "OUT_OF_STOCK" in codes(o) and not o["orderable"]


def test_later_date_and_partial_stock_are_warnings():
    o = Q.build_offer(product(), PRICE, avail(amount=100, day="2026-11-18"), date(2026, 11, 16), 500, "L", NOW)
    assert o["orderable"] and {"LATER_DATE", "PARTIAL_STOCK"} <= codes(o)


def test_plan_splits_when_stock_short():
    cheap = Q.build_offer(product(id="A"), PRICE, avail(amount=300), date(2026, 11, 16), 1000, "L", NOW)
    dear = Q.build_offer(product(id="B"), {**PRICE, "comparisonPrice": {"value": 1.6},
                                           "catalogPrice": {**PRICE["catalogPrice"], "value": 1.6}},
                         avail(amount=5000), date(2026, 11, 16), 1000, "L", NOW)
    lines, left = Q.plan(1000, "L", [dear, cheap])
    assert [(l["product_id"], l["quantity"]) for l in lines] == [("A", 300), ("B", 700)] and left == 0


# --- allergens and the mandate --------------------------------------------------

def test_allergen_unknown_is_never_safe():
    p = product(allergens={"declared": False, "contains": [], "may_contain": [], "free_from": []})
    assert catalog.allergen_status(p, ["AM"])["status"] == "unknown"
    assert catalog.allergen_status(product(), ["AM"])["status"] == "contains"
    # family: avoiding tree nuts also catches hazelnut
    p = product(allergens={"declared": True, "contains": ["SH"], "may_contain": [], "free_from": []})
    assert catalog.allergen_status(p, ["AN"])["status"] == "contains"


def mandate(**kw):
    return Mandate(**{"id": "M", "buyer": "B", "agent_id": "a", "issued_by": "h", **kw})


def line(**kw):
    o = Q.build_offer(product(), PRICE, avail(), date(2026, 11, 16), 100, "L", NOW)
    o.update(kw)
    return o


def run(m, lines, agent="a", **kw):
    return evaluate(m, agent, lines, requested_date=date(2026, 11, 16), now=NOW, **kw)


def test_four_tiers():
    assert run(mandate(), [line()])[0] == "silent"
    assert run(mandate(notify_above_eur=100), [line()])[0] == "notify"
    assert run(mandate(auto_approve_limit_eur=100), [line()])[0] == "ask"
    assert run(mandate(hard_limit_eur=100), [line()])[0] == "block"


def test_wrong_agent_and_revoked_mandate_block():
    assert run(mandate(), [line()], agent="someone-else")[0] == "block"
    assert run(mandate(revoked=True), [line()])[0] == "block"
    assert run(mandate(valid_until=NOW - timedelta(days=1)), [line()])[0] == "block"


def test_allergen_rules(monkeypatch):
    unknown = product(allergens={"declared": False, "contains": [], "may_contain": [], "free_from": []})
    monkeypatch.setattr(catalog, "get", lambda pid: unknown)
    assert run(mandate(avoid_allergens=["AM"]), [line()])[0] == "ask"
    assert run(mandate(avoid_allergens=["AM"], unknown_allergens="block"), [line()])[0] == "block"
    monkeypatch.setattr(catalog, "get", lambda pid: product())
    out, findings = run(mandate(avoid_allergens=["AM"]), [line()])
    assert out == "block" and any(f.rule == "allergen.contains" for f in findings)


def test_substitution_and_blocking_issue(monkeypatch):
    monkeypatch.setattr(catalog, "get", lambda pid: product())
    assert run(mandate(substitutions="ask"), [line()], substitute_for={"1": "2"})[0] == "ask"
    blocked = line(issues=[{"code": "CUTOFF_PASSED", "severity": "blocking", "message": "late"}])
    assert run(mandate(), [blocked])[0] == "block"


# --- trust: signatures and the audit chain ---------------------------------------

def test_jws_roundtrip_and_tamper():
    tok = signing.sign({"id": "Q-1", "total": 10}, "test+jws")
    assert signing.verify(tok) == {"id": "Q-1", "total": 10}
    h, p, s = tok.split(".")
    forged = signing._b64(signing.canonical({"id": "Q-1", "total": 1}))
    assert signing.verify(f"{h}.{forged}.{s}") is None


def test_audit_chain_detects_edits(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(config, "DB_URL", "")
    for i in range(3):
        store.audit("test.event", "tester", "system", f"S{i}", None, {"i": i})
    assert store.verify_chain()["ok"]
    store.db().execute("UPDATE audit_event SET data = '{\"i\": 99}' WHERE seq = 2")
    r = store.verify_chain()
    assert (r["ok"], r["events"], r["broken_at_seq"]) == (False, 1, 2)


def test_audit_chain_with_a_saved_head_detects_a_cut_tail(tmp_path, monkeypatch):
    """Deleting the newest events leaves a shorter chain that still verifies; a head saved earlier catches it."""
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(config, "DB_URL", "")
    for i in range(3):
        store.audit("test.event", "tester", "system", f"S{i}", None, {"i": i})
    head = store.verify_chain()["head"]
    store.audit("test.event", "tester", "system", "S3", None, {"i": 3})
    assert store.verify_chain(head)["ok"] and store.verify_chain(head)["saved_head"] == {"found": True, "seq": 3}
    store.db().execute("DELETE FROM audit_event WHERE seq >= 3")
    assert store.verify_chain()["ok"]  # the chain alone can't tell
    r = store.verify_chain(head)
    assert r["ok"] is False and r["saved_head"] == {"found": False, "seq": None}


@pytest.fixture(autouse=True)
def _signing_key(tmp_path, monkeypatch):
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    yield
    signing._key.cache_clear()


@pytest.fixture(autouse=True)
def _offline_catalog(monkeypatch):
    """Tests never touch data/ (CI has no Valio data files); individual tests override this."""
    monkeypatch.setattr(catalog, "get", lambda pid: product(id=pid))


def test_partial_stock_on_single_product_asks(monkeypatch):
    short = Q.build_offer(product(), PRICE, avail(amount=50), date(2026, 11, 16), 100, "L", NOW)
    out, findings = run(mandate(), [short])
    assert out == "ask" and any(f.rule == "availability.partial_stock" for f in findings)


def test_rename_compat_env_and_legacy_jws(monkeypatch):
    """Valtakirja -> Sous rename: old env names and old signature types keep working."""
    monkeypatch.delenv("SOUS_QUOTE_TTL", raising=False)
    monkeypatch.setenv("VALTAKIRJA_QUOTE_TTL", "123")
    assert config.env("QUOTE_TTL") == "123"
    monkeypatch.setenv("SOUS_QUOTE_TTL", "456")
    assert config.env("QUOTE_TTL") == "456"
    legacy = signing.sign({"id": "M-1"}, "valtakirja-mandate+jws")
    assert signing.verify(legacy, typ="sous-mandate+jws") == {"id": "M-1"}
    assert signing.verify(signing.sign({"id": "Q"}, "sous-quote+jws"), typ="sous-mandate+jws") is None


def test_name_pack_count_not_multiplied_twice():
    """'4x1kg' sold per KI of 4 bags is 4 kg per KI, not 16 (no comparison price to cross-check)."""
    p = product(names={"fi": "Paulig Presidentti 4x1kg kahvipavut", "en": "", "sv": ""},
                sales_unit="KI", base_unit="BAG", units={"BAG": 1.0, "KI": 4.0}, size_from_name=(4.0, "KG"))
    assert Q.content_per_sales_unit(p, None)[:2] == (4.0, "KG")


def test_search_cards_show_the_case_size_from_the_catalog():
    """A case of 6 x 0,5 l sold per KI: the card says 3 L per KI (no price call), as a quote would."""
    p = product(name="Kuohukerma 0,5 l", names={"fi": "Kuohukerma 0,5 l", "en": "", "sv": ""}, sales_unit="KI",
                base_unit="ST", units={"ST": 1.0, "KI": 6.0}, size_from_name=(0.5, "L"))
    c = catalog.card(p)["content_per_sales_unit"]
    assert (c["amount"], c["unit"], c["source"]) == (3.0, "L", "pack size from product name")
    assert c["base_units_per_sales_unit"] == 6 and "no price call" in c["basis"]
    assert c["amount"] == Q.content_per_sales_unit(p, None)[0]
    assert catalog.card(product(size_from_name=None))["content_per_sales_unit"] is None
    assert catalog.card(product(sales_unit="KG", size_from_name=None))["content_per_sales_unit"]["unit"] == "KG"


def test_reasons_use_readable_names():
    from sous.mandate import readable_name
    assert readable_name("BRAVO CHEF 400G CHOPPED TOMATOES") == "Bravo chef 400g chopped tomatoes"
    assert readable_name("Valio Eila 3 % maitojuoma 1 l") == "Valio Eila 3 % maitojuoma 1 l"


def test_valio_unknown_marker_on_stock_line_is_not_known_stock():
    """Valio: availableAmount >= 999999 on any line means unknown, any quantity may be ordered."""
    o = Q.build_offer(product(), PRICE, avail("Stock", 999999), date(2026, 11, 16), 5000, "L", NOW)
    assert o["orderable"] and "PARTIAL_STOCK" not in codes(o) and o["quantity"] == 5000
    from sous import aimo
    assert aimo.DEFAULT_PAUSE == 30.0
