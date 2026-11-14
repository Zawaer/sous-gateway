# Demo runbook

## Sunday 15.11: live demo day (peer review and top-5 pitch)

`scripts/demo_prep.py` gets a server ready and writes everything Toivo needs to one private file:

```bash
uv run python scripts/demo_prep.py --base $SOUS --check-only   # health + judge links, no sandbox, no Valio call
uv run python scripts/demo_prep.py --base $SOUS                # + one private demo sandbox, warmed
uv run python scripts/demo_prep.py --base $SOUS --reset        # a fresh sandbox even if the last one is usable
```

It checks `/health` (ok, commit, clock mode, Valio shared budget and circuit), `/rules`, `/holds`, `/ui/`,
`/ui/break.html`, `/buyer/` and MCP `initialize` at `/mcp`. Then it creates **one** sandbox, checks its seeded held
order is `pending_approval` with `approve_by` in the future, and warms the cache with 2 live lookups: the
quickstart's hotel milk quote and the hospital's substitutes for 468376. It places no order and runs no attack
(those are the live demo). Output: `~/sous-demo-<date>.json` plus a printable `.txt` next to it, mode 600, never
inside a git checkout (the script refuses). The file has the tokens, the links to open in order, the MCP connect
command, the four prompts and the stage steps with expected outcomes. Stdout shows only each token's last 4
characters. Exit 1 with a list means something a judge would hit is broken: fix it or switch to staging.

Rerunning without `--reset` reuses the sandbox from the file if it is still active, has 3 h or more left, and
its seeded order has not been decided yet. Otherwise it makes a new one and says why.

**Lifetimes.** A sandbox lives **24 h** (`SOUS_SANDBOX_TTL_HOURS`) and has 15 live Valio lookups. Its seeded
held order expires at Monday delivery's cut-off minus the approval margin, about **Sun 16:45**. The per-mandate
daily limit (hotel €8,000) fills with each live 1,000 L order. A new sandbox has new mandate copies, so its
counters start at zero. You can create at most 3 sandboxes per IP per hour, and conformance-kit runs count too.

| Time (Helsinki) | What |
|---|---|
| **08:00** | Code freeze. The last deploy to prod is whatever is green. |
| **~09:30** | `demo_prep.py --base $SOUS` against **prod** (the sandbox is valid until Mon ~09:30). Print the `.txt` or keep it open on the laptop, not on the projector. Open the tabs in the order the file lists them. Paste the approver token in the laptop and phone inboxes and the agent token on the break page. Run the MCP connect command in the Claude Code terminal. If prod fails, run the same command against staging and say so on stage. |
| **10:00** | Submission. |
| **10:30–13:00** | Peer review, 3 minutes per table (see below). |
| **~13:45** | If we are in the top 5: `demo_prep.py --base $SOUS --reset`. This gives fresh mandates (daily limits back to zero) and an undecided seeded order. Then open the tabs again. |
| **14:15** | Top-5 pitch from our own laptop, following [docs/pitch/stage-pitch.md](pitch/stage-pitch.md). The demo order is in the table below. |

**3 minutes at a peer-review table.** (1) Seller inbox: the held standing-order renewal and its drift reasons,
one sentence on the four tiers (30 s). (2) Claude Code over MCP: send the hotel milk prompt. It gets a signed live
quote and orders as Auto + FYI (60 s). (3) Break page: click "agent approves its own order" to get a 403 and the
audit event (30 s). (4) Leave them the README judge path and `POST /sandbox` so they can try it themselves. Don't approve the
seeded order at every table: approve it once, then show the live hospital substitute's hold. If the seeded order
has been decided, rerun `demo_prep.py`: it makes a new sandbox on its own.

**Stage demo order** (beat 2 of the pitch). The `.txt` has the same steps with copy-paste prompts.

| # | Do | Expect |
|---|---|---|
| 1 | Claude Code: send the hotel prompt (1,000 L lactose-free milk) | Signed live quote, `ordered`, **Auto + FYI** (`spend.notify`) at €1,000–2,500; above €2,500 or €2.50/L it is held instead |
| 2 | Claude Code: send the hospital substitute prompt (468376, order 4 packs) | **Held** (substitution, maybe `allergen.unknown`), "decide by" from Valio's cut-off; approve on the phone |
| 3 | Break page: "agent approves its own order" | **403**, blocked, event in the audit log |
| 4 | (if time) Seller inbox: seeded renewal | Held by `standing.quantity_creep` / `quantity_trend` / `cost_drift` |

**If the Wi-Fi or Valio fails:** the seeded sandbox (held renewal, standing-order history, audit log) and the
break page make **no Valio calls**, so they still work as long as the gateway is reachable (use a phone hotspot).
If Valio's circuit is open, `/health` shows `circuit: open` and quotes fail. In that case show the inbox and the
break page and say why. If the gateway is unreachable, use deck slides 4–10 (production screenshots).
Never show a token or the approver code on the projector.

