"""REST API (OpenAPI at /openapi.json, docs at /docs) with the MCP server mounted at /mcp."""

from __future__ import annotations

import contextlib
import os
from datetime import date
from typing import Annotated, Literal

import time
from collections import defaultdict, deque

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from . import access, agent_auth, aimo, catalog, clock, config, exposure, quote as Q, rules, sandbox, service as S, signing, standing, store
from .mandate import Mandate
from .mcp_server import mcp

mcp_app = mcp.streamable_http_app(
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    catalog._db()  # build the index on first start
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(mcp_app.router.lifespan_context(mcp_app))
        yield


app = FastAPI(
    title="Sous: Valio Aimo agent gateway",
    version="0.2.0",
    description=(
        "Lets external purchasing agents search Valio Aimo's catalog, get signed quotes with live price and "
        "availability, and place orders within a mandate (sous) issued by their human. The mandate engine "
        "decides each order's tier: silent or notify (ordered), ask (waits for a human), block (refused). "
        "Every step is in a hash-chained audit log. The same operations are available over MCP at /mcp.\n\n"
        "Discovery and open standards: A2A agent card at /.well-known/agent-card.json, MCP server card at "
        "/.well-known/mcp/server-card.json, signing keys at /.well-known/jwks.json, JSON Schemas at /schemas/{mandate,"
        "quote,order,receipt}.json, and AP2 / schema.org views of mandates, quotes and orders (tag `standards`). "
        "Mapping and gaps: https://github.com/Zawaer/sous/blob/main/docs/STANDARDS.md"
    ),
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["Mcp-Session-Id"])


@app.middleware("http")
async def clock_header(request: Request, call_next):
    """Demo clock never silent: every response says which instant business rules were evaluated at."""
    response = await call_next(request)
    if (h := clock.header()) is not None:
        response.headers["X-Sous-Clock"] = h
    return response


@app.middleware("http")
async def mcp_without_slash(request: Request, call_next):
    """Serve /mcp and /mcp/ alike: a 307 redirect can drop https behind a TLS proxy and breaks some MCP clients."""
    if request.scope["path"] == "/mcp":
        request.scope["path"] = "/mcp/"
        request.scope["raw_path"] = b"/mcp/"
    elif request.scope["path"] in ("/mcp/server-card", "/mcp/server-card/"):  # SEP-2127's recommended location
        request.scope["path"] = "/.well-known/mcp/server-card.json"
        request.scope["raw_path"] = request.scope["path"].encode()
    return await call_next(request)
app.mount("/mcp", mcp_app)
WEB_DIR = config.ROOT / "web"
if WEB_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=WEB_DIR, html=True), name="ui")
BUYER_DIR = config.ROOT / "buyer"
if BUYER_DIR.is_dir():
    app.mount("/buyer", StaticFiles(directory=BUYER_DIR, html=True), name="buyer")

# The reference BUYER agent (agent/server.py) is not part of the gateway; mounted only for the demo page.
if config.env("BUYER_AGENT") == "1":
    from agent.server import app as buyer_agent_app
    app.mount("/agent", buyer_agent_app)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ui/" if WEB_DIR.is_dir() else "/docs")


_hits: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    # CF-Connecting-IP is client-controlled unless Cloudflare is the only way in: trust it only when told to.
    # Otherwise uvicorn --proxy-headers has already resolved X-Forwarded-For into request.client.
    if config.env("TRUST_CF_HEADER") == "1" and (ip := request.headers.get("cf-connecting-ip")):
        return ip
    return request.client.host if request.client else "?"


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    """Per-IP limit on writes and MCP calls, so one visitor can't burn our Valio API budget."""
    if request.method == "POST":
        q = _hits[_client_ip(request)]
        now = time.time()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= config.RATE_LIMIT_PER_MIN:
            return JSONResponse(status_code=429, content={"error": "rate_limited",
                                                          "message": "Too many requests; try again in a minute."})
        q.append(now)
    return await call_next(request)


