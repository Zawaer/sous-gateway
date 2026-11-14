"""The Monday rush (scripts/rush.py), offline: 20 agents for different buyers order one short product at once
through the real gateway (TestClient); Valio's stock is never over-allocated, under either allocation policy."""

from pathlib import Path

import pytest

from scripts import rush


@pytest.mark.parametrize("policy", ["first_come", "pro_rata"])
def test_rush_20_agents_never_over_allocates(policy, tmp_path):
    shm = Path("/dev/shm")  # tmpfs when there is one: the rush commits often, a slow disk only adds time
    r = rush.run(agents=20, stock=150, policy=policy, think_ms=(0, 20),
                 data_dir=str(shm if shm.is_dir() else tmp_path))
    assert r["demand"] > r["stock"]
    assert r["placed"] <= r["stock"] and r["allocated"] <= r["stock"] and r["over_allocated"] == 0
    assert r["placed"] >= 0.9 * r["stock"]  # the stock is used, not left idle
    assert r["refusals"].get("availability.allocated")
    assert all(b["status"] != "none" or b["got"] == 0 for b in r["buyers"])
