# Challenge brief

## 5. Trusted Trade Agents (Valio Aimo) — €4,000

**Task:** Let external autonomous purchasing agents find products, compare offers and place orders with Valio Aimo (food-service wholesaler, 20k+ kitchens, 25k products, 700 suppliers) **without exposing real systems, confidential data or the marketplace roadmap**. Define the interface and operating model, built on **open, verifiable standards**, not a black box.

- **Core requirements:**
  1. Product, price and availability lookup in a structured form for an external agent.
  2. Request price/availability → place an order (fixed prices, no negotiation).
  3. Permission and human-approval management: what the agent decides alone and what needs sign-off.
- Optional: delivery-time and sustainability comparison (certificates/labels; carbon ranges as a stretch), substitution/exception handling, traceability with a human-readable log.
- Example: "I need 1,000 litres of lactose-free milk by Monday" from a hotel chain's agent → structured products, prices, availability → order within its authority or escalate to a human.
- Problems they'd love: a trust protocol (minimum verification for stock, price, origin), a guardrails model (spend threshold, substitutions outside preferences), response logic (partial availability, good-better-best substitution), an audit trail for buyer and Valio Aimo.
- Scenario ideas: need → recipe → order; last-minute event; supplier stock-out with a recipe-compatible substitute; proactive selling agent; allergen-safe substitution (school/hospital); standing-order drift detection.
- Preferred tech: agentic/tool-use frameworks, open interop standards (MCP etc.), recommendation logic, optional verifiable credentials or a trust layer. x402 / Nevermined are relevant but not required.
- **Resources:** synthetic catalog, simulated suppliers, mock APIs (prices, availability behaviour, lead times, certificates).
  - Resource portal: see the Junction platform (password-protected)
  - API key: on the Junction platform, kept in `.env`
  - Webshop test accounts: shared later.
- Mentors: Hanna Forsström, Pirjo Kaartinen (commercial); Minni Ruhtinas (online sales); Tommi Nurmi-Aro, Ville Takanen, Riku Tapper (IT, integrations, data governance).
- **Judging:** Working end-to-end demo (request → lookup → order/approval) **30%** · Solving the user need 20% · Autonomy & controls 20% · Robustness to exceptions 15% · Openness & path to production 15%.
- **Prizes:** €4,000 / €2,000 / €1,000.
