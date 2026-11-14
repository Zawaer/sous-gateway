"""The Sous buyer agent after held-out set 2 (evals/heldout2/findings.yaml): stock on hand subtracted, long Finnish
lists split, headcounts scoped (special-diet subgroups), shopping lists kept away from the recipe planner (and
spices the buyer has left out of a plan), piece counts converted by pack content, and delivery days moved off
Sundays and holidays. Offline: a scripted model, the recipe tests' fake catalog and the in-process gateway. The
sentences are new ones written for these tests, not the held-out cases (set 2 has had its one blind run)."""

import json
from datetime import date

import pytest

from agent import recipe as R
from agent.buyer import BuyerConfig, BuyerRun, Step
from agent.dates import deliverable, holidays
from agent.llm import ChatResponse, LLMConfig
from agent.sous_agent import SousAgent, parse_need
from test_recipe import CATALOG, PRODUCTS, TODAY, gw, offline, product  # noqa: F401  (fixtures)

SAT = date(2026, 11, 14)  # a Saturday
MON = date(2026, 11, 16)


def items(text, today=SAT):
    return [(i.amount, i.unit) for i in parse_need(text, today, MON).items]


# --- 1. stock on hand is subtracted, never ordered -------------------------------------------------------------

def test_stock_with_a_stated_total_is_subtracted_without_doubt():
    n = parse_need("Meillä on jo 4 kg kuhaa, tarvitaan yhteensä 11 kg torstaiksi", SAT, MON)
    assert [(i.amount, i.unit) for i in n.items] == [(7, "KG")]
    assert "kuhaa" in n.items[0].text and n.items[0].on_hand == 4
    assert not n.risks and n.stock  # a total minus stock is certain: nothing to declare

    n = parse_need("Kermaa jälkiruokaan, 6 litraa varastossa, yhteensä 20 litraa", SAT, MON)
    assert [(i.amount, i.unit) for i in n.items] == [(14, "L")]


def test_stock_without_a_total_is_subtracted_and_declared():
    n = parse_need("We need 30 kg potatoes; we have 5 kg in stock", SAT, MON)
    assert [(i.amount, i.unit) for i in n.items] == [(25, "KG")]
    assert any("Subtracted 5 KG" in r and "quoting 25 KG" in r for r in n.risks)
    n = parse_need("We already have 2 kg butter, so 6 kg butter and 10 l milk please", SAT, MON)
    assert [(i.amount, i.unit) for i in n.items] == [(4, "KG"), (10, "L")]


def test_stock_of_another_product_is_never_an_item():
    n = parse_need("Meillä on vielä 8 litraa maitoa. Tilaa 20 l laktoositonta maitoa ja 3 kg voita", SAT, MON)
    assert [(i.amount, i.unit) for i in n.items] == [(20, "L"), (3, "KG")]  # lactose-free milk: not reduced
    assert "8 litraa maitoa" in n.stock[0] and "on hand" in n.as_text()


def test_in_stock_as_a_condition_is_not_stock():
    assert items("10 l oat drink if it's in stock") == [(10, "L")]


def test_the_runtime_quotes_the_amount_after_stock():
    a = SousAgent(llm=None, gateway=_NoGw(), cfg=BuyerConfig("M", "a", SAT, default_delivery=MON))
    a.need = parse_need("We need 30 kg potatoes; we have 5 kg in stock", SAT, MON)
    args, refusal = a.vet(BuyerRun("x", "m"), "request_quote",
                          {"items": [{"product_ids": ["1"], "amount": 30, "unit": "KG", "note": "#1"}]})
    assert refusal is None and args["items"][0]["amount"] == 25
    assert any("Subtracted 5 KG" in r for r in args["declared_risks"])


# --- 2. long lists split into every item ------------------------------------------------------------------------

