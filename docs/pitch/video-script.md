# Video script (2:00 hard limit)

Target: about 280 spoken words at a calm pace. **VO** is the voice-over; everything else is what's on screen.
**[REC]** = screen-record this. **[STILL]** = a static slide or screenshot. Numbers match `docs/STATUS.md` and `evals/results.json`.

Before recording (full steps in [demo-runbook.md](../demo-runbook.md)): record before Sun 15.11 17:00 (the mock's cut-off). Use production, https://135-181-8-209.sslip.io, with the demo mandates seeded. Open the UI on a phone (or a phone-sized browser) with the approver code entered. Connect Claude Code over MCP (`claude mcp add --transport http valtakirja https://135-181-8-209.sslip.io/mcp/`). Run `scripts/demo_standing.py --base <URL>` once, so the held renewal is in the inbox. For the history beat, change the hospital mandate once *after* its order is decided (e.g. raise the auto-approve limit), so "then vs now" shows a difference.

---

## 0:00–0:10 Hook: 13 minutes vs seconds

- **Screen:** split screen. Left **[STILL]**: Puntari's run, timer at 13 minutes, final answer "call wholesalers on Saturday". Right **[REC]**: the Sous receipt appearing within seconds.
- **VO:** "We asked a good shopping agent for a thousand litres of lactose-free milk by Monday. It researched for thirteen minutes, then told us to phone a wholesaler on Saturday."

## 0:10–0:20 Problem, from Valio Aimo's side

- **Screen [STILL]:** one line on black: "Agents will buy. Who defines how?" Then the Sous name.
- **VO:** "Agents will buy from wholesalers either way. So we built Valio Aimo's own front door for them. Every agent carries a mandate, a valtakirja, from its human, and the gateway enforces it."

## 0:20–0:42 Demo 1: hotel, 1,000 L, notify (and the cut-off the API missed)

- **Screen [REC]:** Claude Code terminal on a laptop. Prompt: "1,000 litres of lactose-free milk by Monday for Hotel Aurora." Show the MCP tool calls: `search_products` → `request_quote` → `place_order`. Flash the skipped Terminal line (`closingTime` Thu 12.11 passed, API still says Mon 16.11). Cut to the order in the web UI: tier **notify**, Valio Eila 1 L × 1000, €1,400 ex VAT, signed receipt.
- **VO:** "Claude Code connects over MCP for a hotel. It searches 17,546 products without touching Valio's API, then gets a signed live quote with two calls. It skips supplier-shipped milk whose cut-off has already passed, though the API still says Monday. A thousand litres of Valio Eila, fourteen hundred euros: above the hotel's notify line, so it's ordered and the manager gets an FYI."

## 0:42–0:58 Demo 2: hospital, no allergen data, ask, approved on a phone

- **Screen [REC]:** hospital order with a substitute line. Tier **ask**, reason in plain words (no allergen declaration; mandate avoids milk and nuts). Phone **[REC]**: the ntfy push arrives (only if `VALTAKIRJA_NTFY_TOPIC` is set on the gateway; otherwise open the link from the inbox), the approval link opens with the cut-off countdown; tap **Approve** with a note. Status flips to ordered.
- **VO:** "The hospital kitchen must stay milk- and nut-free. This substitute has no allergen declaration; nearly half the catalog has none. Unknown means unsafe, so the order waits. The head of catering approves it on his phone."

## 0:58–1:12 Demo 3: standing order drifts, ask before renewing

- **Screen [REC]:** the held weekly renewal in the inbox. Drift findings: `standing.quantity_creep` milk +18 % vs the 4-week median, `standing.cost_drift` +€135 (+16 %). Small label: "history simulated, priced at a live quote".
- **VO:** "The hotel's weekly dairy order renews itself. This week the milk is up eighteen percent on the four-week median, and the bill a hundred and thirty-five euros higher. So the gateway asks before renewing."

## 1:12–1:24 Audit and "mandate as of decision"

- **Screen [REC]:** audit view: event chain with hashes, `/audit/verify` all green. Then the order history: mandate when decided vs mandate now, with the version timeline.
- **VO:** "Every step lands in a hash-chained log that both sides read. MariaDB system versioning shows what the mandate said when each order was decided."

## 1:24–1:40 Why trust it, and why it's open

- **Screen [STILL]:** three columns: "Rules in code" / "Signed: Ed25519 JWS, public JWKS" / "MCP + OpenAPI · any model: Claude, Gemini, open-weight on Verda (EU)".
- **VO:** "The language model never decides. Limits, allergens and cut-offs are plain code, and quotes and receipts are signed. Any agent can buy: Claude, Gemini, or an open-weight model running on Verda in the EU."

## 1:40–1:52 Numbers

- **Screen [STILL]:** three big numbers: **45/45** test requests handled correctly · **36/36** unsafe actions blocked or escalated · **≤2** Valio API calls per request. Source line: "evals/results.json".
- **VO:** "On forty-five scripted requests: all forty-five handled correctly, and all thirty-six unsafe actions blocked or sent to a human."

## 1:52–2:00 Close

- **Screen [STILL]:** "Sous" + one-liner + public URL + GitHub link.
- **VO:** "Sous. AI purchasing agents carry a signed mandate from their human, and Valio Aimo's gateway enforces it."

---

## Recording checklist

| Shot | Source | Type |
|---|---|---|
| Puntari 13-minute run, final answer | Toivo's Puntari screenshot | STILL |
| Claude Code + MCP tool calls, hotel order | laptop terminal | REC |
| Skipped Terminal line (cut-off passed) | quote in the order panel or `scripts/demo.py 1` output | REC |
| Hotel order detail (notify, signed receipt) | web UI `/ui/#/orders/<id>` | REC |
| Hospital "ask" with reason | web UI, `scripts/demo.py 2` | REC |
| ntfy push (if configured) + phone approval with cut-off countdown | phone, `approval_url` | REC |
| Standing order held for drift | web UI inbox after `scripts/demo_standing.py` | REC |
| Audit chain + verify; order history AS OF | web UI audit, `/orders/<id>/history` | REC |
| Trust / openness, numbers, close | deck slides | STILL |

Tips: record each REC beat separately (10–20 s) and cut. Speed up the tool-call scroll if needed, but keep each receipt on screen for at least two seconds. If the cut runs long, trim the trust section first, then the drift beat; never the hotel or hospital beats.
