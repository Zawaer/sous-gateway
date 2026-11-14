"""Rules added after comparing with other gateways: weekly limit, split orders, first-time products, Valio's pause.

Offline: the catalog and Valio's price/availability (quote.offers) are replaced; the clock is S._now.
"""

from datetime import date, datetime, timedelta

import pytest

from sous import catalog, config, pause, quote as Q, service as S, signing, store
from sous.mandate import Mandate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)  # a Friday
MON = date(2026, 11, 26)  # deliverable in every test clock below


def product(pid):
    return {
        "id": pid, "name": f"Milk {pid} 1 l", "names": {"fi": "", "en": "", "sv": ""}, "brand": "", "vendor": "",
        "category": "10400", "country_of_origin": "fi", "sales_unit": "ST", "base_unit": "ST", "units": {"ST": 1.0},
        "allowed_lot_size": 1.0, "temperature": "chilled", "labels": [], "claims": [],
        "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": []},
        "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": None,
        "official_substitutes": [], "deleted": False,
    }


PRODUCTS = {pid: product(pid) for pid in ("MILK", "OATS")}
clock = {"now": NOW}
PRICE = {"eur": 1.0}  # Valio's price per 1 l pack in fake_offers (PRICE[pid] overrides it per product)


def fake_offers(product_ids, requested, need=None, need_unit=None):
    e = PRICE.get(product_ids[0], PRICE["eur"])
    pr = {"catalogPrice": {"value": e, "valueWithTax": round(e * 1.135, 2), "validTo": None}, "comparisonPrice": {"value": e},
          "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}
    av = {"logisticModel": "Stock", "closingTime": "2026-11-25T17:00:00+0200", "salesBlock": False,
          "scheduleLines": [{"salesBlock": False, "availableAmount": 99999, "availableDate": "2026-11-26",
                             "salesUnit": "ST"}]}
    return [Q.build_offer(PRODUCTS[p], pr, av, requested, need, need_unit, clock["now"]) for p in product_ids], clock["now"]


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(config, "REQUIRE_AGENT_AUTH", False)
    monkeypatch.setattr(config, "APPROVER_CODE", "")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: PRODUCTS.get(pid))
    monkeypatch.setattr(Q, "offers", fake_offers)
    clock["now"] = NOW
    PRICE.clear()
    PRICE["eur"] = 1.0
    monkeypatch.setattr(S, "_now", lambda: clock["now"])
    monkeypatch.setattr(S.notify, "send", lambda *a, **k: None)
    yield
    signing._key.cache_clear()


def issue(mid="M", buyer="Hotel", **kw):
    S.issue_mandate(Mandate(**{"id": mid, "buyer": buyer, "agent_id": "a", "issued_by": "chef",
                               "auto_approve_limit_eur": 5000, "hard_limit_eur": None,
                               "min_delivery_reliability": None, "unusual_quantity_factor": None,
                               "max_price_change_pct": None, **kw}))


def order(eur, pid="MILK", mid="M"):
    """Quote and order `eur` litres of a €1/L product: the order total is `eur` euros."""
    q = S.request_quote(S.QuoteRequest(agent_id="a", mandate_id=mid, delivery_date=MON, items=[
        S.QuoteItem(product_ids=[pid], amount=eur, unit="L")]))["quote"]
    return S.place_order("a", mid, q["id"])


def fired(o):
    return [f["rule"] for f in o["decision"]["findings"] if f["outcome"] != "silent"]


def test_new_rules_are_off_by_default():
    issue(auto_approve_limit_eur=50)
    for _ in range(4):
        o = order(15)
        assert o["status"] == "ordered" and fired(o) == [], o["decision"]


def test_split_orders_are_checked_together_within_the_window():
    issue(auto_approve_limit_eur=50, split_window_minutes=60)
    for i in range(3):  # €45 together: under the €50 the agent may spend alone
        clock["now"] = NOW + timedelta(minutes=10 * i)
        assert order(15)["status"] == "ordered"
    clock["now"] = NOW + timedelta(minutes=30)
    o = order(15)
    assert o["status"] == "pending_approval" and fired(o) == ["spend.split_orders"]
    msg = o["decision"]["findings"][0]["message"]
    assert "3 more in the last 60 min" in msg and "€60.00" in msg
    # an hour after the first three, they no longer count
    clock["now"] = NOW + timedelta(minutes=95)
    assert order(15)["status"] == "ordered"


