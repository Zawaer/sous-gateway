"""Judge sandbox: anyone can try Sous on the public gateway without touching the curated demo data or
burning the shared Valio Aimo API key.

POST /sandbox (no login; per-IP and global caps) creates a sandbox id like SBX-7Q2K with its own copies of
the hotel, hospital and school demo mandates (M-HOTEL-AURORA-SBX-7Q2K, agent aurora-purchasing-agent-sbx-7q2k),
each requiring agent authentication, valid for 24 h and marked `sandbox`. The visitor gets, once:

- one agent token (valid for the sandbox's three mandates, stored only as a SHA-256 hash, agent_auth.py), and
- one approver token (sbxa_...) that approves or rejects ONLY orders under the sandbox's mandates
  (api.require_approver), sent as X-Approver-Code like the real approver code.

Sandbox orders are real gateway orders: same mandate engine, same signatures, same hash-chained audit log.
They are kept out of the curated views: list endpoints hide sandbox data unless asked for one sandbox
(?sandbox=SBX-7Q2K), and sandbox notifications stay in the in-app feed (no push, no webhook).

Valio budget: each sandbox may make SOUS_SANDBOX_QUOTES live lookups (quotes, substitutes, suggestions that
reached Valio; cached answers are free), and all sandboxes share SOUS_SANDBOX_CALLS_PER_MIN live calls per
minute (default half the gateway's budget), enforced in aimo._take_budget through `metered`. Over either
limit the visitor gets a friendly 429 with retry_after.

Cleanup: expired sandboxes are swept lazily (their mandates revoked, their data hidden);
DELETE /sandbox/{id} (approver) ends one early. Nothing is deleted from the audit log.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from datetime import date, datetime, timedelta

from . import agent_auth, aimo, config, store
from .mandate import Mandate

ID_RE = re.compile(r"^SBX-[0-9A-Z]{4}$")
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O, 1/I/L
APPROVER_PREFIX = "sbxa_"
# (scenario, curated mandate copied from scripts/seed.py)
SCENARIOS = [("hotel", "M-HOTEL-AURORA"), ("hospital", "M-HOSPITAL-KITCHEN"), ("school", "M-SCHOOL-ESPOO")]

_SQLITE = """CREATE TABLE IF NOT EXISTS sandbox (
  id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, created_ts REAL NOT NULL,
  expires_at TEXT NOT NULL, expires_ts REAL NOT NULL, ip_sha256 TEXT NOT NULL, approver_sha256 TEXT NOT NULL,
  quotes_used INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL)"""
_MARIADB = """CREATE TABLE IF NOT EXISTS sandbox (
  id VARCHAR(16) PRIMARY KEY, status VARCHAR(16) NOT NULL, created_at VARCHAR(40) NOT NULL,
  created_ts DOUBLE NOT NULL, expires_at VARCHAR(40) NOT NULL, expires_ts DOUBLE NOT NULL,
  ip_sha256 CHAR(64) NOT NULL, approver_sha256 CHAR(64) NOT NULL, quotes_used INT NOT NULL DEFAULT 0,
  data LONGTEXT NOT NULL, INDEX (ip_sha256), INDEX (status))"""

_ready_for = None
_create_lock = threading.Lock()
_last_sweep = 0.0
_clock = time.time  # patched in tests


def _err(code: str, message: str, status: int, retry_after: int | None = None):
    from . import service as S
    return S.GatewayError(code, message, status, retry_after)


def _ensure() -> None:
    global _ready_for
    with store._lock:
        conn = store.db()
        if _ready_for is conn:
            return
        store._exec(_MARIADB if config.DB_URL else _SQLITE)
        _ready_for = conn


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def normalize(sandbox_id: str) -> str:
    sid = (sandbox_id or "").strip().upper()
    if not ID_RE.match(sid):
        raise _err("invalid_sandbox", f"'{sandbox_id}' is not a sandbox id (they look like SBX-7Q2K).", 400)
    return sid


def owner(mandate_id: str | None) -> str | None:
    """The sandbox a mandate belongs to: its signed `sandbox` field, if that sandbox is registered and lists it.
    Never inferred from the id, so nobody can put their data into (or out of) a sandbox by naming."""
    m = store.get_mandate(mandate_id) if mandate_id else None
    sid = (m or {}).get("sandbox")
    row = _row(sid) if sid else None
    return sid if row and mandate_id in row["data"]["mandates"].values() else None


def index() -> dict:
    """Every registered sandbox's mandate ids and buyer names (any status), from the sandbox table."""
    _ensure()
    out: dict = {"mandates": {}, "buyers": {}, "ids": set()}
    tpl = None
    for r in store._exec("SELECT id, data FROM sandbox")[0]:
        sid, data = r["id"], json.loads(r["data"])
        out["ids"].add(sid)
        for mid in data["mandates"].values():
            out["mandates"][mid] = sid
        buyers = data.get("buyers")
        if buyers is None:
            tpl = tpl or _templates()
            buyers = {name: f"{tpl[base].buyer} (sandbox {sid})" for name, base in SCENARIOS}
        for b in buyers.values():
            out["buyers"][b] = sid
    return out


