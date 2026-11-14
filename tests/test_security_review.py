"""Fixes from the read-only security review of the overnight trust changes (Codex run 01a1d86a).

Offline, like test_read_scopes.py (test_sandbox.py's fixtures): no data/, Valio Aimo mocked.
Stock sizing of anonymous quotes is tested in test_exposure.py.
"""

import logging
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from test_sandbox import CODE, c, env  # noqa: F401  (pytest fixtures)

from sous import access, config

SOUS = pathlib.Path(__file__).resolve().parent.parent / "sous"


# --- approver code compared in constant time, in one place -------------------

def test_approver_code_ok():
    assert access.approver_code_ok(CODE)
    assert not access.approver_code_ok("wrong") and not access.approver_code_ok("") and not access.approver_code_ok(None)


def test_approver_code_ok_without_configured_code(monkeypatch):
    monkeypatch.setattr(config, "APPROVER_CODE", "")
    assert not access.approver_code_ok("") and not access.approver_code_ok("anything")


def test_rest_guards_use_compare_digest(c, monkeypatch):
    calls = []
    real = access.hmac.compare_digest

    def spy(a, b):
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(access.hmac, "compare_digest", spy)
    assert c.post("/mandates/M-NOPE/revoke", headers={"X-Approver-Code": "wrong"}).status_code == 401
    assert calls, "require_approver must compare the code with hmac.compare_digest"
    calls.clear()
    assert c.delete("/sandbox/SBX-NOPE", headers={"X-Approver-Code": "wrong"}).status_code == 401
    assert calls, "require_global_approver must compare the code with hmac.compare_digest"


# --- fail closed without an approver code outside development ---------------

def test_startup_refuses_internet_facing_roles_without_code(monkeypatch):
    monkeypatch.setattr(config, "APPROVER_CODE", "")
    for role in ("prod", "staging", "demo"):
        with pytest.raises(RuntimeError, match="SOUS_APPROVER_CODE"):
            access.startup_check(role)


def test_startup_allows_dev_with_a_warning_and_any_role_with_a_code(monkeypatch, caplog):
    monkeypatch.setattr(config, "APPROVER_CODE", "")
    with caplog.at_level(logging.WARNING, logger="sous.access"):
        access.startup_check("dev")
        access.startup_check("ci")
    assert "SOUS_APPROVER_CODE is not set" in caplog.text
    monkeypatch.setattr(config, "APPROVER_CODE", CODE)
    access.startup_check("prod")  # no error


def test_app_lifespan_runs_the_check(monkeypatch):
    from sous import api, shared
    monkeypatch.setattr(config, "APPROVER_CODE", "")
    monkeypatch.setattr(shared, "ROLE", "staging")
    with pytest.raises(RuntimeError, match="refusing to start"):
        with TestClient(api.app):
            pass


def test_without_code_an_agent_token_still_cannot_approve(c, monkeypatch):
    monkeypatch.setattr(config, "APPROVER_CODE", "")
    for how in ({"Authorization": "Bearer vka_not-a-real-token"}, {"X-Approver-Code": "vka_not-a-real-token"}):
        for action in ("approve", "reject"):
            r = c.post(f"/orders/PO-NOPE/{action}", headers=how, json={"by": "agent"})
            assert r.status_code == 403 and "agent_cannot_approve" in r.text, r.text


def test_no_plain_comparison_of_the_approver_code():
    plain = re.compile(r"(==|!=)\s*config\.APPROVER_CODE|config\.APPROVER_CODE\s*(==|!=)")
    hits = [f"{p.name}:{i}" for p in SOUS.glob("*.py")
            for i, line in enumerate(p.read_text().splitlines(), 1) if plain.search(line)]
    assert hits == []
