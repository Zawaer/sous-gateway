<!-- Description below: 4,565 characters (limit 5,000), counted with Python len() on the text between the
     Description and Challenge headings, trimmed. Recount after any edit:
     python3 -c "s=open('docs/pitch/submission.md').read();print(len(s.split(chr(10)+'## Description'+chr(10))[1].split(chr(10)+'## Challenge'+chr(10))[0].strip()))"
     Every number is from docs/STATUS.md (run ids given), except drafting (docs/GEMINI.md) and conformance
     (docs/CONFORMANCE.md; staging 15/15 at 7c9a965, agent board 14.11 05:23Z). -->

# Junction submission

## Name
Sous

## Punchline
AI purchasing agents carry a signed mandate from their human, and Valio Aimo's gateway enforces it.

## Description
**Sous is Valio Aimo's front door for AI purchasing agents.** A hotel's, hospital's or school's agent connects over MCP or OpenAPI, searches the catalog, gets a signed quote with live price and availability, and orders, inside limits its human set in a signed mandate (*valtakirja*). Plain code puts every order in one of four tiers: auto, auto + FYI, needs approval, refused. No language model ever approves an order or relaxes a rule; the one classifier we use (Gemini Decisions, on products with no allergen declaration) can only add a refusal. Live: https://135-181-8-209.sslip.io (a judge path in 5 minutes is at the top of the README).

**The problem.** We asked a general shopping agent for "1,000 litres of lactose-free milk by Monday". After 13 minutes of research it concluded it would have to phone wholesalers on Saturday. Agents will buy from wholesalers anyway; if Valio Aimo doesn't define how they do it safely and openly, someone else's platform will.

**Who uses it.** Valio Aimo runs the gateway. The buyer's agent does the routine work; a head of catering approves held orders on a phone; Valio Aimo staff read the same audit trail.

**Request → lookup → order → approval, live.** Search covers 17,546 products (fi/sv/en) on a local index with no API calls. A quote costs at most 2 batched Valio calls (2 more when substitutes are priced). It carries pack and lot sizes, VAT, cut-offs, and stock as buckets (ample/limited/short), never Valio's exact numbers. A held order carries an approve-by time, Valio's earliest cut-off minus 15 minutes, so the human decides while the delivery can still be met. Notifications go by push, in-app feed and signed webhook. In one real run, Claude Code on a laptop ordered 1,000 × Valio Eila 1 L (€1,400 ex VAT) over MCP with 2 Valio calls; above the hotel's FYI threshold, the manager was told.

**Exceptions.** When an item can't be covered as asked, the signed quote lists options (part now + rest later, all later, the next date after a passed cut-off, a substitute), each with a mandate preview. Substitutes keep the product type (sour milk is not milk) and the allergen rules. Terminal products show a cut-off that already passed while promising Monday; we trust the cut-off. 30 % of food products declare no allergens; for a mandate with allergen rules, unknown means a human decides. Valio Aimo can put a product on hold or recall it with one call, and standing orders that drift (milk +18 % against the 4-week median) ask before renewing.

**Controls.** Every rule has an id, tier and plain meaning at `/rules`. Agents can't approve (403, audited). Mandates, quotes and receipts are Ed25519 JWS; orders use only the signed payloads, one per quote, bound to the agent. A hash-chained log plus MariaDB system versioning shows what the mandate said when each order was decided. Reads are scoped: another buyer's order answers 404.

**Measured (our own cases, live Valio prices).** Scripted buyer requests: 60/60 locally with the stricter demo policy, 44/44 unsafe actions blocked or escalated; 59/59 in CI on CRACI (run 43554621653). Attacks: 20 parallel orders on one quote give 1 order and 19 rejections; order slicing, self-approval, prompt injection and an unusual quantity are all held or refused. LLM buyer agent on 49 sentences (CI run 43557209740): Kimi K3.1 Pro on Verda Inference 48/49, Gemini 4 Hydrogen 46/49, 0 mandate violations auto-ordered. A 15-check black-box conformance kit passes 15/15 on staging; judges can run seven attacks themselves at `/ui/break.html` in a private sandbox.

**AI where it helps, never deciding.** Gemini drafts a mandate from plain language and a validator checks every field (23/23 on our cases, CI run 43566010358); nothing that loosens a mandate survives. Nemotron 3.5 Super VL reads a photo of an order list into a basket, with 0 silent errors over 32 page reads (CI run 43530170621); unreadable lines go to a human.

**Open and run like production.** MCP tools return typed structured output; AP2 v0.2 claim sets (unsigned views), an A2A agent card, MCP server card and schema.org exports exist. CI on CRACI: tests with MariaDB, SBOM and grype gate, deny-by-default egress per job. Production and staging run on Verda CPU instances in Finland. Aiven for Valkey shares one Valio call budget across deployments (live in CI; not yet on the servers).

**Honest limits.** Identity is demo-grade: one shared approver code for humans, bearer tokens for agents. There is no real order API, so "ordered" means accepted and signed by Sous, at list prices. All eval cases were written by us, not held out. Next: buyer login (OIDC), agent keys bound to the mandate, Valio Aimo's order API with contract prices, key rotation.

## Challenge
- **Valio Aimo: Trusted Trade Agents** (main)
- Tech challenges: **Best Use of Verda** ([write-up](../tech-challenges/verda.md)), **Best Use of Gemini API** ([gemini.md](../tech-challenges/gemini.md)), **Best Use of Nemotron** ([nemotron.md](../tech-challenges/nemotron.md)), **Best Use of Aiven** ([aiven.md](../tech-challenges/aiven.md)), **Best use of CRACI** ([craci.md](../tech-challenges/craci.md)), **Best Use of Codex** ([codex.md](../tech-challenges/codex.md))

## Technologies
- Python, FastAPI, Pydantic, uv; JavaScript (seller UI, reference buyer)
- Model Context Protocol (structured tool output), OpenAPI, JSON Schema
- Ed25519, JWS, JWKS; W3C Verifiable Credentials (opt-in buyer-signed mandates); AP2, A2A, schema.org exports
- MariaDB (system-versioned tables), SQLite FTS5
- Aiven for Valkey (shared Valio API budget and cache)
- Verda: CPU instances in Finland, Verda Inference (Kimi K3.1 Pro)
- Google Gemini API (buyer agent, mandate drafting, menu planning, Decisions API); NVIDIA Nemotron 3.5 Super VL (photo reading)
- GitHub Actions on CRACI runners; syft (CycloneDX SBOM), grype
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
