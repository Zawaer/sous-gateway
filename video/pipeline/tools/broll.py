"""B-roll from ElevenLabs Flows video (Veo 3.1) on Rene's key. Writes broll/<name>.mp4.

    uv run --with httpx python tools/broll.py [names...]
"""
import sys, time, pathlib, json
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import el, httpx
ROOT = pathlib.Path(__file__).resolve().parent.parent
NEG = "text, captions, subtitles, logos, brand names, labels on packaging, watermark, faces, people looking at camera, cartoon, CGI look"
CLIPS = {
    "opening": ("Early morning in a professional hotel pastry kitchen. Close-up of a pastry chef's hands whisking cold whipping cream "
                "in a stainless steel bowl on a steel bench; rows of small empty panna cotta glasses in the soft background. Natural "
                "window light, shallow depth of field, slow gentle push-in, documentary film look, muted cream, steel and deep green tones.", 6),
    "kitchen": ("A calm professional hotel kitchen seen from across the room: steel benches, warm pendant lights, a cook moving softly "
                "out of focus in the far background. Very shallow depth of field, everything softly blurred bokeh, slow lateral camera "
                "drift, documentary film look, warm cream and deep green tones.", 8),
}

def gen(name):
    prompt, secs = CLIPS[name]
    c = el.client()
    r = c.post("/v1/flows/video", json={"model_id": "veo-3.1-generate-001", "prompt": prompt, "negative_prompt": NEG,
                                         "duration_secs": secs, "aspect_ratio": "16:9", "resolution": "1080p",
                                         "generate_audio": False, "enhance_prompt": True})
    if r.status_code != 200:
        raise SystemExit(f"{name}: HTTP {r.status_code} {r.text[:400]}")
    gid = r.json()["id"]; print(name, "started", gid, flush=True)
    for _ in range(120):
        time.sleep(10)
        j = c.get(f"/v1/flows/video/{gid}").json()
        if j.get("status") == "completed":
            v = httpx.get(j["content_url"], timeout=300, follow_redirects=True); v.raise_for_status()
            out = ROOT / "broll" / f"{name}.mp4"; out.write_bytes(v.content)
            print(name, "done", out, len(v.content) // 1024, "KB", flush=True); return
        if j.get("status") == "failed":
            raise SystemExit(f"{name} failed: {j.get('failure_reason')} {j.get('error_message')}")
    raise SystemExit(f"{name}: timed out")

if __name__ == "__main__":
    for n in (sys.argv[1:] or list(CLIPS)):
        gen(n)
