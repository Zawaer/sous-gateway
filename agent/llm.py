"""Provider-agnostic chat client over the OpenAI-compatible Chat Completions API, with tool calling.

One small httpx client covers every provider we use, because they all speak the same wire format:

    preset  base URL                                                   key env var              default model
    gemini  https://generativelanguage.googleapis.com/v1beta/openai/  GEMINI_API_KEY           gemini-4-hydrogen
    verda   https://inference.verda.com/v1                             VERDA_INFERENCE_API_KEY  kimi-k3-1-pro
    nvidia  https://integrate.api.nvidia.com/v1                        NVIDIA_API_KEY           nvidia/nemotron-3.5-super-vl-120b-a12b
            (also the LLM eval's buyer agent, text + tools; nemotron-3-super-120b-a12b answered 404 to our key)
    verda-selfhosted  $SOUS_SELFHOSTED_URL (our vLLM on a Verda GPU)  SOUS_SELFHOSTED_KEY  $SOUS_SELFHOSTED_MODEL
            (default Qwen/Qwen3-30B-A3B-Instruct-2507)
    gemma   (the Gemini API's OpenAI endpoint)                        GEMINI_API_KEY           gemma-4-31b-it
    anthropic https://api.anthropic.com/v1                             ANTHROPIC_API_KEY        claude-opus-5-5
    openai  https://api.openai.com/v1                                  OPENAI_API_KEY           (set --model)
    local   $LLM_BASE_URL (vLLM, Ollama, llama.cpp server, ...)       LLM_API_KEY (optional)   $LLM_MODEL

LLM_BASE_URL / LLM_API_KEY / LLM_MODEL override any preset; LLM_PROVIDER picks the preset when none is
passed. LLM_REASONING_EFFORT sets reasoning_effort (Verda's Kimi K3.1 Pro defaults to max, which is slow
and costly for tool calling; the presets send "low"). LLM_PRICE_IN / LLM_PRICE_OUT (USD per 1M tokens)
override the built-in price table for the cost estimate. LLM_MIN_INTERVAL_S spaces requests out (the nvidia
preset defaults to 1.6 s: NVIDIA's free tier allows ~40 requests/min).

API keys are only ever put in the Authorization header: never logged, never in repr(), never in errors.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field

import httpx

PRESETS: dict[str, dict] = {
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "key_env": "GEMINI_API_KEY", "model": "gemini-4-hydrogen", "reasoning_effort": "low",
               "where": "Google Gemini API (global; Google's own infrastructure)"},
    "verda": {"base_url": "https://inference.verda.com/v1", "key_env": "VERDA_INFERENCE_API_KEY",
              "model": "kimi-k3-1-pro", "reasoning_effort": "low",
              "where": "Verda Inference (open-weight model on Verda GPUs in the EU; prompts not stored)"},
    # Nemotron 3.5 Super VL (vision + text) on NVIDIA's API catalog (build.nvidia.com). Images go in the
    # messages as OpenAI-style image_url parts (base64 data URLs); see agent/vision.py. Reasoning is a
    # chat-template switch, not reasoning_effort: off by default (extraction, latency); LLM_THINKING=1 turns it on.
    "nvidia": {"base_url": "https://integrate.api.nvidia.com/v1", "key_env": "NVIDIA_API_KEY",
               "model": "nvidia/nemotron-3.5-super-vl-120b-a12b", "reasoning_effort": None,
               "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
               "min_interval_s": 1.6,  # free tier: ~40 requests/min per key
               "where": "NVIDIA API catalog (build.nvidia.com; NVIDIA-hosted NIM, US)"},
    # Our own open-weight model on a Verda GPU (vLLM behind an authenticated HTTPS proxy; docs/VERDA.md). URL, key
    # and model id come from the environment, so the endpoint can move without a code change.
    "verda-selfhosted": {"base_url": "", "base_url_env": "SOUS_SELFHOSTED_URL", "key_env": "SOUS_SELFHOSTED_KEY",
                         "model": "Qwen/Qwen3-30B-A3B-Instruct-2507", "model_env": "SOUS_SELFHOSTED_MODEL",
                         "reasoning_effort": None,
                         "where": "self-hosted open-weight model, vLLM on our own Verda GPU (Finland)"},
    # Gemma 4 (open weights, Apache 2.0) served by the Gemini API: no thinking level on this model.
    "gemma": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "key_env": "GEMINI_API_KEY",
              "model": "gemma-4-31b-it", "reasoning_effort": None,
              "where": "Google Gemini API serving Gemma 4 (open-weight model; could run on our own Verda GPU)"},
    # Claude through Anthropic's OpenAI-compatible endpoint. Opus/Haiku 5.5 reject sampling parameters, so no
    # temperature; thinking stays at the model's default (adaptive, effort medium).
    "anthropic": {"base_url": "https://api.anthropic.com/v1", "key_env": "ANTHROPIC_API_KEY",
                  "model": "claude-opus-5-5", "reasoning_effort": None, "temperature": None,
                  "where": "Anthropic API (Claude; US)"},
    # OpenAI reasoning models take max_completion_tokens and only the default temperature.
    "openai": {"base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY", "model": "",
               "reasoning_effort": "low", "temperature": None, "max_tokens_field": "max_completion_tokens",
               "where": "OpenAI API (US)"},
    "local": {"base_url": "http://127.0.0.1:8000/v1", "key_env": "LLM_API_KEY", "model": "",
              "reasoning_effort": None, "where": "self-hosted OpenAI-compatible server (e.g. vLLM on a Verda GPU)"},
}

# USD per 1M tokens (input, output incl. thinking), from the providers' pricing pages on 13.11.2026.
# Verda shows its prices only in the console: set LLM_PRICE_IN / LLM_PRICE_OUT to get a cost estimate.
PRICES: dict[str, tuple[float, float]] = {
    "gemini-4-hydrogen": (1.50, 7.50),
    "gemini-4-argon": (2.00, 10.00),
    "gemini-4-carbon": (2.00, 10.00),
    "gemini-3.8-flash": (0.75, 3.75),
    # NVIDIA API catalog free tier: rate-limited (~40 requests/min), no per-token charge
    "nvidia/nemotron-3-super-120b-a12b": (0.0, 0.0),
    "nvidia/nemotron-3.5-super-vl-120b-a12b": (0.0, 0.0),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-haiku-5-5": (0.10, 0.50),
}

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class LLMError(RuntimeError):
    """A provider error after retries. The message never contains the API key."""


@dataclass
class LLMConfig:
    provider: str
    base_url: str
    model: str
    api_key: str = field(default="", repr=False)  # repr=False: the key never shows up in logs or tracebacks
    reasoning_effort: str | None = None
    temperature: float | None = 0.0
    max_tokens: int | None = 4096
    timeout_s: float = 120.0
    max_retries: int = 6
    price_in: float | None = None   # USD per 1M input tokens
    price_out: float | None = None  # USD per 1M output tokens
    where: str = ""
    extra_body: dict | None = None  # provider-specific body fields (e.g. NVIDIA's chat_template_kwargs)
    min_interval_s: float = 0.0     # at least this long between request starts (provider rate limits)
    max_tokens_field: str = "max_tokens"  # OpenAI's reasoning models want max_completion_tokens

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"

    def cost_usd(self, prompt_tokens: int, completion_tokens: int) -> float | None:
        if self.price_in is None or self.price_out is None:
            return None
        return round((prompt_tokens * self.price_in + completion_tokens * self.price_out) / 1e6, 4)


def config_from_env(provider: str | None = None, model: str | None = None, env: dict | None = None) -> LLMConfig:
    """Build a config from a preset plus LLM_* environment overrides (explicit arguments win)."""
    env = os.environ if env is None else env
    provider = provider or env.get("LLM_PROVIDER") or "gemini"
    if provider not in PRESETS:
        raise ValueError(f"Unknown LLM provider {provider!r}; use one of {sorted(PRESETS)}.")
    p = PRESETS[provider]
    model = model or env.get("LLM_MODEL") or (env.get(p["model_env"]) if p.get("model_env") else None) or p["model"]
    if not model:
        raise ValueError(f"Set LLM_MODEL (or --model) for provider {provider!r}.")
    effort = env.get("LLM_REASONING_EFFORT", p["reasoning_effort"]) or None
    price = PRICES.get(model, (None, None))
    price_in = float(env["LLM_PRICE_IN"]) if env.get("LLM_PRICE_IN") else price[0]
    price_out = float(env["LLM_PRICE_OUT"]) if env.get("LLM_PRICE_OUT") else price[1]
    extra = dict(p.get("extra_body") or {})
    if provider == "nvidia" and env.get("LLM_THINKING") in ("1", "true", "on"):
        extra["chat_template_kwargs"] = {"enable_thinking": True}
    interval = float(env["LLM_MIN_INTERVAL_S"]) if env.get("LLM_MIN_INTERVAL_S") else p.get("min_interval_s", 0.0)
    base_url = env.get("LLM_BASE_URL") or (env.get(p["base_url_env"]) if p.get("base_url_env") else None) \
        or p["base_url"]
    if not base_url:
        raise ValueError(f"Set {p.get('base_url_env') or 'LLM_BASE_URL'} for provider {provider!r}.")
    return LLMConfig(provider=provider, base_url=base_url.rstrip("/"),
                     model=model, api_key=env.get("LLM_API_KEY") or env.get(p["key_env"], ""),
                     reasoning_effort=effort, temperature=p.get("temperature", 0.0),
                     max_tokens_field=p.get("max_tokens_field", "max_tokens"),
                     price_in=price_in, price_out=price_out, where=p["where"], extra_body=extra or None,
                     min_interval_s=interval)


def key_present(provider: str, env: dict | None = None) -> bool:
    env = os.environ if env is None else env
    p = PRESETS[provider]
    if p.get("base_url_env") and not (env.get("LLM_BASE_URL") or env.get(p["base_url_env"])):
        return False  # no endpoint configured
    return bool(env.get("LLM_API_KEY") or env.get(p["key_env"]))


@dataclass
class ChatResponse:
    message: dict            # the assistant message as returned (echo it back verbatim: Gemini's thought
                             # signatures ride along in tool_calls[].extra_content)
    finish_reason: str | None
    usage: dict              # prompt_tokens, completion_tokens, total_tokens (0 when not reported)
    latency_ms: int
    retries: int

    @property
    def tool_calls(self) -> list[dict]:
        return self.message.get("tool_calls") or []

    @property
    def content(self) -> str:
        c = self.message.get("content")
        if isinstance(c, list):  # some servers return content parts
            return "".join(x.get("text", "") for x in c if isinstance(x, dict))
        return c or ""


class ChatClient:
    """chat(messages, tools) -> ChatResponse, with retries and backoff on 429 / 5xx / timeouts."""

    def __init__(self, cfg: LLMConfig, http: httpx.Client | None = None, sleep=time.sleep, clock=time.monotonic):
        self.cfg = cfg
        self._http = http or httpx.Client(timeout=cfg.timeout_s)
        self._sleep = sleep
        self._clock = clock
        self._last_start: float | None = None

    def _pace(self) -> None:
        """Keep at least cfg.min_interval_s between request starts (retries included)."""
        if self.cfg.min_interval_s > 0 and self._last_start is not None:
            wait = self._last_start + self.cfg.min_interval_s - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_start = self._clock()

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             response_format: dict | None = None) -> ChatResponse:
        """response_format: e.g. {"type": "json_schema", "json_schema": {...}} (structured output)."""
        c = self.cfg
        body: dict = {"model": c.model, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if response_format:
            body["response_format"] = response_format
        if c.temperature is not None:
            body["temperature"] = c.temperature
        if c.max_tokens:
            body[c.max_tokens_field] = c.max_tokens
        if c.reasoning_effort:
            body["reasoning_effort"] = c.reasoning_effort
        if c.extra_body:
            body.update(c.extra_body)
        headers = {"Content-Type": "application/json"}
        if c.api_key:
            headers["Authorization"] = f"Bearer {c.api_key}"
        url = f"{c.base_url}/chat/completions"
        t0 = time.perf_counter()
        last = ""
        for attempt in range(c.max_retries + 1):
            self._pace()
            try:
                r = self._http.post(url, json=body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = f"{type(e).__name__}"
                if attempt == c.max_retries:
                    break
                self._sleep(_backoff(attempt))
                continue
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:  # an empty or HTML 200 (seen from Verda): a provider glitch, retry it
                    last = f"HTTP 200 with a non-JSON body ({len(r.content)} bytes)"
                    if attempt == c.max_retries:
                        break
                    self._sleep(_backoff(attempt))
                    continue
                choice = (data.get("choices") or [{}])[0]
                msg = dict(choice.get("message") or {})
                msg["role"] = "assistant"
                u = data.get("usage") or {}
                usage = {k: int(u.get(k) or 0) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
                return ChatResponse(message=msg, finish_reason=choice.get("finish_reason"), usage=usage,
                                    latency_ms=round((time.perf_counter() - t0) * 1000), retries=attempt)
            last = f"HTTP {r.status_code}: {_error_text(r)}"
            if r.status_code not in RETRY_STATUS or attempt == c.max_retries:
                break
            self._sleep(_retry_after(r) or _backoff(attempt))
        raise LLMError(f"{c.label} chat completion failed: {last}")


def _backoff(attempt: int) -> float:
    return min(30.0, 1.0 * 2 ** attempt) * (0.8 + 0.4 * random.random())


def _retry_after(r: httpx.Response) -> float | None:
    v = r.headers.get("retry-after")
    try:
        return min(60.0, float(v)) if v else None
    except ValueError:
        return None


def _error_text(r: httpx.Response) -> str:
    """The provider's error message (bodies don't echo the key; headers, which might, are never read)."""
    try:
        e = r.json().get("error") or {}
        if isinstance(e, list):
            e = e[0] if e else {}
        return str(e.get("message") or e.get("code") or e)[:300]
    except Exception:  # noqa: BLE001
        return r.text[:300]