def require_approver(request: Request, x_approver_code: str | None = Header(default=None),
                     authorization: str | None = Header(default=None, include_in_schema=False)):
    """Human-only actions. In production this is the buyer's own login (e.g. OIDC); here a shared demo code.
    A judge sandbox's approver token (sandbox.py) also works, but only to approve/reject its own orders.
    An agent credential (vka_..., as bearer or as the code) on approve/reject is refused with 403 and audited,
    even when no approver code is configured: agents never decide their own orders."""
    path = request.url.path
    if path.endswith(("/approve", "/reject")):
        presented = [t for t in (agent_auth.bearer(authorization), x_approver_code)
                     if t and t.startswith(agent_auth.PREFIX)]
        if presented:
            raise S.refuse_agent_approval(request.path_params.get("order_id", ""), presented[0],
                                          "approve" if path.endswith("/approve") else "reject")
    if config.APPROVER_CODE and x_approver_code != config.APPROVER_CODE and not (
            request.url.path.endswith(("/approve", "/reject"))
            and sandbox.may_approve(x_approver_code, request.path_params.get("order_id"))):
        raise HTTPException(401, "Human approver code required (X-Approver-Code header).")


def require_global_approver(x_approver_code: str | None = Header(default=None)):
    if config.APPROVER_CODE and x_approver_code != config.APPROVER_CODE:
        raise HTTPException(401, "Human approver code required (X-Approver-Code header).")


_SANDBOX_Q = Query(None, alias="sandbox", description="Judge sandbox id (e.g. SBX-7Q2K): show only its data. "
                                     "Without it, sandbox data is hidden. With it, the call needs the sandbox's approver or agent token "
                                     "(401 approver_code_required otherwise, also for unknown ids).")


_agent_bearer = HTTPBearer(auto_error=False, scheme_name="agentToken",
                           description="Agent credential for mandates with require_agent_auth (see POST /mandates).")


def agent_token(cred: HTTPAuthorizationCredentials | None = Depends(_agent_bearer)) -> str | None:
    """The agent's credential from `Authorization: Bearer <token>` (only checked when the mandate requires it)."""
    return cred.credentials if cred else None


# --- read scopes (sous/access.py) --------------------------------------------
# Detail routes answer 404 for what the caller may not read, the same as for what does not exist.

def _read_mandate(mandate_id: str, r: access.Reader = Depends(access.reader)):
    access.check_mandate(r, mandate_id)


def _read_order(order_id: str, r: access.Reader = Depends(access.reader)):
    access.check_order(r, order_id)


def _read_quote(quote_id: str, r: access.Reader = Depends(access.reader)):
    access.check_quote(r, quote_id)


def _read_standing(standing_id: str, r: access.Reader = Depends(access.reader)):
    access.check_standing(r, standing_id)


_HTTP_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
               405: "method_not_allowed", 409: "conflict", 429: "rate_limited"}


@app.exception_handler(StarletteHTTPException)
async def http_error(_: Request, e: StarletteHTTPException):
    """Same shape as gateway errors: {error, message}, so agents parse one format."""
    return JSONResponse(status_code=e.status_code, headers=getattr(e, "headers", None),
                        content={"error": _HTTP_CODES.get(e.status_code, f"http_{e.status_code}"),
                                 "message": str(e.detail)})


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, e: RequestValidationError):
    problems = []
    for err in e.errors():
        where = ".".join(str(x) for x in err.get("loc", ()) if x not in ("body", "query", "path"))
        problems.append(f"{where}: {err.get('msg')}" if where else str(err.get("msg")))
    return JSONResponse(status_code=422, content={
        "error": "invalid_request",
        "message": "; ".join(problems) + ". Dates are YYYY-MM-DD (e.g. delivery_date=2026-11-16).",
        "details": jsonable_encoder(e.errors())})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.exception_handler(S.GatewayError)
async def gateway_error(_: Request, e: S.GatewayError):
    if e.retry_after is None:
        return JSONResponse(status_code=e.status, content={"error": e.code, "message": e.message})
    return JSONResponse(status_code=e.status, headers={"Retry-After": str(e.retry_after)},
                        content={"error": e.code, "message": e.message, "retry_after": e.retry_after})


