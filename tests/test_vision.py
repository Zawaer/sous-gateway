"""Photo -> order (agent/vision.py), offline: a scripted vision model and a fake gateway; no network, no keys."""

import base64
import io
import json
from datetime import date

import httpx
import pytest
from PIL import Image

from agent import recipe as R, vision as V
from agent.llm import ChatClient, ChatResponse, LLMConfig, config_from_env

TODAY = date(2026, 11, 13)  # a Friday


def png(w=400, h=300) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), (250, 250, 240)).save(b, "PNG")
    return b.getvalue()


class FakeLLM:
    def __init__(self, answers):
        self.answers = list(answers)
        self.cfg = LLMConfig(provider="fake", base_url="", model="scripted-vl")
        self.seen = []

    def chat(self, messages, tools=None):
        self.seen.append(messages)
        a = self.answers.pop(0)
        return ChatResponse({"role": "assistant", "content": a if isinstance(a, str) else json.dumps(a)}, "stop",
                            {"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020}, 5, 0)


def item(text, name, q, unit, conf=0.95, **kw):
    return {"text_as_written": text, "name_fi": name, "name_en": None, "quantity": q, "unit": unit,
            "confidence": conf, **kw}


ORDER = {"kind": "order_list", "delivery_date": "2026-11-16", "notes": None,
         "unreadable": ["line 7: smudged, maybe 'juusto'"],
         "items": [item("maito laktoositon 40 l", "maito laktoositon", 40, "l"),
                   item("voi 5 kg", "voi", 5, "kg"),
                   item("kananmunat 360 kpl", "kananmuna", 360, "kpl"),
                   item("jauheliha 1? kg", "jauheliha", 12, "kg", 0.4, note="second digit unclear")],
         "dishes": []}


# --- reading: validation, repair, cross-check, second look ----------------------------------------

def test_reads_image_into_validated_json_with_the_image_attached():
    llm = FakeLLM([{**ORDER, "items": [item("ruokakerma 2,5 l", "ruokakerma", "2,5", "litraa", "90")]}])
    vr = V.read_order_image(png(), "order_list", llm, today=TODAY)
    it = vr.read.items[0]
    assert (it.quantity, it.unit, it.confidence) == (2.5, "l", 0.9)  # decimal comma, unit alias, percentage
    assert vr.read.delivery_date == date(2026, 11, 16) and vr.calls == 1 and not vr.repaired
    sys_msg, user = llm.seen[0]
    assert "ltk" in sys_msg["content"] and "Today is Friday 2026-11-13" in sys_msg["content"]
    part = next(p for p in user["content"] if p["type"] == "image_url")
    assert part["image_url"]["url"].startswith("data:image/png;base64,")
    assert "order list" in user["content"][0]["text"]


def test_invalid_json_gets_one_repair_retry_then_fails_cleanly():
    llm = FakeLLM(["Here is the order: maito 40 l", {**ORDER, "kind": "tilauslista"}])
    vr = V.read_order_image(png(), None, llm, today=TODAY, verify=False)
    assert vr.repaired and vr.calls == 2 and vr.read.kind == "order_list" and vr.errors
    assert "not valid" in llm.seen[1][-1]["content"]

    llm = FakeLLM(["nope", '{"kind": "poster"}'])
    with pytest.raises(V.VisionError) as e:
        V.read_order_image(png(), None, llm, today=TODAY)
    assert e.value.result.calls == 2


def test_code_cross_check_triggers_one_targeted_second_look():
    # the model's number disagrees with its own transcription ("voi 5 kg" read as 4): code catches it
    first = {**ORDER, "unreadable": [], "items": [item("voi 5 kg", "voi", 4, "kg", 0.9),
                                                  item("kerma 10 l", "kerma", 10, "l")]}
    llm = FakeLLM([first, {"lines": [{"index": 0, "text_as_written": "voi 5 kg", "quantity": 5, "unit": "kg",
                                      "confidence": 0.92}]}])
    vr = V.read_order_image(png(), None, llm, today=TODAY)
    assert vr.calls == 2 and vr.read.items[0].quantity == 5 and not vr.flags
    assert vr.rechecked[0]["before"]["quantity"] == 4 and "not a number written" in vr.rechecked[0]["why"]
    assert "index 0" in llm.seen[1][-1]["content"][0]["text"] and "kerma" not in llm.seen[1][-1]["content"][0]["text"]

    # second look doesn't resolve it: the line is capped below the threshold and goes to a human
    llm = FakeLLM([first, {"lines": [{"index": 0, "text_as_written": "voi 5 kg", "quantity": 4, "unit": "kg",
                                      "confidence": 0.99}]}])
    vr = V.read_order_image(png(), None, llm, today=TODAY)
    assert vr.read.items[0].confidence <= 0.5 and vr.flags[0]["index"] == 0

    # nothing suspicious: no second call
    llm = FakeLLM([{**first, "items": [item("kerma 10 l", "kerma", 10, "l")]}])
    assert V.read_order_image(png(), None, llm, today=TODAY).calls == 1


def test_unit_cross_check_and_think_tags():
    it = V.ReadItem(**item("kevytmaito 1 l 6 ltk", "kevytmaito", 6, "ltk"))
    assert V.cross_check(it, "order_list") is None  # the pack size in the name is not the order unit
    it = V.ReadItem(**item("kahvi 8 pkt", "kahvi", 8, "kg"))
    assert "line says pkt" in V.cross_check(it, "order_list")
    assert V.extract_json('<think>hmm {"a": 1}</think>\n```json\n{"kind": "menu"}\n```') == {"kind": "menu"}


def test_big_photo_is_downscaled_to_jpeg():
    data, mime = V.prepare_image(png(3000, 2000))
    assert mime == "image/jpeg" and max(Image.open(io.BytesIO(data)).size) == V.MAX_SIDE
    with pytest.raises(ValueError):
        V.prepare_image(b"GIF89a....")


# --- from the reading to one basket quote -----------------------------------------------------------

CARDS = {
    "milk": {"id": "M1", "name": "Valio milk 1 l lactose free", "size_from_name": [1.0, "L"], "sales_unit": "ST"},
    "butter": {"id": "B1", "name": "Valio butter 500 g", "size_from_name": [0.5, "KG"], "sales_unit": "ST"},
    "egg": {"id": "E1", "name": "Kieku Barn egg L180 12,3 kg", "size_from_name": [12.3, "KG"], "sales_unit": "KI"},
    "cream": {"id": "C1", "name": "Valio whipping cream 1 l", "size_from_name": [1.0, "L"], "sales_unit": "ST"},
    "potato": {"id": "P1", "name": "POTATO WASHED 5KG", "size_from_name": [5.0, "KG"], "sales_unit": "ST"},
    "sub": {"id": "P2", "name": "POTATO PEELED 5KG", "size_from_name": [5.0, "KG"], "sales_unit": "ST"},
}
SEARCH = {"maito": "milk", "voi": "butter", "kananmuna": "egg", "kuohukerma": "cream", "peruna": "potato"}


class FakeGateway:
    def __init__(self, short=()):
        self.calls = []
        self.short = set(short)

    def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == "get_mandate":
            return False, {"mandate": {"id": args["mandate_id"]}}
        if tool == "search_products":
            hit = next((k for w, k in SEARCH.items() if w in args["query"]), None)
            return False, {"products": [dict(CARDS[hit])] if hit else []}
        if tool == "suggest_substitutes":
            return False, {"options": [{"product_id": "P2", "name": CARDS["sub"]["name"], "tier": "good",
                                        "compromises": ["peeled"], "offer": {"orderable": True}}]}
        if tool == "request_quote":
            lines, unc = [], []
            for i, it in enumerate(args["items"]):
                pid = it["product_ids"][0]
                if pid in self.short:
                    unc.append({"item_index": i, "uncovered": it["amount"], "unit": it["unit"]})
                    continue
                lines.append({"item_index": i, "product_id": pid, "name": pid, "quantity": 1, "sales_unit": "ST",
                              "covers": {"amount": it["amount"]}, "line_total_ex_vat": 10.0, "orderable": True,
                              "substitute_for": it.get("substitute_for"), "issues": []})
            q = {"id": f"Q-{len(self.calls)}", "expires_at": "2026-11-13T23:00:00+02:00", "lines": lines,
                 "uncovered": unc, "total_ex_vat": 10.0 * len(lines), "total_inc_vat": 11.35 * len(lines),
                 "mandate_preview": {"outcome": "silent", "findings": []}}
            return False, {"quote": q}
        if tool == "place_order":
            return False, {"id": "PO-1", "status": "ordered", "decision": {"outcome": "silent", "findings": []},
                           "total_ex_vat": 30.0}
        raise AssertionError(tool)

    def quotes(self):
        return [a for t, a in self.calls if t == "request_quote"]


def read(d) -> V.VisionRead:
    return V.VisionRead(read=V.ImageRead.model_validate(d), prompt_version="v2", model="fake")


def test_order_list_quotes_confident_lines_and_asks_about_the_rest():
    gw = FakeGateway()
    po = V.order_from_read(read(ORDER), gateway=gw, mandate_id="M-HOTEL-AURORA", agent_id="aurora-purchasing-agent",
                           today=TODAY, place=True)
    [q] = gw.quotes()
    assert q["delivery_date"] == "2026-11-16"
    items = {i["product_ids"][0]: i for i in q["items"]}
    assert items["M1"]["amount"] == 40 and items["M1"]["unit"] == "L"
    assert items["B1"]["amount"] == 5 and items["B1"]["unit"] == "KG"
    assert items["E1"]["unit"] == "KG" and items["E1"]["amount"] == 22.0  # 360 eggs x 60 g = 21.6 kg, rounded up
    search = [a for t, a in gw.calls if t == "search_products"]
    assert search[0]["claims"] == ["FREE_FROM_LACTOSE"]  # "laktoositon" became a catalog filter
    assert len(q["items"]) == 3  # the 0.4-confidence mince line is NOT ordered
    assert any("jauheliha" in c and "0.40" in c for c in po.to_confirm)
    assert any(c.startswith("unreadable:") for c in po.to_confirm)
    assert po.order["status"] == "ordered" and "please confirm" in po.summary()
    d = po.to_dict()
    assert d["quote"]["id"] and {l["status"] for l in d["lines"]} == {"quoted", "needs_confirmation"}
    assert "Needs a human" in V.render(po, "photo.jpg")


def test_delivery_note_orders_only_the_shortfall_and_substitutes_what_is_out():
    note = {"kind": "delivery_note", "delivery_date": "2026-11-13", "dishes": [], "unreadable": [], "items": [
        item("Maito laktoositon 1 l 40 24 l", "maito laktoositon", 40, "l", delivered_quantity=24),
        item("Voi 500 g 5 5 kg", "voi", 5, "kg", delivered_quantity=5),
        item("Peruna kiinteä 25 0 kg", "peruna", 25, "kg", delivered_quantity=0)]}
    gw = FakeGateway(short={"P1"})
    po = V.order_from_read(read(note), gateway=gw, mandate_id="M", agent_id="a", today=TODAY)
    first, revised = gw.quotes()
    assert [(i["product_ids"][0], i["amount"]) for i in first["items"]] == [("M1", 16), ("P1", 25)]
    assert first["delivery_date"] == "2026-11-16"  # the note's date is past: next delivery day
    assert [(i["product_ids"], i.get("substitute_for")) for i in revised["items"]] == [(["M1"], None), (["P2"], "P1")]
    st = {l.item.name_fi: l.status for l in po.lines}
    assert st == {"maito laktoositon": "quoted", "voi": "no_shortfall", "peruna": "quoted_with_substitute"}
    assert po.quote["id"] == f"Q-{len(gw.calls)}"


def test_menu_goes_to_the_recipe_planner(monkeypatch):
    seen = {}

    def fake_plan(text, **kw):
        seen.update(text=text, **kw)
        return R.Plan(request=text, interpretation="table", mandate_id=kw["mandate_id"], agent_id=kw["agent_id"])
    monkeypatch.setattr(R, "plan", fake_plan)
    menu = {"kind": "menu", "delivery_date": "2026-11-16", "items": [], "unreadable": [], "dishes": [
        {"name": "Lohikeitto", "portions": 80, "day": "ma"}, {"name": "Lasagne", "portions": 120},
        {"name": "Rosolli", "portions": None}]}
    po = V.order_from_read(read(menu), gateway=FakeGateway(), mandate_id="M", agent_id="a", today=TODAY,
                           recipe_llm=object())
    assert seen["text"] == "Menu: Lohikeitto for 80 portions; Lasagne for 120 portions, delivery 2026-11-16"
    assert seen["delivery_date"] == date(2026, 11, 16) and po.plan is not None
    assert any("Rosolli" in c for c in po.to_confirm)


def test_nothing_to_order():
    note = {"kind": "delivery_note", "items": [item("Voi 5 5 kg", "voi", 5, "kg", delivered_quantity=5)]}
    po = V.order_from_read(read(note), gateway=FakeGateway(), mandate_id="M", agent_id="a", today=TODAY)
    assert po.error == "everything on the delivery note was delivered" and po.quote is None


# --- the nvidia preset ----------------------------------------------------------------------------

def test_nvidia_preset_sends_images_and_turns_reasoning_off():
    cfg = config_from_env("nvidia", env={"NVIDIA_API_KEY": "nvapi-test"})
    assert cfg.base_url == "https://integrate.api.nvidia.com/v1"
    assert cfg.model == "nvidia/nemotron-3.5-super-vl-120b-a12b" and "nvapi" not in repr(cfg)
    assert config_from_env("nvidia", env={"NVIDIA_API_KEY": "k", "LLM_THINKING": "1"}).extra_body == {
        "chat_template_kwargs": {"enable_thinking": True}}
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content), auth=request.headers["authorization"])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(ORDER)},
                                                      "finish_reason": "stop"}], "usage": {}})
    client = ChatClient(cfg, http=httpx.Client(transport=httpx.MockTransport(handler)))
    vr = V.read_order_image(png(), None, client, today=TODAY, verify=False)
    assert vr.read.kind == "order_list" and vr.model.startswith("nvidia:")
    assert sent["chat_template_kwargs"] == {"enable_thinking": False} and "reasoning_effort" not in sent
    img = sent["messages"][1]["content"][1]["image_url"]["url"]
    assert base64.b64decode(img.split(",", 1)[1])[:8] == b"\x89PNG\r\n\x1a\n"