@pytest.mark.parametrize("text, expect", [
    ("Tilaa graavilohta 3 kg, perunoita 10 kg, smetanaa 2 kg ja punasipulia 1 kg perjantaiksi",
     [(3, "KG"), (10, "KG"), (2, "KG"), (1, "KG")]),
    ("Tarvitsemme maitoa 20 l, kermaa 5 l, voita 3 kg ja juustoa 2 kg",
     [(20, "L"), (5, "L"), (3, "KG"), (2, "KG")]),
    ("ruisleipää 6 kg, tilliä 1 kg, kalalientä 10 l sekä sitruunoita 30 kpl",
     [(6, "KG"), (1, "KG"), (10, "L"), (30, "ST")]),
    ("Beställ 4 kg kycklingfilé, 2 kg ris och 5 l grädde", [(4, "KG"), (2, "KG"), (5, "L")]),
])
def test_long_lists_split(text, expect):
    assert items(text) == expect


# --- 3. headcounts: only items without an amount, only with a portion rule, subgroups scaled to themselves ------

def test_headcount_only_for_items_without_an_amount():
    n = parse_need("For 60 guests: 8 kg salmon, potatoes and bananas", SAT, MON)
    assert [(i.amount, i.unit, i.source) for i in n.items] == [
        (8, "KG", "stated"), (12, "KG", "portion"), (9, "KG", "portion")]  # 60 x 200 g, 60 x 150 g fruit


def test_a_special_diet_subgroup_scales_to_the_subgroup():
    n = parse_need("Lunch for 30 pupils: rice. One pupil is allergic to milk, so a dairy-free yoghurt for him.", SAT, MON)
    assert n.headcount == 30
    assert [(i.amount, i.unit, i.headcount) for i in n.items] == [(2.1, "KG", 30), (0.2, "KG", 1)]
    assert n.items[1].subgroup and "special-diet group only" in n.assumptions[1]
    assert not any("milk" in i.foods for i in n.items)  # "allergic to milk" is not milk to buy

    n = parse_need("Kalaa 90 asukkaalle, ei saa olla kalaa allergisille 3 asukkaalle, heille kalkkunaa", SAT, MON)
    assert n.headcount == 90
    assert [(i.amount, i.headcount, i.foods) for i in n.items] == [(13.5, 90, ["fish"]), (0.5, 3, ["chicken"])]


def test_one_diet_headcount_alone_is_the_whole_group():
    n = parse_need("Gluten-free pasta for 60 children and milk", SAT, MON)
    assert n.headcount == 60 and [(i.amount, i.unit) for i in n.items] == [(6, "KG"), (12, "L")]


# --- 4. a shopping list is quoted as it is; the planner leaves out what the buyer has --------------------------

def test_a_party_shopping_list_is_not_a_menu():
    n = parse_need("Joulujuhlat 80 hengelle: kinkkua 9 kg, rosolli valmiina 4 kg, joulutortut 150 kpl, "
                   "glögiä 12 litraa", SAT, MON)
    assert not n.menu
    assert [(i.amount, i.unit) for i in n.items] == [(9, "KG"), (4, "KG"), (150, "ST"), (12, "L")]
    assert parse_need("Lasagne for 40 ylihuomenna, we have pasta sheets, order the rest", SAT, MON).menu
    assert parse_need("Kinkkua 9 kg ja rosollia 4 kg 80 hengelle, valmistamme itse, tilaa raaka-aineet",
                      SAT, MON).menu


def test_planner_leaves_out_the_spices_the_buyer_has(gw):
    p = R.plan("Lasagne for 40 on Thursday. Mausteet löytyy.", gateway=gw, mandate_id="M-HOTEL",
               agent_id="hotel-agent", today=TODAY, declare_assumptions=True)
    assert not p.error
    notes = [i["note"] for i in p.trace.steps[-1]["args"]["items"]] if p.trace.steps else []
    spices = {n.key: n for n in p.needs if R.is_spice(n)}
    assert {"oregano", "suola", "mustapippuri"} <= set(spices)
    assert all(n.status == "covered_by_stock" for n in spices.values())
    assert not {"oregano", "suola", "mustapippuri"} & set(notes)
    assert {l["product_id"] for l in p.quote["lines"]}.isdisjoint({"110", "111", "112", "113"})
    assert any("assumed fully on hand" in r and "salt" in r for r in p.declared_risks)


