# Best use of CRACI

Sous holds Valio's API key and signs orders, so its security claim is "it talks to nothing it shouldn't". All CI runs on CRACI; every job declares deny-by-default egress, and a `craci-evidence` job **proves** it via the CRACI API, signed ([docs/CRACI.md](../CRACI.md)).

**Technical depth**
- Each job's CRACI network trace is checked against its `network:` block parsed from ci.yml at the audited commit, with our own preset table. Undeclared host: fail; no trace: `incomplete`. Run 43611583279: every deny-mode job PASS (eval: only Valio, Valkey, PyPI).
- SBOMs: CRACI vs our syft image SBOM (49/49 PyPI), vs uv.lock exact wheel files (found unpinned `hatchling` build deps), cache lineage via the provenance API (21/21 caches CRACI-made, 0 off-main).
- Signed bundle (Ed25519 JWS, gateway's own code), `--verify`.

**Creative use of compute**: `redteam-scale` asks for a **32-vCPU** NVMe runner: all **4,000 red-team requests in 15.4 s** (one gateway process per vCPU, Valio replayed, no keys): 2,036/2,036 unsafe held, 0 gateway violations. It scales linearly (2.4k/5.1k/9.6k/15.5k req/min at 4/8/16/32 CPUs), while one pinned gateway process stays flat at 24-29 req/s from 4 to 30 CPUs: the GIL ceiling in [CAPACITY.md](../CAPACITY.md), measured in a 6-min job.

**Network tracing**: the trace of that job showed CRACI's `dockerhub` preset misses `production.cloudfront.docker.com` (31 blocked fetches broke the MariaDB pull); now declared, and the run's evidence shows only PyPI + Docker Hub. Earlier it caught runner probes allowed with no matched rule in a deny job.

**Vulnerability management**: CRACI's API has no findings/AI-fix route (probed each run: 404, recorded in the bundle). So grype scans **CRACI's own job SBOMs** (201 packages and Actions CI actually downloaded), each finding mapped back to its jobs, cross-checked with the image scan, and the run fails on any Critical with a fix. Run 43611583279: CI downloads 0 findings; image 218 (0 Critical); the 6 PyPI ones are base-image `pip` no job downloaded, a gap only the cross-check shows. Signed in the bundle.

**Honest limits**: `api.craci.com` is 403 from CRACI VMs, so the auditor runs on GitHub-hosted; no SBOM hashes or completeness verdict in the API; CRACI's vulnerability view and AI fixes are web-only (screenshots in the write-up).

Code: [craci_evidence.py](../../scripts/craci_evidence.py) · [craci_scale.py](../../scripts/craci_scale.py) · [ci.yml](../../.github/workflows/ci.yml)
