# Use Sous from the AI assistant you already have

Sous is a remote MCP server (Streamable HTTP, protocol 2025-11-25, every tool with an `outputSchema`). Any
assistant that can add a remote MCP server can buy from Valio Aimo through it, under a mandate it can't exceed.

Below, `SOUS` is the gateway: `https://135-181-8-209.sslip.io` (production demo) or
`https://staging.135-181-63-134.sslip.io` (staging, same code).

## 1. Get credentials (30 seconds)

```bash
curl -s -X POST $SOUS/sandbox | jq '{agent_token, approver_token, mandates: [.mandates[] | {scenario, mandate_id, agent_id}], connect: .quickstart.connect}'
```

A judge sandbox gives you three mandates (hotel, hospital, school) valid 24 h, and two secrets, each shown once:

| Secret | Who holds it | What it can do |
|---|---|---|
| `agent_token` (`vka_...`) | **the assistant** | search, quote, order within the mandates, read their orders |
| `approver_token` (`sbxa_...`) | **you, the human** | approve or reject held orders (seller UI). Never give it to an assistant. |

## 2. Pick your assistant

There are two MCP URLs. They serve the same tools; only the authentication differs:

| URL | Authentication | For |
|---|---|---|
| `$SOUS/mcp` | header `Authorization: Bearer <agent_token>` | clients that can set a header: Claude Code, Cursor, VS Code, the MCP SDKs |
| `$SOUS/connect/mcp` | OAuth 2.1 (sign in by pasting the agent token), or the same header | clients that only do OAuth: claude.ai, Claude Desktop and mobile, ChatGPT |

Why two: claude.ai and ChatGPT connectors can't send a fixed header (Claude's "request headers" option is a beta
for a few organizations; ChatGPT offers only OAuth or no authentication), so `/connect/mcp` answers `401` with
OAuth discovery. `/mcp` stays open for header clients and anonymous catalog search.

### Claude Code (verified: header)

```bash
claude mcp add --transport http sous $SOUS/mcp --header "Authorization: Bearer <agent_token>"
```

The sandbox response has this line ready-made as `quickstart.claude_code`. Or, with OAuth (no token in your shell
history): `claude mcp add --transport http sous $SOUS/connect/mcp`, then `/mcp` in Claude Code, choose sous,
Authenticate, and paste the agent token in the browser page.

### claude.ai, Claude Desktop, Claude mobile (OAuth)

1. Settings > Connectors > **Add custom connector** (on Team/Enterprise an Owner adds it under Organization settings).
2. Name `Sous`, URL **`$SOUS/connect/mcp`** (e.g. `https://135-181-8-209.sslip.io/connect/mcp`). Leave the
   advanced OAuth client fields empty: Claude registers itself (dynamic client registration).
3. **Connect**. A Sous page opens: paste the `agent_token`, press Connect. Claude stores an access token
   (`vko_...`, 1 h, refreshed automatically); it never sees your approver token.
4. In a chat, enable the Sous connector and ask, e.g. the sandbox's `prompts[0].prompt`.

Desktop and mobile use the same connector once added on claude.ai.

### ChatGPT (OAuth)

1. chatgpt.com/plugins > **+** > **Add custom MCP server** (older UI: Settings > Apps > Advanced settings >
   Developer mode, then Create app). Availability depends on your plan and workspace settings.
2. Server URL **`$SOUS/connect/mcp`**, authentication **OAuth**. ChatGPT discovers and registers itself.
3. Sign in on the Sous page by pasting the `agent_token`.
4. ChatGPT asks you to confirm each write tool call (`request_quote`, `place_order`, ...): Sous marks only the
   read tools `readOnlyHint`. Plans limited to read-only connectors get search and lookups, not quotes or orders.

### Cursor (header)

`~/.cursor/mcp.json` (or `.cursor/mcp.json` in a project):

```json
{"mcpServers": {"sous": {"url": "https://135-181-8-209.sslip.io/mcp",
                         "headers": {"Authorization": "Bearer <agent_token>"}}}}
```

### VS Code / GitHub Copilot agent mode (header, token prompted once and stored as a secret)

`.vscode/mcp.json`:

```json
{
  "inputs": [{"type": "promptString", "id": "sous-token", "description": "Sous agent token (vka_...)", "password": true}],
  "servers": {"sous": {"type": "http", "url": "https://135-181-8-209.sslip.io/mcp",
                       "headers": {"Authorization": "Bearer ${input:sous-token}"}}}
}
```

### Anything else

Any MCP client: `$SOUS/mcp` with the header. Clients that can't set headers but can pass tool arguments can send
`agent_token` as an argument (documented in each tool). Browser-based clients: CORS allows any origin, the
`Authorization`, `Mcp-Session-Id` and `Mcp-Protocol-Version` request headers, and exposes `Mcp-Session-Id` and
`WWW-Authenticate`.

## 3. What the assistant can and can't do

It can: search the 17.5k-product catalog (free), ask for signed quotes priced live at Valio Aimo, look for
substitutes, place orders, follow up on orders and standing orders, read its own mandates.

It can't, whatever it is told:

- **approve or reject an order.** Orders the mandate sends to a human come back `pending_approval` with an
  `approval_url` for you; an agent credential presented on an approval endpoint is refused and audited. The OAuth
  sign-in page refuses approver tokens, and `/connect/mcp` never forwards an approver credential.
- **exceed the mandate.** Spend limits, allergens, categories, price caps, cut-offs and stock are checked by the
  gateway on every quote and order (`mandate_preview`, then the order's tier: silent, notify, ask, block); a
  refused order stays refused.
- **see other buyers.** The token reads only its own mandates, quotes and orders.

You stay in control: when the buyer rotates or revokes the agent credential (`POST /mandates/{id}/agent-credential`,
approver only) every OAuth grant made with it stops working at once; a sandbox's grants end (401, refresh refused) when the sandbox is deleted or
expires (24 h). Removing the connector in the assistant drops its tokens on that side.

## 4. How the OAuth part works (for reviewers)

`sous/oauth.py`, tests in `tests/test_oauth.py`, live check in `scripts/connect_check.py`.

- `/connect/mcp` without a valid token: `401` + `WWW-Authenticate: Bearer resource_metadata="$SOUS/.well-known/oauth-protected-resource/connect/mcp"`.
- RFC 9728 metadata names `$SOUS` as the authorization server; RFC 8414 metadata at
  `/.well-known/oauth-authorization-server`: `/oauth/register` (RFC 7591, open), `/oauth/authorize`,
  `/oauth/token`; `code` + S256 PKCE only; refresh tokens rotate on every use. Redirect URIs: https, or http on
  localhost/127.0.0.1 with any port (Claude Code, RFC 8252). Claude's hosted callback is
  `https://claude.ai/api/mcp/auth_callback`.
- The "login" is the agent token. The gateway keeps only SHA-256 hashes of codes and tokens; the agent token a
  grant stands for is stored encrypted under a key derived from that grant's own token, so the database alone
  reveals no credential.
- At `/connect/mcp` the access token is exchanged for its agent token and the request is served by the ordinary
  `/mcp` app, so read scopes, agent authentication, mandate rules and sandbox budgets apply byte-for-byte as with
  the header. The access token works only there (REST endpoints take the agent token).
- Not implemented: client ID metadata documents (Claude falls back to DCR), token revocation endpoint (rotate the
  agent credential instead), DPoP.

Check it yourself (one sandbox, at most one live quote per mode):

```bash
uv run python scripts/connect_check.py $SOUS --quote
```
