"""REST API (OpenAPI at /openapi.json, docs at /docs) with the MCP server mounted at /mcp."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib.metadata
import os
import subprocess
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import time
from collections import defaultdict, deque

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from . import access, autonomy, account, agent_auth, aimo, catalog, clock, config, erp, exposure, holds, lost_demand, oauth, offers, origin as origin_mod, pause, peppol, proof, quote as Q, rules, sandbox, service as S, signing, standing, store
from .mandate import Mandate
from .mcp_server import mcp

mcp_app = mcp.streamable_http_app(
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    access.startup_check()  # fail closed: no prod/staging/demo gateway without an approver code
    catalog._db()  # build the index on first start
    sweeper = asyncio.create_task(_sweep_overdue())
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(mcp_app.router.lifespan_context(mcp_app))
        yield
    sweeper.cancel()


def _background(fn):
    """Run a sweep as background work for the Valio budget (aimo.priority): never the interactive slice."""
    with aimo.background():
        return fn()


async def _sweep_overdue(every: float = 60):
    """Held orders past their approve_by become expired_cutoff (and the buyer is told) within a minute."""
    while True:
        try:
            await asyncio.to_thread(_background, S.sweep_overdue)
        except Exception:  # noqa: BLE001 - reads expire lazily too; never kill the app over the sweep
            pass
        try:  # orders whose hand-off to Valio's ERP failed are retried from the outbox (sous/erp.py)
            await asyncio.to_thread(_background, erp.process_due)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(every)


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
        "Mapping and gaps: https://github.com/Zawaer/sous-gateway/blob/main/docs/STANDARDS.md"
    ),
    lifespan=lifespan,
)
# /connect/mcp (OAuth for claude.ai / ChatGPT connectors, sous/oauth.py). Registered before CORS so CORS wraps it
# and its 401 carries the CORS headers a browser-based client needs to read WWW-Authenticate.
app.middleware("http")(oauth.connect_gate)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["Mcp-Session-Id", "Mcp-Protocol-Version", "WWW-Authenticate"])
app.include_router(oauth.router)


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



# /agent and /agent/ always open the buyer-agent page, mounted or not (prod 404'd, staging 307'd to a 404).
# Declared before the mount, so they win over it; the agent's API stays under /agent/<route>.
@app.get("/agent", include_in_schema=False)
@app.get("/agent/", include_in_schema=False)
def agent_page():
    return RedirectResponse("/ui/agent.html" if (WEB_DIR / "agent.html").is_file() else "/ui/")


# The reference BUYER agent (agent/server.py) is not part of the gateway; mounted only for the demo page.
if config.env("BUYER_AGENT") == "1":
    from agent.server import app as buyer_agent_app
    app.mount("/agent", buyer_agent_app)


@app.get("/", include_in_schema=False)
def root():
    """The live lander (web/live.html): the gateway's orders as a flow, for judges before they try it."""
    if not WEB_DIR.is_dir():
        return RedirectResponse("/docs")
    return RedirectResponse("/ui/live.html" if (WEB_DIR / "live.html").is_file() else "/ui/")


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
    if path.endswith(("/approve", "/reject", "/approve-with-changes")):
        presented = [t for t in (agent_auth.bearer(authorization), x_approver_code)
                     if t and t.startswith(agent_auth.PREFIX)]
        if presented:
            raise S.refuse_agent_approval(request.path_params.get("order_id", ""), presented[0],
                                          "reject" if path.endswith("/reject") else "approve")
    if config.APPROVER_CODE and access.approver_code_ok(x_approver_code):
        return "global approver code"
    order_id = request.path_params.get("order_id")
    if path.endswith(("/approve", "/reject", "/approve-with-changes")) and sandbox.may_approve(x_approver_code, order_id):
        o = store.get_order(order_id) or {}
        return f"sandbox approver {sandbox.owner(o.get('mandate_id'))}"
    if config.APPROVER_CODE:
        raise HTTPException(401, "Human approver code required (X-Approver-Code header).")
    return "open (no approver code configured)"


def require_global_approver(x_approver_code: str | None = Header(default=None)):
    if config.APPROVER_CODE and not access.approver_code_ok(x_approver_code):
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
    problems, dates = [], False
    for err in e.errors():
        where = ".".join(str(x) for x in err.get("loc", ()) if x not in ("body", "query", "path"))
        kind = err.get("type") or ""
        msg = ("unknown field (not part of this request; check the spelling)" if kind == "extra_forbidden" else
               "required field is missing" if kind == "missing" else err.get("msg"))
        problems.append(f"{where}: {msg}" if where else str(msg))
        dates = dates or kind.startswith(("date", "datetime")) or "date" in where.rsplit(".", 1)[-1]
    # The date hint only when a date field is what failed: otherwise it hides the real problem.
    return JSONResponse(status_code=422, content={
        "error": "invalid_request",
        "message": "; ".join(problems) + (". Dates are YYYY-MM-DD (e.g. delivery_date=2026-11-16)." if dates else "."),
        "fields": sorted({".".join(str(x) for x in err.get("loc", ()) if x not in ("body", "query", "path"))
                          for err in e.errors()} - {""}),
        "details": jsonable_encoder(e.errors())})


