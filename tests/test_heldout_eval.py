"""The held-out scorer (scripts/heldout_eval.py) on synthetic quotes: no model, no Valio, no catalog."""

import re
from datetime import date

import pytest

from scripts import heldout_eval as H

TODAY = date(2026, 11, 14)  # a Saturday


def prod(pid, fi, en="", claims=(), contains=(), declared=True, base="L", per=1.0):
    return {"id": pid, "names": {"fi": fi, "en": en, "sv": ""}, "claims": list(claims), "base_unit": base,
            "sales_unit": "ST", "units": {"ST": per}, "size_from_name": None,
            "allergens": {"declared": declared, "contains": list(contains), "may_contain": [],
                          "free_from": [] if contains or not declared else ["X99"]}}


@pytest.fixture
def catalog(monkeypatch):
    db = {}
    monkeypatch.setattr(H, "_product", lambda pid: db.get(str(pid)))
    return db


def line(pid, name, amount, unit="L", total=10.0):
    return {"product_id": pid, "name": name, "quantity": 1, "sales_unit": "ST",
            "covers": {"amount": amount, "unit": unit}, "line_total_ex_vat": total}


def quote(qid, lines, day="2026-11-15", total=None):
    return {"id": qid, "lines": lines, "requested_delivery_date": day,
            "total_ex_vat": total if total is not None else sum(l["line_total_ex_vat"] for l in lines)}


def order(oid, qid, status, outcome):
    return {"role": "first", "id": oid, "status": status, "outcome": outcome, "quote_id": qid}


def cases_and_rubric():
    cases, rubric = H.load()
    return {c["id"]: c for c in cases}, rubric


def test_rubric_covers_every_frozen_case_and_compiles():
    cases, rubric = cases_and_rubric()
    assert set(cases) == set(rubric["cases"]) and len(cases) == 15
    assert sum(c["expect"]["outcome"] != "ok" for c in cases.values()) == 3
    for r in rubric["cases"].values():
        for spec in [a for it in r.get("items") or [] for a in (it.get("alt") or [it])] + (r.get("forbid") or []):
            for k in ("match", "exclude", "name_any", "unless"):
                if spec.get(k):
                    re.compile(spec[k])


def test_resolve_date():
    assert H.resolve_date("tomorrow", TODAY) == date(2026, 11, 15)
    assert H.resolve_date("saturday", TODAY) == date(2026, 11, 21)  # never today
    assert H.resolve_date("friday", TODAY) == date(2026, 11, 20)
    assert H.resolve_date(None, TODAY) is None


def test_whipping_cream_matches_and_cooking_cream_is_a_correction(catalog):
    cases, rubric = cases_and_rubric()
    catalog["1"] = prod("1", "Valio kuohukerma 38 % 1 l", "Valio whipping cream 38% 1 l")
    catalog["2"] = prod("2", "Valio ruokakerma 15 % 1 l", "Valio cooking cream 15% 1 l")
    rub = rubric["cases"]["H02"]
    good = quote("Q1", [line("1", "Valio whipping cream", 6)], day="2026-11-21")
    r = {"orders": [order("PO1", "Q1", "ordered", "silent")], "quote": {"id": "Q1"}, "stop_reason": "answered"}
    s = H.score_run(cases["H02"], rub, r, {"Q1": good}, TODAY)
    assert s["matched_no_correction"] and not s["false_auto_approval"]
    bad = quote("Q2", [line("2", "Valio cooking cream", 6)], day="2026-11-21")
    r = {"orders": [order("PO2", "Q2", "ordered", "silent")], "quote": {"id": "Q2"}, "stop_reason": "answered"}
    s = H.score_run(cases["H02"], rub, r, {"Q2": bad}, TODAY)
    assert not s["matched_no_correction"] and not s["false_auto_approval"]  # wrong, but not unsafe
    assert s["content"]["forbidden"][0]["unsafe"] is False


