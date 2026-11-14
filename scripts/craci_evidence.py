"""Egress proof for a trust gateway, built from the CRACI API (docs/CRACI.md).

For one workflow run of this repo it:

1. reads every job's CRACI network trace and checks each outbound connection against the egress
   allowlist that job *declares* in .github/workflows/ci.yml (the `network:` input of its
   cracicorp/setup step, parsed here independently of CRACI). A deny-mode job that reached a host its
   declaration doesn't explain fails the check: "deny-by-default" becomes evidence a buyer can read;
2. downloads each job's CRACI CycloneDX SBOM (with its completeness verdict) and diffs the docker job's
   SBOM against our own syft SBOM of the image, and the test job's PyPI downloads against uv.lock
   (versions and sha256 hashes);
3. asks CRACI who created the image (OCI manifest provenance) and every GitHub Actions cache on main
   (cache provenance: a cache nobody on CRACI created is a cache whose contents nobody observed);
4. writes one evidence bundle (JSON), signs it as a compact JWS (typ sous-ci-evidence+jws) with Sous's
   own Ed25519 signing code, and a Markdown summary for the job page.

    uv run python scripts/craci_evidence.py --run-id 123 [--raw-dir raw/] [--replay raw/]

Env: CRACI_API_TOKEN (CRACI API), GITHUB_TOKEN (job list, caches), GITHUB_REPOSITORY.
`--replay DIR` rebuilds the bundle from a previous run's raw API responses, offline (for tests/debugging).
Exit 1 if any deny-mode job reached an undeclared host; 0 otherwise (missing data is reported, not fatal).
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CRACI_API = "https://api.craci.com"
GITHUB_API = "https://api.github.com"
SETUP_ACTION = "cracicorp/setup@"
SELF_JOB = "craci-evidence"

# Hosts behind each built-in CRACI preset we use or might use (docs.craci.com/guides/setup-action-network).
# Our own reading, so the check doesn't just echo CRACI's verdict back.
PRESET_HOSTS = {
    "pypi": ["pypi.org", "files.pythonhosted.org", "pypi.python.org"],
    "dockerhub": ["registry-1.docker.io", "auth.docker.io", "index.docker.io", "production.cloudflare.docker.com",
                  "docker.io"],
    "ghcr": ["ghcr.io", "pkg-containers.githubusercontent.com"],
    "github": ["github.com", "codeload.github.com", "objects.githubusercontent.com"],
    "debian": ["deb.debian.org", "security.debian.org", "*.debian.org"],
    "ubuntu": ["archive.ubuntu.com", "security.ubuntu.com", "ports.ubuntu.com", "*.ubuntu.com"],
    "npm": ["registry.npmjs.org", "registry.yarnpkg.com"],
    "cargo": ["crates.io", "index.crates.io", "static.crates.io"],
    "golang": ["proxy.golang.org", "sum.golang.org"],
}
# "CRACI always allows CI job and cache management traffic to GitHub services": the runner talking to
# GitHub (job control, logs, artifacts, caches, checkout). Listed separately in the evidence, never hidden.
# Narrow on purpose (taken from real traces, where CRACI tags them github-actions-* / github-downloads-*): a
# blanket *.blob.core.windows.net or *.githubusercontent.com would hide an exfiltration path to any Azure account.
GITHUB_INFRA = ["github.com", "codeload.github.com", "api.github.com", "*.actions.githubusercontent.com",
                "productionresultssa*.blob.core.windows.net", "cache.blob.core.windows.net"]


# ---------------------------------------------------------------- declared policy (ci.yml)

def declared_policies(workflow_text: str) -> dict:
    """{display name: policy} for every job, from the cracicorp/setup step's `network` input.

    policy = {"job": key, "mode": "observe" | "deny" | "allow", "presets": [...] | None (all),
              "tls": [{"hosts": [...], "cidrs": [...], "ports": [...]}], "tcp": [...]}
    """
    wf = yaml.safe_load(workflow_text)
    out = {}
    for key, job in (wf.get("jobs") or {}).items():
        name = job.get("name") or key
        net = None
        for step in job.get("steps") or []:
            if str(step.get("uses", "")).startswith(SETUP_ACTION):
                net = (step.get("with") or {}).get("network")
        pol = {"job": key, "mode": "observe", "presets": None, "tls": [], "tcp": []}
        if net is not None:
            n = yaml.safe_load(net) if isinstance(net, str) else net
            n = n or {}
            pol["mode"] = n.get("default", "deny")
            pol["presets"] = n.get("presets")  # None = every preset
            for c in n.get("connections") or []:
                if "tls" in c:
                    t = c["tls"]
                    pol["tls"].append({"hosts": t.get("serverNamePatterns") or [], "cidrs": t.get("cidrs") or [],
                                       "ports": t.get("ports") or []})
                if "tcp" in c:
                    t = c["tcp"]
                    pol["tcp"].append({"hosts": t.get("hostPatterns") or [], "cidrs": t.get("cidrs") or [],
                                       "ports": t.get("ports") or []})
        out[name] = pol
    return out


def _host_match(host: str, patterns) -> bool:
    host = (host or "").lower().rstrip(".")
    for p in patterns:
        p = p.lower()
        if p.startswith("*.") and (host.endswith(p[1:]) and host != p[2:]):
            return True
        if "*" in p and not p.startswith("*.") and fnmatch.fnmatch(host, p):
            return True
        if host == p:
            return True
    return False


def explain(policy: dict, host: str, port: int | None) -> str | None:
    """Which declared rule (or implicit GitHub traffic) allows host:port; None if nothing does."""
    for t in policy["tls"]:
        if _host_match(host, t["hosts"]) and (port is None or not t["ports"] or port in t["ports"]):
            return f"tls {','.join(t['hosts'])}:{','.join(map(str, t['ports']))}"
    for t in policy["tcp"]:
        if _host_match(host, t["hosts"]) and (port is None or not t["ports"] or port in t["ports"]):
            return f"tcp {','.join(t['hosts'])}"
    presets = PRESET_HOSTS.keys() if policy["presets"] is None else policy["presets"]
    for p in presets:
        if _host_match(host, PRESET_HOSTS.get(p, [])) and port in (None, 80, 443):
            return f"preset {p}"
    if _host_match(host, GITHUB_INFRA) and port in (None, 80, 443):
        return "github-infra (implicit)"
    return None


# ---------------------------------------------------------------- network trace

def connections(trace: dict) -> list[dict]:
    """Flatten a CRACI network trace into one row per outbound access (DNS lookups counted separately)."""
    rows = []
    for o in trace.get("observations") or []:
        proto = o.get("protocol")
        if proto == "http":
            h = o["http"]
            u = urllib.parse.urlsplit(h.get("originUrl", ""))
            rows.append({"proto": "http", "host": (u.hostname or h.get("ipAddress", "")).lower(),
                         "port": u.port or (443 if u.scheme == "https" else 80),
                         "allowed": not h.get("policyDenied", False), "rule": h.get("matchedRule"),
                         "deny_reason": h.get("denyReason"), "bytes_out": 0,
                         "bytes_in": int(h.get("bodySizeBytes") or 0), "url": h.get("originUrl", "")})
        elif proto == "tcp":
            t = o["tcp"]
            host = t.get("tlsServerName") or next(iter(t.get("resolvedHosts") or []), None) or t.get("remoteIp", "")
            rows.append({"proto": "tls" if t.get("tlsServerName") else "tcp", "host": host.lower(),
                         "port": t.get("port"), "allowed": bool(t.get("allowed")), "rule": t.get("matchedRule"),
                         "deny_reason": t.get("denyReason"), "bytes_out": int(t.get("bytesToRemote") or 0),
                         "bytes_in": int(t.get("bytesFromRemote") or 0), "ip": t.get("remoteIp"),
                         "error": t.get("error")})
        elif proto == "ssh_git":
            s = o["sshGit"]
            rows.append({"proto": "ssh_git", "host": s.get("host", "").lower(), "port": s.get("port"),
                         "allowed": bool(s.get("allowed")), "rule": s.get("matchedRule"),
                         "deny_reason": s.get("denyReason"), "bytes_out": int(s.get("bytesToUpstream") or 0),
                         "bytes_in": int(s.get("bytesFromUpstream") or 0)})
    return rows


def check_egress(policy: dict, trace: dict) -> dict:
    """Per-destination table and verdict for one job."""
    rows = connections(trace)
    dests: dict = {}
    for r in rows:
        k = (r["host"], r["port"])
        d = dests.setdefault(k, {"host": r["host"], "port": r["port"], "protos": set(), "allowed": 0, "denied": 0,
                                 "bytes_out": 0, "bytes_in": 0, "craci_rules": set(), "deny_reasons": set()})
        d["protos"].add(r["proto"])
        d["allowed" if r["allowed"] else "denied"] += 1
        d["bytes_out"] += r["bytes_out"]
        d["bytes_in"] += r["bytes_in"]
        if r.get("rule"):
            d["craci_rules"].add(r["rule"])
        if r.get("deny_reason"):
            d["deny_reasons"].add(r["deny_reason"])
    undeclared, out = [], []
    for d in sorted(dests.values(), key=lambda d: (d["host"], d["port"] or 0)):
        why = explain(policy, d["host"], d["port"])
        d["declared_by"] = why
        if policy["mode"] != "observe" and d["allowed"] and why is None:
            undeclared.append(f"{d['host']}:{d['port']}")
        for s in ("protos", "craci_rules", "deny_reasons"):
            d[s] = sorted(d[s])
        out.append(d)
    if policy["mode"] == "observe":
        verdict = "observe"   # no allowlist declared: the trace documents, it can't prove
    else:
        verdict = "fail" if undeclared else "pass"
    return {"mode": policy["mode"], "verdict": verdict, "undeclared_allowed": undeclared,
            "blocked": [f"{d['host']}:{d['port']}" for d in out if d["denied"]],
            "connections": len(rows), "dns_lookups": sum(1 for o in trace.get("observations") or []
                                                          if o.get("protocol") == "dns"),
            "destinations": out}


def suggest_policy(check: dict) -> str:
    """The narrowest `network:` block that would have allowed what an observe-mode job did.

    CRACI's matchedRule is the ground truth here: traffic it matched to no rule went out only because the job
    had no policy, so under `default: deny` it needs an explicit connection."""
    presets, tls = set(), defaultdict(set)
    for d in check["destinations"]:
        if not d["allowed"] or _host_match(d["host"], GITHUB_INFRA):
            continue
        if not d.get("craci_rules"):
            tls[d["port"]].add(d["host"])
            continue
        for p, hosts in PRESET_HOSTS.items():
            if _host_match(d["host"], hosts):
                presets.add(p)
                break
    lines = ["default: deny", f"presets: [{', '.join(sorted(presets))}]"]
    if tls:
        lines.append("connections:")
        for port, hosts in sorted(tls.items(), key=lambda kv: kv[0] or 0):
            lines += [f"  - tls:", f"      serverNamePatterns: [{', '.join(sorted(hosts))}]", f"      ports: [{port}]"]
    return "\n".join(lines)


# ---------------------------------------------------------------- SBOMs

def _norm_name(t: str, name: str) -> str:
    name = name.lower()
    return re.sub(r"[-_.]+", "-", name) if t == "pypi" else name


def purl_key(purl: str) -> tuple[str, str, str] | None:
    """(type, name, version) from a package URL, names normalised per ecosystem."""
    m = re.match(r"pkg:([^/]+)/(.+?)(?:@([^?#]+))?(?:[?#].*)?$", purl or "")
    if not m:
        return None
    t, path, ver = m.group(1).lower(), urllib.parse.unquote(m.group(2)), urllib.parse.unquote(m.group(3) or "")
    name = path.rsplit("/", 1)[-1] if t == "pypi" else path
    return t, _norm_name(t, name), ver


def sbom_packages(sbom: dict) -> dict:
    """{(type, name, version): {"hashes": {...}}} for every component with a purl (nested ones too)."""
    out = {}

    def walk(comps):
        for c in comps or []:
            k = purl_key(c.get("purl", ""))
            if k:
                out.setdefault(k, {"hashes": {}})["hashes"].update({h.get("alg"): h.get("content") for h in c.get("hashes") or []})
            walk(c.get("components"))
    walk(sbom.get("components"))
    return out


def _components(sbom: dict):
    def walk(comps):
        for c in comps or []:
            yield c
            yield from walk(c.get("components"))
    yield from walk(sbom.get("components"))


def _restored(c: dict) -> bool:
    return any(p.get("name") == "craci:source:restored" and p.get("value") == "true" for p in c.get("properties") or [])


def sbom_summary(sbom: dict, repo: str | None = None) -> dict:
    pk = sbom_packages(sbom)
    meta = sbom.get("metadata") or {}
    props = {p.get("name"): p.get("value") for p in meta.get("properties") or []}
    comps = list(_components(sbom))
    own = sorted({c["purl"].split("@", 1)[1][:12] for c in comps if repo and
                  c.get("purl", "").lower().startswith(f"pkg:github/{repo.lower()}@")})
    out = {"spec": sbom.get("specVersion"), "serial": sbom.get("serialNumber"), "packages": len(pk),
           "by_type": dict(Counter(k[0] for k in pk)),
           # CRACI marks what a job inherited from a restored cache rather than downloaded itself
           "restored_from_cache": sum(1 for c in comps if _restored(c)),
           "observed_in_job": sum(1 for c in comps if not _restored(c)),
           "completeness": next((v for k, v in props.items() if "complete" in (k or "").lower()), None),
           "job": {k.split(":")[-1]: v for k, v in props.items() if k and k.startswith("craci:job:")}}
    if repo:
        out["own_repo_commits"] = len(own)
    return out


def oci_refs(sbom: dict) -> list[dict]:
    """Container images a job pulled, by digest (pkg:oci purls in its CRACI SBOM)."""
    out = []
    for c in _components(sbom):
        m = re.match(r"pkg:oci/([^@]+)@(sha256:[0-9a-f]{64})(?:\?(.*))?$", c.get("purl", ""))
        if m:
            q = urllib.parse.parse_qs(m.group(3) or "")
            out.append({"name": m.group(1), "digest": m.group(2),
                        "repository_url": (q.get("repository_url") or [""])[0]})
    return out


def cache_refs(sbom: dict) -> list[dict]:
    """GitHub Actions caches a job restored or saved (pkg:ghacache purls), with CRACI's entry id and key."""
    out = []
    for c in _components(sbom):
        p = c.get("purl", "")
        if p.startswith("pkg:ghacache/"):
            q = urllib.parse.parse_qs(p.split("?", 1)[1] if "?" in p else "")
            out.append({"entry_id": (q.get("entry_id") or [""])[0], "key": (q.get("key") or [""])[0],
                        "version": p.split("@", 1)[1].split("?", 1)[0] if "@" in p else "",
                        "restored": _restored(c)})
    return out


