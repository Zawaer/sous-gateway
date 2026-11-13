"""Issue the demo mandates (idempotent). Run: uv run python -m scripts.seed"""

from valtakirja import service as S, signing, store
from valtakirja.mandate import Mandate

MANDATES = [
    Mandate(
        id="M-HOTEL-AURORA",
        buyer="Hotel Aurora, Helsinki",
        agent_id="aurora-purchasing-agent",
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
]


def main():
    for m in MANDATES:
        existing = store.get_mandate(m.id)
        if existing and signing.verify(existing["_jws"], typ="valtakirja-mandate+jws"):
            continue  # keep existing mandates (and their audit history) across restarts
        # missing, or signed with a key we no longer have: (re)issue under the current gateway key
        S.issue_mandate(m)
        print("issued", m.id)


if __name__ == "__main__":
    main()
