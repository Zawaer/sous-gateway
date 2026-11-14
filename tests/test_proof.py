"""GET /proof and /proof/data: every figure comes from a file in the repo (or the loaded catalog), never typed in.

Offline: no Valio calls, no model calls; CI has no data/ catalog, so the allergen audit must say not available.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from sous import api, catalog, config, proof

EVALS = config.ROOT / "evals"


def _json(rel: str):
    return json.loads((config.ROOT / rel).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def proof_public(monkeypatch):
    monkeypatch.setenv("SOUS_PROOF_PUBLIC", "1")


@pytest.fixture
def no_catalog(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CATALOG_DB", tmp_path / "none.db")
    monkeypatch.setattr(config, "PRODUCTS_JSON", tmp_path / "none.json")
    proof._cache = None
    yield
    proof._cache = None  # never leak this test's view into another test


@pytest.fixture
def client():
    proof._cache = None
    yield TestClient(api.app)
    proof._cache = None


def test_proof_data_offline_without_catalog(no_catalog, client):
    r = client.get("/proof/data")
    assert r.status_code == 200
    d = r.json()
    a = d["allergen_audit"]
    assert a["available"] is False and "catalog" in a["reason"]
    assert "all_active" not in a and "food" not in a  # no number without a source
    assert d["headline"]  # the rest still renders


def test_proof_page_is_public_html(client):
    r = client.get("/proof")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "/proof/data" in r.text


def test_scripted_eval_matches_file(no_catalog, client):
    d = client.get("/proof/data").json()
    src = _json("evals/results.json")
    e = d["scripted_eval"]
    assert e["available"] and e["source"]["path"] == "evals/results.json"
    assert e["source"]["generated_at"] == src["generated_at"]
    for k, v in e["summary"].items():
        assert src["summary"][k] == v, k
    assert len(e["cases"]) == len(src["cases"])
    assert sum(c["passed"] for c in e["cases"]) == src["summary"]["cases_passed"]
    assert e["checks_total"] == sum(len(c["checks"]) for c in src["cases"])
    assert sum(e["unsafe_handled_by"].values()) == src["summary"]["unsafe_requests"]
    h = d["headline"]
    assert h["scripted_cases"] == {"passed": src["summary"]["cases_passed"], "total": src["summary"]["cases_total"],
                                   "source": "evals/results.json"}


def test_llm_models_match_files_and_oracle_is_not_a_model(no_catalog, client):
    d = client.get("/proof/data").json()["llm_agents"]
    files = {p.name: json.loads(p.read_text()) for p in EVALS.glob("llm_results.*.json")}
    model_files = {n: f for n, f in files.items() if f.get("provider") != "oracle"}
    assert len(d["models"]) == len(model_files)
    for m in d["models"]:
        f = files[m["source"]["path"].split("/")[-1]]
        assert m["model_label"] == f["model_label"] and m["source"]["generated_at"] == f["generated_at"]
        for k, v in m["summary"].items():
            assert f["summary"][k] == v, k
    assert all(m["provider"] != "oracle" for m in d["models"])
    if any(f.get("provider") == "oracle" for f in files.values()):
        assert any("oracle" in x["reason"] for x in d["excluded"])


def test_other_sections_match_files(no_catalog, client):
    d = client.get("/proof/data").json()
    for run in d["drafting"]["runs"]:
        f = _json(run["source"]["path"])
        assert (run["passed"], run["cases"]) == (f["passed"], f["cases"])
    rec = _json("evals/recipe_results.json")
    assert [(r["provider"], r["passed"], r["cases"]) for r in d["recipe"]["runs"]] == \
           [(r["provider"], r["passed"], r["cases"]) for r in rec]
    vis = _json("evals/vision/results.json")
    assert {v["variant"]: v["summary"]["silent_errors"] for v in d["vision"]["variants"]} == \
           {v["variant"]: v["summary"]["silent_errors"] for v in vis.values()}
    from sous import rules
    assert d["rules"]["count"] == len(rules.register()) == d["headline"]["rules"]
    c = d["conformance"]
    assert c["available"] and c["total"] == c["passed"] + c["failed"] + c["skipped"]
    assert c["kit_checks"] == 15 or c["kit_checks"] >= c["total"]
    if c["source"]["path"] == "docs/CONFORMANCE.md":
        assert c["tally_line_matches"] is True  # parsed rows agree with the run's own summary line


def test_missing_sources_say_not_available(tmp_path):
    assert proof.scripted_eval(tmp_path / "results.json")["available"] is False
    assert proof.recipe_eval(tmp_path / "x.json")["available"] is False
    assert proof.vision_eval(tmp_path / "x.json")["available"] is False
    assert proof.llm_evals(tmp_path)["available"] is False and proof.llm_evals(tmp_path)["models"] == []
    assert proof.draft_evals(tmp_path)["available"] is False
    assert proof.search_quality(tmp_path / "t.py")["available"] is False
    c = proof.conformance(tmp_path / "c.json", tmp_path / "C.md", tmp_path / "k.py")
    assert c["available"] is False and "passed" not in c


def test_stored_conformance_report_wins(tmp_path):
    rep = {"base": "https://gw.test", "summary": {"pass": 1, "fail": 1, "skip": 0},
           "checks": [{"id": "a", "title": "A", "status": "pass", "detail": "x"},
                      {"id": "b", "title": "B", "status": "fail", "detail": "y"}]}
    (tmp_path / "c.json").write_text(json.dumps(rep))
    c = proof.conformance(tmp_path / "c.json", tmp_path / "C.md", config.ROOT / "scripts" / "conformance.py")
    assert (c["passed"], c["failed"], c["total"], c["target"]) == (1, 1, 2, "https://gw.test")


def _p(pid, category, contains=(), may=(), free=(), deleted=False):
    return {"id": pid, "category": category, "deleted": deleted,
            "allergens": {"declared": bool(contains or may or free), "contains": list(contains),
                          "may_contain": list(may), "free_from": list(free)}}


def test_allergen_audit_counts_with_the_engines_definitions(monkeypatch, tmp_path):
    db = tmp_path / "catalog.db"
    rows = [_p("1", "1010", contains=["AM"]),            # food, declared, list
            _p("2", "1010"),                              # food, nothing
            _p("3", "1010", free=["AG"]),                 # food, declared, but no allergen list
            _p("4", "1010", free=["X99"]),                # food, declared, list (nothing to declare)
            _p("5", "6310"),                              # non-food (cleaning), nothing
            _p("6", "1010", deleted=True)]                # deleted: not counted
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE product (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    conn.executemany("INSERT INTO product VALUES (?, ?)", [(r["id"], json.dumps(r)) for r in rows])
    conn.commit()
    monkeypatch.setattr(config, "CATALOG_DB", db)
    monkeypatch.setattr(catalog._local, "conn", sqlite3.connect(db), raising=False)
    a = proof.allergen_audit()
    assert a["available"] and a["products_total"] == 6 and a["deleted"] == 1
    assert a["all_active"] == {"products": 5, "no_declaration": 2, "no_declaration_pct": 40.0,
                               "no_allergen_list": 3, "no_allergen_list_pct": 60.0}
    assert a["food"] == {"products": 4, "no_declaration": 1, "no_declaration_pct": 25.0,
                         "no_allergen_list": 2, "no_allergen_list_pct": 50.0}
    assert a["non_food"]["products"] == 1 and a["non_food"]["no_declaration"] == 1
    # 'no allergen list' is exactly where allergen_status cannot call an uncovered allergen safe
    assert catalog.allergen_status(rows[2], ["AM"])["status"] == "unknown"
    assert catalog.allergen_status(rows[3], ["AM"])["status"] == "safe"


def test_run_provenance(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_SHA", "abcdef0123")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    assert proof.run_provenance() == {"origin": "ci", "ci_run_id": "123", "commit": "abcdef0",
                                      "ci_run_url": "https://github.com/o/r/actions/runs/123"}
    monkeypatch.delenv("GITHUB_RUN_ID")
    assert proof.run_provenance()["origin"] == "local"


def test_source_reads_recorded_provenance(tmp_path):
    f = config.ROOT / "evals" / "results.json"
    s = proof._source(f, {"generated_at": "t", "provenance": {"origin": "ci", "ci_run_id": "9", "commit": "abc"}})
    assert (s["origin"], s["ci_run_id"], s["run_commit"], s["generated_at"]) == ("ci", "9", "abc", "t")
    assert proof._source(f, {})["origin"] == "not recorded"


def test_proof_private_until_submission(client, monkeypatch):
    monkeypatch.delenv("SOUS_PROOF_PUBLIC")
    monkeypatch.setattr(config, "APPROVER_CODE", "c0de")
    r = client.get("/proof")
    assert r.status_code == 200 and "published with our submission" in r.text and "Scripted eval" not in r.text
    assert client.get("/proof/data").status_code == 403
    assert client.get("/proof/data", headers={"x-approver-code": "c0de"}).status_code == 200
