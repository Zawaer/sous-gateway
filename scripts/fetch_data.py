"""Download Valio Aimo's static data files into data/ if missing, then build the catalog index.

Run: uv run python -m scripts.fetch_data   (needs VALIO_AIMO_API_KEY)
"""

import sys

import httpx

from valtakirja import catalog, config

FILES = {
    "products/download": config.PRODUCTS_JSON,
    "delivery-reliability/download": config.RELIABILITY_CSV,
    "carbon-footprint/download": config.CARBON_CSV,
}


def main() -> int:
    if not config.AIMO_API_KEY:
        print("VALIO_AIMO_API_KEY is not set", file=sys.stderr)
        return 1
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"X-API-Key": config.AIMO_API_KEY, "User-Agent": "valtakirja-gateway/0.1"}
    with httpx.Client(base_url=config.AIMO_API_BASE, headers=headers, follow_redirects=True, timeout=300) as c:
        for path, dest in FILES.items():
            if dest.exists() and dest.stat().st_size > 0:
                continue
            print(f"downloading {path} -> {dest}")
            tmp = dest.with_suffix(dest.suffix + ".part")
            with c.stream("GET", f"/{path}") as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
            tmp.rename(dest)
    if not config.CATALOG_DB.exists():
        print("building catalog index:", catalog.build(), "products")
    return 0


if __name__ == "__main__":
    sys.exit(main())
