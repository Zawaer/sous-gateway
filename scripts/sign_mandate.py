"""Sign a mandate as the buyer's human, as a W3C Verifiable Credential (vc+jwt), and send it to the gateway.

The private key stays in a file you choose (keep it OUTSIDE the repo); it's created on first use (mode 0600).
The gateway verifies the signature against your did:key, checks the key may speak for the buyer (first use pins
it, or an approver registers it) and countersigns.

    uv run python scripts/sign_mandate.py --key ~/.sous/kide.pem mandate.json            # sign + POST
    uv run python scripts/sign_mandate.py --key ~/.sous/kide.pem mandate.json --no-post  # just print the VC
    uv run python scripts/sign_mandate.py --key ~/.sous/kide.pem --did                   # show your did:key
    uv run python scripts/sign_mandate.py --key ~/.sous/kide.pem --revoke M-KIDE          # signed revocation

mandate.json holds Mandate fields (sous/mandate.py), e.g.
    {"id": "M-KIDE", "buyer": "Kahvila Kide", "agent_id": "kide-agent", "issued_by": "Aino",
     "auto_approve_limit_eur": 150, "hard_limit_eur": 800, "avoid_allergens": ["AM"]}
YAML works too when PyYAML is installed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sous import config, credentials as C  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def load_key(path: Path, alg: str):
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    if path.resolve().is_relative_to(REPO):
        sys.exit(f"Refusing to create a private key inside the repo ({path}); pick a path outside it.")
    key = C.new_key(alg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600)
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    print(f"New {alg} key in {path}", file=sys.stderr)
    return key


def load_mandate(path: Path) -> dict:
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        import yaml  # optional
        return yaml.safe_load(text)
    return json.loads(text)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mandate", nargs="?", type=Path, help="mandate JSON/YAML file")
    ap.add_argument("--key", type=Path, required=True, help="private key file (PEM; created if missing)")
    ap.add_argument("--alg", choices=["ES256", "EdDSA"], default="ES256", help="for a new key (default ES256)")
    ap.add_argument("--gateway", default=config.PUBLIC_BASE_URL or "http://localhost:8000")
    ap.add_argument("--days", type=int, default=90, help="validUntil = now + days (default 90)")
    ap.add_argument("--no-post", action="store_true", help="print the signed credential, don't send it")
    ap.add_argument("--did", action="store_true", help="print the key's did:key and exit")
    ap.add_argument("--revoke", metavar="MANDATE_ID", help="sign and send a revocation for this mandate")
    ap.add_argument("--reason", help="revocation reason")
    a = ap.parse_args()

    key = load_key(a.key.expanduser(), a.alg)
    did = C.did_key(key.public_key())
    if a.did:
        print(did)
        return
    if a.revoke:
        m = httpx.get(f"{a.gateway}/mandates/{a.revoke}", timeout=30).raise_for_status().json()
        if not m.get("credential"):
            sys.exit(f"{a.revoke} isn't buyer-signed; an approver revokes it at POST /mandates/{{id}}/revoke.")
        rev = C.sign_revocation(key, a.revoke, m["credential"], a.reason)
        r = httpx.post(f"{a.gateway}/mandates/{a.revoke}/revoke-credential", json={"revocation": rev}, timeout=30)
        print(json.dumps(r.json(), indent=2, ensure_ascii=False))
        sys.exit(r.status_code >= 400)
    if not a.mandate:
        ap.error("give a mandate file (or --did / --revoke)")

    now = datetime.now(config.TZ).replace(microsecond=0)
    token = C.sign_vc(key, load_mandate(a.mandate), now, now + timedelta(days=a.days))
    if a.no_post:
        print(token)
        return
    r = httpx.post(f"{a.gateway}/mandates/credential", json={"credential": token}, timeout=30)
    body = r.json()
    if r.status_code >= 400:
        sys.exit(f"Refused ({r.status_code}): {body.get('error')}: {body.get('message')}")
    mid = body["mandate"]["id"]
    print(f"Mandate {mid} signed by {did} (you), countersigned by the gateway {body.get('countersigned_by')}.")
    if body.get("agent_credential"):
        print(f"Agent credential (shown once): {body['agent_credential']['header']}")
    print(f"Check it: {a.gateway}/mandates/{mid}/verify")


if __name__ == "__main__":
    main()
