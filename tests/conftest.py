import pytest


@pytest.fixture(autouse=True)
def _no_live_allergen_ai(monkeypatch):
    """No test calls the Gemini Decisions API, even with GEMINI_API_KEY in the shell (tests/test_allergen_ai.py
    switches it on with a fake)."""
    monkeypatch.setenv("SOUS_ALLERGEN_AI", "0")
    # The live re-check at approval (sous/recheck.py) asks Valio Aimo again; tests that approve orders without
    # stubbing Valio keep the old behaviour. tests/test_recheck.py switches it on with fake offers.
    monkeypatch.setenv("SOUS_APPROVAL_RECHECK", "0")
