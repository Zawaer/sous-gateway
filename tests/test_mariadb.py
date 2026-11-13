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
