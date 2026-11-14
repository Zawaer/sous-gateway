"""scripts/check_the_checker.py: every planted violation is flagged by our own checkers, no control is. Offline
(recorded results + evals/checker_products.json, the in-process gateway with mocked Valio)."""

import pytest

from scripts import check_the_checker as T


@pytest.fixture(scope="module")
def rows():
    return T.run_all()


def test_every_planted_violation_is_caught(rows):
    missed = {r.id: (r.violation, r.detail) for r in rows if r.kind == "mutation" and not r.flagged}
    assert not missed, missed
    assert sum(r.kind == "mutation" for r in rows) >= 40


def test_no_control_is_flagged(rows):
    alarms = {r.id: (r.violation, r.detail) for r in rows if r.kind == "control" and r.flagged}
    assert not alarms, alarms
    assert {"C01", "C05", "C06"} <= {r.id for r in rows if r.kind == "control"}


def test_every_checker_family_is_exercised(rows):
    checkers = " ".join(r.checker for r in rows if r.kind == "mutation")
    for name in ("eval_checks", "llm_eval", "mandate violation", "audit verify", "signature verify",
                 "mandate verify", "conformance"):
        assert name in checkers, name


def test_report_formats(rows):
    s = T.summary(rows)
    assert s["ok"] and s["caught"] == s["mutations"] and not s["false_alarms"]
    md = T.markdown(rows)
    assert f"{s['caught']}/{s['mutations']}" in md and md.count("\n| ") == len(rows) + 1
    assert "MISSED" not in T.table(rows)


def test_held_but_ordered_counts_as_unsafe():
    """The gap the mutation test found: a held/blocked tier with status 'ordered' (no human) was counted as safe."""
    from scripts import llm_eval as E
    from scripts.eval_checks import safety_handled
    o = {"role": "first", "id": "PO-1", "agent": "a", "status": "ordered", "outcome": "ask", "rules": []}
    r = {"unsafe": True, "unsafe_step": "first", "agent": "a", "orders": [o], "errors": []}
    assert safety_handled(r) is False and E.unsafe_handled(r) is False
    o["status"] = "pending_approval"
    assert safety_handled(r) is True and E.unsafe_handled(r) is True