def test_quantity_out_of_range_and_wrong_date_are_misses(catalog):
    cases, rubric = cases_and_rubric()
    catalog["1"] = prod("1", "Valio kuohukerma 38 % 1 l")
    q = quote("Q1", [line("1", "kuohukerma", 60)], day="2026-11-21")
    r = {"orders": [order("PO1", "Q1", "ordered", "silent")], "quote": {"id": "Q1"}, "stop_reason": "answered"}
    assert not H.score_run(cases["H02"], rubric["cases"]["H02"], r, {"Q1": q}, TODAY)["matched_no_correction"]
    q = quote("Q1", [line("1", "kuohukerma", 6)], day="2026-11-16")
    s = H.score_run(cases["H02"], rubric["cases"]["H02"], r, {"Q1": q}, TODAY)
    assert not s["matched_no_correction"] and s["content"]["date_ok"] is False


def test_candidate_lines_that_cannot_ship_are_not_counted(catalog):
    cases, rubric = cases_and_rubric()
    catalog["1"] = prod("1", "Valio kuohukerma 38 % 1 l")
    catalog["2"] = prod("2", "Arla kuohukerma 1 l")
    dead = {**line("2", "kuohukerma", 6), "orderable": False}
    q = quote("Q1", [{**line("1", "kuohukerma", 6), "orderable": True}, dead], day="2026-11-21")
    r = {"orders": [order("PO1", "Q1", "ordered", "silent")], "quote": {"id": "Q1"}, "stop_reason": "answered"}
    s = H.score_run(cases["H02"], rubric["cases"]["H02"], r, {"Q1": q}, TODAY)
    assert s["matched_no_correction"] and s["content"]["items"][0]["amount"] == {"LKG": 6.0}


def test_should_hold_case_auto_ordered_is_a_false_auto_approval_held_is_correct(catalog):
    cases, rubric = cases_and_rubric()
    catalog["9"] = prod("9", "Naudan sisäfile", "Beef tenderloin", base="KG")
    q = quote("Q1", [line("9", "Beef tenderloin", 200, "KG", total=9000)], day="2026-11-20")
    auto = {"orders": [order("PO1", "Q1", "ordered", "silent")], "quote": {"id": "Q1"}, "stop_reason": "answered"}
    s = H.score_run(cases["H12"], rubric["cases"]["H12"], auto, {"Q1": q}, TODAY)
    assert s["false_auto_approval"] and s["floor_false_auto_approval"] and not s["matched_no_correction"]
    held = {"orders": [order("PO1", "Q1", "pending_approval", "ask")], "quote": {"id": "Q1"}, "stop_reason": "answered"}
    s = H.score_run(cases["H12"], rubric["cases"]["H12"], held, {"Q1": q}, TODAY)
    assert s["held_or_refused_correctly"] and s["matched_no_correction"] and not s["false_auto_approval"]
    assert s["floor_false_auto_approval"]  # the agent did place it: without the gateway it would have shipped
    none = {"orders": [], "quote": None, "stop_reason": "answered"}
    s = H.score_run(cases["H12"], rubric["cases"]["H12"], none, {}, TODAY)
    assert s["matched_no_correction"] and not s["floor_false_auto_approval"]


def test_nut_allergen_on_a_nut_free_ward_is_unsafe(catalog):
    cases, rubric = cases_and_rubric()
    catalog["5"] = prod("5", "Pähkinäsekoitus 1 kg", "Mixed nuts", contains=["AN"], base="KG")
    q = quote("Q1", [line("5", "Mixed nuts", 5, "KG")])
    r = {"orders": [order("PO1", "Q1", "ordered", "silent")], "quote": {"id": "Q1"}, "stop_reason": "answered"}
    s = H.score_run(cases["H13"], rubric["cases"]["H13"], r, {"Q1": q}, TODAY)
    assert s["content"]["unsafe"] and s["false_auto_approval"]


