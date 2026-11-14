# CI

One workflow, [`.github/workflows/ci.yml`](../.github/workflows/ci.yml), runs on every push to
`main`, every pull request and on demand (`workflow_dispatch`). It runs on [CRACI](https://docs.craci.com) runners: the repo variable `CI_RUNNER=craci` is set (without it, GitHub-hosted `ubuntu-24.04`). Last run with every job green, including the live evals: [43530484114](https://github.com/Zawaer/sous/actions/runs/43530484114) (e84bb41).

```
test ──────────► eval (main / manual only; needs VALIO_AIMO_API_KEY + scripts/eval.py)
     └─────────► llm-eval (manual, or main pushes that change agent/; needs VALIO_AIMO_API_KEY + GEMINI_API_KEY and/or VERDA_INFERENCE_API_KEY)
docker ────────► smoke
draft-eval (manual with draft_only, or main pushes that change agent/draft.py / its cases; Gemini optional)
```

| Job | What it does | CRACI size | CRACI egress policy |
|---|---|---|---|
| `test` | `uv sync --frozen` + `pytest` against a MariaDB 11.4 service (system-versioned tables). uv cache keyed on `uv.lock`. | 8 | default (observe: all presets, unmatched allowed) |
| `docker` | Builds the [Dockerfile](../Dockerfile) with buildx (layer cache in the Actions cache), writes a CycloneDX SBOM with syft, scans it with grype, fails only on **critical vulnerabilities that have a fix**. Uploads `sbom-and-vulns` (SBOM, grype JSON, TSV) and the image. Severity table in the job summary. | 8 | default (observe) |
| `smoke` | Loads that image and starts the real [compose.yaml](../compose.yaml) (MariaDB + gateway) with the override [tests/fixtures/compose.smoke.yaml](../tests/fixtures/compose.smoke.yaml): a 20-product fixture catalog instead of Valio's data, no API key. [tests/fixtures/smoke.sh](../tests/fixtures/smoke.sh) checks `/health` (storage = mariadb), `/docs`, `/openapi.json`, `/.well-known/jwks.json` (Ed25519 key), a catalog search, and MCP `initialize` + `tools/list` at `/mcp/`. | 2 (default) | **deny**, no exceptions (the MariaDB image comes from the docker job's artifact) |
| `eval` | Runs `scripts/eval.py` (the scripted buyer requests in `evals/cases.yaml`) against the live Valio Aimo API and uploads `evals/results.json` as `eval-results`. Skips itself (with a notice) when the `VALIO_AIMO_API_KEY` secret or `scripts/eval.py` is missing. Never runs on pull requests. Valio data files are cached between runs. | 8 | **deny**, only `pypi` + `junction26.valioaimo.fi:443` + Aiven Valkey (`*.aivencloud.com:23770`, TLS) |
| `llm-eval` | Runs the same cases' natural-language requests through the LLM buyer agent (`agent/`, `scripts/llm_eval.py`), once per model in `LLM_EVAL_MODELS` (default includes `verda:deepseek-4-1-flash-1017`, but the repo variable sets `gemini:gemini-4-hydrogen verda:kimi-k3-1-pro`, so DeepSeek has never run). Uploads `evals/llm_results.*.json` + `docs/LLM_EVAL.md` as `llm-eval-results` and puts the comparison table in the job summary. Skips with a notice when the Valio key or both model keys are missing, and on pushes that don't touch the agent, its tools or the cases. Fails only if an auto-approved order violates its mandate. See [LLM_EVAL.md](LLM_EVAL.md). | 8 | **deny**, only `pypi` + `junction26.valioaimo.fi`, `generativelanguage.googleapis.com`, `inference.verda.com`, `integrate.api.nvidia.com` (443) + Aiven Valkey (`*.aivencloud.com:23770`, TLS) |
| `draft-eval` | Plain-language rules -> mandate draft (`scripts/draft_eval.py`, `evals/draft_cases.yaml`): the offline rule parser, then Gemini (`--provider gemini`) through the same validator. Writes `evals/draft_results.<provider>.json` (artifact `draft-eval-results`) and a per-case rules-vs-Gemini table in the job summary. No Valio key and no catalog download (the validator uses its built-in claim list, as in `test`). Runs by hand (`gh workflow run CI -f draft_only=true` skips the other evals) or on pushes to main that touch the drafter or its cases; fails only if the rule parser regresses. Own job, no `needs`: it shares no rate-limited key with `eval`/`llm-eval`, and a `draft_only` dispatch is in its own concurrency group, so it can't bump a queued push run. | 2 (default) | **deny**, only `pypi` + `generativelanguage.googleapis.com:443` |

All actions are pinned to full commit SHAs (version in a comment).

### Offline smoke without touching the app

The image's normal start command is unchanged: `scripts/fetch_data.py` requires *some*
`VALIO_AIMO_API_KEY`, but skips each download when the file already exists in `/data`. The smoke
override mounts a copy of `tests/fixtures/catalog/` (products.json, delivery-reliability.csv,
carbon-footprint.csv: 20 records from the challenge data) at `/data` and sets the key to a dummy
value, so the gateway builds its index from the fixtures and never calls Valio.