def where(scope: str, cols: dict[str, str]) -> tuple[str, list]:
    """SQL condition for list queries: scope "" hides every sandbox's rows, "SBX-XXXX" keeps only that one's.
    `cols` maps a column to what it holds: "buyer" (buyer name) or "subject" (mandate or sandbox id).
    Matches exact registered values only (see index), never a substring of a self-chosen id."""
    idx = index()
    values = {"buyer": idx["buyers"], "subject": {**idx["mandates"], **{i: i for i in idx["ids"]}}}
    parts, args = [], []
    for col, kind in cols.items():
        vals = [v for v, sid in values[kind].items() if not scope or sid == scope]
        if vals:
            parts.append(f"COALESCE({col}, '') IN ({','.join('?' * len(vals))})")
            args += vals
    if not parts:
        return ("", []) if not scope else (" AND 1=0", [])
    cond = " OR ".join(parts)
    return (f" AND NOT ({cond})", args) if not scope else (f" AND ({cond})", args)


def _row(sid: str) -> dict | None:
    _ensure()
    rows = store._exec("SELECT * FROM sandbox WHERE id = ?", (sid,))[0]
    if not rows:
        return None
    r = dict(rows[0])
    r["data"] = json.loads(r["data"])
    return r


def _state(row: dict) -> str:
    return "expired" if row["status"] == "active" and _clock() >= row["expires_ts"] else row["status"]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, config.TZ).isoformat(timespec="seconds")


# --- lifecycle ----------------------------------------------------------------

def _close(row: dict, status: str, by: str, actor_type: str) -> None:
    """Revoke the sandbox's mandates and mark it `status` (expired / deleted)."""
    from . import service as S
    _, n, _ = store._exec("UPDATE sandbox SET status = ? WHERE id = ? AND status = 'active'", (status, row["id"]))
    if n != 1:
        return  # someone else closed it first
    for mid in row["data"]["mandates"].values():
        try:
            m = S._mandate(mid)
            if not m.revoked:  # credentials stay, so the agent gets "sandbox_expired" rather than an auth error
                S.revoke_mandate(mid, by)
        except S.GatewayError:
            pass
    store.audit(f"sandbox.{status}", by, actor_type, row["id"], None,
                {"sandbox": row["id"], "mandates": list(row["data"]["mandates"].values()),
                 "quotes_used": row["quotes_used"]})


def sweep(force: bool = False) -> int:
    """Close sandboxes past their expiry (at most once a minute unless forced). Returns how many."""
    global _last_sweep
    now = _clock()
    if not force and now - _last_sweep < 60:
        return 0
    _last_sweep = now
    _ensure()
    rows = store._exec("SELECT id FROM sandbox WHERE status = 'active' AND expires_ts <= ?", (now,))[0]
    for r in rows:
        if (row := _row(r["id"])) is not None:
            _close(row, "expired", "sandbox-expiry", "system")
    return len(rows)


def _templates() -> dict[str, Mandate]:
    from scripts.seed import MANDATES
    return {m.id: m for m in MANDATES}