def diff_sboms(a: dict, b: dict, a_name: str, b_name: str, types=None) -> dict:
    """Packages in one SBOM and not the other, compared on (type, name, version), per ecosystem."""
    pa, pb = sbom_packages(a), sbom_packages(b)
    if types:
        pa = {k: v for k, v in pa.items() if k[0] in types}
        pb = {k: v for k, v in pb.items() if k[0] in types}
    ka, kb = set(pa), set(pb)
    by_name_b = {(t, n): v for t, n, v in kb}
    version_mismatch = sorted(f"{n}: {a_name} {v} vs {b_name} {by_name_b[(t, n)]}"
                              for t, n, v in ka - kb if (t, n) in by_name_b)
    only = lambda s, other_names: sorted(f"pkg:{t}/{n}@{v}" for t, n, v in s if (t, n) not in other_names)  # noqa: E731
    names_a, names_b = {(t, n) for t, n, _ in ka}, {(t, n) for t, n, _ in kb}
    types_all = sorted({k[0] for k in ka | kb})
    return {"compared_on": "purl type + normalised name + version", "types": types_all,
            f"{a_name}_packages": len(ka), f"{b_name}_packages": len(kb), "in_both": len(ka & kb),
            f"only_{a_name}": only(ka - kb, names_b), f"only_{b_name}": only(kb - ka, names_a),
            "version_mismatch": version_mismatch,
            "per_type": {t: {a_name: sum(1 for k in ka if k[0] == t), b_name: sum(1 for k in kb if k[0] == t),
                             "both": sum(1 for k in ka & kb if k[0] == t)} for t in types_all}}


