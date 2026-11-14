# Recipe planning eval

Menu requests from `evals/recipe_cases.yaml` through `agent/recipe.py` (`scripts/recipe_eval.py`). The model (or the curated table) only interprets the request; quantities, product choice constraints and the mandate tier are code and the gateway.

A case passes when the dishes, portions, date and stock were understood, every main ingredient is in the plan, its quantity is within 35 % of the reference recipe, no quoted line breaks the mandate's allergen rules and the tier is as expected. *Within 20 %* counts main-ingredient quantities close to the reference.

| provider | mode | passed | quantities within 20 % | structured output | Valio calls | LLM calls | s per case |
|---|---|---|---|---|---|---|---|
| table | full (quote) | 7/7 | 23/23 | 0/0 | 27 | 0 | 19.9 |
| gemini:gemini-4-hydrogen | full (quote) | 5/7 | 16/23 | 7/7 | 12 | 7 | 20.3 |
| verda:kimi-k3-1-pro | full (quote) | 5/7 | 12/23 | 7/7 | 27 | 7 | 22.4 |

## table

| case | pass | interpreted by | ingredients | quantities | tier | total € | notes |
|---|---|---|---|---|---|---|---|
| R01 | yes | table | 5/5 | 5/5 | ask | 235.55 |  |
| R02 | yes | table | 4/4 | 4/4 | ask | 410.22 |  |
| R03 | yes | table | 2/2 | 2/2 | ask | 208.89 |  |
| R04 | yes | table | 3/3 | 3/3 | block | 204.57 |  |
| R05 | yes | table | 3/3 | 3/3 | ask | 92.83 |  |
| R06 | yes | table | 3/3 | 3/3 | block | 117.52 |  |
| R07 | yes | table | 3/3 | 3/3 | ask | 587.42 |  |

## gemini:gemini-4-hydrogen

| case | pass | interpreted by | ingredients | quantities | tier | total € | notes |
|---|---|---|---|---|---|---|---|
| R01 | yes | llm:gemini:gemini-4-hydrogen | 5/5 | 3/5 | ask | 241.59 | lasagnelevy: 6.00 KG vs reference 4.8 (+25%); tomaattimurska: 12.00 KG vs reference 9.6 (+25%) |
| R02 | yes | llm:gemini:gemini-4-hydrogen | 4/4 | 3/4 | ask | 397.62 | peruna: 9.60 KG vs reference 12 (-20%) |
| R03 | yes | llm:gemini:gemini-4-hydrogen | 2/2 | 2/2 | ask | 200.57 |  |
| R04 | no | llm:gemini:gemini-4-hydrogen | 2/3 | 1/3 | block | 236.93 | peruna: 24.00 KG vs reference 16 (+50%); lanttu: missing from the plan |
| R05 | no | llm:gemini:gemini-4-hydrogen | 3/3 | 2/3 | ask | 101.06 | peruna: 3.75 KG vs reference 6 (-38%) |
| R06 | yes | llm:gemini:gemini-4-hydrogen | 3/3 | 2/3 | block | 121.36 | tomaattimurska: 4.00 KG vs reference 3.2 (+25%) |
| R07 | yes | llm:gemini:gemini-4-hydrogen | 3/3 | 3/3 | ask | 562.62 |  |

## verda:kimi-k3-1-pro

| case | pass | interpreted by | ingredients | quantities | tier | total € | notes |
|---|---|---|---|---|---|---|---|
| R01 | yes | llm:verda:kimi-k3-1-pro | 5/5 | 1/5 | ask | 201.86 | lasagnelevy: 6.00 KG vs reference 4.8 (+25%); tomaattimurska: 12.00 KG vs reference 9.6 (+25%); maito: 9.60 L vs reference 14.4 (-33%) |
| R02 | no | llm:verda:kimi-k3-1-pro | 4/4 | 1/4 | ask | 407.11 | peruna: 9.60 KG vs reference 12 (-20%); kuohukerma: 3.20 L vs reference 4.8 (-33%); joulukinkku: 20.00 KG vs reference 12 (+67%) |
| R03 | yes | llm:verda:kimi-k3-1-pro | 2/2 | 2/2 | ask | 207.07 |  |
| R04 | no | llm:verda:kimi-k3-1-pro | 3/3 | 1/3 | block | 181.19 | porkkana: 8.00 KG vs reference 16 (-50%); lanttu: 6.00 KG vs reference 10 (-40%) |
| R05 | yes | llm:verda:kimi-k3-1-pro | 3/3 | 3/3 | ask | 107.55 |  |
| R06 | yes | llm:verda:kimi-k3-1-pro | 3/3 | 2/3 | block | 177.96 | jauheliha: 2.80 KG vs reference 3.6 (-22%) |
| R07 | yes | llm:verda:kimi-k3-1-pro | 3/3 | 2/3 | ask | 583.38 | porkkana: 9.00 KG vs reference 12 (-25%) |
