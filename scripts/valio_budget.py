"""Print the shared Valio Aimo budget as /health shows it (valio_aimo.shared_budget). Never prints the URL.

    uv run python -m scripts.valio_budget            # JSON line
    uv run python -m scripts.valio_budget --summary  # also a Markdown line in $GITHUB_STEP_SUMMARY (CI)
"""

from __future__ import annotations

import json
import os
import sys

from sous import shared


def main() -> None:
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