def _proof_public() -> bool:
    """The evidence stays private until submission (other teams read each other's pages):
    SOUS_PROOF_PUBLIC=1 publishes it; until then only the approver code reads /proof/data."""
    return (config.env("PROOF_PUBLIC", "") or "").strip().lower() in ("1", "true", "yes")


_PROOF_SOON = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Evidence · Sous</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f8f8f3;color:#000;
font:16px/1.5 system-ui,sans-serif}@media(prefers-color-scheme:dark){body{background:#141412;color:#f2f1ea}}
main{max-width:30rem;padding:24px}h1{font:400 2rem Georgia,serif;margin:0 0 8px}a{color:inherit}</style></head>
<body><main><h1>Evidence</h1><p>Our evaluation results are published with our submission.</p>
<p><a href="/ui/">Seller UI</a> · <a href="/docs">API</a></p></main>
<script>try{const c=localStorage.getItem("vk-approver-code");if(c)fetch("/proof/data",{headers:{"x-approver-code":c}})
.then(r=>{if(r.ok)location.replace("/proof?preview=1")})}catch(e){}</script></body></html>"""


# --- Monday rush (the live view's demo) ---------------------------------------------------------------------------
import json as _json  # noqa: E402
import sys as _sys  # noqa: E402
import threading as _threading  # noqa: E402

_RUSH = {"lock": _threading.Lock(), "at": 0.0, "result": None}
RUSH_CACHE_S = 60
RUSH_ARGS = ["--agents", "40", "--stock", "300", "--policy", "pro_rata", "--think-ms", "300,2500", "--json"]


@app.post("/rush/run", tags=["demo"])
def rush_run():
    """The Monday rush, for the live view (web/live.html): 40 buyers' agents ask for one short product (300 packs of
    butter, 700 asked) in the same second, through the real gateway code (quotes, mandates, stock reservations, pro
    rata share by order history). Offline and isolated: a fake Valio stock and a throwaway database in a separate
    process (scripts/rush.py), so nothing touches live orders or Valio's API. One run at a time; a run from the last
    60 s is shared. Returns the result with each buyer's timeline (events: t_ms, step, status, qty)."""
    if _RUSH["result"] and time.time() - _RUSH["at"] < RUSH_CACHE_S:
        return {**_RUSH["result"], "cached": True}
    if not _RUSH["lock"].acquire(blocking=False):
        raise HTTPException(429, "A rush is running right now; try again in a few seconds.")
    try:
        p = subprocess.run([_sys.executable, "-m", "scripts.rush", *RUSH_ARGS], cwd=config.ROOT, capture_output=True,
                           text=True, timeout=90)
        if p.returncode != 0 or not p.stdout.strip():
            raise HTTPException(503, "The rush could not run: " + (p.stderr.strip().splitlines() or ["no output"])[-1][:200])
        r = _json.loads(p.stdout.strip().splitlines()[-1])
        r["ran_at"] = clock.now().isoformat(timespec="seconds")
        _RUSH.update(at=time.time(), result=r)
        return {**r, "cached": False}
    except subprocess.TimeoutExpired:
        raise HTTPException(503, "The rush took too long; try again.")
    finally:
        _RUSH["lock"].release()


@app.get("/proof", include_in_schema=False)
def proof_page(preview: bool = False):
    """The evidence page for judges (web/proof.html); it renders GET /proof/data. Until SOUS_PROOF_PUBLIC is set,
    a short note instead (team members who entered the approver code in /ui are sent on to the preview)."""
    page = WEB_DIR / "proof.html"
    if not _proof_public() and not preview:
        return HTMLResponse(_PROOF_SOON)
    if not page.is_file():
        return RedirectResponse("/proof/data")
    return FileResponse(page, media_type="text/html")


@app.get("/proof/data", tags=["meta"])
def proof_data(x_approver_code: str | None = Header(default=None)):
    """Our evidence, each figure with its source file and what that file records about its run (scripted eval,
    LLM agents per model, drafting, recipe, vision, conformance, rule count, allergen-data audit of the loaded
    catalog). Read from files and the loaded catalog only; computed once per process. A missing source is
    reported as not available, never as a number. Private until submission (SOUS_PROOF_PUBLIC); before that it
    needs the approver code."""
    if not _proof_public() and config.APPROVER_CODE and not access.approver_code_ok(x_approver_code):
        return JSONResponse(status_code=403, content={
            "error": "not_yet_public", "message": "The evidence is published with our submission."})
    return proof.data()


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


@app.get("/orders/{order_id}/peppol", tags=["standards"], dependencies=[Depends(_read_order)],
         response_class=Response, responses={200: {"content": {"application/xml": {}}}})
def order_peppol(order_id: str, request: Request, doc: Literal["order", "order_response"] = "order"):
    """The order as Peppol BIS Ordering 3.0 UBL 2.1 XML: an Order (doc=order, `ordered` orders) or a header-level
    Order Response (doc=order_response: AP for ordered, RE for refused/rejected/expired). docs/STANDARDS.md."""
    build = STD.peppol_order if doc == "order" else STD.peppol_order_response
    return Response(build(order_id, STD.base_url(str(request.base_url))), media_type="application/xml")


@functools.cache
def _build() -> dict:
    """Which build is running: `version` from pyproject (the installed package), `commit` from SOUS_COMMIT
    (the Dockerfile's SOUS_COMMIT build arg; "unknown" when not passed), else the checkout's git HEAD."""
    try:
        version = importlib.metadata.version("sous")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    commit = os.environ.get("SOUS_COMMIT", "").strip()
    if not commit:
        try:
            root = Path(__file__).resolve().parent.parent
            commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True,
                                    text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            commit = ""
    return {"version": version, "commit": commit or "unknown"}


@app.get("/robots.txt", include_in_schema=False)
def robots():
    # Demo and sandbox deployments: keep search engines out (reached by direct link only).
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")


@app.get("/health", tags=["meta"])
def health():
    return {"ok": True, **_build(), "clock": clock.info(), "valio_api_calls": aimo.stats, "approver_code_required": bool(config.APPROVER_CODE),
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
    origin: list[str] = Query(default=[]),
):
    """Search the static catalog (fi/sv/en with synonyms). No live price or stock: request a quote for that.

    food_only: default ranks food first; true drops non-food; false = no preference.
    origin: keep only products declared from these countries or groups (fi, se, EU, NORDIC ...); the country is
    supplier-declared and products without one drop out."""
    try:
        origins = origin_mod.normalize(origin) if origin else None
    except ValueError as e:
        raise S.GatewayError("invalid_request", str(e), 422)
    return S.search(agent_id, q, claims=claims or None, avoid_allergens=avoid_allergens or None,
                    labels=labels or None, temperature=temperature, category=category, limit=limit,
                    food_only=food_only, origins=origins)


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
    delivery_date: date | None = Query(None, description="Defaults to the next delivery day (said in the answer)"),
    amount: float = 1,
    unit: Annotated[Q.Unit, Query(description=Q.UNIT_DOC)] = "SALES_UNIT",
    mandate_id: str | None = None,
    avoid_allergens: list[str] = Query(default=[]),
    limit: int = Query(3, ge=1, le=6),
    agent_id: str = "anonymous-agent",
    token: str | None = Depends(agent_token),
    r: access.Reader = Depends(access.reader),
    purpose: str | None = Query(None, max_length=120, description="What the product is for ('coffee', 'recipe: "
                                "lasagne'); with the mandate's product_purposes it decides how strict substitution is"),
    substitution: Literal["exact", "ask", "allowed"] | None = None,
):
    """Good / better / best substitutes with their compromises and live offers. With mandate_id the call is
    read-scoped like GET /mandates/{id} (docs/PROTOCOL.md section 22). For an `exact` product (by the mandate's
    product_purposes or the purpose given) there are none; for `ask`, every option needs the buyer's yes."""
    return S.suggest_substitutes(agent_id, product_id, delivery_date, amount, unit, mandate_id,
                                 avoid_allergens or None, limit, agent_token=token, reader=r, purpose=purpose,
                                 substitution=substitution)


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
def list_orders(status: Literal["ordered", "pending_approval", "refused", "rejected", "expired", "expired_cutoff"] | None = None,
                mandate_id: str | None = None, sandbox_id: str | None = _SANDBOX_Q,
                r: access.Reader = Depends(access.reader)):
    """Orders you may read (see docs/PROTOCOL.md "Read access"), newest first."""
    access.check_sandbox_list(r, sandbox_id)
    scope = sandbox.list_scope(sandbox_id)
    if scope is None:
        return {"orders": []}
    S.sweep_overdue()  # held orders past approve_by show as expired_cutoff
    idx, ok = sandbox.index(), access.readable_mandates(r)
    return {"orders": [access.label_order(o) for o in store.list_orders(status, mandate_id)
                       if ((mandate_id and not sandbox_id) or sandbox.keep(scope, o["mandate_id"], idx))
                       and (ok is None or o["mandate_id"] in ok)]}


