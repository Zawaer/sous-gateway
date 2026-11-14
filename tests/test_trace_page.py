"""web/trace.html + web/trace.js, the trust board: served under /ui, no external scripts, and every gateway
endpoint the page calls exists. Offline: no data, no Valio calls."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from sous import api

WEB = Path(__file__).resolve().parents[1] / "web"
HTML = (WEB / "trace.html").read_text(encoding="utf-8")
JS = (WEB / "trace.js").read_text(encoding="utf-8")

# What trace.js reads or posts, as the gateway's route templates.
USED = {
    ("GET", "/orders"), ("GET", "/orders/{order_id}"), ("GET", "/quotes/{quote_id}"),
    ("GET", "/mandates/{mandate_id}"), ("GET", "/audit"), ("GET", "/audit/verify"),
    ("GET", "/.well-known/jwks.json"), ("GET", "/rules"), ("GET", "/rules/{rule_id}"),
    ("GET", "/orders/{order_id}/peppol/order.xml"), ("GET", "/orders/{order_id}/peppol/order-response.xml"),
    ("POST", "/sandbox"), ("POST", "/quotes"), ("POST", "/orders"),
}


def _routes() -> set[tuple[str, str]]:
    return {(m, r.path) for r in api.app.routes for m in (getattr(r, "methods", None) or ())}


def test_every_endpoint_the_page_uses_exists():
    missing = USED - _routes()
    assert not missing, f"trace.js calls endpoints the gateway does not have: {sorted(missing)}"


def test_the_page_names_each_endpoint_it_uses():
    # The static part of each template appears in trace.js, so USED stays in step with the code.
    for _, path in USED:
        static = path.split("{")[0]
        assert static in JS, f"{path} is listed as used but trace.js never calls {static}"


def test_only_local_scripts_and_known_fonts():
    assert re.findall(r'<script[^>]*src="([^"]+)"', HTML) == ["trace.js"]
    for href in re.findall(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"', HTML):
        assert href == "tokens.css" or href.startswith("https://fonts.googleapis.com/"), href


def test_served_under_ui():
    c = TestClient(api.app)
    assert c.get("/ui/trace.html").status_code == 200
    assert c.get("/ui/trace.js").status_code == 200


def test_entry_links():
    assert 'href="/ui/trace.html"' in (WEB / "live.html").read_text(encoding="utf-8")
    assert "/ui/trace.html?order=" in (WEB / "app.js").read_text(encoding="utf-8")
