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
mandate preview → the order. It ends with a "Needs a human (not ordered)" list. For an upload box (the
`/buyer/` page): `agent.vision.order_from_image(image_bytes, gateway=..., mandate_id=..., agent_id=...,
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

1. **Strict schema + validation + one repair retry.** The answer must parse as one JSON object and pass a
   pydantic schema. Units are normalized ("litraa" → l, "st" → kpl), decimal commas and percentage confidences
   are accepted, and `<think>` blocks and markdown fences are stripped. If it still fails, the validation
   error goes back to the model once ("That was not valid: … return only the corrected JSON").
2. **Prompt engineering (v1 → v2).** v1 is the obvious baseline ("extract the order as JSON"). v2 tells the
   model its role (transcribe, never fix or merge) and today's date with the coming weekdays (so "ma 16.11."
   becomes a date). It gives the Finnish/Swedish/English vocabulary and the unit abbreviation table, the
   decimal-comma rule, and to skip crossed-out lines. It explains delivery-note columns and the "puuttuu"
   shortfall rule. It defines confidence ("a digit that could be another digit: below 0.6, say why") and
   says why that matters: a low-confidence line is checked by a human, a wrong confident line is ordered.
3. **A feedback loop that is cheap and targeted (`+verify`).** Code compares each line's quantity and unit
   with the text the model itself transcribed for that line. Example: `text_as_written: "voi 5 kg"` but
   `quantity: 4`. Lines that disagree, or that came back below the threshold, get ONE second call with the
   image that names only those lines ("read line 3 again, character by character"). A mismatch that survives
   the second look is capped at 0.5 confidence, so it goes to a human. Pages that read cleanly cost one call.
4. **An eval set with traps, scored on safety and on accuracy.** 8 pages we rendered ourselves
   (`scripts/vision_make_images.py`, open handwriting fonts, rotation, noise, blur, uneven light, perspective):
   handwritten Finnish order lists, a printed Swedish order, an English note, a printed and a handwritten
   menu, a printed delivery note and a photographed one with a red-pen correction. Traps: a crossed-out line
   (must not be ordered), a smudged line (must be flagged, not guessed), "2,5 l", *ltk/plo/pkt*. The scorer
   reports item recall and precision, exact quantity+unit, delivered quantities, dishes and portions, page
   type, date, and the safety metric: **silent errors**, lines that would be ordered automatically but are
   wrong.

## Results

`scripts/vision_eval.py`, 8 pages / 31 truth lines + 6 dishes. Full per-page problems:
[`evals/vision/results.md`](../evals/vision/results.md).

> **Pending:** the run with Nemotron 3.5 Super VL needs the `NVIDIA_API_KEY` GitHub secret (asked on the agent
> board). It runs with `gh workflow run ci.yml -R Zawaer/sous --ref main -f vision_only=true` and lands in the
> `llm-eval-results` artifact. The table below is filled from that run.

| variant | item recall | qty+unit exact | delivered exact | portions | silent errors | traps | calls | s/page |
|---|---|---|---|---|---|---|---|---|
| v1 (baseline prompt) | | | | | | | | |
| v2 (engineered prompt) | | | | | | | | |
| v2 + verify (second look) | | | | | | | | |

The scorer and pipeline are checked offline with an oracle "model" that answers with the truth
(`--provider oracle`: 100 % on every metric, 0 silent errors, traps 2/2), and by 11 offline tests with a
scripted model and gateway.

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
