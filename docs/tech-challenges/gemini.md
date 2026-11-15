# Best Use of Gemini API

**Problem.** Sous lets AI agents order from Valio Aimo under a mandate signed by their human. Kitchens don't order SKUs: they say *"Lasagne for 120 on Monday, we have 10 kg flour"* or *"Karjalanpaisti 60 annosta perjantaiksi"*. Something has to turn that into a request a gateway can price and check.

**Integration.** `gemini-4-hydrogen` through Gemini's OpenAI-compatible endpoint is the default model of the buyer side:
1. **Menu -> one order (`agent/recipe.py`).** Gemini returns dishes, a per-portion recipe, portions, date and stock on hand with **structured output** (`response_format: json_schema`, `MENU_SCHEMA`), validated with pydantic, one retry with the error. Code scales, subtracts stock and searches under the mandate's allergen rules; the gateway returns **one signed basket quote**.
2. **Single requests (`agent/buyer.py`).** Gemini drives the gateway's MCP tools (search, quote, substitutes, order) and explains the decision.

3. **Undeclared allergens (gateway, `sous/allergen_ai.py`).** 30 % of food products declare no allergens. The **Gemini Decisions API** answers one yes/no per avoided allergen with a calibrated probability; at ≥ 0.8 the gateway refuses, below it nothing is relaxed. Measured on products that do declare: 483/488 "likely contains" confirmed (99.0 %); 468 undeclared products caught.

Gemini never picks the product a line is bought from or the tier; the runtime binds agent, mandate and idempotency key. A misread costs a wrong quantity on a quote a human can read, never an order the mandate forbids.

**Results** (our own cases, live Valio prices, CI on CRACI)
- Recipe eval, 7 menus, run 43595658113: **5/7 pass, 7/7 schema-valid first answers**, 0 allergen-rule violations, tier 7/7; both misses are quantities. ~2,000 tokens, ~$0.007 per menu.
- Buyer eval, 48 sentences, run 43587725624 (11d8448): **46/48 matched without correction, 34/34 unsafe held or refused, 0 mandate violations auto-ordered**, $0.046/request.

**What's new here.** The LLM reads; code decides. Every number after the reading is made by code; the evals measure reading and safety.

**Why Gemini.** The Decisions API's calibrated yes/no lets code apply a fixed threshold, not parse text. Structured output honoured on every eval call (7/7 menus) in Finnish and English; 46/48 buyer sentences at $0.046 per request.

Code: https://github.com/Zawaer/sous · [docs/GEMINI.md](../GEMINI.md) · [evals/recipe_results.md](../../evals/recipe_results.md)
