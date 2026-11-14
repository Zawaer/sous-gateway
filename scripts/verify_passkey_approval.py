"""Re-verify a passkey approval offline, from the audit log alone.

    # from a running gateway (audit + order; send the approver code or a sandbox token if the order is private)
    uv run python scripts/verify_passkey_approval.py --gateway https://sous.example --order PO-1234ABCD [--code ...]

    # from saved files: the audit event (or its `data.passkey`) and optionally the order JSON
    uv run python scripts/verify_passkey_approval.py --event event.json [--order-json order.json]

Checks (sous/passkey_verify.py, only `cryptography` needed): the challenge is SHA-256 of the order binding, the
clientDataJSON is a webauthn.get for that challenge from an origin on the RP ID, the authenticator saw and verified
the user, and the signature verifies with the approver's registered public key. With the order: the binding is
that order (id, quote, total, lines hash, approve_by) and the signed receipt names the same challenge.
Exit code 0 when every check passes.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sous.passkey_verify import check_order, verify_evidence  # noqa: E402


def _get(url: str, code: str | None) -> dict:
    req = urllib.request.Request(url, headers={"X-Approver-Code": code} if code else {})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gateway", help="Gateway base URL")
    ap.add_argument("--order", help="Order id (with --gateway)")
    ap.add_argument("--code", help="X-Approver-Code or sandbox approver token, for private orders")
    ap.add_argument("--event", help="Audit event JSON file (or just its data.passkey object)")
    ap.add_argument("--order-json", help="Order JSON file (GET /orders/{id}) to check the binding against")
    a = ap.parse_args()

    order = None
    if a.gateway and a.order:
        base = a.gateway.rstrip("/")
        order = _get(f"{base}/orders/{a.order}", a.code)
        events = _get(f"{base}/audit?subject={a.order}&limit=200", a.code)["events"]
        evidence = [e["data"]["passkey"] for e in events if e.get("subject") == a.order
                    and e["event"] in ("order.approved", "order.rejected_by_human") and e["data"].get("passkey")]
    elif a.event:
        raw = json.loads(Path(a.event).read_text())
        evidence = [raw.get("data", {}).get("passkey") or raw.get("passkey") or raw]
        if a.order_json:
            order = json.loads(Path(a.order_json).read_text())
    else:
        ap.error("give --gateway and --order, or --event")
    if not evidence:
        print("No passkey decision found for this order.")
        return 1

    ok = True
    for ev in evidence:
        res = verify_evidence(ev)
        checks = res["checks"] + (check_order(ev, order)["checks"] if order else [])
        b = ev.get("binding") or {}
        print(f"{b.get('action', '?')} of {b.get('order_id')} at {b.get('total_ex_vat')} EUR by {ev.get('approver')} "
              f"(credential …{(ev.get('credential_id') or '')[-8:]}, RP {ev.get('rp_id')})")
        for c in checks:
            print(f"  {'ok  ' if c['ok'] else 'FAIL'} {c['check']}" + (f"  ({c['detail']})" if c.get("detail") else ""))
            ok &= c["ok"]
    print("VERIFIED" if ok else "NOT VERIFIED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