@app.get("/orders/{order_id}/peppol/order.xml", tags=["standards"], dependencies=[Depends(_read_order)])
def order_peppol(order_id: str):
    """The order as a Peppol BIS Ordering 3.0 UBL Order (passes the UBL 2.1 XSD and Peppol T01 schematron in CI)."""
    return Response(peppol.order_xml(S.get_order(order_id)), media_type="application/xml")


@app.get("/orders/{order_id}/peppol/order-response.xml", tags=["standards"], dependencies=[Depends(_read_order)])
def order_response_peppol(order_id: str):
    """Valio Aimo's Peppol BIS Order Response: AP accepted, AB held for the buyer's human, RE refused/rejected/expired,
    with the rules that fired as the note."""
    return Response(peppol.order_response_xml(S.get_order(order_id)), media_type="application/xml")


@app.get("/orders/{order_id}", tags=["ordering"], dependencies=[Depends(_read_order)])
def get_order(order_id: str):
    """The order. agent_claim labels the agent's rationale: its own words, unverified, never used by a rule."""
    return access.label_order(S.get_order(order_id))


@app.get("/orders/{order_id}/erp", tags=["ordering"], dependencies=[Depends(_read_order)])
def order_erp(order_id: str):
    """The ORDERS05-shaped document handed to Valio Aimo's ERP for this order and its status: pending, sent,
    confirmed or failed (retried from the outbox); with the default stub, `simulated` (SIM- id: nothing reached
    Valio, which has no order API in this challenge). 404 erp_not_sent: not ordered, or ordered
    before the adapter existed. See docs/INTEGRATION.md."""
    S.get_order(order_id)
    if (d := erp.document(order_id)) is None:
        raise S.GatewayError("erp_not_sent", f"Order {order_id} has not been handed to the ERP.", 404)
    return d


