<!-- Description below: 4,861 characters (limit 5,000), counted with Python len() on the text between the
     Description and Challenge headings, trimmed. Recount after any edit:
     python3 -c "s=open('docs/pitch/submission.md').read();print(len(s.split(chr(10)+'## Description'+chr(10))[1].split(chr(10)+'## Challenge'+chr(10))[0].strip()))"
     Story order follows Valio's criteria: end to end 30, need match 20, autonomy + controls 20, exceptions 15, openness 15.
     Numbers: scripted and LLM (Kimi 47/48, Gemini 46/48) and drafting 23/23 from CI run 43587725624 (11d8448); held-out
     from CI 43620135327 (docs/LLM_EVAL.md#held-out, evals/heldout/); red team Qwen + Gemma + slicing re-run from
     docs/REDTEAM.md; 32-vCPU run 43611589764 (docs/CRACI.md); Nemotron 32 reads CI 43530170621, Verda 8 reads run 6
     (docs/NEMOTRON.md); earned autonomy thresholds PROTOCOL 7.2; conformance 17/17 (docs/CONFORMANCE.md). Checked Sat 14.11 ~18:10Z. -->

# Junction submission

## Name
Sous

## Punchline
AI purchasing agents order from Valio Aimo under their customer's signed mandate: the customer stays in control, and autonomy is earned.

## Description
**Sous is Valio Aimo's front door for AI purchasing agents, built so the customer stays in control and autonomy is earned.** A hotel's, hospital's or school's agent connects over MCP or OpenAPI, searches the catalog, gets a signed quote with live price and availability, and orders inside a mandate (*valtakirja*) its human set. Plain code puts every order in one of four tiers: auto, auto + FYI, needs approval, refused. No language model approves an order or relaxes a rule. Live in Finland: https://135-181-8-209.sslip.io (a 5-minute judge path tops the README).

**The problem.** We asked a general shopping agent for "1,000 litres of lactose-free milk by Monday". After 13 minutes it told us to phone a wholesaler. Agents will buy from wholesalers anyway; the question is whose rules they follow.

**End to end: request → lookup → order → approval.** Search covers 17,546 products (fi/sv/en) on a local index, no API calls. A quote costs at most 2 batched Valio calls (0.47 per request on average in CI) and carries pack and lot sizes, VAT, cut-offs and stock as buckets. A held order reaches the buyer's phone with an approve-by time: the earliest cut-off minus 15 minutes. Claude Code over MCP and claude.ai through our OAuth connector each ordered 1,000 L of lactose-free milk with 2 Valio calls. Scripted cases: 60/60, 43/43 unsafe actions blocked or escalated.

**Matching what the buyer asked.** Search ranks by product type (lactose-free milk before coffee milk), and the gateway checks the chosen product fits its use: skimmed milk for a dessert or coffee cream for whipping is held, with a fitting product from the same search. On our own 48 sentences, Kimi K3.1 Pro matched 47/48 and Gemini 4 Hydrogen 46/48. On 15 blind held-out requests (3 runs each) safety held, 0 false auto-approvals and 9/9 unsafe stopped by the gateway, but matching was weak, 3/36 runs where an order was due: the agent took one item per request and read "for 40" as 40 packs. We publish that; fixes for baskets, portions and menus are in progress, and a second frozen set of 20 waits for the re-run.

**Autonomy and controls: the customer decides.** Held orders are decided by the buyer, never by Valio staff. Agents can't approve (403, audited) and can't enrol an approver device: passkeys are enrolled only with the buyer's own credential, never by whoever opens a link first. A passkey approval signs this order at this total, including any "remember this" choice. Autonomy is earned: after 5 or more unchanged approvals over 2+ weeks for one rule and product type, Sous proposes ask → notify with the evidence; the buyer accepts it as a re-signed mandate, and a later rejection proposes taking it back. Allergens and spend limits are never delegated. Optional controls: four-eyes (the requester can't approve), a cap on a substitute's price premium over the product it replaces, and doubts the agent declares always reach a human. Every rule has an id and plain meaning at `/rules`, in English and Finnish; a hash-chained log plus MariaDB system versioning shows what the mandate said when each order was decided.

**Exceptions.** When an item can't be covered as asked, the signed quote lists options (part now + rest later, a later date, a substitute), each with a mandate preview. Substitutes keep product type and allergen rules and are judged by use: another brand of whipping cream for a dessert asks, coffee cream for coffee doesn't. When needs change mid-contract (a ward goes nut-free), standing, held and upcoming orders and remembered approvals are re-checked. Unknown allergens mean a human decides; drifting standing orders ask; short picks become signed amendments.

**Open and measured.** MCP with typed output, OpenAPI, Ed25519 JWS checkable offline, Peppol BIS Ordering 3.0 validated in CI, AP2/A2A/schema.org exports, a 17-check conformance kit (17/17 on staging). 20 parallel orders on one quote: 1 order. Red team on Verda GPUs: Qwen3-30B-A3B, 976/997 unsafe held of 2,000; Gemma 4 26B, 289/301 of 600; the order-slicing gaps they found are fixed (re-run: 0 gateway violations). On CRACI's 32 vCPUs all 4,000 red-team requests run in 15.4 s, 2,036/2,036 unsafe held. Every number names its CI run in docs/STATUS.md.

**AI where it helps, never deciding.** Gemini drafts mandates (23/23 checked); its Decisions API reads undeclared allergens and can only add a refusal. Nemotron 3.5 Super VL, also on Verda's Nemotron endpoint, reads an order photo into a basket: 0 silent errors over 32 page reads on NVIDIA's API and 8 on Verda with reasoning on.

**Honest limits.** Enrolling an approver still needs a shared code; agents hold bearer tokens. No real order API: "ordered" means signed by Sous at list prices, ERP hand-off labelled simulated. Next: buyer login (OIDC), agent keys bound to the mandate, Valio Aimo's order API with contract prices.

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
