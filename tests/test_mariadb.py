"""MariaDB system-versioning tests; run when VALTAKIRJA_TEST_MARIADB_URL is set (CI has a MariaDB service)."""

import os
import time
from datetime import date

import pytest

from valtakirja import config, service as S, store
from valtakirja.mandate import Mandate

URL = os.getenv("VALTAKIRJA_TEST_MARIADB_URL")
pytestmark = pytest.mark.skipif(not URL, reason="needs VALTAKIRJA_TEST_MARIADB_URL")


@pytest.fixture()
def maria(monkeypatch, tmp_path):
    from valtakirja import signing
    monkeypatch.setattr(signing, "KEY_PATH", tmp_path / "key.pem")
    signing._key.cache_clear()
    monkeypatch.setattr(config, "DB_URL", URL)
    monkeypatch.setattr(store, "_conn", None)
    for t in ("audit_event", "purchase_order", "quote", "mandate"):
        store._exec(f"DROP TABLE IF EXISTS {t}")
    monkeypatch.setattr(store, "_conn", None)
    yield
    store._conn = None
    signing._key.cache_clear()


def test_mandate_as_of_and_order_versions(maria):
    S.issue_mandate(Mandate(id="M1", buyer="B", agent_id="a", issued_by="h", auto_approve_limit_eur=400))
    time.sleep(0.05)
    decided = store.now_iso()
    time.sleep(0.05)
    S.issue_mandate(Mandate(id="M1", buyer="B", agent_id="a", issued_by="h", auto_approve_limit_eur=900))
    assert store.mandate_as_of("M1", decided)["auto_approve_limit_eur"] == 400
    assert len(store.mandate_versions("M1")) == 2

    o = {"id": "PO-1", "quote_id": "Q-1", "mandate_id": "M1", "agent_id": "a", "status": "pending_approval",
         "total_ex_vat": 10.0, "idempotency_key": None, "created_at": decided, "updated_at": decided}
    store.put_order(o)
    store.put_order({**o, "status": "ordered"})
    assert [v["status"] for v in store.order_versions("PO-1")] == ["pending_approval", "ordered"]


def test_audit_tamper_seen_by_versioning(maria):
    for i in range(3):
        store.audit("e", "t", "system", None, None, {"i": i})
    assert store.verify_chain()["ok"]
    store._exec("UPDATE audit_event SET actor = 'x' WHERE seq = 2")
    r = store.verify_chain()
    assert not r["ok"] and r["system_versioning"]["seqs"] == [2]


def test_reconnect_keeps_utc_session_time_zone(maria):
    store._exec("SELECT 1")
    store._conn.close()  # e.g. wait_timeout dropped the connection; ping(reconnect=True) opens a new one
    assert store._exec("SELECT @@session.time_zone AS tz")[0][0]["tz"] == "+00:00"


def test_new_order_insert_never_overwrites(maria):
    o = {"id": "PO-1", "quote_id": "Q-1", "mandate_id": "M1", "agent_id": "a", "status": "pending_approval",
         "total_ex_vat": 10.0, "idempotency_key": "k1", "created_at": store.now_iso(), "updated_at": store.now_iso()}
    store.insert_order(o)
    with pytest.raises(store.Conflict):
        store.insert_order({**o, "id": "PO-2", "status": "ordered"})
    assert store.get_order("PO-1")["status"] == "pending_approval" and store.get_order("PO-2") is None
    assert store.update_order({**o, "status": "ordered"}, expected_status="pending_approval")
    assert not store.update_order({**o, "status": "rejected"}, expected_status="pending_approval")
