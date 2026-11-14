"""scripts/demo_prep.py against the in-process app (TestClient, test_sandbox.py's one-product catalog and mocked
Valio Aimo). Offline. Checks: it gets ready, never prints a token, writes the handoff mode 600 outside any git
checkout, reuses a still-usable sandbox, and fails loudly when something a judge would hit is broken."""

import json
import stat

import pytest
from fastapi.testclient import TestClient

from scripts import demo_prep
from sous import api, clock, sandbox, store, substitutes

from test_sandbox import NOW, env  # noqa: F401  (autouse: temp DB, approver code, mocked Valio, fixed clock)

BASE = "https://sous.test"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: NOW)  # /health's clock agrees with the gateway's fixed test clock
    monkeypatch.setattr(substitutes, "_same_category", lambda category: [])  # no catalog index in CI
    # no lifespan: MCP's session manager runs once per process, so the MCP probe is off here (p.check_mcp)
    return TestClient(api.app, base_url=BASE, headers={"cf-connecting-ip": "10.7.7.7"})


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    home, repo = tmp_path / "home", tmp_path / "repo"
    home.mkdir()
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo)  # run from inside a checkout, like the real thing
    return home, repo


def prep(client, out):
    p = demo_prep.Prep(BASE, client, out)
    p.milk, p.tomatoes, p.check_mcp = ["OAT"], "OAT", False
    return p


def test_ready_writes_private_handoff_and_prints_no_tokens(client, dirs, capsys, env):  # noqa: F811
    home, repo = dirs
    out = demo_prep.default_out()
    assert out.parent == home
    p = prep(client, out)
    assert p.run() == 0, p.problems
    assert env.calls <= 4  # the two warm-up lookups (price + availability each), nothing more

    h = json.loads(out.read_text())
    txt = out.with_suffix(".txt")
    for f in (out, txt):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
        assert repo not in f.resolve().parents
    assert not list(repo.rglob("sous-demo-*"))
    tokens = (h["agent_token"], h["approver_token"])
    assert all(t in txt.read_text() for t in tokens)  # the private file has them...
    printed = capsys.readouterr()
    for t in tokens:  # ...stdout/stderr never do, only the last 4 chars
        assert t not in printed.out + printed.err
        assert t[-4:] in printed.out
    sid = h["sandbox_id"]
    assert h["open_in_order"][0]["url"] == f"{BASE}/ui/?sandbox={sid}#/inbox"
    assert "break.html?sandbox=" in h["open_in_order"][2]["url"]
    assert h["mcp_connect"].startswith("claude mcp add --transport http sous")
    assert h["held_order_id"] and len(h["stage_steps"]) >= 3

    o = client.get(f"/orders?sandbox={sid}", headers={"X-Approver-Code": h["approver_token"]}).json()["orders"]
    assert [x["status"] for x in o] == ["pending_approval"]  # no order placed, the seeded one still waits

    # second run reuses the sandbox (its held order is still waiting); --reset makes a new one
    assert prep(client, out).run() == 0
    assert json.loads(out.read_text())["sandbox_id"] == sid
    assert prep(client, out).run(reset=True) == 0
    assert json.loads(out.read_text())["sandbox_id"] != sid


def test_out_inside_a_git_checkout_is_refused(client, dirs):
    _, repo = dirs
    out = repo / "sous-demo.json"
    p = prep(client, out)
    assert p.run() == 1
    assert not out.exists() and any("git checkout" in x for x in p.problems)


def test_check_only_makes_no_sandbox(client, dirs, env):  # noqa: F811
    p = prep(client, None)
    assert p.run(check_only=True) == 0, p.problems
    assert env.calls == 0
    sandbox._ensure()
    assert store._exec("SELECT id FROM sandbox")[0] == []


def test_broken_page_fails(client, dirs, monkeypatch):
    monkeypatch.setattr(demo_prep, "PAGES", [*demo_prep.PAGES, "/no-such-page"])
    p = prep(client, None)
    assert p.run(check_only=True) == 1
    assert any("/no-such-page" in x for x in p.problems)


def test_used_up_seeded_order_is_not_reused(client, dirs):
    home, _ = dirs
    out = home / "d.json"
    assert prep(client, out).run() == 0
    h = json.loads(out.read_text())
    r = client.post(f"/orders/{h['held_order_id']}/approve", json={"by": "rehearsal"},
                    headers={"X-Approver-Code": h["approver_token"]})
    assert r.status_code == 200, r.text
    p = prep(client, out)
    assert p.run() == 0
    assert json.loads(out.read_text())["sandbox_id"] != h["sandbox_id"]
    assert any("not reusing" in w for w in p.warnings)