# --- the eval scorer ---------------------------------------------------------------------------------

def test_eval_scorer_counts_silent_errors():
    from scripts import vision_eval as E
    truth = {"file": "x", "kind": "order_list", "delivery_date": None, "items": [
        {"name_fi": "voi", "terms": ["voi"], "quantity": 5, "unit": "kg"},
        {"name_fi": "kerma", "terms": ["kerma"], "quantity": 10, "unit": "l"}],
        "must_skip": [["jogurt"]], "must_flag": [["juust"]]}
    good = {"kind": "order_list", "delivery_date": None, "unreadable": ["smudge"],
            "items": [item("voi 5 kg", "voi", 5, "kg"), item("kerma 10 l", "kerma", 10, "l")]}
    s = E.score_case(truth, good, [])
    assert (s["items_found"], s["qty_unit_ok"], s["auto_correct"], s["silent_errors"], s["traps_ok"]) == (2, 2, 2, 0, 2)
    bad = {"kind": "order_list", "delivery_date": None, "unreadable": [],
           "items": [item("voi 3 kg", "voi", 3, "kg"), item("kerma 10 l", "kerma", 10, "l", 0.5),
                     item("jogurtti 4 kg", "jogurtti", 4, "kg"), item("juusto 5 kg", "juusto", 5, "kg")]}
    s = E.score_case(truth, bad, [])
    assert s["silent_errors"] == 3 and s["auto_correct"] == 0 and s["traps_ok"] == 0
    assert s["items_real"] == 2 and s["items_read"] == 4


