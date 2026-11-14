import pytest


@pytest.fixture(autouse=True)
def _no_live_allergen_ai(monkeypatch):
    """No test calls the Gemini Decisions API, even with GEMINI_API_KEY in the shell (tests/test_allergen_ai.py
    switches it on with a fake)."""
    monkeypatch.setenv("SOUS_ALLERGEN_AI", "0")
