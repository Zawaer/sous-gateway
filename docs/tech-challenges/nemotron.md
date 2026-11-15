# Best Use of Nemotron

**Model.** NVIDIA **Nemotron 3.5 Super VL** in two places, same code: the **Nemotron endpoint hosted on Verda with NVIDIA** (preset `verda-nemotron`, preferred) and NVIDIA's API (`integrate.api.nvidia.com`, preset `nvidia`, fallback). See `agent/llm.py`.

**What it does.** "Snap a photo of the order." Kitchens work on paper: order lists, menus, delivery notes. Nemotron reads the photo into strict JSON (page kind, lines as written, quantity + unit, confidence, dishes, date). Code makes **one signed basket quote**: order list -> catalog search; menu -> recipe planner; delivery note -> only the shortfall. The mandate engine decides. No Nemotron, no order (`agent/vision.py`).

**Why it matters.** The failure that matters is reading "12 kg" as "72 kg" and ordering it. The model only transcribes; a line below 0.75 confidence, unreadable or failing the code cross-check goes to a human, never to the basket.

**Technical execution.** Schema + pydantic + one repair retry; `+verify`: code compares each line's number/unit with the model's own transcription, mismatches get ONE targeted second look, survivors capped at 0.5 -> human; a page-level shortfall check; reasoning kept out of the answer. 17 offline tests.

**Quality, improved via eval loops.** 8 pages we rendered (fi/sv/en) with traps (crossed-out line, smudged line), read 3-4 times per run, 6 runs. Safety metric: **silent errors** (wrong lines that would be auto-ordered).
- v1 baseline prompt: 16 silent errors, 76 % auto-correct, traps 0/6.
- v3 prompt from Nemotron's own errors + code checks + second look (NVIDIA API, 32 reads): **0 silent errors, 100 %, 4.3 s/page**; reasoning on: same, 20.7 s.
- **On Verda** (32 + 8 reads): reasoning off is fastest (1.2 s/page) but made **6 silent errors** (a crossed-out line, a red-pen correction). Reasoning on: **0 silent errors, 100 %, 5.5 s/page**, 4x faster than on the free tier, so that is the default there.
- Also tried as a text-only buyer agent (not its role here): 30/48 matched, 0 mandate violations ordered.

**Creativity.** Code checks the model against its own transcription; it asks again only where they disagree. The eval decided reasoning on/off per deployment.

Limits: rendered pages, no held-out real photos; reasoning-on sample on Verda is 8 reads.

Code: https://github.com/Zawaer/sous-gateway · [docs/NEMOTRON.md](../NEMOTRON.md) · [evals/vision/history/](../../evals/vision/history/)
