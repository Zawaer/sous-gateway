"""Assemble the Sous demo video: screen recordings (browser + terminal), two title slates, duo VO, music, sound.

    uv run --with playwright --with pillow python tools/assemble.py [--env staging] [--skip-cards] [--out NAME]

Timeline = SEGMENTS. Browser shots get a browser bar with their real URL (shots/<env>/<shot>.meta.json) and a slow
push-in; terminal scenes are tools/src/t_*.html; captions (data/captions.json) and the flyer's step labels are PNG
overlays rendered in Inter.
"""
from __future__ import annotations

import argparse, json, pathlib, subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
FPS, W, H = 30, 1920, 1080
BAR = 72  # browser bar height; page area 1920x1008

CREAM_Q = "b2=4.7&b3=7.7&b4=13.2&qa=4.2&ka=6.2&sa=0.3&pa=4.3&fa=8.0&oa=11.0"
SEGMENTS = [
    dict(id="s0t", card="c0_title", q="bar=none", dur=2.6, vo=[(0, 0.4)]),
    dict(id="s0a", shot="r1_live_long", ss=0.3, dur=4.6, sfx=[("whoosh", 0.0)]),
    dict(id="s0b", shot="r1_live_long", ss=5.2, dur=6.4, vo=[(1, 0.1)]),
    dict(id="s1", shot="try_run", ss=3.4, dur=12.3, vo=[(2, 0.2)], sfx=[("whoosh", 0.0)], zoom=(0.26, 0.5, 0.42, 0.0),
         chips=[(3.0, 2.7, "Must-builds 1 and 2", "lookup, then request and order at a fixed price")]),
    dict(id="s2", card="t_cream", q=CREAM_Q, dur=25.6, vo=[(3, 0.2), (4, 12.4), (5, 23.4)], sfx=[("whoosh", 0.0), ("seal", 20.2)],
         chips=[(4.4, 3.0, "Lookup", "structured search of products, prices and availability"),
                (10.6, 2.6, "Bonus idea", "handling exceptions: the usual cream's cut-off has passed"),
                (16.8, 3.0, "Order", "commit at a fixed price, with a signed quote"),
                (20.3, 3.0, "Bonus idea", "comparing more than price: reliability, origin, CO₂")]),
    dict(id="s3", shot="r4_order", ss=1.2, dur=2.6),
    dict(id="s4", card="c6_purpose", q="r1=0.1&r2=2.9&a0=4.5&i1=7.0&i2=8.2&i3=9.2&i4=10.2", dur=11.7, sfx=[("whoosh", 0.0)],
         chips=[(0.4, 3.0, "Bonus idea", "handling exceptions: what a product is for decides the substitute")]),
    dict(id="s5a", shot="r7_seller_wait", ss=1.0, dur=4.0, vo=[(6, 0.2)], sfx=[("whoosh", 0.0)], zoom=(0.07, 0.62, 0.45, 0.6),
         chips=[(0.4, 3.2, "Approval", "what a human signs off: the buyer decides, not Valio")]),
    dict(id="s5b", shot="r8_fi", ss=0.0, dur=3.8,
         chips=[(1.7, 2.0, "Bonus idea", "explaining decisions: a plain reason, in Finnish too")]),
    dict(id="s5c", phone="r6_phone_approve", ss=3.6, dur=3.9, backdrop="kitchen", sfx=[("ping", 1.4)]),
    dict(id="s6a", shot="try_run", ss=37.3, to=42.1, dur=6.4, vo=[(7, 0.2)], sfx=[("whoosh", 0.0)], zoom=(0.26, 0.5, 0.6, 0.0)),
    dict(id="s6b", shot="r7_break", ss=2.6, dur=2.6, sfx=[("deny", 0.0)], zoom=(0.07, 0.5, 0.25, 0.0)),
    dict(id="s7a", shot="r1_mandate", ss=0.9, dur=2.8, vo=[(8, 0.2)], sfx=[("whoosh", 0.0)], zoom=(0.08, 0.48, 0.26, 0.2)),
    dict(id="s7b", shot="r5_tiers", ss=0.4, dur=2.2, zoom=(0.05, 0.5, 0.33, 0.0)),
    dict(id="s7c", shot="r5_rule", ss=1.6, dur=3.4, zoom=(0.06, 0.73, 0.18, 0.0)),
    dict(id="s8a", card="c8_numbers", q="bar=none", dur=6.4, vo=[(9, 0.2)], sfx=[("whoosh", 0.0)]),
    dict(id="s8b", card="t_open", q="c2=2.4&title=zsh%20%E2%80%94%20~", dur=4.6),
    dict(id="s9", card="c9_next", q="i1=0.55&i2=1.75&i3=4.85", dur=6.3, vo=[(10, 0.2)], sfx=[("whoosh", 0.0)]),
    dict(id="s10", card="c9_end", dur=5.6, vo=[(11, 0.4)], cap="none", sfx=[("whoosh", 0.0)]),
]
SFX_VOL = {"whoosh": 0.22, "seal": 0.45, "buzz": 0.5, "ping": 0.45, "deny": 0.5, "type": 0.25}
FONTS = '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Newsreader:opsz,wght@6..72,400&display=block" rel="stylesheet">'


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(" ".join(map(str, cmd))[:400] + "\n" + r.stderr[-1500:])


