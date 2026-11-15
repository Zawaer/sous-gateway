"""Add the real MCP server card and the milk order's Peppol XML to data/session-<env>-all.json (for t_open)."""
import sys, json, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent)); import sbx
ROOT = pathlib.Path(__file__).resolve().parent.parent
env = sys.argv[1]; base = sbx.BASES[env]; c = sbx.client(env)
ids = json.loads((ROOT / "data" / f"ids-{env}.json").read_text())
card = c.get("/.well-known/mcp/server-card.json").json()
pe = c.get(f"/orders/{ids.get('cream_order') or ids['milk_order']}/peppol/order.xml"); pe.raise_for_status()
p = ROOT / "data" / f"session-{env}-all.json"; d = json.loads(p.read_text())
d["open"] = {"card": {k: card.get(k) for k in ("name", "title", "version", "remotes") if k in card},
             "peppol_url": f"{base}/orders/{ids.get('cream_order') or ids['milk_order']}/peppol/order.xml", "peppol_head": pe.text.splitlines()[:40]}
p.write_text(json.dumps(sbx.redact(d), ensure_ascii=False, indent=1)); print("open data:", card.get("title"), "| peppol", pe.status_code)
