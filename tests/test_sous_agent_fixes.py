"""The Sous buyer agent after the held-out run (evals/heldout/findings.yaml): every item in one basket, headcounts
as servings, menus through the recipe planner, relative dates in Finnish and English, doubt declared to the
gateway. Offline: a scripted model, the recipe tests' fake catalog and the in-process gateway. The sentences here
are new ones written for these tests, not the held-out cases."""

import json
from datetime import date, datetime

import pytest

from agent.buyer import BuyerConfig, BuyerRun
from agent.dates import past_cutoff, relative_date
from agent.llm import ChatResponse, LLMConfig
from agent.sous_agent import PORTIONS, SousAgent, parse_need
from sous import config
from test_recipe import gw, offline  # noqa: F401  (fixtures: fake catalog + Valio, in-process MCP gateway)

SAT = date(2026, 11, 14)  # a Saturday
MON = date(2026, 11, 16)


# --- 1. every requested item --------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expect", [
    ("5 kg Edam cheese, 10 L cooking cream and 3 boxes of eggs by 2026-11-19",
     [(5, "KG"), (10, "L"), (3, "SALES_UNIT")]),
    ("20 L whole milk and 6 kg butter for Friday", [(20, "L"), (6, "KG")]),
    ("Tarvitsemme 12 litraa kermaa ja 8 kiloa voita", [(12, "L"), (8, "KG")]),
    # one item, described in several phrases
    ("40 kg of coffee beans, Paulig Brazil or Juhla Mokka.", [(40, "KG")]),
    ("Oddlygood Barista oat drink 0.5 l can't be delivered. Find a substitute, 80 litres.", [(80, "L")]),
])
def test_every_item_is_parsed(text, expect):
    n = parse_need(text, SAT, MON)
    assert [(i.amount, i.unit) for i in n.items] == expect
    assert all(i.source == "stated" for i in n.items)


# --- 2. a headcount is servings, never a pack count ----------------------------------------------------------

def test_headcount_becomes_servings_from_the_portion_table():
    n = parse_need("Milk for the coffee break for 80 people tomorrow", SAT, MON)
    (it,) = n.items
    assert n.headcount == 80 and (it.amount, it.unit, it.source) == (2.4, "L", "portion")  # 80 x 0.03 L
    assert "80 people" in n.assumptions[0] and "coffee" in n.assumptions[0]

    n = parse_need("Strawberry yoghurt and muesli for 45 children, torstaiksi", SAT, MON)
    assert [(i.amount, i.unit) for i in n.items] == [(6.8, "KG"), (1.8, "KG")]  # 45 x 150 g, 45 x 40 g
    n = parse_need("Kahvimaitoa ja kahvia 35 hengelle", SAT, MON)
    assert [(i.amount, i.unit) for i in n.items] == [(1.1, "L"), (0.5, "KG")]
    n = parse_need("Lohta pääruoaksi 70 vieraalle", SAT, MON)
    assert (n.items[0].amount, n.items[0].unit) == (10.5, "KG")  # not 70 packs


def test_explicit_amount_wins_and_no_rule_asks():
    n = parse_need("30 litres of milk for 200 people", SAT, MON)
    assert (n.items[0].amount, n.items[0].unit, n.items[0].source) == (30, "L", "stated")
    n = parse_need("Cheese for 30 people next Monday", SAT, MON)  # cheese for what? no rule
    assert n.items[0].source == "ask" and n.items[0].amount is None
    assert "ASK THE BUYER" in n.as_text()


def test_portion_table_is_small_and_explicit():
    assert 10 <= len(PORTIONS) <= 40
    assert all(unit in ("L", "KG") and 0 < per < 1 and what for _, _, per, unit, what in PORTIONS)


# --- 4. relative dates (Helsinki), cut-off ---------------------------------------------------------------------

@pytest.mark.parametrize("phrase, day", [
    ("huomiseksi", date(2026, 11, 15)), ("huomenna", date(2026, 11, 15)), ("tomorrow", date(2026, 11, 15)),
    ("ylihuomenna", MON), ("the day after tomorrow", MON),
    ("torstaiksi", date(2026, 11, 19)), ("torstaina", date(2026, 11, 19)), ("by Thursday", date(2026, 11, 19)),
    ("ensi maanantaina", MON), ("next Monday", MON), ("maanantaihin mennessä", MON),
    ("ensi viikon keskiviikkona", date(2026, 11, 18)), ("in 3 days", date(2026, 11, 17)),
    ("i morgon", date(2026, 11, 15)), ("på fredag", date(2026, 11, 20)),
])
def test_relative_dates(phrase, day):
    assert relative_date(f"10 L maitoa {phrase}", SAT) == day
    n = parse_need(f"10 L milk {phrase}", SAT, date(2026, 11, 17))
    assert n.delivery_date == day and n.date_from == "sentence"


def test_absolute_finnish_date_and_cutoff():
    assert parse_need("5 kg voita 20. marraskuuta", SAT, MON).delivery_date == date(2026, 11, 20)
    late = datetime(2026, 11, 14, 18, 30, tzinfo=config.TZ)
    early = datetime(2026, 11, 14, 9, 0, tzinfo=config.TZ)
    assert past_cutoff(date(2026, 11, 15), late) and not past_cutoff(date(2026, 11, 15), early)
    assert any("cut-off" in r for r in parse_need("10 L maitoa huomiseksi", SAT, MON, now=late).risks)
    assert not parse_need("10 L maitoa huomiseksi", SAT, MON, now=early).risks


# --- 1 + 5. the runtime holds the quote to the parsed need and declares its doubts ------------------------------