def dur_of(p: pathlib.Path) -> float:
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                                capture_output=True, text=True).stdout)


def render_cards():
    names, args = [], []
    for s in SEGMENTS:
        if "card" in s:
            names.append(s["card"]); args += ["--dur", f"{s['card']}={s['dur']}"]
            if s.get("q"):
                args += ["--q", f"{s['card']}={s['q']}"]
    run(["uv", "run", "-q", "--with", "playwright", "--with", "pillow", "python", str(ROOT / "tools" / "render_cards.py"), *names, *args])


# ---------------------------------------------------------------- PNG overlays (browser bar, captions, chips)
BAR_HTML = """<!doctype html><html><head><meta charset="utf-8">""" + FONTS + """<style>
html,body{margin:0;width:1280px;height:48px;background:#dfe1e5;font-family:Inter,sans-serif}
.b{display:flex;align-items:center;height:48px;padding:0 14px;gap:14px}
.tl{display:flex;gap:8px}.tl i{width:12px;height:12px;border-radius:50%;display:block}
.nav{display:flex;gap:16px;color:#5f6368;font-size:17px;margin-left:6px}
.om{flex:1;height:32px;border-radius:16px;background:#fff;display:flex;align-items:center;gap:9px;padding:0 14px;font-size:14px;color:#5f6368;white-space:nowrap;overflow:hidden}
.om b{color:#202124;font-weight:400}
.lk{width:12px;height:13px}
.pf{width:26px;height:26px;border-radius:50%;background:#17362b;color:#fff;font-size:12px;font-weight:600;display:flex;align-items:center;justify-content:center}
</style></head><body><div class="b"><div class="tl"><i style="background:#ff5f56"></i><i style="background:#ffbd2e"></i><i style="background:#27c93f"></i></div>
<div class="nav"><span>&#8592;</span><span>&#8594;</span><span>&#8635;</span></div>
<div class="om"><svg class="lk" viewBox="0 0 12 14"><rect x="1" y="6" width="10" height="7.5" rx="1.5" fill="#5f6368"/><path d="M3.3 6V4.3a2.7 2.7 0 0 1 5.4 0V6" stroke="#5f6368" stroke-width="1.5" fill="none"/></svg><span id="u"></span></div>
<div class="pf">R</div></div></body></html>"""

CAP_HTML = """<!doctype html><html><head><meta charset="utf-8">""" + FONTS + """<style>
html,body{margin:0;width:1280px;height:84px;background:transparent}
.w{width:1280px;height:84px;display:flex;align-items:center;justify-content:center}
.p{max-width:1040px;text-align:center;font:500 21px/1.32 Inter,sans-serif;color:#fff;background:rgba(17,17,17,.78);padding:7px 16px;border-radius:10px;box-decoration-break:clone;-webkit-box-decoration-break:clone}
</style></head><body><div class="w"><span class="p" id="c"></span></div></body></html>"""

