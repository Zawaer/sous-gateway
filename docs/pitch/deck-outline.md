# Deck outline (PDF, 12 slides)

The deck is [deck/index.html](deck/index.html) (+ `deck.css`, `qr.js`, `img/`, local `fonts/`), rendered to
[deck.pdf](deck.pdf) by `scripts/build_deck.py`. PowerPoint style: one idea per slide, a big headline, one visual, few
words. Every screenshot is a real production screen from the judge sandbox SBX-UF7A (Sat 14.11); numbers match
`docs/STATUS.md` and carry their source in the footer. Colours and tier badges follow `web/tokens.css`. It follows the
same arc as the demo video ([video-script.md](video-script.md)): the flow, the three must-builds, the bonus ideas.

1. **Sous.** Valio Aimo's front door for AI purchasing agents; "The agent carries the mandate. Valio Aimo enforces it.
   The customer stays in control." Live page screenshot, prod URL, QR, repo, MIT.
2. **The problem, by example.** "Monday's panna cotta: our usual whipping cream can't come." (its order cut-off had
   passed). An agent on its own swaps in another cream; Sous knows the cream is for a recipe, so that swap waits for the chef's yes (coffee cream could be
   any brand). The gateway's real reason on production, `substitution.use_sensitive`, quoted word for word.
3. **Sous runs all four steps, live.** Request (any agent, MCP or OpenAPI) → Lookup (17,546 products, 0 Valio calls;
   live price and stock for the shortlist, ≤ 2 calls) → Order (Ed25519-signed quote, fixed price, ordered once) →
   Approval (the buyer's signed mandate, four tiers, plain code).
4. **Must-build 1: structured lookup, and more than price.** The real quote JSON: reliability, origin, labels, CO₂;
   the supplier-shipped product whose cut-off passed is skipped. Bonus: compare more than price.
5. **Must-build 2: a fixed price, signed, ordered once.** The buyer's order panel ("Committed to the quote at its fixed
   price"); signature checked against `/.well-known/jwks.json`, one order per quote, on account.
6. **Must-build 3: plain code decides, never a model.** The four tiers and Valio's decision panel, "Deterministic
   rules; no AI in this step."
7. **The buyer decides, on their phone.** Phone approval on the buyer side; Valio's view is read-only.
8. **What it's for decides the substitute.** Coffee → allowed, recipe → buyer decides (`purpose.ask`), allergy menu →
   only this product (`purpose.exact`), from Valio's own substitute suggestions. Bonus: handle exceptions.
9. **Routine runs alone. Exceptions ask.** Standing-order drift (milk +18 %, cost +16 %; history marked simulated) and
   the agent's self-approval refused (403 `agent_cannot_approve`).
10. **Every decision explains itself.** Hash-chained audit log both sides read; the approval page in Finnish. Bonus:
    explain decisions.
11. **Measured, and attacked.** 60/60 scripted requests, 43/43 unsafe blocked or sent to a human, 0 mandate
    violations ordered (5 LLMs, 10 runs), red team 2,000 requests on a Verda GPU, conformance 17/17, Peppol BIS
    documents pass the official validator. Footer: our own cases, not a held-out set.
12. **Open today. A pilot next.** MCP · OpenAPI · signed JSON · Peppol BIS Ordering 3.0 · A2A/AP2 exports · MIT; next:
    buyer login (OIDC) and agent keys bound to the mandate, Valio Aimo's order API (SAP ORDERS05 outbox stubbed today,
    labelled simulated), contract prices. Ask: one Valio Aimo customer kitchen for a pilot.

The stage pitch ([stage-pitch.md](stage-pitch.md)) uses slides 2, 6, 11, 12 and 1; slides 4–10 are the offline
fallback with production screenshots.
