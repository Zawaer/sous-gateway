"""Open-standard views of Sous's native objects, plus discovery documents. See docs/STANDARDS.md.

Sous's own signed formats (sous-mandate+jws, sous-quote+jws, sous-order+jws, buyer vc+jwt) stay the source of
truth: every export here is built from the *signature-verified* payload, never from the stored copy, and is
deterministic (same object, same key -> byte-identical output; Ed25519 signatures are deterministic).

Spec versions targeted (checked 2026-11-13):
- AP2, Agent Payments Protocol v0.2.0 (google-agentic-commerce/AP2, released 2026-04-28; JSON Schemas in
  code/sdk/schemas/ap2 at commit e1ea56d). v0.2 renamed v0.1's IntentMandate / CartMandate / PaymentMandate to
  open/closed Checkout and Payment Mandates (SD-JWT VCs, `vct` claims). We emit the v0.2 claim sets and, for
  readers who know v0.1, a v0.1-shaped IntentMandate / CartMandate alongside.
- UCP Checkout object (dev.ucp.shopping.checkout 2026-04-08), which AP2 v0.2's closed checkout mandate signs.
- A2A Agent Card, A2A protocol v1.0 (a2aproject/A2A v1.0.1, 2026-05-28; specification/a2a.proto), JSON field names.
- MCP Server Card, SEP-2127 / modelcontextprotocol/experimental-ext-server-card, schema v1.
- schema.org (Offer, Product, TypeAndQuantityNode, UnitPriceSpecification, Order, OrderItem).
- W3C VC Data Model 2.0 secured with JOSE (vc+jwt), did:key; JSON Schema 2020-12.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import credentials, service as S, signing, store
from .mandate import Finding, Mandate

AP2_VERSION = "0.2.0"
AP2_SCHEMAS = "https://github.com/google-agentic-commerce/AP2/tree/e1ea56d/code/sdk/schemas/ap2"
UCP_CHECKOUT_VERSION = "dev.ucp.shopping.checkout 2026-04-08"
A2A_PROTOCOL_VERSION = "1.0"
MCP_SERVER_CARD_SCHEMA = "https://static.modelcontextprotocol.io/schemas/v1/server-card.schema.json"
REPO = "https://github.com/Zawaer/sous"
MERCHANT = {"id": "valio-aimo", "name": "Valio Aimo", "website": "https://www.valioaimo.fi"}
AGENT_URN = credentials.AGENT_URN


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64_sha256(data: str | bytes) -> str:
    """base64url(SHA-256), the digest AP2 uses for checkout_hash and mandate references."""
    return _b64(hashlib.sha256(data.encode() if isinstance(data, str) else data).digest())


def _cents(eur: float | None) -> int:
    return int(round((eur or 0) * 100))


def _epoch(iso: str | None) -> int | None:
    return int(datetime.fromisoformat(iso).timestamp()) if iso else None


# --- mandate -> AP2 open mandates -------------------------------------------

def _describe(m: Mandate) -> str:
    """A deterministic plain-language summary (AP2 v0.1 IntentMandate.natural_language_description)."""
    parts = [f"Buy from Valio Aimo for {m.buyer} as agent {m.agent_id}"]
    parts.append(f"up to EUR {m.auto_approve_limit_eur:,.2f} ex VAT per order without a human")
    if m.hard_limit_eur is not None:
        parts.append(f"never above EUR {m.hard_limit_eur:,.2f}")
    if m.daily_limit_eur is not None:
        parts.append(f"at most EUR {m.daily_limit_eur:,.2f} a day without a human")
    if m.avoid_allergens:
        parts.append("avoiding allergens " + ", ".join(m.avoid_allergens)
                     + ("" if m.allow_may_contain else " (incl. may-contain)"))
    if m.required_claims:
        parts.append("only products claiming " + ", ".join(m.required_claims))
    if m.allowed_categories is not None:
        parts.append("only categories " + ", ".join(m.allowed_categories))
    return "; ".join(parts) + "."


def _sous_constraints(m: Mandate) -> dict:
    """Mandate rules AP2 v0.2 has no constraint type for (carried as an extension claim)."""
    return {k: v for k, v in m.model_dump(mode="json").items()
            if k in ("auto_approve_limit_eur", "notify_above_eur", "daily_limit_eur", "allowed_categories",
                     "avoid_allergens", "allow_may_contain", "unknown_allergens", "required_claims", "substitutions",
                     "terminal_products", "min_delivery_reliability", "later_delivery", "max_comparison_price_eur",
                     "require_agent_auth")}


def _issued_at(mandate_id: str) -> str | None:
    """When the current version of a gateway-issued mandate was signed, from the audit log (deterministic)."""
    for e in store.audit_events(mandate_id, limit=50):
        if e.get("subject") == mandate_id and e.get("event") in ("mandate.issued", "mandate.updated"):
            return datetime.fromisoformat(str(e["ts"]).replace("Z", "+00:00")).isoformat(timespec="seconds")
    return None


def ap2_mandate(mandate_id: str) -> dict:
    """The mandate as AP2 v0.2 open checkout + open payment mandate claim sets (unsigned), a v0.1-style
    IntentMandate, and a W3C VC 2.0 envelope. The authorisation evidence is the native signatures."""
    payload = S._signed_mandate(mandate_id)  # verifies the gateway signature (and the buyer's VC, if any)
    m = Mandate(**payload)
    native = S.get_mandate(mandate_id)
    vc_jwt = native.get("credential")
    issued = credentials.parse_vc(vc_jwt)["vc"].get("validFrom") if vc_jwt else _issued_at(mandate_id)
    vc_iat = _epoch(issued)
    exp = int(m.valid_until.timestamp()) if m.valid_until else None
    cnf = {"kid": AGENT_URN + m.agent_id}  # RFC 7800 key id; Sous agents hold bearer tokens, not keys (gap)
    times = {k: v for k, v in (("iat", vc_iat), ("exp", exp)) if v is not None}

    open_checkout = {
        "vct": "mandate.checkout.open.1",
        "constraints": [{"type": "checkout.allowed_merchants", "allowed": [MERCHANT]}],
        "cnf": cnf, **times,
        "sous_constraints": _sous_constraints(m),
    }
    pay_constraints: list[dict] = [
        {"type": "payment.reference", "conditional_transaction_id": b64_sha256(signing.canonical(open_checkout))},
        {"type": "payment.allowed_payees", "allowed": [MERCHANT]},
    ]
    if m.hard_limit_eur is not None:
        pay_constraints.append({"type": "payment.amount_range", "currency": "EUR", "max": _cents(m.hard_limit_eur)})
    if m.valid_until:
        pay_constraints.append({"type": "payment.execution_date", "not_after": m.valid_until.date().isoformat()})
    open_payment = {"vct": "mandate.payment.open.1", "constraints": pay_constraints, "cnf": cnf, **times}

    issuer = payload.get("issuer") or {"did": credentials.gateway_did(), "kind": "gateway"}
    if vc_jwt:
        vc_doc, vc_out = credentials.parse_vc(vc_jwt)["vc"], vc_jwt
    else:  # gateway-issued: the gateway is the issuer of record, so it issues the VC view itself
        vc_doc = {"@context": [credentials.VC_CONTEXT], "id": f"urn:sous:mandate:{m.id}",
                  # Not SousMandateCredential: this view must never be accepted by POST /mandates/credential.
                  "type": ["VerifiableCredential", "SousGatewayMandateAttestation"],
                  "issuer": {"id": credentials.gateway_did()},  # the gateway, signing for the buyer
                  "validFrom": issued or "1970-01-01T00:00:00+00:00",
                  "credentialSubject": {"id": AGENT_URN + m.agent_id, "mandate": m.model_dump(mode="json")}}
        if m.valid_until:
            vc_doc["validUntil"] = m.valid_until.isoformat(timespec="seconds")
        vc_out = credentials.jws_sign(vc_doc, signing._key(), credentials.VC_TYP, {"cty": "vc"})
    return {
        "profile": f"AP2 v{AP2_VERSION} open mandates (unsigned SD-JWT VC claim sets)",
        "spec": {"ap2": AP2_VERSION, "schemas": AP2_SCHEMAS, "vc": "W3C VC Data Model 2.0 (vc+jwt)"},
        "mandate_id": m.id,
        "open_checkout_mandate": open_checkout,
        "open_payment_mandate": open_payment,
        "ap2_v0_1_intent_mandate": {
            "user_cart_confirmation_required": False,  # Sous decides per order: silent / notify / ask / block
            "natural_language_description": _describe(m),
            "merchants": [MERCHANT["name"]],
            "skus": None,
            "requires_refundability": False,
            "intent_expiry": m.valid_until.isoformat() if m.valid_until else None,
        },
        "verifiable_credential": {"issuer_kind": issuer.get("kind"), "vc": vc_doc, "vc_jwt": vc_out},
        "authorization": {
            "native_jws": native["jws"], "buyer_credential": vc_jwt, "issuer": issuer,
            "note": "AP2 mandates are SD-JWTs the user signs with key binding (cnf) to the agent's key. Sous's "
                    "authority is the gateway-signed mandate (sous-mandate+jws), and for buyer-signed mandates the "
                    "buyer's vc+jwt; the claim sets above are views of that, not separately signed by the buyer.",
        },
        "conformance": {
            "open_payment_mandate": "valid against AP2 v0.2.0 open_payment_mandate.json",
            "open_checkout_mandate": "partial: AP2 requires a checkout.line_items constraint enumerating acceptable "
                                     "items; a Sous mandate admits any product that passes its rules "
                                     "(allergens, claims, categories, limits), carried in sous_constraints",
            "amounts": "AP2 payment amounts are what is paid (incl. VAT); Sous limits are ex VAT, so amount_range.max "
                       "is the ex-VAT hard limit and the gateway, not a payment processor, enforces it",
        },
    }


# --- quote -> UCP checkout signed by the merchant (AP2 v0.2 closed checkout mandate input) ---------------

_UNIT_CODES = {"KG": "KGM", "L": "LTR"}  # UN/CEFACT Rec. 20; anything else is a piece (H87)


def _signed_quote(quote_id: str) -> tuple[dict, str]:
    row = store.get_quote(quote_id)
    if not row:
        raise S.GatewayError("quote_not_found", f"No quote {quote_id}.", 404)
    q = signing.verify(row["_jws"], "sous-quote+jws")
    if q is None or q.get("id") != quote_id:
        raise S.GatewayError("quote_signature", "Quote signature does not verify.", 409)
    return q, row["_jws"]


def _checkout_status(q: dict) -> str:
    outcome = (q.get("mandate_preview") or {}).get("outcome")
    if outcome == "block" or q.get("uncovered") or not all(l.get("orderable") for l in q["lines"]) or not q["lines"]:
        return "incomplete"
    return "requires_escalation" if outcome == "ask" else "ready_for_complete"


def _issue_messages(l: dict, path: str) -> list[dict]:
    out = []
    for issue in l.get("issues", []):
        msg = {"type": "error" if issue["severity"] == "blocking" else "warning", "code": issue["code"],
               "path": path, "content": issue["message"]}
        if msg["type"] == "error":
            msg["severity"] = "requires_buyer_input"
        out.append(msg)
    return out


def _line_amounts(l: dict) -> dict:
    """Cents for one quote line: unit price incl. VAT (or the lot price for weighed goods), UCP quantity, totals."""
    qty = l.get("quantity")
    whole = qty is not None and float(qty).is_integer() and qty >= 1
    unit_inc = l.get("unit_price_inc_vat") if whole else l.get("line_total_inc_vat")
    ex, inc = _cents(l.get("line_total_ex_vat")), _cents(l.get("line_total_inc_vat"))
    return {"price": _cents(unit_inc), "quantity": int(qty) if whole else 1,  # UCP quantities are whole
            "totals": [{"type": "subtotal", "amount": ex}, {"type": "tax", "amount": inc - ex},
                       {"type": "total", "amount": inc}]}


def ucp_checkout(q: dict, base_url: str) -> dict:
    """A UCP Checkout object for a verified quote payload. Prices in minor units (cents), VAT as `tax`.

    Only orderable lines are chargeable `line_items`, so their totals sum to the checkout totals (which, like the
    native quote's, count orderable lines only). Lines that can't be ordered (cut-off passed, out of stock, ...)
    go to the extension `sous.unavailable_lines` with their price marked indicative."""
    lines, messages, sous_lines, unavailable = [], [], [], []
    for i, l in enumerate(q["lines"]):
        lid = f"L{i + 1}"
        a = _line_amounts(l)
        sous_lines.append({"line_id": lid, "quantity": l.get("quantity"), "sales_unit": l.get("sales_unit"),
                           "delivery_date": l.get("delivery_date"), "order_cutoff": l.get("order_cutoff"),
                           "orderable": l.get("orderable")})
        if l.get("orderable"):
            messages += _issue_messages(l, f"$.line_items[{len(lines)}]")
            lines.append({"id": lid, "item": {"id": l["product_id"], "title": l["name"], "price": a["price"]},
                          "quantity": a["quantity"], "totals": a["totals"]})
        else:
            messages += _issue_messages(l, f"$.sous.unavailable_lines[{len(unavailable)}]")
            unavailable.append({"id": lid, "item": {"id": l["product_id"], "title": l["name"]},
                                "quantity": a["quantity"], "reasons": [x["code"] for x in l.get("issues", [])],
                                "indicative": {"price": a["price"], "totals": a["totals"],
                                               "note": "Not orderable: not charged, not in the checkout totals"}})
    for g in q.get("uncovered") or []:
        messages.append({"type": "error", "code": "NEED_UNCOVERED", "severity": "requires_buyer_input",
                         "content": f"{g['uncovered']:g} {g.get('unit') or 'units'} of item {g.get('item_index')} "
                                    "could not be covered."})
    preview = q.get("mandate_preview") or {}
    if preview.get("outcome") in ("ask", "block"):
        for f in preview.get("findings", []):
            if f["outcome"] in ("ask", "block"):
                messages.append({"type": "error" if f["outcome"] == "block" else "warning",
                                 "code": f"mandate.{f['rule']}", "content": f["message"],
                                 **({"severity": "unrecoverable"} if f["outcome"] == "block" else {})})
    ex, inc = _cents(q["total_ex_vat"]), _cents(q["total_inc_vat"])
    out = {
        "id": q["id"],
        "merchant": MERCHANT,
        "line_items": lines,
        "status": _checkout_status(q),
        "currency": q.get("currency", "EUR"),
        "totals": [{"type": "subtotal", "amount": ex}, {"type": "tax", "amount": inc - ex},
                   {"type": "total", "amount": inc}],
        "links": [{"type": "terms_of_use", "url": f"{base_url}/docs", "title": "Sous gateway API"},
                  {"type": "protocol", "url": f"{REPO}/blob/main/docs/PROTOCOL.md", "title": "Sous protocol"}],
        "expires_at": q["expires_at"],
        "sous": {"quote_id": q["id"], "mandate_id": q.get("mandate_id"), "agent_id": q.get("agent_id"),
                 "requested_delivery_date": q["requested_delivery_date"], "lines": sous_lines,
                 "mandate_preview": preview.get("outcome")},
    }
    if unavailable:
        out["sous"]["unavailable_lines"] = unavailable
    if messages:
        out["messages"] = messages
    return out


def _cart_label(l: dict) -> str:
    return f"{l['name']} x {l.get('quantity')} {l.get('sales_unit') or ''}".strip()


def ap2_quote(quote_id: str, base_url: str) -> dict:
    """The quote as a merchant-signed UCP checkout (the `checkout_jwt` an AP2 v0.2 closed checkout mandate binds),
    the closed-mandate claims to accept it, and a v0.1-style CartMandate with merchant_authorization."""
    q, jws = _signed_quote(quote_id)
    checkout = ucp_checkout(q, base_url)
    checkout_jwt = signing.sign(checkout, "JWT")
    checkout_hash = b64_sha256(checkout_jwt)
    exp = _epoch(q["expires_at"])
    contents = {
        "id": q["id"],
        "user_cart_confirmation_required": checkout["status"] != "ready_for_complete",
        "payment_request": {  # W3C Payment Request API shape, as AP2 v0.1 uses
            "method_data": [{"supported_methods": "https://valioaimo.fi/pay/trade-account", "data": {}}],
            "details": {
                "id": q["id"],
                "display_items": [{"label": _cart_label(l),
                                   "amount": {"currency": "EUR", "value": l.get("line_total_inc_vat") or 0}}
                                  for l in q["lines"] if l.get("orderable")],
                "total": {"label": "Total incl. VAT", "amount": {"currency": "EUR", "value": q["total_inc_vat"]}},
            },
        },
        "cart_expiry": q["expires_at"],
        "merchant_name": MERCHANT["name"],
    }
    if unorderable := [l for l in q["lines"] if not l.get("orderable")]:  # extension: outside the payment request
        contents["sous_unavailable_items"] = [
            {"label": _cart_label(l), "reasons": [x["code"] for x in l.get("issues", [])],
             "indicative_amount": {"currency": "EUR", "value": l.get("line_total_inc_vat") or 0}}
            for l in unorderable]
    merchant_authorization = signing.sign(
        {"iss": credentials.gateway_did(), "sub": q["id"], "iat": _epoch(q["issued_at"]), "exp": exp,
         "jti": q["id"], "cart_hash": b64_sha256(signing.canonical(contents))}, "JWT")
    return {
        "profile": f"AP2 v{AP2_VERSION} merchant-signed checkout ({UCP_CHECKOUT_VERSION})",
        "spec": {"ap2": AP2_VERSION, "schemas": AP2_SCHEMAS, "checkout": UCP_CHECKOUT_VERSION,
                 "jws": "RFC 7515 compact, alg EdDSA (RFC 8037); key in /.well-known/jwks.json"},
        "quote_id": q["id"],
        "checkout": checkout,
        "checkout_jwt": checkout_jwt,
        "checkout_hash": checkout_hash,
        "checkout_mandate": {"vct": "mandate.checkout.1", "checkout_jwt": checkout_jwt,
                             "checkout_hash": checkout_hash, **({"exp": exp} if exp else {})},
        "ap2_v0_1_cart_mandate": {"contents": contents, "merchant_authorization": merchant_authorization},
        "native": {"jws": jws, "typ": "sous-quote+jws"},
        "conformance": {
            "checkout_mandate": "valid against AP2 v0.2.0 checkout_mandate.json; unsigned: in AP2 the user or agent "
                                "signs it, in Sous the agent instead calls POST /orders under its mandate",
            "signature": "AP2's reference SDK signs with ES256; Sous signs with EdDSA (Ed25519), also a JOSE "
                         "algorithm. Verify with the JWKS",
        },
    }


# --- quote -> schema.org ----------------------------------------------------

def _availability(l: dict) -> str:
    codes = {i["code"] for i in l.get("issues", [])}
    if not l.get("orderable"):
        return "https://schema.org/OutOfStock"
    if "LATER_DATE" in codes:
        return "https://schema.org/BackOrder"
    if "PARTIAL_STOCK" in codes:
        return "https://schema.org/LimitedAvailability"
    return "https://schema.org/InStock"


def _quantity_node(l: dict) -> dict:
    su = l.get("sales_unit") or ""
    return {"@type": "TypeAndQuantityNode", "amountOfThisGood": l.get("quantity"),
            "unitCode": _UNIT_CODES.get(su, "H87"), "unitText": su,
            "typeOfGood": {"@type": "Product", "productID": l["product_id"], "sku": l["product_id"], "name": l["name"]}}


def schema_org_quote(quote_id: str, base_url: str) -> dict:
    """The quote as schema.org JSON-LD: one Offer for the basket (total, validity, seller) with one Offer per line."""
    q, _ = _signed_quote(quote_id)
    seller = {"@type": "Organization", "name": MERCHANT["name"], "url": MERCHANT["website"]}
    line_offers = []
    for i, l in enumerate(q["lines"]):
        line_offers.append({
            "@type": "Offer", "@id": f"{base_url}/quotes/{q['id']}#L{i + 1}",
            "itemOffered": _quantity_node(l)["typeOfGood"],
            "price": l.get("unit_price_ex_vat"), "priceCurrency": q.get("currency", "EUR"),
            "priceSpecification": [
                {"@type": "UnitPriceSpecification", "price": l.get("unit_price_ex_vat"), "priceCurrency": "EUR",
                 "valueAddedTaxIncluded": False, "unitText": l.get("sales_unit")},
                {"@type": "UnitPriceSpecification", "price": l.get("unit_price_inc_vat"), "priceCurrency": "EUR",
                 "valueAddedTaxIncluded": True, "unitText": l.get("sales_unit")}],
            "eligibleQuantity": {"@type": "QuantitativeValue", "value": l.get("quantity"),
                                 "unitCode": _quantity_node(l)["unitCode"], "unitText": l.get("sales_unit")},
            "availability": _availability(l),
            "availabilityStarts": l.get("delivery_date"),
            "seller": seller,
            **({} if l.get("orderable") else {
                "description": "Indicative price only: not orderable ("
                               + ", ".join(x["code"] for x in l.get("issues", [])) + "), not in the basket total"}),
        })
    return {
        "@context": "https://schema.org",
        "@type": "Offer",
        "@id": f"{base_url}/quotes/{q['id']}",
        "identifier": q["id"],
        "seller": seller,
        "priceCurrency": q.get("currency", "EUR"),
        "price": q["total_inc_vat"],
        "priceSpecification": [
            {"@type": "PriceSpecification", "price": q["total_ex_vat"], "priceCurrency": "EUR",
             "valueAddedTaxIncluded": False},
            {"@type": "PriceSpecification", "price": q["total_inc_vat"], "priceCurrency": "EUR",
             "valueAddedTaxIncluded": True}],
        "validFrom": q["issued_at"],
        "validThrough": q["expires_at"],
        "priceValidUntil": q["expires_at"][:10],
        "availabilityStarts": q["requested_delivery_date"],
        "includesObject": [_quantity_node(l) for l in q["lines"] if l.get("orderable")],  # what the price covers
        "addOn": line_offers,
        "subjectOf": {"@type": "DigitalDocument", "url": f"{base_url}/quotes/{q['id']}",
                      "encodingFormat": "application/jose", "description": "Signed native quote (sous-quote+jws)"},
    }


# --- order -> AP2 checkout receipt, schema.org Order ------------------------

def _signed_order(order_id: str) -> dict:
    o = S.get_order(order_id)
    r = signing.verify(o.get("receipt_jws") or "", "sous-order+jws")
    if r is None or r.get("id") != order_id:
        raise S.GatewayError("order_signature", f"Order {order_id}'s receipt does not verify.", 409)
    return {**o, **r}  # the signed fields win over the stored copy


_FINAL_ERROR = {"refused": "mandate_refused", "rejected": "rejected_by_human", "expired": "expired"}


def ap2_receipt(order_id: str, base_url: str) -> dict:
    """The order as an AP2 v0.2 CheckoutReceipt, signed by the gateway. `reference` is the checkout_hash of the
    quote's merchant-signed checkout (Sous has no separate closed mandate: the order is placed under the open one)."""
    o = _signed_order(order_id)
    status = o["status"]
    native = {"receipt_jws": o["receipt_jws"], "typ": "sous-order+jws"}
    if status == "pending_approval":
        return {"profile": f"AP2 v{AP2_VERSION} checkout receipt", "order_id": order_id, "final": False,
                "receipt": None, "receipt_jwt": None, "native": native,
                "note": "Held for a human (ask tier). AP2 receipts describe a final state; ask again after the decision."}
    reference = ap2_quote(o["quote_id"], base_url)["checkout_hash"]
    receipt: dict[str, Any] = {"status": "Success" if status == "ordered" else "Error",
                               "iss": credentials.gateway_did(), "iat": _epoch(o["updated_at"]), "reference": reference}
    if status == "ordered":
        receipt["order_id"] = order_id
    else:
        reason = (o.get("approval") or {}).get("result_reason") or "; ".join(
            f["message"] for f in o["decision"]["findings"] if f["outcome"] == "block") or status
        receipt.update(error=_FINAL_ERROR.get(status, status), error_description=reason)
    return {"profile": f"AP2 v{AP2_VERSION} checkout receipt", "order_id": order_id, "final": True,
            "receipt": receipt, "receipt_jwt": signing.sign(receipt, "JWT"), "native": native}


_ORDER_STATUS = {"ordered": "OrderProcessing", "pending_approval": "OrderProblem", "refused": "OrderCancelled",
                 "rejected": "OrderCancelled", "expired": "OrderCancelled"}


def schema_org_order(order_id: str, base_url: str) -> dict:
    o = _signed_order(order_id)
    seller = {"@type": "Organization", "name": MERCHANT["name"], "url": MERCHANT["website"]}
    return {
        "@context": "https://schema.org",
        "@type": "Order",
        "@id": f"{base_url}/orders/{order_id}",
        "orderNumber": order_id,
        "orderStatus": f"https://schema.org/{_ORDER_STATUS.get(o['status'], 'OrderProblem')}",
        "description": f"Sous status {o['status']}; mandate decision {o['decision']['outcome']}"
                       + (" (held for human approval)" if o["status"] == "pending_approval" else ""),
        "orderDate": o.get("created_at"),
        "seller": seller,
        "customer": {"@type": "Organization", "name": o.get("buyer")},
        "broker": {"@type": "SoftwareApplication", "name": o.get("agent_id"), "description": "Purchasing agent"},
        "acceptedOffer": {"@id": f"{base_url}/quotes/{o['quote_id']}"},
        "orderedItem": [{
            "@type": "OrderItem", "orderItemNumber": f"{order_id}-L{i + 1}",
            "orderQuantity": {"@type": "QuantitativeValue", "value": l.get("quantity"),
                              "unitCode": _UNIT_CODES.get(l.get("sales_unit") or "", "H87"),
                              "unitText": l.get("sales_unit")},
            "orderedItem": {"@type": "Product", "productID": l["product_id"], "sku": l["product_id"], "name": l["name"]},
            "orderDelivery": {"@type": "ParcelDelivery", "expectedArrivalFrom": l.get("delivery_date")},
        } for i, l in enumerate(o["lines"])],
        "totalPaymentDue": {"@type": "PriceSpecification", "price": o["total_ex_vat"], "priceCurrency": "EUR",
                            "valueAddedTaxIncluded": False},
        "subjectOf": {"@type": "DigitalDocument", "url": f"{base_url}/orders/{order_id}",
                      "encodingFormat": "application/jose", "description": "Signed receipt (sous-order+jws)"},
    }


# --- JSON Schemas of the native objects --------------------------------------
# Documentation models of what the gateway returns (the service builds plain dicts). extra="allow": new fields
# don't break old readers; tests validate real quotes and orders against these schemas.

class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class IssueDoc(_Open):
    code: str = Field(description="Stable issue code, e.g. LATER_DATE, CUTOFF_PASSED (PROTOCOL.md section 5)")
    severity: Literal["blocking", "warning"]
    message: str


class QuoteLineDoc(_Open):
    product_id: str
    name: str
    sales_unit: str | None = None
    quantity: float | None = Field(None, description="In sales units, rounded to the allowed lot size")
    unit_price_ex_vat: float | None = None
    unit_price_inc_vat: float | None = None
    vat_rate: float | None = None
    line_total_ex_vat: float | None = None
    line_total_inc_vat: float | None = None
    logistic_model: str | None = None
    delivery_date: str | None = None
    order_cutoff: str | None = None
    orderable: bool
    issues: list[IssueDoc]
    item_index: int | None = None
    substitute_for: str | None = None


class MandatePreviewDoc(_Open):
    outcome: Literal["silent", "notify", "ask", "block"]
    findings: list[Finding]


class QuoteDoc(_Open):
    """Payload of a sous-quote+jws (GET /quotes/{id} returns {quote, jws})."""
    id: str
    agent_id: str
    mandate_id: str | None = None
    issued_at: str
    expires_at: str
    requested_delivery_date: str
    currency: Literal["EUR"]
    items: list[dict]
    lines: list[QuoteLineDoc]
    total_ex_vat: float = Field(description="Orderable total ex VAT: the sum of the orderable lines only, what "
                                            "placing this quote would charge. 0 when no line can be ordered.")
    total_inc_vat: float = Field(description="Orderable total incl. VAT (orderable lines only).")
    indicative_total_ex_vat: float | None = Field(
        None, description="Indicative total ex VAT of every line, unorderable ones included: what the need "
                          "would cost, for reference. Never charged; compare with total_ex_vat.")
    uncovered: list[dict]
    alternatives_considered: list[dict]
    valio_api_calls: dict[str, int]
    rationale: str | None = None
    mandate_preview: MandatePreviewDoc | None = None


class OrderLineDoc(_Open):
    product_id: str
    name: str
    quantity: float | None = None
    sales_unit: str | None = None
    unit_price_ex_vat: float | None = None
    line_total_ex_vat: float | None = None
    delivery_date: str | None = None
    logistic_model: str | None = None
    order_cutoff: str | None = None


class DecisionDoc(_Open):
    outcome: Literal["silent", "notify", "ask", "block"]
    findings: list[Finding]
    by: str
    at: str


class ApprovalDoc(_Open):
    by: str = Field(description="The name the approver gave (free text)")
    authority: str | None = Field(None, description="The credential that let them decide: 'global approver "
                                                    "code', 'sandbox approver SBX-...', or 'open (no approver "
                                                    "code configured)'")
    approved: bool
    note: str | None = None
    at: str
    result_reason: str | None = None


OrderStatus = Literal["ordered", "pending_approval", "refused", "rejected", "expired"]


class ReceiptDoc(_Open):
    """Payload of a sous-order+jws: the order's signed state (re-issued after a human decision)."""
    id: str
    quote_id: str
    mandate_id: str
    status: OrderStatus
    total_ex_vat: float
    lines: list[OrderLineDoc]
    decision: DecisionDoc
    approval: ApprovalDoc | None = None
    updated_at: str


class OrderDoc(ReceiptDoc):
    """GET /orders/{id}: the order with its signed receipt (receipt_jws)."""
    agent_id: str
    buyer: str
    total_inc_vat: float
    delivery_date: str
    requested_delivery_date: str
    order_url: str
    approval_url: str | None = None
    idempotency_key: str | None = None
    agent_rationale: str | None = None
    created_at: str
    receipt_jws: str


SCHEMA_MODELS: dict[str, type[BaseModel]] = {"mandate": Mandate, "quote": QuoteDoc, "order": OrderDoc,
                                             "receipt": ReceiptDoc}


def json_schema(name: str, base_url: str) -> dict:
    model = SCHEMA_MODELS[name]
    s = model.model_json_schema()
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"{base_url}/schemas/{name}.json",
            **s, "description": inspect.cleandoc(model.__doc__ or s.get("description") or "")}


