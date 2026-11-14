"""web/agent.html when the reference buyer agent (/agent/) is off: one calm notice, no dead cards or links.
Offline: no data, no Valio calls."""

import re
from html.parser import HTMLParser
from pathlib import Path

from fastapi.testclient import TestClient

from sous import api, config

PAGE = (Path(__file__).resolve().parents[1] / "web" / "agent.html").read_text(encoding="utf-8")


def test_agent_status_404s_when_the_buyer_agent_is_off():
    if config.env("BUYER_AGENT") == "1":
        return  # mounted on this machine: nothing to check
    assert TestClient(api.app).get("/agent/status").status_code == 404


class _Links(HTMLParser):
    """Every /agent/ link and every card that calls /agent/, with whether it sits inside a .needs-agent element."""

    def __init__(self):
        super().__init__()
        self.stack: list[bool] = []
        self.bad: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        inside = (self.stack[-1] if self.stack else False) or "needs-agent" in (a.get("class") or "").split()
        if tag == "a" and (a.get("href") or "").startswith("/agent/") and not inside:
            self.bad.append(a["href"])
        for key in ("menu-go", "ask-go", "draft-go", "photo-file"):
            if a.get("id") == key and not inside:
                self.bad.append(key)
        if tag not in ("input", "img", "br", "meta", "link", "source"):
            self.stack.append(inside)

    def handle_endtag(self, tag):
        if tag not in ("input", "img", "br", "meta", "link", "source") and self.stack:
            self.stack.pop()


def test_everything_that_needs_the_agent_is_hidden_with_one_notice():
    p = _Links()
    p.feed(PAGE.split("<script>")[0])
    assert p.bad == []
    assert 'id="agent-off" hidden' in PAGE and "isn't switched on on this server" in PAGE
    assert "judge-path-in-5-minutes" in PAGE and "SOUS_BUYER_AGENT=1" in PAGE
    # a 404 from /agent/status switches the page to the notice (and offers break.html only when it exists)
    assert re.search(r"e\.status === 404\) return agentOff\(\)", PAGE)
    assert 'fetch("break.html", {method: "HEAD"})' in PAGE


def test_agent_link_opens_the_page_whether_or_not_the_agent_is_mounted():
    c = TestClient(api.app)
    for path in ("/agent", "/agent/"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code in (302, 307) and r.headers["location"] == "/ui/agent.html"