def test_a_model_plan_saying_spices_on_hand_covers_every_seasoning():
    spec = R.MenuSpec(portions=10, dishes=[R.Dish(name="Stew", ingredients=[
        R.Ingredient(name_fi="naudanliha", name_en="beef", amount_per_portion=150, unit="g"),
        R.Ingredient(name_fi="paprikajauhe", name_en="paprika powder", amount_per_portion=1, unit="g"),
        R.Ingredient(name_fi="suola", name_en="salt", amount_per_portion=2, unit="g")])],
        stock_on_hand=[R.Stock(ingredient="mausteet", plenty=True)])
    needs = {n.key: n for n in R.compute_needs(spec)}
    assert needs["suola"].to_buy == 0 and needs["paprikajauhe"].to_buy == 0 and needs["naudanliha"].to_buy == 1.5


# --- 5. pieces are converted by pack content, never 1:1 into packs ---------------------------------------------

@pytest.mark.parametrize("text, unit", [
    ("120 brioche buns", "ST"), ("60 eggs", "ST"), ("kananmunia 90 kpl", "ST"), ("40 pieces of salmon", "ST"),
    ("3 boxes of eggs", "SALES_UNIT"), ("12 jars of mayonnaise", "SALES_UNIT"), ("10 maitoa", "SALES_UNIT"),
])
def test_count_units(text, unit):
    assert parse_need(text, SAT, MON).items[0].unit == unit


class ScriptLLM:
    def __init__(self, steps):
        self.steps = list(steps)
        self.cfg = LLMConfig(provider="fake", base_url="", model="scripted")

    def chat(self, messages, tools=None, response_format=None):
        step = self.steps.pop(0)(messages)
        msg = {"role": "assistant", "content": step} if isinstance(step, str) else \
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{len(self.steps)}", "type": "function",
                 "function": {"name": step[0], "arguments": json.dumps(step[1])}}]}
        return ChatResponse(msg, "stop", {"prompt_tokens": 10, "completion_tokens": 5}, 1, 0)


@pytest.fixture
def buns(monkeypatch):
    carton = product("130", "Brioche bun 50 pcs", 2.5, "KG", 30.0, sales_unit="KI")
    carton.update(base_unit="ST", units={"KI": 50.0, "ST": 1.0})
    loose = product("131", "Brioche bun tray", 2.5, "KG", 30.0, sales_unit="KI")
    for p in (carton, loose):
        monkeypatch.setitem(PRODUCTS, p["id"], p)
    monkeypatch.setattr("test_recipe.CATALOG", CATALOG + [(carton, ("brioche",)), (loose, ("brioche",))])


def _quote(pid):
    return lambda m: ("request_quote", {"delivery_date": "2026-11-16", "items": [
        {"product_ids": [pid], "amount": 120, "unit": "SALES_UNIT", "note": "#1", "product_type": "bun"}]})


def test_120_buns_in_cartons_of_50_are_3_cartons(gw, buns):
    llm = ScriptLLM([_quote("130"), lambda m: "Quoted."])
    cfg = BuyerConfig("M-HOTEL", "hotel-agent", TODAY, default_delivery=MON, tools=("search_products",
                                                                                     "request_quote"))
    run = SousAgent(llm, gw, cfg).run("120 brioche buns for Monday")
    (args, q), = run.quotes
    assert (args["items"][0]["amount"], args["items"][0]["unit"]) == (120, "ST")
    (line,) = q["lines"]
    assert line["quantity"] == 3 and line["sales_unit"] == "KI"  # not 120 cartons (6,000 buns)