---

# 2-minute video (recorded)

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
   needs the approver code: `SOUS_APPROVER_CODE=<code> uv run python scripts/demo.py 5 --base <base>`
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

### Agent tokens and the sandbox route
When the gateway requires agent authentication (`SOUS_REQUIRE_AGENT_AUTH=1`, on staging since the
night of 13-14.11), quotes and orders need the mandate's bearer token. Two ways:
- **Your own tokens:** put them in `.sous-agent-tokens.json` (gitignored) or `SOUS_AGENT_TOKENS`.
  With `SOUS_APPROVER_CODE` set, the script mints missing ones, but minting *rotates* the mandate's
  credential and cuts off anyone else using it.
- **A sandbox (no shared tokens touched):** `scripts/demo.py 1 2 3 4 --sandbox new --base <base>`
  creates a judge sandbox (private copies of the hotel, hospital and school mandates, 24 h, 15 live
  lookups) and saves its tokens to the token file. Approval links open `/ui/?sandbox=SBX-…`; the UI
  asks for the sandbox's approver token (`sbxa_…`, in the token file). Reuse it with
  `--sandbox SBX-XXXX`. Scene 5 (Kaarna) and `demo_standing.py` have no sandbox copy yet.
  Dry run on staging, 14.11 ~01:05: scenes 1-4 all as expected in 63 s with `--pause 15`,
  ~10 Valio calls; the phone approval with the `sbxa_` token worked.

### Valio's proactive offer (near best-before cream)
Valio's selling agent pushes an offer; the hotel's agent may take it; the mandate still decides.
1. **Valio creates it** (approver code, or a sandbox's `sbxa_` token for its own hotel):
   `POST <base>/offers` with `{"target": {"mandate_ids": ["M-HOTEL-AURORA"]}, "product_ids": ["464194"],
   "discount_pct": 30, "quantity_cap": 60, "valid_until": "<in 2 days>", "reason": "near_best_before",
   "reason_text": "best before 20.11., cream for sauces and desserts"}`. The response is signed (`jws`) and shows
   `mandates` (delivered) and `excluded` (e.g. the hospital, which avoids milk, if targeted). Every new sandbox
   already has one such offer (`seeded.offer`), so in a sandbox skip this step.
2. **The hotel's agent sees it**: MCP `list_offers(mandate_id)` (or `GET /mandates/{id}/offers`): "Valio Aimo
   offers… Near best-before", with the disclosure. The buyer's feed has an `offer.received` notification.
3. **It quotes**: `request_quote(mandate_id, agent_id, delivery_date, items=[{product_ids: ["464194"], amount: 20,
   unit: "SALES_UNIT"}], offer_id="OF-…")`. The line costs the offer price (never above the live price) and
   carries `offer`; `mandate_preview` lists `offer.applied` (notify).
4. **The mandate decides**: `place_order` → **Auto + FYI** (notify: the buyer hears the agent took a seller's
   offer), or **Ask human** when the cream is a first-time product for the hotel (`product.first_time`), as with
   the seeded sandbox offer. Asking for more than is left → `offer_over_cap`; after valid_until → `offer_expired`.
5. Audit log: filter by the offer id for `offer.created` → `offer.offered` → `offer.accepted`.

## If something goes wrong
- A scene prints `scene failed`: the others still run. Usually the Valio mock is slow (503) or
  the per-IP rate limit (60 POST/min) was hit; wait a minute and rerun that scene.
- After Sun 15.11 17:00 the mock's Stock cut-off has passed and most products read as
  "cut-off passed" (see the eval note in `docs/STATUS.md`). Record before that.
- **Judging after Sun 16:30:** @rene-claude-infra sets `SOUS_DEMO_NOW=2026-11-15T09:00:00+02:00` in the
  prod `.env` (with Rene's OK) and redeploys. The gateway then evaluates cut-offs as if it were Sun 09:00,
  advancing in real time (`SOUS_DEMO_CLOCK=frozen` to pin it; restart to reset). It is disclosed everywhere:
  `/health` `clock.mode = "demo"`, a UI banner, `X-Sous-Clock` header, `clock` in every signed quote and
  order, audit events. See PROTOCOL.md section 21. Unset it to return to real time.
- Scene 1 comes back **Ask human** with `spend.daily_limit`: the hotel mandate allows €8,000 per
  day and earlier rehearsals the same day count towards it. That is the mandate working; record on
  a fresh day (it resets at midnight Helsinki time) or use a gateway whose orders were reset.
- Approve says the approver code is wrong: clear it by entering the right one at the prompt
  (the UI asks again on 401).
