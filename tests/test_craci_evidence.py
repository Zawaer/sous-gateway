"""scripts/craci_evidence.py: declared egress policy parsing and the trace check (no network)."""

from pathlib import Path

from scripts import craci_evidence as ce

ROOT = Path(__file__).resolve().parent.parent


def _policies():
    return ce.declared_policies((ROOT / ".github/workflows/ci.yml").read_text())


def _trace(*obs):
    return {"observations": list(obs)}


def _tls(host, port=443, allowed=True, out=100):
    return {"protocol": "tcp", "tcp": {"remoteIp": "1.2.3.4", "resolvedHosts": [host], "port": port,
                                       "bytesToRemote": str(out), "bytesFromRemote": "10", "allowed": allowed,
                                       "tlsAlpn": [], "tlsServerName": host}}


def _http(url, denied=False, rule=None):
    return {"protocol": "http", "http": {"method": "GET", "originUrl": url, "ipAddress": "1.2.3.4",
                                         "statusCode": 403 if denied else 200, "bodySizeBytes": "5",
                                         "policyDenied": denied, "matchedRule": rule}}


def test_policies_parsed_from_ci_yml():
    p = {v["job"]: v for v in _policies().values()}
    assert p["test"]["mode"] == "observe"
    assert p["smoke"]["mode"] == "deny" and p["smoke"]["presets"] is None  # deny, but every preset stays on
    assert p["eval"]["presets"] == ["pypi"]
    assert ["junction26.valioaimo.fi"] == p["eval"]["tls"][0]["hosts"]
    assert p["craci-evidence"]["mode"] == "deny"


def test_eval_job_declared_hosts_pass_and_others_fail():
    pol = next(v for v in _policies().values() if v["job"] == "eval")
    ok = ce.check_egress(pol, _trace(_tls("junction26.valioaimo.fi"), _tls("x.aivencloud.com", 23770),
                                     _http("https://files.pythonhosted.org/a.whl"),
                                     _tls("results-receiver.actions.githubusercontent.com")))
    assert ok["verdict"] == "pass" and not ok["undeclared_allowed"]
    bad = ce.check_egress(pol, _trace(_tls("x.aivencloud.com", 443), _tls("evil.example")))
    assert bad["verdict"] == "fail"
    assert bad["undeclared_allowed"] == ["evil.example:443", "x.aivencloud.com:443"]


def test_blocked_attempts_are_reported_not_failed():
    pol = next(v for v in _policies().values() if v["job"] == "eval")
    r = ce.check_egress(pol, _trace(_http("https://evil.example/x", denied=True)))
    assert r["verdict"] == "pass" and r["blocked"] == ["evil.example:443"]


def test_observe_job_gets_a_suggested_policy():
    pol = next(v for v in _policies().values() if v["job"] == "test")
    r = ce.check_egress(pol, _trace(_http("https://files.pythonhosted.org/a.whl", rule="pypi-pypi.org"),
                                    _tls("example.org", 8443)))
    assert r["verdict"] == "observe"
    s = ce.suggest_policy(r)
    assert "presets: [pypi]" in s and "example.org" in s and "8443" in s


def test_sbom_diff_and_lock_check():
    a = {"components": [{"purl": "pkg:pypi/Foo_Bar@1.0"}, {"purl": "pkg:deb/debian/libc6@2.0?arch=amd64"}]}
    b = {"components": [{"purl": "pkg:pypi/foo-bar@1.0"}, {"purl": "pkg:pypi/baz@2"}]}
    d = ce.diff_sboms(a, b, "craci", "syft")
    assert d["in_both"] == 1 and d["only_syft"] == ["pkg:pypi/baz@2"]
    lock = (ROOT / "uv.lock").read_text()
    name, ver = next((k[1], k[2]) for k in ce.lock_packages(lock))
    locked = ce.lock_packages(lock)[("pypi", name, ver)]
    sha, fname = next(iter(locked["sha256"])), next(iter(locked["files"]))
    r = ce.check_against_lock({"components": [
        {"purl": f"pkg:pypi/{name}@{ver}?file_name={fname}", "hashes": [{"alg": "SHA-256", "content": sha}]},
        {"purl": f"pkg:pypi/{name}@{ver}?file_name=evil-{fname}"},
        {"purl": "pkg:pypi/hatchling@1.0"}, {"purl": "pkg:pypi/not-a-real-pkg@1"}]}, lock)
    assert r["sha256_match"] == 1 and r["wheel_file_in_lock"] == 1
    assert r["wheel_file_not_in_lock"] == [f"evil-{fname}"]
    assert r["unexplained_not_in_lock"] == ["not-a-real-pkg==1"]


