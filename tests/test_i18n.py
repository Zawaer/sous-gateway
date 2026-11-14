"""web/i18n.js: every string the Finnish/English pages ask for exists in both languages. Offline, no browser, no node."""

import json
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"
PAGES = ["approve.html", "break.html", "sign-mandate.html", "agent.html"]
SRC = (WEB / "i18n.js").read_text(encoding="utf-8")
STRINGS = json.loads(SRC.split("/* STRINGS:BEGIN */", 1)[1].split("/* STRINGS:END */", 1)[0])
EN, FI = STRINGS["en"], STRINGS["fi"]


def _keys(html: str) -> set[str]:
    """Keys from data-i18n / data-i18n-html / data-i18n-attr markup and from literal t("…") calls in the page script."""
    keys = set(re.findall(r'data-i18n(?:-html)?="([^"]+)"', html))
    for spec in re.findall(r'data-i18n-attr="([^"]+)"', html):
        keys |= {pair.split("=", 1)[1].strip() for pair in spec.split(",")}
    keys |= set(re.findall(r'\bt\("([a-z0-9_.]*[a-z0-9_])"[,)]', html))  # "break.a." + name: see the prefix test
    keys |= {"tier." + k for k in re.findall(r'I18N\.tier\("([a-z_]+)"\)', html)}
    return keys


def test_both_languages_have_the_same_keys_and_placeholders():
    assert set(EN) == set(FI)
    for k in EN:
        assert EN[k].strip() and FI[k].strip(), k
        assert set(re.findall(r"\{(\w+)\}", EN[k])) == set(re.findall(r"\{(\w+)\}", FI[k])), k


@pytest.mark.parametrize("page", PAGES)
def test_every_key_a_page_uses_exists_in_both_languages(page):
    html = (WEB / page).read_text(encoding="utf-8")
    assert '<script src="i18n.js"></script>' in html and "data-i18n-toggle" in html
    keys = _keys(html)
    assert len(keys) > 10, page
    missing = sorted(k for k in keys if k not in EN or k not in FI)
    assert missing == [], f"{page}: {missing}"


@pytest.mark.parametrize("page", PAGES)
def test_static_english_text_matches_the_english_strings(page):
    """The HTML keeps its English text as the no-JS fallback; it must say what en says."""
    html = (WEB / page).read_text(encoding="utf-8")
    for key, text in re.findall(r'data-i18n="([^"]+)"[^>]*>([^<]+)<', html):
        if "${" not in text:  # a script template, not static markup
            assert " ".join(text.split()) == " ".join(EN[key].split()), (page, key)


def test_dynamic_keys_used_by_prefix_exist():
    # break.html builds break.a.<name>.title/desc; sign-mandate.html shows allergen.<code>; tiers and units come by name
    for name in ["self_approve", "slice", "injection", "tamper_quote", "replay_other_agent", "over_limit", "wrong_allergen"]:
        assert f"break.a.{name}.title" in EN and f"break.a.{name}.desc" in EN
    sign = (WEB / "sign-mandate.html").read_text(encoding="utf-8")
    codes = re.search(r"const ALLERGENS = \[([^\]]+)\]", sign).group(1)
    for code in re.findall(r'"([A-Z]{2})"', codes):
        assert f"allergen.{code}" in EN, code
    for k in ["sign.v_notify", "sign.v_ask", "sign.v_block"] + [f"tier.{x}" for x in ("silent", "notify", "ask", "block")]:
        assert k in EN


def test_html_strings_are_only_markup_we_wrote():
    """data-i18n-html goes to innerHTML: no script, no handlers, single-quoted attributes only."""
    for k, v in {**EN, **FI}.items():
        assert "<script" not in v.lower() and not re.search(r"\son\w+=", v), k
        if "<" in v and not k.endswith("_html"):
            pytest.fail(f"{k} has markup but isn't an _html key")
