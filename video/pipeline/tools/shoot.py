"""Record real gateway pages frame by frame (smooth scroll, fake cursor, highlighter), for the Sous video.

    uv run --with playwright --with httpx python tools/shoot.py <env> <shot> [--probe]

Shots are defined in SHOTS below; each is a list of actions run against a fresh sandbox (secrets/sandbox-<env>.json).
Tokens go into localStorage or the break page's input via an init script / fill; they are never on screen.
--probe writes one PNG per 'goto'/'hold' instead of a video, for looking at layouts.
"""
from __future__ import annotations

import asyncio
import json
import math
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import sbx  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
FPS = 30

CURSOR_JS = r"""
(() => {
  if (window.__cur) return;
  const c = document.createElement('div');
  c.id = '__cur';
  c.innerHTML = window.__touch ? '<div style="width:34px;height:34px;margin:-14px 0 0 -14px;border-radius:50%;background:rgba(0,0,0,.18);border:2px solid rgba(255,255,255,.8)"></div>'
    : '<svg width="26" height="26" viewBox="0 0 24 24"><path d="M4 2l15 9-6.5 1.6L9 19z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  Object.assign(c.style, {position:'fixed', left:'-40px', top:'-40px', zIndex: 2147483647, pointerEvents:'none', transition:'none'});
  document.documentElement.appendChild(c);
  const r = document.createElement('div');
  Object.assign(r.style, {position:'fixed', width:'34px', height:'34px', borderRadius:'50%', border:'3px solid rgba(0,86,112,.7)',
    left:'-99px', top:'-99px', zIndex: 2147483646, pointerEvents:'none', opacity: 0});
  document.documentElement.appendChild(r);
  window.__cur = {c, r, x: -40, y: -40};
  window.__curMove = (x, y) => { window.__cur.x = x; window.__cur.y = y; c.style.left = (x - 3) + 'px'; c.style.top = (y - 2) + 'px'; };
  window.__ripple = (p) => { r.style.left = (window.__cur.x - 17) + 'px'; r.style.top = (window.__cur.y - 17) + 'px';
    r.style.opacity = String(Math.max(0, 1 - p)); r.style.transform = `scale(${0.6 + p})`; };
  const hl = document.createElement('div'); hl.id = '__hl';
  document.documentElement.appendChild(hl);
  window.__hl = (rects, p) => {
    hl.innerHTML = rects.map((b, i) => `<div style="position:fixed;left:${b.x - 4}px;top:${b.y + b.h * 0.12}px;height:${b.h * 0.86}px;width:${Math.max(0, (b.w + 8) * Math.min(1, Math.max(0, p * rects.length - i)))}px;background:rgba(253,219,50,.55);mix-blend-mode:multiply;border-radius:3px;z-index:2147483640;pointer-events:none"></div>`).join('');
  };
})();
"""

HLTEXT_JS = r"""
([scope, phrases]) => {
  const root = document.querySelector(scope) || document.body, out = [];
  for (const ph of phrases) {
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
      const i = n.data.indexOf(ph);
      if (i < 0 || !n.parentElement.offsetParent) continue;
      const r = document.createRange(); r.setStart(n, i); r.setEnd(n, i + ph.length);
      const all = [...r.getClientRects()];
      if (!all.length || all[0].y < 0 || all[all.length - 1].bottom > innerHeight) continue;  // only an on-screen occurrence
      const lines = {};
      for (const b of r.getClientRects()) {
        const k = Math.round(b.y / 4);
        const l = lines[k] || (lines[k] = {x: b.x, y: b.y, r: b.right, h: b.height});
        l.x = Math.min(l.x, b.x); l.r = Math.max(l.r, b.right);
      }
      Object.values(lines).sort((a, b) => a.y - b.y).forEach(l => out.push({x: l.x, y: l.y, w: l.r - l.x, h: l.h}));
      break;
    }
  }
  return out;
}
"""


def ease(t: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * max(0.0, min(1.0, t)))


