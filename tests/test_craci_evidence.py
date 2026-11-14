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


def _http(url, denied=False):
    return {"protocol": "http", "http": {"method": "GET", "originUrl": url, "ipAddress": "1.2.3.4",
                                         "statusCode": 403 if denied else 200, "bodySizeBytes": "5",
                                         "policyDenied": denied}}


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
    r = ce.check_egress(pol, _trace(_http("https://files.pythonhosted.org/a.whl"), _tls("example.org", 8443)))
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
    sha = next(iter(ce.lock_packages(lock)[("pypi", name, ver)]))
    r = ce.check_against_lock({"components": [{"purl": f"pkg:pypi/{name}@{ver}",
                                               "hashes": [{"alg": "SHA-256", "content": sha}]},
                                              {"purl": "pkg:pypi/not-a-real-pkg@1"}]}, lock)
    assert r["sha256_match"] == 1 and r["not_in_lock"] == ["not-a-real-pkg==1"]
