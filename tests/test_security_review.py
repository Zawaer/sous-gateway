"""Fixes from the read-only security review of the overnight trust changes (Codex run 01a1d86a).

Offline, like test_read_scopes.py (test_sandbox.py's fixtures): no data/, Valio Aimo mocked.
Stock sizing of anonymous quotes is tested in test_exposure.py.
"""

import pathlib
import re

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


def test_no_plain_comparison_of_the_approver_code():
    plain = re.compile(r"(==|!=)\s*config\.APPROVER_CODE|config\.APPROVER_CODE\s*(==|!=)")
    hits = [f"{p.name}:{i}" for p in SOUS.glob("*.py")
            for i, line in enumerate(p.read_text().splitlines(), 1) if plain.search(line)]
    assert hits == []