def test_cache_and_image_refs_from_craci_sbom():
    sb = {"components": [
        {"purl": "pkg:ghacache/1/2@abc?entry_id=42&key=valio-data-deadbeef", "properties": []},
        {"purl": "pkg:oci/python@sha256:" + "a" * 64 + "?repository_url=registry-1.docker.io%2Flibrary%2Fpython"},
        {"purl": "pkg:github/Zawaer/sous@" + "b" * 40, "properties": [{"name": "craci:source:restored",
                                                                     "value": "true"}]}]}
    assert ce.cache_refs(sb)[0]["entry_id"] == "42" and ce.cache_refs(sb)[0]["key"] == "valio-data-deadbeef"
    assert ce.oci_refs(sb)[0]["repository_url"] == "registry-1.docker.io/library/python"
    s = ce.sbom_summary(sb, "Zawaer/sous")
    assert s["restored_from_cache"] == 1 and s["own_repo_commits"] == 1


def test_runner_probe_is_accepted_only_in_its_exact_shape():
    pol = next(v for v in _policies().values() if v["job"] == "draft-eval")
    probe = _http("https://pkg.github.com/_dns")
    assert ce.check_egress(pol, _trace(probe))["verdict"] == "pass"
    upload = _http("https://pkg.github.com/upload")
    assert ce.check_egress(pol, _trace(probe, upload))["undeclared_allowed"] == ["pkg.github.com:443"]


def _match(vid, purl, sev, state="fixed", versions=("2.0",)):
    return {"vulnerability": {"id": vid, "severity": sev, "fix": {"state": state, "versions": list(versions)}},
            "artifact": {"purl": purl, "name": purl.split("/")[-1].split("@")[0], "version": purl.split("@")[-1],
                         "type": "python"}}


def test_vulnerabilities_from_craci_sboms_cross_checked_and_gated():
    jobs = {"test": {"components": [{"purl": "pkg:pypi/Hatchling@1.0?file_name=x.whl"},
                                    {"purl": "pkg:pypi/requests@2.0"},
                                    {"purl": "pkg:oci/python@sha256:" + "a" * 64}]},
            "docker": {"components": [{"purl": "pkg:pypi/requests@2.0",
                                       "properties": [{"name": "craci:source:restored", "value": "true"}]}]}}
    sbom, where = ce.craci_scan_sbom(jobs)
    assert sorted(c["purl"] for c in sbom["components"]) == ["pkg:pypi/Hatchling@1.0", "pkg:pypi/requests@2.0"]
    assert where[("pypi", "requests", "2.0")] == {"jobs": ["docker", "test"], "restored_only": False}
    craci = ce.grype_matches({"matches": [_match("GHSA-1", "pkg:pypi/hatchling@1.0", "Critical"),
                                          _match("GHSA-2", "pkg:pypi/requests@2.0", "Medium")]})
    image = ce.grype_matches({"matches": [_match("GHSA-2", "pkg:pypi/requests@2.0", "Medium"),
                                          _match("CVE-3", "pkg:pypi/pip@25.0", "Critical", "not-fixed", ())]})
    v = ce.cross_check_vulns(craci, image, where)
    assert v["in_both"] == ["GHSA-2 requests@2.0"]
    assert [f["id"] for f in v["only_in_craci_jobs"]] == ["GHSA-1"] and v["only_in_craci_jobs"][0]["jobs"] == ["test"]
    assert v["image_pypi_not_downloaded_by_any_job"] == ["CVE-3 pip@25.0"]
    # a Critical with no fix is reported, not blocking; a Critical with a fix blocks
    assert [f["id"] for f in v["blocking_critical_with_fix"]] == ["GHSA-1"]
    assert v["craci_sboms"]["by_severity"]["Critical"] == {"total": 1, "fix_available": 1}
