"""Issue the demo mandates (idempotent). Run: uv run python -m scripts.seed

M-HOTEL-AURORA-SECURE requires agent authentication: its bearer token is minted at runtime and printed
once, the first time it is issued. Lost it? `uv run python -m scripts.seed --rotate-agent-credential`
prints a new one (the old one stops working). Tokens are never stored in plain text or committed.

All five are curated demo data (demo_public=True): readable without credentials, so judges can browse /ui/
read-only (sous/access.py). A mandate seeded before that flag existed is re-signed once with it set.
"""

import sys

from sous import agent_auth, service as S, signing, store
from sous.mandate import Mandate

MANDATES = [
    Mandate(
        id="M-HOTEL-AURORA",
        buyer="Hotel Aurora, Helsinki",
        agent_id="aurora-purchasing-agent",
        demo_public=True,
        issued_by="Anna Virtanen, F&B manager",
        notify_above_eur=1000,
        auto_approve_limit_eur=2500,
        hard_limit_eur=10000,
        daily_limit_eur=8000,
        substitutions="notify",
        terminal_products="silent",
        min_delivery_reliability=0.9,
        later_delivery="ask",
        max_comparison_price_eur={"L": 2.5},
    ),
    Mandate(
        id="M-HOSPITAL-KITCHEN",
        buyer="Meilahti hospital central kitchen",
        agent_id="meilahti-kitchen-agent",
        demo_public=True,
        issued_by="Mikko Laine, head of catering",
        auto_approve_limit_eur=400,
        hard_limit_eur=3000,
        avoid_allergens=["AM", "AN", "AP"],
        allow_may_contain=False,
        unknown_allergens="ask",
        substitutions="ask",
        terminal_products="ask",
        min_delivery_reliability=0.95,
        later_delivery="block",
    ),
    Mandate(
        id="M-SCHOOL-ESPOO",
        buyer="Espoo school kitchens",
        agent_id="espoo-school-agent",
        demo_public=True,
        issued_by="Liisa Korhonen, food services",
        auto_approve_limit_eur=300,
        hard_limit_eur=1500,
        avoid_allergens=["AW", "AE"],
        unknown_allergens="block",
        substitutions="ask",
    ),
    # Demo scene 5 (scripts/demo.py KAARNA): an event caterer with a vegan, nut-free menu.
    Mandate(
        id="M-KAARNA-CATERING",
        buyer="Kaarna Catering Oy, Espoo",
        agent_id="kaarna-event-agent",
        demo_public=True,
        issued_by="Laura Niemi, kitchen manager",
        notify_above_eur=300,
        auto_approve_limit_eur=600,
        hard_limit_eur=2500,
        daily_limit_eur=3000,
        avoid_allergens=["AM", "AE", "AN", "AP"],
        allow_may_contain=False,
        unknown_allergens="ask",
        substitutions="notify",
        terminal_products="ask",
        min_delivery_reliability=0.9,
        later_delivery="ask",
    ),
    # Same rules as M-HOTEL-AURORA, but the agent must prove who it is (Authorization: Bearer <token>):
    # knowing the agent_id from GET /mandates is not enough to order.
    Mandate(
        id="M-HOTEL-AURORA-SECURE",
        buyer="Hotel Aurora, Helsinki",
        agent_id="aurora-secure-agent",
        demo_public=True,
        issued_by="Anna Virtanen, F&B manager",
        require_agent_auth=True,
        notify_above_eur=1000,
        auto_approve_limit_eur=2500,
        hard_limit_eur=10000,
        daily_limit_eur=8000,
        substitutions="notify",
        terminal_products="silent",
        min_delivery_reliability=0.9,
        later_delivery="ask",
        max_comparison_price_eur={"L": 2.5},
    ),
]


def _print_credential(mid: str, cred: dict) -> None:
    print(f"agent credential for {mid} (agent {cred['agent_id']}), shown once:")
    print(f"  {cred['header']}")


def main(rotate: bool = False):
    for m in MANDATES:
        existing = store.get_mandate(m.id)
        if existing and signing.verify(existing["_jws"], typ="sous-mandate+jws") and \
                bool(existing.get("demo_public")) != m.demo_public and not existing.get("credential_sha256"):
            # same mandate, only the public-demo flag is new: re-sign it as it is (keeps revoked, limits; no new token)
            data = {k: v for k, v in existing.items() if not k.startswith("_")}
            S.issue_mandate(Mandate(**{**data, "demo_public": m.demo_public}), False)
            print("marked", m.id, "demo_public" if m.demo_public else "private")
            existing = store.get_mandate(m.id)
        if existing and signing.verify(existing["_jws"], typ="sous-mandate+jws"):
            # keep existing mandates (and their audit history) across restarts; mint a credential only if
            # the mandate needs one and has none (e.g. SOUS_REQUIRE_AGENT_AUTH was just turned on)
            if agent_auth.required(m) and (rotate or agent_auth.status(m)["credential"] is None):
                _print_credential(m.id, S.agent_credential(m.id, "seed script")["agent_credential"])
            continue
        # missing, or signed with a key we no longer have: (re)issue under the current gateway key
        r = S.issue_mandate(m)
        print("issued", m.id)
        if "agent_credential" in r:
            _print_credential(m.id, r["agent_credential"])


if __name__ == "__main__":
    main(rotate="--rotate-agent-credential" in sys.argv[1:])