CHIP_HTML = """<!doctype html><html><head><meta charset="utf-8">""" + FONTS + """<style>
html,body{margin:0;width:900px;height:52px;background:transparent}
.c{display:inline-flex;align-items:center;gap:10px;height:40px;margin:6px;padding:0 16px 0 6px;border-radius:999px;background:#005670;color:#fff;
font:400 16px/1 Inter,sans-serif;box-shadow:0 6px 18px rgba(0,0,0,.18)}
.c b{font-weight:600;background:#fff;color:#005670;border-radius:999px;padding:7px 11px}
.c.bonus{background:#fddb32;color:#23282d}.c.bonus b{background:#23282d;color:#fddb32}
</style></head><body><span class="c" id="c"><b id="h"></b><span id="t"></span></span></body></html>"""


def overlays(env: str):
    from playwright.sync_api import sync_playwright
    caps = json.loads((ROOT / "data" / "captions.json").read_text())
    od = ROOT / "build" / "ov"; od.mkdir(parents=True, exist_ok=True)
    cap_items, chip_items = [], []
    for seg in SEGMENTS:
        if seg.get("cap") != "none":
            for beat, off in seg.get("vo", []):
                for c in caps[str(beat)]["captions"]:
                    cap_items.append((seg["t"] + off + c["t0"], seg["t"] + off + c["t1"] + 0.15, c["text"]))
        for (t0, d, head, text) in seg.get("chips", []):
            chip_items.append((seg["t"] + t0, d, head, text))
    caps_out, chips_out = [], []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 48}, device_scale_factor=1.5)
        for seg in SEGMENTS:
            if "shot" in seg:
                url = json.loads((ROOT / "shots" / env / f"{seg['shot']}.meta.json").read_text())["url"]
                host, _, rest = url.replace("https://", "").partition("/")
                pg.set_content(BAR_HTML, wait_until='domcontentloaded'); pg.evaluate('document.fonts.ready.then(() => 1)')
                pg.evaluate("([h, r]) => { document.getElementById('u').innerHTML = '<b>' + h + '</b>/' + r.replace(/</g, '&lt;'); }", [host, rest])
                pg.wait_for_timeout(120)
                pg.screenshot(path=str(od / f"bar_{seg['id']}.png"))
        b.close()
    cap_items.sort()
    cap_items = [(a, min(z, cap_items[i + 1][0]) if i + 1 < len(cap_items) else z, t) for i, (a, z, t) in enumerate(cap_items)]
    return cap_items, chip_items


BAND_Y, BAND_H = 846, 234  # bottom strip that carries the caption pill and the step label
BAND_HTML = """<!doctype html><html><head><meta charset="utf-8">""" + FONTS + """<style>
html,body{margin:0;width:1280px;height:156px;background:transparent;overflow:hidden;font-family:Inter,sans-serif}
#cw{position:absolute;left:0;right:0;top:69px;height:84px;display:flex;align-items:center;justify-content:center}
#cap{max-width:1040px;text-align:center;font:500 21px/1.32 Inter,sans-serif;color:#fff;background:rgba(17,17,17,.78);padding:7px 16px;border-radius:10px;
box-decoration-break:clone;-webkit-box-decoration-break:clone}
#chip{position:absolute;left:27px;top:20px;display:inline-flex;align-items:center;gap:12px;height:44px;padding:0 18px 0 14px;border-radius:12px;background:rgba(248,248,243,.96);color:#111;border:1px solid rgba(0,0,0,.08);box-shadow:0 8px 24px rgba(0,0,0,.10);white-space:nowrap;font:400 16px/1 Inter,sans-serif}
#chip i{width:9px;height:9px;border-radius:50%;background:#c8e05a;box-shadow:0 0 0 3px rgba(200,224,90,.25)}
#chip b{font:400 21px/1 Newsreader,Georgia,serif;font-weight:400;letter-spacing:-.01em;color:#111}
#chip #ct{color:#6b6b64}
#chip.bonus i{background:#7a5c45;box-shadow:0 0 0 3px rgba(122,92,69,.18)}
</style></head><body><div id="cw"><span id="cap"></span></div><span id="chip"><i></i><b id="h"></b><span id="ct"></span></span>
<script>
window.setState = (cap, ca, chip, ka, ky) => {
  const c = document.getElementById('cap'); c.textContent = cap || ''; c.style.display = cap ? '' : 'none'; c.style.opacity = ca;
  const k = document.getElementById('chip'); k.style.display = chip ? '' : 'none';
  if (chip) { document.getElementById('h').textContent = chip[0]; document.getElementById('ct').textContent = chip[1];
    k.className = chip[0].startsWith('Bonus') ? 'bonus' : ''; k.style.opacity = ka; k.style.transform = `translateY(${ky}px)`; }
};
</script></body></html>"""


