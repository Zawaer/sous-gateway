"""Settings, read from the environment (and `.env` in development)."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("VALTAKIRJA_DATA_DIR", ROOT / "data"))
PRODUCTS_JSON = DATA_DIR / "products.json"
RELIABILITY_CSV = DATA_DIR / "delivery-reliability.csv"
CARBON_CSV = DATA_DIR / "carbon-footprint.csv"
CATALOG_DB = DATA_DIR / "catalog.db"
STATE_DB = Path(os.getenv("VALTAKIRJA_STATE_DB", DATA_DIR / "state.db"))

AIMO_API_BASE = os.getenv("VALIO_AIMO_API_BASE", "https://junction26.valioaimo.fi/api")
AIMO_API_KEY = os.getenv("VALIO_AIMO_API_KEY", "")
# Valio Aimo's mock ignores the customer number; real systems would map it per buyer.
AIMO_CUSTOMER = os.getenv("VALIO_AIMO_CUSTOMER", "123")

TZ = ZoneInfo("Europe/Helsinki")
QUOTE_TTL_SECONDS = int(os.getenv("VALTAKIRJA_QUOTE_TTL", "900"))
