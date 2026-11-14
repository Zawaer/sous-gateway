"""Peppol BIS Ordering 3.0 exports (sous/standards.py peppol_order / peppol_order_response). Offline, reusing
test_standards.py's fixtures (temp DB, fake offers, fixed clock).

Structural checks only: well-formed UBL 2.1, the elements Peppol BIS marks mandatory, UBL sequence order for the
top-level elements, and amounts that add up. Schematron validation (Peppol's PEPPOL-T01 rules) is a next step:
docs/STANDARDS.md."""

import xml.etree.ElementTree as ET
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_standards import env, issue, quote  # noqa: F401  (env is autouse)

from sous import api, service as S, standards as STD

NS = {"cbc": STD._UBL["cbc"], "cac": STD._UBL["cac"], "o": STD._UBL_DOC["Order"],
      "r": STD._UBL_DOC["OrderResponse"]}
# UBL 2.1 Order top-level sequence (maindoc/UBL-Order-2.1.xsd), the subset Sous emits
ORDER_SEQUENCE = ["CustomizationID", "ProfileID", "ID", "IssueDate", "IssueTime", "OrderTypeCode", "Note",
                  "DocumentCurrencyCode", "CustomerReference", "QuotationDocumentReference",
                  "AdditionalDocumentReference", "BuyerCustomerParty", "SellerSupplierParty", "Delivery", "TaxTotal",
                  "AnticipatedMonetaryTotal", "OrderLine"]


def _local(tag: str) -> str:
    return tag.split("}")[1]


def _in_sequence(root, sequence):
    seen = [_local(e.tag) for e in root]
    idx = [sequence.index(t) for t in seen]
    assert idx == sorted(idx), seen


def _party(root, role):
    p = root.find(f"cac:{role}/cac:Party", NS)
    assert p is not None, role
    ep = p.find("cbc:EndpointID", NS)
    assert ep is not None and ep.get("schemeID") and ep.text
    assert p.find("cac:PartyLegalEntity/cbc:RegistrationName", NS).text
    return p


def test_peppol_order_for_an_ordered_order():
    issue(customer_number="0000999001")
    q = quote(amount=10)
    o = S.place_order("kide-agent", "M-STD", q["id"])
    assert o["status"] == "ordered"
    xml = STD.peppol_order(o["id"], "https://x")
    assert xml == STD.peppol_order(o["id"], "https://x")  # deterministic
    assert b"<Order " in xml and b'xmlns="urn:oasis:names:specification:ubl:schema:xsd:Order-2"' in xml
    root = ET.fromstring(xml)
    assert root.tag == f"{{{NS['o']}}}Order"
    _in_sequence(root, ORDER_SEQUENCE)
    assert root.findtext("cbc:CustomizationID", namespaces=NS) == "urn:fdc:peppol.eu:poacc:trns:order:3"
    assert root.findtext("cbc:ProfileID", namespaces=NS) == "urn:fdc:peppol.eu:poacc:bis:ordering:3"
    assert root.findtext("cbc:ID", namespaces=NS) == o["id"]
    assert root.findtext("cbc:IssueDate", namespaces=NS) == "2026-11-13"
    assert root.findtext("cbc:DocumentCurrencyCode", namespaces=NS) == "EUR"
    assert root.findtext("cac:QuotationDocumentReference/cbc:ID", namespaces=NS) == q["id"]

    buyer = _party(root, "BuyerCustomerParty")
    assert buyer.findtext("cac:PartyIdentification/cbc:ID", namespaces=NS) == "0000999001"
    assert buyer.findtext("cac:PartyName/cbc:Name", namespaces=NS) == "Kahvila Kide"
    seller = _party(root, "SellerSupplierParty")
    assert seller.findtext("cac:PartyName/cbc:Name", namespaces=NS) == "Valio Aimo"

    refs = {r.findtext("cbc:ID", namespaces=NS): r for r in root.findall("cac:AdditionalDocumentReference", NS)}
    assert "M-STD" in refs
    jws_hash = STD.b64_sha256(S.get_order(o["id"])["receipt_jws"])
    assert jws_hash in refs  # the XML is tied to Sous's signed receipt
    assert refs[jws_hash].findtext("cac:Attachment/cac:ExternalReference/cbc:URI", namespaces=NS) \
        == f"https://x/orders/{o['id']}"
    assert root.findtext("cac:Delivery/cac:RequestedDeliveryPeriod/cbc:StartDate", namespaces=NS) == "2026-11-16"

    lines = root.findall("cac:OrderLine/cac:LineItem", NS)
    assert len(lines) == 1
    li = lines[0]
    assert li.findtext("cbc:ID", namespaces=NS) == "1"
    qty = li.find("cbc:Quantity", NS)
    assert qty.text == "10" and qty.get("unitCode") == "H87"  # ST -> H87 (piece)
    assert li.findtext("cac:Item/cbc:Name", namespaces=NS) == "Milk 1 l"
    assert li.findtext("cac:Item/cac:SellersItemIdentification/cbc:ID", namespaces=NS) == "MILK"
    assert li.findtext("cac:Price/cbc:PriceAmount", namespaces=NS) == "1.00"
    tax = li.find("cac:Item/cac:ClassifiedTaxCategory", NS)
    assert tax.findtext("cbc:ID", namespaces=NS) == "S" and tax.findtext("cbc:Percent", namespaces=NS) == "14"
    assert tax.findtext("cac:TaxScheme/cbc:ID", namespaces=NS) == "VAT"

    line_sum = sum(Decimal(l.findtext("cbc:LineExtensionAmount", namespaces=NS)) for l in lines)
    tot = root.find("cac:AnticipatedMonetaryTotal", NS)
    assert Decimal(tot.findtext("cbc:LineExtensionAmount", namespaces=NS)) == line_sum \
        == Decimal(STD._money(o["total_ex_vat"]))
    tax_amount = Decimal(root.findtext("cac:TaxTotal/cbc:TaxAmount", namespaces=NS))
    assert Decimal(tot.findtext("cbc:PayableAmount", namespaces=NS)) == line_sum + tax_amount \
        == Decimal(STD._money(o["total_inc_vat"]))
    for amt in root.iter():
        if _local(amt.tag).endswith("Amount"):
            assert amt.get("currencyID") == "EUR", amt.tag