@app.get("/accounts/{customer_number}", tags=["ordering"], dependencies=[Depends(require_global_approver)])
def get_account(customer_number: str):
    """Valio Aimo's (mock) customer account: credit limit, open balance and credit left, payment block, delivery
    days and route (sous/account.py). Global approver only."""
    a = account.backend.get(customer_number)
    if a is None:
        raise S.GatewayError("account_not_found", f"No customer account {customer_number}.", 404)
    return {**a.model_dump(mode="json"), "credit": account.credit(a, clock.now().astimezone(config.TZ).date())}


class AccountUpdate(BaseModel):
    credit_limit_eur: float | None = None
    open_balance_eur: float | None = None
    payment_block: bool | None = None
    payment_block_reason: str | None = Field(None, max_length=200)
    delivery_days: list[Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]] | None = None


@app.patch("/accounts/{customer_number}", tags=["ordering"], dependencies=[Depends(require_global_approver)])
def patch_account(customer_number: str, u: AccountUpdate):
    """Change the MOCK customer account (demo of account.credit_limit / payment_block / delivery_day). In
    production these facts come from Valio's customer master and credit management, not from this call."""
    a = account.backend.get(customer_number)
    if a is None:
        raise S.GatewayError("account_not_found", f"No customer account {customer_number}.", 404)
    changes = u.model_dump(exclude_unset=True)
    a = account.backend.put(account.Account(**{**a.model_dump(), **changes}))
    store.audit("account.updated", "valio", "human", customer_number, a.buyer, {"changes": changes})
    return a.model_dump(mode="json")


@app.get("/orders/{order_id}/history", tags=["audit"], dependencies=[Depends(_read_order)])
def order_history(order_id: str):
    """Every version of the order and the mandate AS OF each decision (MariaDB system-versioned tables)."""
    return S.order_history(order_id)


from . import remembered as RM  # noqa: E402  approve and remember (sous/remembered.py)


class Decision(BaseModel):
    by: str
    note: str | None = None
    remember_allergen_clearance: bool = Field(False, description=(
        "Approve only. The approver confirms the products this order was held for because they have no allergen "
        "data (allergen.unknown) are free of what the mandate avoids, and asks the gateway to remember it for this "
        "buyer: the next order of the same product is not held for that again (rule allergen.cleared). Per "
        "mandate and product; voided when the product data changes; expires after 90 days; DELETE /clearances/{id}."))
    remember: list[RM.RememberIn] = Field(default_factory=list, max_length=20, description=(
        "Approve only: \"approve and remember\" (sous/remembered.py). Each entry names one ask-tier finding of this "
        "held order by {rule, product_id}: substitution (product_id = the substitute), product.first_time, "
        "unit.interpreted or product.type_mismatch; optional days (default 30, max 90), max_quantity and "
        "max_line_total_eur. Next time the same finding under this mandate is a notify (remembered.*) instead of an "
        "ask, until it expires, the product data changes, a cap is exceeded or it is revoked "
        "(DELETE /mandates/{id}/remembered/{rid}). Never softens a block. 422 not_rememberable for any other pick."))


