# CRACI evidence: egress proof for a trust gateway

Sous holds a supplier API key and signs orders for AI buyer agents. The question a buyer (or Valio) should ask
is "what did this thing talk to?". Every CI job on [CRACI](https://docs.craci.com) declares an egress allowlist
in [ci.yml](../.github/workflows/ci.yml). The `craci-evidence` job turns those declarations into a signed check
anyone can rerun. It uses the CRACI API, which records what each job actually did.

Code: [scripts/craci_evidence.py](../scripts/craci_evidence.py), tests:
[tests/test_craci_evidence.py](../tests/test_craci_evidence.py), job: `craci-evidence` in ci.yml.

## What it checks

For one workflow run (this run by default, or any past run with `evidence_run_id`):

1. **Egress vs declaration.** The job parses each job's `cracicorp/setup` `network:` block itself. It reads
   ci.yml **at the audited commit**, so a past run is judged by its own rules. It fetches
   `GET /v1preview1/network-trace` per job and matches every HTTP, TLS, TCP and SSH-git access against that
   declaration: the declared `tls`/`tcp` connections, the hosts behind the declared presets (our own table, not
   CRACI's verdict echoed back), and a narrow list of GitHub runner hosts that CRACI always allows. If a
   deny-mode job reached a host its declaration doesn't explain, the run **fails**. If a deny-mode job has no
   trace, the verdict is `incomplete`, never `pass`. Blocked attempts are listed, along with the bytes sent to
   every non-GitHub destination.
2. **The deny policy an observe job implies.** `test` and `docker` still run without a policy. For them the
   job writes the `default: deny` block their trace implies. Destinations CRACI matched to no rule are exactly
   the ones that would need an explicit connection.
3. **SBOM cross-checks.** `GET /v1preview1/sbom` per job:
   - CRACI's docker-job SBOM vs our syft SBOM of the final image.
   - Every PyPI download vs `uv.lock`: the locked version, and the **exact wheel file name** (CRACI's purl
     carries `file_name`; uv.lock lists every allowed file).
   - What each job SBOM inherited from restored caches (`craci:source:restored`).
4. **Cache lineage.** For every Actions cache named in a job SBOM (`pkg:ghacache/...?entry_id=`),
   `GET /v1preview1/artifact-provenance/gha_cache` returns the CRACI job that wrote it. GitHub then gives that
   job's branch and commit. A cache written on another branch and restored on main is the cache-poisoning path,
   so the job counts those.
5. **Pulled images.** The job takes every `pkg:oci/...@sha256` that a job pulled and calls
   `GET /v1preview1/artifact-provenance/oci_manifest` on it.
6. **Signed bundle.** It writes `evidence.json`, signs it with Sous's own `sous/signing.py` (Ed25519 compact
   JWS, `typ: sous-ci-evidence+jws`), and adds `evidence.jwks.json`, `summary.md` (also the job summary) and
   `raw/` (every API response, for `--replay`). The artifact name is `craci-evidence`.

## Real results

**Run 43580565490** (push of c0636bf, all jobs
green): egress **PASS**. All 4 deny-mode jobs that ran (`eval`, `llm-eval`, `draft-eval`, `smoke`) stayed
inside their declarations.
- `eval` reached `junction26.valioaimo.fi` (18 KiB out, 36 KiB in), Aiven Valkey on port 23770 (119 KiB out)
  and PyPI. Nothing else left the VM.
- `smoke` reached only GitHub (checkout). The gateway, MariaDB and MCP produced no egress.
- CRACI blocked 2 attempts in `draft-eval`: `GET https://copilot-proxy.githubusercontent.com/_dns` and
  `pkg.github.com/_dns`. These are runner probes, not our code. The same probes **went out** from the
  observe-mode `docker` job.

**The check caught something in run 43582172396**
(86b1b39). The evidence job failed: `smoke` runs under `default: deny` with no connections, yet CRACI
**allowed** `GET https://pkg.github.com/_dns` and `GET https://copilot-proxy.githubusercontent.com/_dns`. It
recorded no `matchedRule` for either (`policyDenied: false`, HTTP 200). Under `presets: [pypi]`, CRACI blocks
the same two requests (`draft-eval`). They are the runner's connectivity probes: nothing sent, 2 to 5 bytes
back. So the evidence job now accepts exactly that shape (GET, path `/_dns`, those two hosts, at most 64
bytes back, nothing sent) and labels it `runner probe`. Any other request to those hosts still fails the run
(test `test_runner_probe_is_accepted_only_in_its_exact_shape`). Re-audit of that run: PASS (43584176961); next full run green: 43584327314 (998b7c8, `eval` -> Valio 30 KiB out, signature verified). The open question for CRACI: which rule let
them through in a deny-mode job, and why does the trace name none?

**43580577498** (`draft_only`): `draft-eval` sent
137 KiB to `generativelanguage.googleapis.com` and nothing else. **PASS**.

**Audit of the live LLM run 43557209740** (evidence run
43580597144): `llm-eval` sent 4.2 MiB to Gemini,
4.0 MiB to Verda, 112 KiB to Valio and 584 KiB to Valkey. All are declared. **PASS**.

SBOMs (run 43580565490):
- PyPI: **all 49 of 49** packages CRACI saw the docker job download are in syft's image SBOM at the same
  version. syft additionally lists `pip` (base image) and `sous` itself. syft's 513 cargo crates (inside the
  `uv` binary) and 87 Debian packages arrive inside image layers. CRACI records those as 8 pulled image
  digests (`python`, `uv`, `mariadb`, `buildkit`), not as packages.
