"""Which policy decided: a stamp on every decision, signed into the receipt.

A receipt already says what was decided and which rules fired. The stamp adds what decided it, so the buyer,
Valio Aimo or an auditor can later check that the same rules and the same mandate are still the ones published:

  rules_sha256        SHA-256 of the rule register as published at GET /rules (ids, kinds, tiers, owners, texts,
                      mandate fields; canonical JSON, no URLs, so every deployment of the same code gives the same
                      value). GET /rules returns the same `sha256`.
  rules_count         how many rules that register had
  gateway             {version, commit} of the build that decided
  mandate_jws_sha256  SHA-256 of the signed mandate (its compact JWS) the decision was made under, so a later
                      change to the mandate can't be passed off as the one that applied

`decision.policy` is set when the order is decided, and `approval.policy` when a human's approval re-ran the rules.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.metadata
import json
import os

from . import store


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@functools.cache
def rules_fingerprint() -> tuple[str, int]:
    """(sha256, count) of the English rule register without URLs (the same on every host)."""
    from . import rules
    keep = ("id", "kind", "tiers", "owner", "text", "mandate_field", "http_status", "condition")
    reg = sorted(({k: r.get(k) for k in keep} for r in rules.register()), key=lambda r: r["id"])
    blob = json.dumps(reg, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return _sha(blob), len(reg)


@functools.cache
def gateway() -> dict:
    try:
        version = importlib.metadata.version("sous")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return {"version": version, "commit": os.environ.get("SOUS_COMMIT", "").strip() or "unknown"}


def stamp(mandate_id: str | None) -> dict:
    sha, n = rules_fingerprint()
    row = store.get_mandate(mandate_id) if mandate_id else None
    jws = (row or {}).get("_jws")
    return {"rules_sha256": sha, "rules_count": n, "gateway": gateway(),
            "mandate_jws_sha256": _sha(jws.encode()) if jws else None}
