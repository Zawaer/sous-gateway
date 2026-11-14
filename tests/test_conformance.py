"""The conformance kit (scripts/conformance.py) against the in-process app: same HTTP calls as against a deployed
gateway, but through TestClient, with test_sandbox.py's one-product catalog and mocked Valio Aimo. Offline.
The client runs the app's lifespan, so the MCP endpoint (/mcp) is up for agent_visible_approval."""

import json

from fastapi.testclient import TestClient

from scripts import conformance
import pytest

from sous import api, catalog, config, substitutes

from test_sandbox import env  # noqa: F401  (autouse: temp DB, approver code, mocked Valio, fixed clock)


@pytest.fixture(autouse=True)
def one_product_catalog(monkeypatch):
    """CI has no catalog data: the lifespan's index build and the substitute search see an empty catalog
    (test_sandbox's env already serves OAT through catalog.get)."""
    monkeypatch.setattr(catalog, "_db", lambda: None)
    monkeypatch.setattr(substitutes, "_same_category", lambda category: [])
    # the app's lifespan starts the MCP session manager, which runs once per instance: let each test run it again
    monkeypatch.setattr(api.mcp.session_manager, "_has_started", False)


def run_kit():
    with TestClient(api.app, base_url="https://sous.test", headers={"cf-connecting-ip": "10.9.9.9"}) as client:
        kit = conformance.Kit("https://sous.test", client=client, product="OAT", unit="L",
                              delivery_date="2026-11-16")
        return kit, kit.run_all()


def test_kit_passes_against_the_reference_gateway(env, monkeypatch):  # noqa: F811
    kit, report = run_kit()
    by = {c["id"]: c for c in report["checks"]}
    assert len(by) == len(conformance.Kit.CHECKS) == 17
    failing = {k: c["detail"] for k, c in by.items() if c["status"] != "pass"}
    assert not failing, failing
    assert report["quotes_requested"] == 6  # 4 + the two 10^6-unit stock_oracle quotes
    assert report["sandbox_lookups"]["quotes_used"] <= 5  # the anonymous quote is not the sandbox's
    # agent_visible_approval reached MCP, found the approval link's sandbox parameter and both JWSs, and was refused
    avis = by["agent_visible_approval"]["detail"]
    assert "MCP" in avis and "get_order output read" in avis and "all refused" in avis
    assert "?sandbox=" in avis and "jws" in avis and "pending_approval" in avis
    # stock_oracle: the mock has 99999 in stock; the mandate quote is sized to it, the anonymous one is not
    oracle = by["stock_oracle"]["detail"]
    assert "sized to stock: 99999" in oracle and "covering 1e+06 L" in oracle and "STOCK_SIZING_WITHHELD" in oracle
    text = conformance.table(report) + json.dumps(report)
    assert report["sandbox"] in text
    for secret in (kit.s["agent_token"], kit.s["approver_token"]):  # tokens never appear in the output
        assert secret not in text


def test_kit_catches_exact_stock(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "STOCK_EXPOSURE", "exact")
    _, report = run_kit()
    by = {c["id"]: c for c in report["checks"]}
    assert by["stock_buckets"]["status"] == "fail" and "available_amount" in by["stock_buckets"]["detail"]
    # exact mode sizes the anonymous quote to stock and shows available_amount: the oracle sees both
    assert by["stock_oracle"]["status"] == "fail"
    assert "capped at 99999" in by["stock_oracle"]["detail"]
    assert report["ok"] is False


def test_kit_catches_an_approval_link_the_agent_can_follow(env, monkeypatch):  # noqa: F811
    """Plant a hole: the sandbox id from the approval_url's query works as an approver code."""
    from sous import sandbox
    real = sandbox.may_approve
    monkeypatch.setattr(sandbox, "may_approve", lambda token, order_id: (token or "").startswith("SBX-")
                        or real(token, order_id))
    _, report = run_kit()
    by = {c["id"]: c for c in report["checks"]}
    assert by["agent_visible_approval"]["status"] == "fail"
    assert "ACCEPTED: order_url?sandbox= as X-Approver-Code -> 200" in by["agent_visible_approval"]["detail"]


def test_stock_oracle_reads_messages(env):  # noqa: F811
    """The text scan finds stock numbers in prose but not dates, percentages or the requested amount."""
    leak = {"lines": [{"issues": [{"message": "Only 120 ST in stock; 1000000 needed."}]}]}
    assert conformance.stock_leaks(leak, {1e6})
    assert conformance.stock_leaks({"error": "x", "message": "We can supply 340 units at most."}, {1e6})
    assert conformance.stock_leaks({"x": {"can_supply": 340}}, {1e6}, requested=1e6)
    fine = {"message": "Not enough in stock for the 1000000 L needed (stock level: short).",
            "m2": "Historically delivered in full only 95% of the time.",
            "m3": "Valio Aimo reported the order cut-off for 2026-11-16 only; cut-off Sun 15.11 17:00.",
            "available_amount": None, "valio_api_calls": {"availability": 1}}
    assert conformance.stock_leaks(fine, {1e6}, requested=1e6) == []
    assert conformance.stock_leaks({"note": "covers 437 L"}, {1e6}, reveal={437.0})


def test_agent_material_finds_links_and_tokens(env):  # noqa: F811
    links, tokens = conformance.agent_material(
        {"order": {"approval_url": "https://g.test/ui/?sandbox=SBX-1#/orders/PO-1",
                   "next": {"approve_link": "/orders/PO-1/approve?ticket=abc123def"},
                   "receipt_jws": "a" * 40, "issues": [{"code": "PARTIAL_STOCK"}], "auth": "vka_agent"}},
        agent_token="vka_agent")
    assert "https://g.test/ui/?sandbox=SBX-1#/orders/PO-1" in links
    assert "/orders/PO-1/approve?ticket=abc123def" in links
    values = [t for _, t in tokens]
    assert {"SBX-1", "abc123def", "a" * 40} <= set(values)
    assert "PARTIAL_STOCK" not in values and "vka_agent" not in values