# --- discovery ----------------------------------------------------------------

def _tools() -> list:
    import asyncio
    from .mcp_server import mcp
    return asyncio.run(mcp.list_tools())


def agent_card(base_url: str, version: str, tools: list | None = None) -> dict:
    """A2A v1.0 Agent Card (JSON names of a2a.proto AgentCard). Sous speaks MCP and REST/OpenAPI, not A2A's task
    protocol, so supportedInterfaces use A2A's open-form protocolBinding values "MCP" and "OPENAPI"."""
    from mcp_types import LATEST_PROTOCOL_VERSION
    tools = _tools() if tools is None else tools
    skills = []
    for t in tools:
        desc = (t.description or "").strip()
        ann = t.annotations
        tags = ["valio-aimo", "food-service", "purchasing"]
        if ann is not None and ann.read_only_hint:
            tags.append("read-only")
        skills.append({"id": t.name, "name": t.title or t.name,
                       "description": desc.split("\n\n")[0] or t.name, "tags": tags,
                       "inputModes": ["application/json"], "outputModes": ["application/json"]})
    return {
        "name": "Sous: Valio Aimo agent gateway",
        "description": "Mandate-enforcing purchasing gateway for AI buyer agents: catalog search, signed quotes "
                       "with live price and availability, and orders decided by a deterministic mandate engine "
                       "(silent / notify / ask / block), with a hash-chained audit log.",
        "supportedInterfaces": [
            {"url": f"{base_url}/mcp", "protocolBinding": "MCP", "protocolVersion": LATEST_PROTOCOL_VERSION},
            {"url": f"{base_url}/openapi.json", "protocolBinding": "OPENAPI", "protocolVersion": "3.1"},
        ],
        "provider": {"organization": "Team Sous (Junction 2026)", "url": REPO},
        "version": version,
        "documentationUrl": f"{base_url}/docs",
        "capabilities": {
            "streaming": False, "pushNotifications": False, "extendedAgentCard": False,
            "extensions": [{
                "uri": f"{REPO}/blob/main/docs/PROTOCOL.md",
                "description": "Sous trust protocol: signed mandates, quotes and receipts; tiered decisions; audit.",
                "required": False,
                "params": {
                    "jwks_uri": f"{base_url}/.well-known/jwks.json",
                    "openapi": f"{base_url}/openapi.json",
                    "mcp_server_card": f"{base_url}/.well-known/mcp/server-card.json",
                    "schemas": {n: f"{base_url}/schemas/{n}.json" for n in SCHEMA_MODELS},
                    "signed_types": ["sous-mandate+jws", "sous-quote+jws", "sous-order+jws"],
                    "buyer_mandate_credentials": {
                        "endpoint": f"{base_url}/mandates/credential", "format": "vc+jwt (W3C VC 2.0)",
                        "did_methods": ["did:key"], "algorithms": ["ES256", "EdDSA"]},
                    "exports": {"ap2_mandate": f"{base_url}/mandates/{{id}}/ap2",
                                "ap2_checkout": f"{base_url}/quotes/{{id}}/ap2",
                                "ap2_receipt": f"{base_url}/orders/{{id}}/ap2",
                                "schema_org_offer": f"{base_url}/quotes/{{id}}/schema-org",
                                "schema_org_order": f"{base_url}/orders/{{id}}/schema-org"},
                    "standards": f"{REPO}/blob/main/docs/STANDARDS.md",
                },
            }],
        },
        "securitySchemes": {
            "agentToken": {"httpAuthSecurityScheme": {
                "scheme": "Bearer", "description": "Per-mandate agent credential, required when the mandate has "
                                                    "require_agent_auth (POST /mandates mints it)."}},
            "approverCode": {"apiKeySecurityScheme": {
                "location": "header", "name": "X-Approver-Code",
                "description": "Human-only actions (approve, reject, issue). Not for agents."}},
        },
        "securityRequirements": [],  # reads are open; the agent token is enforced per mandate
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": skills,
    }