def test_pieces_the_gateway_cannot_convert_are_declared_before_ordering(gw, buns):
    a = SousAgent(llm=None, gateway=gw, cfg=BuyerConfig("M-HOTEL", "hotel-agent", TODAY, default_delivery=MON))
    a.need = parse_need("120 brioche buns for Monday", TODAY, MON)
    run = BuyerRun("x", "m")
    args, _ = a.vet(run, "request_quote", {"items": [{"product_ids": ["131"], "amount": 120, "note": "#1"}]})
    q = {"id": "Q-1", "requested_delivery_date": "2026-11-16", "mandate_preview": {"outcome": "silent"},
         "lines": [{"item_index": 0, "quantity": 120, "sales_unit": "KI",
                    "unit_note": "ST is not a unit of this product; read as 120 KI"}]}
    run.steps.append(Step("request_quote", args, False, {"quote": q}, 0))
    refusal = a.vet(run, "place_order", {"quote_id": "Q-1"})[1]
    assert refusal["error"] == "self_check_failed" and "pieces" in refusal["message"]
    args, _ = a.vet(run, "request_quote", {"items": [{"product_ids": ["131"], "amount": 120, "note": "#1"}]})
    assert any("pack content in pieces is unknown" in r for r in args["declared_risks"])


# --- 6. a day Valio doesn't deliver moves to the next delivery day, declared --------------------------------------

def test_delivery_calendar():
    assert deliverable(date(2026, 11, 15)) == (MON, "a Sunday")
    assert deliverable(SAT) == (SAT, None)
    assert deliverable(date(2026, 12, 24)) == (date(2026, 12, 28), "a public holiday (Christmas Eve)")
    h = holidays(2027)
    assert h[date(2027, 3, 26)] == "Good Friday" and h[date(2027, 3, 29)] == "Easter Monday"
    assert h[date(2027, 6, 25)] == "Midsummer Eve" and h[date(2027, 5, 6)] == "Ascension Day"


def test_relative_day_on_a_sunday_moves_and_is_declared():
    n = parse_need("8 kg butter huomiseksi", SAT, date(2026, 11, 17))
    assert n.delivery_date == MON and n.date_from == "sentence"
    assert any("Sunday 2026-11-15" in r and "Monday 2026-11-16" in r for r in n.risks)
    n = parse_need("20 l milk on 6.12.", SAT, MON)
    assert n.delivery_date == date(2026, 12, 7) and "Independence Day" in n.risks[0]
    assert not parse_need("20 l milk on Friday", SAT, MON).risks


def test_the_quote_uses_the_moved_day():
    a = SousAgent(llm=None, gateway=_NoGw(), cfg=BuyerConfig("M", "a", SAT, default_delivery=MON))
    a.need = parse_need("6 l cream tomorrow", SAT, MON)
    args, refusal = a.vet(BuyerRun("x", "m"), "request_quote", {
        "delivery_date": "2026-11-15", "items": [{"product_ids": ["1"], "amount": 6, "unit": "L"}]})
    assert refusal is None and args["delivery_date"] == "2026-11-16"
    assert any("a Sunday" in r for r in args["declared_risks"])


def test_planner_moves_a_sunday_too(gw):
    p = R.plan("Lasagne for 40 sunnuntaiksi", gateway=gw, mandate_id="M-HOTEL", agent_id="hotel-agent",
               today=TODAY)
    assert p.delivery_date == MON and any("a Sunday" in r for r in p.declared_risks)


class _NoGw:
    def list_tools(self):
        class T:
            def __init__(self, name):
                self.name, self.description, self.title = name, name, name
                self.input_schema = {"properties": {"items": {}, "delivery_date": {}, "quote_id": {},
                                                    "declared_risks": {}}}
        return [T(n) for n in ("search_products", "suggest_substitutes", "request_quote", "place_order")]
