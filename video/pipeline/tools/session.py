"""Real MCP session as the sandbox hotel's agent: milk order (beats 2-4) and the purpose case (beat 6).
Writes data/session-<env>.json (tokens redacted). Run inside valtakirja's env:
  uv run --project ../valtakirja python tools/session.py staging [milk|purpose|all]"""
import asyncio, json, sys, time, uuid, pathlib
from datetime import date, timedelta
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import sbx, httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client, create_mcp_http_client

ROOT = pathlib.Path(__file__).resolve().parent.parent

def monday():
    t = date.today(); return t + timedelta(days=(7 - t.weekday()) % 7 or 7)

async def main(env, part):
    s = sbx.load(env); base = sbx.BASES[env]
    hotel = [m for m in s["mandates"] if m["scenario"] == "hotel"][0]
    A, M, when = hotel["agent_id"], hotel["mandate_id"], monday().isoformat()
    log = {"base": base, "sandbox": s["sandbox"]["id"], "mandate": M, "agent": A, "delivery_date": when,
           "jwks": httpx.get(base + "/.well-known/jwks.json", timeout=30).json(), "events": []}
    hc = create_mcp_http_client(headers={"Authorization": "Bearer " + s["agent_token"]})
    async with streamable_http_client(base + "/mcp", http_client=hc) as streams:
        async with ClientSession(streams[0], streams[1]) as cs:
            init = await cs.initialize()
            log["server"] = init.server_info.name
            log["tools"] = [t.name for t in (await cs.list_tools()).tools]
            async def call(scene, name, args):
                t0 = time.time(); r = await cs.call_tool(name, args)
                txt = r.content[0].text if r.content else ""
                try: body = r.structured_content or json.loads(txt)
                except ValueError: body = {"text": txt}
                log["events"].append({"scene": scene, "tool": name, "args": args, "result": body,
                                      "is_error": bool(r.is_error), "seconds": round(time.time() - t0, 2)})
                print(scene, name, "error" if r.is_error else "ok", round(time.time() - t0, 1), "s", "" if not r.is_error else str(body)[:300])
                (ROOT / "data" / f"session-{env}-{part}.json").write_text(json.dumps(sbx.redact(log), ensure_ascii=False, indent=1))
                return body
            if part in ("milk", "all"):
                log["events"].append({"scene": "milk", "user": "I need 1,000 litres of lactose-free milk by Monday."})
                found = await call("milk", "search_products", {"query": "laktoositon maito", "claims": ["FREE_FROM_LACTOSE"], "limit": 12, "agent_id": A})
                ids = [p["id"] for p in found["products"]]
                q = await call("milk", "request_quote", {"agent_id": A, "mandate_id": M, "delivery_date": when,
                               "items": [{"product_ids": ids, "amount": 1000, "unit": "L", "product_type": "milk", "purpose": "breakfast milk"}]})
                q = q.get("quote", q)
                await call("milk", "place_order", {"agent_id": A, "mandate_id": M, "quote_id": q["id"], "idempotency_key": f"video-{uuid.uuid4().hex[:10]}"})
            if part in ("cream", "story"):
                usual, use = "463366", "recipe: panna cotta"
                log["events"].append({"scene": "cream", "user": "Monday's panna cotta: our usual whipping cream can't come. Find a replacement."})
                await call("cream", "search_products", {"query": "kuohukerma laktoositon", "limit": 12, "agent_id": A})
                await call("cream", "request_quote", {"agent_id": A, "mandate_id": M, "delivery_date": when,
                           "items": [{"product_ids": [usual], "amount": 20, "unit": "L", "purpose": use}]})
                subs = await call("cream", "suggest_substitutes", {"product_id": usual, "mandate_id": M, "agent_id": A, "purpose": use,
                                  "amount": 20, "unit": "L", "limit": 3, "delivery_date": when})
                pick = subs["options"][0]["product_id"]
                q = await call("cream", "request_quote", {"agent_id": A, "mandate_id": M, "delivery_date": when,
                               "items": [{"product_ids": [pick], "amount": 20, "unit": "L", "substitute_for": usual, "purpose": use}]})
                q = q.get("quote", q)
                await call("cream", "place_order", {"agent_id": A, "mandate_id": M, "quote_id": q["id"], "idempotency_key": f"video-{uuid.uuid4().hex[:10]}"})
            if part in ("trio", "story"):
                log["events"].append({"scene": "purpose", "user": "What may replace each cream, by what it's for?"})
                for pid, purpose in (("463442", "coffee"), ("463366", "recipe: panna cotta"), ("463366", "allergy menu")):
                    await call("purpose", "suggest_substitutes", {"product_id": pid, "mandate_id": M, "agent_id": A, "purpose": purpose,
                               "amount": 20, "unit": "L", "limit": 3, "delivery_date": when})
            if part in ("purpose", "all"):
                log["events"].append({"scene": "purpose", "user": "Whipping cream for Monday's panna cotta is short. What can we use?"})
                for pid, purpose in (("463442", "coffee"), ("464194", "recipe: panna cotta"), ("464194", "allergy menu")):
                    await call("purpose", "suggest_substitutes", {"product_id": pid, "mandate_id": M, "agent_id": A, "purpose": purpose, "amount": 20, "limit": 3, "delivery_date": when})
                q = await call("purpose", "request_quote", {"agent_id": A, "mandate_id": M, "delivery_date": when,
                               "items": [{"product_ids": ["464699"], "amount": 20, "unit": "SALES_UNIT", "substitute_for": "464194", "purpose": "recipe: panna cotta"}]})
                q = q.get("quote", q)
                await call("purpose", "place_order", {"agent_id": A, "mandate_id": M, "quote_id": q["id"], "idempotency_key": f"video-{uuid.uuid4().hex[:10]}"})
    out = ROOT / "data" / f"session-{env}-{part}.json"
    out.write_text(json.dumps(sbx.redact(log), ensure_ascii=False, indent=1)); print("wrote", out)

asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "all"))