def render_band(caps, chips, total) -> pathlib.Path:
    """Transparent strip (1920x234) with the active caption and step label at every frame; identical frames reuse one shot."""
    from playwright.sync_api import sync_playwright
    out = ROOT / "build" / "band.mov"
    ff = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "png", "-i", "-",
                           "-c:v", "qtrle", "-pix_fmt", "argb", str(out)], stdin=subprocess.PIPE)
    fade = lambda t, a, z, d: max(0.0, min(1.0, (t - a) / d, (z - t) / d))
    last_key, last_png, shots = None, None, 0
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 156}, device_scale_factor=1.5)
        pg.set_content(BAND_HTML, wait_until="domcontentloaded"); pg.evaluate("document.fonts.ready.then(() => 1)"); pg.wait_for_timeout(400)
        for i in range(int(round(total * FPS))):
            t = i / FPS
            cap = next(((a, z, x) for (a, z, x) in caps if a <= t < z), None)
            chip = next(((a, d, h, x) for (a, d, h, x) in chips if a <= t < a + d), None)
            ca = round(fade(t, cap[0], cap[1], 0.12), 2) if cap else 0
            ka = round(fade(t, chip[0], chip[0] + chip[1], 0.3), 2) if chip else 0
            key = (cap[2] if cap else None, ca, chip[3] if chip else None, ka)
            if key != last_key:
                pg.evaluate("a => setState(...a)", [cap[2] if cap else "", ca, [chip[2], chip[3]] if chip else None, ka, round((1 - ka) * 8, 1)])
                last_png = pg.screenshot(type="png", omit_background=True); last_key = key; shots += 1
            ff.stdin.write(last_png)
        b.close()
    ff.stdin.close()
    if ff.wait():
        raise SystemExit("band encode failed")
    print(f"band: {shots} distinct frames")
    return out


def render_alpha_card(name: str, dur: float, query: str) -> pathlib.Path:
    """A card page rendered with a transparent background, frame by frame, to an alpha .mov (identical frames reused)."""
    from playwright.sync_api import sync_playwright
    out = ROOT / "build" / f"{name}_alpha.mov"
    ff = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "png", "-i", "-",
                           "-c:v", "qtrle", "-pix_fmt", "argb", str(out)], stdin=subprocess.PIPE)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=1.5)
        pg.goto(f"{(ROOT / 'tools' / 'src' / (name + '.html')).as_uri()}?dur={dur}&{query}", wait_until="domcontentloaded")
        pg.evaluate("document.fonts.ready.then(() => 1)"); pg.wait_for_timeout(300)
        last_html, last_png = None, None
        for i in range(int(round(dur * FPS))):
            pg.evaluate(f"render({i / FPS})")
            html = pg.evaluate("document.body.innerHTML + '|' + [...document.querySelectorAll('*')].map(e => e.style.cssText).join(';')")
            if html != last_html:
                last_png = pg.screenshot(type="png", omit_background=True); last_html = html
            ff.stdin.write(last_png)
        b.close()
    ff.stdin.close()
    if ff.wait():
        raise SystemExit(f"alpha encode failed for {name}")
    return out


