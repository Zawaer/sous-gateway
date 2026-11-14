# Best use of CRACI

Sous is a purchasing gateway that holds Valio's API key and signs orders, so the property that matters is "it talks to nothing it shouldn't". All build, test and eval jobs run on CRACI, and each declares its egress allowlist. A `craci-evidence` job then **proves** the allowlists held, using the CRACI API ([docs/CRACI.md](../CRACI.md)).

**Technical depth**
- For every job in the run it fetches the CRACI network trace and parses that job's `network:` block from ci.yml at the audited commit. It matches each HTTP/TLS/TCP access against the declaration with our own preset table, not CRACI's verdict. Any undeclared host in a deny-mode job fails the run; a missing trace gives `incomplete`, never `pass`.
- Run [43580565490](https://github.com/Zawaer/sous/actions/runs/43580565490): PASS. `eval` reached only Valio (18 KiB out), Aiven Valkey and PyPI. `smoke` (gateway + MariaDB + MCP) had no egress. Two runner probes were blocked. Audit of the live LLM run 43557209740: Gemini 4.2 MiB and Verda 4.0 MiB out, all declared.
- SBOMs: 49/49 PyPI packages CRACI saw downloaded are in our syft image SBOM; 49/49 match an exact wheel file in uv.lock. Found: `hatchling` + 4 deps (the build backend) come in unpinned, and 137 of the docker job's 155 SBOM components (89 old commits) are inherited from buildkit caches.
- The bundle is signed with the gateway's own Ed25519 JWS code (`typ sous-ci-evidence+jws`, ephemeral key) and checked with `--verify`.

**Creative use of CRACI features**
- Network traces as a **product test**: the CI allowlist is the gateway's security claim, and the trace is its evidence, signed for a buyer.
- **Cache lineage** for cache poisoning: 21/21 restored caches were written by CRACI jobs CRACI names, and GitHub gives their branch (0 off-main).
- **Policy from trace**: for jobs still in observe mode, the job writes the `default: deny` block their trace implies.
- Pulled image digests are checked with the OCI provenance API.

**Honest limits** (details in CRACI.md): `api.craci.com` answers 403 from inside CRACI VMs even with `default: allow`, so the auditor runs on a GitHub-hosted runner, outside the platform it audits. The API has no SBOM hashes, no completeness verdict and no AI-fix endpoint. OCI provenance covers only pushed images (ours is loaded), and `test`/`docker` are not yet deny-mode.

Code: [scripts/craci_evidence.py](../../scripts/craci_evidence.py) · [ci.yml](../../.github/workflows/ci.yml) · [docs/CI.md](../CI.md)