@app.post("/orders/{order_id}/approve", tags=["approvals"])
def approve(order_id: str, d: Decision, authority: str = Depends(require_approver)):
    """`by` is the name the approver gives; the order and audit also record which credential approved."""
    o = S.decide(order_id, d.by, True, d.note, authority=authority, remember=d.remember or None)
    if d.remember_allergen_clearance and o.get("status") == "ordered":
        from . import clearance
        o = {**o, "allergen_clearances": clearance.record_from_order(o, S._mandate(o["mandate_id"]), d.by,
                                                                     authority, d.note)}
    return o


from . import approve_changes as AC  # noqa: E402  approve with changes (sous/approve_changes.py)


@app.post("/orders/{order_id}/approve-with-changes", tags=["approvals"])
def approve_with_changes(order_id: str, req: AC.ChangesIn, authority: str = Depends(require_approver)):
    """Approve a held order with edited quantities (0 removes a line): re-quoted live, re-checked against the
    mandate, placed as a new order that this approval covers; the original is closed as replaced
    (sous/approve_changes.py). A refusal changes nothing."""
    return AC.approve_with_changes(order_id, req, authority)


@app.get("/clearances", tags=["approvals"])
def list_clearances(mandate_id: str | None = None, include_inactive: bool = False,
                    _: str = Depends(require_global_approver)):
    """Remembered allergen clearances (sous/clearance.py), newest first."""
    from . import clearance
    return {"clearances": clearance.list_for(mandate_id, active_only=not include_inactive)}


@app.delete("/clearances/{clearance_id}", tags=["approvals"])
def revoke_clearance(clearance_id: str, by: str = Query("approver"), authority: str = Depends(require_global_approver)):
    """Forget a remembered allergen clearance: the product is held for allergen.unknown again."""
    from . import clearance
    c = clearance.revoke(clearance_id, by, "global approver code")
    if not c:
        raise HTTPException(404, f"No clearance {clearance_id}.")
    return c


@app.get("/mandates/{mandate_id}/remembered", tags=["approvals"], dependencies=[Depends(_read_mandate)])
def list_remembered(mandate_id: str, include_inactive: bool = False):
    """Remembered decisions of this mandate ("approve and remember", sous/remembered.py), newest first, each with
    its JWS (typ sous-remembered+jws). Read-scoped like GET /mandates/{id}."""
    return {"mandate_id": mandate_id,
            "remembered": [RM.summary(r) for r in RM.list_for(mandate_id, active_only=not include_inactive)]}


@app.delete("/mandates/{mandate_id}/remembered/{remembered_id}", tags=["approvals"])
def revoke_remembered(mandate_id: str, remembered_id: str, by: str = Query("approver"),
                      authority: str = Depends(require_global_approver)):
    """Human-only. Forget a remembered decision: the finding asks a human again from the next quote."""
    r = RM.revoke(mandate_id, remembered_id, by, "global approver code")
    if not r:
        raise HTTPException(404, f"No remembered decision {remembered_id} for mandate {mandate_id}.")
    return RM.summary(r)


@app.post("/orders/{order_id}/reject", tags=["approvals"])
def reject(order_id: str, d: Decision, authority: str = Depends(require_approver)):
    return S.decide(order_id, d.by, False, d.note, authority=authority)


from . import amendments as AM  # noqa: E402  post-order amendments (sous/amendments.py)


def _amendment_actor(request: Request, x_approver_code: str | None = Header(default=None),
                     authorization: str | None = Header(default=None, include_in_schema=False)) -> tuple[str, bool]:
    """(authority, is_agent). A human: the global approver code, or this order's sandbox approver token (open
    when no code is configured). The buyer's agent: its vka_ credential for the order's mandate (only on
    accept/reject; reporting an amendment is Valio's)."""
    order_id = request.path_params.get("order_id", "")
    tok = next((t for t in (agent_auth.bearer(authorization), x_approver_code)
                if t and t.startswith(agent_auth.PREFIX)), None)
    if tok:
        o, who = store.get_order(order_id) or {}, agent_auth.identify(tok) or {}
        if request.url.path.rstrip("/").endswith("/amendments"):
            raise S.GatewayError("agent_cannot_amend", "Only Valio Aimo reports amendments; an agent credential "
                                 "was presented.", 403)
        if o.get("mandate_id") not in (who.get("mandate_ids") or []):
            raise S.GatewayError("agent_auth_invalid", f"The agent credential is not valid for order {order_id}.", 403)
        return f"agent credential ...{who['last4']}", True
    if config.APPROVER_CODE and access.approver_code_ok(x_approver_code):
        return "global approver code", False
    if sandbox.may_approve(x_approver_code, order_id):
        return f"sandbox approver {sandbox.owner((store.get_order(order_id) or {}).get('mandate_id'))}", False
    if config.APPROVER_CODE:
        raise HTTPException(401, "Approver code required (X-Approver-Code header), or the agent's credential "
                                 "to accept or reject an amendment.")
    return "open (no approver code configured)", False


