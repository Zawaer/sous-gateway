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
GITHUB_INFRA = ["github.com", "api.github.com", "*.github.com", "*.actions.githubusercontent.com",
                "*.githubusercontent.com", "*.blob.core.windows.net", "*.githubapp.com",
                "token.actions.githubusercontent.com"]


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
    """The narrowest `network:` block that would have allowed what an observe-mode job did."""
    presets, tls = set(), defaultdict(set)
    for d in check["destinations"]:
        if not d["allowed"]:
            continue
        for p, hosts in PRESET_HOSTS.items():
            if _host_match(d["host"], hosts):
                presets.add(p)
                break
        else:
            if not _host_match(d["host"], GITHUB_INFRA):
                tls[d["port"]].add(d["host"])
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
                out[k] = {"hashes": {h.get("alg"): h.get("content") for h in c.get("hashes") or []}}
            walk(c.get("components"))
    walk(sbom.get("components"))
    return out


def sbom_summary(sbom: dict) -> dict:
    pk = sbom_packages(sbom)
    meta = sbom.get("metadata") or {}
    props = {p.get("name"): p.get("value") for p in meta.get("properties") or []}
    comp = [c for c in (sbom.get("compositions") or [])]
    return {"spec": sbom.get("specVersion"), "serial": sbom.get("serialNumber"), "packages": len(pk),
            "by_type": dict(Counter(k[0] for k in pk)),
            "completeness": {k: v for k, v in props.items() if "complete" in (k or "").lower()} or None,
            "compositions": [c.get("aggregate") for c in comp] or None,
            "properties": props}


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
    """{(pypi, name, version): {"sha256": {...all wheel/sdist hashes}}} from uv.lock."""
    out = {}
    for p in tomllib.loads(lock_text).get("package", []):
        if "version" not in p or (p.get("source") or {}).get("editable") or (p.get("source") or {}).get("virtual"):
            continue
        hashes = {f["hash"].split(":", 1)[1] for f in (p.get("wheels") or []) if "hash" in f}
        if p.get("sdist", {}).get("hash"):
            hashes.add(p["sdist"]["hash"].split(":", 1)[1])
        out[("pypi", _norm_name("pypi", p["name"]), p["version"])] = hashes
    return out


def check_against_lock(sbom: dict, lock_text: str) -> dict:
    """Did the job download exactly what uv.lock pins? Versions, and sha256 where CRACI recorded one."""
    lock = lock_packages(lock_text)
    seen = {k: v for k, v in sbom_packages(sbom).items() if k[0] == "pypi"}
    lock_names = {(t, n): v for t, n, v in lock}
    not_in_lock, wrong_version, hash_ok, hash_bad, no_hash = [], [], 0, [], 0
    for k, v in seen.items():
        if k in lock:
            sha = (v["hashes"].get("SHA-256") or v["hashes"].get("sha256") or "").lower()
            if not sha:
                no_hash += 1
            elif sha in lock[k]:
                hash_ok += 1
            else:
                hash_bad.append(f"{k[1]}=={k[2]}")
        elif (k[0], k[1]) in lock_names:
            wrong_version.append(f"{k[1]}: saw {k[2]}, lock {lock_names[(k[0], k[1])]}")
        else:
            not_in_lock.append(f"{k[1]}=={k[2]}")
    return {"lock_packages": len(lock), "craci_pypi_packages": len(seen),
            "in_lock": len(seen) - len(not_in_lock) - len(wrong_version),
            "not_in_lock": sorted(not_in_lock), "wrong_version": sorted(wrong_version),
            "sha256_match": hash_ok, "sha256_mismatch": sorted(hash_bad), "no_hash_recorded": no_hash}


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
    return f"HTTP {status}: {str(body)[:200]}"


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
            entry["sbom"] = sbom_summary(sbom)
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
    st, caches = api.github(f"/repos/{args.repo}/actions/caches", {"per_page": 100, "ref": "refs/heads/main",
                                                                     "sort": "created_at"}, "github_caches")
    ledger = []
    for c in ((caches or {}).get("actions_caches") or [])[: args.max_caches] if st == 200 else []:
        st2, body = api.craci("/v1preview1/artifact-provenance/gha_cache", {"cache_id": c["id"]}, f"cache_{c['id']}")
        creators = body.get("jobs_created_in", []) if st2 == 200 and isinstance(body, dict) else []
        ledger.append({"cache_id": c["id"], "key": c["key"], "created_at": c.get("created_at"),
                       "size_bytes": c.get("size_in_bytes"),
                       "family": re.sub(r"[-#](sha256:)?[0-9a-f]{8,}.*$|#\d+$", "", c["key"]),
                       "craci_creators": [{"github_job_id": str((x.get("provider") or {}).get("job_id")),
                                           "repository": (x.get("provider") or {}).get("repository"),
                                           "accepted_at": x.get("accepted_at")} for x in creators],
                       "status": "recorded" if creators else ("not recorded by CRACI" if st2 in (200, 404)
                                                              else problem(st2, body))})
    if st != 200:
        bundle["api_gaps"].append(f"GitHub cache list unavailable (HTTP {st})")
    foreign = [x for x in ledger for cr in x["craci_creators"] if cr["repository"] not in (None, args.repo)]
    prov["gha_caches"] = {"checked": len(ledger), "created_on_craci": sum(1 for x in ledger if x["craci_creators"]),
                          "not_recorded": sum(1 for x in ledger if x["status"] == "not recorded by CRACI"),
                          "lookup_failed": sum(1 for x in ledger if x["status"].startswith("HTTP")),
                          "created_by_other_repo": len(foreign), "entries": ledger}

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
    sb = bundle["sboms"]
    if "docker_craci_vs_syft" in sb:
        d = sb["docker_craci_vs_syft"]
        out += ["", "### SBOM: CRACI (docker job, observed downloads) vs syft (final image)", "",
                "| Ecosystem | CRACI | syft | both |", "|---|---|---|---|"]
        out += [f"| {t} | {v['craci']} | {v['syft']} | {v['both']} |" for t, v in d["per_type"].items()]
        out += ["", f"Only in CRACI: {len(d['only_craci'])}; only in syft: {len(d['only_syft'])}; "
                    f"version mismatches: {len(d['version_mismatch'])}."]
    for k, v in sb.items():
        if k.endswith("_vs_uv_lock"):
            out.append(f"- `{k[:-11]}` PyPI downloads vs uv.lock: {v['in_lock']}/{v['craci_pypi_packages']} pinned, "
                       f"{len(v['not_in_lock'])} not in lock, {len(v['wrong_version'])} wrong version, sha256 "
                       f"{v['sha256_match']} match / {len(v['sha256_mismatch'])} mismatch / "
                       f"{v['no_hash_recorded']} not recorded")
    comp = [(j["job"], (j.get("sbom") or {}).get("completeness")) for j in bundle["jobs"] if j.get("sbom")]
    if comp:
        out += ["", "CRACI SBOM completeness: " + "; ".join(f"`{k}` {v}" for k, v in comp)]
    c = bundle["provenance"].get("gha_caches") or {}
    if c:
        out += ["", f"### Cache provenance (main): {c['checked']} caches, {c['created_on_craci']} created by a "
                    f"recorded CRACI job, {c['not_recorded']} not recorded, {c['created_by_other_repo']} from another "
                    f"repo, {c['lookup_failed']} lookups failed"]
    img = bundle["provenance"].get("image")
    if img:
        p = img["oci_manifest_provenance"]
        out += ["", f"Image `{img['digest'][:19]}...`: CRACI provenance "
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