def lock_packages(lock_text: str) -> dict:
    """{(pypi, name, version): {"sha256": {...}, "files": {...}}} (every wheel/sdist uv.lock allows) from uv.lock."""
    out = {}
    for p in tomllib.loads(lock_text).get("package", []):
        if "version" not in p or (p.get("source") or {}).get("editable") or (p.get("source") or {}).get("virtual"):
            continue
        files = list(p.get("wheels") or []) + ([p["sdist"]] if p.get("sdist") else [])
        out[("pypi", _norm_name("pypi", p["name"]), p["version"])] = {
            "sha256": {f["hash"].split(":", 1)[1] for f in files if "hash" in f},
            "files": {urllib.parse.unquote(f["url"].rsplit("/", 1)[-1]) for f in files if "url" in f}}
    return out


def _file_names(sbom: dict) -> dict:
    """{(pypi, name, version): {file_name qualifiers}} from CRACI's purls (pkg:pypi/x@1?file_name=x-1-py3-none-any.whl)."""
    out = defaultdict(set)
    for c in _components(sbom):
        k = purl_key(c.get("purl", ""))
        if k and k[0] == "pypi" and "?" in c["purl"]:
            for f in urllib.parse.parse_qs(c["purl"].split("?", 1)[1]).get("file_name", []):
                out[k].add(f)
    return out


