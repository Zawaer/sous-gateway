"""Voice-over per beat with the ElevenLabs voice "Rene" (+ character timestamps for captions).

    uv run --with httpx python tools/vo.py [beat numbers...] [--model eleven_multilingual_v2]

Writes audio/vo_<n>.mp3 and audio/vo_<n>.json (script text, tts text, alignment, duration).
Captions use the script text; the TTS text only respells words the voice might misread.
"""
import base64, json, pathlib, subprocess, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import el

ROOT = pathlib.Path(__file__).resolve().parent.parent
VOICE = "IwYXDFDLoal34muyBJoD"  # "Rene" (cloned, en, Finnish accent)
SPEAKERS = {"rene": ("IwYXDFDLoal34muyBJoD", "ELEVENLABS"),        # Rene's account
            "toivo": ("4kPimxRneO2jmBW235sy", "TOIVO_VOICE")}      # Toivo's account (key in TOIVO_VOICE)
SPEAKER = ["rene", "toivo", "rene", "toivo", "rene", "toivo", "rene", "toivo", "rene", "toivo", "rene", "toivo"]
BEATS = [
    "With Sous, AI agents buy from Valio Aimo for a kitchen. The question is what they may do alone.",
    "There are three answers: order alone, ask a person, or refuse. Here's each one, live.",
    "Alone first, with the brief's own example: a thousand litres of lactose-free milk for Monday. Sous checks live price and stock, signs a fixed price, and the agent orders by itself.",
    "Now a harder one: whipping cream for Monday's panna cotta. The search covers over seventeen thousand products without calling Valio's systems, and the quote shows the usual cream's cut-off has passed.",
    "So the agent asks for substitutes, and says the cream is for a recipe. The pick comes back as a signed quote: a fixed price, with reliability, origin and CO₂.",
    "The agent orders, and the order waits, because what it's for decides the swap. Coffee could take any cream of the same kind; a recipe needs the chef. Anything off the usual waits too: bigger quantities, later delivery, a price jump, different allergens.",
    "Valio Aimo sees the order, but doesn't decide it. The chef gets the reason in plain words, in Finnish too, and approves on the phone.",
    "And some things are never allowed. A school's order over its limit is refused outright, and so is an agent approving its own order.",
    "All of it comes from the mandate the buyer issued, run by plain code in four tiers. No language model approves an order or relaxes a rule.",
    "On our own test cases, not a held-out set: zero mandate violations across five models. And it's open: MCP, OpenAPI, Peppol, MIT.",
    "Next: buyer login, Valio Aimo's order API and contract prices.",
    "Sous. Agents do the buying, and the customer stays in control.",
]
RESPELL = [("Sous", "Soo"), ("CO₂", "C O two"), ("17,546", "seventeen thousand five hundred and forty-six"),
           ("OpenAPI", "Open A P I"), ("API", "A P I"), ("MCP", "M C P"), ("MIT", "M I T"), ("FYI", "F Y I")]


def tts_text(s: str) -> str:
    for a, b in RESPELL:
        s = s.replace(a, b)
    return s


def gen(n: int, model: str) -> None:
    voice, keyname = SPEAKERS[SPEAKER[n]]
    c = el.client(keyname)
    text = tts_text(BEATS[n])
    if model == "eleven_v3":  # Rene's pick (sample D): the voice's own defaults on v3; v3 takes no request stitching
        body = {"text": text, "model_id": model,
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True}}
    else:
        body = {"text": text, "model_id": model,
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.85, "style": 0.15, "use_speaker_boost": True, "speed": 0.85},
                "previous_text": tts_text(BEATS[n - 1]) if n else None,
                "next_text": tts_text(BEATS[n + 1]) if n + 1 < len(BEATS) else None}
    r = c.post(f"/v1/text-to-speech/{voice}/with-timestamps", params={"output_format": "mp3_44100_192"}, json=body)
    if r.status_code != 200:
        raise SystemExit(f"beat {n}: HTTP {r.status_code} {r.text[:300]}")
    j = r.json()
    mp3 = ROOT / "audio" / f"vo_{n}.mp3"
    mp3.write_bytes(base64.b64decode(j["audio_base64"]))
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp3)],
                               capture_output=True, text=True).stdout)
    al = j.get("normalized_alignment") or j.get("alignment")
    (ROOT / "audio" / f"vo_{n}.json").write_text(json.dumps({"script": BEATS[n], "tts": text, "model": model, "duration": dur,
                                                             "speaker": SPEAKER[n], "alignment": al}, ensure_ascii=False))
    words = len(BEATS[n].split())
    print(f"beat {n} ({SPEAKER[n]}): {dur:5.2f} s, {words} words, {words / dur * 60:.0f} wpm")


if __name__ == "__main__":
    model = "eleven_v3"
    args = [a for a in sys.argv[1:]]
    if "--model" in args:
        i = args.index("--model"); model = args[i + 1]; del args[i:i + 2]
    beats = [int(a) for a in args] or list(range(len(BEATS)))
    for n in beats:
        gen(n, model)
