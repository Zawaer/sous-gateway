"""The purpose-built Sous agent's deterministic parts: need parsing, quote correction, the pre-order self-check."""

from datetime import date

import pytest

from agent.buyer import BuyerConfig, BuyerRun, Step
from agent.sous_agent import SOUS_TOOLS, SousAgent, parse_need

SAT = date(2026, 11, 14)  # a Saturday
MON = date(2026, 11, 16)


@pytest.mark.parametrize("text, amount, unit, day", [
    ("We need 1,000 litres of lactose-free milk delivered on Monday.", 1000, "L", MON),
    ("40 kg of coffee beans, Paulig Brazil or Juhla Mokka.", 40, "KG", MON),
    ("12 jars of Hellmann's Real Mayonnaise 750 ml.", 12, "SALES_UNIT", MON),
    ("50 litres of Valio lactose-free semi-skimmed milk in 0.5 l packs.", 50, "L", MON),
    ("Oddlygood Barista oat drink 0.5 l can't be delivered. Find a substitute, 80 litres.", 80, "L", MON),
    ("200 litres of milk by Saturday 14 November.", 200, "L", SAT),
    # not in evals/cases.yaml
    ("Need 2 500 liters of skimmed milk on Wednesday", 2500, "L", date(2026, 11, 18)),
    ("6 kilos of butter tomorrow", 6, "KG", MON),  # tomorrow is Sunday: moved to Monday (declared)
    ("3 cases of passata on 20.11.", 3, "SALES_UNIT", date(2026, 11, 20)),
    ("150 l oat drink, 2026-11-19", 150, "L", date(2026, 11, 19)),
])
def test_parse_need(text, amount, unit, day):
    n = parse_need(text, SAT, MON)
    assert (n.amount, n.unit, n.delivery_date) == (amount, unit, day)


def test_parse_ids_substitute_and_unknown_amount():
    n = parse_need("Our usual skimmed milk (464119) can't make Monday. Find the best substitute, 200 litres.", SAT, MON)
    assert n.product_ids == ["464119"] and n.substitute and n.amount == 200
    n = parse_need("Some oat drink, please.", SAT, MON)
    assert not n.sure and n.date_from == "default" and n.delivery_date == MON


class _Gw:
    def list_tools(self):
        class T:
            def __init__(self, name):
                self.name, self.description, self.title = name, name, name
                self.input_schema = {"properties": {"items": {}, "delivery_date": {}, "quote_id": {}}}
        return [T(n) for n in SOUS_TOOLS + ("get_order",)]


def _agent(text):
    a = SousAgent(llm=None, gateway=_Gw(), cfg=BuyerConfig("M", "a", SAT, default_delivery=MON))
    a.need = parse_need(text, SAT, MON)
    assert [t["function"]["name"] for t in a.tools] == [*SOUS_TOOLS, "plan_menu"]
    return a


def test_quote_is_held_to_the_parsed_need():
    a = _agent("500 litres of the skimmed milk we always buy (464119) by Saturday 14 November.")
    args, refusal = a.vet(BuyerRun("x", "m"), "request_quote", {
        "delivery_date": "2026-11-16", "items": [{"product_ids": ["469477"], "amount": 500, "unit": "SALES_UNIT"}]})
    assert refusal is None
    assert args["delivery_date"] == "2026-11-14"
    assert args["items"][0]["unit"] == "L" and args["items"][0]["product_ids"][0] == "464119"
    assert len(a.corrections) == 3


def test_self_check_orders_only_the_latest_matching_quote():
    a = _agent("200 litres of milk by Saturday 14 November.")
    run = BuyerRun("x", "m")
    q = {"id": "Q-2", "requested_delivery_date": "2026-11-16", "mandate_preview": {"outcome": "block"}}
    run.steps.append(Step("request_quote", {"items": [{"product_ids": ["1"]}]}, False, {"quote": q}, 0))
    assert a.vet(run, "place_order", {"quote_id": "Q-1"})[1]["error"] == "self_check_failed"
    assert "2026-11-14" in a.vet(run, "place_order", {"quote_id": "Q-2"})[1]["message"]
    q["requested_delivery_date"] = "2026-11-14"
    assert a.vet(run, "place_order", {"quote_id": "Q-2"})[1] is None  # block is the gateway's call, not a failed check
