#!/usr/bin/env bash
# Smoke-test a running gateway: discovery endpoints, a catalog search over the fixture
# products, and an MCP initialize + tools/list over Streamable HTTP.
# Usage: tests/fixtures/smoke.sh [base_url]   (default http://127.0.0.1:8000)
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"

echo "waiting for $BASE/health"
for i in $(seq 1 60); do
  curl -fsS "$BASE/health" >/dev/null 2>&1 && break
  [ "$i" = 60 ] && { echo "gateway did not become healthy"; exit 1; }
  sleep 2
done

check() {  # check <path> <jq filter that must be truthy>
  local body
  body=$(curl -fsS "$BASE$1")
  echo "$body" | jq -e "$2" >/dev/null || { echo "FAIL $1: $2"; echo "$body" | head -c 600; echo; exit 1; }
  echo "ok   $1"
}

check /health '.ok == true'
check /openapi.json '.openapi and (.paths | has("/products"))'
check /.well-known/jwks.json '.keys | length > 0 and (.[0].crv == "Ed25519")'
check '/products?q=maito&limit=5' '.products | length > 0'
curl -fsS "$BASE/docs" | grep -qi swagger || { echo "FAIL /docs"; exit 1; }
echo "ok   /docs"
if [ -n "${EXPECT_STORAGE:-}" ]; then check /health ".storage == \"$EXPECT_STORAGE\""; fi

# MCP: initialize, then list tools with the returned session id.
hdr=$(mktemp)
init=$(curl -fsS -D "$hdr" -X POST "$BASE/mcp" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"ci-smoke","version":"1"}}}')
echo "$init" | grep -q '"serverInfo"' || { echo "FAIL mcp initialize"; echo "$init"; exit 1; }
echo "ok   /mcp initialize"
sid=$(grep -i '^mcp-session-id:' "$hdr" | cut -d' ' -f2 | tr -d '\r' || true)
sh=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream')
[ -n "$sid" ] && sh+=(-H "mcp-session-id: $sid")
curl -fsS -X POST "$BASE/mcp" "${sh[@]}" -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
tools=$(curl -fsS -X POST "$BASE/mcp" "${sh[@]}" -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
echo "$tools" | grep -q '"search_products"' || { echo "FAIL mcp tools/list"; echo "$tools"; exit 1; }
echo "ok   /mcp tools/list"
echo "smoke passed"