Run it locally:

```bash
docker build -t sous:ci .
cp -r tests/fixtures/catalog /tmp/vk-smoke && echo "MARIADB_PASSWORD=dev" > .env
SMOKE_DATA_DIR=/tmp/vk-smoke docker compose -f compose.yaml -f tests/fixtures/compose.smoke.yaml up -d --no-build
EXPECT_STORAGE=mariadb tests/fixtures/smoke.sh
```

### Shared Valio budget (Aiven for Valkey)

`eval` and `llm-eval` get `SOUS_VALKEY_URL` from the `VALKEY_URL` secret and run as
`SOUS_DEPLOYMENT_ROLE=ci`, so their Valio calls count against the same budget as prod and staging and
stop at half of it ([AIVEN.md](AIVEN.md)). `scripts/valio_budget.py` prints the shared budget
(backend, usage by role; never the URL) before/after the eval and into the job summary. Without the
secret the jobs use per-process limits as before.

## Switching to CRACI

1. **Connect GitHub (one time, org/repo owner in a browser):** CRACI web app →
   Settings → Builds → Install GitHub App, covering `Zawaer/sous`
   ([guide](https://docs.craci.com/guides/github-connection/)). Without it, CRACI jobs queue forever.
2. **Set the repo variable** `CI_RUNNER` = `craci`
   (GitHub → Settings → Secrets and variables → Actions → Variables), or
   `gh variable set CI_RUNNER -R Zawaer/sous --body craci`.
   The label is exactly `craci`: CRACI puts no size or arch suffix in `runs-on`.
3. Re-run CI (`gh workflow run CI -R Zawaer/sous`). To go back, delete the variable.
4. For the eval job: `gh secret set VALIO_AIMO_API_KEY -R Zawaer/sous` (paste the key at the prompt).

Each job starts with a `cracicorp/setup@v1` step that sets `size` and, for `smoke`/`eval`, a network
policy. CRACI reads those inputs when it schedules the job and ignores the step's `if:`; the `if:`
(`vars.CI_RUNNER == 'craci'`) only stops the step from executing on GitHub-hosted runners.

If a CRACI job fails with a `403`/connection reset, open the job → **View trace** →
**Proxy violations**, and add what is missing to that job's `network:` block
([reference](https://docs.craci.com/guides/setup-action-network/)). Expected first-run candidates:
nothing for `smoke`; for `eval`, a host `scripts/eval.py` contacts besides Valio (if it calls an LLM,
add e.g. `api.anthropic.com` as a second `tls` connection, and say so in the eval's docs).

## What to show the CRACI judges

1. **Compute sized per job, in parallel.** `test` and `docker` (8 vCPU each) start together;
   `smoke` and `eval` fan out after them. Cold vs warm run times show the uv cache and the buildx
   layer cache (NVMe-backed on CRACI).
2. **Two SBOMs that should agree.** CRACI's own job SBOM (built from the packages it saw each job
   download through its proxy) next to our syft CycloneDX SBOM of the final image
   (`sbom-and-vulns` artifact). The Python packages in both come from `uv.lock`; differences are
   worth explaining (we have not done this comparison yet). syft's image SBOM in run 43530484114 has 694 packages: 94 PyPI (uv.lock), 87 Debian from the
   `python:3.12-slim` base, and 513 Rust crates embedded in the `uv` binary copied from
   `ghcr.io/astral-sh/uv` (read from its cargo-auditable metadata); CRACI sees the image pulls and
   the PyPI downloads it proxied.
3. **Vulnerability management with a sane gate.** The grype report keeps everything; the build fails
   only on critical + fix-available, so CI stays actionable instead of permanently red on unfixable
   base-image CVEs. Run 43530484114: 0 critical, 61 high (2 fixable), 73 medium (12 fixable). Compare with CRACI's vulnerability view for the same run.
4. **Network trace as a security property of the gateway.** This is the point of the product: a
   purchasing gateway that holds a supplier API key must not talk to anything else.
   - `smoke`: the gateway boots, serves REST and MCP, signs with its Ed25519 key and talks to
     MariaDB under a **deny-all** policy with no exceptions (both images are loaded from the docker job's artifact). Its trace should
     show **no** traffic from the app at all.
   - `eval`: the trace should show **only `junction26.valioaimo.fi`** plus package registries
     (PyPI for uv and the locked dependencies). Any other destination would be a proxy violation,
     i.e. a blocked exfiltration attempt, not a silent leak of the Valio API key or order data.
   - `test` and `docker` run in observe mode, so their traces document every registry the build
     touches (PyPI, Docker Hub, ghcr.io, GitHub releases for uv/syft/grype, the grype DB). The next
     step for a **Complete** SBOM is to move them to `default: deny` with exactly those sources,
     using that trace as the allowlist.