def next_delivery_day(today: date | None = None) -> date:
    """Next Monday (tomorrow on a Sunday), the demo's usual delivery day."""
    today = today or datetime.fromtimestamp(_clock(), config.TZ).date()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def create(ip: str, base_url: str = "") -> dict:
    """A new sandbox for one visitor. Tokens are in this response only."""
    from . import service as S
    if not config.SANDBOX_ENABLED:
        raise _err("sandbox_disabled", "Sandboxes are turned off on this gateway.", 404)
    sweep(force=True)
    now = _clock()
    ip_hash = _sha(f"sous-sandbox:{ip}")
    with _create_lock:
        _ensure()
        recent = [r["created_ts"] for r in store._exec(
            "SELECT created_ts FROM sandbox WHERE ip_sha256 = ? AND created_ts > ?", (ip_hash, now - 3600))[0]]
        if len(recent) >= config.SANDBOX_PER_IP_HOUR:
            wait = int(min(recent) + 3600 - now) + 1
            raise _err("sandbox_rate_limited",
                       f"You already made {len(recent)} sandboxes in the last hour (the limit is "
                       f"{config.SANDBOX_PER_IP_HOUR}). Keep using one of those, or try again in "
                       f"{max(1, wait // 60)} min.", 429, wait)
        active = [r["expires_ts"] for r in store._exec(
            "SELECT expires_ts FROM sandbox WHERE status = 'active' AND expires_ts > ?", (now,))[0]]
        if len(active) >= config.SANDBOX_MAX_ACTIVE:
            wait = int(min(active) - now) + 1
            raise _err("sandbox_full", f"All {config.SANDBOX_MAX_ACTIVE} sandboxes are in use right now; "
                                       "please try again later or ask the Sous team.", 429, min(wait, 3600))
        while True:
            sid = "SBX-" + "".join(secrets.choice(_ALPHABET) for _ in range(4))
            if _row(sid) is None:
                break
        approver = APPROVER_PREFIX + secrets.token_urlsafe(24)
        expires = now + config.SANDBOX_TTL_HOURS * 3600
        tpl = _templates()
        mandates = {name: f"{base}-{sid}" for name, base in SCENARIOS}
        data = {"mandates": mandates,
                "agents": {name: f"{tpl[base].agent_id}-{sid.lower()}" for name, base in SCENARIOS},
                "buyers": {name: f"{tpl[base].buyer} (sandbox {sid})" for name, base in SCENARIOS}}
        store._exec("INSERT INTO sandbox (id, status, created_at, created_ts, expires_at, expires_ts, ip_sha256, "
                    "approver_sha256, quotes_used, data) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sid, "active", _iso(now), now, _iso(expires), expires, ip_hash, _sha(approver), 0,
                     json.dumps(data)))
    valid_until = datetime.fromtimestamp(expires, config.TZ)
    token = None
    out_mandates = []
    for name, base in SCENARIOS:
        t = tpl[base]
        m = Mandate(**{**t.model_dump(), "id": mandates[name], "agent_id": data["agents"][name],
                       "buyer": data["buyers"][name], "valid_until": valid_until, "revoked": False,
                       "require_agent_auth": True, "sandbox": sid,
                       "demo_public": False})  # private to the sandbox's token holders (sous/access.py)
        S.issue_mandate(m, False)
        token = agent_auth.mint(m, f"sandbox {sid}", token=token)["token"]
        out_mandates.append({"scenario": name, "mandate_id": m.id, "agent_id": m.agent_id, "buyer": m.buyer,
                             "rules": _rules(m)})
    store.audit("sandbox.created", "sandbox", "system", sid, None,
                {"sandbox": sid, "mandates": list(mandates.values()), "expires_at": _iso(expires),
                 "quotes_limit": config.SANDBOX_QUOTES})
    base_url = (config.PUBLIC_BASE_URL or base_url).rstrip("/")
    return {
        "sandbox": {"id": sid, "status": "active", "expires_at": _iso(expires),
                    "quotes_limit": config.SANDBOX_QUOTES, "status_url": f"{base_url}/sandbox/{sid}"},
        "agent_token": token,
        "approver_token": approver,
        "tokens_note": ("Shown once; the gateway keeps only their SHA-256 hashes. agent_token is the agents' "
                        "Authorization: Bearer credential for all three mandates. approver_token approves or "
                        "rejects orders under these mandates only: send it as X-Approver-Code, or paste it when "
                        "the UI asks for the approver code."),
        "mandates": out_mandates,
        "quickstart": quickstart(sid, base_url, token, approver, out_mandates),
        "prompts": prompts(out_mandates),
    }