@app.post("/orders/{order_id}/amendments", tags=["amendments"])
def report_amendment(order_id: str, req: AM.AmendmentIn, actor: tuple[str, bool] = Depends(_amendment_actor)):
    """Valio Aimo reports a post-order exception on an ordered order (short_pick, delay, substitution_at_pick,
    cancel_line). Global approver code, or the sandbox approver token for a sandbox order. Signed (typ
    sous-amendment+jws), audited (order.amended), re-checked against the mandate: notify-tier is applied at once,
    ask/block waits for the buyer (status pending_buyer). Returns the order with amendments[]."""
    return access.label_order(AM.report(order_id, req, actor[0]))


@app.post("/orders/{order_id}/amendments/{amendment_id}/accept", tags=["amendments"])
def accept_amendment(order_id: str, amendment_id: str, r: AM.Resolution,
                     actor: tuple[str, bool] = Depends(_amendment_actor)):
    """The buyer accepts a pending_buyer amendment. A human (approver code / sandbox approver token) may accept
    ask-tier; the agent (Bearer vka_...) only what the mandate as it stands lets through without a human
    (403 agent_cannot_accept otherwise); nobody a block-tier one (409 amendment_blocked)."""
    return access.label_order(AM.resolve(order_id, amendment_id, True, r, authority=actor[0], agent=actor[1]))


@app.post("/orders/{order_id}/amendments/{amendment_id}/reject", tags=["amendments"])
def reject_amendment(order_id: str, amendment_id: str, r: AM.Resolution,
                     actor: tuple[str, bool] = Depends(_amendment_actor)):
    """The buyer (human or agent) rejects a pending_buyer amendment: the line(s) concerned are cancelled with a
    credit note line."""
    return access.label_order(AM.resolve(order_id, amendment_id, False, r, authority=actor[0], agent=actor[1]))


from .passkey_routes import router as passkey_router  # noqa: E402  passkey approval (sous/passkey.py)
app.include_router(passkey_router)


# --- rule register ----------------------------------------------------------

def _rules_lang(lang: str | None, accept_language: str | None) -> str | None:
    """?lang=fi|en wins; else Accept-Language starting with fi."""
    if lang:
        return "fi" if lang.lower().startswith("fi") else None
    return "fi" if (accept_language or "").strip().lower().startswith("fi") else None


@app.get("/rules", tags=["rules"])
def rules_register(lang: str | None = Query(None, description="fi: Finnish `text` (English in text_en) and "
                                            "Finnish owners/tiers/kinds; default English, with text_fi on each rule"),
                   accept_language: str | None = Header(None)):
    """Every rule the gateway can fire: id, kind (finding or refusal), the tiers it can produce, owner (buyer
    mandate, Valio, gateway safety), plain-language text (and text_fi, Finnish) and the mandate field that sets
    it. Each decision finding links here with `rule_url`."""
    return rules.document(_rules_lang(lang, accept_language))


@app.get("/rules/{rule_id}", tags=["rules"])
def rule_detail(rule_id: str, lang: str | None = Query(None, description="fi: Finnish `text`, English in text_en"),
                accept_language: str | None = Header(None)):
    if not (r := rules.get(rule_id, _rules_lang(lang, accept_language))):
        raise S.GatewayError("rule_not_found", f"No rule {rule_id}; GET /rules lists them all.", 404)
    return r


# --- Valio pause of agent ordering (sous/pause.py) ---------------------------

@app.get("/agent-ordering", tags=["holds"])
def agent_ordering(x_approver_code: str | None = Header(default=None)):
    """Whether Valio Aimo has paused agent ordering for every buyer (with its reason) and how many single buyers are
    paused. Which buyers and why only with the global approver code: a buyer's pause can be about invoices."""
    return pause.status(full=not config.APPROVER_CODE or access.approver_code_ok(x_approver_code))


@app.post("/agent-ordering/pause", tags=["holds"], dependencies=[Depends(require_global_approver)])
def pause_agent_ordering(req: pause.PauseIn):
    """Valio's kill switch (global approver code only). Without `buyer`, every buyer's agents are stopped. Every
    order under an affected mandate is refused (rule ordering.paused), and held orders can't be approved."""
    return pause.pause(req)


@app.post("/agent-ordering/resume", tags=["holds"], dependencies=[Depends(require_global_approver)])
def resume_agent_ordering(req: pause.ResumeIn):
    """Lift a pause (the every-buyer one without `buyer`). New decisions only; refused orders stay refused."""
    if (out := pause.resume(req)) is None:
        raise HTTPException(404, f"Agent ordering is not paused for {req.buyer or 'every buyer'}.")
    return out


