"""Regression tests for the core review fixes. Offline: no Valio API calls, no data files."""

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import httpx
import pytest

from sous import aimo, catalog, config, quote as Q, service as S, signing, store
from sous.mandate import Mandate, evaluate

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=config.TZ)
REAL_CATALOG_GET = catalog.get  # the env fixture stubs catalog.get; the thread-safety test needs the real one


def product(pid="1", **kw):
    p = {
        "id": pid, "name": "Lactose-free milk 1 l", "names": {"fi": "Laktoositon maito 1 l", "en": "", "sv": ""},
        "brand": "", "vendor": "", "category": "10400", "country_of_origin": "fi", "sales_unit": "ST",
        "base_unit": "ST", "units": {"ST": 1.0}, "allowed_lot_size": 0.01, "temperature": "chilled", "labels": [],
        "claims": ["FREE_FROM_LACTOSE"],
        "allergens": {"declared": True, "contains": ["AM"], "may_contain": [], "free_from": []},
        "size_from_name": (1.0, "L"), "delivery_reliability": 0.99, "co2_kg_per_kg_group_avg": 1.4,
        "official_substitutes": [], "deleted": False,
    }
    p.update(kw)
    return p


PRICE = {"catalogPrice": {"value": 1.4, "valueWithTax": 1.59, "validTo": "2026-12-31T00:00:00+0200"},
         "comparisonPrice": {"value": 1.4}, "comparisonUnit": "L", "tax": {"taxRate": 13.5}, "error": False}


def avail(amount=5000.0, closing="2026-11-15T17:00:00+0200", day="2026-11-16", model="Stock"):
    return {"logisticModel": model, "closingTime": closing, "salesBlock": False,
            "scheduleLines": [{"salesBlock": False, "availableAmount": amount, "availableDate": day,
                               "salesUnit": "ST"}]}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(config, "DB_URL", "")
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(catalog, "get", lambda pid: product(pid))
    monkeypatch.setattr(S, "_now", lambda: NOW)
    monkeypatch.setattr(S.notify, "send", lambda *a, **k: None)
    yield
    signing._key.cache_clear()


def mandate(mid="M", agent="a", **kw):
    m = Mandate(**{"id": mid, "buyer": "Hotel", "agent_id": agent, "issued_by": "chef", **kw})
    S.issue_mandate(m)
    return m


def quote(qid="Q-1", amount=1000, agent="a", mandate_id="M", day="2026-11-16"):
    line = Q.build_offer(product(), PRICE, avail(), date.fromisoformat(day), amount, "L", NOW)
    q = {"id": qid, "agent_id": agent, "mandate_id": mandate_id, "issued_at": NOW.isoformat(),
         "expires_at": "2026-11-13T23:00:00+02:00", "requested_delivery_date": day, "currency": "EUR",
         "lines": [line], "total_ex_vat": line["line_total_ex_vat"], "total_inc_vat": line["line_total_inc_vat"],
         "uncovered": [], "alternatives_considered": []}
    store.put_quote(q, signing.sign(q, "sous-quote+jws"))
    return qid


def gateway_error(fn, *a, **kw) -> S.GatewayError:
    with pytest.raises(S.GatewayError) as e:
        fn(*a, **kw)
    return e.value


def count_orders() -> int:
    return len(store.list_orders())


# --- 1. concurrency ----------------------------------------------------------------

def test_concurrent_orders_with_same_idempotency_key_make_one_order(monkeypatch):
    mandate()
    quote()
    slow = store.spent_today
    monkeypatch.setattr(store, "spent_today", lambda *a: (time.sleep(0.05), slow(*a))[1])
    with ThreadPoolExecutor(4) as ex:
        results = list(ex.map(lambda _: S.place_order("a", "M", "Q-1", idempotency_key="k1"), range(4)))
    assert len({o["id"] for o in results}) == 1 and count_orders() == 1
    assert store.get_quote("Q-1")["_order_id"] == results[0]["id"]


def test_concurrent_orders_cannot_overrun_daily_limit(monkeypatch):
    mandate(auto_approve_limit_eur=5000, daily_limit_eur=2000)
    quote("Q-1")
    quote("Q-2")
    slow = store.spent_today
    monkeypatch.setattr(store, "spent_today", lambda *a: (time.sleep(0.05), slow(*a))[1])
    with ThreadPoolExecutor(2) as ex:
        a, b = ex.map(lambda q: S.place_order("a", "M", q), ["Q-1", "Q-2"])
    assert sorted([a["status"], b["status"]]) == ["ordered", "pending_approval"]


