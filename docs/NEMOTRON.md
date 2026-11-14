# NVIDIA Nemotron: "snap a photo of the order"

Our entry for NVIDIA's **Best Use of Nemotron**. Code: [`agent/vision.py`](../agent/vision.py); eval:
[`scripts/vision_eval.py`](../scripts/vision_eval.py) on [`evals/vision/`](../evals/vision/); tests:
[`tests/test_vision.py`](../tests/test_vision.py).

## Model

**NVIDIA Nemotron 3.5 Super VL** (`nvidia/nemotron-3.5-super-vl-120b-a12b`, 120B MoE / 12B active, RADIO vision
encoder) through NVIDIA's OpenAI-compatible API (`https://integrate.api.nvidia.com/v1`, preset `nvidia` in
[`agent/llm.py`](../agent/llm.py), key `NVIDIA_API_KEY`). Images go in as base64 data URLs (`image_url` parts).
Reasoning mode is a chat-template switch (`chat_template_kwargs.enable_thinking`). We turn it off by default
because reading a page is perception, not deliberation, and it costs latency. The eval also measures it
switched on (`+think`).

## What it does

A kitchen's real inputs are often on paper: a handwritten order list taped to the cold-room door, next week's
printed menu, a supplier's delivery note with shortfalls. With Sous, the cook photographs the page and the
buyer agent turns it into a signed quote under the kitchen's mandate:

```
photo --(Nemotron VL: reads only)--> strict JSON: page kind, every line as written, quantity + unit,
                                     confidence per line, dishes + portions, delivery date, unreadable lines
      --(code)--> cross-checks each line against its own transcription (the number and unit on the line)
      --(Nemotron VL, only if needed)--> ONE targeted second look at just the suspicious lines
      --(code)--> order list:    lines -> catalog search (Finnish first) -> ONE basket quote
                  menu:          dishes x portions -> recipe planner (agent/recipe.py) -> ONE basket quote
                  delivery note: ordered - delivered = shortfall -> ONE basket quote (+ substitutes if out)
      --(gateway)--> mandate engine: ordered / held for a human / refused, with the rule that fired
```

Nemotron is the first step of the flow: without it there is no order. It reads Finnish, Swedish and
English handwriting, Finnish decimal commas ("2,5 l") and kitchen shorthand (*ltk* = box, *plo* = bottle,
*pkt* = pack, *kpl* = pieces). It also tells printed menus apart from order lists and delivery notes.
On a delivery note it reads the "ordered" and "delivered" columns, including a handwritten correction
("puuttuu 3 l").

```
uv run python -m agent.vision --provider nvidia --mandate M-HOTEL-AURORA --agent aurora-purchasing-agent \
    evals/vision/images/01_order_handwritten_fi.jpg            # add --order to place it under the mandate
uv run python -m agent.vision --read-only photo.jpg            # just Nemotron's reading, as JSON
uv run python -m agent.vision --from-json evals/vision/samples/01_order_handwritten_fi.read.json  # no key needed
```

The output shows the chain: each line read with its confidence → the products → the quote → the
mandate preview → the order. It ends with a "Needs a human (not ordered)" list. For an upload box (built in `/agent/`, 08371ec, but switched off on both servers until they have model keys): `agent.vision.order_from_image(image_bytes, gateway=..., mandate_id=..., agent_id=...,
llm=ChatClient(config_from_env("nvidia")))` returns a `PhotoOrder`, and its `.to_dict()` is the JSON for the
page.

## Why it matters

The challenge is about **trusted** trade agents. A model that misreads "12 kg" as "72 kg" and orders it is the
failure that matters. Sous treats Nemotron's reading as evidence, not as a decision:

- **The model never orders.** It transcribes and structures. Matching products, arithmetic, pack sizes and
  the mandate rules (spend, allergens, price caps, who must approve) are deterministic code in the gateway.
- **Uncertainty routes to a human.** Every line carries a confidence. Lines below 0.75, lines code can't
  reconcile, and unreadable lines are never ordered. They come back as "please confirm" next to the quote.
- **It meets the kitchen where it is.** No new app to type into: the paper the kitchen already uses becomes
  a signed, mandate-checked quote in seconds.

## How we improved its outputs

