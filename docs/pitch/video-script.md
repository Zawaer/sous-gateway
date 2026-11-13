# Video script (2:00 hard limit)

Target: about 270 spoken words at a calm pace. **VO** is the voice-over; everything else is what's on screen.
**[REC]** = screen-record this. **[STILL]** = a static slide or screenshot. Fill placeholders like `[EVAL: ...]` from `evals/results.json` before recording.

Before recording: reset the demo (`scripts/demo_orders.py`), seed the mandates, open the UI on a phone (or a phone-sized browser) with the approver code entered, and connect Claude Code over MCP (`claude mcp add --transport http valtakirja <public URL>/mcp/`). For the history beat, change the hospital mandate once *after* its order is decided (e.g. raise the auto-approve limit) so "then vs now" shows a difference.

---

## 0:00–0:12 Hook: 13 minutes vs seconds

- **Screen:** split screen. Left **[STILL]**: Puntari's run, timer at 13 minutes, final answer "call wholesalers on Saturday". Right **[REC]**: the Valtakirja receipt appearing within seconds.
- **VO:** "We asked a good shopping agent for a thousand litres of lactose-free milk by Monday. It researched for thirteen minutes, then told us to phone a wholesaler on Saturday. Wholesalers don't publish prices or stock to agents."

## 0:12–0:24 Problem, from Valio Aimo's side

- **Screen [STILL]:** one line on black: "Agents will buy. Who defines how?" Then the Valtakirja name.
- **VO:** "Agents are coming either way. If Valio Aimo doesn't define how they buy, someone else's platform will. So we built the front door. Valtakirja means mandate: every agent carries one from its human, and Valio Aimo enforces it."

## 0:24–0:46 Demo 1: hotel, 1,000 L, notify

- **Screen [REC]:** Claude Code terminal on a laptop. Prompt: "1,000 litres of lactose-free milk by Monday for Hotel Aurora." Show the MCP tool calls: `search_products` → `request_quote` → `place_order`. Cut to the order in the web UI: tier **notify**, Valio Eila 1 L × 1,000, €1,400 ex VAT, signed receipt.
- **VO:** "Claude Code, a general agent, connects over MCP for the hotel. It searches 17,546 products, gets a live quote with two Valio API calls, and orders a thousand litres of Valio Eila at €1.40. That's above the hotel's notify line, so it goes through and the manager gets an FYI."

## 0:46–1:02 Demo 2: hospital, no allergen data, ask, approved on a phone

- **Screen [REC]:** hospital order with a substitute line. Tier **ask**, reason in plain words (no allergen declaration; mandate avoids milk and nuts). Cut to a phone **[REC]**: the approval link opens with the cut-off countdown; tap **Approve** with a note. Status flips to ordered.
- **VO:** "The hospital kitchen must stay milk- and nut-free. The substitute has no allergen declaration. Nearly half the catalog has none. Unknown means unsafe, so the order waits. The head of catering approves it from his phone."

## 1:02–1:14 Demo 3: Terminal past cut-off, blocked, Stock alternative

- **Screen [REC]:** quote line for a Terminal product: `closingTime` Thu 12.11 (already passed), API still says Mon 16.11. Highlight "cut-off passed" and the Stock alternative the gateway picked.
- **VO:** "Valio's own API still promises Monday on supplier-shipped products whose cut-off has already passed. The gateway catches that and picks a stocked product that can really arrive."

## 1:14–1:28 Audit and "mandate as of decision"

- **Screen [REC]:** audit view: event chain with hashes, `/audit/verify` all green. Then the order history: mandate when decided vs mandate now, with the version timeline.
- **VO:** "Every step lands in a hash-chained log that both sides read. MariaDB system versioning answers the auditor's question: what did the mandate say when this order was decided?"

## 1:28–1:44 Why trust it, and why it's open

- **Screen [STILL]:** three columns: "Rules in code" / "Signed: Ed25519 JWS, public JWKS" / "MCP + OpenAPI, one service".
- **VO:** "The language model never decides. Limits, allergens and cut-offs are plain code. Mandates, quotes and receipts are signed; anyone can verify them. MCP and OpenAPI expose the same service, so any agent works."

## 1:44–1:52 Numbers

- **Screen [STILL]:** three big numbers: `[EVAL: % correct]` correct with no manual fixes · `[EVAL: % unsafe blocked]` unsafe orders stopped · `[EVAL: API calls per request]` Valio API calls per request.
- **VO:** "On `[EVAL: N]` test requests: `[EVAL: % correct]` handled correctly with no manual fixes, and `[EVAL: % unsafe blocked]` of unsafe orders stopped."

## 1:52–2:00 Close

- **Screen [STILL]:** "Valtakirja" + one-liner + public URL + GitHub link.
- **VO:** "Valtakirja. AI purchasing agents carry a signed mandate from their human, and Valio Aimo's gateway enforces it."

---

## Recording checklist

| Shot | Source | Type |
|---|---|---|
| Puntari 13-minute run, final answer | Toivo's Puntari screenshot | STILL |
| Claude Code + MCP tool calls, hotel order | laptop terminal | REC |
| Hotel order detail (notify, signed receipt) | web UI `/ui/#/orders/<id>` | REC |
| Hospital "ask" with reason | web UI | REC |
| Phone approval with cut-off countdown | phone, `approval_url` | REC |
| Terminal past cut-off → Stock alternative | quote in UI or `/docs` | REC |
| Audit chain + verify; order history AS OF | web UI audit, `/orders/<id>/history` | REC |
| Trust / openness, numbers, close | deck slides | STILL |

Tips: record each REC beat separately (10–20 s) and cut. Speed up the tool-call scroll if needed, but keep each receipt on screen for at least two seconds. If the cut runs long, trim the trust section first, never the demo beats.
