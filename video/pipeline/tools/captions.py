"""Caption chunks per beat from the ElevenLabs character timestamps -> data/captions.json.

Sentences of the TTS text and the script text line up one to one (the respellings never add or drop a full stop),
so each script sentence gets the time span of its TTS sentence; long sentences are split at a comma or colon,
with time shared by length. Also prints each sentence's start, for timing the visuals.
"""
import json, pathlib, re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPLIT = re.compile(r"(?<=[.!?])\s+")
MAX = 64


def spans(text: str, chars: list[str], t0: list[float], t1: list[float]):
    """(start, end) time of each sentence of `text`, located in the aligned character stream."""
    joined = "".join(chars)
    out, pos = [], 0
    for s in SPLIT.split(text.strip()):
        i = joined.find(s[:12], pos)
        if i < 0:
            i = pos
        j = min(len(chars) - 1, i + len(s) - 1)
        out.append((t0[i], t1[j]))
        pos = j + 1
    return out


def chunks(sentence: str) -> list[str]:
    if len(sentence) <= MAX:
        return [sentence]
    parts = re.split(r"(?<=[,:])\s+", sentence)
    out, cur = [], ""
    for p in parts:
        if cur and len(cur) + 1 + len(p) > MAX:
            out.append(cur); cur = p
        else:
            cur = (cur + " " + p).strip()
    if cur:
        out.append(cur)
    return out


def main():
    allc = {}
    for f in sorted((ROOT / "audio").glob("vo_*.json"), key=lambda p: int(p.stem.split("_")[1])):
        n = int(f.stem.split("_")[1])
        d = json.loads(f.read_text())
        al = d["alignment"]
        sp = spans(d["tts"], al["characters"], al["character_start_times_seconds"], al["character_end_times_seconds"])
        sents = SPLIT.split(d["script"].strip())
        assert len(sents) == len(sp), (n, len(sents), len(sp))
        caps = []
        for s, (a, b) in zip(sents, sp):
            cs = chunks(s)
            total = sum(len(c) for c in cs)
            t = a
            for c in cs:
                dt = (b - a) * len(c) / total
                caps.append({"t0": round(t, 3), "t1": round(t + dt, 3), "text": c})
                t += dt
        allc[n] = {"duration": d["duration"], "sentences": [{"t0": a, "t1": b, "text": s} for s, (a, b) in zip(sents, sp)], "captions": caps}
        print(n, f"{d['duration']:.2f}s", " | ".join(f"{a:.1f}-{b:.1f}" for a, b in sp))
    (ROOT / "data" / "captions.json").write_text(json.dumps(allc, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