class _Gw:
    def list_tools(self):
        class T:
            def __init__(self, name):
                self.name, self.description, self.title = name, name, name
                self.input_schema = {"properties": {"items": {}, "delivery_date": {}, "quote_id": {},
                                                    "declared_risks": {}}}
        return [T(n) for n in ("search_products", "suggest_substitutes", "request_quote", "place_order")]


def _agent(text):
    a = SousAgent(llm=None, gateway=_Gw(), cfg=BuyerConfig("M", "a", SAT, default_delivery=MON))
    a.need = parse_need(text, SAT, MON)
    return a


def test_a_quote_that_drops_items_goes_back_then_all_items_are_corrected():
    a = _agent("6 kg butter, 20 L milk and coffee for 50 people on Thursday")
    assert [i.source for i in a.need.items] == ["stated", "stated", "portion"]
    run = BuyerRun("x", "m")
    args, refusal = a.vet(run, "request_quote", {"items": [{"product_ids": ["1"], "amount": 6, "unit": "KG"}]})
    assert refusal["error"] == "missing_items" and "milk" in refusal["message"]
    args, refusal = a.vet(run, "request_quote", {"items": [
        {"product_ids": ["1"], "amount": 6, "unit": "KG", "note": "#1"},
        {"product_ids": ["2"], "amount": 20, "unit": "SALES_UNIT", "note": "#2"},
        {"product_ids": ["3"], "amount": 50, "unit": "SALES_UNIT", "note": "#3"}]})
    assert refusal is None
    assert [(i["amount"], i["unit"]) for i in args["items"]] == [(6, "KG"), (20, "L"), (0.6, "KG")]
    assert args["delivery_date"] == "2026-11-19"
    assert any("Assumed 0.6 KG" in r and "50 people" in r for r in args["declared_risks"])


def test_no_rule_means_ask_and_an_open_amount_is_declared():
    a = _agent("Cheese for 30 people")
    args, refusal = a.vet(BuyerRun("x", "m"), "request_quote",
                          {"items": [{"product_ids": ["1"], "amount": 30, "unit": "SALES_UNIT"}]})
    assert refusal["error"] == "need_clarification"
    a = _agent("Some oat drink, please.")
    args, refusal = a.vet(BuyerRun("x", "m"), "request_quote",
                          {"items": [{"product_ids": ["1"], "amount": 10, "unit": "L"}]})
    assert refusal is None and "No amount given" in args["declared_risks"][0]


# --- the whole loop through the in-process gateway, with a scripted model --------------------------------------

class ScriptLLM:
    """Each step gets the conversation so far and returns (tool name, args) or the final answer text."""

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


def _last(messages):
    return json.loads(messages[-1]["content"])


def _order_latest(m):
    return "place_order", {"quote_id": _last(m)["quote"]["id"]}


def test_multi_item_request_is_one_basket_with_the_assumption_held_for_a_human(gw):
    llm = ScriptLLM([
        lambda m: ("request_quote", {"delivery_date": "2026-11-16", "items": [
            {"product_ids": ["107"], "amount": 4, "unit": "KG", "note": "#1"},
            {"product_ids": ["106"], "amount": 60, "unit": "SALES_UNIT", "note": "#2"}]}),
        _order_latest,
        lambda m: "Ordered.",
    ])
    a = SousAgent(llm, gw, BuyerConfig("M-HOTEL", "hotel-agent", SAT, default_delivery=MON))
    run = a.run("4 kg butter and milk for the coffee for 60 people on Monday")
    (args, q), = run.quotes
    assert [(i["amount"], i["unit"]) for i in args["items"]] == [(4, "KG"), (1.8, "L")]  # 60 x 0.03 L, not 60 packs
    assert q["declared_risks"] and "60 people" in q["declared_risks"][0]
    (o,) = run.orders
    assert o["status"] == "pending_approval"  # under every limit, but the agent's assumption asks a human
    assert "agent.declared_risk" in [f["rule"] for f in o["decision"]["findings"]]


def test_a_menu_goes_to_the_recipe_planner_not_to_ready_meals(gw):
    llm = ScriptLLM([
        # the model first tries a ready-made lasagne: sent back to the planner
        lambda m: ("request_quote", {"items": [{"product_ids": ["100"], "amount": 40, "unit": "SALES_UNIT"}]}),
        lambda m: ("plan_menu", {}),
        lambda m: ("place_order", {"quote_id": _last(m)["quote_id"]}),
        lambda m: "Planned and held for approval.",
    ])
    a = SousAgent(llm, gw, BuyerConfig("M-HOTEL", "hotel-agent", SAT, default_delivery=MON), menu_llm=None)
    run = a.run("Lasagne for 40 ylihuomenna, we have pasta sheets, order the rest")
    assert run.steps[0].is_error and run.steps[0].result["error"] == "use_plan_menu"
    (args, q), = run.quotes
    notes = [i["note"] for i in args["items"]]
    assert "lasagnelevy" not in notes and "naudan-sian jauheliha" in notes  # sheets on hand, the rest ordered
    mince = next(i for i in args["items"] if i["note"] == "naudan-sian jauheliha")
    assert mince["amount"] == pytest.approx(3.6)  # 90 g x 40 portions
    assert q["requested_delivery_date"] == "2026-11-16"  # ylihuomenna from Saturday
    assert any("40 portions" in r for r in q["declared_risks"])
    assert any("lasagne sheets" in r for r in q["declared_risks"])
    (o,) = run.orders
    assert o["quote_id"] == q["id"] and o["status"] == "pending_approval"
    assert run.agent_notes["parsed_need"]["menu"] and run.agent_notes["menu_plan"]["interpretation"] == "table"
