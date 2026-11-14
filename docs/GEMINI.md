# Gemini in Sous

Sous is Valio Aimo's front door for AI purchasing agents: a buyer's agent searches the catalog, gets a signed quote
with live price and stock, and orders under a signed mandate (*valtakirja*) from its human. The gateway enforces
the mandate in plain code and puts every order into one of four tiers: silent, notify, ask (a human approves) or
block.

Gemini is the default brain of the **buyer side**: the reference buyer agent (`agent/buyer.py`) and the recipe
planner (`agent/recipe.py`) use `gemini-4-hydrogen` whenever `GEMINI_API_KEY` is set. It is never on the gateway
side, and it never decides policy.

## What Gemini does

**1. Reads the chef's language.** Kitchens don't order SKUs; they say *"Lasagne for 120 on Monday, we have 10 kg
flour and plenty of salt"*, *"Joululounas 80 hengelle: lohikeitto ja joulukinkku"* or *"Karjalanpaisti 60
annosta perjantaiksi, meillä on 5 kg sipulia"*. Gemini turns that into a structured plan: the dishes, a standard
Finnish foodservice recipe per dish (ingredients with Finnish and English names, raw amount per portion in g / ml /
pcs, dietary notes), the number of portions, the delivery day and what is already on hand. Finnish, Swedish and
English all work, including Finnish case endings ("sipulia", "jauhoja") and partly-mixed sentences.

**2. Drives the gateway's tools for a single need.** For a direct request (*"1,000 litres of lactose-free milk on
Monday"*) the buyer agent gives Gemini the gateway's own MCP tools (search, quote, substitutes, order) and lets it
choose the search terms, candidate products, amount, unit and date, then explain the gateway's decision to the
buyer in plain language.

## What Gemini does not do

Every number after the interpretation is code, and every decision is the gateway's:

| Step | Who |
|---|---|
| Dishes, recipe, portions, date, stock on hand from the sentence | **Gemini** (schema-checked JSON) |
| Scaling by portions, g -> kg, ml -> L, pieces by weight, stock concentrate by weight, subtracting stock, rounding up | code (`agent/recipe.py`) |
| Which catalog products may be candidates (mandate allergens and claims, unit, name check, pack size) | code + gateway search |
| Pack sizes, cheapest orderable product, split on short stock, what is uncovered | gateway (`sous/quote.py`) |
| Substitutes (never relaxing allergen rules) | gateway (`sous/substitutes.py`) |
| Tier: silent / notify / ask / block | gateway mandate engine (`sous/mandate.py`) |

The model never sees prices when it plans a menu, never picks the product a line is bought from and cannot present
another mandate: the runtime binds the agent id, mandate and idempotency key to every call. A model that misreads
the chef costs a wrong quantity on a quote a human can read, never an order the mandate forbids.

## How the output was made reliable

1. **Structured output.** The recipe plan is requested with `response_format: {type: "json_schema"}` through
   Gemini's OpenAI-compatible endpoint (`MENU_SCHEMA` in `agent/recipe.py`: plain types and enums, no `$ref` or
   `anyOf`, which Gemini's schema subset rejects). Gemini honoured the schema on every eval call. The answer is
   still validated with pydantic; an invalid one gets one retry with the validation error, and a server that
   rejects the schema gets plain JSON prompting instead (recorded in the plan).
2. **Prompt that leaves the arithmetic out.** The model is asked only for per-portion amounts of raw ingredients
   (with portion-size anchors: a lunch soup ~350 ml, a main course ~300-350 g) and the plain Finnish wholesale word
   for each ingredient, with the lexicon's names offered as a vocabulary. Scaling to 120 portions, unit
   conversions and stock subtraction are code, so the model cannot get them wrong.
3. **Deterministic fallbacks.** Without a key, or if Gemini's answer is invalid twice, a curated recipe table
   (`agent/recipes.json`: lasagne, lohikeitto, joulukinkku, rosolli, karjalanpaisti, kasvissosekeitto) interprets
   the request, and the output says which path was used. Tests and the demo run offline.
4. **An eval loop in CI.** `scripts/recipe_eval.py` runs seven menu requests (`evals/recipe_cases.yaml`, Finnish
   and English, four different mandates incl. avoid-milk and avoid-gluten/egg) through the planner per provider and
   scores: dishes, portions, date and stock understood; every main ingredient in the plan; quantities against a
   reference recipe; no quoted line breaking the mandate's allergen rules; the tier. `scripts/llm_eval.py` does the
   same for single purchase requests (40 cases). Both run in CI's `llm-eval` job with the `GEMINI_API_KEY` secret
   (`gh workflow run ci.yml -f recipe_only=true` for the recipe eval alone). What the first run showed and what
   changed:
   - the eval hit the gateway's per-minute budget of live Valio calls; the planner now waits it out like the buyer
     agent does;
   - one undeliverable salt pack put a whole lunch basket in the block tier; an ingredient that can't be delivered
     and has no substitute is now left out and reported, so the human sees the gap and the rest can go ahead;
   - bulk packs (3 kg of pepper for 30 g) are left out of the candidates for small needs, and an over-sized pack the
     gateway still picks is re-quoted without it when that is cheaper;
   - the quantity check was split: a pass needs every main ingredient within 35 % of the reference recipe (50 g of
     lasagne sheets per portion is as right as 40 g; a misread of portions or units is off by far more), and
     *within 20 %* is reported as a score.

