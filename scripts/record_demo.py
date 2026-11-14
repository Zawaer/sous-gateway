"""Record the draft pitch video (docs/pitch/video-script.md) beat by beat, then assemble it.

Everything on screen is a real response from a running gateway; nothing is mocked. Steps:

    # 1. real MCP session (Streamable HTTP) as Hotel Aurora's agent -> transcript JSON
    uv run python scripts/record_demo.py mcp --base https://<gateway> --out OUT
    # 2. terminal animation from that transcript (Pillow frames piped into ffmpeg)
    uv run --with pillow python scripts/record_demo.py term OUT/mcp_transcript.json OUT/clips/03a_mcp_terminal.mp4
    # 3. browser clips (Playwright + Chromium, frame capture at 1920x1080)
    uv run --with playwright python scripts/record_demo.py ui CLIP --base URL --order PO-... --out OUT/clips/x.mp4
    # 4. title / still cards
    uv run --with pillow python scripts/record_demo.py cards OUT/clips
    # 5. cut, caption and concatenate (shot list = SHOTS below)
    uv run python scripts/record_demo.py assemble OUT

Requires ffmpeg on PATH and `python -m playwright install chromium`. Never pass the production URL to
the commands that write (mcp, ui approve); see docs/pitch/video/README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

W, H, FPS = 1920, 1080, 30
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
SANS_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
SERIF = "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"
PROD = "135-181-8-209"


def next_monday(today: date | None = None) -> date:
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def ffmpeg_writer(path: Path, fps: int = FPS) -> subprocess.Popen:
    path.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
         "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
         "-pix_fmt", "yuv420p", str(path)], stdin=subprocess.PIPE)


# --- 1. real MCP session ------------------------------------------------------------------

async def _mcp_session(base: str, out: Path, when: date) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    agent, mandate = "aurora-purchasing-agent", "M-HOTEL-AURORA"
    log: list[dict] = [{"kind": "header", "url": base + "/mcp/", "ts": time.time()},
                       {"kind": "user", "text": "1,000 litres of lactose-free milk by Monday for Hotel Aurora."}]

    async with streamable_http_client(base + "/mcp/") as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            tools = await s.list_tools()
            log.append({"kind": "init", "server": init.server_info.name, "tools": [t.name for t in tools.tools]})

            async def call(name: str, args: dict) -> dict:
                t0 = time.time()
                r = await s.call_tool(name, args)
                body = r.structured_content or json.loads(r.content[0].text)
                log.append({"kind": "call", "tool": name, "args": args, "result": body,
                            "is_error": bool(r.is_error), "seconds": round(time.time() - t0, 2)})
                return body

            found = await call("search_products", {"query": "laktoositon maito", "claims": ["FREE_FROM_LACTOSE"],
                                                   "limit": 12, "agent_id": agent})
            ids = [p["id"] for p in found["products"]]
            q = await call("request_quote", {"agent_id": agent, "mandate_id": mandate,
                                             "delivery_date": when.isoformat(),
                                             "items": [{"product_ids": ids, "amount": 1000, "unit": "L"}]})
            q = q.get("quote", q)
            await call("place_order", {"agent_id": agent, "mandate_id": mandate, "quote_id": q["id"],
                                       "idempotency_key": f"video-{uuid.uuid4().hex[:12]}"})
    out.mkdir(parents=True, exist_ok=True)
    (out / "mcp_transcript.json").write_text(json.dumps(log, ensure_ascii=False, indent=1))
    print("wrote", out / "mcp_transcript.json")


# --- 2. terminal render -------------------------------------------------------------------

def transcript_lines(log: list[dict]) -> list[tuple[str, str]]:
    """(style, text) lines shown in the terminal, all taken from the real transcript."""
    import textwrap

    L: list[tuple[str, str]] = []
    order = {"header": 0, "init": 1, "user": 2}  # show the connection before the request
    for ev in sorted(log, key=lambda e: order.get(e["kind"], 3)):
        k = ev["kind"]
        if k == "header":
            L.append(("dim", f"MCP client  ->  {ev['url']}   (Streamable HTTP)"))
        elif k == "init":
            L.append(("dim", f"connected to {ev['server']}: {len(ev['tools'])} tools"))
            L.append(("", ""))
        elif k == "user":
            L.append(("prompt", "> " + ev["text"]))
            L.append(("", ""))
        elif k == "call":
            a, r = ev["args"], ev["result"]
            args = ", ".join(f"{k2}={json.dumps(v, ensure_ascii=False)}" for k2, v in a.items()
                             if k2 not in ("items",)) + (", items=[...]" if "items" in a else "")
            if len(args) > 92:
                args = args[:89] + "..."
            L.append(("tool", f"● {ev['tool']}({args})"))
            if ev["tool"] == "search_products":
                ps = r.get("products", [])
                L.append(("out", f"  └ {len(ps)} products from the local catalog index, 0 Valio API calls"))
                for p in ps[:4]:
                    L.append(("dim", f"     {p['id']}  {p.get('name', '')[:70]}"))
                if len(ps) > 4:
                    L.append(("dim", f"     ... {len(ps) - 4} more"))
            elif ev["tool"] == "request_quote":
                q = r.get("quote", r)
                calls = sum(v for k2, v in (q.get("valio_api_calls") or {}).items() if k2 != "cache_hits")
                L.append(("out", f"  └ signed quote {q['id']}  €{q['total_ex_vat']:,.2f} ex VAT  "
                                  f"({calls} Valio API calls, live price + stock)"))
                for ln in q.get("lines", []):
                    L.append(("ok", f"     ✓ {ln['quantity']:g} × {ln['name'][:58]} ({ln.get('logistic_model')})  "
                                    f"€{ln.get('line_total_ex_vat') or 0:,.2f}"))
                alts = q.get("alternatives_considered", [])
                term = [x for x in alts if "cut-off" in " ".join(x.get("why_not", []))]
                for x in (term or alts)[:2]:
                    for j, part in enumerate(textwrap.wrap(f"– skipped {x['name']}: {x['why_not'][0]}", 98)):
                        L.append(("warn", ("     " if j == 0 else "       ") + part))
                pv = q.get("mandate_preview") or {}
                if pv:
                    L.append(("dim", f"     mandate preview: {pv.get('outcome')}"))
            elif ev["tool"] == "place_order":
                d = r.get("decision", {})
                L.append(("out", f"  └ {r.get('id')}  status={r.get('status')}  tier={d.get('outcome')}"))
                for f in d.get("findings", [])[:2]:
                    L.append(("dim", f"     {f['rule']}: {f['message'][:84]}"))
                if r.get("receipt_jws"):
                    L.append(("ok", f"     signed receipt (Ed25519 JWS) {r['receipt_jws'][:28]}..."))
            L.append(("", ""))
    return L


def render_term(transcript: Path, out: Path, seconds: float = 16.0) -> None:
    from PIL import Image, ImageDraw, ImageFont

    lines = transcript_lines(json.loads(transcript.read_text()))
    font, bold = ImageFont.truetype(MONO, 27), ImageFont.truetype(MONO_B, 27)
    colors = {"": (220, 220, 220), "dim": (140, 145, 150), "prompt": (255, 255, 255), "tool": (120, 190, 255),
              "out": (230, 230, 230), "ok": (110, 210, 130), "warn": (235, 190, 90)}
    lh, x0, y0 = 38, 70, 120
    # Timeline: prompt typed char by char, then each line appears; tool calls get a short "running" pause.
    events: list[tuple[float, int, int]] = []  # (time, line index, chars visible)
    t = 0.6
    for i, (style, text) in enumerate(lines):
        if style == "prompt":
            for c in range(1, len(text) + 1):
                events.append((t, i, c))
                t += 0.035
            t += 0.6
        else:
            events.append((t, i, len(text)))
            t += 0.9 if style == "tool" else 0.12
    scale = (seconds - 3.0) / t if t > seconds - 3.0 else 1.0  # keep the last frame on screen >= 3 s
    w = ffmpeg_writer(out)
    frames = int(seconds * FPS)
    ei = 0
    shown: dict[int, int] = {}
    for f in range(frames):
        now = f / FPS
        while ei < len(events) and events[ei][0] * scale <= now:
            shown[events[ei][1]] = events[ei][2]
            ei += 1
        img = Image.new("RGB", (W, H), (24, 26, 30))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 64], fill=(40, 43, 48))
        for j, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([28 + j * 34, 22, 48 + j * 34, 42], fill=c)
        d.text((W // 2 - 260, 16), "terminal: buyer agent over MCP (staging)", font=font, fill=(170, 170, 175))
        visible = sorted(shown)
        first = max(0, len(visible) - 20)  # scroll; keep the bottom free for captions
        for row, i in enumerate(visible[first:]):
            style, text = lines[i]
            d.text((x0, y0 + row * lh), text[: shown[i]], font=bold if style in ("prompt", "tool") else font,
                   fill=colors[style])
        if int(now * 2) % 2 == 0 and ei < len(events):
            d.rectangle([x0, y0 + len(visible[first:]) * lh, x0 + 14, y0 + len(visible[first:]) * lh + 30],
                        fill=(200, 200, 200))
        w.stdin.write(img.tobytes())
    w.stdin.close()
    w.wait()
    print("wrote", out)


# --- 3. browser clips --------------------------------------------------------------------

class Recorder:
    """Screenshots in a loop (crisp 1920x1080 text, unlike Playwright's built-in VP8 video)."""

    def __init__(self, page, phone: bool = False):
        self.page, self.phone, self.frames, self.running = page, phone, [], False

    async def _loop(self):
        while self.running:
            t = time.time()
            self.frames.append((t, await self.page.screenshot(type="jpeg", quality=92)))
            await asyncio.sleep(max(0, 1 / 12 - (time.time() - t)))

    def start(self):
        self.running = True
        self.task = asyncio.create_task(self._loop())

    async def stop(self, out: Path):
        self.running = False
        await self.task
        from io import BytesIO

        from PIL import Image, ImageDraw, ImageFont
        w = ffmpeg_writer(out)
        t0, t_end = self.frames[0][0], self.frames[-1][0]
        cache = {}
        k = 0
        for f in range(int((t_end - t0) * FPS)):
            now = t0 + f / FPS
            while k + 1 < len(self.frames) and self.frames[k + 1][0] <= now:
                k += 1
            if k not in cache:
                cache.clear()
                img = Image.open(BytesIO(self.frames[k][1])).convert("RGB")
                if self.phone:  # phone viewport on a neutral background, with a simple bezel
                    ph = img.resize((int(img.width * 1000 / img.height), 1000))
                    bg = Image.new("RGB", (W, H), (32, 34, 38))
                    d = ImageDraw.Draw(bg)
                    x = (W - ph.width) // 2
                    d.rounded_rectangle([x - 18, 22, x + ph.width + 18, 58 + ph.height], 46, fill=(10, 10, 12))
                    bg.paste(ph, (x, 40))
                    d.text((80, 80), "phone", font=ImageFont.truetype(SANS, 30), fill=(150, 150, 155))
                    img = bg
                elif img.size != (W, H):
                    img = img.resize((W, H))
                cache[k] = img.tobytes()
            w.stdin.write(cache[k])
        w.stdin.close()
        w.wait()
        print("wrote", out)


async def _ui(clip: str, base: str, order: str | None, so: str | None, out: Path, code: str | None) -> None:
    from playwright.async_api import async_playwright

    phone = clip == "approve"
    async with async_playwright() as p:
        b = await p.chromium.launch()
        if phone:
            ctx = await b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2.5,
                                      is_mobile=True, has_touch=True)
        else:  # 1280x720 CSS px at 1.5x = 1920x1080 pixels, so text reads at video size
            ctx = await b.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1.5)
        init = "localStorage.setItem('vk-approver', 'Mikko Laine (head of catering)');"
        if code:  # approver code goes straight into storage: it never appears on screen
            init += f"localStorage.setItem('vk-approver-code', {json.dumps(code)});"
        await ctx.add_init_script(init)
        pg = await ctx.new_page()
        rec = Recorder(pg, phone=phone)

        async def scroll(where: str, dy: int, steps: int = 20, dt: float = 0.05):
            """Smooth wheel scroll over the side panel ('sheet') or the main page ('main')."""
            vp = pg.viewport_size
            x = vp["width"] // 2 if phone or where == "main" else int(vp["width"] * 0.85)
            await pg.mouse.move(x, vp["height"] // 2)
            for _ in range(steps):
                await pg.mouse.wheel(0, dy / steps)
                await asyncio.sleep(dt)

        if clip == "order":  # order panel: tiers, how it happened, receipt signature check
            await pg.goto(f"{base}/ui/#/orders")
            await pg.wait_for_timeout(1500)
            rec.start()
            await asyncio.sleep(0.8)
            await pg.goto(f"{base}/ui/#/orders/{order}")
            await asyncio.sleep(3.5)
            await scroll("sheet", 520, 30)
            await asyncio.sleep(1.5)
            btn = pg.get_by_text("Verify receipt signature")
            if await btn.count():
                await btn.first.scroll_into_view_if_needed()
                await asyncio.sleep(0.6)
                await btn.first.click()
            await asyncio.sleep(3.0)
        elif clip == "ask":  # held order with the reason in plain words
            await pg.goto(f"{base}/ui/#/inbox")
            await pg.wait_for_timeout(1500)
            rec.start()
            await asyncio.sleep(2.0)
            await pg.goto(f"{base}/ui/#/orders/{order}")
            await asyncio.sleep(3.0)
            await scroll("sheet", 760, 35)
            await asyncio.sleep(3.0)
        elif clip == "approve":  # phone: approval link, countdown, note, Approve
            await pg.goto(f"{base}/ui/#/orders/{order}")
            await pg.wait_for_timeout(2000)
            rec.start()
            await asyncio.sleep(2.0)
            await scroll("sheet", 1400, 30)
            await asyncio.sleep(1.0)
            ta = pg.locator("#sheet-body textarea, .sheet textarea").first
            if not await ta.count():
                ta = pg.locator("textarea").first
            await ta.scroll_into_view_if_needed()
            await ta.click()
            await ta.type("Checked with the supplier: no milk or nuts. OK.", delay=35)
            await asyncio.sleep(0.6)
            await pg.get_by_role("button", name="Approve order").first.click()
            await asyncio.sleep(2.0)
            await scroll("sheet", -3000, 20)
            await asyncio.sleep(3.0)
        elif clip == "standing":  # held renewal, drift findings, trend bars
            await pg.goto(f"{base}/ui/#/inbox")
            await pg.wait_for_timeout(1500)
            rec.start()
            await asyncio.sleep(2.5)
            await pg.goto(f"{base}/ui/#/standing/{so}")
            await asyncio.sleep(3.5)
            await scroll("sheet", 500, 30)
            await asyncio.sleep(3.0)
        elif clip == "audit":  # chain verified, then the order's history (mandate as of decision)
            await pg.goto(f"{base}/ui/#/audit")
            await pg.wait_for_timeout(1500)
            rec.start()
            await asyncio.sleep(3.5)
            await scroll("main", 300, 20)
            await asyncio.sleep(1.0)
            await pg.goto(f"{base}/ui/#/orders/{order}")
            await asyncio.sleep(2.5)
            asof = pg.locator("#asof")
            if await asof.count():
                await asof.scroll_into_view_if_needed()
            await asyncio.sleep(4.0)
        else:
            raise SystemExit(f"unknown clip {clip}")
        await rec.stop(out)
        await b.close()


# --- 4. cards ----------------------------------------------------------------------------

def cards(out: Path, seconds: dict[str, float] | None = None) -> None:
    from PIL import Image, ImageDraw, ImageFont

    seconds = seconds or {}
    BG, FG, MUTED, ACC = (246, 244, 239), (24, 24, 24), (120, 118, 112), (30, 90, 160)

    def f(path, size):
        return ImageFont.truetype(path, size)

    def centered(d, y, text, font, fill=FG):
        w = d.textlength(text, font=font)
        d.text(((W - w) / 2, y), text, font=font, fill=fill)

    def still(name: str, draw, secs: float, dark: bool = False):
        img = Image.new("RGB", (W, H), (12, 12, 14) if dark else BG)
        draw(ImageDraw.Draw(img))
        png = out / f"{name}.png"
        img.save(png)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(png), "-t", str(secs),
                        "-r", str(FPS), "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                        str(out / f"{name}.mp4")], check=True)
        print("wrote", out / f"{name}.mp4")

    def puntari(d):  # left half of the hook; the real screenshot is Toivo's
        d.rectangle([0, 0, W // 2, H], fill=(230, 226, 218))
        centered_half = lambda y, t, fo, fill=FG: d.text(((W // 2 - d.textlength(t, font=fo)) / 2, y), t,
                                                        font=fo, fill=fill)
        centered_half(300, "[HUMAN: Puntari screenshot]", f(SANS_B, 44), (150, 60, 40))
        centered_half(380, "general shopping agent", f(SANS, 36), MUTED)
        centered_half(470, "13 min", f(SANS_B, 130))
        centered_half(660, "“call wholesalers on Saturday”", f(SERIF, 40))

    def problem(d):
        centered(d, 400, "Agents will buy. Who defines how?", f(SERIF, 84), (240, 240, 240))
        centered(d, 560, "Sous", f(SANS_B, 72), (150, 190, 240))
        centered(d, 660, "every agent carries a valtakirja: a signed mandate from its human", f(SANS, 36), (150, 150, 155))

    def trust(d):
        cols = [("Rules in code", ["limits, allergens,", "cut-offs: plain code", "no model approves"]),
                ("Signed", ["Ed25519 JWS quotes", "and receipts", "public JWKS"]),
                ("Open", ["MCP + OpenAPI", "any model: Claude,", "Gemini, open-weight", "on Verda (EU)"])]
        for i, (h, body) in enumerate(cols):
            x = 120 + i * 580
            d.rounded_rectangle([x, 300, x + 520, 760], 24, fill=(255, 255, 255), outline=(220, 216, 208), width=2)
            d.text((x + 40, 350), h, font=f(SANS_B, 52), fill=ACC)
            for j, line in enumerate(body):
                d.text((x + 40, 460 + j * 60), line, font=f(SANS, 34), fill=FG)

    def numbers(d):
        nums = [("45/45", "test requests\nhandled correctly"), ("36/36", "unsafe actions\nblocked or escalated"),
                ("≤2", "Valio API calls\nper request")]
        for i, (n, lbl) in enumerate(nums):
            cx = 330 + i * 630
            d.text((cx - d.textlength(n, font=f(SANS_B, 150)) / 2, 300), n, font=f(SANS_B, 150), fill=FG)
            for j, line in enumerate(lbl.split("\n")):
                d.text((cx - d.textlength(line, font=f(SANS, 40)) / 2, 520 + j * 54), line, font=f(SANS, 40),
                       fill=MUTED)
        centered(d, 820, "source: evals/results.json", f(MONO, 30), MUTED)

    def close(d):
        centered(d, 300, "Sous", f(SERIF, 120))
        centered(d, 480, "AI purchasing agents carry a signed mandate from their human,", f(SANS, 44))
        centered(d, 540, "and Valio Aimo's gateway enforces it.", f(SANS, 44))
        centered(d, 680, "https://135-181-8-209.sslip.io", f(MONO, 40), ACC)
        centered(d, 740, "github.com/Zawaer/sous", f(MONO, 40), ACC)

    out.mkdir(parents=True, exist_ok=True)
    still("00a_hook_puntari_placeholder", puntari, seconds.get("hook", 10))
    still("01_problem", problem, seconds.get("problem", 10), dark=True)
    still("07_trust", trust, seconds.get("trust", 14))
    still("08_numbers", numbers, seconds.get("numbers", 12))
    still("09_close", close, seconds.get("close", 8))


# --- 5. assemble -------------------------------------------------------------------------

def _ass_time(t: float) -> str:
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def vo_chunks(text: str, start: float, dur: float, max_chars: int = 118) -> list[tuple[float, float, str]]:
    """Split a VO line into caption chunks (sentence-ish, at most two lines) timed by length."""
    import re
    import textwrap

    chunks: list[str] = []
    for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
        chunks += textwrap.wrap(sent, max_chars) if len(sent) > max_chars else [sent]
    total = sum(len(c) for c in chunks)
    out, t = [], start
    for c in chunks:
        d = dur * len(c) / total
        out.append((t, t + d, c))
        t += d
    return out


def assemble(root: Path, shots_file: Path) -> None:
    shots = json.loads(shots_file.read_text())
    clips, tmp = root / "clips", root / "tmp"
    tmp.mkdir(exist_ok=True)
    parts, events, t = [], [], 0.0
    for i, s in enumerate(shots):
        part = tmp / f"part{i:02d}.mp4"
        if s.get("hook_split"):  # left: Puntari still, right: receipt clip, side by side
            left, right = clips / s["clip"], clips / s["right"]
            vf = (f"[0:v]crop=960:1080:0:0[l];[1:v]trim=start={s.get('right_start', 0)},setpts=PTS-STARTPTS,"
                  f"scale=-2:1080,crop=960:1080:(iw-960)/2+{s.get('right_x', 0)}:0[r];[l][r]hstack=inputs=2,fps={FPS},tpad=stop_mode=clone:stop_duration=30")
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(left), "-i", str(right),
                   "-filter_complex", vf, "-t", str(s["dur"])]
        else:
            speed = s.get("speed", 1.0)
            vf = f"setpts=PTS/{speed},fps={FPS},scale={W}:{H},tpad=stop_mode=clone:stop_duration=30"
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(s.get("start", 0)), "-i", str(clips / s["clip"]),
                   "-vf", vf, "-t", str(s["dur"])]
        subprocess.run(cmd + ["-an", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(part)], check=True)
        parts.append(part)
        if s.get("vo"):  # the script's VO line, spread over the whole beat (vo_dur, may span later shots)
            events += vo_chunks(s["vo"], t + 0.2, s.get("vo_dur", s["dur"]) - 0.4)
        if s.get("tag"):  # top-right label: what this shot is, or what a human still has to do
            events.append((t, t + s["dur"], "{\\an9\\fs26\\bord8}" + s["tag"]))
        t += s["dur"]
    (tmp / "list.txt").write_text("".join(f"file '{p.name}'\n" for p in parts))
    joined = tmp / "joined.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"),
                    "-c", "copy", str(joined)], check=True)
    ass = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "",
           "[V4+ Styles]",
           "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
           "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
           "MarginL, MarginR, MarginV, Encoding",
           "Style: Default,DejaVu Sans,44,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB0000000,0,0,0,0,100,100,0,0,3,14,0,2,"
           "140,140,40,1", "", "[Events]",
           "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for a, b_, text in events:
        ass.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(b_)},Default,,0,0,0,,{text}")
    (tmp / "captions.ass").write_text("\n".join(ass) + "\n")
    draft = root / "valtakirja-draft.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(joined), "-vf", f"subtitles={tmp / 'captions.ass'}",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    str(draft)], check=True)
    print(f"wrote {draft} ({t:.1f} s)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mcp")
    m.add_argument("--base", required=True)
    m.add_argument("--out", type=Path, required=True)
    m.add_argument("--date", type=date.fromisoformat, default=None)
    t = sub.add_parser("term")
    t.add_argument("transcript", type=Path)
    t.add_argument("out", type=Path)
    t.add_argument("--seconds", type=float, default=16.0)
    u = sub.add_parser("ui")
    u.add_argument("clip", choices=["order", "ask", "approve", "standing", "audit"])
    u.add_argument("--base", required=True)
    u.add_argument("--order")
    u.add_argument("--so")
    u.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("cards")
    c.add_argument("out", type=Path)
    a = sub.add_parser("assemble")
    a.add_argument("root", type=Path)
    a.add_argument("--shots", type=Path, default=Path(__file__).with_name("record_demo_shots.json"))
    x = ap.parse_args()
    if getattr(x, "base", None) and PROD in x.base and x.cmd in ("mcp", "ui"):
        sys.exit("refusing to record against production; use staging or a local gateway")
    if x.cmd == "mcp":
        asyncio.run(_mcp_session(x.base.rstrip("/"), x.out, x.date or next_monday()))
    elif x.cmd == "term":
        render_term(x.transcript, x.out, x.seconds)
    elif x.cmd == "ui":
        import os
        asyncio.run(_ui(x.clip, x.base.rstrip("/"), x.order, x.so, x.out, os.getenv("SOUS_APPROVER_CODE") or os.getenv("VALTAKIRJA_APPROVER_CODE")))
    elif x.cmd == "cards":
        cards(x.out)
    elif x.cmd == "assemble":
        assemble(x.root, x.shots)


if __name__ == "__main__":
    main()
