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

# Public deployment
PUBLIC_BASE_URL = os.getenv("VALTAKIRJA_PUBLIC_BASE_URL", "").rstrip("/")
# When set, approving/rejecting orders and issuing/revoking mandates need this code
# in the X-Approver-Code header (stands in for the buyer's real login in this demo).
APPROVER_CODE = os.getenv("VALTAKIRJA_APPROVER_CODE", "")
# When "1", every mandate needs agent authentication (Authorization: Bearer) for quotes, orders and renewals,
# not only those with require_agent_auth=true. See valtakirja/agent_auth.py.
REQUIRE_AGENT_AUTH = os.getenv("VALTAKIRJA_REQUIRE_AGENT_AUTH", "") in ("1", "true", "yes")
RATE_LIMIT_PER_MIN = int(os.getenv("VALTAKIRJA_RATE_LIMIT_PER_MIN", "60"))  # POST requests per client IP
AIMO_CALLS_PER_MIN = int(os.getenv("VALTAKIRJA_AIMO_CALLS_PER_MIN", "60"))  # protects our Valio API key
MAX_CANDIDATES_PER_QUOTE = 200
# MariaDB (system-versioned state): mysql://user:pass@host:port/db. Empty = local SQLite file.
DB_URL = os.getenv("VALTAKIRJA_DB_URL", "")
