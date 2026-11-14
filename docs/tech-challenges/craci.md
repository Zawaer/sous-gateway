# Best use of CRACI

Sous is a purchasing gateway that holds a supplier API key (Valio Aimo) and signs orders. The property that matters most is "it talks to nothing it shouldn't". So every CI job runs on CRACI (`CI_RUNNER=craci`), and each job's network policy is the allowlist the product claims it needs.

Green run with every job: [43530484114](https://github.com/Zawaer/sous/actions/runs/43530484114) (e84bb41): test, docker, smoke, eval (45/45), llm-eval. Each job log links its CRACI job page.

**Network traces as a product test**
- `smoke` starts the real `compose.yaml` (MariaDB + gateway) under `default: deny` with **no allowed destinations**: images come from the `docker` job's artifact, so the gateway boots, signs and serves MCP with zero egress.
- `eval`: deny, except PyPI, `junction26.valioaimo.fi:443` and Aiven Valkey (TLS, one port). `llm-eval`: the same plus `generativelanguage.googleapis.com`, `inference.verda.com` and `integrate.api.nvidia.com`. Anything else is a proxy violation.
- The trace found a real bug in our config: Valkey connected over TCP but the TLS handshake got EOF (run 43524836008) because our rule was TCP-only and CRACI's proxy classifies it as TLS. With a TLS rule the eval jobs share the Valio budget through Valkey (run 43530484114: `backend: valkey`).
- `test` and `docker` still run in observe mode; their traces are the allowlist for moving them to deny. Not done yet.

**SBOM and vulnerability management** (artifact `sbom-and-vulns`, run 43530484114)
- syft CycloneDX SBOM of the final image: **694 packages** (94 PyPI, 87 Debian, 513 Rust crates embedded in the `uv` binary).
- grype: **0 critical**, 61 high (2 with a fix), 73 medium (12 with a fix). The gate fails only on *critical with a fix*, so CI stays actionable. Not done: fixing the 2 fixable highs, comparing against CRACI's own SBOM view.

**Compute sized per job.** `test`, `docker`, `eval`, `llm-eval` at 8 vCPU: CRACI's "VM never reported liveness" showed the eval jobs ran out of memory loading the 87 MB catalog on the default size.

**AI fixes, provenance, API.** Not used: no CRACI AI fix suggestions, no build provenance/attestation, no CRACI API calls.

**Honest state of `main`.** Several pushes after 43530484114 failed the test job (a test needing `data/`, fixed in e1328eb) or were cancelled by newer pushes.

Code: https://github.com/Zawaer/sous · [docs/CI.md](../CI.md) · [ci.yml](../../.github/workflows/ci.yml)
