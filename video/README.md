# Demo video

**[sous-demo.mp4](sous-demo.mp4)** (1:57.8, 1920×1080, −14 LUFS; captions in [sous-demo.srt](sous-demo.srt)) is the
Junction 2026 demo for Valio Aimo's "Trusted Trade Agents" challenge. It is built around must-build 3, what an
agent may decide alone and what a human signs off, through three real cases on production (sous.saarikko.dev):

1. **Alone:** the brief's own request, 1,000 L of lactose-free milk for Monday, on the live try page. Request,
   lookup, a fixed signed price, and the agent orders by itself (Auto + FYI).
2. **Asks a person:** whipping cream for Monday's panna cotta.
   - Over MCP: the search has 0 Valio API calls, and the usual cream's order cut-off has passed.
   - Substitutes come back for a recipe; the pick is an Ed25519-signed quote with reliability, origin and CO₂.
   - The order is held, because what a product is for decides the swap. Anything off the usual also waits for a
     person: bigger quantities, later delivery, a price jump, different allergens.
   - Valio Aimo sees the held order read-only, with a plain reason in Finnish too. The chef approves on the phone.
3. **Refused:** a school's order over its hard limit (the buyer is notified at once), and an agent trying to approve
   its own order (HTTP 403).

Then the mandate and its four tiers, run by plain code (no language model approves an order), the numbers from
docs/STATUS.md ("our own test cases, not a held-out set"), the open standards and the path to production.

Every product shot is a real recording of production (commits 923a330 and 85db94e, sandbox SBX-V9MH). One order,
PO-184ABBD8, runs through the terminal replay and every screen. The terminal is a replay of the real MCP session in
[pipeline/data/session-prod6-all.json](pipeline/data/session-prod6-all.json). The voices are ElevenLabs eleven_v3
(Rene and Toivo). The music and the blurred kitchen behind the phone are generated.

## Script

| # | Voice | Line |
|---|---|---|
| 0 | Rene | With Sous, AI agents buy from Valio Aimo for a kitchen. The question is what they may do alone. |
| 1 | Toivo | There are three answers: order alone, ask a person, or refuse. Here's each one, live. |
| 2 | Rene | Alone first, with the brief's own example: a thousand litres of lactose-free milk for Monday. Sous checks live price and stock, signs a fixed price, and the agent orders by itself. |
| 3 | Toivo | Now a harder one: whipping cream for Monday's panna cotta. The search covers over seventeen thousand products without calling Valio's systems, and the quote shows the usual cream's cut-off has passed. |
| 4 | Rene | So the agent asks for substitutes, and says the cream is for a recipe. The pick comes back as a signed quote: a fixed price, with reliability, origin and CO₂. |
| 5 | Toivo | The agent orders, and the order waits, because what it's for decides the swap. Coffee could take any cream of the same kind; a recipe needs the chef. Anything off the usual waits too: bigger quantities, later delivery, a price jump, different allergens. |
| 6 | Rene | Valio Aimo sees the order, but doesn't decide it. The chef gets the reason in plain words, in Finnish too, and approves on the phone. |
| 7 | Toivo | And some things are never allowed. A school's order over its limit is refused outright, and so is an agent approving its own order. |
| 8 | Rene | All of it comes from the mandate the buyer issued, run by plain code in four tiers. No language model approves an order or relaxes a rule. |
| 9 | Toivo | On our own test cases, not a held-out set: zero mandate violations across five models. And it's open: MCP, OpenAPI, Peppol, MIT. |
| 10 | Rene | Next: buyer login, Valio Aimo's order API and contract prices. |
| 11 | Toivo | Sous. Agents do the buying, and the customer stays in control. |

## Rebuilding

Everything the cut is made from is in [pipeline/](pipeline/). Run all commands from `video/pipeline/`; Python is
run through `uv`, and the cards and recordings need Playwright's Chromium.

| Step | Command |
|---|---|
| Final cut (cards, captions band, browser bars, mix to −14 LUFS) | `uv run --with playwright --with pillow python tools/assemble.py --env prod6 --out sous-demo` |
| Voice lines (needs `ELEVENLABS` / `TOIVO_VOICE` keys in the environment or the repo's `.env`) | `uv run --with httpx python tools/vo.py`, then `python3 tools/tighten.py` and `python3 tools/captions.py` |
| New recordings against a fresh judge sandbox | `tools/sbx.py <env> new`, `tools/session.py <env> story`, `tools/shoot.py <env> <shot>` (shots in `tools/shots.json`) |

- `tools/assemble.py`: the timeline is `SEGMENTS`.
- `tools/src/`: the HTML cards (title, terminal replay, purpose list, numbers, path to production, end card).
- `shots/prod6/`: the screen recordings used.
- `audio/`: voice, music and sound.
- `data/`: the session and caption timings.

Not included: sandbox tokens and approver codes (`secrets/`, created per run and never committed), API keys, and the
intermediate renders. In the published session data each JWS is stored as its three parts; the terminal card joins
them and checks the Ed25519 signature against the gateway's JWKS when it renders.