- `test` and `eval`: 49 of 49 locked PyPI downloads match an **exact wheel file in uv.lock**. Not in the lock:
  `hatchling` and 4 of its dependencies, because the `[build-system]` backend is resolved unpinned at build
  time. That is a real supply-chain gap the lockfile doesn't cover. In `eval` there is also `pip`/`uv` from
  `pipx install uv==0.13.6`.
- The docker job's CRACI SBOM has 155 components. **137 of them are inherited from restored caches**, including
  89 earlier commits of this repo carried forward through the buildkit layer cache. That is CRACI's transitive
  cache SBOM working as documented, and a reason to prune the cache now and then.
- Cache lineage: 21 caches (17 buildkit blobs, 2 buildx indexes, setup-uv, valio-data). **All 21 were written
  by a CRACI job CRACI can name**, from 10 commits, **0 from another branch**.

Signature check (both bundles above): `uv run python scripts/craci_evidence.py --verify <artifact dir>` prints
`signature valid ...; evidence.json matches the signed payload`.

## Vulnerability management on CRACI's own SBOMs

CRACI's API has no vulnerability, advisory, finding or AI-fix route. The public
[openapi.json](https://api.craci.com/openapi.json) (14.11) lists only `sbom`, `sbom/oci_manifest`,
`network-trace`, `artifact-provenance/*`, `job-logs`, `jobs` and `usage`. Unauthenticated, the real routes answer 401,
while `/v1preview1/{vulnerabilities,findings,advisories,vulnerability-report,fixes,ai-fix,vex,remediations}` and
`/v1experimental1/{vulnerabilities,findings,advisories,vex}` answer 404. docs.craci.com and its agent skill
(`skills/craci/SKILL.md`) don't mention vulnerability data either. The evidence job probes 5 of those routes with
our token **on every run** and records the status codes in the bundle. If one starts answering, the summary says so.
So CRACI's vulnerability view is web-only for now. **For the write-up: take screenshots of CRACI's vulnerability
view and any AI-fix suggestion for run 43611583279.**

What the job does with what the API does give:
- It merges every PyPI package and GitHub Action that CRACI's job SBOMs say a job downloaded (or restored from a
  cache) into one CycloneDX file. grype (`anchore/scan-action/download-grype`, the same action and version as the
  docker job) scans it. Each finding is attributed back to the jobs that downloaded the package.
- It cross-checks that scan with the docker job's grype scan of the image (from syft's SBOM). Each finding is put
  in one of three groups: in both scans; only in what CI jobs downloaded (build tools, test dependencies, Actions:
  CI exposure that an image scan never sees); or a PyPI finding in the image on a package that no job downloaded
  (it came inside a base-image layer).
- **Gate:** the run fails on any Critical finding with a fix available, in either scan. The finding counts, the
  three groups, the blocking list and the probe results all go into `evidence.json`, so they are signed with the
  egress verdict (`summary.vuln_verdict`), and into the job summary.

**Run 43611583279** (push of 84e7826, everything green):
- CRACI SBOMs: 201 PyPI packages and Actions refs from 7 jobs, **0 findings**.
- Image: 218 findings, 0 Critical. 61 High with 2 fixable, 73 Medium with 12 fixable; nearly all are Debian packages
  in the base image.
