"""Check "use Sous from the AI assistant you already have" end to end with the official MCP Python client.

    uv run python scripts/connect_check.py https://staging.135-181-63-134.sslip.io            # header + OAuth
    uv run python scripts/connect_check.py <base> --mode header --quote                       # one live quote

Creates ONE judge sandbox (POST /sandbox), then, over Streamable HTTP:
- header: <base>/mcp/ with `Authorization: Bearer <agent token>` (Claude Code, Cursor, VS Code);
- oauth:  <base>/connect/mcp with no credentials, as claude.ai / ChatGPT connect: 401 -> protected-resource
  metadata -> authorization-server metadata -> dynamic client registration -> authorize with S256 PKCE (the
  consent form is submitted with the sandbox's agent token, as a user pasting it would) -> token -> MCP.
Each mode: initialize, list tools, search_products, and with --quote one request_quote (live Valio lookup, counted
against the sandbox's budget; at most one per mode). Prints what it saw; never prints tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
from mcp.client.auth import OAuthClientProvider
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken


class Memory:
    def __init__(self):
        self.tokens, self.client = None, None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        self.client = info


async def session_run(url: str, http: httpx2.AsyncClient, sbx: dict, quote: bool) -> dict:
    hotel = next(m for m in sbx["mandates"] if m["scenario"] == "hotel")
    out = {}
    async with streamable_http_client(url, http_client=http) as (read, write):
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            out["server"] = f"{init.server_info.name} {init.server_info.version}, protocol {init.protocol_version}"
            tools = (await s.list_tools()).tools
            out["tools"] = len(tools)
            out["output_schemas"] = sum(1 for t in tools if t.output_schema)
            r = await s.call_tool("search_products", {"query": "laktoositon maito", "agent_id": hotel["agent_id"],
                                                      "limit": 5})
            found = (r.structured_content or {}).get("products") or []
            out["search"] = {"is_error": r.is_error, "products": [p["id"] for p in found][:5]}
            m = await s.call_tool("get_mandate", {"mandate_id": hotel["mandate_id"]})
            out["get_mandate"] = {"is_error": m.is_error, "id": ((m.structured_content or {}).get("mandate") or {}).get("id")}
            if quote:
                q = await s.call_tool("request_quote", {
                    "agent_id": hotel["agent_id"], "mandate_id": hotel["mandate_id"],
                    "delivery_date": sbx["mandates"][0].get("next_delivery_day") or _next_day(sbx),
                    "items": [{"product_ids": [p["id"] for p in found][:3] or ["464119"], "amount": 20,
                               "unit": "L"}]})
                body = q.structured_content or {}
                qq = body.get("quote") or {}
                out["request_quote"] = {"is_error": q.is_error, "error": body.get("error"), "quote_id": qq.get("id"),
                                        "total_ex_vat": qq.get("total_ex_vat"),
                                        "preview": (qq.get("mandate_preview") or {}).get("outcome")}
    return out


def _next_day(sbx: dict) -> str:
    body = json.loads(sbx["quickstart"]["curl_quote"].split("-d '", 1)[1].rstrip("'"))
    return body["delivery_date"]


async def header_mode(base: str, sbx: dict, quote: bool) -> dict:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {sbx['agent_token']}"})
    async with http:
        return await session_run(f"{base}/mcp/", http, sbx, quote)


async def oauth_mode(base: str, sbx: dict, quote: bool) -> dict:
    """What claude.ai does, with the browser step automated: GET the consent page, POST the agent token."""
    pending: dict = {}
    seen: dict = {}
    async with httpx.AsyncClient(timeout=30) as browser:
        unauth = await browser.post(f"{base}/connect/mcp", json={},
                                    headers={"Accept": "application/json, text/event-stream"})
        seen["unauthenticated"] = {"status": unauth.status_code,
                                   "www_authenticate": unauth.headers.get("www-authenticate")}

        async def redirect_handler(url: str) -> None:
            page = await browser.get(url)
            assert page.status_code == 200 and 'name="agent_token"' in page.text, page.text[:300]
            q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
            r = await browser.post(f"{base}/oauth/authorize",
                                   data={**q, "agent_token": sbx["agent_token"], "action": "allow"})
            assert r.status_code == 302, r.text[:300]
            back = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}
            pending.update(back)

        async def callback_handler() -> AuthorizationCodeResult:
            return AuthorizationCodeResult(code=pending["code"], state=pending.get("state"), iss=pending.get("iss"))

        storage = Memory()
        auth = OAuthClientProvider(
            server_url=f"{base}/connect/mcp",
            client_metadata=OAuthClientMetadata(client_name="sous connect_check", redirect_uris=[
                "http://localhost:33418/callback"], grant_types=["authorization_code", "refresh_token"],
                response_types=["code"], token_endpoint_auth_method="none"),
            storage=storage, redirect_handler=redirect_handler, callback_handler=callback_handler)
        http = create_mcp_http_client(auth=auth)
        async with http:
            seen |= await session_run(f"{base}/connect/mcp", http, sbx, quote)
        t = storage.tokens
        seen["oauth"] = {"client_registered": bool(storage.client and storage.client.client_id),
                         "access_token_prefix": t.access_token[:4] if t else None,
                         "refresh_token": bool(t and t.refresh_token), "expires_in": t.expires_in if t else None}
    return seen


def cors(base: str) -> dict:
    r = httpx.options(f"{base}/mcp/", headers={
        "Origin": "https://example-browser-client.dev", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type,mcp-protocol-version,mcp-session-id"})
    return {"preflight_status": r.status_code, "allow_origin": r.headers.get("access-control-allow-origin"),
            "allow_headers": r.headers.get("access-control-allow-headers"),
            "allow_methods": r.headers.get("access-control-allow-methods")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--mode", choices=["header", "oauth", "both"], default="both")
    ap.add_argument("--quote", action="store_true", help="one live request_quote per mode")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    r = httpx.post(f"{base}/sandbox", timeout=30)
    r.raise_for_status()
    sbx = r.json()
    report = {"sandbox": sbx["sandbox"]["id"], "cors": cors(base)}
    if a.mode in ("header", "both"):
        report["header"] = asyncio.run(header_mode(base, sbx, a.quote))
    if a.mode in ("oauth", "both"):
        report["oauth"] = asyncio.run(oauth_mode(base, sbx, a.quote))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
