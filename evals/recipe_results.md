# Recipe planning eval

Menu requests from `evals/recipe_cases.yaml` through `agent/recipe.py` (`scripts/recipe_eval.py`). The model (or the curated table) only interprets the request; quantities, product choice constraints and the mandate tier are code and the gateway.

A case passes when the dishes, portions, date and stock were understood, every main ingredient is in the plan, its quantity is within 35 % of the reference recipe, no quoted line breaks the mandate's allergen rules and the tier is as expected. *Within 20 %* counts main-ingredient quantities close to the reference.

| provider | mode | passed | quantities within 20 % | structured output | Valio calls | LLM calls | s per case |
|---|---|---|---|---|---|---|---|
| table | full (quote) | 7/7 | 23/23 | 0/0 | 24 | 0 | 1.2 |
| gemini:gemini-4-hydrogen | full (quote) | 4/7 | 13/23 | 7/7 | 13 | 7 | 10.1 |
| verda:kimi-k3-1-pro | full (quote) | 4/7 | 9/23 | 7/7 | 23 | 7 | 10.6 |

## table

| case | pass | interpreted by | ingredients | quantities | tier | total € | notes |
|---|---|---|---|---|---|---|---|
| R01 | yes | table | 5/5 | 5/5 | silent | 236.69 |  |
| R02 | yes | table | 4/4 | 4/4 | ask | 410.22 |  |
| R03 | yes | table | 2/2 | 2/2 | notify | 269.69 |  |
| R04 | yes | table | 3/3 | 3/3 | ask | 204.57 |  |
| R05 | yes | table | 3/3 | 3/3 | ask | 92.83 |  |
| R06 | yes | table | 3/3 | 3/3 | ask | 115.85 |  |
| R07 | yes | table | 3/3 | 3/3 | ask | 587.42 |  |

## gemini:gemini-4-hydrogen

| case | pass | interpreted by | ingredients | quantities | tier | total € | notes |
|---|---|---|---|---|---|---|---|
| R01 | yes | llm:gemini:gemini-4-hydrogen | 5/5 | 3/5 | silent | 246.02 | lasagnelevy: 6.00 KG vs reference 4.8 (+25%); tomaattimurska: 12.00 KG vs reference 9.6 (+25%) |
| R02 | no | llm:gemini:gemini-4-hydrogen | 4/4 | 2/4 | ask | 397.62 | peruna: 9.60 KG vs reference 12 (-20%); joulukinkku: 20.00 KG vs reference 12 (+67%) |
| R03 | yes | llm:gemini:gemini-4-hydrogen | 2/2 | 2/2 | notify | 265.12 |  |
| R04 | no | llm:gemini:gemini-4-hydrogen | 2/3 | 1/3 | ask | 229.02 | peruna: 20.00 KG vs reference 16 (+25%); lanttu: missing from the plan |
| R05 | yes | llm:gemini:gemini-4-hydrogen | 3/3 | 2/3 | ask | 104.81 | peruna: 4.50 KG vs reference 6 (-25%) |
| R06 | no | llm:gemini:gemini-4-hydrogen | 3/3 | 1/3 | ask | 119.69 | lasagnelevy: 2.40 KG vs reference 1.6 (+50%); tomaattimurska: 4.00 KG vs reference 3.2 (+25%) |
| R07 | yes | llm:gemini:gemini-4-hydrogen | 3/3 | 2/3 | ask | 559.35 | porkkana: 9.00 KG vs reference 12 (-25%) |

## verda:kimi-k3-1-pro

| case | pass | interpreted by | ingredients | quantities | tier | total € | notes |
|---|---|---|---|---|---|---|---|
| R01 | yes | llm:verda:kimi-k3-1-pro | 5/5 | 1/5 | ask | 203.0 | lasagnelevy: 6.00 KG vs reference 4.8 (+25%); tomaattimurska: 12.00 KG vs reference 9.6 (+25%); maito: 9.60 L vs reference 14.4 (-33%) |
| R02 | no | llm:verda:kimi-k3-1-pro | 4/4 | 1/4 | ask | 571.12 | peruna: 9.60 KG vs reference 12 (-20%); kuohukerma: 3.20 L vs reference 4.8 (-33%); joulukinkku: 24.00 KG vs reference 12 (+100%) |
| R03 | yes | llm:verda:kimi-k3-1-pro | 2/2 | 1/2 | notify | 303.66 | sianliha: 4.50 KG vs reference 6 (-25%) |
| R04 | no | llm:verda:kimi-k3-1-pro | 3/3 | 0/3 | ask | 189.55 | porkkana: 10.00 KG vs reference 16 (-38%); peruna: 20.00 KG vs reference 16 (+25%); lanttu: 6.00 KG vs reference 10 (-40%) |
| R05 | yes | llm:verda:kimi-k3-1-pro | 3/3 | 2/3 | ask | 101.06 | peruna: 4.50 KG vs reference 6 (-25%) |
| R06 | yes | llm:verda:kimi-k3-1-pro | 3/3 | 2/3 | ask | 179.1 | tomaattimurska: 4.00 KG vs reference 3.2 (+25%) |
| R07 | no | llm:verda:kimi-k3-1-pro | 3/3 | 2/3 | ask | 567.51 | porkkana: 7.50 KG vs reference 12 (-38%) |
