"""Render the Sous video's title/motion cards and the stage PNGs, deterministically.

Each card in tools/src/<name>.html sets window.DURATION (from ?dur=) and window.render(t). This script steps
t at 30 fps, screenshots every frame at 1920x1080 and pipes the PNGs into ffmpeg.

    uv run --with playwright --with pillow python tools/render_cards.py                 # all cards + stage
    uv run --with playwright --with pillow python tools/render_cards.py c5_tiers --dur c5_tiers=7.4
    uv run --with playwright --with pillow python tools/render_cards.py --stage          # stage PNGs + mask only
    uv run --with playwright --with pillow python tools/render_cards.py --preview c1_idea=1,4,7.5
    # extra query args for a card (e.g. hide the chapter bar):  --q c5_tiers=bar=none

Chromium once: uv run --with playwright python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "src"
CARDS = {"c0_hook": 7.0, "c1_idea": 8.0, "c5_tiers": 6.0, "c8_numbers": 8.0, "c9_end": 6.0, "a_milk": 0, "a_purpose": 0, "t_milk": 0, "t_purpose": 0, "t_open": 0, "t_cream": 0, "c6_purpose": 11.7, "c9_next": 5.1, "c0_title": 2.6}
FPS = 30
FONTS_JS = """Promise.all([
  document.fonts.load('400 40px Newsreader'), document.fonts.load('500 40px Newsreader'),
  document.fonts.load('400 20px Inter'), document.fonts.load('500 20px Inter'), document.fonts.load('600 20px Inter'),
  document.fonts.load('400 20px "Fragment Mono"')]).then(() => document.fonts.ready).then(() =>
  ['40px Newsreader', '20px Inter', '20px "Fragment Mono"'].map(f => document.fonts.check(f)))"""


def open_page(page, name: str, query: str) -> None:
    page.goto(f"{(SRC / (name + '.html')).as_uri()}?{query}")
    ok = page.evaluate(FONTS_JS)
    if not all(ok):
        print(f"WARNING {name}: a web font did not load {ok}", file=sys.stderr)
    page.evaluate("window.READY ? Promise.resolve(window.READY).then(() => true) : true")
    page.wait_for_timeout(150)


def render_card(page, name: str, dur: float, extra: str) -> Path:
    out = ROOT / "cards" / f"{name}.mp4"
    open_page(page, name, f"dur={dur}" + (f"&{extra}" if extra else ""))
    if not dur:  # the page sets its own length from its query (a_milk: b2/b3/b4, a_purpose: dur)
        dur = page.evaluate("window.DURATION")
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "png", "-i", "-",
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(FPS),
         "-movflags", "+faststart", str(out)], stdin=subprocess.PIPE)
    n = round(dur * FPS)
    for i in range(n):
        page.evaluate(f"render({i / FPS})")
        ff.stdin.write(page.screenshot(type="png"))
    ff.stdin.close()
    if ff.wait():
        raise SystemExit(f"ffmpeg failed for {name}")
    print(f"wrote {out} ({n} frames, {dur:.2f} s)")
    return out


def render_stage(page) -> None:
    from PIL import Image, ImageDraw

    for n in range(6):
        open_page(page, "stage", f"n={n}")
        out = ROOT / "stage" / f"stage_{n}.png"
        page.screenshot(path=str(out), type="png")
        print("wrote", out)
    s = 4  # supersample for a smooth edge
    big = Image.new("L", (1504 * s, 846 * s), 0)
    ImageDraw.Draw(big).rounded_rectangle([0, 0, 1504 * s - 1, 846 * s - 1], radius=16 * s, fill=255)
    mask = big.resize((1504, 846), Image.LANCZOS)
    mask.save(ROOT / "stage" / "mask_1504x846.png")
    print("wrote", ROOT / "stage" / "mask_1504x846.png")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("names", nargs="*", help=f"cards to render (default all): {', '.join(CARDS)}")
    ap.add_argument("--dur", action="append", default=[], help="name=seconds")
    ap.add_argument("--q", action="append", default=[], help="name=extra query string")
    ap.add_argument("--stage", action="store_true", help="render only the stage PNGs and the mask")
    ap.add_argument("--preview", action="append", default=[], help="name=t1,t2,... write PNG stills to tools/preview/")
    a = ap.parse_args()
    durs = dict(CARDS)
    for d in a.dur:
        k, v = d.split("=", 1)
        durs[k] = float(v)
    extra = {}
    for q in a.q:
        k, v = q.split("=", 1)
        extra[k] = v

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1.5)
        page = ctx.new_page()
        if a.preview:
            for spec in a.preview:
                name, ts = spec.split("=", 1)
                open_page(page, name, f"dur={durs[name]}" + (f"&{extra[name]}" if name in extra else ""))
                for t in ts.split(","):
                    page.evaluate(f"render({float(t)})")
                    out = ROOT / "tools" / "preview" / f"{name}_{t}.png"
                    page.screenshot(path=str(out), type="png")
                    print("wrote", out)
        elif a.stage:
            render_stage(page)
        else:
            for name in a.names or list(CARDS):
                render_card(page, name, durs[name], extra.get(name, ""))
            if not a.names:
                render_stage(page)
        b.close()


if __name__ == "__main__":
    main()