We measured first and then fixed what Nemotron actually got wrong, across five CI runs with the real model
(`evals/vision/history/`). Each step below came from a specific failure in those runs.

1. **Strict schema + validation + one repair retry.** The answer must parse as one JSON object and pass a
   pydantic schema. Units are normalized ("litraa" → l, "st" → kpl), decimal commas and percentage confidences
   are accepted, and `<think>` blocks and markdown fences are stripped. If it still fails, the validation
   error goes back to the model once. In practice Nemotron returned valid JSON on 239 of 240 page reads, and
   the repair retry fixed the one that wasn't.
2. **Prompt v1 → v2 (domain).** v1 is the obvious baseline ("extract the order as JSON"). v2 tells the model
   its role (transcribe, never fix or merge) and today's date with the coming weekdays, so "ma 16.11." becomes
   a date. It adds the Finnish/Swedish/English vocabulary, the unit abbreviation table, the decimal-comma rule,
   delivery-note columns and the "puuttuu" shortfall rule. It also defines confidence ("a digit that could be
   another digit: below 0.6, say why") and says why it matters. Result: silent errors went from 16 to 0, both
   traps were handled, and qty+unit went from 83 % to 100 %.
3. **Prompt v2 → v3 (from run-1 outputs).** The real outputs showed four more failure types.
   - **Headings and signatures read as lines** ("Tilaus - toimitus ma 16.11.", "- Ville", "Tuote"): v3 lists
     what is not an item.
   - **Table rows transcribed as only the product cell** ("Maito laktoositon 1 l"): the pack size in the
     name looked like the quantity, so code flagged correct rows. v3 asks for whole rows,
     `"Maito laktoositon 1 l | 40 | 24 | l"`.
   - **A red-pen "puuttuu 3 l!" listed as its own line**: v3 says a correction belongs to its row.
   - **Menus**: "60 hlö" written once in the heading must apply to every dish, and "raaka-aineet toimitettava
     ma 16.11." is the delivery date.

   Result: portions went from 50 % to 100 %, date from 88 % to 100 %, and auto-correct from 84 % to 87–100 %.
4. **Code checks the model, deterministically.**
   - **Cross-check:** each line's quantity and unit must appear in the text the model itself transcribed for
     that line. `"voi 5 kg"` with `quantity: 4` is a mismatch.
   - **Non-product lines are dropped visibly:** a line with no name, no quantity and no "number + unit" is
     shown as ignored, not ordered.
   - **Page-level shortfall check** (from run 2, where the same photo was read correctly in one run and not in
     the next): if the page says "puuttuu 3 l" but no row shows it, every row that looks delivered in full is
     suspect. A printed "Puuttuvat tuotteet toimitetaan erikseen" footer has no amount and doesn't count:
     that false alarm came from run 3.
5. **A targeted feedback loop (`+verify`).** Only lines code finds suspicious, or that came back below 0.75,
   get ONE second call with the image ("read index 3 again, character by character"). For a loose shortfall
   the call also says what the note says and asks which row it belongs to. A fix only counts if the row now
   shows the note: a row changed without it is treated as a guess. Anything unresolved is capped at 0.5
   confidence and goes to a human. In run 5 the second look fixed the red-pen correction every time it was
   needed. Clean pages cost one call.
6. **An eval set with traps, scored on safety and on accuracy.** We rendered 8 pages ourselves
   (`scripts/vision_make_images.py`, open handwriting fonts, rotation, noise, blur, uneven light, perspective):
   - handwritten Finnish order lists, a printed Swedish order and an English note;
   - a printed menu and a handwritten one;
   - a printed delivery note and a photographed one with a red-pen correction.

   The traps are a crossed-out line (must not be ordered), a smudged line (must be flagged, not guessed),
   "2,5 l" and *ltk/plo/pkt*. The scorer reports item recall and precision, exact quantity+unit, delivered
   quantities, dishes and portions, page type and date. The safety metric is **silent errors**: lines that
   would be ordered automatically but are wrong. Pages are read several times (`--repeat`), because the
   model is not deterministic and a single lucky run proves little.

## Results

NVIDIA Nemotron 3.5 Super VL, `scripts/vision_eval.py` in CI (`vision_only`): 8 pages, 31 truth lines and
6 dishes, each page read 3–4 times. Reading only, no Valio calls. Full table and per-page problems:
[`evals/vision/results.md`](../evals/vision/results.md); every run: `evals/vision/history/`.

| variant | pages read | qty+unit exact | delivered exact | portions | page type | date | auto-correct | silent errors | traps | calls | s/page |
|---|---|---|---|---|---|---|---|---|---|---|---|
| v1 baseline prompt | 24 | 83 % | 44 % | 50 % | 62 % | 75 % | 76 % | **16** | 0/6 | 24 | 5.5 |
| v2 domain prompt | 24 | 100 % | 100 % | 50 % | 100 % | 88 % | 84 % | 0 | 6/6 | 24 | 3.3 |
| v3 prompt from real errors | 24 | 100 % | 100 % | 100 % | 100 % | 100 % | 100 % | 0 | 6/6 | 24 | 4.9 |
| **v3 + code checks + second look** (default) | 32 | 100 % | 100 % | 100 % | 100 % | 100 % | **100 %** | **0** | 8/8 | 37 | 4.3 |
| v3 + second look + reasoning on | 8 | 100 % | 100 % | 100 % | 100 % | 100 % | 100 % | 0 | 2/2 | 8 | 20.7 |

Each row is the **latest CI run of that variant**, and the pipeline code changed between runs, so rows come from different runs: v1 and v2 from run 3 (CI 43528096128, e0988ad), v3 from run 4 (CI 43529398231, f27f862), v3+verify from run 5 (CI 43530170621, c64cd9d), reasoning-on from run 2 (CI 43526999077, d8ab856). Earlier runs of the same variant were worse, see below and `evals/vision/history/`. All runs are on the same 8 pages we rendered ourselves; there is no held-out set of real kitchen photos.

Item recall was 100 % in every variant. *auto-correct* is the share of truth lines that would be ordered
automatically and correctly. The rest go to a human, never to the basket wrongly.

Run to run, the numbers moved. v3+verify had 1 silent error in run 3 (fixed by the page-level check, e0988ad) and 94 % auto-correct with 0 silent errors in run 4. v3 alone was not perfect either. In run 3 it applied the red-pen correction on the photographed
delivery note in only 1 of 3 reads (auto-correct 87 %, delivered 93 %). The code checks caught the other two,
so they went to a human instead of becoming silent errors. With the page check and the targeted second look
(run 5, 4 reads per page), all 32 pages were right and only 3 needed a second call. Reasoning mode reaches
the same accuracy at ~5x the latency (20.7 s vs 4.3 s per page), so it stays off. Default variant cost per
page: ~3,300 prompt + ~470 completion tokens, 1.16 calls; free on build.nvidia.com.

Real readings are recorded in `evals/vision/samples/`. `--from-json` replays them through the live gateway
without a key, e.g. the photographed delivery note: "Ruokakerma 1 l | 5 | 5 | l | puuttuu 3 l!" → delivered
2 → shortfall 3 L + 4 kg of mince → one quote, mandate preview *ask* (the cream is above the hotel's €2.50/L
cap).

The scorer and the pipeline are also checked offline: an oracle "model" answering with the truth scores 100 %
(`--provider oracle`), and 16 offline tests use a scripted model and gateway.

## Engineering notes

- **Latency:** phone photos are EXIF-rotated and downscaled to 1600 px JPEG before upload. Reasoning is off by
  default. The second look only runs when code finds a suspicious line. The JSON answer is capped at 4,096 tokens.
- **Errors:** 429 / 5xx / timeouts retry with backoff and `Retry-After` (the free tier allows ~40 requests/min).
  Invalid JSON gets one repair retry, then a clean `VisionError`. A failed second look keeps the flags
  (fail-safe). The API key is only ever sent in the Authorization header, never logged or put in errors.
- **No key, no problem for the demo:** `--from-json` replays a saved reading through the same pipeline.
- **CI:** the `llm-eval` job has a `vision_only` manual input. It runs the vision eval when the
  `NVIDIA_API_KEY` secret exists (`integrate.api.nvidia.com` is on the job's CRACI egress allowlist). It
  makes no Valio calls.
