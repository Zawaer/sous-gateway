"""Sous's Peppol documents against the official tooling: UBL 2.1 XSD + OpenPeppol's BIS schematron (T01 Order,
T76 Order Response), via scripts/peppol_validate.py. The same orders as tests/test_peppol.py (offline fixtures).

Needs lxml + saxonche and the pinned artifacts (`python -m scripts.peppol_validate fetch`). Skipped when they are
missing, except with SOUS_PEPPOL_VALIDATION=1 (CI's peppol job), where a skip would hide a failure.
"""

import os

import pytest
from test_standards import env, issue, quote  # noqa: F401  (env is autouse)

from sous import service as S, standards as STD

REQUIRED = os.environ.get("SOUS_PEPPOL_VALIDATION") == "1"
try:
    import lxml  # noqa: F401
    import saxonche  # noqa: F401
    from scripts import peppol_validate as PV
    READY = all((PV.DIR / n).exists() for n in PV.ARTIFACTS)
except ImportError:
    READY = False
if not READY:
    if REQUIRED:
        raise RuntimeError("SOUS_PEPPOL_VALIDATION=1 but lxml/saxonche or the Peppol artifacts are missing")
    pytest.skip("Peppol validation tooling not installed (python -m scripts.peppol_validate fetch)",
                allow_module_level=True)


def assert_valid(xml: bytes):
    r = PV.validate(xml)
    assert not r["xsd_errors"], r["xsd_errors"]
    assert not r["fatal"], [f"{f['id']}: {f['text']} @ {f['location'].split('}')[-2:]}" for f in r["fatal"]]
    return r


def test_validator_passes_official_examples_and_catches_a_broken_one():
    assert PV.selftest()


def test_order_is_valid_peppol():
    issue(customer_number="0000999001")
    o = S.place_order("kide-agent", "M-STD", quote(amount=10)["id"])
    assert assert_valid(STD.peppol_order(o["id"], "https://x"))["kind"] == "Order"


def test_order_responses_ap_re_ca_are_valid_peppol():
    from sous import amendments as AM
    issue()
    o = S.place_order("kide-agent", "M-STD", quote()["id"])
    assert_valid(STD.peppol_order_response(o["id"], "https://x"))  # AP
    pending = S.place_order("kide-agent", "M-STD", quote(amount=500)["id"])
    S.decide(pending["id"], "Aino", False, "too much")
    assert_valid(STD.peppol_order_response(pending["id"], "https://x"))  # RE
    o2 = S.place_order("kide-agent", "M-STD", quote(amount=10)["id"])
    AM.report(o2["id"], AM.AmendmentIn(kind="short_pick", line=0, delivered_quantity=6, reason="warehouse"),
              "global approver code")
    assert_valid(STD.peppol_order_response(o2["id"], "https://x"))  # CA, changed line
    o3 = S.place_order("kide-agent", "M-STD", quote(amount=3)["id"])
    AM.report(o3["id"], AM.AmendmentIn(kind="cancel_line", line=0, reason="out of stock"), "global approver code")
    assert_valid(STD.peppol_order_response(o3["id"], "https://x"))  # CA, line not accepted


def test_the_peppol_xml_routes_built_by_sous_peppol_are_valid_too():
    from sous import peppol
    issue()
    o = S.get_order(S.place_order("kide-agent", "M-STD", quote(amount=10)["id"])["id"])
    assert_valid(peppol.order_xml(o))
    assert_valid(peppol.order_response_xml(o))  # AP
    held = S.get_order(S.place_order("kide-agent", "M-STD", quote(amount=500)["id"])["id"])
    assert held["status"] == "pending_approval"
    assert_valid(peppol.order_response_xml(held))  # AB