# --- discovery --------------------------------------------------------------

@app.get("/.well-known/jwks.json", tags=["trust"])
def jwks():
    """Public key for verifying quotes, mandates and order receipts (Ed25519, JWS)."""
    return signing.jwks()


from . import standards as STD  # noqa: E402  open-standard views and discovery; docs/STANDARDS.md


@app.get("/.well-known/agent-card.json", tags=["standards"])
def a2a_agent_card(request: Request):
    """A2A v1.0 Agent Card: interfaces (MCP, OpenAPI), auth schemes, JWKS, schemas and skills (= MCP tools)."""
    return STD.agent_card(STD.base_url(str(request.base_url)), app.version)


@app.get("/.well-known/agent.json", include_in_schema=False)
def a2a_agent_card_legacy(request: Request):
    return a2a_agent_card(request)  # A2A < 0.3 path


@app.get("/.well-known/mcp/server-card.json", tags=["standards"])
def mcp_server_card(request: Request):
    """MCP Server Card (SEP-2127, schema v1); also at /mcp/server-card."""
    return STD.mcp_server_card(STD.base_url(str(request.base_url)), app.version)


@app.get("/.well-known/ai-catalog.json", tags=["standards"])
def ai_catalog(request: Request):
    return JSONResponse(STD.ai_catalog(STD.base_url(str(request.base_url))), media_type="application/ai-catalog+json")


@app.get("/schemas/{name}.json", tags=["standards"])
def json_schema(name: Literal["mandate", "quote", "order", "receipt"], request: Request):
    """JSON Schema (2020-12) of a native object: mandate, quote (sous-quote+jws payload), order, receipt."""
    return JSONResponse(STD.json_schema(name, STD.base_url(str(request.base_url))),
                        media_type="application/schema+json")


@app.get("/mandates/{mandate_id}/ap2", tags=["standards"], dependencies=[Depends(_read_mandate)])
def mandate_ap2(mandate_id: str):
    """The mandate as AP2 v0.2 open checkout/payment mandate claims, a v0.1 IntentMandate and a W3C VC."""
    return STD.ap2_mandate(mandate_id)


@app.get("/quotes/{quote_id}/ap2", tags=["standards"], dependencies=[Depends(_read_quote)])
def quote_ap2(quote_id: str, request: Request):
    """The quote as a gateway(merchant)-signed UCP checkout for an AP2 v0.2 checkout mandate, and a v0.1 CartMandate."""
    return STD.ap2_quote(quote_id, STD.base_url(str(request.base_url)))


@app.get("/quotes/{quote_id}/schema-org", tags=["standards"], dependencies=[Depends(_read_quote)])
def quote_schema_org(quote_id: str, request: Request):
    """The quote as schema.org Offer JSON-LD."""
    return JSONResponse(STD.schema_org_quote(quote_id, STD.base_url(str(request.base_url))),
                        media_type="application/ld+json")


@app.get("/orders/{order_id}/ap2", tags=["standards"], dependencies=[Depends(_read_order)])
def order_ap2(order_id: str, request: Request):
    """The order as a signed AP2 v0.2 CheckoutReceipt (final states only)."""
    return STD.ap2_receipt(order_id, STD.base_url(str(request.base_url)))


@app.get("/orders/{order_id}/schema-org", tags=["standards"], dependencies=[Depends(_read_order)])
def order_schema_org(order_id: str, request: Request):
    """The order as schema.org Order JSON-LD with OrderItems."""
    return JSONResponse(STD.schema_org_order(order_id, STD.base_url(str(request.base_url))),
                        media_type="application/ld+json")


@app.get("/health", tags=["meta"])
def health():
    return {"ok": True, "clock": clock.info(), "valio_api_calls": aimo.stats, "approver_code_required": bool(config.APPROVER_CODE),
            "agent_auth_required_globally": config.REQUIRE_AGENT_AUTH,
            "storage": store.backend(), "valio_aimo": aimo.health()}


# --- catalog ----------------------------------------------------------------