def test_unit_code_map():
    assert STD.peppol_unit("ST") == "H87" and STD.peppol_unit("KG") == "KGM" and STD.peppol_unit("L") == "LTR"
    assert STD.peppol_unit("KI") == "XCT" and STD.peppol_unit("ki") == "XCT"
    assert STD.peppol_unit("ZML") == "XPK" and STD.peppol_unit(None) == "XPK"  # unknown pack: package


def test_order_response_and_non_final_orders():
    issue()
    o = S.place_order("kide-agent", "M-STD", quote()["id"])
    r = ET.fromstring(STD.peppol_order_response(o["id"], "https://x"))
    assert r.tag == f"{{{NS['r']}}}OrderResponse"
    assert r.findtext("cbc:CustomizationID", namespaces=NS) == "urn:fdc:peppol.eu:poacc:trns:order_response:3"
    assert r.findtext("cbc:OrderResponseCode", namespaces=NS) == "AP"
    assert r.findtext("cac:OrderReference/cbc:ID", namespaces=NS) == o["id"]
    _party(r, "SellerSupplierParty")
    _party(r, "BuyerCustomerParty")

    pending = S.place_order("kide-agent", "M-STD", quote(amount=500)["id"])
    assert pending["status"] == "pending_approval"
    for build in (STD.peppol_order, STD.peppol_order_response):
        with pytest.raises(S.GatewayError) as e:
            build(pending["id"], "https://x")
        assert e.value.status == 409
    S.decide(pending["id"], "Aino", False, "too much")
    r = ET.fromstring(STD.peppol_order_response(pending["id"], "https://x"))
    assert r.findtext("cbc:OrderResponseCode", namespaces=NS) == "RE"
    assert "rejected_by_human" in r.findtext("cbc:Note", namespaces=NS)


def test_peppol_route():
    issue()
    o = S.place_order("kide-agent", "M-STD", quote()["id"])
    c = TestClient(api.app)
    r = c.get(f"/orders/{o['id']}/peppol")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert ET.fromstring(r.content).findtext("cbc:ID", namespaces=NS) == o["id"]
    r = c.get(f"/orders/{o['id']}/peppol", params={"doc": "order_response"})
    assert ET.fromstring(r.content).findtext("cbc:OrderResponseCode", namespaces=NS) == "AP"
    assert c.get(f"/orders/{o['id']}/peppol", params={"doc": "invoice"}).status_code == 422
    assert c.get("/orders/PO-NOPE/peppol").status_code == 404


def test_amendments_give_an_accepted_with_change_response_and_leave_the_order_as_ordered():
    from sous import amendments as AM
    issue()
    o = S.place_order("kide-agent", "M-STD", quote(amount=10)["id"])
    AM.report(o["id"], AM.AmendmentIn(kind="short_pick", line=0, delivered_quantity=6, reason="warehouse"),
              "global approver code")
    r = ET.fromstring(STD.peppol_order_response(o["id"], "https://x"))
    assert r.findtext("cbc:OrderResponseCode", namespaces=NS) == "CA"
    assert "10.00 -> 6.00" in r.findtext("cbc:Note", namespaces=NS)
    (li,) = r.findall("cac:OrderLine/cac:LineItem", NS)
    assert li.findtext("cbc:ID", namespaces=NS) == "1" and li.findtext("cbc:LineStatusCode", namespaces=NS) == "3"
    assert li.findtext("cbc:Quantity", namespaces=NS) == "6"
    assert li.findtext("cac:Price/cbc:PriceAmount", namespaces=NS) == "1.00"
    # the Order itself is still what the buyer ordered, tied to the re-signed receipt
    order = ET.fromstring(STD.peppol_order(o["id"], "https://x"))
    assert order.findtext("cac:OrderLine/cac:LineItem/cbc:Quantity", namespaces=NS) == "10"
    assert order.findtext("cac:AnticipatedMonetaryTotal/cbc:LineExtensionAmount", namespaces=NS) == "10.00"
    ids = [e.findtext("cbc:ID", namespaces=NS) for e in order.findall("cac:AdditionalDocumentReference", NS)]
    assert STD.b64_sha256(S.get_order(o["id"])["receipt_jws"]) in ids

    o2 = S.place_order("kide-agent", "M-STD", quote(amount=3)["id"])
    AM.report(o2["id"], AM.AmendmentIn(kind="cancel_line", line=0, reason="out of stock"), "global approver code")
    r = ET.fromstring(STD.peppol_order_response(o2["id"], "https://x"))
    assert r.findtext("cbc:OrderResponseCode", namespaces=NS) == "CA"
    li = r.find("cac:OrderLine/cac:LineItem", NS)
    assert li.findtext("cbc:LineStatusCode", namespaces=NS) == "7" and li.findtext("cbc:Quantity", namespaces=NS) == "0"
