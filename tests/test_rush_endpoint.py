"""POST /rush/run: the Monday rush in a separate process, with each buyer's timeline (web/live.html)."""

from fastapi.testclient import TestClient

from sous import api


def test_rush_run_returns_a_timeline_and_never_over_allocates(monkeypatch):
    monkeypatch.setattr(api, "RUSH_ARGS", ["--agents", "8", "--stock", "40", "--policy", "pro_rata",
                                           "--think-ms", "0,20", "--json"])
    api._RUSH.update(at=0.0, result=None)
    c = TestClient(api.app)
    r = c.post("/rush/run").json()
    assert r["agents"] == 8 and r["over_allocated"] == 0 and r["allocated"] <= r["stock"]
    assert all(b["events"] and b["events"][0]["step"] == "quote" for b in r["buyers"])
    assert r["product"]["id"] == "RUSH-BUTTER" and r["cached"] is False
    assert c.post("/rush/run").json()["cached"] is True
    api._RUSH.update(at=0.0, result=None)