def test_split_rule_leaves_big_orders_to_the_auto_limit():
    issue(auto_approve_limit_eur=50, split_window_minutes=60)
    assert order(40)["status"] == "ordered"
    o = order(60)  # over the limit on its own: spend.auto_limit, not reported twice
    assert fired(o) == ["spend.auto_limit"]


def test_weekly_limit_counts_monday_to_sunday():
    issue(weekly_limit_eur=100)
    assert order(90)["status"] == "ordered"
    o = order(20)
    assert o["status"] == "pending_approval" and fired(o) == ["spend.weekly_limit"]
    assert "€110.00" in o["decision"]["findings"][0]["message"]
    clock["now"] = NOW + timedelta(days=3)  # Monday: a new week
    assert order(20)["status"] == "ordered"


def test_first_time_product_gets_the_chosen_tier_until_it_has_been_ordered():
    issue(first_time_products="ask")
    o = order(10)
    assert o["status"] == "pending_approval" and fired(o) == ["product.first_time"]
    assert S.decide(o["id"], "chef", True)["status"] == "ordered"
    assert order(10)["status"] == "ordered"  # now a known product
    o = order(10, pid="OATS")
    assert fired(o) == ["product.first_time"] and o["decision"]["findings"][0]["product_id"] == "OATS"


def test_first_time_notify_lets_the_order_through_with_an_fyi():
    issue(first_time_products="notify")
    o = order(10)
    assert o["status"] == "ordered" and o["decision"]["outcome"] == "notify"


def test_pause_refuses_orders_and_approvals_until_resumed():
    issue(auto_approve_limit_eur=50)
    held = order(60)
    assert held["status"] == "pending_approval"
    pause.pause(pause.PauseIn(reason="Incident: agent orders on hold", by="valio ops"))
    o = order(10)
    assert o["status"] == "refused" and "ordering.paused" in fired(o)
    assert "Incident: agent orders on hold" in o["decision"]["findings"][0]["message"]
    assert S.decide(held["id"], "chef", True)["status"] == "refused"
    assert pause.status()["paused_for_everyone"] is True
    pause.resume(pause.ResumeIn(by="valio ops"))
    assert order(10)["status"] == "ordered"
    events = [e["event"] for e in store.audit_events(limit=200)]
    assert "ordering.paused" in events and "ordering.resumed" in events


def test_pause_for_one_buyer_leaves_the_others():
    issue("M", buyer="Hotel")
    issue("M2", buyer="School")
    pause.pause(pause.PauseIn(buyer="School", reason="Unpaid invoices"))
    assert order(10, mid="M")["status"] == "ordered"
    assert order(10, mid="M2")["status"] == "refused"
    assert pause.resume(pause.ResumeIn(buyer="Hotel")) is None  # not paused


def test_pause_over_rest_needs_the_global_approver_code(monkeypatch):
    from fastapi.testclient import TestClient
    from sous.api import app
    monkeypatch.setattr(config, "APPROVER_CODE", "secret")
    c = TestClient(app)
    assert c.get("/agent-ordering").json() == {"paused_for_everyone": False, "everyone": None, "buyer_pauses": 0}
    assert c.post("/agent-ordering/pause", json={"reason": "x"}).status_code == 401
    r = c.post("/agent-ordering/pause", json={"reason": "Incident"}, headers={"x-approver-code": "secret"})
    assert r.status_code == 200 and r.json()["paused_for_everyone"] is True
    assert c.get("/agent-ordering").json()["everyone"]["reason"] == "Incident"
    assert c.post("/agent-ordering/resume", json={}, headers={"x-approver-code": "secret"}).status_code == 200
    assert c.post("/agent-ordering/resume", json={}, headers={"x-approver-code": "secret"}).status_code == 404
    rules = {r["id"] for r in c.get("/rules").json()["rules"]}
    assert {"ordering.paused", "spend.weekly_limit", "spend.split_orders", "product.first_time"} <= rules


def test_usual_products_count_as_known_for_first_time():
    issue(first_time_products="ask", usual_products=["MILK"])
    assert order(10)["status"] == "ordered"
    assert fired(order(10, pid="OATS")) == ["product.first_time"]


