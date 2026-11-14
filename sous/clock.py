"""The gateway's one business clock, with an optional, clearly disclosed demo clock.

Valio Aimo's mock API returns fixed dates (Stock cut-off Sun 15.11 17:00 Helsinki for delivery Mon 16.11),
so after that instant every Stock product reads "cut-off passed". SOUS_DEMO_NOW (ISO datetime; no offset =
Helsinki time) makes the gateway evaluate business time (cut-offs, quote expiry, approval deadlines, "today"
for daily limits and standing orders) as if it were that instant, advancing in real time from process start
(demo_now + (real_now - start)), or frozen at it with SOUS_DEMO_CLOCK=frozen.

It is never silent: quotes and orders carry `clock` in their signed payload, audit events record the mode,
/health shows it, every REST response has `X-Sous-Clock`, and the MCP instructions say so.

Technical timers (rate limits, caches, circuit breaker, sandbox expiry) and cryptographic validity windows
(buyer-signed credentials, revocation timestamps) stay on real time: use real_now() / time.time() for those.
"""

from __future__ import annotations

from datetime import datetime

from . import config

NOTE = ("Demo clock: cut-offs are evaluated as if it were {now}; Valio's mock returns fixed dates "
        "(Stock cut-off Sun 15.11 17:00 Helsinki), so after that the real clock would make every product "
        "unorderable.")

_demo: datetime | None = None
_frozen = False
_anchor_real: datetime | None = None


def _parse(value: str | datetime) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value.strip())
    return dt.replace(tzinfo=config.TZ) if dt.tzinfo is None else dt.astimezone(config.TZ)


def configure(demo_now: str | datetime | None = None, mode: str | None = None) -> None:
    """Set (or clear, with demo_now=None) the demo clock. Called once at import from the environment;
    tests call it directly. mode: "frozen" or "advancing" (default)."""
    global _demo, _frozen, _anchor_real
    _demo = _parse(demo_now) if demo_now else None
    _frozen = (mode or "").strip().lower() == "frozen"
    _anchor_real = real_now()


def real_now() -> datetime:
    return datetime.now(config.TZ)


def is_demo() -> bool:
    return _demo is not None


def mode() -> str:
    return "demo" if _demo is not None else "real"


def now() -> datetime:
    """Business time: what cut-offs, expiry, deadlines and 'today' are evaluated against."""
    if _demo is None:
        return real_now()
    if _frozen:
        return _demo
    return _demo + (real_now() - _anchor_real)


def info(at: datetime | None = None) -> dict:
    """The `clock` object signed into quotes and orders. `at` = the instant the artifact was evaluated at."""
    t = (at or now()).isoformat(timespec="seconds")
    if _demo is None:
        return {"mode": "real", "now": t}
    return {"mode": "demo", "now": t, "advancing": not _frozen,
            "configured": _demo.isoformat(timespec="seconds"),
            "real_now": real_now().isoformat(timespec="seconds"), "note": NOTE.format(now=t)}


def audit_tag() -> dict:
    """What every audit event records about the clock (the event's own ts stays real time)."""
    return {"mode": "demo", "now": now().isoformat(timespec="seconds")} if _demo is not None else {"mode": "real"}


def header() -> str | None:
    """Value of the X-Sous-Clock response header, or None in real mode."""
    return f"demo {now().isoformat(timespec='seconds')}" if _demo is not None else None


configure(config.env("DEMO_NOW") or None, config.env("DEMO_CLOCK"))