# ---------------------------------------------------------------- segments
def phone_assets():
    from PIL import Image, ImageDraw
    pw, ph = 383, 830
    x0, y0 = (W - pw) // 2, (H - ph) // 2 - 30
    bg = Image.new("RGB", (W, H), (226, 226, 220))
    d = ImageDraw.Draw(bg)
    d.rounded_rectangle([x0 - 14, y0 - 14, x0 + pw + 14, y0 + ph + 14], radius=62, fill=(17, 17, 17))
    bg.save(ROOT / "build" / "phone_bg.png")
    bz = Image.new("RGBA", (W, H), (0, 0, 0, 0)); dz = ImageDraw.Draw(bz)
    dz.rounded_rectangle([x0 - 14, y0 - 14, x0 + pw + 14, y0 + ph + 14], radius=62, fill=(17, 17, 17, 255))
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0)); ImageDraw.Draw(sh).rounded_rectangle([x0 - 8, y0 + 4, x0 + pw + 20, y0 + ph + 36], radius=66, fill=(0, 0, 0, 90))
    from PIL import ImageFilter
    bz = Image.alpha_composite(sh.filter(ImageFilter.GaussianBlur(18)), bz)
    bz.save(ROOT / "build" / "phone_bezel.png")
    s = 4
    m = Image.new("L", (pw * s, ph * s), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, pw * s - 1, ph * s - 1], radius=48 * s, fill=255)
    m.resize((pw, ph), Image.LANCZOS).save(ROOT / "build" / "phone_mask.png")
    return x0, y0, pw, ph