@app.get("/products", tags=["catalog"])
def search_products(
    q: str = "",
    claims: list[str] = Query(default=[]),
    avoid_allergens: list[str] = Query(default=[]),
    labels: list[str] = Query(default=[]),
    temperature: Literal["ambient", "chilled", "frozen"] | None = None,
    category: str | None = None,
    limit: int = Query(15, ge=1, le=50),
    agent_id: str = "anonymous-agent",
    food_only: bool | None = None,
):
    """Search the static catalog (fi/sv/en with synonyms). No live price or stock: request a quote for that.

    food_only: default ranks food first; true drops non-food; false = no preference."""
    return S.search(agent_id, q, claims=claims or None, avoid_allergens=avoid_allergens or None,
                    labels=labels or None, temperature=temperature, category=category, limit=limit,
                    food_only=food_only)


@app.get("/products/{product_id}", tags=["catalog"])
def get_product(product_id: str):
    p = catalog.get(product_id)
    if not p:
        raise HTTPException(404, f"No product {product_id}")
    return p


@app.get("/allergens", tags=["catalog"])
def allergen_codes():
    """GS1 allergen codes used in mandates and search filters."""
    return {"codes": catalog.ALLERGENS, "groups": {k: sorted(v) for k, v in catalog.ALLERGEN_GROUPS.items()}}


@app.get("/products/{product_id}/substitutes", tags=["catalog"])
def suggest_substitutes(
    product_id: str,
    delivery_date: date,
    amount: float = 1,
    unit: Annotated[Q.Unit, Query(description=Q.UNIT_DOC)] = "SALES_UNIT",
    mandate_id: str | None = None,
    avoid_allergens: list[str] = Query(default=[]),
    limit: int = Query(3, ge=1, le=6),
    agent_id: str = "anonymous-agent",
    token: str | None = Depends(agent_token),
    r: access.Reader = Depends(access.reader),
):
    """Good / better / best substitutes with their compromises and live offers. With mandate_id the call is
    read-scoped like GET /mandates/{id} (docs/PROTOCOL.md section 22)."""
    return S.suggest_substitutes(agent_id, product_id, delivery_date, amount, unit, mandate_id,
                                 avoid_allergens or None, limit, agent_token=token, reader=r)


# --- quotes and orders ------------------------------------------------------

@app.post("/quotes", tags=["ordering"])
def request_quote(req: S.QuoteRequest, token: str | None = Depends(agent_token),
                  r: access.Reader = Depends(access.reader)):
    """Live price + availability for a shortlist, as a signed, expiring quote (with a mandate preview).

    If the mandate requires agent authentication, send `Authorization: Bearer <agent token>`.
    Stock appears as `stock_level` buckets unless SOUS_STOCK_EXPOSURE says otherwise (docs/PROTOCOL.md)."""
    return S.request_quote(req, agent_token=token, approver=exposure.is_approver(r))


@app.get("/quotes/{quote_id}", tags=["ordering"], dependencies=[Depends(_read_quote)])
def get_quote(quote_id: str):
    q = store.get_quote(quote_id)
    if not q:
        raise HTTPException(404, f"No quote {quote_id}")
    jws = q.pop("_jws")
    q.pop("_order_id")
    return {"quote": q, "jws": jws, "agent_claim": access.agent_claim(q.get("rationale"))}


class OrderRequest(BaseModel):
    agent_id: str
    mandate_id: str
    quote_id: str
    idempotency_key: str | None = None
    rationale: str | None = Field(None, max_length=500, description=S.RATIONALE_DOC)


@app.post("/orders", tags=["ordering"])
def place_order(req: OrderRequest, token: str | None = Depends(agent_token)):
    """Order a quote. The mandate decides: ordered, pending_approval or refused, with reasons.

    If the mandate requires agent authentication, send `Authorization: Bearer <agent token>`
    (401 agent_auth_required without it, 403 agent_auth_invalid with a wrong one)."""
    return S.place_order(req.agent_id, req.mandate_id, req.quote_id, req.idempotency_key, agent_token=token,
                         rationale=req.rationale)