def test_price_change_against_the_last_price_paid():
    issue(max_price_change_pct=20)
    assert order(10)["status"] == "ordered"  # no history: nothing to compare with
    PRICE["eur"] = 1.15  # +15 %: within the mandate's 20 %
    clock["now"] += timedelta(minutes=1)
    assert order(10)["status"] == "ordered"
    clock["now"] += timedelta(minutes=1)
    PRICE["eur"] = 1.50  # +30 % on the last price paid (1.15)
    o = order(10)
    assert o["status"] == "pending_approval" and fired(o) == ["price.changed"]
    msg = o["decision"]["findings"][0]["message"]
    assert "€1.50 per ST now, 30% above the €1.15 paid" in msg and "above 20%" in msg
    PRICE["eur"] = 0.70  # -39 % below 1.15: also asked (a different pack read as the usual one)
    assert "below" in order(10)["decision"]["findings"][0]["message"]
    assert fired(order(10, pid="OATS")) == []  # another product has its own history


def test_price_change_is_on_by_default():
    S.issue_mandate(Mandate(id="D", buyer="Hotel", agent_id="a", issued_by="chef", auto_approve_limit_eur=5000,
                            min_delivery_reliability=None))
    assert Mandate.model_fields["max_price_change_pct"].default == 20.0
    assert order(10, mid="D")["status"] == "ordered"
    PRICE["eur"] = 2.0
    assert fired(order(10, mid="D")) == ["price.changed"]


def test_demo_policy_substitutes_and_new_products_ask_and_seed_upgrades_deployments():
    from scripts import seed
    hotel = next(m for m in seed.MANDATES if m.id == "M-HOTEL-AURORA")
    assert all(m.substitutions == "ask" and m.first_time_products == "ask" and m.weekly_limit_eur
               for m in seed.MANDATES)
    assert {"464119", "476240", "464201"} <= set(hotel.usual_products)  # the judge quickstart's milks
    school = next(m for m in seed.MANDATES if m.id == "M-SCHOOL-ESPOO")
    assert school.autonomy == "reorder" and all(m.autonomy == "order" for m in seed.MANDATES if m is not school)
    # a deployment seeded before the policy: the next start re-signs it with the policy fields, keeps the rest
    S.issue_mandate(Mandate(**{**hotel.model_dump(), "substitutions": "notify", "first_time_products": None,
                               "usual_products": [], "weekly_limit_eur": None, "auto_approve_limit_eur": 1234}))
    seed.main()
    m = S.get_mandate("M-HOTEL-AURORA")["mandate"]
    assert m["substitutions"] == "ask" and m["first_time_products"] == "ask" and m["weekly_limit_eur"] == 25000
    assert m["auto_approve_limit_eur"] == 1234  # what a human changed stays
    assert signing.verify(S.get_mandate("M-HOTEL-AURORA")["jws"], "sous-mandate+jws")


def test_demo_mandates_add_up_sliced_orders_red_team_rt00435():
    """Red team on a Verda GPU (docs/REDTEAM.md): with the split rule off, the school's agent placed 3 x €160.40 of
    coconut cream silently (€481.20 > €300 alone). Every demo mandate now sums an hour of the agent's own orders,
    the school has a daily limit, and a deployment seeded before that is re-signed at the next start."""
    from scripts import seed
    assert all(m.split_window_minutes == 60 for m in seed.MANDATES)
    assert {"split_window_minutes", "daily_limit_eur"} <= set(seed.POLICY_FIELDS)
    school = next(m for m in seed.MANDATES if m.id == "M-SCHOOL-ESPOO")
    assert school.auto_approve_limit_eur < school.daily_limit_eur < school.hard_limit_eur
    S.issue_mandate(Mandate(**{**school.model_dump(), "split_window_minutes": None, "daily_limit_eur": None}))
    seed.main()
    m = Mandate(**S.get_mandate(school.id)["mandate"])
    assert m.split_window_minutes == 60 and m.daily_limit_eur == school.daily_limit_eur
    # the replayed attack, on the re-signed mandate's limits (€300 alone): three 25-litre slices of €160.40
    issue(auto_approve_limit_eur=m.auto_approve_limit_eur, split_window_minutes=m.split_window_minutes,
          daily_limit_eur=m.daily_limit_eur)
    PRICE["eur"] = 160.40
    assert order(1)["status"] == "ordered"
    clock["now"] += timedelta(minutes=5)
    o = order(1)
    assert o["status"] == "pending_approval" and fired(o) == ["spend.split_orders"]
    assert "€320.80" in o["decision"]["findings"][0]["message"]
    clock["now"] += timedelta(minutes=5)
    o = order(1)  # the held slice adds nothing: still the first slice + this one
    assert o["status"] == "pending_approval" and fired(o) == ["spend.split_orders"]



# --- QA findings 14.11 (rene-codex-qa): ordered time, exact cents and percentages, private pauses ---------------

