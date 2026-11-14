# Deck outline (PDF, 12 slides)

The deck is [deck/index.html](deck/index.html), rendered to [deck.pdf](deck.pdf) by `scripts/build_deck.py`. One idea
per slide, numbers with sources in the footer. Numbers must match `docs/STATUS.md`; colours and tier badges follow
`web/tokens.css` (Valio Aimo petrol as accent only, tiers by fill weight + icon).

1. **Sous.** The one-liner, live URL, QR, repo, MIT.
2. **Problem.** "1,000 L of lactose-free milk by Monday": 13 minutes and a phone call vs. seconds over MCP
   (Valio Eila × 1000, €1,400, 2 Valio calls, Auto + FYI). Agents will buy anyway; whose rules?
3. **How it works.** Agent → MCP/OpenAPI → search (17,546 products, 0 calls, type guard) → signed quote (live price,
   stock buckets, options) → mandate engine (plain code, `/rules`) → orders (holds, recalls) → audit. Phone and
   shared record below.
4. **Four tiers.** Real production orders for Auto / Auto + FYI / Needs approval / Refused; the mandate screenshot.
5. **Human in the loop.** approve_by = earliest cut-off − 15 min; `expired_cutoff`, `approval_deadline_passed`;
   agents can't approve. Phone screenshot.
6. **Exceptions.** Response options (on_time … next_date … substitute); Terminal cut-off; 30 % of food products
   without allergen data; product type; holds and recalls.
7. **Beyond one order.** Standing-order drift (milk +18 %, cost +€135 / +16 %) and Valio suggests inside the mandate.
8. **Trust.** Signed receipt, AS OF query; only signed data, two audit layers, scoped reads, stock buckets,
   `agent_claim`.
9. **Try to break it.** Attack table (20 parallel → 1 ordered, slicing, self-approval, injection, unusual quantity);
   conformance kit 15/15 on staging; `/ui/break.html`.
10. **Models read, code decides.** Kimi 48/49, Gemini 46/49 (CI 43557209740), drafting 23/23 (43566010358),
    Nemotron 0 silent errors (43530170621). H20 and "our own cases" in the footer.
11. **Numbers.** 60/60, 44/44, 0 violations, ≤ 2 calls (1.24 avg in CI), search 56 → 100 %, 36 % to a human.
12. **Open, production, next.** MCP structured output, AP2/A2A/schema.org exports, conformance kit; CRACI, Verda,
    Aiven (live in CI, not yet on the servers); next: OIDC, bound agent keys, real order API, key rotation. CRACI
    evidence line stays conditional until docs/CRACI.md has a green run.