@app.get("/orders", tags=["ordering"])
def list_orders(status: Literal["ordered", "pending_approval", "refused", "rejected", "expired"] | None = None,
                mandate_id: str | None = None, sandbox_id: str | None = _SANDBOX_Q,
                r: access.Reader = Depends(access.reader)):
    """Orders you may read (see docs/PROTOCOL.md "Read access"), newest first."""
    access.check_sandbox_list(r, sandbox_id)
    scope = sandbox.list_scope(sandbox_id)
    if scope is None:
        return {"orders": []}
    idx, ok = sandbox.index(), access.readable_mandates(r)
    return {"orders": [access.label_order(o) for o in store.list_orders(status, mandate_id)
                       if ((mandate_id and not sandbox_id) or sandbox.keep(scope, o["mandate_id"], idx))
                       and (ok is None or o["mandate_id"] in ok)]}


@app.get("/orders/{order_id}", tags=["ordering"], dependencies=[Depends(_read_order)])
def get_order(order_id: str):
    """The order. agent_claim labels the agent's rationale: its own words, unverified, never used by a rule."""
    return access.label_order(S.get_order(order_id))


@app.get("/orders/{order_id}/history", tags=["audit"], dependencies=[Depends(_read_order)])
def order_history(order_id: str):
    """Every version of the order and the mandate AS OF each decision (MariaDB system-versioned tables)."""
    return S.order_history(order_id)


class Decision(BaseModel):
    by: str
    note: str | None = None


@app.post("/orders/{order_id}/approve", tags=["approvals"], dependencies=[Depends(require_approver)])
def approve(order_id: str, d: Decision):
    return S.decide(order_id, d.by, True, d.note)


@app.post("/orders/{order_id}/reject", tags=["approvals"], dependencies=[Depends(require_approver)])
def reject(order_id: str, d: Decision):
    return S.decide(order_id, d.by, False, d.note)


# --- rule register ----------------------------------------------------------

@app.get("/rules", tags=["rules"])
def rules_register():
    """Every rule the gateway can fire: id, kind (finding or refusal), the tiers it can produce, owner (buyer
    mandate, Valio, gateway safety), plain-language text and the mandate field that sets it. Each decision
    finding links here with `rule_url`."""
    return rules.document()


@app.get("/rules/{rule_id}", tags=["rules"])
def rule_detail(rule_id: str):
    if not (r := rules.get(rule_id)):
        raise S.GatewayError("rule_not_found", f"No rule {rule_id}; GET /rules lists them all.", 404)
    return r


# --- mandates ---------------------------------------------------------------

@app.post("/mandates", tags=["mandates"], dependencies=[Depends(require_approver)])
def issue_mandate(m: Mandate, mint_agent_credential: bool | None = None):
    """A human issues a mandate to their agent; the gateway signs it.

    With require_agent_auth (or mint_agent_credential=true) the response includes agent_credential: the agent's
    bearer token, shown this once."""
    return S.issue_mandate(m, mint_agent_credential)


@app.get("/mandates", tags=["mandates"])
def list_mandates(sandbox_id: str | None = _SANDBOX_Q, r: access.Reader = Depends(access.reader)):
    """Mandates you may read: all with the approver code, a sandbox's with its token, an agent's own with its
    bearer token, and the public demo mandates (demo_public) without credentials."""
    access.check_sandbox_list(r, sandbox_id)
    scope = sandbox.list_scope(sandbox_id)
    if scope is None:
        return {"mandates": []}
    idx = sandbox.index()
    return {"mandates": [m for m in store.list_mandates()
                         if sandbox.keep(scope, m["id"], idx) and access.can_read_mandate(r, m["id"], m)]}


@app.get("/mandates/{mandate_id}", tags=["mandates"], dependencies=[Depends(_read_mandate)])
def get_mandate(mandate_id: str):
    return S.get_mandate(mandate_id)


@app.get("/mandates/{mandate_id}/history", tags=["mandates"], dependencies=[Depends(_read_mandate)])
def mandate_history(mandate_id: str):
    """Every version of the mandate with its validity period."""
    return S.mandate_history(mandate_id)