class Shot:
    def __init__(self, page, out: pathlib.Path, probe: bool):
        self.page, self.out, self.probe, self.n = page, out, probe, 0
        self.proc = None
        self.cur = (None, None)
        self.marks: dict[str, float] = {}
        self.hl_rects: list = []
        self.hl_p = 0.0

    async def start(self):
        if not self.probe:
            self.out.parent.mkdir(parents=True, exist_ok=True)
            self.proc = subprocess.Popen(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg", "-i", "-",
                 "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-r", str(FPS), str(self.out)],
                stdin=subprocess.PIPE)

    async def overlay(self):
        await self.page.evaluate(CURSOR_JS)
        if self.cur[0] is not None:
            await self.page.evaluate(f"window.__curMove({self.cur[0]}, {self.cur[1]})")
        await self.page.evaluate(f"window.__hl({json.dumps(self.hl_rects)}, {self.hl_p})")

    async def frame(self, repeat: int = 1):
        await self.overlay()
        if self.probe:
            return
        img = await self.page.screenshot(type="jpeg", quality=93)
        for _ in range(repeat):
            self.proc.stdin.write(img)
            self.n += 1

    async def hold(self, secs: float, every: int = 3):
        frames = int(secs * FPS)
        i = 0
        while i < frames:
            k = min(every, frames - i)
            await self.frame(k)
            i += k
        if self.probe:
            p = self.out.with_suffix(f".{len(self.marks)}.png")
            await self.page.screenshot(path=str(p))
            self.marks[f"probe{len(self.marks)}"] = 0
            print("probe", p)

    async def box(self, target: str):
        loc = self.locate(target)
        await loc.wait_for(state="visible", timeout=20000)
        b = await loc.bounding_box()
        return loc, b

    def locate(self, target: str):
        if target.startswith("text="):
            return self.page.get_by_text(target[5:], exact=False).first
        if target.startswith("role="):
            role, name = target[5:].split(":", 1)
            return self.page.get_by_role(role, name=name).first
        return self.page.locator(target).first

    async def move(self, target: str | tuple, secs: float = 0.7):
        if isinstance(target, tuple):
            x1, y1 = target
        else:
            _, b = await self.box(target)
            x1, y1 = b["x"] + b["width"] * 0.5, b["y"] + b["height"] * 0.55
        vp = self.page.viewport_size
        x0, y0 = self.cur if self.cur[0] is not None else (vp["width"] * 0.62, vp["height"] * 0.92)
        frames = max(1, int(secs * FPS))
        for f in range(1, frames + 1):
            e = ease(f / frames)
            self.cur = (x0 + (x1 - x0) * e, y0 + (y1 - y0) * e - math.sin(math.pi * e) * 40)
            await self.frame()
        self.cur = (x1, y1)

    async def click(self, target: str, secs: float = 0.7):
        await self.move(target, secs)
        await self.page.mouse.click(*self.cur)
        for f in range(8):
            await self.page.evaluate(f"window.__ripple({(f + 1) / 8})")
            await self.frame()
        await self.page.evaluate("window.__ripple(1)")

    async def scroll(self, dy: float, secs: float, at: tuple | None = None):
        vp = self.page.viewport_size
        x, y = at or (self.cur if self.cur[0] is not None else (vp["width"] * 0.5, vp["height"] * 0.5))
        await self.page.mouse.move(x, y)
        frames = max(1, int(secs * FPS))
        done = 0.0
        for f in range(1, frames + 1):
            want = dy * ease(f / frames)
            await self.page.mouse.wheel(0, want - done)
            done = want
            await self.page.wait_for_timeout(16)
            await self.frame()

    async def scroll_to(self, target: str, secs: float, offset: float = 0.3):
        """Scroll the element's nearest scrollable ancestor so the element sits at `offset` of the viewport height."""
        loc = self.locate(target)
        await loc.wait_for(state="attached", timeout=20000)
        b = await loc.bounding_box()
        vp = self.page.viewport_size
        dy = b["y"] - vp["height"] * offset
        await self.scroll(dy, secs, at=(b["x"] + 10, min(vp["height"] - 20, max(20, b["y"]))))

    async def highlight(self, targets: list[str], secs: float = 0.8):
        rects = []
        for t in targets:
            _, b = await self.box(t)
            rects.append({"x": b["x"], "y": b["y"], "w": b["width"], "h": b["height"]})
        self.hl_rects = rects
        frames = max(1, int(secs * FPS))
        for f in range(1, frames + 1):
            self.hl_p = ease(f / frames)
            await self.frame()

    async def hltext(self, scope: str, phrases: list[str], secs: float = 0.9):
        """Marker-pen highlight of exact phrases (line by line), found in text nodes under `scope`."""
        rects = await self.page.evaluate(HLTEXT_JS, [scope, phrases])
        if not rects:
            raise SystemExit(f"hltext: none of {phrases} found under {scope}")
        self.hl_rects = rects
        frames = max(1, int(secs * FPS))
        for f in range(1, frames + 1):
            self.hl_p = ease(f / frames)
            await self.frame()

    def unhighlight(self):
        self.hl_rects, self.hl_p = [], 0.0

    async def finish(self):
        if self.proc:
            self.proc.stdin.close()
            self.proc.wait()
            print(f"wrote {self.out} ({self.n / FPS:.1f} s)")