def _rules(m: Mandate) -> str:
    parts = [f"auto-approve up to €{m.auto_approve_limit_eur:,.0f}"]
    if m.notify_above_eur is not None:
        parts.insert(0, f"FYI above €{m.notify_above_eur:,.0f}")
    if m.hard_limit_eur is not None:
        parts.append(f"refused above €{m.hard_limit_eur:,.0f}")
    if m.avoid_allergens:
        parts.append("avoids " + ", ".join(m.avoid_allergens))
    parts.append(f"substitutes: {m.substitutions}")
    return "; ".join(parts)


def quickstart(sid: str, base: str, token: str, approver: str, mandates: list[dict]) -> dict:
    hotel = next(m for m in mandates if m["scenario"] == "hotel")
    body = json.dumps({"agent_id": hotel["agent_id"], "mandate_id": hotel["mandate_id"],
                       "delivery_date": next_delivery_day().isoformat(),
                       "items": [{"product_ids": ["464119", "476240", "464201"], "amount": 100, "unit": "L"}]})
    return {
        "claude_code": f'claude mcp add --transport http sous {base}/mcp/ --header "Authorization: Bearer {token}"',
        "mcp_url": f"{base}/mcp/",
        "curl_quote": (f"curl -s {base}/quotes -H 'Authorization: Bearer {token}' "
                       f"-H 'Content-Type: application/json' -d '{body}'"),
        "curl_order": (f"curl -s {base}/orders -H 'Authorization: Bearer {token}' -H 'Content-Type: application/json' "
                       f"-d '{{\"agent_id\": \"{hotel['agent_id']}\", \"mandate_id\": \"{hotel['mandate_id']}\", "
                       f"\"quote_id\": \"<quote.id>\", \"idempotency_key\": \"try-1\"}}'"),
        "curl_approve": (f"curl -s -X POST {base}/orders/<PO-id>/approve -H 'X-Approver-Code: {approver}' "
                         "-H 'Content-Type: application/json' -d '{\"by\": \"<your name>\"}'"),
        "seller_ui": f"{base}/ui/?sandbox={sid}#/inbox",
        "buyer_ui": f"{base}/buyer/?sandbox={sid}",
        "status": f"{base}/sandbox/{sid}",
    }


def prompts(mandates: list[dict]) -> list[dict]:
    by = {m["scenario"]: m for m in mandates}
    day = next_delivery_day()
    when = f"{day:%A} {day.isoformat()}"
    tomorrow = datetime.fromtimestamp(_clock(), config.TZ).date() + timedelta(days=1)

    def who(s):
        return f"Using the sous MCP tools, as agent {by[s]['agent_id']} under mandate {by[s]['mandate_id']}: "

    return [
        {"title": "Hotel: milk within the mandate",
         "prompt": who("hotel") + f"order 1,000 litres of lactose-free milk for delivery on {when}.",
         "expect": "Searched free, priced live, split across pack sizes; ordered with an FYI to the hotel "
                   "(notify tier: over €1,000, under the €2,500 the agent may spend alone)."},
        {"title": "Hospital: a substitute needs a human",
         "prompt": who("hospital") + "our usual Bravo Chef chopped tomatoes 400 g (product 468376) won't do this "
                   f"week. Find substitutes for 40 packs for {when}, pick the best one and order 4 packs of it "
                   "as a substitute.",
         "expect": "Good/better/best substitutes with their compromises; the order is held for a human "
                   "(ask tier: substitutes need approval, and any product without allergen data is flagged). "
                   "Approve or reject it in the seller UI with your approver token."},
        {"title": "Past the cut-off",
         "prompt": who("hotel") + f"order 50 litres of exactly product 464119 for delivery tomorrow "
                   f"({tomorrow.isoformat()}), no other product.",
         "expect": "Refused with the rule that fired when the order cut-off for tomorrow has passed "
                   "(availability.cutoff_passed), or a later delivery date flagged for a human. Never ordered "
                   "silently."},
        {"title": "School: over the budget",
         "prompt": who("school") + f"order 1,500 litres of lactose-free milk for {when}.",
         "expect": "Refused: the total is above the school's €1,500 hard limit (spend.hard_limit). The agent "
                   "cannot override it."},
    ]