@app.get("/mandates/{mandate_id}/suggestions", tags=["mandates"])
def mandate_suggestions(mandate_id: str, delivery_date: date, agent_id: str, limit: int = Query(5, ge=1, le=20),
                        basis: list[str] = Query(default=[]), token: str | None = Depends(agent_token),
                        r: access.Reader = Depends(access.reader)):
    """Valio Aimo's own commercial suggestions (sponsored, not neutral advice): better-value alternatives to what
    the buyer orders, seasonal and new products. Offers only, never orders; pre-checked against the mandate."""
    from . import suggest
    return suggest.suggestions(mandate_id, agent_id, delivery_date, basis or None, limit, agent_token=token,
                               reader=r)


class CredentialAction(BaseModel):
    by: str
    action: Literal["rotate", "revoke"] = "rotate"


@app.get("/mandates/{mandate_id}/agent-credential", tags=["mandates"], dependencies=[Depends(_read_mandate)])
def agent_credential_status(mandate_id: str):
    """Whether the mandate's agent must authenticate, and which credential is active (last 4 chars; no secret)."""
    return S.agent_credential_status(mandate_id)


@app.post("/mandates/{mandate_id}/agent-credential", tags=["mandates"], dependencies=[Depends(require_approver)])
def agent_credential(mandate_id: str, req: CredentialAction):
    """Human-only. rotate: mint a new bearer token for the mandate's agent (shown once; the old one stops working).
    revoke: delete it, so a mandate that requires agent auth can't be used until a new one is minted."""
    return S.agent_credential(mandate_id, req.by, req.action)


@app.post("/mandates/{mandate_id}/revoke", tags=["mandates"], dependencies=[Depends(require_approver)])
def revoke_mandate(mandate_id: str, d: Decision):
    return S.revoke_mandate(mandate_id, d.by)


# --- buyer-signed mandates (W3C Verifiable Credentials) ---------------------

class CredentialIn(BaseModel):
    credential: str  # compact JWS, typ vc+jwt


class RevocationIn(BaseModel):
    revocation: str  # compact JWS, typ sous-revocation+jws


class BuyerKeyIn(BaseModel):
    did: str
    by: str


@app.post("/mandates/credential", tags=["mandates"])
def issue_credential_mandate(c: CredentialIn, mint_agent_credential: bool | None = None):
    """The buyer's human signs the mandate as a W3C Verifiable Credential (vc+jwt, ES256 or EdDSA, issuer a
    did:key); the gateway verifies it, checks the key may speak for the buyer and countersigns it.

    The buyer's key is the authorisation here, so no approver code: the first credential for a buyer with no
    registered keys pins its key (trust on first use). See docs/PROTOCOL.md "Buyer-signed mandates"."""
    return S.issue_credential_mandate(c.credential, mint_agent_credential)


@app.get("/mandates/{mandate_id}/verify", tags=["mandates", "trust"], dependencies=[Depends(_read_mandate)])
def verify_mandate(mandate_id: str):
    """Who authorised this mandate, and does it still hold: the buyer's VC signature (if buyer-signed), the gateway's
    countersignature, whether the issuer key may speak for the buyer, the validity window and revocation."""
    return S.verify_mandate(mandate_id)


@app.post("/mandates/{mandate_id}/revoke-credential", tags=["mandates"])
def revoke_credential_mandate(mandate_id: str, r: RevocationIn):
    """The buyer revokes their own mandate with a statement signed by the same key that issued it."""
    return S.revoke_by_credential(mandate_id, r.revocation)


@app.get("/buyers/{buyer}/keys", tags=["mandates", "trust"])
def buyer_keys(buyer: str):
    """The did:keys that may sign mandates for this buyer (pinned on first use or registered by an approver)."""
    from . import credentials
    return {"buyer": buyer, "keys": credentials.buyer_keys(buyer), "trust_on_first_use": config.BUYER_KEY_TOFU}