def mcp_server_card(base_url: str, version: str) -> dict:
    """MCP Server Card (SEP-2127, schema v1). Lists the remote only; tools are discovered over the connection."""
    from mcp_types import DEFAULT_NEGOTIATED_VERSION, LATEST_PROTOCOL_VERSION
    return {
        "$schema": MCP_SERVER_CARD_SCHEMA,
        "name": "io.github.zawaer/sous",
        "version": version,
        "title": "Sous: Valio Aimo agent gateway",
        "description": "Search Valio Aimo's catalog, get signed quotes and order within a mandate.",
        "websiteUrl": f"{base_url}/docs",
        "repository": {"url": REPO, "source": "github"},
        "remotes": [{
            "type": "streamable-http",
            "url": f"{base_url}/mcp",
            "headers": [{"name": "Authorization", "description": "Agent bearer credential, when the mandate "
                                                                  "requires agent authentication.",
                         "isRequired": False, "isSecret": True, "value": "Bearer {agent_token}",
                         "variables": {"agent_token": {"description": "Shown once when the mandate is issued.",
                                                       "isRequired": False, "isSecret": True}}}],
            "supportedProtocolVersions": sorted({DEFAULT_NEGOTIATED_VERSION, LATEST_PROTOCOL_VERSION}),
        }],
    }


def ai_catalog(base_url: str) -> dict:
    """AI Catalog (/.well-known/ai-catalog.json) entry pointing at the MCP Server Card, per the server-card
    extension's discovery doc."""
    return {"specVersion": "1.0", "entries": [{
        "identifier": "urn:air:github.com:mcp:zawaer-sous",
        "type": "application/mcp-server-card+json",
        "url": f"{base_url}/.well-known/mcp/server-card.json"}]}


def base_url(request_base: str) -> str:
    from . import config
    return config.PUBLIC_BASE_URL or request_base.rstrip("/")


__all__ = ["ap2_mandate", "ap2_quote", "ap2_receipt", "schema_org_quote", "schema_org_order", "json_schema",
           "agent_card", "mcp_server_card", "ai_catalog", "base_url"]
