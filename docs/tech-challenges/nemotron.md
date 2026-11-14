# Best Use of Nemotron

**Model.** NVIDIA **Nemotron 3.5 Super VL** (`nvidia/nemotron-3.5-super-vl-120b-a12b`) via NVIDIA's OpenAI-compatible API (`integrate.api.nvidia.com`, preset `nvidia` in `agent/llm.py`). Reasoning off by default.

**What it does (meaningful use).** "Snap a photo of the order." Kitchens work on paper: order lists, menus, delivery notes. Nemotron reads the photo into strict JSON (page kind, lines as written, quantity + unit, confidence per line, dishes, date). Code then makes **one signed basket quote**: order list -> catalog search; menu -> recipe planner; delivery note -> only the shortfall. The mandate engine decides. Without Nemotron there is no order (`agent/vision.py`).

**Why it matters.** The failure that matters is reading "12 kg" as "72 kg" and ordering it. The model only transcribes; a line below 0.75 confidence, unreadable or failing the code cross-check is never ordered but listed for a human.

**Technical execution.** Schema + pydantic validation + one repair retry (valid JSON on 239 of 240 page reads); `+verify`: code compares each line's number/unit with the model's own transcription, mismatches get ONE targeted second look, survivors are capped at 0.5 -> human; a page-level shortfall check; retries with `Retry-After`. 16 offline tests.

**Quality of output, improved via eval loops.** 8 pages we rendered ourselves (fi/sv/en) with traps (crossed-out line, smudged line), each read 3-4 times per run, 5 CI runs. Safety metric: **silent errors** (wrong lines that would be auto-ordered).
- v1 baseline prompt (run 3, 24 reads): 16 silent errors, 76 % of lines auto-ordered correctly, traps 0/6.
- v3 prompt, written from Nemotron's own run-1 errors (run 4): 0 silent errors, 100 %.
- v3 + code checks + second look, the default (run 5, CI 43530170621, 32 reads): **0 silent errors, 100 % auto-correct, traps 8/8, 4.3 s/page**. Earlier versions had 1 silent error (runs 2, 3), which led to the page check and the second look.
- Reasoning on: same accuracy, 20.7 s/page; off.
- As buyer agent (CI 43587725624): 30/48, 0 mandate violations ordered.

**Creativity.** Code checks the model against its own transcription; it asks again only where they disagree.

Limits: rendered pages, not real kitchen photos; no held-out set. CLI today; the photo upload in `/agent/` (08371ec) is off on the servers.

Code: https://github.com/Zawaer/sous · [docs/NEMOTRON.md](../NEMOTRON.md) · [evals/vision/history/](../../evals/vision/history/)
