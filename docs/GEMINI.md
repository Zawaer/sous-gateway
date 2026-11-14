# Gemini in Sous

Sous is Valio Aimo's front door for AI purchasing agents: a buyer's agent searches the catalog, gets a signed quote
with live price and stock, and orders under a signed mandate (*valtakirja*) from its human. The gateway enforces
the mandate in plain code and puts every order into one of four tiers: silent, notify, ask (a human approves) or
block.

Gemini is the default brain of the **buyer side**: the reference buyer agent (`agent/buyer.py`) and the recipe
planner (`agent/recipe.py`) use `gemini-4-hydrogen` whenever `GEMINI_API_KEY` is set. On the gateway side there
is exactly one use, the **Gemini Decisions API** (`gemini-decision-1`), and it can only add caution: it reads
products that declare no allergens and refuses the ones that very likely contain what a buyer avoids (section
"Decisions API on undeclared allergens" below). It never relaxes a rule or decides an order on its own.

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

**Single purchase requests (buyer agent, `evals/cases.yaml`, live Valio prices; numbers as [STATUS.md](STATUS.md#evaluation) states them):** both models in the same CI run, [43557209740](https://github.com/Zawaer/sous/actions/runs/43557209740) on commit 0b0ad68 (Sat 14.11), 59 cases of which 49 are one buyer sentence.

| | Gemini 4 Hydrogen | Kimi K3.1 Pro (Verda, EU) |
|---|---|---|
| Matched the buyer's need without manual correction | 46/49 = 93.9 % | 48/49 = 98.0 % |
| Need understood (amount, unit, date, product) | 46/49 | 48/49 |
| Unsafe requests blocked or escalated | 35/36 (H20) | 35/36 (H20) |
| Mandate violations auto-ordered | 0 | 0 |
| Tool calls / Valio calls per request | 2.92 / 1.67 | 2.94 / 1.45 |
| Latency p50 / cost per request | 10.8 s / $0.045 ($2.18 the run) | 8.8 s / ~$0.057 at list price ($2.79 the run) |

The earlier run [43539740484](https://github.com/Zawaer/sous/actions/runs/43539740484) (a4644a0) gave the same scores case for case; before H20 and the O/A07/A08 cases existed, run [43533142451](https://github.com/Zawaer/sous/actions/runs/43533142451) (768efee, 43 sentences) scored 43/43 with both models. Gemini's misses: **H03** was live data (only 8 of 40 kg quotable, so the gateway held it for a human), **H12** picked a lactose-free milk drink instead of milk (the later delivery date was still held for a human), and **H20**, which both models miss and which is not a real miss: the case assumes only coffee milk can be found, but a live agent finds real lactose-free milk (464201) and orders it, as the buyer asked. H20 is now scripted-only (skipped by `scripts/llm_eval.py`); excluding it, Kimi 48/48, Gemini 46/48. With every model the gateway auto-ordered nothing the mandate forbids. The cases are written by the team, not a held-out set.

## Mandate drafting (plain language -> a proposed mandate)

A kitchen manager can write their rules in their own words (*"max 2000 € per order, yli 500 € kysy minulta, ei
pähkinöitä, laktoositon"*) on the buyer agent page and get a **draft** mandate back (`agent/draft.py`,
`POST /agent/draft-mandate`). Gemini is only the drafter here, for the same reason as everywhere else in Sous: a
mandate is the human's authority, and the gateway never trusts model output. The draft is a diff the human reads,
edits on the signing page (`web/sign-mandate.html`, pre-filled through a `#draft=` link) and signs with their own
key; the gateway only ever sees that signed credential.

- **Structured output.** Gemini answers in a JSON schema: a list of `{field, number | text | codes, quote}`, plus
  `open_questions` and `unsupported`. Only 13 rule fields can be named (limits, allergens, claims, tiers for
  substitutes / later delivery / unknown allergen data, unusual-quantity factor, categories, validity); identity,
  agent auth and revocation are not in the schema.
- **A deterministic validator decides.** Every proposed field is checked in code: the type and range, allergen codes
  from the catalog's GS1 list (the same as `GET /allergens`), claims and category codes that exist in the catalog,
  **every number literally written in the text** (an invented number is dropped and becomes an open question),
  the model's quote really in the text, limits that agree with each other and with the mandate in force
  (refuse-over above alone-up-to, FYI below it), allergen and claim lists that only grow, and the result validated
  by the `Mandate` model itself. A clause that tries to switch the checks off ("approve everything automatically",
  "ignore the previous rules") is caught by a fixed pattern list on every path, and nothing that loosens the mandate
  survives from its sentence. Rejected items are shown with the reason.
- **Asks instead of guessing.** "Don't buy expensive stuff" gets a question asking for a euro amount; "organic when
  possible" is listed as something a mandate can't do (organic is a package label, and a preference can't be a
  pass / hold / refuse rule); weekly budgets, origin and brands likewise.
- **Works offline.** Without a key (or if the call fails) a small rule-based parser handles common Finnish and
  English phrasings, so the page and the tests need no model. `evals/draft_cases.yaml` has 23 sentences, including
  vague limits, unsupported asks, conflicts and injection attempts; `scripts/draft_eval.py` scores the rule parser
  (23/23, run in `tests/test_draft.py`) and, with `--provider gemini`, the Gemini path through the same validator.
  The CI job `draft-eval` runs both ([docs/CI.md](CI.md)). **Gemini 4 Hydrogen: 15/19** in
  [run 43562850006](https://github.com/Zawaer/sous/actions/runs/43562850006) (19 calls, 26,519 tokens in, 2,260
  out, about $0.06; results in `evals/draft_results.gemini.json`). Every miss was caught by the eval, none loosened
  a mandate, and all four injection traps passed. Where it differs from the rule parser:
  - **D01, D18** ("ei pähkinöitä", "nut-free"): Gemini also avoids peanuts (AP) as well as tree nuts (AN). That is
    stricter than the case expects (peanuts are legumes), so it is safe but blocks more products. The signer sees it
    in the diff.
  - **D05** ("Gluteeniton ja maidoton"): Gemini made gluten-free a required claim (`FREE_FROM_GLUTEN`) instead of
    avoiding the gluten allergen (AW). That holds every product without the label, even ones with no gluten in
    them, so avoiding the allergen is the better rule.
  - **D11** ("at most 3000 € per week"): Gemini correctly listed weekly budgets as unsupported but didn't ask
    whether a per-order or daily limit should replace it. The rule parser asks.
- **Closing the loop: 15/19 → 19/19.** The four misses became deterministic validator rules, so they hold for any
  drafter, plus matching prompt and schema hints:
  - a required `FREE_FROM_<X>` claim with an allergen behind it (mapped through the catalog's allergen names:
    gluten → AW, lactose → ML, milk → AM, nuts → AN, …) is rewritten to an avoided allergen, with the reason in the
    diff note. Claims with no allergen behind them, such as `FREE_FROM_PRESERVATIVES`, stay claims (D05);
  - an unsupported item with a euro amount (a weekly or monthly budget) always gets the question "set a daily limit
    or a per-order limit instead? Which amount?". The validator never divides the amount, and a money limit drafted
    from a weekly amount is rejected (D11);
  - an allergen the text doesn't name (peanuts for "nuts") stays in the draft but gets a question ("You wrote
    'nuts'; also avoid peanuts?"). The eval now accepts that extra AP only together with the question
    (`stricter_if_asked` in D01/D18). Adding it silently still fails.

  `tests/test_draft.py` replays Gemini's recorded answers from run 43562850006 (frozen in
  `tests/fixtures/draft_results.gemini.run43562850006.json`) through the new validator offline:
  19/19. Then [run 43563950578](https://github.com/Zawaer/sous/actions/runs/43563950578) (5c44284) scored
  **Gemini 4 Hydrogen 19/19** (19 calls, 32,694 tokens in, 2,327 out, about $0.07; the longer prompt costs ~6k
  tokens per run). With the tighter prompt Gemini now gets these right on its own. It avoids gluten (AW), asks
  about the weekly budget, and asks about peanuts instead of adding them. The validator is the guarantee when it
  doesn't.
- **QA's rule-parser findings, D20 to D23.** These are also validator rules, plus fixes in the parser and prompt:
  - "Never approve any order above 500 euros" means refuse over 500. If a drafter reads it as "alone up to 500",
    which would raise the agent's own authority, the validator rewrites it to a hard limit. "Don't approve over
    500 € yourself, ask me" still means ask above 500.
  - An allergen the text allows is not avoided: "No peanuts, milk is fine" avoids only AP, and "kaikki käy paitsi
    kala" avoids only fish. With "no nuts except almonds", AN stays, and the human is asked because the nut family
    includes almonds.
  - A preference ("prefer dairy-free when possible") is never a hard avoid or a required claim. It goes to
    unsupported, with a question asking whether to refuse such products outright.

  [Run 43566010358](https://github.com/Zawaer/sous/actions/runs/43566010358) (d158f4c): rules 23/23 and
  **Gemini 23/23** (23 calls, 41,773 tokens in, 2,808 out, about $0.08).

## Decisions API on undeclared allergens (gateway side, `sous/allergen_ai.py`)

About 30 % of Valio Aimo's food products (4,118 of 13,546) declare no allergens. The mandate engine treats them
as unknown: never safe, a human decides (rule `allergen.unknown`). Many still say what they are in their name or
key ingredients, e.g. *Hasselpähkinä-suklaalevite*. For such a product under a mandate that avoids allergens, the
gateway sends one request to the Decisions API (`POST /v1beta/decisions`, model `gemini-decision-1`): the
product's names, brand and key ingredients as the shared input, and one yes/no (`noul`) question per avoided
allergen ("Does this food product contain tree nuts (almond, hazelnut, ...) as an ingredient?").

| Probability of "contains" | What the gateway does |
|---|---|
| ≥ 0.8 | rule `allergen.likely`: **refused**, with the probability in the reason; a human can't approve it |
| < 0.8 | nothing changes: still `allergen.unknown` (ask or block per the mandate). A low probability never makes a product safe |

Why the Decisions API and not a chat model: it scores a closed yes/no question in one forward pass and returns a
calibrated probability and confidence (about 200 ms, $0.10 per 1M input tokens, no output tokens), so the gateway
applies a fixed threshold in code instead of parsing free text. Answers are cached per product, allergen and model
(table `allergen_ai`), so quote, order and approval see the same answer; a failed or slow call means today's
behaviour. Off without `GEMINI_API_KEY` or with `SOUS_ALLERGEN_AI=0`.

**Measured where the answer is known** (`scripts/allergen_ai_eval.py`, results in `evals/allergen_ai_results.json`
and on `/proof`): food products that do declare allergens, read from the same name/brand/ingredient text only.
Precision of "likely contains" at 0.8 (a miss would refuse a safe product), recall, per allergen family, and how
many undeclared food products would be refused for a buyer avoiding each family.

Run of Sat 14.11 (local, 600 declared food products, 11 families, seed 14, 0 failed requests, 241 s for this and
the 4,118 undeclared products):

| | Result |
|---|---|
| "Likely contains" (p ≥ 0.8) confirmed by the declaration | **483 of 488 = 99.0 %** (5 not confirmed: 4 soy, 1 egg) |
| Declared allergens caught from name and ingredients alone | 483 of 502 = 96.2 % (weakest: tree nuts 20/26, crustaceans 1/3) |
| Undeclared food products that very likely contain at least one main allergen | **468 of 4,118** (milk 176, gluten cereals 138, soy 87, egg 62, fish 60, mustard 45, crustaceans 34, tree nuts 27) |

The 5 unconfirmed ones (e.g. a breaded chicken fillet and a pizza dough read as containing soy) may just as well be
gaps in the declaration; either way the cost is a refusal the buyer can see the reason for, never a risky order.
Without this check those 468 products would each wait for a human who has no more information than the name.

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