@app.post("/buyers/{buyer}/keys", tags=["mandates", "trust"], dependencies=[Depends(require_approver)])
def add_buyer_key(buyer: str, k: BuyerKeyIn):
    """Human-only: register a did:key that may sign mandates for this buyer (turns off trust on first use for it)."""
    from . import credentials
    try:
        row = credentials.add_buyer_key(buyer, k.did, "approver", k.by)
    except credentials.CredentialError as e:
        raise S.GatewayError(e.code, e.message, e.status) from None
    store.audit("buyer.key_registered", k.by, "human", buyer, buyer, {"did": row["did"]})
    return {"buyer": buyer, "keys": credentials.buyer_keys(buyer)}


@app.delete("/buyers/{buyer}/keys/{did}", tags=["mandates", "trust"], dependencies=[Depends(require_approver)])
def remove_buyer_key(buyer: str, did: str, by: str):
    """Human-only: remove a key. Mandates it signed stop verifying (they can't be used for quotes or orders)."""
    from . import credentials
    if not credentials.remove_buyer_key(buyer, did):
        raise HTTPException(404, f"{did} is not a key of buyer {buyer}.")
    store.audit("buyer.key_removed", by, "human", buyer, buyer, {"did": did})
    return {"buyer": buyer, "keys": credentials.buyer_keys(buyer)}


# --- standing orders --------------------------------------------------------

class RenewRequest(BaseModel):
    delivery_date: date | None = None
    agent_id: str | None = None
    amounts: list[float] | None = None
    rationale: str | None = Field(None, max_length=500, description=S.RATIONALE_DOC)


class SimulateHistory(BaseModel):
    by: str
    quote_id: str
    weeks: list[list[float]]


@app.post("/standing-orders", tags=["standing orders"])
def create_standing_order(req: standing.StandingOrderIn):
    """A kitchen's recurring weekly order, renewed by its agent under the mandate."""
    return standing.create(req)


@app.get("/standing-orders", tags=["standing orders"])
def list_standing_orders(mandate_id: str | None = None, sandbox_id: str | None = _SANDBOX_Q,
                         r: access.Reader = Depends(access.reader)):
    access.check_sandbox_list(r, sandbox_id)
    scope = sandbox.list_scope(sandbox_id)
    if scope is None:
        return {"standing_orders": []}
    idx, ok = sandbox.index(), access.readable_mandates(r)
    return {"standing_orders": [so for so in standing.list_(mandate_id)
                                if ((mandate_id and not sandbox_id) or sandbox.keep(scope, so["mandate_id"], idx))
                                and (ok is None or so["mandate_id"] in ok)]}


@app.get("/standing-orders/{standing_id}", tags=["standing orders"], dependencies=[Depends(_read_standing)])
def get_standing_order(standing_id: str):
    """The standing order with its renewal history and the drift baseline (medians of the last renewals)."""
    return standing.get(standing_id)


@app.post("/standing-orders/{standing_id}/renew", tags=["standing orders"])
def renew_standing_order(standing_id: str, req: RenewRequest, token: str | None = Depends(agent_token)):
    """Quote this week's order live, check it for drift (quantity creep, cost, product switch, supply), order it.

    Any drift lifts the order to at least ask: it waits for a human with the drift findings."""
    return standing.renew(standing_id, req.delivery_date, agent_id=req.agent_id, amounts=req.amounts,
                          agent_token=token, rationale=req.rationale)


@app.post("/standing-orders/{standing_id}/pause", tags=["standing orders"], dependencies=[Depends(require_approver)])
def pause_standing_order(standing_id: str, d: Decision):
    return standing.set_active(standing_id, False, d.by)


@app.post("/standing-orders/{standing_id}/resume", tags=["standing orders"], dependencies=[Depends(require_approver)])
def resume_standing_order(standing_id: str, d: Decision):
    return standing.set_active(standing_id, True, d.by)


@app.post("/standing-orders/{standing_id}/simulate-history", tags=["standing orders"],
          dependencies=[Depends(require_approver)])
def simulate_standing_history(standing_id: str, req: SimulateHistory):
    """Demo only: store past renewals marked simulated=True, amounts given here, priced at a live signed quote."""
    return standing.simulate_history(standing_id, req.quote_id, req.weeks, req.by)


# --- notifications ----------------------------------------------------------

