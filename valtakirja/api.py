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
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel

from . import aimo, catalog, config, service as S, signing, store
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
    title="Valtakirja: Valio Aimo agent gateway",
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


@app.exception_handler(S.GatewayError)
async def gateway_error(_: Request, e: S.GatewayError):
    return JSONResponse(status_code=e.status, content={"error": e.code, "message": e.message})


# --- discovery --------------------------------------------------------------

@app.get("/.well-known/jwks.json", tags=["trust"])
def jwks():
    """Public key for verifying quotes, mandates and order receipts (Ed25519, JWS)."""
    return signing.jwks()


@app.get("/health", tags=["meta"])
def health():
    return {"ok": True, "valio_api_calls": aimo.stats, "approver_code_required": bool(config.APPROVER_CODE),
            "storage": store.backend()}


# --- catalog ----------------------------------------------------------------

@app.get("/products", tags=["catalog"])
def search_products(
    q: str = "",
    claims: list[str] = Query(default=[]),
    avoid_allergens: list[str] = Query(default=[]),
    labels: list[str] = Query(default=[]),
    temperature: Literal["ambient", "chilled", "frozen"] | None = None,
    category: str | None = None,
    limit: int = 15,
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
    limit: int = 3,
    agent_id: str = "anonymous-agent",
):
    """Good / better / best substitutes with their compromises and live offers."""
    return S.suggest_substitutes(agent_id, product_id, delivery_date, amount, unit, mandate_id,
                                 avoid_allergens or None, limit)


# --- quotes and orders ------------------------------------------------------

@app.post("/quotes", tags=["ordering"])
def request_quote(req: S.QuoteRequest):
    """Live price + availability for a shortlist, as a signed, expiring quote (with a mandate preview)."""
    return S.request_quote(req)


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
def place_order(req: OrderRequest):
    """Order a quote. The mandate decides: ordered, pending_approval or refused, with reasons."""
    return S.place_order(req.agent_id, req.mandate_id, req.quote_id, req.idempotency_key)


@app.get("/orders", tags=["ordering"])
def list_orders(status: str | None = None, mandate_id: str | None = None):
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
def issue_mandate(m: Mandate):
    """A human issues a mandate to their agent; the gateway signs it."""
    return S.issue_mandate(m)


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
def mandate_suggestions(mandate_id: str, delivery_date: date, agent_id: str, limit: int = 5,
                        basis: list[str] = Query(default=[])):
    """Valio Aimo's own commercial suggestions (sponsored, not neutral advice): better-value alternatives to what
    the buyer orders, seasonal and new products. Offers only, never orders; pre-checked against the mandate."""
    from . import suggest
    return suggest.suggestions(mandate_id, agent_id, delivery_date, basis or None, limit)


@app.post("/mandates/{mandate_id}/revoke", tags=["mandates"], dependencies=[Depends(require_approver)])
def revoke_mandate(mandate_id: str, d: Decision):
    return S.revoke_mandate(mandate_id, d.by)


# --- notifications ----------------------------------------------------------

@app.get("/notifications", tags=["notifications"])
def list_notifications(buyer: str | None = None, order_id: str | None = None, limit: int = 50):
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
def audit(subject: str | None = None, buyer: str | None = None, limit: int = 200):
    """The audit log, newest first. Both the buyer and Valio Aimo read the same record."""
    return {"events": store.audit_events(subject, buyer, limit)}


@app.get("/audit/verify", tags=["audit"])
def audit_verify():
    """Recompute the hash chain: proves no event was edited or deleted."""
    return store.verify_chain()
