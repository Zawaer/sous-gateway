#!/usr/bin/env python3
"""Write a small, made-up catalog in Valio Aimo's download format (products.json + the two CSVs) to a folder.

The CI smoke job uses tests/fixtures/catalog/ (20 real Valio products) when it is there. The public copy of this
repo leaves Valio's data out (docs/PUBLISHING.md), so the smoke job falls back to this synthetic catalog: invented
products, ids and GTINs, enough for the gateway to start, build its index and answer a search for "maito".
Standard library only (the smoke job has no network and no uv).

Usage: python3 tests/fixtures/make_synthetic_catalog.py <out_dir>
"""

import json
import sys
from pathlib import Path

PRODUCTS = [
    # id, fi, sv, en, category, sales unit, base unit, allergens contained
    ("900001", "Esimerkki kevytmaito 1 l", "Exempel lättmjölk 1 l", "Example semi-skimmed milk 1 l", "10400", "KI", "ST", ["AM"]),
    ("900002", "Esimerkki laktoositon maito 1 l", "Exempel laktosfri mjölk 1 l", "Example lactose-free milk 1 l", "10400", "KI", "ST", ["AM"]),
    ("900003", "Esimerkki kauramaito 1 l", "Exempel havredryck 1 l", "Example oat drink 1 l", "10450", "KI", "ST", ["AW"]),
    ("900004", "Esimerkki maustamaton jogurtti 1 kg", "Exempel naturell yoghurt 1 kg", "Example natural yoghurt 1 kg", "10600", "KI", "ST", ["AM"]),
    ("900005", "Esimerkki voi 500 g", "Exempel smör 500 g", "Example butter 500 g", "10800", "KI", "ST", ["AM"]),
    ("900006", "Esimerkki porkkana 1 kg", "Exempel morot 1 kg", "Example carrot 1 kg", "17400", "KG", "KG", []),
]


def _texts(fi: str, sv: str, en: str) -> list[dict]:
    return [{"value": en, "language": "en"}, {"value": fi, "language": "fi"}, {"value": sv, "language": "sv"}]


def products() -> list[dict]:
    out = []
    for pid, fi, sv, en, cat, su, bu, contains in PRODUCTS:
        out.append({
            "id": pid, "salesUnitGtin": f"0000000{pid}", "salesUnit": su, "baseUnit": bu, "category": cat,
            "allowedLotSize": 1, "deleted": False, "temperatureCondition": 4, "vendorName": "EXAMPLE VENDOR",
            "countryOfOrigin": "fi", "units": [{"unitId": su, "sizeInBaseUnits": 1}],
            "synkkaData": {
                "gtin": f"0000001{pid}", "lastUpdated": "2026-01-01T00:00:00+0200", "brand": "Example",
                "names": _texts(fi, sv, en), "keyIngredients": _texts(fi, sv, en),
                "classifications": [{"name": "allergen", "values": [{"id": a, "unit": "CONTAINS"} for a in contains]}],
            },
        })
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    out = Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "products.json").write_text(json.dumps(products(), ensure_ascii=False, indent=1) + "\n")
    ids = [p[0] for p in PRODUCTS]
    (out / "delivery-reliability.csv").write_text("productId,deliveryReliability\n" + "".join(f"{i},0.98\n" for i in ids))
    (out / "carbon-footprint.csv").write_text("productId,AVG kg CO2-eq/kg\n" + "".join(f"{i},1.2\n" for i in ids))
    print(f"wrote a synthetic {len(ids)}-product catalog to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
