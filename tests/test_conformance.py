"""The conformance kit (scripts/conformance.py) against the in-process app: same HTTP calls as against a deployed
gateway, but through TestClient, with test_sandbox.py's one-product catalog and mocked Valio Aimo. Offline."""

import json

from fastapi.testclient import TestClient

from scripts import conformance
from sous import api, config

from test_sandbox import env  # noqa: F401  (autouse: temp DB, approver code, mocked Valio, fixed clock)


def run_kit():
    client = TestClient(api.app, base_url="https://sous.test", headers={"cf-connecting-ip": "10.9.9.9"})
    kit = conformance.Kit("https://sous.test", client=client, product="OAT", unit="L", delivery_date="2026-11-16")
    return kit, kit.run_all()


def test_kit_passes_against_the_reference_gateway(env):  # noqa: F811
    kit, report = run_kit()
    by = {c["id"]: c for c in report["checks"]}
    assert len(by) == len(conformance.Kit.CHECKS)
    failing = {k: c["detail"] for k, c in by.items() if c["status"] != "pass"}
    assert not failing, failing
    assert report["quotes_requested"] == 4
    assert report["sandbox_lookups"]["quotes_used"] <= 4
    text = conformance.table(report) + json.dumps(report)
    assert report["sandbox"] in text
    for secret in (kit.s["agent_token"], kit.s["approver_token"]):  # tokens never appear in the output
        assert secret not in text


def test_kit_catches_exact_stock(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "STOCK_EXPOSURE", "exact")
    _, report = run_kit()
    by = {c["id"]: c for c in report["checks"]}
    assert by["stock_buckets"]["status"] == "fail" and "available_amount" in by["stock_buckets"]["detail"]
    assert report["ok"] is False
