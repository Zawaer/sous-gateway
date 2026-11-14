"""Peppol BIS Ordering 3.0 documents for an order: the UBL 2.1 Order and the Order Response.

The path to production: a buyer's procurement or ERP system that speaks Peppol (the EU's e-procurement network,
mandatory for public buyers in many countries) can take the gateway's decision as a standard document instead of
our JSON. GET /orders/{id}/peppol/order.xml is the order as the buyer's agent placed it (lines from the signed
quote); GET /orders/{id}/peppol/order-response.xml is Valio Aimo's answer, with the Peppol response code taken from
the gateway's decision:

  ordered                    -> AP  accepted
  pending_approval           -> AB  message acknowledged (held for the buyer's human; no commitment yet)
  refused / rejected /       -> RE  rejected, with the rules that fired as the reason
  expired / expired_cutoff

Passes the UBL 2.1 XSD and OpenPeppol's BIS schematron (T01 / T76) in CI (scripts/peppol_validate.py,
tests/test_peppol_official.py); not exchanged over a Peppol access point; no Peppol participant ids are registered
for the demo parties (the endpoint ids use scheme 0216, Finnish OVT, with placeholder values, marked as such).
Items are identified by Valio Aimo's product id (SellersItemIdentification); the catalog file has no GTINs.
"""

from __future__ import annotations

from datetime import datetime
from xml.etree import ElementTree as ET

from . import config

CUSTOMIZATION_ORDER = "urn:fdc:peppol.eu:poacc:trns:order:3"
CUSTOMIZATION_RESPONSE = "urn:fdc:peppol.eu:poacc:trns:order_response:3"
PROFILE = "urn:fdc:peppol.eu:poacc:bis:ordering:3"
NS = {"cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
      "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"}
ORDER_NS = "urn:oasis:names:specification:ubl:schema:xsd:Order-2"
RESPONSE_NS = "urn:oasis:names:specification:ubl:schema:xsd:OrderResponse-2"
SELLER = {"name": "Valio Aimo Oy", "endpoint": "0216:003700000000-DEMO"}  # placeholder OVT id, not registered
# Valio sales units -> UN/ECE Recommendation 20 codes (Peppol unitCode); anything else counts as pieces.
UNIT = {"ST": "EA", "KPL": "EA", "KG": "KGM", "L": "LTR", "KI": "CS", "PAK": "PK", "PRK": "CT", "PSS": "BG",
        "BOT": "BO", "PLO": "BO", "RAS": "CA", "SK": "CS"}
RESPONSE_CODE = {"ordered": "AP", "pending_approval": "AB"}

for _p, _u in NS.items():
    ET.register_namespace(_p, _u)


def _el(parent, tag: str, text=None, **attrs):
    p, t = tag.split(":")
    e = ET.SubElement(parent, f"{{{NS[p]}}}{t}", {k: str(v) for k, v in attrs.items() if v is not None})
    if text is not None:
        e.text = str(text)
    return e


def _amount(parent, tag: str, value) -> None:
    _el(parent, tag, f"{float(value or 0):.2f}", currencyID="EUR")


def _party(parent, wrapper: str, name: str, endpoint: str, response: bool = False) -> None:
    """Peppol T01 needs PartyLegalEntity/RegistrationName; T76 (Order Response) has no PartyName."""
    party = _el(_el(parent, wrapper), "cac:Party")
    scheme, value = endpoint.split(":", 1)
    _el(party, "cbc:EndpointID", value, schemeID=scheme)
    if not response:
        _el(_el(party, "cac:PartyName"), "cbc:Name", name)
        _el(_el(_el(party, "cac:PostalAddress"), "cac:Country"), "cbc:IdentificationCode", "FI")
    _el(_el(party, "cac:PartyLegalEntity"), "cbc:RegistrationName", name)


def _buyer_endpoint(o: dict) -> str:
    return f"0216:DEMO-{(o.get('mandate_id') or 'BUYER')[:40]}"  # placeholder, not a registered participant


def _head(root, customization: str, doc_id: str, at: str) -> None:
    dt = datetime.fromisoformat(at)
    _el(root, "cbc:CustomizationID", customization)
    _el(root, "cbc:ProfileID", PROFILE)
    _el(root, "cbc:ID", doc_id)
    _el(root, "cbc:IssueDate", dt.date().isoformat())
    _el(root, "cbc:IssueTime", dt.astimezone(config.TZ).strftime("%H:%M:%S"))