def test_approve_and_reject_racing_only_one_wins(monkeypatch):
    mandate(auto_approve_limit_eur=100)
    o = S.place_order("a", "M", quote())
    assert o["status"] == "pending_approval"
    slow = S._mandate
    monkeypatch.setattr(S, "_mandate", lambda mid: (time.sleep(0.05), slow(mid))[1])
    outcomes = []

    def go(approve):
        try:
            outcomes.append(S.decide(o["id"], "chef", approve)["status"])
        except S.GatewayError as e:
            outcomes.append(e.code)

    ts = [threading.Thread(target=go, args=(x,)) for x in (True, False)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(outcomes) in (["not_pending", "ordered"], ["not_pending", "rejected"])
    assert store.get_order(o["id"])["status"] in ("ordered", "rejected")


def test_insert_order_never_overwrites_and_decide_is_conditional():
    mandate(auto_approve_limit_eur=100)
    o = S.place_order("a", "M", quote(), idempotency_key="k1")
    with pytest.raises(store.Conflict):
        store.insert_order({**o, "id": "PO-OTHER"})  # same idempotency key
    assert store.get_order(o["id"])["status"] == "pending_approval" and count_orders() == 1
    # someone else decided in between: the conditional update refuses
    store.put_order({**o, "status": "rejected"})
    assert not store.update_order({**o, "status": "ordered"}, expected_status="pending_approval")


def test_failed_order_insert_releases_the_quote(monkeypatch):
    mandate()
    quote()

    def boom(o):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "insert_order", boom)
    with pytest.raises(RuntimeError):
        S.place_order("a", "M", "Q-1")
    assert store.get_quote("Q-1")["_order_id"] is None


# --- 2. decide() re-checks the mandate ---------------------------------------------------

def _pending():
    o = S.place_order("a", "M", quote())
    assert o["status"] == "pending_approval"
    return o


def test_approve_after_mandate_expired_is_refused(monkeypatch):
    mandate(auto_approve_limit_eur=100, valid_until=NOW + timedelta(hours=1))
    o = _pending()
    monkeypatch.setattr(S, "_now", lambda: NOW + timedelta(hours=2))
    r = S.decide(o["id"], "chef", True)
    assert r["status"] == "refused" and "expired" in r["approval"]["result_reason"]


def test_approve_after_delivery_date_is_expired(monkeypatch):
    mandate(auto_approve_limit_eur=100)
    o = _pending()
    # cut-off check is not what fires here: drop it to isolate the delivery-date check
    stored = store.get_order(o["id"])
    for l in stored["lines"]:
        l["order_cutoff"] = None
    store.put_order(stored)
    monkeypatch.setattr(S, "_now", lambda: datetime(2026, 11, 17, 9, 0, tzinfo=config.TZ))
    r = S.decide(o["id"], "chef", True)
    assert r["status"] == "expired" and "2026-11-16" in r["approval"]["result_reason"]


def test_approve_after_revocation_is_refused():
    mandate(auto_approve_limit_eur=100)
    o = _pending()
    S.revoke_mandate("M", "chef")
    assert S.decide(o["id"], "chef", True)["status"] == "refused"


# --- 3. naive datetimes ----------------------------------------------------------------

def test_naive_valid_until_and_closing_time_are_helsinki():
    m = Mandate(id="M", buyer="B", agent_id="a", issued_by="h", valid_until="2026-11-14T00:00:00")
    assert m.valid_until.tzinfo is not None and m.valid_until.utcoffset() == timedelta(hours=2)
    out, _ = evaluate(m, "a", [], requested_date=date(2026, 11, 16), now=NOW)  # no TypeError
    assert out == "block"
    assert Q._dt("2026-11-15T17:00:00").tzinfo is not None
    o = Q.build_offer(product(), PRICE, avail(closing="2026-11-13T15:00:00"), date(2026, 11, 16), 10, "L", NOW)
    assert "CUTOFF_PASSED" in {i["code"] for i in o["issues"]}


# --- 4. quotes are bound to their agent and mandate --------------------------------------

def test_quote_for_another_agent_or_mandate_is_rejected():
    mandate("M", "a")
    mandate("M2", "b")
    quote("Q-1", amount=100, agent="a", mandate_id="M")
    assert gateway_error(S.place_order, "b", "M2", "Q-1").code == "quote_mismatch"
    mandate("M3", "a")
    assert gateway_error(S.place_order, "a", "M3", "Q-1").code == "quote_mismatch"
    assert S.place_order("a", "M", "Q-1")["status"] == "ordered"


# --- 5. signed payloads, not the stored copy -------------------------------------------