# --- Valio's lost-demand view (sous/lost_demand.py) ---------------------------

@app.get("/stats/lost-demand", tags=["holds"])
def lost_demand_view(days: int | None = Query(7, ge=1, le=365, description="Window in days; omit for 7"),
                     all_time: bool = False, x_approver_code: str | None = Header(default=None)):
    """What buyers' agents wanted and didn't get, and why: orders Valio couldn't supply vs refused by the buyer's
    rules vs declined or approved too late (with value), quantity no product covered, and why candidates were passed
    over. Totals are public; the per-buyer breakdown needs the global approver code."""
    full = not config.APPROVER_CODE or access.approver_code_ok(x_approver_code)
    return lost_demand.report(None if all_time else days, with_buyers=full)


# --- Valio caps on agent autonomy (sous/autonomy.py) -------------------------

@app.get("/autonomy-caps", tags=["holds"])
def autonomy_caps(x_approver_code: str | None = Header(default=None)):
    """The autonomy levels, Valio's cap for every buyer (if any) and how many single buyers are capped. Which buyers
    and why only with the global approver code."""
    return autonomy.status(full=not config.APPROVER_CODE or access.approver_code_ok(x_approver_code))


@app.post("/autonomy-caps", tags=["holds"], dependencies=[Depends(require_global_approver)])
def set_autonomy_cap(req: autonomy.CapIn):
    """Cap a buyer's agents (or every buyer's) at advise or reorder, whatever their mandate says (rule autonomy.capped)."""
    return autonomy.set_cap(req)


@app.delete("/autonomy-caps", tags=["holds"], dependencies=[Depends(require_global_approver)])
def lift_autonomy_cap(buyer: str | None = None, by: str = "valio"):
    if (out := autonomy.lift(buyer, by)) is None:
        raise HTTPException(404, f"No autonomy cap for {buyer or 'every buyer'}.")
    return out


# --- Valio product holds (sous/holds.py) --------------------------------------

@app.get("/holds", tags=["holds"])
def list_holds():
    """Products Valio Aimo has on hold right now (recall, quality hold, sales stop). Public: buyers' agents need
    it. A held product is quoted unorderable (PRODUCT_HELD) and blocks ordering and approval (rule product.held)."""
    return {"holds": [holds.public(h) for h in holds.list_active()]}


@app.post("/holds", tags=["holds"], dependencies=[Depends(require_global_approver)])
def create_hold(req: holds.HoldIn):
    """Valio staff put a product on hold (global approver code only; holds apply to every buyer and sandbox).
    Buyers with open, standing or (recall) last-14-day orders for it are notified; see `affected`."""
    p = catalog.get(req.product_id)
    if not p:
        raise HTTPException(404, f"No product {req.product_id}")
    return holds.create(req, p.get("name"))


@app.delete("/holds/{hold_id}", tags=["holds"], dependencies=[Depends(require_global_approver)])
def lift_hold(hold_id: str, by: str = "valio"):
    """Lift a hold: the product can be quoted and ordered again (new quotes; old quotes stay as signed)."""
    if not (h := holds.lift(hold_id, by)):
        raise HTTPException(404, f"No hold {hold_id}")
    return h


# --- Valio proactive offers (sous/offers.py) -----------------------------------

def _offer_issuer(r: access.Reader = Depends(access.reader)) -> str | None:
    """None for the global approver code; the sandbox id for a sandbox's sbxa_ token (its own buyers only)."""
    if r.everything:
        return None
    for sid in sorted(r.sandboxes):
        if sandbox.list_scope(sid):
            return sid
    raise HTTPException(401, "Valio side: the approver code (X-Approver-Code) or a sandbox's approver token is "
                             "required.")


@app.post("/offers", tags=["offers"])
def create_offer(req: offers.OfferIn, scope: str | None = Depends(_offer_issuer)):
    """Valio Aimo makes a signed offer (JWS typ sous-offer+jws) to buyers' agents: target mandates, a buyer or a
    segment (category); price or discount at or below Valio's current price; quantity cap; valid_until; reason.
    Mandates whose rules exclude a product (avoided allergens, categories, claims) don't get it; a buyer with 3
    active offers gets no more. Returns the offer with `mandates` (delivered) and `excluded` (with reasons)."""
    return offers.create(req, scope)


@app.get("/offers", tags=["offers"])
def list_offers(scope: str | None = Depends(_offer_issuer)):
    """Valio side: every offer in your scope (the global code: curated and sandbox ones; a sandbox token: its own),
    newest first, with use against the cap."""
    return {"offers": offers.list_valio("*" if scope is None else scope)}


