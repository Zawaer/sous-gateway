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

# Each demo buyer's usual products: what they have bought from Valio Aimo before (a real buyer's would be imported
# from their order history at onboarding). With first_time_products="ask", any OTHER product needs a human's yes
# the first time. Simulated: the product ids the buyer's routine requests in evals/cases.yaml and scripts/demo.py
# search, quote or order (evals/results.json and the LLM eval runs), plus the hotel's standing-order dairy.
HOTEL_USUAL = [
    "460448", "460885", "461091", "461125", "461159", "461548", "461581", "461770", "461795", "461851", "462072", "462090",
    "462626", "462921", "463038", "463203", "463335", "463380", "463442", "463779", "463814", "464073", "464119", "464146",
    "464179", "464198", "464201", "464237", "464306", "464398", "464401", "464411", "464440", "465039", "465042", "465532",
    "466172", "466288", "466933", "467017", "469119", "469165", "469287", "469330", "469363", "470026", "470084", "470828",
    "472551", "472582", "472633", "472690", "472737", "472749", "472869", "473211", "473257", "473625", "473843", "474014",
    "474849", "475123", "475230", "475243", "475408", "475727", "475728", "475732", "476240", "476548", "476584", "477050",
    "477388", "477424",
]
HOSPITAL_USUAL = [
    "460325", "460428", "460443", "460804", "460825", "460999", "461174", "464205", "464226", "464320", "464411", "464761",
    "465761", "466510", "466670", "466898", "467974", "468319", "468417", "468465", "469040", "469239", "469512", "469840",
    "470340", "470611", "470863", "470889", "471130", "471424", "471582", "471690", "471694", "471881", "472145", "472270",
    "472303", "472387", "472598", "472833", "472879", "473278", "473424", "473485", "473609", "474674", "474724", "474774",
    "474957", "475224", "477227",
]
SCHOOL_USUAL = [
    "460043", "460302", "460396", "460464", "460507", "461115", "461973", "462324", "463149", "463171", "463263", "463313",
    "463351", "463380", "463422", "463883", "463982", "464082", "464097", "464119", "464217", "464700", "464924", "465058",
    "465393", "465831", "466032", "466313", "466510", "466565", "466652", "467464", "467496", "467676", "467880", "468121",
    "468206", "468224", "468269", "468413", "468445", "468498", "468967", "469192", "469209", "469214", "469224", "469306",
    "469337", "469340", "469477", "469549", "470794", "470927", "471112", "471178", "471268", "471450", "471484", "471524",
    "471575", "471847", "472106", "472291", "472395", "472463", "472730", "473189", "473226", "473436", "473491", "474235",
    "474291", "474343", "474758", "476522", "476560", "476679", "476771", "476797", "476856",
]

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
        weekly_limit_eur=25000,
        substitutions="ask",
        first_time_products="ask",
        usual_products=HOTEL_USUAL,
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
        weekly_limit_eur=6000,
        first_time_products="ask",
        usual_products=HOSPITAL_USUAL,
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
        weekly_limit_eur=3000,
        first_time_products="ask",
        usual_products=SCHOOL_USUAL,
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
        weekly_limit_eur=6000,
        first_time_products="ask",  # a new buyer: no usual products yet, so every first order asks
        avoid_allergens=["AM", "AE", "AN", "AP"],
        allow_may_contain=False,
        unknown_allergens="ask",
        substitutions="ask",
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
        weekly_limit_eur=25000,
        substitutions="ask",
        first_time_products="ask",
        usual_products=HOTEL_USUAL,
        terminal_products="silent",
        min_delivery_reliability=0.9,
        later_delivery="ask",
        max_comparison_price_eur={"L": 2.5},
    ),
]


# Demo policy fields the seed brings existing deployments up to date on (re-signed at startup when they differ).
POLICY_FIELDS = ("substitutions", "first_time_products", "usual_products", "weekly_limit_eur")


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
        if existing and signing.verify(existing["_jws"], typ="sous-mandate+jws") and not existing.get("revoked") \
                and not existing.get("credential_sha256") and \
                any(existing.get(f) != getattr(m, f) for f in POLICY_FIELDS):
            # the demo policy changed (e.g. substitutes now always ask): re-sign with the new policy fields, keep
            # the rest as it is (limits a human changed, history); no new agent token
            data = {k: v for k, v in existing.items() if not k.startswith("_")}
            S.issue_mandate(Mandate(**{**data, **{f: getattr(m, f) for f in POLICY_FIELDS}}), False)
            print("updated policy of", m.id, ", ".join(f for f in POLICY_FIELDS if existing.get(f) != getattr(m, f)))
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