def test_order_is_built_from_the_signed_quote_not_the_db_copy():
    mandate(auto_approve_limit_eur=500)
    quote(amount=1000)  # signed total €1,400 -> ask
    row = store._exec("SELECT data FROM quote WHERE id = 'Q-1'")[0][0]
    tampered = json.loads(row["data"])
    tampered["total_ex_vat"] = 1.0
    for l in tampered["lines"]:
        l["line_total_ex_vat"] = 1.0
    store._exec("UPDATE quote SET data = ? WHERE id = 'Q-1'", (json.dumps(tampered),))
    o = S.place_order("a", "M", "Q-1")
    assert o["total_ex_vat"] == 1400.0 and o["status"] == "pending_approval"


def test_quote_signature_must_have_quote_typ():
    mandate()
    q = json.loads(store._exec("SELECT data FROM quote WHERE id = ?", (quote(),))[0][0]["data"])
    store._exec("UPDATE quote SET jws = ? WHERE id = 'Q-1'", (signing.sign(q, "sous-order+jws"),))
    assert gateway_error(S.place_order, "a", "M", "Q-1").code == "quote_signature"


def test_verify_checks_typ():
    tok = signing.sign({"x": 1}, "sous-quote+jws")
    assert signing.verify(tok, "sous-quote+jws") == {"x": 1}
    assert signing.verify(tok, "sous-mandate+jws") is None
    assert signing.verify("garbage") is None


def test_tampered_mandate_is_rejected():
    mandate(auto_approve_limit_eur=500)
    row = store._exec("SELECT data FROM mandate WHERE id = 'M'")[0][0]
    m = json.loads(row["data"])
    m["auto_approve_limit_eur"] = 1e9  # edited in the DB, not re-signed
    store._exec("UPDATE mandate SET data = ? WHERE id = 'M'", (json.dumps(m),))
    assert S._mandate("M").auto_approve_limit_eur == 500  # the signed payload wins
    store._exec("UPDATE mandate SET jws = ? WHERE id = 'M'", (signing.sign(m, "sous-quote+jws"),))
    assert gateway_error(S._mandate, "M").code == "mandate_signature"


# --- 6. lot sizes in plan() ----------------------------------------------------------------

def test_plan_respects_lot_sizes():
    six = product("A", allowed_lot_size=6.0)
    o = Q.build_offer(six, PRICE, avail(amount=5000), date(2026, 11, 16), 10, "L", NOW)
    assert o["allowed_lot_size"] == 6.0
    other = Q.build_offer(product("B", delivery_reliability=0.95), PRICE, avail(), date(2026, 11, 16), 10, "L", NOW)
    lines, left = Q.plan(10, "L", [o, other])
    assert [(l["product_id"], l["quantity"]) for l in lines] == [("A", 12)] and left == 0
    # stock caps to whole lots: 20 in stock, lots of 6 -> 18, the rest from B
    short = Q.build_offer(six, PRICE, avail(amount=20), date(2026, 11, 16), 30, "L", NOW)
    lines, left = Q.plan(30, "L", [short, other])
    assert [(l["product_id"], l["quantity"]) for l in lines] == [("A", 18), ("B", 12)] and left == 0


# --- 7. catalog connection is safe across threads ------------------------------------------

def test_catalog_get_from_many_threads(tmp_path, monkeypatch):
    db = tmp_path / "catalog.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE product (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    c.executemany("INSERT INTO product VALUES (?, ?)", [(str(i), json.dumps({"id": str(i)})) for i in range(200)])
    c.commit()
    c.close()
    monkeypatch.setattr(config, "CATALOG_DB", db)
    monkeypatch.setattr(catalog, "_local", threading.local())
    with ThreadPoolExecutor(16) as ex:
        got = list(ex.map(lambda i: REAL_CATALOG_GET(str(i % 200)), range(3000)))
    assert all(g["id"] == str(i % 200) for i, g in enumerate(got))


# --- 8. allergen families and what counts as declared -----------------------------------------

def allergens(contains=(), may=(), free=(), claims=()):
    return product(claims=list(claims), allergens={"declared": True, "contains": list(contains),
                                                   "may_contain": list(may), "free_from": list(free)})


def test_allergen_specific_code_hits_declared_family():
    st = catalog.allergen_status
    assert st(allergens(contains=["AN"]), ["SH"])["status"] == "contains"
    assert st(allergens(contains=["AW"]), ["UW"])["status"] == "contains"
    assert st(allergens(may=["AN"]), ["SH"])["status"] == "may_contain"
    assert st(allergens(contains=["AM"]), ["ML"])["status"] == "contains"
    # ... unless the product is explicitly free of that member
    assert st(allergens(contains=["AM"], free=["ML"]), ["ML"])["status"] == "safe"
    assert st(allergens(contains=["AM"], claims=["FREE_FROM_LACTOSE"]), ["ML"])["status"] == "safe"
    assert st(allergens(contains=["AW"], free=["UW"]), ["UW"])["status"] == "safe"
    # family down to member still works
    assert st(allergens(contains=["SH"]), ["AN"])["status"] == "contains"