@app.delete("/offers/{offer_id}", tags=["offers"])
def withdraw_offer(offer_id: str, by: str = "valio", scope: str | None = Depends(_offer_issuer)):
    """Withdraw an offer: no new quotes on it; orders on its quotes are blocked (rule offer.expired)."""
    if not (o := offers.withdraw(offer_id, by, scope)):
        raise HTTPException(404, f"No offer {offer_id}")
    return o


@app.get("/mandates/{mandate_id}/offers", tags=["mandates"], dependencies=[Depends(_read_mandate)])
def mandate_offers(mandate_id: str, include_inactive: bool = False):
    """Valio Aimo's active offers for this mandate (sponsored; read-scoped like GET /mandates/{id}). Accept one
    with POST /quotes {..., offer_id}; the mandate decides as always."""
    return offers.for_mandate(mandate_id, include_inactive)


# --- mandates ---------------------------------------------------------------

@app.post("/mandates", tags=["mandates"], dependencies=[Depends(require_approver)])
def issue_mandate(m: Mandate, mint_agent_credential: bool | None = None,
                  change_note: str | None = Query(None, max_length=200, description=(
                      "Why the buyer's needs changed, e.g. 'Ward 3 is now nut-free'; opens the needs.changed "
                      "findings when this update tightens allergen or diet constraints"))):
    """A human issues a mandate to their agent; the gateway signs it.

    With require_agent_auth (or mint_agent_credential=true) the response includes agent_credential: the agent's
    bearer token, shown this once. An update that tightens allergen or diet constraints re-checks every standing
    order, held or upcoming order and remembered decision of the mandate and returns `needs_review` (held,
    flagged, revoked, unaffected, proposed substitutes; summary and summary_fi). See sous/needs.py."""
    return S.issue_mandate(m, mint_agent_credential, change_note)


@app.get("/mandates/{mandate_id}/needs-reviews", tags=["mandates"], dependencies=[Depends(_read_mandate)])
def mandate_needs_reviews(mandate_id: str, limit: int = Query(20, ge=1, le=100)):
    """The re-checks run when this mandate's allergen or diet constraints changed (newest first): what was held,
    flagged for review or revoked, and what kept running (sous/needs.py)."""
    from . import needs
    return {"mandate_id": mandate_id, "needs_reviews": needs.list_for(mandate_id, limit)}


@app.get("/mandates", tags=["mandates"])
def list_mandates(request: Request, sandbox_id: str | None = _SANDBOX_Q, r: access.Reader = Depends(access.reader)):
    """Mandates you may read: all with the approver code, a sandbox's with its token, an agent's own with its
    bearer token, and the public demo mandates (demo_public) without credentials. The mandates your agent or
    sandbox token was issued for are always listed (even sandbox ones without ?sandbox=) and marked `yours: true`,
    so a credential answers "which mandate am I?" (MCP: my_mandates)."""
    access.check_sandbox_list(r, sandbox_id)
    scope = sandbox.list_scope(sandbox_id)
    if scope is None:
        return {"mandates": []}
    idx = sandbox.index()
    own = access.credential_mandates(request.headers.get("x-approver-code"),
                                     agent_auth.bearer(request.headers.get("authorization")))
    return {"mandates": [({**m, "yours": True} if m["id"] in own else m) for m in store.list_mandates()
                         if (m["id"] in own or sandbox.keep(scope, m["id"], idx))
                         and access.can_read_mandate(r, m["id"], m)]}


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


@app.post("/sandbox/{sandbox_id}/attacks/{name}", tags=["sandbox"])
def sandbox_attack(sandbox_id: str, name: Literal["self_approve", "slice", "injection", "tamper_quote",
                                                  "replay_other_agent", "over_limit", "wrong_allergen"],
                   token: str | None = Depends(agent_token), x_approver_code: str | None = Header(default=None)):
    """Try to break it (web/break.html): run one canned attack against your sandbox through the real gateway code,
    with seeded quotes (no Valio call, no lookup budget). Needs the sandbox's agent token (Bearer vka_...) or its
    approver token (sbxa_..., bearer or X-Approver-Code); 30 per sandbox per hour. Returns the verdict (blocked /
    held / refused), the rules (each in /rules) and the audit events; any orders are real, labelled sandbox orders."""
    from . import attacks
    return attacks.run(sandbox_id, name, token or x_approver_code)


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
def audit_verify(head: str | None = Query(default=None, pattern="^[0-9a-f]{64}$",
                                          description="A head hash you saved earlier: fails if it's no longer in the "
                                                      "chain (its newest events were deleted or rewritten)")):
    """Recompute the hash chain: proves no event was edited or deleted (pass ?head= to also catch a cut tail)."""
    return store.verify_chain(head)