# Why a downloaded PyPI package can legitimately be missing from uv.lock.
NOT_LOCKED_REASONS = {
    "hatchling": "build backend ([build-system] requires, resolved at build time, not locked)",
    "editables": "build backend dependency", "pathspec": "build backend dependency",
    "pluggy": "build backend dependency", "trove-classifiers": "build backend dependency",
    "tomlkit": "build backend dependency", "packaging": "build backend dependency",
    "pip": "tooling (pipx installing uv)", "uv": "tooling (pipx install uv==<pinned>)",
}


def check_against_lock(sbom: dict, lock_text: str) -> dict:
    """Did the job download exactly what uv.lock pins? Version, then the exact wheel file name (CRACI records the
    file name, not a hash, so sha256 can't be compared), and sha256 if CRACI ever records one."""
    lock = lock_packages(lock_text)
    seen = {k: v for k, v in sbom_packages(sbom).items() if k[0] == "pypi"}
    names = _file_names(sbom)
    lock_names = {(t, n): v for t, n, v in lock}
    not_in_lock, wrong_version, file_ok, file_bad, hash_ok, hash_bad = [], [], 0, [], 0, []
    for k, v in seen.items():
        if k in lock:
            for f in names.get(k, ()):
                if f in lock[k]["files"]:
                    file_ok += 1
                else:
                    file_bad.append(f)
            sha = (v["hashes"].get("SHA-256") or v["hashes"].get("sha256") or "").lower()
            if sha and sha in lock[k]["sha256"]:
                hash_ok += 1
            elif sha:
                hash_bad.append(f"{k[1]}=={k[2]}")
        elif (k[0], k[1]) in lock_names:
            wrong_version.append(f"{k[1]}: saw {k[2]}, lock {lock_names[(k[0], k[1])]}")
        else:
            not_in_lock.append({"package": f"{k[1]}=={k[2]}", "why": NOT_LOCKED_REASONS.get(k[1], "unexplained")})
    return {"lock_packages": len(lock), "craci_pypi_packages": len(seen),
            "in_lock": len(seen) - len(not_in_lock) - len(wrong_version),
            "not_in_lock": sorted(not_in_lock, key=lambda x: x["package"]),
            "unexplained_not_in_lock": sorted(x["package"] for x in not_in_lock if x["why"] == "unexplained"),
            "wrong_version": sorted(wrong_version),
            "wheel_file_in_lock": file_ok, "wheel_file_not_in_lock": sorted(file_bad),
            "sha256_match": hash_ok, "sha256_mismatch": sorted(hash_bad),
            "sha256_recorded_by_craci": hash_ok + len(hash_bad)}


# ---------------------------------------------------------------- API clients (+ raw capture / replay)