def test_allergen_lone_free_from_is_not_a_declaration():
    st = catalog.allergen_status
    assert st(allergens(free=["AE"]), ["AM"])["status"] == "unknown"  # says nothing about milk
    assert st(allergens(free=["AM"]), ["AM"])["status"] == "safe"  # explicitly milk-free
    assert st(allergens(free=["AN"]), ["SH"])["status"] == "safe"  # family free covers the member
    assert st(allergens(free=["X99"]), ["AM"])["status"] == "safe"  # "nothing to declare"
    assert st(allergens(contains=["X99"]), ["AM"])["status"] == "safe"
    assert st(allergens(contains=["AE"]), ["AM"])["status"] == "safe"  # a full allergen list without milk
    assert st(allergens(), ["AM"])["status"] == "unknown"


# --- 9. Valio Aimo transport errors become AimoError (-> 503) -----------------------------------

@pytest.mark.parametrize("response", [
    httpx.ConnectError("refused"),
    httpx.ReadTimeout("slow"),
    httpx.Response(200, content=b"<html>oops</html>"),
    httpx.Response(200, json=["not", "an", "object"]),
])
def test_aimo_transport_and_decode_errors(monkeypatch, response):
    monkeypatch.setattr(aimo, "_cache", {})

    def post(url, json=None):
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(aimo._client, "post", post)
    with pytest.raises(aimo.AimoError):
        aimo.prices(["1"], date(2026, 11, 16))
    req = S.QuoteRequest(agent_id="a", delivery_date=date(2026, 11, 16),
                         items=[S.QuoteItem(product_ids=["1"], amount=10, unit="L")])
    e = gateway_error(S.request_quote, req)
    assert (e.code, e.status) == ("valio_unavailable", 503)


# --- 11. closingTime belongs to an earlier available date --------------------------------------

def test_cutoff_of_earlier_date_does_not_block_later_requested_date():
    o = Q.build_offer(product(), PRICE, avail(day="2026-11-14", closing="2026-11-13T15:00:00+0200"),
                      date(2026, 11, 16), 10, "L", NOW)
    codes = {i["code"] for i in o["issues"]}
    assert o["orderable"] and "CUTOFF_PASSED" not in codes and "CUTOFF_UNKNOWN" in codes
    assert o["order_cutoff"] is None and o["delivery_date"] == "2026-11-16"
    assert Q.quote_expiry(NOW, [o]) == NOW + timedelta(seconds=config.QUOTE_TTL_SECONDS)


# --- 12. rate limiter client IP --------------------------------------------------------------

def test_rate_limiter_trusts_cf_header_only_when_configured(monkeypatch):
    from starlette.requests import Request

    from sous import api
    req = Request({"type": "http", "headers": [(b"cf-connecting-ip", b"6.6.6.6")], "client": ("10.0.0.5", 1234)})
    monkeypatch.delenv("SOUS_TRUST_CF_HEADER", raising=False)
    assert api._client_ip(req) == "10.0.0.5"
    monkeypatch.setenv("SOUS_TRUST_CF_HEADER", "1")
    assert api._client_ip(req) == "6.6.6.6"


# --- 13. uncovered amounts are kept per unit ------------------------------------------------

def test_uncovered_is_summed_per_unit():
    m = Mandate(id="M", buyer="B", agent_id="a", issued_by="h")
    line = Q.build_offer(product(), PRICE, avail(), date(2026, 11, 16), 10, "L", NOW)
    out, f = evaluate(m, "a", [line], requested_date=date(2026, 11, 16), now=NOW,
                      uncovered={"L": 5, "KG": 2, "SALES_UNIT": 3})
    [partial] = [x for x in f if x.rule == "need.partial"]
    assert out == "ask" and "5 L" in partial.message and "2 KG" in partial.message and "3 units" in partial.message
    # the old scalar form still works
    _, f = evaluate(m, "a", [line], requested_date=date(2026, 11, 16), now=NOW, uncovered=4.0, need_unit="L")
    assert any(x.rule == "need.partial" and "4 L" in x.message for x in f)
    q = {"uncovered": [{"uncovered": 5, "unit": "L"}, {"uncovered": 2, "unit": "KG"}, {"uncovered": 1, "unit": "L"}],
         "lines": [line], "requested_delivery_date": "2026-11-16"}
    _, f = S._evaluate(m, "a", q, NOW)
    [partial] = [x for x in f if x.rule == "need.partial"]
    assert "6 L" in partial.message and "2 KG" in partial.message
