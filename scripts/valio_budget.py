"""Print the shared Valio Aimo budget as /health shows it (valio_aimo.shared_budget). Never prints the URL.

    uv run python -m scripts.valio_budget            # JSON line
    uv run python -m scripts.valio_budget --probe    # also where a connection fails (DNS/TCP/TLS), no secrets
    uv run python -m scripts.valio_budget --summary  # also a Markdown line in $GITHUB_STEP_SUMMARY (CI)
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
from urllib.parse import urlsplit

from sous import shared


def probe() -> dict:
    """Where a connection to SOUS_VALKEY_URL stops (DNS, TCP, TLS), without revealing host, port or password."""
    if not shared.URL:
        return {"configured": False}
    u = urlsplit(shared.URL)
    out = {"scheme": u.scheme, "has_port": u.port is not None, "has_password": bool(u.password)}
    try:
        addr = socket.getaddrinfo(u.hostname, u.port or 6379, type=socket.SOCK_STREAM)[0][4]
        out["dns"] = True
        with socket.create_connection(addr[:2], timeout=5) as sock:
            out["tcp"] = True
            if u.scheme == "rediss":
                with ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname):
                    out["tls"] = True
    except Exception as e:
        out["failed"] = type(e).__name__
    return out


def main() -> None:
    if "--probe" in sys.argv:
        print("valkey_probe", json.dumps(probe(), sort_keys=True))
    b = shared.health(local_used=0, local_limit=0)
    print("shared_budget", json.dumps(b, sort_keys=True))
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if "--summary" in sys.argv and summary:
        with open(summary, "a") as f:
            if b["backend"] == "valkey":
                f.write(f"**Shared Valio budget (Aiven for Valkey):** role `{b['role']}`, "
                        f"{b['used_this_minute']}/{b['limit']} calls in the last minute "
                        f"(this role may use {b['role_limit']}), by role {b['by_role'] or '{}'}\n\n")
            else:
                f.write(f"**Shared Valio budget:** local (per-process) only"
                        f"{', Valkey unreachable: ' + str(b.get('error')) if b['configured'] else ''}\n\n")


if __name__ == "__main__":
    main()