def _xml(root, default_ns: str) -> bytes:
    ET.indent(root)
    ET.register_namespace("", default_ns)  # <Order xmlns="...">, not <ns0:Order>
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def order_xml(o: dict) -> bytes:
    root = ET.Element(f"{{{ORDER_NS}}}Order")
    _head(root, CUSTOMIZATION_ORDER, o["id"], o["created_at"])
    note = f"Placed by agent {o.get('agent_id')} under mandate {o.get('mandate_id')}; quote {o.get('quote_id')}."
    if o.get("agent_rationale"):
        note += f" Agent's stated reason: {o['agent_rationale']}"
    _el(root, "cbc:Note", note)
    _el(root, "cbc:DocumentCurrencyCode", "EUR")
    _el(_el(root, "cac:QuotationDocumentReference"), "cbc:ID", o.get("quote_id"))
    _party(root, "cac:BuyerCustomerParty", o.get("buyer") or "Buyer", _buyer_endpoint(o))
    _party(root, "cac:SellerSupplierParty", SELLER["name"], SELLER["endpoint"])
    period = _el(_el(root, "cac:Delivery"), "cac:RequestedDeliveryPeriod")
    _el(period, "cbc:StartDate", o.get("requested_delivery_date") or o.get("delivery_date"))
    _el(period, "cbc:EndDate", o.get("delivery_date") or o.get("requested_delivery_date"))
    total = _el(root, "cac:AnticipatedMonetaryTotal")
    _amount(total, "cbc:LineExtensionAmount", o.get("total_ex_vat"))
    _amount(total, "cbc:PayableAmount", o.get("total_inc_vat") or o.get("total_ex_vat"))
    for n, l in enumerate(o.get("lines") or [], 1):
        item = _el(_el(root, "cac:OrderLine"), "cac:LineItem")
        _el(item, "cbc:ID", n)
        _el(item, "cbc:Quantity", f"{float(l.get('quantity') or 0):g}", unitCode=UNIT.get(l.get("sales_unit") or "", "EA"))
        _amount(item, "cbc:LineExtensionAmount", l.get("line_total_ex_vat"))
        if l.get("delivery_date"):
            _el(_el(_el(item, "cac:Delivery"), "cac:RequestedDeliveryPeriod"), "cbc:StartDate", l["delivery_date"])
        _amount(_el(item, "cac:Price"), "cbc:PriceAmount", l.get("unit_price_ex_vat"))
        it = _el(item, "cac:Item")
        _el(it, "cbc:Name", l.get("name") or l.get("product_id"))
        _el(_el(it, "cac:SellersItemIdentification"), "cbc:ID", l.get("product_id"))
    return _xml(root, ORDER_NS)


def response_code(o: dict) -> str:
    return RESPONSE_CODE.get(o.get("status"), "RE")


def order_response_xml(o: dict) -> bytes:
    root = ET.Element(f"{{{RESPONSE_NS}}}OrderResponse")
    at = o.get("updated_at") or o["created_at"]
    _head(root, CUSTOMIZATION_RESPONSE, f"{o['id']}-R", at)
    code = response_code(o)
    _el(root, "cbc:OrderResponseCode", code)
    d = o.get("decision") or {}
    fired = [f for f in d.get("findings") or [] if f.get("outcome") != "silent"]
    note = {"AP": "Accepted by the mandate engine" + (" after a human's approval" if o.get("approval") else ""),
            "AB": "Received; held for the buyer's human to approve",
            "RE": f"Not accepted ({o.get('status')})"}[code]
    if fired:
        note += ": " + "; ".join(f"{f['rule']}: {f['message']}" for f in fired[:5])
    elif (o.get("approval") or {}).get("result_reason"):
        note += f": {o['approval']['result_reason']}"
    _el(root, "cbc:Note", note[:1000])
    _el(root, "cbc:DocumentCurrencyCode", "EUR")
    _el(_el(root, "cac:OrderReference"), "cbc:ID", o["id"])
    _party(root, "cac:SellerSupplierParty", SELLER["name"], SELLER["endpoint"], True)
    _party(root, "cac:BuyerCustomerParty", o.get("buyer") or "Buyer", _buyer_endpoint(o), True)
    # No order lines: Peppol T76-R008 forbids them on AP (accepted as is), and AB / RE need none.
    return _xml(root, RESPONSE_NS)
