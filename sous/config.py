"""Settings, read from the environment (and `.env` in development)."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def env(name: str, default=None):
    """SOUS_<name>, falling back to the pre-rename VALTAKIRJA_<name> so existing .env files keep working."""
    v = os.getenv(f"SOUS_{name}")
    return v if v is not None else os.getenv(f"VALTAKIRJA_{name}", default)


DATA_DIR = Path(env("DATA_DIR", ROOT / "data"))
PRODUCTS_JSON = DATA_DIR / "products.json"
RELIABILITY_CSV = DATA_DIR / "delivery-reliability.csv"
CARBON_CSV = DATA_DIR / "carbon-footprint.csv"
CATALOG_DB = DATA_DIR / "catalog.db"
STATE_DB = Path(env("STATE_DB", DATA_DIR / "state.db"))

AIMO_API_BASE = os.getenv("VALIO_AIMO_API_BASE", "https://junction26.valioaimo.fi/api")
AIMO_API_KEY = os.getenv("VALIO_AIMO_API_KEY", "")
# Valio Aimo's mock ignores the customer number; real systems would map it per buyer.
AIMO_CUSTOMER = os.getenv("VALIO_AIMO_CUSTOMER", "123")

TZ = ZoneInfo("Europe/Helsinki")
QUOTE_TTL_SECONDS = int(env("QUOTE_TTL", "900"))

# Public deployment
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", "").rstrip("/")
# When set, approving/rejecting orders and issuing/revoking mandates need this code
# in the X-Approver-Code header (stands in for the buyer's real login in this demo).
APPROVER_CODE = env("APPROVER_CODE", "")
# When "1", every mandate needs agent authentication (Authorization: Bearer) for quotes, orders and renewals,
# not only those with require_agent_auth=true. See sous/agent_auth.py.
REQUIRE_AGENT_AUTH = env("REQUIRE_AGENT_AUTH", "") in ("1", "true", "yes")
# Buyer-signed mandates (POST /mandates/credential): the first credential for a buyer with no registered keys
# pins its key (trust on first use). "0" = only keys an approver registered (POST /buyers/{id}/keys).
BUYER_KEY_TOFU = env("BUYER_KEY_TOFU", "1") in ("1", "true", "yes")
RATE_LIMIT_PER_MIN =int(env("RATE_LIMIT_PER_MIN", "60"))  # POST requests per client IP
AIMO_CALLS_PER_MIN = int(env("AIMO_CALLS_PER_MIN", "60"))  # protects our Valio API key
MAX_CANDIDATES_PER_QUOTE = 200
# MariaDB (system-versioned state): mysql://user:pass@host:port/db. Empty = local SQLite file.
DB_URL = env("DB_URL", "")

# Judge sandbox (sous/sandbox.py): POST /sandbox gives a visitor private copies of the demo mandates.
SANDBOX_ENABLED = env("SANDBOX", "1") not in ("0", "false", "no")
SANDBOX_TTL_HOURS = float(env("SANDBOX_TTL_HOURS", "24"))
SANDBOX_PER_IP_HOUR = int(env("SANDBOX_PER_IP_HOUR", "3"))  # new sandboxes per client IP per hour
SANDBOX_MAX_ACTIVE = int(env("SANDBOX_MAX_ACTIVE", "50"))
SANDBOX_QUOTES = int(env("SANDBOX_QUOTES", "15"))  # live quote/substitute/suggestion lookups per sandbox
# Live Valio Aimo calls per minute shared by all sandboxes: at most half the gateway's budget by default,
# so visitors can never starve the curated demo.
SANDBOX_CALLS_PER_MIN = int(env("SANDBOX_CALLS_PER_MIN", str(max(1, AIMO_CALLS_PER_MIN // 2))))
