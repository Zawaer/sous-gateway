"""Music bed and sound effects from ElevenLabs (audio/music.mp3, audio/sfx_*.mp3)."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import el
ROOT = pathlib.Path(__file__).resolve().parent.parent
MUSIC = ("Instrumental background music for a calm, confident product demo video. Minimal warm electronic, around 100 bpm, "
         "soft felt piano, gentle plucked synth arpeggio, light brushed percussion, warm sub bass. Starts sparse and curious, "
         "builds slightly with a little more energy in the middle third, a clear but understated lift around 60 seconds, "
         "then resolves to a warm ending. No vocals, no drops, no risers, nothing distracting; sits under a voice-over.")
SFX = {
    "whoosh": ("very soft short airy whoosh transition, subtle, clean, UI motion", 0.8),
    "seal": ("soft satisfying digital confirmation tick, like a stamp being pressed gently, clean UI sound", 0.6),
    "buzz": ("smartphone vibration buzz twice on a wooden table, short, close", 1.2),
    "ping": ("gentle pleasant phone notification chime, single soft note", 1.0),
    "deny": ("muted low soft error thud, UI denial sound, short, not harsh", 0.6),
    "type": ("quiet laptop keyboard typing, a short burst of soft key presses", 2.0),
}
c = el.client()  # Rene: his key for everything except Toivo's voice
what = sys.argv[1:] or ["music"] + list(SFX)
for name in what:
    if name == "music":
        r = c.post("/v1/music", params={"output_format": "mp3_44100_192"},
                   json={"prompt": MUSIC, "music_length_ms": 125000, "model_id": "music_v1", "force_instrumental": True}, timeout=600)
        out = ROOT / "audio" / "music.mp3"
    else:
        text, secs = SFX[name]
        r = c.post("/v1/sound-generation", params={"output_format": "mp3_44100_192"},
                   json={"text": text, "duration_seconds": secs, "prompt_influence": 0.6})
        out = ROOT / "audio" / f"sfx_{name}.mp3"
    if r.status_code != 200:
        print(name, "HTTP", r.status_code, r.text[:300]); continue
    out.write_bytes(r.content); print("wrote", out, len(r.content) // 1024, "KB")
