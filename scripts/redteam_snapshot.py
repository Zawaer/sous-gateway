"""Record the Valio Aimo replay snapshot for the red-team run: ONE batched price call and ONE batched availability
call for a fixed subset of at most 1,000 products (scripts/redteam_gen.py --ids), then never again.

    VALIO_AIMO_API_KEY=... uv run python scripts/redteam_snapshot.py   # writes evals/redteam/valio_snapshot.json

The file holds Valio's response items as returned (product id, prices, tax, availability, cut-off): no key, no
header, no customer data. sous/replay.py serves it with SOUS_VALIO_REPLAY=evals/redteam/valio_snapshot.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default="2026-11-16")
    ap.add_argument("--out", default=str(ROOT / "evals" / "redteam" / "valio_snapshot.json"))
    a = ap.parse_args(argv)

    import httpx

    from scripts.redteam_gen import snapshot_ids
    from sous import config

    if not config.AIMO_API_KEY:
        print("VALIO_AIMO_API_KEY is not set", file=sys.stderr)
        return 1
    ids = snapshot_ids(1000)
    assert 0 < len(ids) <= 1000, len(ids)
    body = {"customer": config.AIMO_CUSTOMER, "requestedDate": a.date, "products": ids}
    out = {"recorded_at": datetime.now(config.TZ).isoformat(timespec="seconds"), "requested_date": a.date,
           "source": "Valio Aimo mock API: one batched /price and one batched /availability call", "ids": ids}
    with httpx.Client(base_url=config.AIMO_API_BASE, timeout=60,
                      headers={"X-API-Key": config.AIMO_API_KEY, "User-Agent": "sous-gateway/0.1 (redteam snapshot)"}) as c:
        for kind in ("price", "availability"):
            r = c.post(f"/{kind}", json=body)
            r.raise_for_status()
            payload = r.json()
            out[kind] = {it["productId"]: {**it, "_timeNow": payload.get("timeNow")}
                         for it in payload.get("products", []) if isinstance(it, dict) and it.get("productId")}
            print(f"{kind}: {len(out[kind])} items (HTTP {r.status_code})")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=0, sort_keys=True) + "\n")
    print(f"Wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