def get(sandbox_id: str) -> dict:
    """Public status: no tokens, just what the visitor needs to keep going."""
    sid = normalize(sandbox_id)
    sweep()
    row = _row(sid)
    if row is None:
        raise _err("sandbox_not_found", f"No sandbox {sid}.", 404)
    mids = row["data"]["mandates"]
    orders = [o for o in store.list_orders() if o["mandate_id"] in mids.values()]
    counts: dict[str, int] = {}
    for o in orders:
        counts[o["status"]] = counts.get(o["status"], 0) + 1
    return {"id": sid, "status": _state(row), "created_at": row["created_at"], "expires_at": row["expires_at"],
            "quotes_used": row["quotes_used"], "quotes_limit": config.SANDBOX_QUOTES,
            "quotes_left": max(0, config.SANDBOX_QUOTES - row["quotes_used"]),
            "mandates": [{"scenario": k, "mandate_id": v, "agent_id": row["data"]["agents"][k]}
                         for k, v in mids.items()],
            "orders": {"total": len(orders), "by_status": counts,
                       "recent": [{k: o.get(k) for k in ("id", "mandate_id", "status", "total_ex_vat",
                                                          "approval_url", "created_at")} for o in orders[:20]]}}


def delete(sandbox_id: str, by: str) -> dict:
    sid = normalize(sandbox_id)
    row = _row(sid)
    if row is None:
        raise _err("sandbox_not_found", f"No sandbox {sid}.", 404)
    _close(row, "deleted", by, "human")
    return get(sid)


# --- access and isolation -------------------------------------------------------

def may_approve(token: str | None, order_id: str | None) -> bool:
    """Whether a sandbox approver token may decide this order (only orders under its own sandbox's mandates)."""
    if not token or not token.startswith(APPROVER_PREFIX) or not order_id:
        return False
    o = store.get_order(order_id)
    sid = owner(o["mandate_id"]) if o else None
    row = _row(sid) if sid else None
    return bool(row and _state(row) == "active" and hmac.compare_digest(row["approver_sha256"], _sha(token)))


def list_scope(sandbox_id: str | None) -> str | None:
    """For list endpoints: "" = curated data only (the default), "SBX-XXXX" = only that sandbox,
    None = show nothing (an expired, deleted or unknown sandbox is hidden)."""
    if not sandbox_id:
        return ""
    sid = normalize(sandbox_id)
    sweep()
    row = _row(sid)
    return sid if row and _state(row) == "active" else None


def keep(scope: str, mandate_id: str | None, idx: dict) -> bool:
    """Whether a row under `mandate_id` belongs in a list with this scope (see list_scope); idx = index()."""
    return idx["mandates"].get(mandate_id or "", "") == scope


def authorize(m, agent_id: str, token: str | None, operation: str) -> None:
    """Live lookups (substitutes, suggestions) under a sandbox mandate need the sandbox's agent token or its
    approver token, so nobody else can drain the sandbox's Valio budget. No-op for other mandates."""
    sid = getattr(m, "sandbox", None)
    if not sid:
        return
    row = _row(sid)
    if token and token.startswith(APPROVER_PREFIX) and row and \
            hmac.compare_digest(row["approver_sha256"], _sha(token)):
        return
    try:
        agent_auth.check(m, agent_id, token, operation)
    except agent_auth.AuthError as e:
        raise _err(e.code, e.message, e.status) from None


@contextlib.contextmanager
def metered(mandate: Mandate | None):
    """Run a live lookup for a sandbox mandate under the sandbox's Valio budget (no-op for other mandates)."""
    sid = getattr(mandate, "sandbox", None)
    if not sid:
        yield
        return
    row = _row(sid)
    if row is None or _state(row) != "active":
        raise _err("sandbox_expired", f"Sandbox {sid} has expired or was closed; create a new one (POST /sandbox).",
                   410)
    m = aimo.Meter(sid, allowed=row["quotes_used"] < config.SANDBOX_QUOTES)
    reset = aimo.meter.set(m)
    try:
        yield
    finally:
        aimo.meter.reset(reset)
        if m.live:
            store._exec("UPDATE sandbox SET quotes_used = quotes_used + 1 WHERE id = ?", (sid,))
