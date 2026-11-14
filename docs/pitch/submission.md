<!-- Description below: 4,691 characters (limit 5,000), counted with Python len() on the text between the
     Description and Challenge headings, trimmed. Recount after any edit:
     python3 -c "s=open('docs/pitch/submission.md').read();print(len(s.split(chr(10)+'## Description'+chr(10))[1].split(chr(10)+'## Challenge'+chr(10))[0].strip()))"
     Story order follows Valio's criteria: end to end 30, need match 20, autonomy + controls 20, exceptions 15, openness 15.
     Numbers: scripted 60/60 and drafting 23/23 from CI run 43587725624 (11d8448); 5 models x generic/Sous agent from local runs
     (docs/STATUS.md, b86c6d6, evals/llm_results.*.json); Decisions allergen check docs/GEMINI.md (483/488, 468/4,118); held-out
     from CI 43620135327 (docs/LLM_EVAL.md#held-out, evals/heldout/); red team Qwen + Gemma + slicing re-run from
     docs/REDTEAM.md; 32-vCPU run 43611589764 (docs/CRACI.md); Nemotron 32 reads CI 43530170621, Verda 8 reads run 6
     (docs/NEMOTRON.md); earned autonomy thresholds PROTOCOL 7.2; conformance 17/17 (docs/CONFORMANCE.md). Checked Sat 14.11 ~18:10Z. -->

# Junction submission

## Name
Sous

## Punchline
Trust between Valio Aimo and its customers, for AI purchasing agents: the customer stays in control, and autonomy is earned.

## Description
**Sous is Valio Aimo's front door for AI purchasing agents, built on trust between Valio Aimo and its customers: the customer stays in control, and autonomy is earned.** A hotel's, hospital's or school's agent connects over MCP or OpenAPI, searches the catalog, gets a signed quote with live price and availability, and orders inside a mandate (*valtakirja*) its human set. Plain code puts every order in one of four tiers: auto, auto + FYI, needs approval, refused. No language model approves an order or relaxes a rule.

**The problem.** We asked a general shopping agent for "1,000 litres of lactose-free milk by Monday". After 13 minutes it told us to phone a wholesaler. Agents will buy anyway; the question is whose rules they follow.

**End to end, hosted: request → lookup → order → approval.** Search covers 17,546 products (fi/sv/en) on a local index, no API calls. A quote costs at most 2 batched Valio calls (0.47 per request on average in CI). A held order reaches the buyer's phone with an approve-by time (earliest cut-off minus 15 min). Claude Code over MCP and claude.ai through our OAuth connector each ordered 1,000 L of lactose-free milk with 2 Valio calls. Try it now at https://135-181-8-209.sslip.io (Finland): in claude.ai add the connector and press "Create a demo sandbox and connect"; you get the approver token, the assistant only the agent token. Scripted cases: 60/60, 43/43 unsafe blocked or escalated.

**Matching what the buyer asked.** Search ranks by product type (lactose-free milk before coffee milk), and the gateway checks the chosen product fits its use: skimmed milk for a dessert or coffee cream for whipping is held, with a fitting product from the same search. On our own 48 sentences, 5 models x 2 agents auto-ordered 0 mandate violations; with our Sous agent, Gemini 4 Hydrogen and Gemma 4 31B matched 48/48, Kimi K3.1 Pro 47/48. On 15 blind held-out requests (3 runs each) safety held, 0 false auto-approvals and 9/9 unsafe stopped by the gateway, but matching was weak, 3/36 runs where an order was due: one item per request, "for 40" read as 40 packs. Fixes are in progress; a second frozen set of 20 waits for the re-run.

**Autonomy and controls: the customer decides.** Only an authenticated person approves, with the approver code or a passkey bound to this order and total; agents get 403 (audited), can't enrol an approver device, and our claude.ai connector refuses an approver token. Autonomy is earned: after 5+ unchanged approvals over 2+ weeks for one rule and product type, Sous proposes ask → notify with the evidence; the buyer accepts it as a re-signed mandate, and a later rejection proposes taking it back. Allergens and spend limits are never delegated. Options: four-eyes, a cap on a substitute's price premium, and doubts the agent declares always reach a human. Every rule is at `/rules` (en/fi). Every quote and receipt is Ed25519-signed, and the order card inside Claude checks Valio's signature itself; a hash-chained log plus MariaDB system versioning shows what the mandate said at each decision.

**Exceptions.** When an item can't be covered as asked, the signed quote lists options (part now + rest later, a later date, a substitute), each with a mandate preview. Substitutes keep product type and allergens and are judged by use (another brand of whipping cream for a dessert asks). When needs change (a ward goes nut-free), running orders and remembered approvals are re-checked. Unknown allergens go to a human; drifting standing orders ask; short picks become signed amendments; Valio sees lost demand.

**Open and measured.** MCP with typed output, OpenAPI, signatures checkable offline, Peppol BIS Order and Order Response validated in CI, AP2/A2A/schema.org exports, a conformance kit (17/17 on staging). Red team on Verda GPUs: Qwen3-30B-A3B, 976/997 unsafe held; Gemma 4 26B, 289/301; the slicing gaps found are fixed (re-run: 0). CRACI's 32 vCPUs run all 4,000 red-team requests in 15.4 s, 2,036/2,036 unsafe held. Run ids: docs/STATUS.md.

**AI where it helps, never deciding.** Gemini drafts mandates (23/23 checked). On products with no allergen declaration, Gemini's Decisions API can only add a refusal: 483 of 488 "likely contains" answers confirmed by the declaration (99.0 %); 468 undeclared products caught. Nemotron 3.5 Super VL, also on Verda, reads an order photo: 0 silent errors (32 reads on NVIDIA's API, 8 on Verda).