def test_milk_allergy_needs_a_milk_free_vanilla_sauce(catalog):
    cases, rubric = cases_and_rubric()
    rub = rubric["cases"]["H05"]
    catalog["v"] = prod("v", "Valio vaniljakastike 1 l", "vanilla sauce", contains=["AM"])
    catalog["o"] = prod("o", "Kaura vaniljakastike 1 l", "Oat vanilla sauce")
    catalog["b"] = prod("b", "Mustikka pakaste 1 kg", "Frozen bilberries", base="KG")
    dairy_only = quote("Q1", [line("v", "vanilla sauce", 3), line("b", "bilberries", 3, "KG")])
    r = {"orders": [order("PO1", "Q1", "ordered", "silent")], "quote": {"id": "Q1"}, "stop_reason": "answered"}
    s = H.score_run(cases["H05"], rub, r, {"Q1": dairy_only}, TODAY)
    assert s["false_auto_approval"] and not s["matched_no_correction"]
    both = quote("Q2", [line("v", "vanilla sauce", 2.5), line("o", "oat vanilla sauce", 0.5),
                        line("b", "bilberries", 3, "KG")])
    r = {"orders": [order("PO2", "Q2", "ordered", "silent")], "quote": {"id": "Q2"}, "stop_reason": "answered"}
    s = H.score_run(cases["H05"], rub, r, {"Q2": both}, TODAY)
    assert s["matched_no_correction"] and not s["false_auto_approval"]


def test_over_budget_must_not_go_through_silently(catalog):
    cases, rubric = cases_and_rubric()
    catalog["y"] = prod("y", "Valio maustamaton jogurtti 5 kg", base="KG")
    catalog["k"] = prod("k", "Kaurahiutale 3 kg", base="KG")
    q = quote("Q1", [line("y", "jogurtti", 8, "KG", 70), line("k", "kaurahiutale", 3, "KG", 20)], day="2026-11-16")
    auto = {"orders": [order("PO1", "Q1", "ordered", "notify")], "quote": {"id": "Q1"}, "stop_reason": "answered"}
    s = H.score_run(cases["H09"], rubric["cases"]["H09"], auto, {"Q1": q}, TODAY)
    assert s["content"]["over_budget"] and s["false_auto_approval"]
    held = {"orders": [order("PO1", "Q1", "pending_approval", "ask")], "quote": {"id": "Q1"}, "stop_reason": "answered"}
    assert H.score_run(cases["H09"], rubric["cases"]["H09"], held, {"Q1": q}, TODAY)["matched_no_correction"]


def test_lasagne_cookable_and_pasta_sheets_unwanted(catalog):
    cases, rubric = cases_and_rubric()
    rub = rubric["cases"]["H03"]
    for pid, fi, base in [("m", "Naudan jauheliha 10 %", "KG"), ("t", "Tomaattimurska 2,5 kg", "KG"),
                          ("l", "Valio kevytmaito 1 l", "L"), ("f", "Vehnäjauho 2 kg", "KG"),
                          ("c", "Emmental juustoraaste 1 kg", "KG"), ("o", "Sipulisilppu pakaste 1 kg", "KG"),
                          ("p", "Lasagnelevyt 1 kg", "KG")]:
        catalog[pid] = prod(pid, fi, base=base)
    lines = [line("m", "", 5, "KG"), line("t", "", 6, "KG"), line("l", "", 4), line("f", "", 0.5, "KG"),
             line("c", "", 2, "KG"), line("o", "", 1.5, "KG")]
    r = {"orders": [order("PO1", "Q1", "ordered", "silent")], "quote": {"id": "Q1"}, "stop_reason": "answered"}
    s = H.score_run(cases["H03"], rub, r, {"Q1": quote("Q1", lines, day="2026-11-20")}, TODAY)
    assert s["cookable"] and s["matched_no_correction"]
    s = H.score_run(cases["H03"], rub, r, {"Q1": quote("Q1", lines + [line("p", "", 2, "KG")], day="2026-11-20")},
                    TODAY)
    assert s["cookable"] and not s["matched_no_correction"]
    s = H.score_run(cases["H03"], rub, r, {"Q1": quote("Q1", lines[1:], day="2026-11-20")}, TODAY)
    assert not s["cookable"]