class Api:
    def __init__(self, repo: str, raw_dir: Path | None, replay: Path | None):
        self.repo, self.raw_dir, self.replay = repo, raw_dir, replay
        self.calls: list[dict] = []

    def _file(self, name: str) -> Path:
        return (self.replay or self.raw_dir) / (re.sub(r"[^A-Za-z0-9_.-]+", "_", name) + ".json")

    def get(self, base: str, path: str, params: dict, name: str, token_env: str, accept="application/json"):
        """(status, body) where body is parsed JSON, or text for non-JSON. Never raises on HTTP errors."""
        if self.replay:
            f = self._file(name)
            if not f.exists():
                return 0, None
            rec = json.loads(f.read_text())
            return rec["status"], rec["body"]
        url = f"{base}{path}?{urllib.parse.urlencode(params, doseq=True)}" if params else f"{base}{path}"
        headers = {"Accept": accept, "User-Agent": "sous-craci-evidence"}
        token = os.environ.get(token_env, "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, TimeoutError) as e:
            status, raw = 0, str(e).encode()
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw.decode(errors="replace")
        self.calls.append({"api": base, "path": path, "params": params, "status": status})
        if self.raw_dir:
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            self._file(name).write_text(json.dumps({"url": url, "status": status, "body": body}))
        return status, body

    def craci(self, path, params, name, accept="application/json"):
        return self.get(CRACI_API, path, params, name, "CRACI_API_TOKEN", accept)

    def github(self, path, params, name):
        return self.get(GITHUB_API, path, params, name, "GITHUB_TOKEN", "application/vnd.github+json")


def problem(status, body) -> str:
    if isinstance(body, dict):
        return f"HTTP {status}: {body.get('title', '')} {body.get('detail', '')}".strip()
    return f"HTTP {status}: {str(body).strip()[:200]}"


# ---------------------------------------------------------------- the bundle

def build(args) -> tuple[dict, int]:
    api = Api(args.repo, Path(args.raw_dir) if args.raw_dir else None, Path(args.replay) if args.replay else None)
    st, run = api.github(f"/repos/{args.repo}/actions/runs/{args.run_id}", {}, "github_run")
    run = run if st == 200 else {}
    head_sha = run.get("head_sha") or os.environ.get("GITHUB_SHA", "")
    attempt = args.run_attempt or run.get("run_attempt") or 1
    st, jobs_body = api.github(f"/repos/{args.repo}/actions/runs/{args.run_id}/attempts/{attempt}/jobs",
                               {"per_page": 100}, "github_jobs")
    jobs = (jobs_body or {}).get("jobs", []) if st == 200 else []

    # The allowlists as declared at the audited commit (a past run is judged by its own ci.yml).
    wf_text = None
    if head_sha and not args.workflow_file:
        r = subprocess.run(["git", "show", f"{head_sha}:.github/workflows/ci.yml"], capture_output=True, text=True,
                           cwd=ROOT)
        wf_text = r.stdout if r.returncode == 0 else None
    wf_source = f"git {head_sha[:12]}:.github/workflows/ci.yml" if wf_text else None
    if wf_text is None:
        wf_path = Path(args.workflow_file or ROOT / ".github/workflows/ci.yml")
        wf_text, wf_source = wf_path.read_text(), str(wf_path.relative_to(ROOT) if wf_path.is_relative_to(ROOT)
                                                      else wf_path)
    policies = declared_policies(wf_text)

    bundle = {"schema": "sous-ci-evidence/1", "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "repository": args.repo, "run_id": str(args.run_id), "run_attempt": attempt, "commit": head_sha,
              "branch": run.get("head_branch"), "event": run.get("event"),
              "run_url": run.get("html_url") or f"https://github.com/{args.repo}/actions/runs/{args.run_id}",
              "policy_source": wf_source, "jobs": [], "sboms": {}, "provenance": {}, "api_gaps": []}
    if not jobs:
        bundle["api_gaps"].append(f"GitHub job list unavailable (HTTP {st})")
    failed = 0
    job_sboms: dict[str, dict] = {}
    for j in sorted(jobs, key=lambda j: j["name"]):
        pol = policies.get(j["name"])
        key = pol["job"] if pol else j["name"]
        entry = {"job": key, "name": j["name"], "github_job_id": str(j["id"]), "conclusion": j.get("conclusion"),
                 "runner": j.get("runner_name"), "declared": None, "egress": None, "sbom": None}
        if key == SELF_JOB:
            entry["note"] = "this job (its trace is not terminal while it runs)"
            bundle["jobs"].append(entry)
            continue
        if j.get("conclusion") == "skipped" or not j.get("runner_name"):
            entry["note"] = "skipped: no runner, no traffic"
            bundle["jobs"].append(entry)
            continue
        if pol:
            entry["declared"] = {"mode": pol["mode"], "presets": pol["presets"] if pol["presets"] is not None
                                 else "all", "tls": pol["tls"], "tcp": pol["tcp"]}
        q = {"provider": "github", "repo": args.repo, "job_id": str(j["id"])}
        st, trace = api.craci("/v1preview1/network-trace", q, f"trace_{key}")
        if st == 200 and isinstance(trace, dict):
            chk = check_egress(pol or {"mode": "observe", "presets": None, "tls": [], "tcp": []}, trace)
            entry["egress"] = chk
            entry["craci_job"] = trace.get("job")
            if chk["verdict"] == "fail":
                failed += 1
            if chk["verdict"] == "observe":
                entry["suggested_network_policy"] = suggest_policy(chk)
        else:
            entry["egress"] = {"verdict": "unavailable", "error": problem(st, trace)}
        st, sbom = api.craci("/v1preview1/sbom", q, f"sbom_{key}", "application/vnd.cyclonedx+json")
        if st == 200 and isinstance(sbom, dict):
            job_sboms[key] = sbom
            entry["sbom"] = sbom_summary(sbom, args.repo)
            entry["sbom"]["sha256"] = hashlib.sha256(json.dumps(sbom, sort_keys=True).encode()).hexdigest()
        else:
            entry["sbom"] = {"error": problem(st, sbom)}
        bundle["jobs"].append(entry)

    # SBOM cross-checks
    lock_text = (ROOT / "uv.lock").read_text()
    if head_sha:
        r = subprocess.run(["git", "show", f"{head_sha}:uv.lock"], capture_output=True, text=True, cwd=ROOT)
        lock_text = r.stdout if r.returncode == 0 else lock_text
    for key in ("test", "eval", "llm-eval", "draft-eval"):
        if key in job_sboms:
            bundle["sboms"][f"{key}_vs_uv_lock"] = check_against_lock(job_sboms[key], lock_text)
    syft = None
    if args.syft_sbom and Path(args.syft_sbom).exists():
        syft = json.loads(Path(args.syft_sbom).read_text())
        bundle["sboms"]["syft_image"] = sbom_summary(syft) | {
            "sha256": hashlib.sha256(Path(args.syft_sbom).read_bytes()).hexdigest()}
        if "docker" in job_sboms:
            bundle["sboms"]["docker_craci_vs_syft"] = diff_sboms(job_sboms["docker"], syft, "craci", "syft")
    else:
        bundle["api_gaps"].append("no syft SBOM of the image (artifact sbom-and-vulns missing)")

    # Provenance: the image, then the caches main's jobs can restore.
    digest = args.image_digest
    if not digest and syft:
        v = ((syft.get("metadata") or {}).get("component") or {}).get("version", "")
        digest = v if v.startswith("sha256:") else None
    prov = bundle["provenance"]
    if digest:
        prov["image"] = {"digest": digest}
        st, body = api.craci("/v1preview1/artifact-provenance/oci_manifest", {"digest": digest}, "prov_image")
        prov["image"]["oci_manifest_provenance"] = body if st == 200 else problem(st, body)
        st, body = api.craci("/v1experimental1/sbom/oci_manifest", {"digest": digest}, "sbom_image",
                             "application/vnd.cyclonedx+json")
        prov["image"]["oci_manifest_sbom"] = sbom_summary(body) if st == 200 and isinstance(body, dict) \
            else problem(st, body)
    # Base images the docker/test/smoke jobs pulled (from CRACI's SBOMs): who does CRACI say created them?
    images = {}
    for key, sb in job_sboms.items():
        for r in oci_refs(sb):
            images.setdefault(r["digest"], r | {"pulled_by": []})["pulled_by"].append(key)
    for i, (dg, r) in enumerate(sorted(images.items())):
        st, body = api.craci("/v1preview1/artifact-provenance/oci_manifest", {"digest": dg}, f"prov_oci_{i}")
        if st == 200 and isinstance(body, dict):
            r["kind"] = body.get("kind")
            r["created_by_craci_jobs"] = len(body.get("jobs_created_in") or [])
            r["refers"] = len(body.get("refers") or [])
        else:
            r["provenance"] = problem(st, body)
    prov["pulled_images"] = list(images.values())

    # Cache lineage: every Actions cache a job's SBOM names, and the CRACI job that created it.
    ledger, seen_ids = [], set()
    for key, sb in job_sboms.items():
        for c in cache_refs(sb):
            if not c["entry_id"] or c["entry_id"] in seen_ids or len(ledger) >= args.max_caches:
                continue
            seen_ids.add(c["entry_id"])
            st2, body = api.craci("/v1preview1/artifact-provenance/gha_cache", {"cache_id": c["entry_id"]},
                                  f"cache_{c['entry_id']}")
            creators = body.get("jobs_created_in", []) if st2 == 200 and isinstance(body, dict) else []
            ledger.append({"job": key, "entry_id": c["entry_id"], "key": c["key"],
                           "family": re.sub(r"-(sha256:)?[0-9a-f]{8,}.*$|#\d+$", "", c["key"]),
                           "craci_creators": [{"github_job_id": str((x.get("provider") or {}).get("job_id")),
                                               "repository": (x.get("provider") or {}).get("repository"),
                                               "accepted_at": x.get("accepted_at"),
                                               "cache_key": (x.get("metadata") or {}).get("cache_key")}
                                              for x in creators],
                           "status": "recorded" if creators else ("no creating job recorded" if st2 in (200, 404)
                                                                  else problem(st2, body))})
    # Which branch and commit wrote each cache? (GitHub's view of the creating job CRACI named.) A cache written
    # by a pull-request or feature-branch job and restored on main is the cache-poisoning path.
    gh_jobs: dict[str, dict] = {}
    for x in ledger:
        for cr in x["craci_creators"]:
            jid = cr["github_job_id"]
            if cr["repository"] == args.repo and jid not in gh_jobs and len(gh_jobs) < 40:
                st3, gj = api.github(f"/repos/{args.repo}/actions/jobs/{jid}", {}, f"github_job_{jid}")
                gh_jobs[jid] = gj if st3 == 200 and isinstance(gj, dict) else {}
            gj = gh_jobs.get(jid) or {}
            cr.update({"branch": gj.get("head_branch"), "commit": (gj.get("head_sha") or "")[:12] or None,
                       "run_id": str(gj["run_id"]) if gj.get("run_id") else None, "job_name": gj.get("name")})
    off_main = sorted({f"{x['key'][:60]} <- {cr['branch']}" for x in ledger for cr in x["craci_creators"]
                       if cr.get("branch") not in (None, bundle.get("branch") or "main")})
    foreign = [x for x in ledger for cr in x["craci_creators"] if cr["repository"] not in (None, args.repo)]
    prov["gha_caches"] = {"checked": len(ledger), "created_on_craci": sum(1 for x in ledger if x["craci_creators"]),
                          "created_on_other_branch": off_main,
                          "creator_commits": sorted({cr["commit"] for x in ledger for cr in x["craci_creators"]
                                                     if cr.get("commit")}),
                          "not_recorded": sum(1 for x in ledger if x["status"] == "no creating job recorded"),
                          "lookup_failed": sum(1 for x in ledger if x["status"].startswith("HTTP")),
                          "created_by_other_repo": len(foreign),
                          "families": dict(Counter(x["family"] for x in ledger)), "entries": ledger}

    missing = [j["job"] for j in bundle["jobs"] if (j.get("egress") or {}).get("verdict") == "unavailable"
               and (j.get("declared") or {}).get("mode", "observe") != "observe"]
    if missing:
        bundle["api_gaps"].append(f"no CRACI network trace for deny-mode job(s) {', '.join(missing)}: not proven")
    bundle["summary"] = {
        # pass needs a trace for every deny-mode job that ran; a missing trace is never read as "clean"
        "egress_verdict": "fail" if failed else ("incomplete" if missing else "pass"),
        "jobs_without_trace": missing,
        "jobs_checked": sum(1 for j in bundle["jobs"] if (j.get("egress") or {}).get("verdict") in ("pass", "fail")),
        "jobs_observe_only": sum(1 for j in bundle["jobs"] if (j.get("egress") or {}).get("verdict") == "observe"),
        "undeclared_hosts": sorted({h for j in bundle["jobs"] for h in (j.get("egress") or {}).get(
            "undeclared_allowed", [])}),
        "hosts_contacted": sorted({f"{d['host']}:{d['port']}" for j in bundle["jobs"]
                                   for d in (j.get("egress") or {}).get("destinations", []) if d["allowed"]}),
    }
    bundle["api_calls"] = [{k: c[k] for k in ("path", "status")} | {"api": c["api"].split("//")[1]}
                           for c in api.calls]
    return bundle, (1 if failed else 0)


# ---------------------------------------------------------------- signing + summary

def sign_bundle(bundle: dict) -> tuple[str, dict, str]:
    """Compact JWS over the bundle with sous.signing; (jws, public JWK, key note)."""
    sys.path.insert(0, str(ROOT))
    from sous import signing  # noqa: E402  (reads SOUS_DATA_DIR for the key)

    note = ("stable key from SOUS_CI_SIGNING_KEY" if os.environ.get("SOUS_CI_SIGNING_KEY_SET") == "1" else
            "ephemeral Ed25519 key generated in this job (CI holds no gateway key); the public JWK is in "
            "evidence.jwks.json and the job summary, so the JWS proves the bundle is unchanged since this job, "
            "not that the production gateway produced it")
    return signing.sign(bundle, "sous-ci-evidence+jws"), signing.jwks(), note


def _kb(n: int) -> str:
    return f"{n / 1024:.0f} KiB" if n < 10 * 1024 * 1024 else f"{n / 1024 / 1024:.1f} MiB"


def markdown(bundle: dict, jwk: dict | None, key_note: str | None, digest: str | None) -> str:
    s = bundle["summary"]
    icon = {"pass": "PASS", "fail": "FAIL", "observe": "observe only", "unavailable": "no trace"}
    out = [f"## CRACI egress proof: {s['egress_verdict'].upper()}", "",
           f"Run [{bundle['run_id']}]({bundle['run_url']}) at `{(bundle['commit'] or '')[:12]}`, allowlists from "
           f"`{bundle['policy_source']}`. Every outbound connection CRACI recorded for each job, checked against the "
           "`network:` that job declares.", "",
           "| Job | Declared | Verdict | Destinations (allowed / blocked) | Undeclared |", "|---|---|---|---|---|"]
    for j in bundle["jobs"]:
        e = j.get("egress") or {}
        d = j.get("declared") or {}
        decl = "-" if not d else (d["mode"] if d["mode"] == "observe" else
                                  f"{d['mode']}; presets {d['presets']}; "
                                  f"{', '.join(h for t in d['tls'] + d['tcp'] for h in t['hosts']) or 'no connections'}")
        dests = e.get("destinations") or []
        hosts = ", ".join(f"{x['host']}:{x['port']}" for x in dests if x["allowed"] and x["declared_by"] !=
                          "github-infra (implicit)") or ("none besides GitHub" if "destinations" in e
                                                          else e.get("error", "-"))
        nblk = sum(1 for x in dests if x["denied"])
        verdict = icon.get(e.get("verdict"), j.get("note", "-"))
        out.append(f"| `{j['job']}` | {decl} | {verdict} | {hosts} / {nblk} | "
                   f"{', '.join(e.get('undeclared_allowed') or []) or '-'} |")
    out += ["", "Bytes sent per non-GitHub destination (the gateway's data leaving the VM):", ""]
    for j in bundle["jobs"]:
        for x in (j.get("egress") or {}).get("destinations", []):
            if x["allowed"] and x["declared_by"] != "github-infra (implicit)" and x["bytes_out"]:
                out.append(f"- `{j['job']}` -> {x['host']}:{x['port']}: {_kb(x['bytes_out'])} out, "
                           f"{_kb(x['bytes_in'])} in ({x['declared_by']})")
    blocked = [(j["job"], x) for j in bundle["jobs"] for x in (j.get("egress") or {}).get("destinations", [])
               if x["denied"]]
    if blocked:
        out += ["", "Blocked by the deny policy (attempts that never left the VM):", ""]
        out += [f"- `{k}` -> {x['host']}:{x['port']} x{x['denied']} ({'; '.join(x['deny_reasons']) or 'denied'})"
                for k, x in blocked]
    for j in bundle["jobs"]:
        if j.get("suggested_network_policy"):
            out += ["", f"<details><summary>`{j['job']}` runs in observe mode: the deny policy its trace implies"
                        "</summary>", "", "```yaml", j["suggested_network_policy"], "```", "</details>"]
    sb = bundle["sboms"]
    if "docker_craci_vs_syft" in sb:
        d = sb["docker_craci_vs_syft"]
        out += ["", "### SBOM: CRACI (docker job, observed downloads) vs syft (final image)", "",
                "| Ecosystem | CRACI | syft | both |", "|---|---|---|---|"]
        out += [f"| {t} | {v['craci']} | {v['syft']} | {v['both']} |" for t, v in d["per_type"].items()]
        out += ["", f"Only in CRACI: {len(d['only_craci'])}; only in syft: {len(d['only_syft'])}; "
                    f"version mismatches: {len(d['version_mismatch'])}."]
        pp = d["per_type"].get("pypi", {})
        out += [f"PyPI: {pp.get('both', 0)} of CRACI's {pp.get('craci', 0)} observed downloads are in syft's image "
                f"SBOM; syft-only PyPI: {', '.join(x for x in d['only_syft'] if x.startswith('pkg:pypi')) or 'none'}."]
    out += ["", "### PyPI downloads (CRACI) vs uv.lock", "",
            "| Job | Downloads | Locked version | Exact wheel file in lock | Not in lock (why) |", "|---|---|---|---|---|"]
    for k, v in sb.items():
        if k.endswith("_vs_uv_lock") and v["craci_pypi_packages"]:
            why = ", ".join(f"{x['package']} ({x['why'].split(' (')[0]})" for x in v["not_in_lock"]) or "-"
            bad = f" ({len(v['wheel_file_not_in_lock'])} not)" if v["wheel_file_not_in_lock"] else ""
            out.append(f"| `{k[:-11]}` | {v['craci_pypi_packages']} | {v['in_lock']} | {v['wheel_file_in_lock']}{bad}"
                       f" | {why} |")
    out += ["", "### CRACI job SBOMs", "", "| Job | Components | Observed in job | Inherited from restored caches "
            "| Commits of this repo listed |", "|---|---|---|---|---|"]
    for j in bundle["jobs"]:
        x = j.get("sbom") or {}
        if "packages" in x:
            out.append(f"| `{j['job']}` | {x['packages']} | {x['observed_in_job']} | {x['restored_from_cache']} | "
                       f"{x.get('own_repo_commits', '-')} |")
    c = bundle["provenance"].get("gha_caches") or {}
    if c:
        fam = ", ".join(f"{k} x{v}" for k, v in sorted(c["families"].items()))
        out += ["", f"Cache lineage: {c['checked']} Actions caches named in the job SBOMs ({fam}): "
                    f"{c['created_on_craci']} created by a CRACI job it can name, {c['not_recorded']} with no creating "
                    f"job recorded, {c['created_by_other_repo']} from another repo, {c['lookup_failed']} lookups failed; "
                    f"written by {len(c['creator_commits'])} commits, "
                    f"{len(c['created_on_other_branch'])} by a job on another branch"
                    f"{': ' + '; '.join(c['created_on_other_branch']) if c['created_on_other_branch'] else ''}."]
    imgs = bundle["provenance"].get("pulled_images") or []
    if imgs:
        out += ["", "Images pulled (by digest, from CRACI's SBOMs): " + "; ".join(
            f"`{i['repository_url'] or i['name']}@{i['digest'][7:19]}` ({', '.join(sorted(set(i['pulled_by'])))}; "
            f"{i.get('provenance') or str(i.get('kind')) + ', created by ' + str(i.get('created_by_craci_jobs')) + ' CRACI jobs'})"
            for i in imgs)]
    img = bundle["provenance"].get("image")
    if img:
        p = img["oci_manifest_provenance"]
        out += ["", f"Built image `{img['digest'][:19]}...` (loaded into the runner, never pushed): CRACI provenance "
                    f"{'recorded' if isinstance(p, dict) else p}"]
    if bundle["api_gaps"]:
        out += ["", "Gaps: " + "; ".join(bundle["api_gaps"])]
    if digest:
        out += ["", f"Bundle sha256 `{digest}`, signed JWS `evidence.jws` (typ `sous-ci-evidence+jws`), "
                    f"kid `{jwk['keys'][0]['kid'] if jwk else '-'}`. {key_note or ''}"]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "Zawaer/sous"))
    ap.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID"))
    ap.add_argument("--run-attempt", type=int, default=None)
    ap.add_argument("--workflow-file", help="judge against this file instead of ci.yml at the run's commit")
    ap.add_argument("--syft-sbom", default="sbom.cdx.json")
    ap.add_argument("--image-digest")
    ap.add_argument("--max-caches", type=int, default=100)
    ap.add_argument("--out", default="craci-evidence")
    ap.add_argument("--raw-dir", help="save every raw API response here")
    ap.add_argument("--replay", help="read raw API responses from this directory instead of calling the APIs")
    ap.add_argument("--no-sign", action="store_true")
    ap.add_argument("--no-fail", action="store_true", help="report undeclared egress without failing")
    args = ap.parse_args()
    if not args.run_id:
        ap.error("--run-id (or GITHUB_RUN_ID) is required")
    if not args.replay and not os.environ.get("CRACI_API_TOKEN"):
        print("::warning::CRACI_API_TOKEN not set: CRACI data will be missing")
    bundle, code = build(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(bundle, indent=2, sort_keys=True, default=str)
    (out / "evidence.json").write_text(blob + "\n")
    digest = hashlib.sha256(blob.encode()).hexdigest()
    jwk = note = None
    if not args.no_sign:
        jws, jwk, note = sign_bundle(bundle)
        (out / "evidence.jws").write_text(jws + "\n")
        (out / "evidence.jwks.json").write_text(json.dumps(jwk, indent=2) + "\n")
    md = markdown(bundle, jwk, note, digest)
    (out / "summary.md").write_text(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md)
    print(md)
    if bundle["summary"]["egress_verdict"] == "incomplete":
        print("::warning::CRACI evidence incomplete: " + "; ".join(bundle["api_gaps"]))
    for h in bundle["summary"]["undeclared_hosts"]:
        print(f"::error::A deny-mode job reached {h}, which its declared network policy does not allow")
    return 0 if args.no_fail else code


if __name__ == "__main__":
    raise SystemExit(main())