**Honest limits.** Enrolling an approver needs a shared code; agents hold bearer tokens. No real order API: "ordered" means signed by Sous at list prices, ERP hand-off simulated. Next: OIDC login, agent keys bound to the mandate, Valio's order API with contract prices.

## Challenge
- **Valio Aimo: Trusted Trade Agents** (main)
- Built on **MariaDB** (for mariadb.org): mandates, orders, standing orders, holds and the pause switch live in system-versioned tables, so "what did the buyer's mandate say when this order was decided?" is one `SELECT … FOR SYSTEM_TIME AS OF TIMESTAMP '<decision time>'` query, served at `GET /orders/{id}/history`; the hash-chained audit log sits next to it.
- Tech challenges: **Best Use of Verda** ([write-up](../tech-challenges/verda.md)), **Best Use of Gemini API** ([gemini.md](../tech-challenges/gemini.md)), **Best Use of Nemotron** ([nemotron.md](../tech-challenges/nemotron.md)), **Best Use of Aiven** ([aiven.md](../tech-challenges/aiven.md)), **Best Use of Gemma** ([gemma.md](../tech-challenges/gemma.md)), **Best use of CRACI** ([craci.md](../tech-challenges/craci.md)), **Best Use of Codex** ([codex.md](../tech-challenges/codex.md))

## Technologies
- Python, FastAPI, Pydantic, uv; JavaScript (seller UI, reference buyer)
- Model Context Protocol (structured tool output), OpenAPI, JSON Schema
- Ed25519, JWS, JWKS; WebAuthn passkeys (approval bound to the order and total); W3C Verifiable Credentials (opt-in buyer-signed mandates); AP2, A2A, schema.org exports; Peppol BIS Ordering 3.0 (UBL 2.1)
- MariaDB (system-versioned tables), SQLite FTS5
- Aiven for Valkey (shared Valio API budget and cache)
- Verda: CPU instances in Finland, Verda Inference (Kimi K3.1 Pro), the Nemotron endpoint hosted with NVIDIA, GPU instances with vLLM (self-hosted Qwen3-30B-A3B and Gemma 4 26B-A4B, red team)
- Google Gemini API (buyer agent, mandate drafting, menu planning, Decisions API); Gemma 4 open models (buyer agent, red team); NVIDIA Nemotron 3.5 Super VL (photo reading)
- GitHub Actions on CRACI runners (incl. a 32-vCPU red-team job); syft (CycloneDX SBOM), grype on the image and on CRACI's own job SBOMs
- Docker Compose, Caddy, ntfy
- Claude Code and OpenAI Codex (development, reviews, and as buyer agents)

## Links
- Demo: https://135-181-8-209.sslip.io (staging fallback: https://staging.135-181-63-134.sslip.io)
- Source: https://github.com/Zawaer/sous (MIT)
- Pitch deck: [docs/pitch/deck.pdf](deck.pdf)
- Judge handout: [docs/pitch/try-it.md](try-it.md)

## Before submitting
- [ ] Recount the description (comment at the top) after any edit; keep it under 5,000.
- [ ] Re-check the numbers against docs/STATUS.md on the day; if prod was redeployed, re-check that `/rules`, `/ui/break.html` and `/sandbox` answer on the demo URL.
- [ ] Verda: post the Discord summary (docs/tech-challenges/README.md).
- [ ] Video ≤ 2:00 public; demo URL and repo link open in a private window; repo public.
- [ ] No secrets (approver code, API keys, Valkey URI) anywhere in the text, deck or video.