- The 6 PyPI findings in the image are all `pip 25.0.1`, which ships inside the `python` base image. No job
  downloaded it, so CRACI records only the image digest for it. That is the gap between a download SBOM and an
  image SBOM, made visible.
- Vulnerability verdict: **PASS**; egress: **PASS**; signature verified.

## Creative use of compute: the 32-vCPU red team

`redteam-scale` (manual, `-f redteam_scale=true`) asks CRACI for `size: "32"` and runs, offline, with no Valio
or model key:
- the **full 4,000-request scripted red team**, one gateway process per vCPU;
- the red team pinned to 4/8/16 CPUs;
- the one-process load test with gateway and MariaDB pinned to 4/8/16/30 CPUs (`scripts/craci_scale.py`).

**Run 43611589764** took 6 minutes for the whole job:
- All 4,000 red-team requests ran in **15.4 s**: 2,036/2,036 unsafe held, 0 gateway violations.
- The red team scales linearly: 2.4k, 5.1k, 9.6k and 15.5k requests/min at 4, 8, 16 and 32 CPUs.
- One gateway process stays flat at 24-29 req/s from 4 to 30 CPUs, which is the GIL bottleneck in docs/CAPACITY.md.
- Its `craci-evidence` job proves the job reached only PyPI and Docker Hub.

The first attempt found a gap in CRACI's `dockerhub` preset (run 43611050520). Docker Hub now serves blobs from
`production.cloudfront.docker.com`. The preset doesn't cover that host, so CRACI blocked 31 fetches and the MariaDB
service pull failed with "denied: Forbidden". The trace named the host, and the job now declares it explicitly
(100.9 MiB in). If the snapshot or the cached catalog is missing, the job skips with a notice; the public copy has
neither.

## Why it runs on a GitHub-hosted runner

From inside a CRACI VM, `api.craci.com` answers `403 Access denied`. That happened with it in the TLS
allowlist (run 43579559941) and even with `default: allow`, while `example.com` was reachable (run
43579657052). From outside CRACI the same token works. So the auditor runs on `ubuntu-24.04`, outside the
platform it audits; all audited jobs run on CRACI. Setting the repo variable `CRACI_EVIDENCE_RUNNER=craci`
moves the job back. Its deny policy (PyPI + `api.craci.com` + `api.github.com`) is already written.

## Signing key, honestly

CI holds no gateway key, so `sous.signing` creates an **ephemeral** Ed25519 key in the job and the public JWK
ships next to the JWS. The signature proves the bundle hasn't changed since that job. It does not prove that
the production gateway made it, and it is not verifiable against the gateway's `/.well-known/jwks.json`.
Binding it would take a CI signing key that the gateway publishes in its JWKS. That needs a new secret, which
we haven't added.

## For a buyer

`/health` returns the running build's `commit`. Find the CI run for that commit, download its `craci-evidence`
artifact, run `--verify`, and read `summary.egress_verdict` plus the per-job hosts. The bundle is created after
the image is built, so the image can't contain its own evidence. We did not add a
`/.well-known/sous-build-evidence.json`, because it would always describe an earlier build.

## What the CRACI API lacks (as of 14.11)

- No access to `api.craci.com` from CRACI runners (see above).
- SBOM components carry no hashes. We compare wheel file names; with hashes we could match the sha256 values
  that uv.lock already pins.
- No SBOM completeness verdict in the API ("Complete", "Complete with connections", ...). The web UI shows it;
  `metadata.properties` has only `craci:job:*`.
- Image provenance covers artifacts a CRACI job **pushed**. Pulled base images return 404, and our image is
  `load`ed, not pushed, so the built image has no CRACI provenance.
- No vulnerability, advisory, finding, VEX or AI-fix endpoint (see "Vulnerability management" above). To
  cross-check CRACI's own vulnerability results and AI fixes against grype, we would need at least
  `GET /v1preview1/vulnerabilities?provider=github&repo=&job_id=` (findings per job: id, purl, severity, fix
  state and versions) and the AI-fix text or patch per finding.
- The `dockerhub` preset misses `production.cloudfront.docker.com`, where Docker Hub now serves blobs (run
  43611050520).
- A job's own trace is unavailable while it runs (409), so the evidence job can't audit itself.
- Allowed traffic without a `matchedRule` under `default: deny` (the `/_dns` probes in `smoke`, run 43582172396): the trace says "allowed" but not why.