async def run(env: str, name: str, probe: bool):
    from playwright.async_api import async_playwright

    s = sbx.load(env)
    base, sid = sbx.BASES[env], s["sandbox"]["id"]
    ctxdata = json.loads((ROOT / "data" / f"ids-{env}.json").read_text()) if (ROOT / "data" / f"ids-{env}.json").exists() else {}
    spec = SHOTS[name]
    phone = spec.get("phone", False)
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--host-resolver-rules=MAP sous.saarikko.dev 135.181.8.209"])
        if phone:
            ctx = await b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True, has_touch=True)
        else:
            ctx = await b.new_context(viewport={"width": 1280, "height": 672}, device_scale_factor=1.5)  # page area under a browser bar
        init = ("window.__touch = true;" if phone else "") + ("localStorage.setItem('vk-approver', 'Mikko Laine (head chef)');"
                f"localStorage.setItem('vk-approver-code:{sid}', {json.dumps(s['approver_token'])});"
                f"localStorage.setItem('vk-approver-code', {json.dumps(s['approver_token'])});")
        await ctx.add_init_script(init)
        page = await ctx.new_page()
        shot = Shot(page, ROOT / "shots" / env / f"{name}.mp4", probe)
        await shot.start()
        fmt = {"base": base, "sbx": sid, **ctxdata}
        first = next((a[1] for a in spec["actions"] if a[0] == "goto"), "")
        (ROOT / "shots" / env).mkdir(parents=True, exist_ok=True)
        (ROOT / "shots" / env / f"{name}.meta.json").write_text(json.dumps({"url": first.format(**fmt), "phone": phone}))
        for act in spec["actions"]:
            kind, *args = act
            args = [a.format(**fmt) if isinstance(a, str) else a for a in args]
            if kind == "goto":
                await page.goto(args[0], wait_until="networkidle")
                await page.evaluate(CURSOR_JS)
            elif kind == "wait":
                if isinstance(args[0], (int, float)):
                    await page.wait_for_timeout(args[0])
                else:
                    await shot.locate(args[0]).wait_for(state="visible", timeout=30000)
            elif kind == "hold":
                await shot.hold(*args)
            elif kind == "move":
                await shot.move(*args)
            elif kind == "click":
                await shot.click(*args)
            elif kind == "fill_secret":  # (selector, which token): typed without frames, so it never shows
                await shot.locate(args[0]).fill(s[args[1]])
            elif kind == "type":
                loc = shot.locate(args[0])
                for ch in args[1]:
                    await loc.type(ch)
                    await shot.frame(2)
            elif kind == "scroll":
                await shot.scroll(*args)
            elif kind == "scroll_to":
                await shot.scroll_to(*args)
            elif kind == "highlight":
                await shot.highlight(*args)
            elif kind == "hltext":
                await shot.hltext(*args)
            elif kind == "unhighlight":
                shot.unhighlight()
            elif kind == "js":
                await page.evaluate(args[0])
            elif kind == "mark":
                shot.marks[args[0]] = shot.n / FPS
            else:
                raise SystemExit(f"unknown action {kind}")
        await shot.finish()
        if shot.marks and not probe:
            (ROOT / "shots" / env / f"{name}.marks.json").write_text(json.dumps(shot.marks, indent=1))
        await b.close()


SHOTS: dict[str, dict] = json.loads((ROOT / "tools" / "shots.json").read_text()) if (ROOT / "tools" / "shots.json").exists() else {}

if __name__ == "__main__":
    env, name = sys.argv[1], sys.argv[2]
    asyncio.run(run(env, name, "--probe" in sys.argv))
