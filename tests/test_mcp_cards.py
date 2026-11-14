"""MCP Apps cards (io.modelcontextprotocol/ui): tools point at ui://sous/card.html, the card is served as an app
resource, the server advertises the extension, and UI tools' results carry the gateway's keys in _meta. Offline."""

import asyncio

from mcp import Client

from sous import config, signing
from sous import mcp_server as M


def run(coro):
    return asyncio.run(coro)


def test_ui_tools_point_at_the_card_and_others_dont():
    tools = {t.name: t for t in run(M.mcp.list_tools())}
    for name in M.UI_TOOLS:
        assert tools[name].meta["ui"]["resourceUri"] == M.CARD_URI
        assert tools[name].meta["ui/resourceUri"] == M.CARD_URI  # the flat key older hosts read
    assert not (tools["search_products"].meta or {}).get("ui")


def test_card_resource_is_an_mcp_app_and_speaks_the_protocol():
    res = {str(r.uri): r for r in run(M.mcp.list_resources())}
    assert res[M.CARD_URI].mime_type == "text/html;profile=mcp-app"
    html = list(run(M.mcp.read_resource(M.CARD_URI)))[0].content
    for needle in ("ui/initialize", "ui/notifications/initialized", "ui/notifications/tool-result",
                   "ui/notifications/size-changed", "ui/open-link", "Ed25519", M.CARD_META_KEY):
        assert needle in html, needle
    assert "<script src=" not in html and "fetch(" not in html  # no network, no libraries


def test_server_advertises_the_apps_extension():
    async def caps():
        async with Client(M.mcp) as c:
            return c.server_capabilities
    assert "io.modelcontextprotocol/ui" in (run(caps()).extensions or {})


def test_ui_results_carry_the_keys_in_meta_not_in_the_content(monkeypatch, tmp_path):
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    r = M._result("place_order", {"id": "PO-1", "status": "ordered"})
    meta = r.meta[M.CARD_META_KEY]
    assert meta["jwks"] == signing.jwks() and meta["gateway"] == config.PUBLIC_BASE_URL
    assert "jwks" not in r.content[0].text and "jwks" not in r.structured_content  # never in what the model reads
    assert M._result("search_products", {"products": []}).meta is None
    signing._key.cache_clear()
