"""REST API (OpenAPI at /openapi.json, docs at /docs) with the MCP server mounted at /mcp."""

from __future__ import annotations

import contextlib
import os
from datetime import date
from typing import Literal

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
from pydantic import BaseModel

from . import aimo, catalog, config, service as S, signing, standing, store
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
    version="0.1.0",
    description=(
        "Lets external purchasing agents search Valio Aimo's catalog, get signed quotes with live price and "
        "availability, and place orders within a mandate (valtakirja) issued by their human. The mandate engine "
        "decides each order's tier: silent or notify (ordered), ask (waits for a human), block (refused). "
        "Every step is in a hash-chained audit log. The same operations are available over MCP at /mcp."
    ),
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["Mcp-Session-Id"])


@app.middleware("http")
async def mcp_without_slash(request: Request, call_next):
    """Serve /mcp and /mcp/ alike: a 307 redirect can drop https behind a TLS proxy and breaks some MCP clients."""
    if request.scope["path"] == "/mcp":
        request.scope["path"] = "/mcp/"
        request.scope["raw_path"] = b"/mcp/"
    return await call_next(request)
app.mount("/mcp", mcp_app)
WEB_DIR = config.ROOT / "web"
if WEB_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=WEB_DIR, html=True), name="ui")
BUYER_DIR = config.ROOT / "buyer"
if BUYER_DIR.is_dir():
    app.mount("/buyer", StaticFiles(directory=BUYER_DIR, html=True), name="buyer")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ui/" if WEB_DIR.is_dir() else "/docs")


_hits: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    # CF-Connecting-IP is client-controlled unless Cloudflare is the only way in: trust it only when told to.
    # Otherwise uvicorn --proxy-headers has already resolved X-Forwarded-For into request.client.
    if os.getenv("VALTAKIRJA_TRUST_CF_HEADER") == "1" and (ip := request.headers.get("cf-connecting-ip")):
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


def require_approver(x_approver_code: str | None = Header(default=None)):
    """Human-only actions. In production this is the buyer's own login (e.g. OIDC); here a shared demo code."""
    if config.APPROVER_CODE and x_approver_code != config.APPROVER_CODE:
        raise HTTPException(401, "Human approver code required (X-Approver-Code header).")


_agent_bearer = HTTPBearer(auto_error=False, scheme_name="agentToken",
                           description="Agent credential for mandates with require_agent_auth (see POST /mandates).")


def agent_token(cred: HTTPAuthorizationCredentials | None = Depends(_agent_bearer)) -> str | None:
    """The agent's credential from `Authorization: Bearer <token>` (only checked when the mandate requires it)."""
    return cred.credentials if cred else None


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


@app.get("/health", tags=["meta"])
def health():
    return {"ok": True, "valio_api_calls": aimo.stats, "approver_code_required": bool(config.APPROVER_CODE),
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
    unit: Literal["L", "KG", "SALES_UNIT"] = "SALES_UNIT",
    mandate_id: str | None = None,
    avoid_allergens: list[str] = Query(default=[]),
    limit: int = Query(3, ge=1, le=6),
    agent_id: str = "anonymous-agent",
):
    """Good / better / best substitutes with their compromises and live offers."""
    return S.suggest_substitutes(agent_id, product_id, delivery_date, amount, unit, mandate_id,
                                 avoid_allergens or None, limit)


# --- quotes and orders ------------------------------------------------------

@app.post("/quotes", tags=["ordering"])
def request_quote(req: S.QuoteRequest, token: str | None = Depends(agent_token)):
    """Live price + availability for a shortlist, as a signed, expiring quote (with a mandate preview).

    If the mandate requires agent authentication, send `Authorization: Bearer <agent token>`."""
    return S.request_quote(req, agent_token=token)


@app.get("/quotes/{quote_id}", tags=["ordering"])
def get_quote(quote_id: str):
    q = store.get_quote(quote_id)
    if not q:
        raise HTTPException(404, f"No quote {quote_id}")
    jws = q.pop("_jws")
    q.pop("_order_id")
    return {"quote": q, "jws": jws}


class OrderRequest(BaseModel):
    agent_id: str
    mandate_id: str
    quote_id: str
    idempotency_key: str | None = None


@app.post("/orders", tags=["ordering"])
def place_order(req: OrderRequest, token: str | None = Depends(agent_token)):
    """Order a quote. The mandate decides: ordered, pending_approval or refused, with reasons.

    If the mandate requires agent authentication, send `Authorization: Bearer <agent token>`
    (401 agent_auth_required without it, 403 agent_auth_invalid with a wrong one)."""
    return S.place_order(req.agent_id, req.mandate_id, req.quote_id, req.idempotency_key, agent_token=token)


