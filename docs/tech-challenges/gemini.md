# Best Use of Gemini API

Sous lets AI agents order from Valio Aimo under a mandate signed by their human. **Gemini (`gemini-4-hydrogen`) is the default brain of the buyer side**: it turns how a chef talks into something a gateway can price and check.

**What Gemini does**
1. **Menu -> one order (`agent/recipe.py`).** *"Lasagne for 120 on Monday, we have 10 kg flour and plenty of salt"* or *"Karjalanpaisti 60 annosta perjantaiksi, meillä on 5 kg sipulia"* becomes a plan: dishes, a foodservice recipe per dish (raw amount per portion), portions, delivery date, stock on hand. Requested with **structured output** (`response_format: json_schema`, `MENU_SCHEMA`), then validated with pydantic; one retry with the validation error. Code scales, subtracts stock, finds products under the mandate's allergen rules, gets **one signed basket quote**.
2. **Single requests (`agent/buyer.py`).** Gemini drives the gateway's MCP tools (search, quote, substitutes, order) and explains the decision.

**What it does not do.** Gemini never picks the product a line is bought from or decides the tier; the runtime binds agent, mandate and idempotency key. A misread costs a wrong quantity on a quote a human can read, never an order the mandate forbids.

**Results (CI on CRACI, run 43524836008, live Valio prices)**
- Recipe eval, 7 menus (Finnish and English, 4 mandates incl. avoid-milk and avoid-gluten/egg): **7/7 schema-valid on the first answer, 8/8 dishes, portions/date 7/7, stock 4/4, 22/23 main ingredients, 0 allergen-rule violations on quoted lines, tier 7/7**; 4/7 within 35 % of the reference recipe (cook's judgment, e.g. 50 g vs 40 g lasagne sheets). ~2,000 tokens, ~$0.006, 10.1 s per menu.
- Buyer eval, 40 requests: **36/40 matched without correction, 38/40 understood, 0 mandate violations**, $0.037/request (a local run: 37/40, 39/40). Best understanding of the models we ran.
- The eval loop improved the planner: undeliverable items reported instead of blocking the basket, oversize packs dropped. Spend: $3.02 ([COSTS.md](../COSTS.md)).

**Why Gemini.** Structured output in Finnish, Swedish and English, honoured on every eval call, makes "a sentence from the kitchen -> one auditable quote" work. An offline table knows six dishes; Gemini knows the menu.

[PENDING: web demo surface. Today: CLI (`python -m agent.recipe`) and CI.]

Code: https://github.com/Zawaer/sous · [docs/GEMINI.md](../GEMINI.md) · [evals/recipe_results.md](../../evals/recipe_results.md)
