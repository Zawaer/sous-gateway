"""Reference buyer agent over HTTP, for the demo page web/agent.html.

The buyer's agent is NOT part of Valio Aimo's gateway: it runs on the buyer's side and talks to the gateway
like any other agent (here over the in-process MCP server, the same tools as /mcp). It is mounted at /agent/
by sous/api.py only when SOUS_BUYER_AGENT=1, so judges can watch the models work in a browser:

    POST /agent/plan-menu   menu text  -> Gemini (agent/recipe.py) -> ingredients -> ONE basket quote + preview
    POST /agent/read-photo  photo      -> Nemotron VL (agent/vision.py) -> lines -> ONE basket quote + preview
    POST /agent/ask         a sentence -> the LLM buyer agent (agent/buyer.py) with the gateway's tools (sandbox only)
    POST /agent/order       quote_id   -> place_order at the gateway (idempotency key from the quote)
    POST /agent/draft-mandate rules text -> a PROPOSED mandate change (agent/draft.py): diff, open questions,
                                         sources; the human checks it and signs it on web/sign-mandate.html
    GET  /agent/usage                  -> model calls, tokens and cost per provider (today and all time)

Guardrails, because these calls cost money and the gateway's Valio key is shared:
- only for judge-sandbox mandates (Authorization: Bearer <the sandbox's agent token>), or for other mandates
  with the human approver code (X-Approver-Code);
- per client IP at most SOUS_AGENT_PER_IP_HOUR model-backed requests per hour (default 10);
- a global daily model budget SOUS_AGENT_DAILY_USD (default 5), counted from the usage table;
- images: JPEG or PNG, at most SOUS_AGENT_MAX_IMAGE_MB (default 4) MB;
- model timeouts (SOUS_AGENT_LLM_TIMEOUT_S, default 45 s per call, 2 retries).
Every model-backed request writes one row to the `agent_usage` table (provider, model, calls, tokens, cost).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import threading
import time
from datetime import date, datetime
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from sous import agent_auth, config, sandbox, service as S, store

from . import draft as DR, recipe as R, vision as V

BANNER = "Reference buyer agent (runs on the buyer's side; shown here for the demo)"
PER_IP_HOUR = int(config.env("AGENT_PER_IP_HOUR", "10"))
DAILY_USD = float(config.env("AGENT_DAILY_USD", "5"))
MAX_IMAGE_BYTES = int(float(config.env("AGENT_MAX_IMAGE_MB", "4")) * 1024 * 1024)
LLM_TIMEOUT_S = float(config.env("AGENT_LLM_TIMEOUT_S", "45"))
# Cost estimate for models without a list price in agent.llm.PRICES (e.g. Nemotron on NVIDIA's API catalog),
# USD per 1M tokens in / out: deliberately on the high side, so the daily budget errs towards stopping early.
FALLBACK_PRICE = (float(config.env("AGENT_PRICE_IN", "1.0")), float(config.env("AGENT_PRICE_OUT", "4.0")))

DISPLAY = {
    "gemini-4-hydrogen": "Gemini 4 Hydrogen", "gemini-4-argon": "Gemini 4 Argon", "gemini-4-carbon": "Gemini 4 Carbon",
    "gemini-3.8-flash": "Gemini 3.8 Flash", "nvidia/nemotron-3.5-super-vl-120b-a12b": "NVIDIA Nemotron 3.5 Super VL",
    "kimi-k3-1-pro": "Kimi K3.1 Pro",
}


class AgentError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, retry_after: int | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.retry_after = code, message, status, retry_after


app = FastAPI(
    title="Sous reference buyer agent",
    version="0.1.0",
    description=(
        f"**{BANNER}.** Not part of Valio Aimo's gateway: a buyer's purchasing agent that uses Google Gemini to "
        "understand a menu and NVIDIA Nemotron 3.5 Super VL to read a photographed order list or delivery note, "
        "then asks the gateway for ONE basket quote and the mandate's preview. The models never see prices or "
        "decide anything; the gateway's mandate engine does. Allowed only for judge-sandbox mandates "
        "(Authorization: Bearer <sandbox agent token>, from POST /sandbox) or with the approver code; "
        f"{PER_IP_HOUR} model requests per IP per hour and a daily model budget of ${DAILY_USD:g}."),
)


@app.exception_handler(AgentError)
async def _agent_error(_: Request, e: AgentError):
    body = {"error": e.code, "message": e.message}
    headers = None
    if e.retry_after is not None:
        body["retry_after"] = e.retry_after
        headers = {"Retry-After": str(e.retry_after)}
    return JSONResponse(status_code=e.status, content=body, headers=headers)


@app.exception_handler(S.GatewayError)
async def _gateway_error(_: Request, e: S.GatewayError):
    return JSONResponse(status_code=e.status, content={"error": e.code, "message": e.message})


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, e: StarletteHTTPException):
    return JSONResponse(status_code=e.status_code, content={"error": f"http_{e.status_code}", "message": str(e.detail)})


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, e: RequestValidationError):
    problems = []
    for err in e.errors():
        where = ".".join(str(x) for x in err.get("loc", ()) if x not in ("body", "query", "path"))
        problems.append(f"{where}: {err.get('msg')}" if where else str(err.get("msg")))
    return JSONResponse(status_code=422, content={"error": "invalid_request", "message": "; ".join(problems)})


# --- usage table --------------------------------------------------------------------------------------

_SQLITE = """CREATE TABLE IF NOT EXISTS agent_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, day TEXT NOT NULL, endpoint TEXT NOT NULL,
  provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
  prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0, cost_estimated INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
  ip_sha256 TEXT NOT NULL, mandate_id TEXT)"""
_MARIADB = """CREATE TABLE IF NOT EXISTS agent_usage (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, ts DOUBLE NOT NULL, day VARCHAR(10) NOT NULL, endpoint VARCHAR(32) NOT NULL,
  provider VARCHAR(32) NOT NULL, model VARCHAR(128) NOT NULL, status VARCHAR(16) NOT NULL, calls INT NOT NULL DEFAULT 0,
  prompt_tokens INT NOT NULL DEFAULT 0, completion_tokens INT NOT NULL DEFAULT 0,
  cost_usd DOUBLE NOT NULL DEFAULT 0, cost_estimated TINYINT NOT NULL DEFAULT 0, latency_ms INT NOT NULL DEFAULT 0,
  ip_sha256 CHAR(64) NOT NULL, mandate_id VARCHAR(80), INDEX (day), INDEX (ip_sha256, ts))"""
_ready_for = None
_reserve_lock = threading.Lock()
_clock = time.time  # patched in tests


def _ensure() -> None:
    global _ready_for
    with store._lock:
        conn = store.db()
        if _ready_for is conn:
            return
        store._exec(_MARIADB if config.DB_URL else _SQLITE)
        _ready_for = conn


def _today() -> str:
    return datetime.fromtimestamp(_clock(), config.TZ).date().isoformat()


def spent_today() -> float:
    _ensure()
    rows = store._exec("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM agent_usage WHERE day = ?", (_today(),))[0]
    return float(rows[0]["s"] or 0)


def _reserve(ip: str, endpoint: str, provider: str, model: str, mandate_id: str) -> int:
    """Check the per-IP limit and the daily budget, then record a pending row (counts towards the IP's limit)."""
    _ensure()
    now = _clock()
    ip_hash = hashlib.sha256(f"sous-agent:{ip}".encode()).hexdigest()
    with _reserve_lock:
        recent = [r["ts"] for r in store._exec(
            "SELECT ts FROM agent_usage WHERE ip_sha256 = ? AND ts > ?", (ip_hash, now - 3600))[0]]
        if len(recent) >= PER_IP_HOUR:
            wait = int(min(recent) + 3600 - now) + 1
            raise AgentError("agent_rate_limited",
                             f"This demo allows {PER_IP_HOUR} model requests per hour from one address; "
                             f"try again in {max(1, wait // 60)} min.", 429, wait)
        spent = spent_today()
        if spent >= DAILY_USD:
            raise AgentError("agent_budget_exhausted",
                             f"Today's model budget for the demo agent (${DAILY_USD:g}) is used up "
                             f"(${spent:.2f}); it resets at midnight Helsinki time.", 429, 3600)
        _, _, rid = store._exec(
            "INSERT INTO agent_usage (ts, day, endpoint, provider, model, status, ip_sha256, mandate_id) "
            "VALUES (?,?,?,?,?,?,?,?)", (now, _today(), endpoint, provider, model, "pending", ip_hash, mandate_id))
    return rid


def _finish(rid: int, meter: "Meter", status: str) -> None:
    store._exec("UPDATE agent_usage SET status = ?, calls = ?, prompt_tokens = ?, completion_tokens = ?, "
                "cost_usd = ?, cost_estimated = ?, latency_ms = ? WHERE id = ?",
                (status, meter.calls, meter.prompt_tokens, meter.completion_tokens, meter.cost_usd,
                 int(meter.cost_estimated), meter.latency_ms, rid))


def usage_totals() -> dict:
    _ensure()
    rows = store._exec(
        "SELECT provider, model, COUNT(*) AS requests, SUM(calls) AS calls, SUM(prompt_tokens) AS pin, "
        "SUM(completion_tokens) AS pout, SUM(cost_usd) AS cost, MAX(cost_estimated) AS est, "
        "SUM(CASE WHEN day = ? THEN cost_usd ELSE 0 END) AS cost_today, "
        "SUM(CASE WHEN day = ? THEN 1 ELSE 0 END) AS requests_today, MIN(day) AS since "
        "FROM agent_usage WHERE calls > 0 GROUP BY provider, model ORDER BY provider, model",
        (_today(), _today()))[0]
    providers = [{"provider": r["provider"], "model": r["model"], "display": DISPLAY.get(r["model"], r["model"]),
                  "requests": int(r["requests"]), "model_calls": int(r["calls"] or 0),
                  "prompt_tokens": int(r["pin"] or 0), "completion_tokens": int(r["pout"] or 0),
                  "cost_usd": round(float(r["cost"] or 0), 4), "cost_estimated": bool(r["est"]),
                  "requests_today": int(r["requests_today"] or 0),
                  "cost_usd_today": round(float(r["cost_today"] or 0), 4), "since": r["since"]} for r in rows]
    return {"providers": providers,
            "total_cost_usd": round(sum(p["cost_usd"] for p in providers), 4),
            "today": {"day": _today(), "spent_usd": round(spent_today(), 4), "budget_usd": DAILY_USD},
            "limits": {"per_ip_per_hour": PER_IP_HOUR, "max_image_mb": MAX_IMAGE_BYTES / 1024 / 1024},
            "note": "cost_estimated: the model has no list price in agent/llm.py; estimated at "
                    f"${FALLBACK_PRICE[0]:g} / ${FALLBACK_PRICE[1]:g} per 1M tokens in / out."}


# --- models ---------------------------------------------------------------------------------------------

class Meter:
    """Wraps a ChatClient: counts calls, tokens, latency and cost of everything the chain asks the model."""

    def __init__(self, llm):
        self.llm = llm
        self.cfg = llm.cfg
        self.calls = self.prompt_tokens = self.completion_tokens = self.latency_ms = 0

    def chat(self, messages, tools=None, response_format=None):
        t0 = time.perf_counter()
        try:
            r = (self.llm.chat(messages, tools=tools, response_format=response_format) if response_format
                 else self.llm.chat(messages, tools=tools))
        finally:
            self.calls += 1
            self.latency_ms += round((time.perf_counter() - t0) * 1000)
        self.prompt_tokens += r.usage.get("prompt_tokens", 0)
        self.completion_tokens += r.usage.get("completion_tokens", 0)
        return r

    @property
    def cost_estimated(self) -> bool:
        return self.cfg.price_in is None or self.cfg.price_out is None

    @property
    def cost_usd(self) -> float:
        c = self.cfg.cost_usd(self.prompt_tokens, self.completion_tokens)
        if c is None:
            c = (self.prompt_tokens * FALLBACK_PRICE[0] + self.completion_tokens * FALLBACK_PRICE[1]) / 1e6
        return round(c, 5)

    def info(self) -> dict:
        return {"provider": self.cfg.provider, "model": self.cfg.model,
                "display": DISPLAY.get(self.cfg.model, self.cfg.model), "where": self.cfg.where,
                "calls": self.calls, "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "latency_ms": self.latency_ms, "cost_usd": self.cost_usd, "cost_estimated": self.cost_estimated}


def _preset_env(provider: str) -> dict:
    """Only the provider's own key: the server ignores LLM_PROVIDER / LLM_MODEL / LLM_BASE_URL overrides, so the
    page always runs (and names) the preset model. Empty when the key is not set."""
    from .llm import PRESETS
    key_env = PRESETS[provider]["key_env"]
    return {key_env: os.environ[key_env]} if os.environ.get(key_env) else {}


def make_llm(provider: str):
    """A ChatClient with the server's timeouts, or None when this server has no key for the provider."""
    from .llm import PRESETS, ChatClient, config_from_env
    env = _preset_env(provider)
    if not env:
        return None
    cfg = config_from_env(provider, PRESETS[provider]["model"], env=env)
    cfg.timeout_s, cfg.max_retries = LLM_TIMEOUT_S, 2
    return ChatClient(cfg)


@contextlib.contextmanager
def gateway(agent_token: str | None):
    """The gateway as the buyer's agent sees it: its MCP tools (in-process here), with the agent's credential."""
    from sous.mcp_server import mcp
    from .buyer import MCPGateway
    with MCPGateway(mcp, timeout_s=180) as gw:
        gw._agent_token = agent_token or None  # never the server's own $SOUS_AGENT_TOKEN
        yield gw


# --- access -----------------------------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False, scheme_name="sandboxAgentToken",
                     description="The judge sandbox's agent token (POST /sandbox), for its mandates.")


def _token(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str | None:
    return cred.credentials if cred else None


def _client_ip(request: Request) -> str:
    if config.env("TRUST_CF_HEADER") == "1" and (ip := request.headers.get("cf-connecting-ip")):
        return ip
    return request.client.host if request.client else "?"


def authorize(mandate_id: str, agent_id: str, token: str | None, approver_code: str | None, operation: str,
              sandbox_only: bool = False):
    """Sandbox mandates: the sandbox's agent token. Any other mandate: the human approver code (unless
    sandbox_only)."""
    m = S._mandate(mandate_id)  # 404 mandate_not_found
    if m.agent_id != agent_id:
        raise AgentError("agent_mismatch", f"Mandate {mandate_id} is issued to agent '{m.agent_id}', not '{agent_id}'.",
                         403)
    sid = sandbox.owner(mandate_id)
    if sid:
        row = sandbox._row(sid)
        if row is None or sandbox._state(row) != "active":
            raise AgentError("sandbox_expired", f"Sandbox {sid} has expired or was closed; create a new one.", 410)
        try:  # the sandbox's agent token (its approver token can't act as the agent at the gateway)
            agent_auth.check(m, agent_id, token, operation)
        except agent_auth.AuthError as e:
            raise AgentError(e.code, e.message + " (use the agent_token from POST /sandbox)", e.status) from None
        return m
    if sandbox_only:
        raise AgentError("sandbox_required", "This endpoint runs only for judge-sandbox mandates: create one "
                                             "(POST /sandbox) and send its agent token as Authorization: Bearer.", 403)
    if config.APPROVER_CODE and not (approver_code and hmac.compare_digest(approver_code, config.APPROVER_CODE)):
        raise AgentError("sandbox_or_approver_required",
                         "The demo agent runs only for judge-sandbox mandates (create one: POST /sandbox, then send "
                         "its agent token as Authorization: Bearer) or with the approver code (X-Approver-Code).", 403)
    return m


def _display(label: str) -> str:
    return DISPLAY.get(label.split(":")[-1], label)


# --- endpoints ------------------------------------------------------------------------------------------

class PlanMenuIn(BaseModel):
    text: str = Field(..., min_length=3, max_length=1000, examples=["Lasagne for 120 on Monday, we have 10 kg flour"])
    mandate_id: str
    agent_id: str
    delivery_date: date | None = None
    provider: Literal["gemini", "table"] = Field("gemini", description="gemini (default) or table (no model)")


def _quote_view(q: dict | None) -> dict | None:
    if not q:
        return None
    return {**{k: q.get(k) for k in ("id", "expires_at", "total_ex_vat", "total_inc_vat", "uncovered")},
            "lines": [{"product_id": l["product_id"], "name": l["name"], "quantity": l["quantity"],
                       "sales_unit": l["sales_unit"], "line_total_ex_vat": l["line_total_ex_vat"],
                       "orderable": l["orderable"], "substitute_for": l.get("substitute_for")} for l in q["lines"]]}


@app.post("/plan-menu", tags=["buyer agent"])
def plan_menu(req: PlanMenuIn, request: Request, token: str | None = Depends(_token),
              x_approver_code: str | None = Header(default=None)):
    """Menu text -> Gemini interprets dishes, portions, ingredients and stock (strict JSON) -> code scales and
    subtracts stock -> catalog candidates -> ONE basket quote with the mandate's preview. Not ordered."""
    authorize(req.mandate_id, req.agent_id, token, x_approver_code, "agent.plan_menu")
    llm = make_llm("gemini") if req.provider == "gemini" else None
    note = None
    if req.provider == "gemini" and llm is None:
        note = "No Gemini key on this server: the curated recipe table interpreted the text instead."
    meter = Meter(llm) if llm else None
    rid = _reserve(_client_ip(request), "plan-menu", llm.cfg.provider, llm.cfg.model, req.mandate_id) if llm else None
    status = "error"
    try:
        with gateway(token) as gw:
            p = R.plan(req.text, gateway=gw, mandate_id=req.mandate_id, agent_id=req.agent_id, llm=meter,
                       delivery_date=req.delivery_date)
        status = "ok" if not p.error else "no_plan"
    finally:
        if rid is not None:
            _finish(rid, meter, status)
    d = p.to_dict()
    used_llm = p.interpretation.startswith("llm:")
    interp = {"path": p.interpretation, "used_model": used_llm, "note": note,
              "by": _display(p.interpretation) if used_llm else "Curated recipe table (no model)",
              "structured_output": p.llm.structured, "errors": p.llm.errors,
              "spec": {k: d[k] for k in ("dishes", "portions", "delivery_date", "stock_on_hand")}}
    if meter:
        interp["model"] = meter.info()
        if not used_llm and meter.calls:
            interp["by"] = f"Curated recipe table ({_display(meter.cfg.model)}'s answer was not valid)"
    q = d["quote"]
    return {
        "banner": BANNER, "request": req.text, "mandate_id": req.mandate_id, "agent_id": req.agent_id,
        "interpretation": interp,
        "ingredients": d["ingredients"],
        "quote": q and {k: q[k] for k in ("id", "expires_at", "total_ex_vat", "total_inc_vat", "lines")},
        "uncovered": (q or {}).get("uncovered", []),
        "substitutes": d["substitutes"],
        "mandate_preview": (q or {}).get("mandate_preview"),
        "summary": d["summary"], "error": d["error"],
        "valio_api_calls": d["valio_api_calls"], "gateway_calls": len(d["steps"]), "steps": d["steps"],
    }


@app.post("/read-photo", tags=["buyer agent"])
async def read_photo(request: Request, image: UploadFile = File(..., description="JPEG or PNG, at most 4 MB"),
                     mandate_id: str = Form(...), agent_id: str = Form(...),
                     kind_hint: Literal["order_list", "menu", "delivery_note"] | None = Form(None),
                     token: str | None = Depends(_token), x_approver_code: str | None = Header(default=None)):
    """Photo of an order list, menu or delivery note -> NVIDIA Nemotron 3.5 Super VL reads it (lines with
    confidence; a second look at doubtful lines) -> confident lines become ONE basket quote with the mandate's
    preview; low-confidence and unreadable lines are flagged for a human, never ordered. Not ordered."""
    from starlette.concurrency import run_in_threadpool
    data = await image.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise AgentError("image_too_large", f"The image is over {MAX_IMAGE_BYTES // (1024 * 1024)} MB; "
                                            "send a smaller photo (a phone photo at 1600 px is plenty).", 413)
    if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
        raise AgentError("unsupported_image", "Send a JPEG or PNG image.", 415)
    return await run_in_threadpool(_read_photo, data, mandate_id, agent_id, kind_hint, token, x_approver_code,
                                   _client_ip(request))


def _read_photo(data: bytes, mandate_id: str, agent_id: str, kind_hint: str | None, token: str | None,
                approver_code: str | None, ip: str) -> dict:
    authorize(mandate_id, agent_id, token, approver_code, "agent.read_photo")
    llm = make_llm("nvidia")
    if llm is None:
        raise AgentError("vision_unavailable", "This server has no NVIDIA_API_KEY, so the photo reader "
                                               "(Nemotron 3.5 Super VL) is off. Try 'Plan from a menu' instead.", 503)
    try:
        V.prepare_image(data)
    except Exception:  # noqa: BLE001
        raise AgentError("unsupported_image", "The image could not be opened; send a JPEG or PNG photo.", 415) from None
    meter = Meter(llm)
    recipe = make_llm("gemini")
    recipe_meter = Meter(recipe) if recipe else None
    rid = _reserve(ip, "read-photo", llm.cfg.provider, llm.cfg.model, mandate_id)
    rid2 = None
    status = "error"
    try:
        try:
            vr = V.read_order_image(data, kind_hint, meter)
        except V.VisionError as e:
            status = "unreadable"
            raise AgentError("vision_failed", str(e), 502) from None
        if vr.read and vr.read.kind == "menu" and recipe_meter:
            rid2 = _reserve(ip, "read-photo", recipe.cfg.provider, recipe.cfg.model, mandate_id)
        with gateway(token) as gw:
            po = V.order_from_read(vr, gateway=gw, mandate_id=mandate_id, agent_id=agent_id,
                                   recipe_llm=recipe_meter if rid2 else None)
        status = "ok" if po.final_quote else "no_quote"
    finally:
        _finish(rid, meter, status)
        if rid2 is not None:
            _finish(rid2, recipe_meter, status)
    d = po.to_dict()
    vis = d["vision"]
    read = vis["read"] or {}
    q = d["quote"]
    lines = [{**ln, "needs_human": ln["status"] == "needs_confirmation"
              or (ln["confidence"] is not None and ln["confidence"] < V.CONFIRM_BELOW)} for ln in d["lines"]]
    return {
        "banner": BANNER, "mandate_id": mandate_id, "agent_id": agent_id,
        "reading": {"by": _display(vis["model"]), "model": meter.info(), "kind": d["kind"],
                    "delivery_date_on_page": read.get("delivery_date"), "notes": read.get("notes"),
                    "items": read.get("items", []), "dishes": read.get("dishes", []),
                    "unreadable": read.get("unreadable", []), "second_look": vis["rechecked"], "flags": vis["flags"],
                    "ignored": vis["ignored"], "repaired": vis["repaired"], "confirm_below": V.CONFIRM_BELOW},
        "lines": lines, "to_confirm": d["to_confirm"], "delivery_date": d["delivery_date"],
        "recipe": None if not po.plan else {"by": _display(po.plan.interpretation) if po.plan.interpretation.startswith(
            "llm:") else "Curated recipe table (no model)", "model": recipe_meter.info() if rid2 else None,
            "ingredients": d["recipe_plan"]["ingredients"], "substitutes": d["recipe_plan"]["substitutes"]},
        "quote": q and {k: q[k] for k in ("id", "expires_at", "total_ex_vat", "total_inc_vat", "lines")},
        "uncovered": (q or {}).get("uncovered", []),
        "substitutes": [ln["substitute"] for ln in d["lines"] if ln.get("substitute")],
        "mandate_preview": (q or {}).get("mandate_preview"),
        "summary": d["summary"], "error": d["error"], "valio_api_calls": d["valio_api_calls"],
    }


class AskIn(BaseModel):
    text: str = Field(..., min_length=3, max_length=600, examples=["1,000 litres of lactose-free milk by Monday"])
    mandate_id: str
    agent_id: str
    provider: Literal["auto", "gemini", "verda"] = Field(
        "auto", description="gemini (Gemini 4 Hydrogen), verda (Kimi K3.1 Pro on Verda) or auto (Gemini if "
                            "configured, else Verda)")
    allow_order: bool = Field(True, description="false: the agent may quote but not call place_order")


@app.post("/ask", tags=["buyer agent"])
def ask(req: AskIn, request: Request, token: str | None = Depends(_token)):
    """A buyer's sentence -> the reference buyer agent (agent/buyer.py: an LLM with the gateway's MCP tools)
    searches, quotes and (unless allow_order=false) orders under the SANDBOX mandate. The mandate engine decides
    the tier; the response has the tool-call transcript and the result. Sandbox mandates only; the sandbox's
    live-lookup budget applies to its quotes."""
    from .buyer import BUYER_TOOLS, BuyerAgent, BuyerConfig, helsinki_today
    m = authorize(req.mandate_id, req.agent_id, token, None, "agent.ask", sandbox_only=True)
    order = [p for p in (["gemini", "verda"] if req.provider == "auto" else [req.provider])]
    llm = next((x for x in (make_llm(p) for p in order) if x), None)
    if llm is None:
        raise AgentError("llm_unavailable", "This server has no key for "
                         + " or ".join({"gemini": "Gemini (GEMINI_API_KEY)",
                                        "verda": "Verda (VERDA_INFERENCE_API_KEY)"}[p] for p in order) + ".", 503)
    meter = Meter(llm)
    rid = _reserve(_client_ip(request), "ask", llm.cfg.provider, llm.cfg.model, req.mandate_id)
    status = "error"
    tools = BUYER_TOOLS if req.allow_order else tuple(t for t in BUYER_TOOLS if t != "place_order")
    try:
        with gateway(token) as gw:
            agent = BuyerAgent(meter, gw, BuyerConfig(req.mandate_id, req.agent_id, helsinki_today(), buyer=m.buyer,
                                                      max_tool_calls=10, max_llm_calls=12, tools=tools))
            run = agent.run(req.text)
        status = "ok" if run.stop_reason == "answered" else run.stop_reason[:16]
    finally:
        _finish(rid, meter, status)
    summary = run.summary()
    q = run.quotes[-1][1] if run.quotes else None
    return {"banner": BANNER, "request": req.text, "mandate_id": req.mandate_id, "agent_id": req.agent_id,
            "by": _display(llm.cfg.model), "model": meter.info(), "allow_order": req.allow_order,
            "transcript": run.transcript(), "answer": run.final_text, "stop_reason": run.stop_reason,
            "quote": _quote_view(q), "mandate_preview": (q or {}).get("mandate_preview"),
            "order": _order_view(run.orders[0] if run.orders else None), "orders_placed": summary["orders_placed"],
            "valio_api_calls": run.valio_api_calls, "tool_calls": run.tool_calls}


class DraftIn(BaseModel):
    text: str = Field(..., min_length=3, max_length=1500,
                      examples=["max 2000 € per order, yli 500 € kysy minulta, ei pähkinöitä"])
    mandate_id: str = Field(..., description="The mandate in force; the draft is a diff against it")
    agent_id: str
    provider: Literal["auto", "gemini", "rules"] = Field(
        "auto", description="gemini (Gemini 4 Hydrogen, JSON schema), rules (offline parser, no model) or auto "
                            "(Gemini if this server has a key, else rules)")


@app.post("/draft-mandate", tags=["buyer agent"])
def draft_mandate(req: DraftIn, request: Request, token: str | None = Depends(_token),
                  x_approver_code: str | None = Header(default=None)):
    """Plain-language rules -> a PROPOSED mandate: the fields, a field-by-field diff against the mandate in force,
    the words each field came from, open questions (vague limits, conflicts) and what a mandate can't express.
    The model (if any) only drafts; a deterministic validator checks every field (types, ranges, allergen codes,
    claims and categories that exist, numbers literally in the text, no loosening from injection attempts).
    Nothing is changed: the human edits and signs the result on the signing page."""
    m = authorize(req.mandate_id, req.agent_id, token, x_approver_code, "agent.draft_mandate")
    llm = make_llm("gemini") if req.provider in ("auto", "gemini") else None
    meter = Meter(llm) if llm else None
    rid = _reserve(_client_ip(request), "draft-mandate", llm.cfg.provider, llm.cfg.model, req.mandate_id) \
        if llm else None
    status = "error"
    try:
        d = DR.draft_mandate(req.text, m, provider="gemini" if meter else "rules", llm=meter)
        status = "ok" if d["parser"] != "rules" or not meter else "fallback"
    finally:
        if rid is not None:
            _finish(rid, meter, status)
    used = d["parser"] != "rules"
    note = None
    if req.provider == "gemini" and llm is None:
        note = "No Gemini key on this server: the offline rule-based drafter read the text instead."
    return {"banner": BANNER, "request": req.text, "mandate_id": req.mandate_id, "agent_id": req.agent_id,
            "by": _display(d["parser"]) if used else "Offline rule-based drafter (no model)",
            "used_model": used, "note": note, "model": meter.info() if meter else None, **d}


class OrderIn(BaseModel):
    quote_id: str
    mandate_id: str
    agent_id: str


@app.post("/order", tags=["buyer agent"])
def order(req: OrderIn, token: str | None = Depends(_token), x_approver_code: str | None = Header(default=None)):
    """Place the quote under the mandate. The gateway's mandate engine decides: ordered (silent / notify),
    pending_approval (ask: a human approves in the seller UI) or refused (block). Repeating it is safe."""
    authorize(req.mandate_id, req.agent_id, token, x_approver_code, "agent.order")
    o = S.place_order(req.agent_id, req.mandate_id, req.quote_id, idempotency_key=f"agent-web:{req.quote_id}",
                      agent_token=token, rationale="Placed by the reference buyer agent from the demo page "
                                                   "after the human pressed Order.")
    return {"order": _order_view(o)}


def _order_view(o: dict | None) -> dict | None:
    if not o:
        return None
    dec = o.get("decision") or {}
    return {"id": o["id"], "status": o["status"], "tier": dec.get("outcome"),
            "total_ex_vat": o.get("total_ex_vat"), "approval_url": o.get("approval_url"),
            "findings": [{"rule": f["rule"], "outcome": f["outcome"], "message": f["message"]}
                         for f in dec.get("findings", [])]}


@app.get("/usage", tags=["buyer agent"])
def usage():
    """Model usage by the demo agent: requests, model calls, tokens and cost per provider (all time and today)."""
    return usage_totals()


@app.get("/status", tags=["buyer agent"])
def status():
    """Which models this server can use (no secrets: only whether a key is configured)."""
    return {"banner": BANNER, "models": {
        "menu": {"provider": "gemini", "display": DISPLAY["gemini-4-hydrogen"], "available": bool(_preset_env("gemini")),
                 "fallback": "curated recipe table"},
        "ask": {"provider": "gemini" if _preset_env("gemini") else "verda",
                "display": DISPLAY["gemini-4-hydrogen"] if _preset_env("gemini") else DISPLAY["kimi-k3-1-pro"],
                "available": bool(_preset_env("gemini") or _preset_env("verda")), "sandbox_only": True},
        "draft": {"provider": "gemini", "display": DISPLAY["gemini-4-hydrogen"],
                  "available": bool(_preset_env("gemini")), "fallback": "offline rule-based drafter"},
        "photo": {"provider": "nvidia", "display": DISPLAY["nvidia/nemotron-3.5-super-vl-120b-a12b"],
                  "available": bool(_preset_env("nvidia"))}},
        "today": {"spent_usd": round(spent_today(), 4), "budget_usd": DAILY_USD},
        "limits": {"per_ip_per_hour": PER_IP_HOUR, "max_image_mb": MAX_IMAGE_BYTES / 1024 / 1024}}
