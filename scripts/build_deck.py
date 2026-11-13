"""Render the pitch deck (docs/pitch/deck/index.html) to docs/pitch/deck.pdf, one 1920x1080 page per slide.

    uv run --with playwright python scripts/build_deck.py            # first time: add `python -m playwright install chromium`
    uv run --with playwright python scripts/build_deck.py --png out/ # also write one PNG per slide, for checking layout

Fonts and images are local files next to the HTML; nothing is fetched at render time.
"""

import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "docs/pitch/deck/index.html"
PDF = ROOT / "docs/pitch/deck.pdf"


async def main(png_dir: Path | None) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        await page.goto(HTML.as_uri(), wait_until="networkidle")
        await page.evaluate("document.fonts.ready")
        overflow = await page.evaluate("""() => [...document.querySelectorAll('.slide')].flatMap((s, i) =>
            [...s.querySelectorAll('*')].filter(e => { const r = e.getBoundingClientRect(), b = s.getBoundingClientRect();
              return r.width && (r.right > b.right + 1 || r.bottom > b.bottom + 1); }).map(e => `slide ${i + 1}: <${e.tagName.toLowerCase()}>`))""")
        for o in overflow:
            print("overflow:", o)
        await page.emulate_media(media="print")
        await page.pdf(path=str(PDF), width="1920px", height="1080px", print_background=True, prefer_css_page_size=True)
        n = await page.evaluate("document.querySelectorAll('.slide').length")
        if png_dir:
            png_dir.mkdir(parents=True, exist_ok=True)
            slides = page.locator(".slide")
            for i in range(n):
                await slides.nth(i).screenshot(path=str(png_dir / f"slide-{i + 1:02d}.png"))
        await browser.close()
    print(f"{PDF.relative_to(ROOT)}: {n} slides, {PDF.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--png", type=Path, help="also write one PNG per slide to this folder")
    asyncio.run(main(ap.parse_args().png))
