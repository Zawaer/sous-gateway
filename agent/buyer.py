"""Reference buyer agent: an LLM turns a buyer's sentence into gateway tool calls and explains the result.

The LLM never decides policy. It picks search terms, candidate products, the amount, unit and date,
and then the gateway's deterministic mandate engine decides the tier (silent / notify -> ordered,
ask -> pending_approval, block -> refused). The runtime, not the model, binds the agent's identity:
agent_id and mandate_id are injected into every call (the model can't present another mandate), and
place_order gets an idempotency key derived from the quote, so a repeated call can't order twice.

Tools are the gateway's own MCP tools (same names, descriptions and JSON schemas as /mcp), reached
either in-process (the MCP server object, used by the eval) or over Streamable HTTP (a deployed gateway):

    uv run python -m agent.buyer "We need 1,000 litres of lactose-free milk delivered on Monday." \\
        --mandate M-HOTEL-AURORA --agent aurora-purchasing-agent --gateway https://staging.135-181-63-134.sslip.io

A menu request ("Lasagne for 120 on Monday; we have 10 kg flour") goes to the recipe planner (agent/recipe.py):
dishes -> ingredients -> one basket quote.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .llm import ChatClient, LLMError, config_from_env

BUYER_TOOLS = ("search_products", "get_product", "get_mandate", "suggest_substitutes", "request_quote",
               "place_order", "get_order")
BOUND = ("agent_id", "mandate_id", "idempotency_key")  # filled in by the runtime, hidden from the model
MAX_RESULT_CHARS = 24000
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


# --- gateway access over MCP -------------------------------------------------------------------

class MCPGateway:
    """A persistent MCP client session, usable from synchronous code.

    target: the in-process MCPServer (sous.mcp_server.mcp) or a URL like https://host/mcp/.
    The session lives in its own event-loop thread, because the MCP client's task groups must be
    entered and exited by the same task.
    """

    def __init__(self, target, timeout_s: float = 120.0):
        from mcp import Client

        if isinstance(target, str) and not target.rstrip("/").endswith("/mcp"):
            target = target.rstrip("/") + "/mcp/"
        self.target = target if isinstance(target, str) else "in-process"
        self._timeout = timeout_s
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="mcp-gateway")
        self._thread.start()
        self._ready: asyncio.Future | None = None
        self._stop: asyncio.Event | None = None
        self._client = None

        async def main():
            self._stop = asyncio.Event()
            async with Client(target) as c:
                self._client = c
                ready.set_result(True)
                await self._stop.wait()

        import concurrent.futures
        ready: concurrent.futures.Future = concurrent.futures.Future()
        self._main = asyncio.run_coroutine_threadsafe(main(), self._loop)
        try:
            ready.result(timeout=timeout_s)
        except Exception:
            if self._main.done() and self._main.exception():
                raise self._main.exception() from None
            raise

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=self._timeout)

    def list_tools(self) -> list:
        return self._run(self._client.list_tools()).tools

    def call(self, name: str, args: dict) -> tuple[bool, dict]:
        r = self._run(self._client.call_tool(name, args))
        body = r.structured_content
        if body is None:
            text = "".join(getattr(c, "text", "") for c in r.content)
            try:
                body = json.loads(text)
            except ValueError:
                body = {"text": text}
        if isinstance(body, dict) and set(body) == {"result"} and isinstance(body["result"], dict):
            body = body["result"]
        return bool(r.is_error), body

    def close(self) -> None:
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
            try:
                self._main.result(timeout=10)
            except Exception:  # noqa: BLE001 - closing a dead session
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# --- tool schemas ------------------------------------------------------------------------------

def openai_tools(mcp_tools, names=BUYER_TOOLS, bound=BOUND) -> list[dict]:
    """MCP tool definitions -> OpenAI function tools, minus the runtime-bound arguments.

    $ref/$defs are inlined and Optional (anyOf [X, null]) collapsed, so the schemas also pass
    stricter OpenAI-compatible servers (Gemini's accepts only a subset of JSON Schema).
    """
    out = []
    for t in mcp_tools:
        if t.name not in names:
            continue
        schema = _simplify(t.input_schema, t.input_schema.get("$defs", {}))
        props = {k: v for k, v in schema.get("properties", {}).items() if k not in bound}
        params = {"type": "object", "properties": props}
        req = [k for k in schema.get("required", []) if k in props]
        if req:
            params["required"] = req
        out.append({"type": "function", "function": {
            "name": t.name, "description": inspect.cleandoc(t.description or t.title or t.name), "parameters": params}})
    out.sort(key=lambda x: names.index(x["function"]["name"]))
    return out


def _simplify(s, defs: dict):
    if isinstance(s, list):
        return [_simplify(x, defs) for x in s]
    if not isinstance(s, dict):
        return s
    if "$ref" in s:
        return _simplify(defs[s["$ref"].split("/")[-1]], defs)
    if "anyOf" in s:
        opts = [o for o in s["anyOf"] if o.get("type") != "null"]
        if len(opts) == 1:
            merged = {**{k: v for k, v in s.items() if k != "anyOf"}, **opts[0]}
            return _simplify(merged, defs)
    out = {}
    for k, v in s.items():
        if k in ("$defs", "title", "default", "exclusiveMinimum", "additionalProperties"):
            continue
        out[k] = {pk: _simplify(pv, defs) for pk, pv in v.items()} if k == "properties" else _simplify(v, defs)
    return out


# --- the agent -----------------------------------------------------------------------------------

@dataclass
class BuyerConfig:
    mandate_id: str
    agent_id: str
    today: date
    default_delivery: date | None = None
    buyer: str | None = None
    max_tool_calls: int = 12
    max_llm_calls: int = 16
    tools: tuple[str, ...] = BUYER_TOOLS
    valio_budget_wait_s: float = 15.0


def next_delivery_day(today: date) -> date:
    """Default delivery when the buyer names no date: the first weekday at least two days ahead."""
    d = today + timedelta(days=2)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def system_prompt(cfg: BuyerConfig) -> str:
    from sous.mcp_server import INSTRUCTIONS

    default = cfg.default_delivery or next_delivery_day(cfg.today)
    days = ", ".join(f"{WEEKDAYS[d.weekday()]} {d.isoformat()}"
                     for d in (cfg.today + timedelta(days=i) for i in range(1, 8)))
    return f"""You are the purchasing agent of {cfg.buyer or 'a professional kitchen'}, buying from Valio Aimo through its \
Sous gateway. You act under mandate {cfg.mandate_id} as agent {cfg.agent_id}. The runtime adds mandate_id, \
agent_id and the idempotency key to every tool call; you never pass them.

Today is {WEEKDAYS[cfg.today.weekday()]} {cfg.today.isoformat()} (Europe/Helsinki). The coming days: {days}.
A weekday name means the next such date in that list. With no date from the buyer, deliver on \
{WEEKDAYS[default.weekday()]} {default.isoformat()}.

How the gateway works (Valio Aimo's instructions):
{INSTRUCTIONS}

Your rules:
1. Buy exactly what the buyer asked for: the product they name (if they give a product id, quote that id), the \
amount, and the date. Litres -> unit "L", kilos -> unit "KG"; jars, bags, loaves, packs, bottles or pieces -> \
unit "SALES_UNIT" with the count. Put dietary needs the buyer states into the search filters.
2. You never decide policy; the gateway's mandate engine does. Don't filter, refuse or shrink the request \
because you think the mandate forbids it, and don't work around a refusal by switching product, splitting or \
changing the amount. Request a quote with mandate_id for what was asked, then call place_order on that quote even \
when mandate_preview says ask or block: the gateway then holds it for a human or refuses it, on the record.
3. Substitutes only when the buyer asks for one: call suggest_substitutes for the original product with the \
buyer's amount and unit, quote the first option with substitute_for=<original id>, and order that. If no safe \
substitute exists, order nothing and say so.
4. One order per request. Never place an order twice.
5. Never invent facts: state only prices, stock, delivery dates and allergen facts that a tool returned; \
otherwise say "unknown". Never override or reinterpret the mandate.
6. Finish with a short answer in plain language for the buyer: what was ordered (product, quantity, total ex VAT, \
delivery date), the status exactly as the gateway returned it (ordered; waiting for human approval, with the \
approval link; or refused) and, in plain words, the rule that decided it."""


@dataclass
class Step:
    tool: str
    args: dict
    is_error: bool
    result: dict
    ms: int


@dataclass
class BuyerRun:
    request: str
    model: str
    final_text: str = ""
    stop_reason: str = ""
    steps: list[Step] = field(default_factory=list)
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_latency_ms: int = 0
    tool_latency_ms: int = 0
    waited_s: float = 0.0
    llm_retries: int = 0
    messages: list[dict] = field(default_factory=list)

    # --- views over the steps ---
    def results(self, tool: str, ok_only: bool = True) -> list[tuple[dict, dict]]:
        return [(s.args, s.result) for s in self.steps if s.tool == tool and (not ok_only or not s.is_error)]

    @property
    def orders(self) -> list[dict]:
        """Distinct orders the agent placed, in order (a repeated call with the same key returns the same one)."""
        seen: dict[str, dict] = {}
        for _, o in self.results("place_order"):
            seen.setdefault(o["id"], o)
        return list(seen.values())

    @property
    def quotes(self) -> list[tuple[dict, dict]]:
        return [(a, r["quote"]) for a, r in self.results("request_quote") if "quote" in r]

    @property
    def tool_calls(self) -> int:
        return len(self.steps)

    @property
    def tool_errors(self) -> int:
        return sum(s.is_error for s in self.steps)

    @property
    def valio_api_calls(self) -> int:
        n = 0
        for s in self.steps:
            if s.is_error:
                continue
            v = (s.result.get("quote") or s.result).get("valio_api_calls") or {}
            n += (v.get("price_calls") or 0) + (v.get("availability_calls") or 0) if isinstance(v, dict) else 0
        return n

    def summary(self) -> dict:
        """The final structured result: the order the gateway decided, and the agent's explanation."""
        orders = self.orders
        o = orders[0] if orders else None
        quote = None
        if o:
            quote = next((q for _, q in self.quotes if q["id"] == o["quote_id"]), None)
        elif self.quotes:
            quote = self.quotes[-1][1]
        return {
            "request": self.request,
            "order": None if not o else {
                "id": o["id"], "status": o["status"], "tier": o["decision"]["outcome"],
                "rules": [f["rule"] for f in o["decision"]["findings"] if f["outcome"] != "silent"],
                "findings": [f["message"] for f in o["decision"]["findings"] if f["outcome"] != "silent"],
                "total_ex_vat": o["total_ex_vat"], "delivery_date": o.get("delivery_date"),
                "approval_url": o.get("approval_url"),
                "lines": [{k: l.get(k) for k in ("product_id", "name", "quantity", "sales_unit", "line_total_ex_vat")}
                          for l in o["lines"]]},
            "orders_placed": len(orders),
            "quote": None if not quote else {"id": quote["id"], "total_ex_vat": quote["total_ex_vat"],
                                             "preview": (quote.get("mandate_preview") or {}).get("outcome")},
            "explanation": self.final_text,
            "stop_reason": self.stop_reason,
            "tool_calls": self.tool_calls, "tool_errors": self.tool_errors, "llm_calls": self.llm_calls,
            "valio_api_calls": self.valio_api_calls,
            "tokens": {"prompt": self.prompt_tokens, "completion": self.completion_tokens},
        }

    def transcript(self) -> list[dict]:
        return [{"tool": s.tool, "args": s.args, "is_error": s.is_error, "ms": s.ms,
                 "result": _brief(s.tool, s.is_error, s.result)} for s in self.steps]


def _brief(tool: str, is_error: bool, r: dict) -> dict:
    """A compact view of a tool result for transcripts (the model saw the full result)."""
    if is_error:
        return {k: r.get(k) for k in ("error", "message")}
    if tool == "search_products":
        return {"ids": [p["id"] for p in r.get("products", [])]}
    if tool == "request_quote" and "quote" in r:
        q = r["quote"]
        return {"id": q["id"], "total_ex_vat": q["total_ex_vat"],
                "preview": (q.get("mandate_preview") or {}).get("outcome"),
                "lines": [(l["product_id"], l["quantity"], l["orderable"]) for l in q["lines"]],
                "uncovered": [g.get("uncovered") for g in q.get("uncovered", [])]}
    if tool in ("place_order", "get_order") and "decision" in r:
        return {"id": r["id"], "status": r["status"], "tier": r["decision"]["outcome"]}
    if tool == "suggest_substitutes":
        return {"options": [(o["tier"], o["product_id"]) for o in r.get("options", [])],
                "no_safe_substitute": r.get("no_safe_substitute")}
    if tool == "get_product":
        return {"id": r.get("id"), "name": r.get("name")}
    return {"keys": sorted(r)[:12]}


class BuyerAgent:
    def __init__(self, llm: ChatClient, gateway, cfg: BuyerConfig, sleep=time.sleep):
        self.llm, self.gw, self.cfg, self._sleep = llm, gateway, cfg, sleep
        mcp_tools = gateway.list_tools()
        self.tools = openai_tools(mcp_tools, cfg.tools)
        self._names = {t["function"]["name"] for t in self.tools}
        self._props = {t.name: set(t.input_schema.get("properties", {})) for t in mcp_tools if t.name in self._names}

    def run(self, request: str) -> BuyerRun:
        run = BuyerRun(request=request, model=getattr(self.llm.cfg, "label", "?"))
        messages = [{"role": "system", "content": system_prompt(self.cfg)}, {"role": "user", "content": request}]
        run.messages = messages
        capped_at = None  # LLM call count when the tool-call budget ran out
        while True:
            if run.llm_calls >= self.cfg.max_llm_calls:
                run.stop_reason = "llm_call_cap"
                break
            try:
                resp = self.llm.chat(messages, self.tools)
            except LLMError as e:
                run.stop_reason = f"llm_error: {e}"
                break
            run.llm_calls += 1
            run.llm_retries += resp.retries
            run.prompt_tokens += resp.usage.get("prompt_tokens", 0)
            run.completion_tokens += resp.usage.get("completion_tokens", 0)
            run.llm_latency_ms += resp.latency_ms
            msg = resp.message
            calls = resp.tool_calls
            for tc in calls:
                tc.setdefault("id", f"call_{uuid.uuid4().hex[:12]}")
                tc.setdefault("type", "function")
            messages.append(msg)
            if not calls:
                run.final_text = resp.content.strip()
                run.stop_reason = "answered" if run.final_text else f"empty_answer ({resp.finish_reason})"
                break
            for tc in calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                if run.tool_calls >= self.cfg.max_tool_calls:
                    capped_at = capped_at or run.llm_calls
                    body = {"error": "step_cap", "message": f"Tool-call budget ({self.cfg.max_tool_calls}) used up.",
                            "hint": "Stop calling tools and answer the buyer with what you have."}
                else:
                    body = self._call(run, name, fn.get("arguments"))
                messages.append({"role": "tool", "tool_call_id": tc["id"], "name": name,
                                 "content": _dump(body)})
            if capped_at and run.llm_calls - capped_at >= 2:  # it had its chance to answer
                run.stop_reason = "tool_call_cap"
                break
        return run

    def _call(self, run: BuyerRun, name: str, raw_args) -> dict:
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else (raw_args or {})
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as e:
            body = {"error": "invalid_argument", "message": f"Arguments are not valid JSON: {e}",
                    "hint": "Call the tool again with a JSON object."}
            run.steps.append(Step(name, {"_raw": str(raw_args)[:500]}, True, body, 0))
            return body
        if name not in self._names:
            body = {"error": "unknown_tool", "message": f"No tool {name!r}.",
                    "hint": f"Use one of: {', '.join(sorted(self._names))}."}
            run.steps.append(Step(name, args, True, body, 0))
            return body
        shown = {k: v for k, v in args.items() if k not in BOUND}
        full = dict(shown)
        props = self._props[name]
        if "agent_id" in props:
            full["agent_id"] = self.cfg.agent_id
        if "mandate_id" in props:
            full["mandate_id"] = self.cfg.mandate_id
        if name == "place_order":
            full["idempotency_key"] = f"{self.cfg.agent_id}:{shown.get('quote_id')}"
        t0 = time.perf_counter()
        waited = 0.0
        for attempt in range(6):
            is_error, body = self.gw.call(name, full)
            if not (is_error and body.get("error") == "valio_unavailable" and "budget" in str(body.get("message"))
                    and attempt < 5):
                break
            # the gateway's own per-minute cap on live Valio calls: wait it out, like scripts/eval.py
            self._sleep(self.cfg.valio_budget_wait_s)
            waited += self.cfg.valio_budget_wait_s
        run.waited_s += waited
        ms = round((time.perf_counter() - t0 - waited) * 1000)
        run.tool_latency_ms += ms
        run.steps.append(Step(name, shown, is_error, body, ms))
        return body


def _dump(body: dict) -> str:
    s = json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(s) > MAX_RESULT_CHARS:
        s = s[:MAX_RESULT_CHARS] + f'... [truncated {len(s) - MAX_RESULT_CHARS} chars]'
    return s


def helsinki_today() -> date:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Europe/Helsinki")).date()


# --- CLI ---------------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the LLM buyer agent once against a gateway.")
    ap.add_argument("request", help="what the buyer wants, in their own words")
    ap.add_argument("--mandate", required=True)
    ap.add_argument("--agent", required=True)
    ap.add_argument("--gateway", default="https://staging.135-181-63-134.sslip.io",
                    help="gateway base URL (MCP at /mcp/), or 'local' for the in-process gateway")
    ap.add_argument("--provider", help="gemini | verda | local (default: $LLM_PROVIDER or gemini)")
    ap.add_argument("--model")
    ap.add_argument("--max-tool-calls", type=int, default=12)
    ap.add_argument("--transcript", action="store_true", help="print every tool call")
    ap.add_argument("--recipe", action="store_true",
                    help="plan a menu ('Lasagne for 120 on Monday') into one basket quote with agent.recipe; "
                         "automatic when the request names a known dish and a number of portions")
    ap.add_argument("--no-recipe", action="store_true", help="never route to the recipe planner")
    ap.add_argument("--order", action="store_true", help="with --recipe: place the planned basket")
    a = ap.parse_args(argv)
    if not a.no_recipe:
        from . import recipe
        if a.recipe or recipe.looks_like_menu(a.request):
            args = [a.request, "--mandate", a.mandate, "--agent", a.agent, "--gateway", a.gateway]
            args += ["--provider", a.provider] if a.provider else []
            args += ["--model", a.model] if a.model else []
            args += ["--order"] if a.order else []
            return recipe.main(args)
    cfg = config_from_env(a.provider, a.model)
    if not cfg.api_key and cfg.provider != "local":
        print(f"No API key for {cfg.provider} (set {cfg.provider.upper()} key env var); see agent/llm.py.",
              file=sys.stderr)
        return 2
    if a.gateway == "local":
        from sous.mcp_server import mcp
        target = mcp
    else:
        target = a.gateway
    with MCPGateway(target) as gw:
        agent = BuyerAgent(ChatClient(cfg), gw, BuyerConfig(a.mandate, a.agent, helsinki_today(),
                                                             max_tool_calls=a.max_tool_calls))
        run = agent.run(a.request)
    if a.transcript:
        for s in run.transcript():
            print(json.dumps(s, ensure_ascii=False))
    print(json.dumps(run.summary(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