SUN_2350 = datetime(2026, 11, 15, 23, 50, tzinfo=config.TZ)


@pytest.mark.parametrize("limit", ["daily_limit_eur", "weekly_limit_eur"])
def test_an_order_counts_in_the_period_it_was_approved(limit):
    issue(auto_approve_limit_eur=50, **{limit: 100})
    clock["now"] = SUN_2350
    held = order(70)
    assert held["status"] == "pending_approval"
    clock["now"] = SUN_2350 + timedelta(minutes=15)  # Monday 00:05: approved, so Monday's spend
    assert S.decide(held["id"], "chef", True)["status"] == "ordered"
    clock["now"] += timedelta(minutes=1)
    o = order(40)  # €70 + €40 = €110 ordered today / this week
    assert o["status"] == "pending_approval" and fired(o) == [f"spend.{limit[:-4]}"]


def test_split_orders_ignore_orders_a_human_approved():
    issue(auto_approve_limit_eur=50, split_window_minutes=60, first_time_products="ask")
    o = order(40)
    assert fired(o) == ["product.first_time"] and S.decide(o["id"], "chef", True)["status"] == "ordered"
    clock["now"] += timedelta(minutes=1)
    assert order(15)["status"] == "ordered"  # €40 already had a human; €15 alone is fine


@pytest.mark.parametrize("rule", ["daily_limit_eur", "weekly_limit_eur", "split"])
def test_limits_compare_exact_cents(rule):
    kw = {"auto_approve_limit_eur": 3.30, "split_window_minutes": 60} if rule == "split" else {rule: 3.30}
    issue(**kw)
    PRICE["eur"] = 1.10  # one pack each: €1.10, then €2.20
    assert order(1)["status"] == "ordered"
    PRICE["eur"] = 2.20
    assert order(1)["status"] == "ordered"  # €3.30 is exactly the limit: not above it
    PRICE["eur"] = 0.01
    assert order(1)["status"] == "pending_approval"


def test_last_paid_price_is_the_order_that_went_through_last():
    issue(first_time_products="ask", max_price_change_pct=20)
    PRICE["eur"] = 1.0
    cheap = order(10)
    clock["now"] += timedelta(minutes=1)
    PRICE["eur"] = 2.0
    dear = order(10)
    clock["now"] += timedelta(minutes=1)
    assert S.decide(dear["id"], "chef", True)["status"] == "ordered"
    clock["now"] += timedelta(minutes=1)
    assert S.decide(cheap["id"], "chef", True)["status"] == "ordered"  # most recently ordered: €1.00
    clock["now"] += timedelta(minutes=1)
    o = order(10)  # €2.00 again: +100 % on €1.00
    assert fired(o) == ["price.changed"] and "€1.00 paid" in o["decision"]["findings"][0]["message"]


def test_a_price_change_of_exactly_the_limit_passes():
    issue(max_price_change_pct=20)
    PRICE["eur"] = 1.10
    assert order(10)["status"] == "ordered"
    clock["now"] += timedelta(minutes=1)
    PRICE["eur"] = 0.88  # exactly 20 % lower
    assert order(10)["status"] == "ordered"
    clock["now"] += timedelta(minutes=1)
    PRICE["eur"] = 0.70  # 20.45 % below 0.88
    assert fired(order(10)) == ["price.changed"]


def test_price_per_litre_against_the_same_kind_of_product():
    """Rene: not only the unit price; a pricier product per litre than the buyer usually pays asks too."""
    issue(max_price_change_pct=20)
    PRICE["MILK"] = 1.00
    assert order(10, pid="MILK")["status"] == "ordered"
    clock["now"] += timedelta(minutes=1)
    PRICE["OATS"] = 1.15  # +15 % per litre on what was paid for this kind of product
    assert order(10, pid="OATS")["status"] == "ordered"
    clock["now"] += timedelta(minutes=1)
    PRICE["MILK"] = 1.50  # same product: price.changed (+50 % on its own last price)...
    o = order(10, pid="MILK")
    assert "price.changed" in fired(o)
    clock["now"] += timedelta(minutes=1)
    PRICE["OATS"] = 1.45  # ...other product: +26 % on the €1.15/L last paid for this kind (OATS itself was €1.15)
    PRICE["MILK"] = 1.00
    issue("M3", max_price_change_pct=20)
    assert order(10, pid="MILK", mid="M3")["status"] == "ordered"
    clock["now"] += timedelta(minutes=1)
    o = order(10, pid="OATS", mid="M3")  # first OATS order under M3: €1.45/L vs €1.00/L paid for MILK
    assert fired(o) == ["price.comparison_changed"]
    msg = o["decision"]["findings"][0]["message"]
    assert "€1.45/L, 45% above the €1.00/L paid" in msg and "above 20%" in msg
    clock["now"] += timedelta(minutes=1)
    PRICE["OATS"] = 0.50  # cheaper per litre than usual: no question
    assert order(10, pid="OATS", mid="M3")["status"] in ("ordered", "pending_approval")


