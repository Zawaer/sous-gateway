# Stage pitch: 3 minutes + 3 minutes Q&A (top 5, Sun 15.11 14:15)

Not the video. This is the live pitch from our own laptop; the mic is cut at 3:00. One speaker talks, the other drives
the laptop. Numbers are from [docs/STATUS.md](../STATUS.md); don't round them differently on stage.

Peer-review criteria, and where each one lands: **Innovation** (beat 3: the mandate as a signed, enforceable object
the seller runs, not a chatbot), **Impact** (beats 1 and 5: every kitchen that buys from a wholesaler, the
allergen-safety case for hospitals and schools), **Implementation** (beats 2 and 4: it is live, measured, and attacked).

## Before going on

- Laptop: browser tabs open in this order, logged in, zoomed to 125 %:
  1. `/ui/?sandbox=<id>#/inbox` of a sandbox made that morning (its held standing-order renewal is waiting);
  2. `/ui/break.html?sandbox=<id>` with the agent token pasted;
  3. terminal with Claude Code connected to `$SOUS/mcp` (sandbox agent token), prompt typed, not sent.
- Phone (for the approval): the same inbox link, approver token pasted.
- Fallback if the venue network fails: deck slides 4–10 carry the same story with production screenshots. If
  production is down, use staging (same flow); say so.
- Never show the approver code or a token on the projector.

## Beats

| Time | Beat | Say (roughly) | Show |
|---|---|---|---|
| 0:00–0:25 | **1. Problem** | "We asked a shopping agent for 1,000 litres of lactose-free milk by Monday. Thirteen minutes later it told us to phone a wholesaler on Saturday. Agents will buy for kitchens anyway; the question is whose rules they follow. If Valio Aimo doesn't define that, someone else's platform will." | Slide 2 |
| 0:25–1:25 | **2. Live demo moment** | "This is Claude Code, any agent, connected to Valio Aimo over MCP. I ask for the hotel's milk." Send the prompt: it searches, gets a signed quote with live price, orders, and the gateway answers *Auto + FYI* with the rule. "Now the hospital's agent asks for a substitute." It is *held*: "Needs approval, decide by" a clock time from Valio's cut-off. Approve it on the phone. Then one click on the break page: "agent approves its own order", 403, in the audit log. | Terminal, phone, `/ui/break.html` |
| 1:25–1:55 | **3. The trust idea** | "The agent carries a mandate, *valtakirja*, signed for its human: limits, allergens, substitutions. Valio Aimo's gateway enforces it in plain code. No language model ever approves an order or relaxes a rule: the agent's model reads the request and explains the result, and our one classifier can only add a refusal. Every order lands in one of four tiers, and buyer and Valio Aimo read the same hash-chained record." | Slide 6 (tiers) |
| 1:55–2:25 | **4. Numbers** | "60 of 60 scripted buyer requests on live Valio prices; 43 of 43 unsafe actions stopped or sent to a human. Three LLM agents on the same 48 sentences, scored in one CI run: Kimi 47, Gemini 46, Nemotron 30 matched the buyer, and zero mandate violations ordered by any of them. A 17-check conformance kit anyone can run against a gateway: 17 of 17 on staging. All our own cases, and we say so." | Slide 11 |
| 2:25–2:50 | **5. Why it can be a real product** | "It is open: MCP and OpenAPI, signed JSON, AP2 and A2A exports, MIT. It runs on Verda in Finland with CI on CRACI. What's missing is honest and short: real buyer login and keys bound to agents, and Valio Aimo's order API with contract prices." | Slide 12 |
| 2:50–3:00 | **6. Ask** | "We'd like one Valio Aimo customer kitchen for a pilot: one mandate, one agent, real orders. Sous: the agent carries the mandate, Valio Aimo enforces it." | Slide 1 (URL + QR) |

Timing check: rehearse twice with a stopwatch. If the demo runs long, cut beat 5 to its last sentence; never cut
the ask.

## The 8 most likely questions

Two sentences each, then stop. Longer answers: [qa-prep.md](qa-prep.md).

1. **Why not let the LLM decide?**
   A model can be talked into anything and can't be audited; a prompt injection in a product name must not raise a
   spend limit. So the model only turns language into a request, and limits, allergens, cut-offs and tiers are plain
   code with a rule id anyone can look up at `/rules`.

2. **How do you know the agent did what the buyer asked?**
   We score it: three models on the same 48 buyer sentences in one CI run, Kimi K3.1 Pro 47/48, Gemini 4 Hydrogen
   46/48 and Nemotron 3.5 Super VL 30/48 matched without correction, and 0 mandate violations ordered with any. The cases are ours, not a held-out
   set, which is why the gateway, not the model, is the safety net.

3. **What stops an agent from lying about who it is or approving its own order?**
   Quotes and orders are bound to the agent and mandate they were issued to, so another agent's key gets
   `quote_mismatch`, and an agent that tries to approve gets 403, written to the audit log. Identity is demo-grade
   today (bearer tokens, one approver code); production needs buyer login and agent keys bound to the mandate.

4. **What happens when stock is short or the cut-off has passed?**
   The signed quote lists options, part now and the rest later, the next deliverable date, or a substitute of the
   same product type, each with what the mandate would do with it. A held order carries an approve-by time from
   Valio's cut-off, and past it the order expires and the buyer is told the next date.

5. **30 % of food products have no allergen data. Isn't "unknown means a human" too strict?**
   It only applies to mandates with allergen rules, so the hotel's milk order is never held. For a hospital, "we
   don't know" is not "safe", and one tap on a phone is cheap compared with a wrong guess.

6. **Is this a real product or a hackathon demo?**
   It is live on Verda with MariaDB, signed artifacts, CI on CRACI with an SBOM and deny-by-default egress, and a
   conformance kit that any gateway can be checked against. What's missing is listed in docs/STATUS.md: Valio Aimo's
   order API (today "ordered" means accepted and signed by us, at list prices), buyer login and bound agent keys.

7. **Why would Valio Aimo run this instead of a marketplace doing it?**
   It keeps the agent channel on Valio Aimo's terms: its rules, its data, its audit trail, and its own sponsored
   suggestions that are checked against the buyer's mandate first. Exact stock stays private (agents see ample,
   limited or short), and Valio Aimo can pause agent ordering or recall a product with one call.

8. **Why MCP, and what about AP2 or other standards?**
   MCP is what today's agents speak, so Claude Code ordered with zero integration code; the same operations are on
   OpenAPI. The trust lives in signed JWS objects, and we export AP2 v0.2 claim sets (unsigned views today), an A2A
   agent card and schema.org orders, so a change of transport doesn't change the guarantees.
