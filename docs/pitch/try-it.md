# Try Sous yourself (5 minutes)

Sous is Valio Aimo's front door for AI purchasing agents. Your agent shops; a signed mandate from "your" human decides what it may do alone. You get a private sandbox, so you can't break the demo, and nothing you do is a real delivery.

The demo server is `https://135-181-8-209.sslip.io` (production). If `/rules` returns 404 there, it is still on an older build: use staging, `https://staging.135-181-63-134.sslip.io`, which runs current `main`.

## 1. Get a sandbox

```bash
export SOUS=https://135-181-8-209.sslip.io
curl -s -X POST $SOUS/sandbox | tee sandbox.json | jq
```

You get (keep this output, the tokens are shown once):

- a sandbox id like `SBX-7Q2K`, valid for 24 hours;
- three buyers, each with a mandate and an agent:
  - **Hotel Aurora**: may spend up to €2,500 alone, FYI above €1,000;
  - **Meilahti hospital kitchen**: €400 alone, no milk or nuts, substitutes need a human;
  - **Espoo schools**: €300 alone, nothing over €1,500, no gluten or egg;
- `agent_token`: your agents' credential (for all three);
- `approver_token`: lets you approve or reject your own held orders, nobody else's;
- `quickstart` and `prompts`: everything below, already filled in;
- `seeded`: demo data already in your sandbox, so the seller UI isn't empty: a hotel standing order with four weeks of simulated history, and this week's renewal **held for approval** because milk crept up (`standing.quantity_creep`, `standing.quantity_trend`, `standing.cost_drift`). It is priced from reference prices stored in the gateway, not a live Valio quote, and says so on the order. Creating the sandbox makes no Valio call.

## 2. Connect an agent

With Claude Code, paste `quickstart.claude_code`:

```bash
claude mcp add --transport http sous $SOUS/mcp/ --header "Authorization: Bearer <agent_token>"
```

Any MCP client works the same way; or use plain REST with `quickstart.curl_quote` and `curl_order` (replace `<quote.id>` with the quote's id; the idempotency key is your sandbox id plus the quote id, so a retry returns the same order and keys never clash with other sandboxes).

## 3. Ask it to buy something

Paste a prompt from `prompts` (they include your mandate and agent ids). Prices and stock are live Valio Aimo data, so products and totals vary; the tiers and rules below are what the mandates guarantee:

| Prompt | What happens |
|---|---|
| Hotel: 1,000 L of lactose-free milk for Monday | Free catalog search, then one signed quote with live prices. Only plain lactose-free milk is chosen silently (no coffee milk or milk drinks). Between €1,000 and €2,500, the usual case, it is **ordered with an FYI** (`spend.notify`); above €2,500 or the €2.50/L cap it is held. |
| Hospital: substitute for chopped tomatoes | Ranked substitutes with their compromises. An order for a substitute is **held for a human** (`substitution`); missing allergen data is flagged, never treated as safe, and milk or nuts are refused. |
| Hotel: one exact product for tomorrow | Depends on the clock: after the order cut-off, **refused** with the rule (`availability.cutoff_passed`); before it, a small ordinary order. The agent can't talk its way around a passed cut-off. |
| School: 1,500 L of milk | **Refused** whenever the total is over the school's €1,500 hard limit (`spend.hard_limit`), i.e. at any price above €1/L; never ordered silently. |

Try to break it: ask for something with nuts for the hospital, claim to be another agent, reuse a quote. The mandate engine is plain code: no language model can approve an order or relax a rule.

## 4. Be the human

Open `quickstart.seller_ui` (`/ui/?sandbox=SBX-7Q2K#/inbox`): your held orders, each with the rules that fired. The seeded standing-order renewal is already there (`seeded.held_order_id`); its standing order shows the four-week history and the drift. Approve or reject; when asked for the approver code, paste your `approver_token`. Then look at the order's timeline and the audit log: every step is hash-chained and the receipt is signed (Ed25519), checkable in the browser. `quickstart.buyer_ui` is the buyer's side of the same orders.

## Limits

- 15 live Valio Aimo lookups per sandbox (quotes, substitutes, suggestions); repeats served from the cache are free. `GET /sandbox/<id>` shows how many are left.
- When the limit is reached you get a friendly `429 sandbox_limit` with `retry_after`; sandboxes also share a per-minute budget, so the live demo never starves.
- 3 new sandboxes per IP per hour. After 24 h the sandbox expires: its mandates are revoked and it is hidden.