def test_public_pause_status_hides_single_buyers_and_reasons(monkeypatch):
    from fastapi.testclient import TestClient
    from sous.api import app
    monkeypatch.setattr(config, "APPROVER_CODE", "secret")
    issue("M2", buyer="Private School")
    pause.pause(pause.PauseIn(buyer="Private School", reason="Unpaid invoices"))
    c = TestClient(app)
    public = c.get("/agent-ordering").json()
    assert public == {"paused_for_everyone": False, "everyone": None, "buyer_pauses": 1}
    assert "Private School" not in str(public) and "invoice" not in str(public).lower()
    full = c.get("/agent-ordering", headers={"x-approver-code": "secret"}).json()
    assert full["pauses"][0]["buyer"] == "Private School"
    o = order(10, mid="M2")  # the buyer's own agent learns why
    assert o["status"] == "refused" and "Unpaid invoices" in o["decision"]["findings"][0]["message"]


# --- autonomy level (mandate) capped by Valio, and Peppol BIS Ordering export -------------------------------

def test_autonomy_advise_holds_every_order():
    issue(autonomy="advise")
    o = order(10)
    assert o["status"] == "pending_approval" and fired(o) == ["autonomy.advise"]


def test_autonomy_reorder_only_usual_products_alone():
    issue(autonomy="reorder", usual_products=["MILK"])
    assert order(10)["status"] == "ordered"
    o = order(10, pid="OATS")
    assert fired(o) == ["autonomy.reorder"] and S.decide(o["id"], "chef", True)["status"] == "ordered"
    clock["now"] += timedelta(minutes=1)
    assert order(10, pid="OATS")["status"] == "ordered"  # bought once, now a usual product


def test_valio_cap_lowers_but_never_raises_autonomy():
    from sous import autonomy
    issue("M", buyer="Hotel")
    issue("M2", buyer="School", autonomy="advise")
    autonomy.set_cap(autonomy.CapIn(buyer="Hotel", max="advise", reason="New account, first month supervised"))
    o = order(10)
    assert fired(o) == ["autonomy.capped"] and "First month supervised".lower() in o["decision"]["findings"][0]["message"].lower()
    autonomy.set_cap(autonomy.CapIn(max="reorder", reason="Valio-wide caution"))  # every buyer: reorder
    assert fired(order(10, mid="M2")) == ["autonomy.advise"]  # the mandate's own lower level stays
    autonomy.lift("Hotel", "valio")
    o = order(10, pid="OATS")  # every-buyer cap still says reorder; OATS is new
    assert fired(o) == ["autonomy.capped"] and "Valio-wide caution" in o["decision"]["findings"][0]["message"]
    assert autonomy.status()["everyone"]["max"] == "reorder" and "caps" not in autonomy.status()


def test_autonomy_caps_over_rest(monkeypatch):
    from fastapi.testclient import TestClient
    from sous.api import app
    monkeypatch.setattr(config, "APPROVER_CODE", "secret")
    c = TestClient(app)
    assert c.post("/autonomy-caps", json={"buyer": "Hotel", "max": "advise", "reason": "x"}).status_code == 401
    r = c.post("/autonomy-caps", json={"buyer": "Hotel", "max": "advise", "reason": "Invoices overdue"},
               headers={"x-approver-code": "secret"})
    assert r.status_code == 200
    public = c.get("/autonomy-caps").json()
    assert public["buyer_caps"] == 1 and "Hotel" not in str(public) and "Invoices" not in str(public)
    assert c.get("/autonomy-caps", headers={"x-approver-code": "secret"}).json()["caps"][0]["max"] == "advise"
    assert c.delete("/autonomy-caps?buyer=Hotel", headers={"x-approver-code": "secret"}).status_code == 200


