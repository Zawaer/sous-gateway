# CI

One workflow, [`.github/workflows/ci.yml`](../.github/workflows/ci.yml), runs on every push to
`main`, every pull request and on demand (`workflow_dispatch`). It runs on GitHub-hosted runners
today (`ubuntu-24.04`, the same base as CRACI Slim) and on [CRACI](https://docs.craci.com) runners once one repo variable is set.

```
test ──────────► eval (main / manual only; needs VALIO_AIMO_API_KEY + scripts/eval.py)
docker ────────► smoke
```

| Job | What it does | CRACI size | CRACI egress policy |
|---|---|---|---|
| `test` | `uv sync --frozen` + `pytest` against a MariaDB 11.4 service (system-versioned tables). uv cache keyed on `uv.lock`. | 4 | default (observe: all presets, unmatched allowed) |
| `docker` | Builds the [Dockerfile](../Dockerfile) with buildx (layer cache in the Actions cache), writes a CycloneDX SBOM with syft, scans it with grype, fails only on **critical vulnerabilities that have a fix**. Uploads `sbom-and-vulns` (SBOM, grype JSON, TSV) and the image. Severity table in the job summary. | 8 | default (observe) |
| `smoke` | Loads that image and starts the real [compose.yaml](../compose.yaml) (MariaDB + gateway) with the override [tests/fixtures/compose.smoke.yaml](../tests/fixtures/compose.smoke.yaml): a 20-product fixture catalog instead of Valio's data, no API key. [tests/fixtures/smoke.sh](../tests/fixtures/smoke.sh) checks `/health` (storage = mariadb), `/docs`, `/openapi.json`, `/.well-known/jwks.json` (Ed25519 key), a catalog search, and MCP `initialize` + `tools/list` at `/mcp/`. | 2 (default) | **deny**, only `dockerhub` |
| `eval` | Runs `scripts/eval.py` against the live Valio Aimo API. Skips itself (with a notice) when the `VALIO_AIMO_API_KEY` secret or `scripts/eval.py` is missing. Never runs on pull requests. Valio data files are cached between runs. | 2 (default) | **deny**, only `pypi` + `junction26.valioaimo.fi:443` |

All actions are pinned to full commit SHAs (version in a comment).

### Offline smoke without touching the app

The image's normal start command is unchanged: `scripts/fetch_data.py` requires *some*
`VALIO_AIMO_API_KEY`, but skips each download when the file already exists in `/data`. The smoke
override mounts a copy of `tests/fixtures/catalog/` (products.json, delivery-reliability.csv,
carbon-footprint.csv: 20 records from the challenge data) at `/data` and sets the key to a dummy
value, so the gateway builds its index from the fixtures and never calls Valio.

Run it locally:

```bash
docker build -t valtakirja:ci .
cp -r tests/fixtures/catalog /tmp/vk-smoke && echo "MARIADB_PASSWORD=dev" > .env
SMOKE_DATA_DIR=/tmp/vk-smoke docker compose -f compose.yaml -f tests/fixtures/compose.smoke.yaml up -d --no-build
EXPECT_STORAGE=mariadb tests/fixtures/smoke.sh
```

## Switching to CRACI

1. **Connect GitHub (one time, org/repo owner in a browser):** CRACI web app →
   Settings → Builds → Install GitHub App, covering `Zawaer/valtakirja`
   ([guide](https://docs.craci.com/guides/github-connection/)). Without it, CRACI jobs queue forever.
2. **Set the repo variable** `CI_RUNNER` = `craci`
   (GitHub → Settings → Secrets and variables → Actions → Variables), or
   `gh variable set CI_RUNNER -R Zawaer/valtakirja --body craci`.
   The label is exactly `craci`: CRACI puts no size or arch suffix in `runs-on`.
3. Re-run CI (`gh workflow run CI -R Zawaer/valtakirja`). To go back, delete the variable.
4. For the eval job: `gh secret set VALIO_AIMO_API_KEY -R Zawaer/valtakirja` (paste the key at the prompt).

Each job starts with a `cracicorp/setup@v1` step that sets `size` and, for `smoke`/`eval`, a network
policy. CRACI reads those inputs when it schedules the job and ignores the step's `if:`; the `if:`
(`vars.CI_RUNNER == 'craci'`) only stops the step from executing on GitHub-hosted runners.

If a CRACI job fails with a `403`/connection reset, open the job → **View trace** →
**Proxy violations**, and add what is missing to that job's `network:` block
([reference](https://docs.craci.com/guides/setup-action-network/)). Expected first-run candidates:
nothing for `smoke`; for `eval`, a host `scripts/eval.py` contacts besides Valio (if it calls an LLM,
add e.g. `api.anthropic.com` as a second `tls` connection, and say so in the eval's docs).

## What to show the CRACI judges

1. **Compute sized per job, in parallel.** `test` (4 vCPU) and `docker` (8 vCPU) start together;
   `smoke` and `eval` fan out after them. Cold vs warm run times show the uv cache and the buildx
   layer cache (NVMe-backed on CRACI).
2. **Two SBOMs that should agree.** CRACI's own job SBOM (built from the packages it saw each job
   download through its proxy) next to our syft CycloneDX SBOM of the final image
   (`sbom-and-vulns` artifact). The Python packages in both come from `uv.lock`; differences are
   worth explaining. syft's image SBOM has ~90 PyPI packages (uv.lock), ~90 Debian packages from the
   `python:3.12-slim` base, and ~500 Rust crates embedded in the `uv` binary copied from
   `ghcr.io/astral-sh/uv` (read from its cargo-auditable metadata); CRACI sees the image pulls and
   the PyPI downloads it proxied.
3. **Vulnerability management with a sane gate.** The grype report keeps everything; the build fails
   only on critical + fix-available, so CI stays actionable instead of permanently red on unfixable
   base-image CVEs. Compare with CRACI's vulnerability view for the same run.
4. **Network trace as a security property of the gateway.** This is the point of the product: a
   purchasing gateway that holds a supplier API key must not talk to anything else.
   - `smoke`: the gateway boots, serves REST and MCP, signs with its Ed25519 key and talks to
     MariaDB under a **deny-all** policy (only Docker Hub for the mariadb image). Its trace should
     show **no** traffic from the app at all.
   - `eval`: the trace should show **only `junction26.valioaimo.fi`** plus package registries
     (PyPI for uv and the locked dependencies). Any other destination would be a proxy violation,
     i.e. a blocked exfiltration attempt, not a silent leak of the Valio API key or order data.
   - `test` and `docker` run in observe mode, so their traces document every registry the build
     touches (PyPI, Docker Hub, ghcr.io, GitHub releases for uv/syft/grype, the grype DB). The next
     step for a **Complete** SBOM is to move them to `default: deny` with exactly those sources,
     using that trace as the allowlist.