def test_unit_in_its_own_column_is_not_a_mismatch():
    it = V.ReadItem(**item("Voi 500 g | 5 | 5 | kg", "voi", 5, "kg", delivered_quantity=5))
    assert V.cross_check(it, "delivery_note") is None


def test_doubtful_delivery_lines_go_to_a_human_not_to_delivered_in_full():
    # QA repro: an uncertain "5 / 5" must not be declared fulfilled; a missing delivered column must not be
    # treated as an all-missing shortfall and ordered; a line that fails the cross-check is not ordered
    note = {"kind": "delivery_note", "items": [
        item("Voi 5 5 kg", "voi", 5, "kg", 0.40, delivered_quantity=5),
        item("Kerma 10 4 kg", "kerma", 10, "l", 0.95, delivered_quantity=4),
        item("Maito laktoositon 40 l", "maito laktoositon", 40, "l", 0.95)]}
    gw = FakeGateway()
    po = V.order_from_read(read(note), gateway=gw, mandate_id="M", agent_id="a", today=TODAY, place=True)
    assert [l.status for l in po.lines] == ["needs_confirmation"] * 3
    assert [l.reason.split(" ")[0] for l in po.lines] == ["read", "check:", "delivered"]
    assert len(po.to_confirm) == 3 and not gw.quotes() and po.order is None
    assert po.error == "no line can be ordered automatically"