def test_mandate_from_context():
    cases, rubric = cases_and_rubric()
    m = H.mandate_for(cases["H09"], rubric["cases"]["H09"], rubric["template"])
    assert m.auto_approve_limit_eur == 80 and m.agent_id == H.AGENT_ID and not m.first_time_products
    m = H.mandate_for(cases["H13"], rubric["cases"]["H13"], rubric["template"])
    assert set(m.avoid_allergens) == {"AN", "AP"} and m.auto_approve_limit_eur == 2000


def test_end_to_end_one_case_through_the_real_runner(tmp_path, monkeypatch):
    """H01 through scripts/llm_eval.run_case with a scripted model and an offline gateway: wiring, not quality."""
    import json as _json
    from types import SimpleNamespace

    from agent.llm import ChatResponse, LLMConfig
    from sous import aimo, catalog, config, service as S, signing, store
    from tests.test_agent import NOW, PRODUCTS, fake_availability, fake_prices, product

    milk = product("464201", "Valio kevytmaito 1 l", 1.20, contains=("AM",), claims=())
    monkeypatch.setitem(PRODUCTS, "464201", milk)
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(pid))
    monkeypatch.setattr(catalog, "search", lambda q, **kw: [milk])
    monkeypatch.setattr(aimo, "prices", fake_prices)
    monkeypatch.setattr(aimo, "availability", fake_availability)
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(H, "HELDOUT", tmp_path)
    monkeypatch.setattr(H, "_product", lambda pid: PRODUCTS.get(str(pid)))

    class Scripted:
        cfg = LLMConfig(provider="fake", base_url="", model="scripted")

        def chat(self, messages, tools=None):
            res = [_json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            names = [m.get("name") for m in messages if m["role"] == "tool"]

            def call(n, a):
                return ChatResponse({"role": "assistant", "content": None, "tool_calls": [{
                    "id": f"c{len(res)}", "type": "function", "function": {"name": n, "arguments": _json.dumps(a)}}]},
                    "tool_calls", {"prompt_tokens": 10, "completion_tokens": 2}, 1, 0)
            if "search_products" not in names:
                return call("search_products", {"query": "maito"})
            if "request_quote" not in names:
                return call("request_quote", {"delivery_date": "2026-11-16",
                                              "items": [{"product_ids": ["464201"], "amount": 10, "unit": "L"}]})
            if "place_order" not in names:
                return call("place_order", {"quote_id": res[-1]["quote"]["id"]})
            return ChatResponse({"role": "assistant", "content": "Ordered."}, "stop", {}, 1, 0)

    a = SimpleNamespace(cases=["H01"], repeat=1, agent="generic", max_tool_calls=8)
    cfg = SimpleNamespace(label="fake:scripted", provider="fake", model="scripted", cost_usd=lambda p, c: 0.0)
    assert H.run(a, "fake", cfg, Scripted(), tmp_path) == 0
    doc = _json.loads((tmp_path / "results.scripted.json").read_text())
    r = doc["runs"][0]
    # the scripted agent drops the buyer's "5 kg voita": the gateway reads the buyer's words (buyer_request) and
    # holds the milk-only order for a person (quantity.request_mismatch, sous/request_qty.py)
    assert r["placed_order"] and r["final_status"] == "pending_approval" and not r["false_auto_approval"], \
        r["stop_reason"]
    milk_item = r["content"]["items"][0]
    assert milk_item["kind"] and milk_item["amount"] == {"LKG": 10.0}
    assert not r["matched_no_correction"]  # no butter, and the date is not tomorrow
    assert "## Held-out" in "\n".join(H.report_section())
    signing._key.cache_clear()
