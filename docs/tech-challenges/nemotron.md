# Best Use of Nemotron

**Model.** NVIDIA **Nemotron 3.5 Super VL** (`nvidia/nemotron-3.5-super-vl-120b-a12b`) via NVIDIA's OpenAI-compatible API (`integrate.api.nvidia.com`, preset `nvidia` in `agent/llm.py`). Reasoning off by default (reading is perception).

**What it does: "snap a photo of the order".** Kitchens work on paper: a handwritten order list, a printed menu, a delivery note with a shortfall. The cook photographs it; Nemotron reads it into strict JSON (page kind, lines as written, quantity + unit, confidence per line, dishes, date). Then code takes over: order list -> catalog search -> **one signed basket quote**; menu -> recipe planner -> one quote; delivery note -> ordered minus delivered -> a quote for the shortfall only. The mandate engine decides: ordered, held for a human, or refused. Without Nemotron there is no order (`agent/vision.py`).

**Why it matters.** Trusted agents fail when a model reads "12 kg" as "72 kg" and orders it. So the model only transcribes; a line below 0.75 confidence, unreadable or failing the code cross-check is never ordered but listed for a human. It reads Finnish, Swedish and English handwriting, "2,5 l" and kitchen shorthand (*ltk*, *plo*, *pkt*, *kpl*).

**How we improved its outputs**
1. Strict schema + pydantic validation + one repair retry with the error.
2. Prompt v1 -> v2 -> v3: transcribe, never fix; today's date; fi/sv/en unit table; skip crossed-out lines; delivery-note columns; what confidence means. v3 came from Nemotron's run-1 errors.
3. `+verify`: code compares each line's number/unit with the model's own transcription; mismatches get ONE targeted second look; surviving mismatches are capped at 0.5 -> human.
4. Eval: 8 rendered pages with traps (crossed-out line, smudged line), scored on **silent errors** (wrong lines that would be auto-ordered).

**Results (run 2, CI, 8 pages / 31 lines):** v1 baseline: 8 silent errors, traps 0/2, qty+unit exact 84 %, delivered 33 %. **v3: 0 silent errors, traps 2/2, qty+unit 100 %, delivered 100 %, portions 100 %, 4.1 s/page.** v3+verify left 1 silent error (a loose "puuttuu" note on a photographed delivery note), now caught by a page-level check (e0988ad).

[PENDING: run 3 (3 repeats, CI 43528096128) as final numbers; a demo surface (photo upload on /buyer/), today CLI only: `python -m agent.vision --provider nvidia <photo>`.]

Code: https://github.com/Zawaer/sous · [docs/NEMOTRON.md](../NEMOTRON.md) · [evals/vision/history/](../../evals/vision/history/)
