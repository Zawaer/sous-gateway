# Tech challenge write-ups

One file per challenge, each ≤ 2,500 characters, ready to paste. Numbers come from the eval results, `docs/COSTS.md` and CI runs; Each write-up ends with what is not done; keep those lines when pasting.

| Challenge | Write-up | Where it goes |
|---|---|---|
| Best Use of Verda | [verda.md](verda.md) | Submission form (tech challenge field) **and** a post in Verda's Discord challenge channel (text below) |
| Best use of CRACI | [craci.md](craci.md) | Submission form |
| Best Use of Codex | [codex.md](codex.md) | Submission form |
| Best Use of Gemini API | [gemini.md](gemini.md) | Submission form |
| Best Use of Aiven | [aiven.md](aiven.md) | Submission form |
| Best Use of Nemotron | [nemotron.md](nemotron.md) | Submission form: the required short explanation (model, what it does, why it matters, how we improved its outputs) |

## Checklist

- [ ] Repo is **public** before any link is posted (Discord, form).
- [ ] **Verda:** post the Discord text below in Verda's challenge channel (Verda's site is down since Sat ~00:15Z; check it is back); paste verda.md in the form.
- [ ] **Verda:** re-run Kimi when Verda Inference is reachable, then update the numbers here, in verda.md, VERDA.md and STATUS.md.
- [ ] **CRACI:** screenshots of CRACI's SBOM/vulnerability view and the `smoke` job's empty trace for run 43530484114.
- [ ] **Aiven:** set `SOUS_VALKEY_URL` + `SOUS_DEPLOYMENT_ROLE` on prod and staging, redeploy, check `/health` -> `shared_budget.backend == "valkey"`; then update aiven.md ("Where it is live").
- [ ] **Gemini / Nemotron:** model keys on the servers and `SOUS_BUYER_AGENT=1`, or show the CLI in the video; then update the "Not done" lines.
- [ ] No secrets in any pasted text (API keys, approver codes, the Valkey URI).

Done since the first draft: CI run 43530484114 green on CRACI with Valkey; Codex lock race fixed (fe41018); Nemotron runs 3-5 committed; LLM eval re-run (Gemini 39/40).

## Verda Discord post (paste as is)

> **Sous** (Junction 2026, Valio Aimo challenge): Valio Aimo's front door for AI purchasing agents. An agent searches the catalog, gets a signed quote with live price and stock, and orders under a mandate signed by its human; the gateway enforces it in plain code.
>
> **On Verda:** the gateway (FastAPI + MCP + MariaDB) runs on a **CPU instance in Finland**: no GPU needed, and orders and the audit log stay next to the Finnish wholesaler. The buyer agent's model is **Kimi K3.1 Pro on Verda Inference** (open weights, EU, prompts not stored), because what a hospital or school kitchen buys is sensitive.
>
> **Process:** compose deploy -> LLM agent on the gateway's MCP tools -> per-model eval in CI. Kimi on Verda (CI run 43511463514, 40 buyer sentences we wrote, live Valio prices): 36/40 matched without correction (34/40 in a later run), 31/31 unsafe requests blocked or escalated, 0 mandate violations, p50 8.4 s. Next (not run yet): the same model on a Verda GPU with vLLM; only the base URL changes.
>
> Write-up: https://github.com/Zawaer/sous/blob/main/docs/VERDA.md · Repo: https://github.com/Zawaer/sous