## Results

Recipe eval: one CI run (`llm-eval` job, run 43524836008, 14.11.2026 ~01:30 Helsinki, live Valio prices). Not re-run since. Full tables: [`evals/recipe_results.md`](../evals/recipe_results.md) and [LLM_EVAL.md](LLM_EVAL.md).

**Menu -> one order (recipe eval, 7 cases):**

| | Gemini 4 Hydrogen | Kimi K3.1 Pro (Verda, EU) | Recipe table (no model) |
|---|---|---|---|
| Schema-valid plan on the first answer (structured output) | 7/7 | 7/7 | – |
| Dishes understood (incl. two-dish menus, Finnish requests) | 8/8 | 8/8 | 8/8 |
| Portions / delivery day / stock on hand understood | 7/7 / 7/7 / 4/4 | 7/7 / 7/7 / 4/4 | 7/7 / 7/7 / 4/4 |
| Main ingredients in the plan | 22/23 | 23/23 | 23/23 |
| Main quantities within 20 % of the reference recipe | 13/23 | 9/23 | 23/23 |
| Within 35 % (the pass bar) | 4/7 cases | 4/7 cases | 7/7 |
| One signed basket quote; mandate allergen rules held on every line | 7/7; 7/7 | 7/7; 7/7 | 7/7; 7/7 |
| Tier as expected | 7/7 | 7/7 | 7/7 |
| Model calls / tokens per menu | 1 / ~2,000 | 1 / ~1,900 | 0 |
| Seconds per menu (model + search + quote) | 10.1 | 10.6 | 1.2 |
| Model cost per menu | ~$0.006 | – | $0 |

Gemini read every request (English and Finnish, one or two dishes, weekday names, "plenty of", "meillä on 5 kg
sipulia") into a valid plan on its first answer, and no quote broke a mandate's allergen rule. Where it differs
from the reference recipe it is a cook's judgment, not a misreading: 50 g of lasagne sheets per portion instead of
40 g, 250 g of raw bone-in ham for joulukinkku instead of 150 g cooked, a vegetable soup without swede. Those
differences are visible in the ingredient table before anything is ordered, and the quote's total and tier move
with them. The curated table is exact by construction (it *is* the reference), which is why it is the offline
fallback and not the default: it knows six dishes, Gemini knows the menu.

**Single purchase requests (buyer agent, 40 sentences from `evals/cases.yaml`, live Valio prices):**

| | Gemini 4 Hydrogen, CI 43524836008 (91ef7f5) | Gemini 4 Hydrogen, CI 43530717095 (e84bb41) | Kimi K3.1 Pro (Verda, EU), CI 43524836008 |
|---|---|---|---|
| Matched the buyer's need without manual correction | 36/40 = 90 % | **39/40 = 97.5 %** | 34/40 = 85 % |
| Need understood (amount, unit, date, product) | 38/40 | 39/40 | 36/40 |
| Mandate violations auto-ordered | 0 | 0 | 0 |
| Tool calls / Valio calls per request | 3.05 / 1.57 | 3.02 / 1.15 | 3.1 / 1.6 |
| Latency p50 / cost per request | 11.8 s / $0.037 | 9.8 s / $0.037 | 8.8 s / – |

Between the two Gemini runs the eval loop changed the gateway and the cases, not the model: the quote keeps every unorderable candidate as a line so refusals name the real rule (5eaed68), and three case sentences were made unambiguous (H09, P02, A04). Gemini's remaining miss (H03) was live data: Valio reported one coffee undeliverable at that moment. Kimi was not re-run on the new code (Verda Inference returned empty responses, then went offline), so the like-for-like comparison is the 43524836008 column pair: Gemini 36/40 vs Kimi 34/40. With every model the gateway auto-ordered nothing the mandate forbids. The cases are written by the team, not a held-out set.

## Run it

```sh
export GEMINI_API_KEY=...        # never committed; in CI a GitHub secret
uv run python -m agent.recipe --mandate M-HOTEL-AURORA --agent aurora-purchasing-agent \
    "Lasagne for 120 on Monday, we have 10 kg flour and plenty of salt"      # --order to place it
uv run python -m agent.buyer --mandate M-HOTEL-AURORA --agent aurora-purchasing-agent --gateway local \
    "We need 1,000 litres of lactose-free milk delivered on Monday."
uv run python scripts/recipe_eval.py --providers table gemini
uv run python scripts/llm_eval.py --provider gemini
```

Where it runs: Google's Gemini API (global). For EU-only inference the same code runs on Verda Inference
(`--provider verda`, see [VERDA.md](VERDA.md)) or a self-hosted model (`--provider local`).