@app.get("/orders", tags=["ordering"])
def list_orders(status: Literal["ordered", "pending_approval", "refused", "rejected", "expired"] | None = None,
                mandate_id: str | None = None):
    return {"orders": store.list_orders(status, mandate_id)}


@app.get("/orders/{order_id}", tags=["ordering"])
def get_order(order_id: str):
    return S.get_order(order_id)


@app.get("/orders/{order_id}/history", tags=["audit"])
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


# --- mandates ---------------------------------------------------------------

@app.post("/mandates", tags=["mandates"], dependencies=[Depends(require_approver)])
def issue_mandate(m: Mandate, mint_agent_credential: bool | None = None):
    """A human issues a mandate to their agent; the gateway signs it.

    With require_agent_auth (or mint_agent_credential=true) the response includes agent_credential: the agent's
    bearer token, shown this once."""
    return S.issue_mandate(m, mint_agent_credential)


@app.get("/mandates", tags=["mandates"])
def list_mandates():
    return {"mandates": store.list_mandates()}


@app.get("/mandates/{mandate_id}", tags=["mandates"])
def get_mandate(mandate_id: str):
    return S.get_mandate(mandate_id)


@app.get("/mandates/{mandate_id}/history", tags=["mandates"])
def mandate_history(mandate_id: str):
    """Every version of the mandate with its validity period."""
    return S.mandate_history(mandate_id)


@app.get("/mandates/{mandate_id}/suggestions", tags=["mandates"])
def mandate_suggestions(mandate_id: str, delivery_date: date, agent_id: str, limit: int = Query(5, ge=1, le=20),
                        basis: list[str] = Query(default=[])):
    """Valio Aimo's own commercial suggestions (sponsored, not neutral advice): better-value alternatives to what
    the buyer orders, seasonal and new products. Offers only, never orders; pre-checked against the mandate."""
    from . import suggest
    return suggest.suggestions(mandate_id, agent_id, delivery_date, basis or None, limit)


class CredentialAction(BaseModel):
    by: str
    action: Literal["rotate", "revoke"] = "rotate"


@app.get("/mandates/{mandate_id}/agent-credential", tags=["mandates"])
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


# --- standing orders --------------------------------------------------------

class RenewRequest(BaseModel):
    delivery_date: date | None = None
    agent_id: str | None = None
    amounts: list[float] | None = None


class SimulateHistory(BaseModel):
    by: str
    quote_id: str
    weeks: list[list[float]]


@app.post("/standing-orders", tags=["standing orders"])
def create_standing_order(req: standing.StandingOrderIn):
    """A kitchen's recurring weekly order, renewed by its agent under the mandate."""
    return standing.create(req)


@app.get("/standing-orders", tags=["standing orders"])
def list_standing_orders(mandate_id: str | None = None):
    return {"standing_orders": standing.list_(mandate_id)}


@app.get("/standing-orders/{standing_id}", tags=["standing orders"])
def get_standing_order(standing_id: str):
    """The standing order with its renewal history and the drift baseline (medians of the last renewals)."""
    return standing.get(standing_id)


@app.post("/standing-orders/{standing_id}/renew", tags=["standing orders"])
def renew_standing_order(standing_id: str, req: RenewRequest, token: str | None = Depends(agent_token)):
    """Quote this week's order live, check it for drift (quantity creep, cost, product switch, supply), order it.

    Any drift lifts the order to at least ask: it waits for a human with the drift findings."""
    return standing.renew(standing_id, req.delivery_date, agent_id=req.agent_id, amounts=req.amounts,
                          agent_token=token)


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
def list_notifications(buyer: str | None = None, order_id: str | None = None, limit: int = Query(50, ge=1, le=500)):
    """In-app feed of FYIs (notify tier), approval requests (ask tier) and decisions, newest first."""
    from . import notifications_store
    return {"notifications": notifications_store.list_(buyer, limit, order_id)}


class NotificationTest(BaseModel):
    mandate_id: str


@app.post("/notifications/test", tags=["notifications"], dependencies=[Depends(require_approver)])
def test_notification(t: NotificationTest):
    """Send a test notification through every channel configured for this mandate (feed, ntfy, webhook)."""
    from . import notify
    m = S._mandate(t.mandate_id)
    notify.send("test", {"id": None, "buyer": m.buyer, "mandate_id": m.id}, m)
    return {"ok": True, "channels": ["feed", *sorted(notify.channels(m, "buyer"))]}


# --- audit ------------------------------------------------------------------

@app.get("/audit", tags=["audit"])
def audit(subject: str | None = None, buyer: str | None = None, limit: int = Query(200, ge=1, le=5000)):
    """The audit log, newest first. Both the buyer and Valio Aimo read the same record."""
    return {"events": store.audit_events(subject, buyer, limit)}


@app.get("/audit/verify", tags=["audit"])
def audit_verify():
    """Recompute the hash chain: proves no event was edited or deleted."""
    return store.verify_chain()