def test_a_loose_shortfall_note_on_a_delivery_note_is_not_lost():
    # eval run 2, page 08: the red-pen "puuttuu 3 l!" landed in notes, every row looked delivered in full
    first = {"kind": "delivery_note", "delivery_date": "2026-11-16", "notes": "Handwritten red note: 'puuttuu 3 l!'",
             "items": [item("Jauheliha naudan 2 kg | 12 | 8 | kg", "jauheliha", 12, "kg", delivered_quantity=8),
                       item("Tomaattimurska | 6 | 6 | kg", "tomaattimurska", 6, "kg", delivered_quantity=6),
                       item("Ruokakerma 1 l | 5 | 5 | l", "ruokakerma", 5, "l", delivered_quantity=5)]}
    # the second look gets the full-looking rows and applies the correction to the right one
    fix = {"index": 2, "text_as_written": "Ruokakerma 1 l | 5 | 5 | l | puuttuu 3 l!", "quantity": 5,
           "unit": "l", "delivered_quantity": 2, "confidence": 0.9}
    llm = FakeLLM([first, {"lines": [fix]}])
    vr = V.read_order_image(png(), None, llm, today=TODAY)
    asked = llm.seen[1][-1]["content"][0]["text"]
    assert "index 1" in asked and "index 2" in asked and "index 0" not in asked and "Apply it to\nTHAT row" in asked
    assert vr.read.items[2].delivered_quantity == 2 and not vr.flags
    # a changed row that doesn't say where it saw the note is a guess: the rows stay with a human
    llm = FakeLLM([first, {"lines": [{**fix, "text_as_written": "Ruokakerma 1 l | 5 | 5 | l"}]}])
    vr = V.read_order_image(png(), None, llm, today=TODAY)
    assert {f["index"] for f in vr.flags} == {1, 2}
    # no second look (or it changes nothing): the full-looking rows go to a human instead of "delivered in full"
    llm = FakeLLM([first])
    vr = V.read_order_image(png(), None, llm, today=TODAY, verify=False)
    assert {f["index"] for f in vr.flags} == {1, 2}
    po = V.order_from_read(vr, gateway=FakeGateway(), mandate_id="M", agent_id="a", today=TODAY)
    assert [l.status for l in po.lines] == ["not_found", "needs_confirmation", "needs_confirmation"]  # no mince in the fake catalog


