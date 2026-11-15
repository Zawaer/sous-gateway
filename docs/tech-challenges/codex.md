# Best Use of Codex

Sous was built by human-directed agents coordinating on one GitHub issue (the "agent board"). Codex had three jobs.

**1. Builder: the reference buyer (`buyer/`), written by Codex.** Rene's Codex session (`rene-codex-buyer`) built the browser client a kitchen uses against Valio Aimo's gateway: editable purchase request, product shortlist and ranked substitutes, a signed quote verified against the gateway's JWKS *in the browser*, ordering under a mandate, signed receipts and approval follow-up, multi-item baskets with per-item coverage, live refresh of pending orders, and expired-quote handling. 18 commits touch `buyer/`; 12 Node tests (`buyer/buyer.test.mjs`). Codex ran end-to-end checks itself on the live deployment (prod order PO-38ADF620: approved, receipt verified in the browser).

**2. Independent QA: `rene-codex-qa`.** A second Codex session tested the system adversarially and posted 18 blocker reports on the board (as of Sat ~02:45 Helsinki), each with a repro, for example: a 5-item basket made 10 Valio calls instead of 2 (now batched); the audit UI counted cache hits as API calls; the buyer's signature check broke after the rename; a Python 3.11 syntax regression; a low-confidence delivery-note line could be reported as "delivered in full" (fixed in 42eb64e, re-verified by Codex: 13 vision tests pass). Each fix was re-verified by Codex.

**3. Security reviewer: a read-only Codex review of the trust core** (`sous/service.py`, mandates, approvals; ~356k input tokens). Finding: approving a held order re-checked only revocation, expiry and cut-off, not the mandate's *current* rules, so an order held before the mandate was tightened could still be approved. Fixed in **4b10297**: approval re-runs the mandate rules against the mandate as it is now and refuses on block; re-issuing an id is audited as `mandate.updated`; a revoked id can't be revived (409).

**Why it matters.** Sous is about agents acting under a human's mandate; we built it the same way, with Codex writing, testing and reviewing in parallel with Claude Code sessions.

Codex QA's follow-up (board 22:29Z): re-issuing a mandate didn't take the order lock, so a re-issue racing an approval could use the old limits. Fixed in eae0f9d (`issue_mandate` takes `_order_lock`).

**Not measured:** how much faster Codex made us; there is no baseline.

Code: https://github.com/Zawaer/sous-gateway · [buyer/](../../buyer/) · board: the private development repo's issue #1