def build_segment(s: dict, env: str) -> pathlib.Path:
    out = ROOT / "build" / f"{s['id']}.mp4"
    enc = ["-an", "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-color_range", "tv", "-r", str(FPS), str(out)]
    D = s["dur"]
    if "card" in s:
        run(["ffmpeg", "-y", "-v", "error", "-i", str(ROOT / "cards" / f"{s['card']}.mp4"),
             "-vf", f"tpad=stop_mode=clone:stop_duration=5,trim=duration={D},scale=out_range=tv", *enc])
    elif "shot" in s:
        PH = H - BAR
        if s.get("zoom"):  # (amount, focus x, focus y, start): eased over ZD s, then held; done at 2x so it moves smoothly
            z, fx, fy, zs = s["zoom"]; ZD = 1.4
            pz = f"clip((t-{zs})/{ZD},0,1)"
            Z = f"(1+{z}*{pz}*{pz}*(3-2*{pz}))"
            zoom = (f"scale={2 * W}:{2 * PH}:flags=lanczos,"
                    f"scale=w='trunc({2 * W}*{Z}/2)*2':h='trunc({2 * PH}*{Z}/2)*2':eval=frame:flags=bicubic,"
                    f"crop={2 * W}:{2 * PH}:x='(trunc({2 * W}*{Z}/2)*2-{2 * W})*{fx}':y='(trunc({2 * PH}*{Z}/2)*2-{2 * PH})*{fy}',scale={W}:{PH}:flags=lanczos")
        else:
            zoom = f"scale={W}:{PH}"
        # to: freeze on the recording's frame at this time; lift=(top, cut, strip): move content clear of the captions
        src = "[1:v]" + (f"trim=duration={s['to'] - s.get('ss', 0):.3f}," if s.get("to") else "") + "setpts=PTS-STARTPTS"
        if s.get("lift"):
            top, cut, strip = s["lift"]
            src += (f",split[la][lb];[la]crop={W}:{cut - top}:0:{top}[lc];[lb]crop={W}:16:0:{strip},"
                    f"scale={W}:{PH - (cut - top)}[ld];[lc][ld]vstack")
        fc = (f"{src},tpad=stop_mode=clone:stop_duration=6,{zoom},setsar=1[v];"
              f"[0:v]scale={W}:{BAR},pad={W}:{H}:0:0:black[bg];[bg][v]overlay=0:{BAR}:shortest=0,trim=duration={D},scale=out_range=tv,format=yuv420p")
        run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(ROOT / "build" / "ov" / f"bar_{s['id']}.png"),
             "-ss", str(s.get("ss", 0)), "-i", str(ROOT / "shots" / env / f"{s['shot']}.mp4"), "-filter_complex", fc, "-t", str(D), *enc])
    elif "broll" in s:
        clip = ROOT / "broll" / f"{s['broll']}.mp4"
        speed = dur_of(clip) / D  # stretch or squeeze the clip to the segment, gently
        ov = render_alpha_card(s["overlay"], D, s.get("q", ""))
        fc = (f"[0:v]setpts=PTS/{speed:.4f},fps={FPS},scale={W}:{H}:flags=lanczos,eq=saturation=0.92:contrast=1.03,setsar=1[bg];"
              f"[1:v]format=rgba[ov];[bg][ov]overlay=0:0:shortest=0,trim=duration={D},scale=out_range=tv,format=yuv420p")
        run(["ffmpeg", "-y", "-v", "error", "-i", str(clip), "-i", str(ov), "-filter_complex", fc, "-t", str(D), *enc])
    elif "phone" in s:
        x0, y0, pw, ph = phone_assets()
        if s.get("backdrop"):
            bd = ROOT / "broll" / f"{s['backdrop']}.mp4"
            fc = (f"[3:v]fps={FPS},scale={W}:{H},gblur=sigma=26,eq=brightness=-0.06:saturation=0.85,setsar=1[bd];"
                  f"[0:v]format=rgba[bz];[bd][bz]overlay=0:0[bg];"
                  f"[1:v]setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=6,scale={pw}:{ph}:flags=lanczos,format=rgba[v];"
                  f"[2:v]format=gray[m];[v][m]alphamerge[vm];[bg][vm]overlay={x0}:{y0}:shortest=0,trim=duration={D},scale=out_range=tv,format=yuv420p")
            run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(ROOT / "build" / "phone_bezel.png"),
                 "-ss", str(s.get("ss", 0)), "-i", str(ROOT / "shots" / env / f"{s['phone']}.mp4"), "-i", str(ROOT / "build" / "phone_mask.png"),
                 "-i", str(bd), "-filter_complex", fc, "-t", str(D), *enc])
            got = dur_of(out)
            if abs(got - D) > 0.08:
                print(f"  warning: {s['id']} is {got:.2f} s, wanted {D}")
            return out
        fc = (f"[1:v]setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=6,scale={pw}:{ph}:flags=lanczos,format=rgba[v];"
              f"[2:v]format=gray[m];[v][m]alphamerge[vm];[0:v][vm]overlay={x0}:{y0}:shortest=0,trim=duration={D},format=yuv420p")
        run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(ROOT / "build" / "phone_bg.png"),
             "-ss", str(s.get("ss", 0)), "-i", str(ROOT / "shots" / env / f"{s['phone']}.mp4"), "-i", str(ROOT / "build" / "phone_mask.png"),
             "-filter_complex", fc, "-t", str(D), *enc])
    got = dur_of(out)
    if abs(got - D) > 0.08:
        print(f"  warning: {s['id']} is {got:.2f} s, wanted {D}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="staging")
    ap.add_argument("--skip-cards", action="store_true")
    ap.add_argument("--out", default="sous-demo-draft")
    a = ap.parse_args()
    (ROOT / "build").mkdir(exist_ok=True); (ROOT / "out").mkdir(exist_ok=True)
    if not a.skip_cards:
        print("rendering cards…"); render_cards()
    t = 0.0
    for s in SEGMENTS:
        s["t"] = t; t += s["dur"]
    total = t
    caps, chips = overlays(a.env)
    parts = [build_segment(s, a.env) for s in SEGMENTS]
    print(f"timeline {total:.1f} s")
    (ROOT / "build" / "list.txt").write_text("".join(f"file '{p}'\n" for p in parts))
    joined = ROOT / "build" / "joined.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(ROOT / "build" / "list.txt"), "-c", "copy", str(joined)])

    band = render_band(caps, chips, total)
    capd = ROOT / "build" / "captioned.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(joined), "-i", str(band), "-filter_complex", f"[0:v][1:v]overlay=0:{BAND_Y}:shortest=0[v]",
         "-map", "[v]", "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(FPS), "-t", f"{total:.3f}", str(capd)])

    ai, vo_l, fx_l, n = [], [], [], 0
    for s in SEGMENTS:
        for beat, off in s.get("vo", []):
            ai += ["-i", str(ROOT / "audio" / f"vo_{beat}.mp3")]; ms = int((s["t"] + off) * 1000)
            vo_l.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms}[vo{n}]"); n += 1
        for name, off in s.get("sfx", []):
            ai += ["-i", str(ROOT / "audio" / f"sfx_{name}.mp3")]; ms = int((s["t"] + off) * 1000)
            fx_l.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,volume={SFX_VOL[name]},adelay={ms}|{ms}[fx{n}]"); n += 1
    ai += ["-i", str(ROOT / "audio" / "music.mp3")]
    mi = n
    vos = "".join(x.split("]")[-2] + "]" for x in vo_l); fxs = "".join(x.split("]")[-2] + "]" for x in fx_l)
    vos = "".join(f"[{x[x.rindex('[') + 1:-1]}]" for x in vo_l); fxs = "".join(f"[{x[x.rindex('[') + 1:-1]}]" for x in fx_l)
    fc = ";".join(vo_l + fx_l + [
        f"{vos}amix=inputs={len(vo_l)}:normalize=0:duration=longest,apad=whole_dur={total}[vo]", "[vo]asplit=2[vom][vosc]",
        f"[{mi}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{total},volume=0.30,afade=t=in:d=1.5,afade=t=out:st={total - 3.5}:d=3.5[mus]",
        "[mus][vosc]sidechaincompress=threshold=0.02:ratio=5:attack=30:release=500[mduck]",
        f"{fxs}amix=inputs={len(fx_l)}:normalize=0:duration=longest,apad=whole_dur={total}[fx]",
        f"[vom][mduck][fx]amix=inputs=3:normalize=0:duration=first,atrim=0:{total},loudnorm=I=-14:TP=-1.5:LRA=11[out]"])
    (ROOT / "build" / "audio.fc").write_text(fc)
    wav = ROOT / "build" / "mix.wav"
    run(["ffmpeg", "-y", "-v", "error", *ai, "-/filter_complex", str(ROOT / "build" / "audio.fc"), "-map", "[out]", "-ar", "48000", str(wav)])
    import re
    def lufs(pth):
        e = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(pth), "-af", "ebur128=peak=true", "-f", "null", "-"], capture_output=True, text=True).stderr
        e = e[e.rindex("Summary"):]
        return float(re.search(r"I:\s+(-?[0-9.]+) LUFS", e).group(1)), float(re.search(r"Peak:\s+(-?[0-9.]+) dBFS", e).group(1))
    gain = 0.0
    for _ in range(3):
        I, tp = lufs(wav)
        print(f"mix: {I:.1f} LUFS, true peak {tp:.1f} dBFS")
        if abs(I + 14.0) <= 0.3:
            break
        gain += -14.0 - I
        out2 = ROOT / "build" / "mix_lim.wav"
        run(["ffmpeg", "-y", "-v", "error", "-i", str(ROOT / "build" / "mix.wav"), "-af",
             f"volume={gain:.2f}dB,alimiter=limit=0.78:attack=3:release=60:level=false", "-ar", "48000", str(out2)])
        wav = out2
    final = ROOT / "out" / f"{a.out}.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(capd), "-i", str(wav), "-map", "0:v", "-map", "1:a", "-c:v", "copy",
         "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(final)])
    cj = json.loads((ROOT / "data" / "captions.json").read_text())
    fmt = lambda x: f"{int(x // 3600):02d}:{int(x % 3600 // 60):02d}:{int(x % 60):02d},{int(x * 1000 % 1000):03d}"
    srt, k = [], 1
    for s in SEGMENTS:
        for beat, off in s.get("vo", []):
            for c in cj[str(beat)]["captions"]:
                srt.append(f"{k}\n{fmt(s['t'] + off + c['t0'])} --> {fmt(s['t'] + off + c['t1'])}\n{c['text']}\n"); k += 1
    final.with_suffix(".srt").write_text("\n".join(srt))
    print(f"wrote {final} ({dur_of(final):.1f} s)")


if __name__ == "__main__":
    main()