@app.get("/notifications", tags=["notifications"])
def list_notifications(buyer: str | None = None, order_id: str | None = None, limit: int = Query(50, ge=1, le=500),
                       sandbox_id: str | None = _SANDBOX_Q, r: access.Reader = Depends(access.reader)):
    """In-app feed of FYIs (notify tier), approval requests (ask tier) and decisions, newest first.
    Only buyers you may read (see GET /mandates)."""
    from . import notifications_store
    access.check_sandbox_list(r, sandbox_id)
    scope = sandbox.list_scope(sandbox_id)
    if scope is None:
        return {"notifications": []}
    where = None if order_id and not sandbox_id else sandbox.where(scope, {"buyer": "buyer"})
    where = access.and_where(where, access.buyer_where(r))
    return {"notifications": notifications_store.list_(buyer, limit, order_id, where=where)}


class NotificationTest(BaseModel):
    mandate_id: str


@app.post("/notifications/test", tags=["notifications"], dependencies=[Depends(require_approver)])
def test_notification(t: NotificationTest):
    """Send a test notification through every channel configured for this mandate (feed, ntfy, webhook)."""
    from . import notify
    m = S._mandate(t.mandate_id)
    notify.send("test", {"id": None, "buyer": m.buyer, "mandate_id": m.id}, m)
    return {"ok": True, "channels": ["feed", *sorted(notify.channels(m, "buyer"))]}


# --- judge sandbox ----------------------------------------------------------

@app.post("/sandbox", tags=["sandbox"])
def create_sandbox(request: Request):
    """Try Sous yourself: private copies of the hotel, hospital and school demo mandates for 24 h, an agent
    token for them, an approver token for their orders only, and copy-paste quickstarts (MCP, curl, UIs).

    No login; at most a few sandboxes per IP per hour. Tokens are shown once. Each sandbox has a small budget of
    live Valio Aimo lookups (quotes_limit); cached offers are free."""
    return sandbox.create(_client_ip(request), str(request.base_url))


@app.get("/sandbox/{sandbox_id}", tags=["sandbox"])
def get_sandbox(sandbox_id: str, r: access.Reader = Depends(access.reader)):
    """A sandbox's status: active / expired / deleted, live lookups used, its mandates and order counts.
    The recent orders list needs the sandbox's approver token (or agent token, or the approver code)."""
    out = sandbox.get(sandbox_id)
    if not r.everything and not set(m["mandate_id"] for m in out["mandates"]) & r.mandates:
        out["orders"].pop("recent", None)
        out["orders"]["recent_hidden"] = "send the sandbox's approver or agent credential, or the approver code"
    return out


@app.delete("/sandbox/{sandbox_id}", tags=["sandbox"], dependencies=[Depends(require_global_approver)])
def delete_sandbox(sandbox_id: str, by: str = "approver"):
    """Approver only: end a sandbox now (its mandates are revoked; its audit events stay in the chain)."""
    return sandbox.delete(sandbox_id, by)


# --- audit ------------------------------------------------------------------

@app.get("/audit", tags=["audit"])
def audit(subject: str | None = None, buyer: str | None = None, limit: int = Query(200, ge=1, le=20000),
          sandbox_id: str | None = _SANDBOX_Q, r: access.Reader = Depends(access.reader)):
    """The audit log, newest first. Both the buyer and Valio Aimo read the same record.

    Judge-sandbox events are in the same hash chain but hidden here unless ?sandbox=<id> (or a subject) is given.
    Only buyers you may read (see GET /mandates); the approver code reads everything. GET /audit/verify is public."""
    access.check_sandbox_list(r, sandbox_id)
    scope = sandbox.list_scope(sandbox_id)
    if scope is None:
        return {"events": []}
    where = None if subject and not sandbox_id else sandbox.where(scope, {"buyer": "buyer", "subject": "subject"})
    where = access.and_where(where, access.buyer_where(r, subject_col="subject"))
    return {"events": store.audit_events(subject, buyer, limit, where=where)}


@app.get("/audit/verify", tags=["audit"])
def audit_verify():
    """Recompute the hash chain: proves no event was edited or deleted."""
    return store.verify_chain()
