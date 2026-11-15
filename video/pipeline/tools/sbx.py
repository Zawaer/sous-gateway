"""Sandbox helper for the video. Tokens live only in secrets/ (mode 600) and are never printed."""
import json, os, re, sys, pathlib, httpx
ROOT = pathlib.Path(__file__).resolve().parent.parent
# This Mac's resolver has a stale negative cache for the new name; public DNS (1.1.1.1) answers 135.181.8.209.
PIN = {"sous.saarikko.dev": "135.181.8.209"}
import socket as _socket
_orig_gai = _socket.getaddrinfo
def _gai(host, *a, **k):
    h = host.decode() if isinstance(host, bytes) else host
    return _orig_gai(PIN.get(h, host), *a, **k)
_socket.getaddrinfo = _gai
BASES = {"staging": "https://staging.135-181-63-134.sslip.io", "prod": "https://sous.saarikko.dev", "prod2": "https://sous.saarikko.dev", "prod3": "https://sous.saarikko.dev", "prod4": "https://sous.saarikko.dev"}

def redact(o):
    if isinstance(o, dict):
        return {k: ("<redacted>" if "token" in k.lower() or k.lower() in ("approver_code",) else redact(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [redact(x) for x in o]
    if isinstance(o, str):
        return re.sub(r"(sbx[a-z]?_[A-Za-z0-9_-]+|Bearer [A-Za-z0-9._-]+)", "<redacted>", o)
    return o

def path(env): return ROOT / "secrets" / f"sandbox-{env}.json"
def load(env): return json.loads(path(env).read_text())
def client(env, who="agent"):
    s = load(env); tok = s["agent_token"] if who == "agent" else s["approver_token"]
    h = {"Authorization": f"Bearer {tok}"}
    if who == "approver": h["X-Approver-Code"] = tok
    return httpx.Client(base_url=BASES[env], headers=h, timeout=60)

if __name__ == "__main__":
    env, cmd = sys.argv[1], sys.argv[2]
    if cmd == "new":
        r = httpx.post(BASES[env] + "/sandbox", timeout=60); r.raise_for_status()
        p = path(env); p.write_text(r.text); os.chmod(p, 0o600)
        print(json.dumps(redact(r.json()), indent=1)[:6000])
    elif cmd == "show":
        print(json.dumps(redact(load(env)), indent=1)[:6000])
