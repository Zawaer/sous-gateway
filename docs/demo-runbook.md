# Demo runbook (2-minute video)

Three things on screen: a terminal running `scripts/demo.py`, the gateway UI on a laptop
(`<base>/ui/`), and a phone with the UI open for the approval. Everything below is live data
from Valio Aimo's mock API; nothing is staged in the UI.

## Before recording
1. Gateway up, demo mandates issued (`uv run python -m scripts.seed`, idempotent).
2. Laptop: open `<base>/ui/#/inbox`, set your name (top right). The **Live activity** column
   on the right shows each agent step as it happens; new orders also pop a toast.
3. Phone: open `<base>/ui/#/inbox` once and enter the approver code when asked (it is kept
   in that browser). On the deployed gateway the code is with Rene; never show it on screen.
4. Dry run: `uv run python scripts/demo.py --base <base> --list`.
5. Scene 5 creates its own buyer (Kaarna Catering) the first time it runs on a gateway, which
   needs the approver code: `VALTAKIRJA_APPROVER_CODE=<code> uv run python scripts/demo.py 5 --base <base>`
   (type it in your own terminal; never show it on screen). Later runs reuse the mandate.

## Scenes
| # | Run | What the viewer sees | Outcome |
|---|-----|----------------------|---------|
| 1 | `scripts/demo.py 1 --base <base>` | Hotel agent: "1,000 l lactose-free milk by Monday". Search costs 0 Valio API calls; the signed quote picks Stock products and skips Terminal ones whose cut-off passed. | **Auto + FYI**: ordered, buyer notified (over €1,000) |
| 2 | `scripts/demo.py 2 --base <base>` | Hospital agent asks for good/better/best substitutes and picks one with **no allergen data**. Laptop toast "Held for a human"; card appears in Approvals with both reasons. | **Ask human** → open the link on the phone, **Approve** |
| 3 | `scripts/demo.py 3 --base <base>` | Cheapest milk is a Terminal product whose cut-off already passed: skipped automatically; when the agent insists on it, refused with the rule. | **Auto**, then **Blocked** |
| 4 | `scripts/demo.py 4 --base <base>` | Over the hospital's €400 limit → asks; rye bread for a gluten-free school → refused. | **Ask human**, **Blocked** |
| 4b | `scripts/demo.py 5 --base <base>` | **New buyer, Kaarna Catering Oy**. Its mandate checks milk, egg, tree nuts and peanuts. Five catalog searches exclude declared “contains” and “may contain” matches with zero Valio API calls. One quote covers oat drink, chickpeas, tomatoes and hummus for 150 guests. **15 kg of rice is missing** for Monday. The agent then requests cashew nut butter. | **Ask human** for the shortage and two products with missing allergen data. **Blocked** for the cashew butter's “may contain” declarations. Open the held order in the buyer view to see the shortage or in the seller view to see the timeline. |
| 4c | `scripts/demo_standing.py --base <base>` (approver code needed once, for the simulated history) | Hotel Aurora's **standing order** for weekly dairy: four past weeks (marked simulated, priced from a real quote) creep 420 → 455 L of milk; this week's live renewal asks 515 L. UI **Standing** tab: trend bars per item, the three drift findings, every renewal. | **Ask human** (quantity creep +18 %, three rises in a row, cost +16 %) instead of auto-renewing |
| 5 | UI: **Audit log** | Chain intact badge; filter by the order id from scene 2 to show every step incl. the phone approval. Click **Verify receipt signature** in the order panel. | Hash chain + Ed25519 checked in the browser |
| 6 | UI: **Stats** | Share handled without a human, unsafe orders stopped, Valio API calls per quote. Eval-set numbers are in `docs/STATUS.md`. | Numbers slide |

Run all scenes in one go with `scripts/demo.py --base <base> --pause 4`.

## If something goes wrong
- A scene prints `scene failed`: the others still run. Usually the Valio mock is slow (503) or
  the per-IP rate limit (60 POST/min) was hit; wait a minute and rerun that scene.
- After Sun 15.11 17:00 the mock's Stock cut-off has passed and most products read as
  "cut-off passed" (see the eval note in `docs/STATUS.md`). Record before that.
- Scene 1 comes back **Ask human** with `spend.daily_limit`: the hotel mandate allows €8,000 per
  day and earlier rehearsals the same day count towards it. That is the mandate working; record on
  a fresh day (it resets at midnight Helsinki time) or use a gateway whose orders were reset.
- Approve says the approver code is wrong: clear it by entering the right one at the prompt
  (the UI asks again on 401).
