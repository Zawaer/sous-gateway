# Recipe planning eval

Menu requests from `evals/recipe_cases.yaml` through `agent/recipe.py` (`scripts/recipe_eval.py`). The model (or the curated table) only interprets the request; quantities, product choice constraints and the mandate tier are code and the gateway.

A case passes when the dishes, portions, date and stock were understood, every main ingredient is in the plan, its quantity is within 35 % of the reference recipe, no quoted line breaks the mandate's allergen rules and the tier is as expected. *Within 20 %* counts main-ingredient quantities close to the reference.

| provider | mode | passed | quantities within 20 % | structured output | Valio calls | LLM calls | s per case |
|---|---|---|---|---|---|---|---|
| table | full (quote) | 6/7 | 20/20 | 0/0 | 19 | 0 | 19.6 |
| gemini:gemini-4-hydrogen | full (quote) | 5/7 | 15/20 | 7/7 | 17 | 7 | 11.2 |
| verda:kimi-k3-1-pro | full (quote) | 4/7 | 10/20 | 7/7 | 18 | 7 | 27.5 |

## table

| case | pass | interpreted by | ingredients | quantities | tier | total € | notes |
|---|---|---|---|---|---|---|---|
| R01 | yes | table | 5/5 | 5/5 | ask | 235.55 |  |
| R02 | yes | table | 4/4 | 4/4 | ask | 410.22 |  |
| R03 | yes | table | 2/2 | 2/2 | ask | 208.89 |  |
| R04 | yes | table | 3/3 | 3/3 | block | 204.57 |  |
| R05 | no | table | - | - | - | - | quote: TypeError in request_quote. |
| R06 | yes | table | 3/3 | 3/3 | block | 117.52 |  |
| R07 | yes | table | 3/3 | 3/3 | ask | 587.42 |  |

## gemini:gemini-4-hydrogen

| case | pass | interpreted by | ingredients | quantities | tier | total € | notes |
|---|---|---|---|---|---|---|---|
| R01 | yes | llm:gemini:gemini-4-hydrogen | 5/5 | 5/5 | ask | 231.64 |  |
| R02 | yes | llm:gemini:gemini-4-hydrogen | 4/4 | 2/4 | ask | 395.58 | peruna: 9.60 KG vs reference 12 (-20%); joulukinkku: 16.00 KG vs reference 12 (+33%) |
| R03 | yes | llm:gemini:gemini-4-hydrogen | 2/2 | 2/2 | ask | 204.44 |  |
| R04 | no | llm:gemini:gemini-4-hydrogen | 2/3 | 1/3 | block | 207.41 | peruna: 20.00 KG vs reference 16 (+25%); lanttu: missing from the plan |
| R05 | no | llm:gemini:gemini-4-hydrogen | - | - | - | - | peruna: 4.50 KG vs reference 6 (-25%); quote: TypeError in request_quote. |
| R06 | yes | llm:gemini:gemini-4-hydrogen | 3/3 | 3/3 | block | 117.52 |  |
| R07 | yes | llm:gemini:gemini-4-hydrogen | 3/3 | 2/3 | ask | 551.44 | porkkana: 9.00 KG vs reference 12 (-25%) |

## verda:kimi-k3-1-pro

| case | pass | interpreted by | ingredients | quantities | tier | total € | notes |
|---|---|---|---|---|---|---|---|
| R01 | no | llm:verda:kimi-k3-1-pro | 5/5 | 2/5 | ask | 193.6 | lasagnelevy: 6.00 KG vs reference 4.8 (+25%); maito: 6.00 L vs reference 14.4 (-58%); juustoraaste: 1.80 KG vs reference 3 (-40%) |
| R02 | yes | llm:verda:kimi-k3-1-pro | 4/4 | 2/4 | ask | 410.88 | peruna: 9.60 KG vs reference 12 (-20%); joulukinkku: 16.00 KG vs reference 12 (+33%) |
| R03 | yes | llm:verda:kimi-k3-1-pro | 2/2 | 2/2 | ask | 214.57 |  |
| R04 | no | llm:verda:kimi-k3-1-pro | 3/3 | 1/3 | block | 151.61 | porkkana: 6.00 KG vs reference 16 (-62%); lanttu: 4.00 KG vs reference 10 (-60%) |
| R05 | no | llm:verda:kimi-k3-1-pro | - | - | - | - | quote: TypeError in request_quote. |
| R06 | yes | llm:verda:kimi-k3-1-pro | 3/3 | 1/3 | block | 183.67 | lasagnelevy: 2.00 KG vs reference 1.6 (+25%); tomaattimurska: 4.00 KG vs reference 3.2 (+25%) |
| R07 | yes | llm:verda:kimi-k3-1-pro | 3/3 | 2/3 | ask | 558.47 | porkkana: 9.00 KG vs reference 12 (-25%) |