def test_peppol_order_and_order_response():
    from xml.etree import ElementTree as ET
    from sous import peppol
    issue(auto_approve_limit_eur=50)
    ok, held = order(20), order(60)
    cbc = "{" + peppol.NS["cbc"] + "}"
    raw = peppol.order_xml(S.get_order(ok["id"]))
    assert b"<Order xmlns=" in raw and b"ns0:" not in raw
    x = ET.fromstring(raw)
    assert x.tag.endswith("}Order") and x.find(cbc + "CustomizationID").text == peppol.CUSTOMIZATION_ORDER
    assert x.find(cbc + "ProfileID").text == peppol.PROFILE and x.find(cbc + "ID").text == ok["id"]
    line = x.find("{%s}OrderLine/{%s}LineItem" % (peppol.NS["cac"], peppol.NS["cac"]))
    assert line.find(cbc + "Quantity").attrib["unitCode"] == "EA" and line.find(cbc + "Quantity").text == "20"
    codes = {o["id"]: ET.fromstring(peppol.order_response_xml(S.get_order(o["id"]))).find(cbc + "OrderResponseCode").text
             for o in (ok, held)}
    assert codes == {ok["id"]: "AP", held["id"]: "AB"}
    S.decide(held["id"], "chef", False, "not this week")
    r = ET.fromstring(peppol.order_response_xml(S.get_order(held["id"])))
    assert r.find(cbc + "OrderResponseCode").text == "RE"


def test_peppol_routes_follow_order_read_access():
    from fastapi.testclient import TestClient
    from sous.api import app
    issue()
    o = order(10)
    c = TestClient(app)
    r = c.get(f"/orders/{o['id']}/peppol/order.xml")
    assert r.status_code in (200, 404) and (r.status_code == 404 or r.headers["content-type"].startswith("application/xml"))
    assert c.get("/orders/PO-NOPE/peppol/order-response.xml").status_code == 404


# --- Valio's lost-demand view (sous/lost_demand.py) ----------------------------------------------------------

def test_lost_demand_splits_supply_buyer_rules_and_human_no(monkeypatch):
    from sous import holds, lost_demand
    monkeypatch.setattr(S, "_options", lambda *a, **k: [])  # no substitute search: CI has no catalog data
    issue(auto_approve_limit_eur=50, hard_limit_eur=100)
    assert order(20)["status"] == "ordered"                      # placed: €20
    no = order(60)                                               # held, then the human says no: declined €60
    S.decide(no["id"], "chef", False, "not this week")
    assert order(150)["status"] == "refused"                     # spend.hard_limit: buyer's own rule, €150
    holds.create(holds.HoldIn(product_id="OATS", kind="recall", reason="Batch fault"), "Oats")
    assert order(10, pid="OATS")["status"] == "refused"          # product.held: Valio couldn't supply, €10
    monkeypatch.setattr(Q, "offers", lambda ids, *a, **k: ([], clock["now"]) if "NOPE" in ids else fake_offers(ids, *a, **k))
    S.request_quote(S.QuoteRequest(agent_id="a", mandate_id="M", delivery_date=MON, items=[
        S.QuoteItem(product_ids=["NOPE"], amount=40, unit="L", note="cream")]))
    r = lost_demand.report(None, now=NOW + timedelta(hours=1))
    c = r["causes"]
    assert (c["supply"]["orders"], c["supply"]["value_ex_vat"]) == (1, 10.0)
    assert c["supply"]["top_rules"][0]["rule"] == "product.held"
    assert (c["buyer"]["value_ex_vat"], c["declined"]["value_ex_vat"], c["too_late"]["orders"]) == (150.0, 60.0, 0)
    assert r["placed_value_ex_vat"] == 20.0 and r["lost_value_ex_vat"] == 220.0
    assert r["uncovered"][0]["item"] == "cream" and r["uncovered"][0]["uncovered"] == 40.0
    assert any(p["code"] == "PRODUCT_HELD" for p in r["passed_over"])
    assert "by_buyer" not in r and lost_demand.report(None, with_buyers=True)["by_buyer"][0]["buyer"] == "Hotel"


def test_lost_demand_over_rest_hides_buyers_without_the_code(monkeypatch):
    from fastapi.testclient import TestClient
    from sous.api import app
    monkeypatch.setattr(config, "APPROVER_CODE", "secret")
    issue(hard_limit_eur=10)
    order(20)
    c = TestClient(app)
    public = c.get("/stats/lost-demand?all_time=true").json()
    assert public["causes"]["buyer"]["orders"] == 1 and "by_buyer" not in public and "Hotel" not in str(public)
    full = c.get("/stats/lost-demand?all_time=true", headers={"x-approver-code": "secret"}).json()
    assert full["by_buyer"][0]["buyer"] == "Hotel"
