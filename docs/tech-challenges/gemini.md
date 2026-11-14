# Best Use of Gemini API

**Problem.** Sous lets AI agents order from Valio Aimo under a mandate signed by their human. Kitchens don't order SKUs: they say *"Lasagne for 120 on Monday, we have 10 kg flour"* or *"Karjalanpaisti 60 annosta perjantaiksi"*. Something has to turn that into a request a gateway can price and check.

**Integration.** `gemini-4-hydrogen` through Gemini's OpenAI-compatible endpoint is the default model of the buyer side:
1. **Menu -> one order (`agent/recipe.py`).** Gemini returns dishes, a per-portion recipe, portions, date and stock on hand with **structured output** (`response_format: json_schema`, `MENU_SCHEMA`), validated with pydantic, one retry with the error. Code scales, subtracts stock and searches under the mandate's allergen rules; the gateway returns **one signed basket quote**.
2. **Single requests (`agent/buyer.py`).** Gemini drives the gateway's MCP tools (search, quote, substitutes, order) and explains the decision.

Gemini never picks the product a line is bought from or the tier; the runtime binds agent, mandate and idempotency key. A misread costs a wrong quantity on a quote a human can read, never an order the mandate forbids.

**Results** (our own cases, live Valio prices, CI on CRACI)
- Recipe eval, 7 menus (fi/en, 4 mandates), run 43524836008: **7/7 schema-valid first answers**, 8/8 dishes, 22/23 main ingredients, 0 allergen-rule violations on quoted lines, tier 7/7; only 4/7 pass the 35 % quantity bar against our reference recipes. ~2,000 tokens, ~$0.006, 10.1 s per menu.
- Buyer eval, 40 sentences: **39/40 matched without correction, 0 mandate violations** (run 43530717095, $0.037/request), up from 36/40 in run 43524836008 after the eval loop fixed the gateway and three ambiguous cases. Kimi K3.1 Pro scored 34/40 in that earlier run; it has not been re-run on current code.

**What's new here.** The LLM reads; code decides. Each menu becomes one auditable quote with every number after the reading made by code, and the eval measures both reading and safety.

**Why Gemini.** Structured output honoured on every eval call (7/7 menus) in Finnish and English, and the best understanding score of the models we ran, at $0.037 per request.

Not done: on the servers it is CLI and CI only. The web page `/agent/` (08371ec) exists but is off until the servers get a model key.

Code: https://github.com/Zawaer/sous · [docs/GEMINI.md](../GEMINI.md) · [evals/recipe_results.md](../../evals/recipe_results.md)
