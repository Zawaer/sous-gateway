# Best use of CRACI

Sous is a purchasing gateway that holds a supplier API key (Valio Aimo) and signs orders. The one property that matters most is "it talks to nothing it shouldn't". So every CI job runs on CRACI (`CI_RUNNER=craci`), and each job's network policy is the allowlist the product claims it needs.

**Compute, sized per job** (`.github/workflows/ci.yml`, `cracicorp/setup` step per job)
- `test` (8 vCPU): 200+ pytest tests against a MariaDB 11.4 service.
- `docker` (8 vCPU): buildx image build, syft SBOM, grype scan.
- `smoke`: starts the real `compose.yaml` (MariaDB + gateway) on a fixture catalog; checks REST, MCP, JWKS.
- `eval` / `llm-eval` (8 vCPU): 45 scripted and 40 LLM buyer requests against the live Valio API, Gemini, Verda Inference and NVIDIA (Nemotron).
- CRACI's "VM never reported liveness" showed the eval jobs ran out of memory on the default size; now size 8.

**Network tracing as a product test**
- `smoke` runs under `default: deny` with **no allowed destinations at all**: the images come from the `docker` job as an artifact, so the gateway boots, signs and serves MCP with zero egress.
- `eval`: deny, except PyPI, `junction26.valioaimo.fi:443` and Aiven Valkey. `llm-eval`: the same plus `generativelanguage.googleapis.com`, `inference.verda.com` and `integrate.api.nvidia.com`. Anything else is a proxy violation: a blocked exfiltration, not a silent leak.
- The trace found a real bug in our config: Aiven Valkey connected over DNS and TCP but the TLS handshake got EOF, because CRACI's proxy classifies it as TLS and our rule was TCP-only. The fix is a TLS rule with the exact port.
- `test` and `docker` run in observe mode; their traces are the allowlist for moving them to deny next.

**SBOM and vulnerability management** (run 43528096128)
- syft CycloneDX SBOM of the final image: **694 packages** (94 PyPI, 87 Debian, 513 Rust crates embedded in the `uv` binary).
- grype: **0 critical**, 61 high (2 with a fix), 73 medium (12 with a fix). The gate fails only on *critical with a fix*, so CI stays actionable instead of red on unfixable base-image CVEs. Report in the `sbom-and-vulns` artifact.

[PENDING: link a green CRACI run, plus screenshots of CRACI's own SBOM and vulnerability view next to ours and the smoke job's empty trace. PENDING: the Valkey TLS rule push and a run showing `backend=valkey`.]

Code: https://github.com/Zawaer/sous · [docs/CI.md](../CI.md) · [ci.yml](../../.github/workflows/ci.yml)