def test_shortfall_page_check_edge_cases_from_eval_run_3():
    rows = [item("Voi 500 g | 5 | 5 | kg", "voi", 5, "kg", delivered_quantity=5),
            item("Kerma 1 l | 10 | 10 | l", "kerma", 10, "l", delivered_quantity=10)]
    # a printed footer "Puuttuvat tuotteet toimitetaan erikseen" (no amount) is not a shortfall
    rd = V.ImageRead(kind="delivery_note", items=rows, notes="LÄHETE 13.11.2026. Puuttuvat tuotteet toimitetaan erikseen.")
    assert V.page_check(rd, []) == {}
    # the red-pen note listed as a line of its own (no quantity) is still loose: the full-looking rows are suspect
    rd = V.ImageRead(kind="delivery_note", items=[*rows, {"text_as_written": "puuttuu 3 l!", "confidence": 0.5}])
    page = V.page_check(rd, [])
    assert set(page) == {0, 1} and "puuttuu 3 l" in page[0]
    # applied to a row: nothing to flag
    rd = V.ImageRead(kind="delivery_note", items=[rows[0], item("Kerma 1 l | 10 | 10 | l | puuttuu 3 l", "kerma", 10,
                                                                "l", delivered_quantity=7)], notes="puuttuu 3 l")
    assert V.page_check(rd, []) == {}


def test_headings_and_signatures_are_not_items_but_possible_products_stay():
    first = {**ORDER, "unreadable": [], "items": [
        {"text_as_written": "Tilaus - toimitus ma 16.11.", "quantity": None, "unit": None, "confidence": 0.9},
        item("voi 5 kg", "voi", 5, "kg"),
        {"text_as_written": "jogurtti 4 kg", "quantity": None, "unit": None, "confidence": 0.0},
        {"text_as_written": "- Ville", "quantity": None, "unit": None, "confidence": 0.9}]}
    llm = FakeLLM([first, {"lines": []}])
    vr = V.read_order_image(png(), None, llm, today=TODAY)
    assert vr.ignored == ["Tilaus - toimitus ma 16.11.", "- Ville"]
    assert [i.text_as_written for i in vr.read.items] == ["voi 5 kg", "jogurtti 4 kg"]
    assert "v3" in V.PROMPTS and V.DEFAULT_PROMPT == "v3" and "Tilaus - toimitus" in V.PROMPTS["v3"](TODAY)
