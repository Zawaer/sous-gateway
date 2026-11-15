"""Shorten long pauses inside each VO beat (eleven_v3 leaves 0.5-0.9 s gaps) without changing the voice.

audio/raw_v3/vo_<n>.{mp3,json} -> audio/vo_<n>.{mp3,json}; every silence longer than KEEP is cut to KEEP and the
character timestamps are shifted by what was removed before them, so captions stay in sync.
"""
import json, pathlib, re, shutil, subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "audio" / "raw_v3"
KEEP = 0.26


def silences(p):
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(p), "-af", "silencedetect=noise=-42dB:d=0.28", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    s = [float(x) for x in re.findall(r"silence_start: ([0-9.]+)", err)]
    e = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", err)]
    return list(zip(s, e))


def main():
    RAW.mkdir(exist_ok=True)
    for f in sorted((ROOT / "audio").glob("vo_*.json")):
        n = f.stem.split("_")[1]
        if not (RAW / f"vo_{n}.json").exists():
            shutil.move(str(f), RAW / f.name)
            shutil.move(str(ROOT / "audio" / f"vo_{n}.mp3"), RAW / f"vo_{n}.mp3")
    total_in = total_out = 0.0
    for jf in sorted(RAW.glob("vo_*.json"), key=lambda p: int(p.stem.split("_")[1])):
        n = jf.stem.split("_")[1]
        d = json.loads(jf.read_text())
        src = RAW / f"vo_{n}.mp3"
        dur = d["duration"]
        cuts = []  # (start, end) of audio removed
        for s, e in silences(src):
            if e >= dur - 0.05:  # trailing silence: keep a short tail
                cuts.append((min(dur, s + 0.25), dur)); continue
            if s <= 0.05:  # leading silence
                cuts.append((0.0, max(0.0, e - 0.08))); continue
            if e - s > KEEP:
                mid = (s + e) / 2
                cuts.append((mid - (e - s - KEEP) / 2, mid + (e - s - KEEP) / 2))
        keep, t = [], 0.0
        for a, b in cuts:
            if a > t:
                keep.append((t, a))
            t = max(t, b)
        if t < dur:
            keep.append((t, dur))
        parts = "".join(f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS[p{i}];" for i, (a, b) in enumerate(keep))
        fc = parts + "".join(f"[p{i}]" for i in range(len(keep))) + f"concat=n={len(keep)}:v=0:a=1[o]"
        out = ROOT / "audio" / f"vo_{n}.mp3"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex", fc, "-map", "[o]", "-b:a", "192k", str(out)], check=True)

        def shift(x):
            removed = sum(min(b, x) - a for a, b in cuts if a < x)
            return max(0.0, x - removed)
        al = d["alignment"]
        al = {**al, "character_start_times_seconds": [round(shift(x), 3) for x in al["character_start_times_seconds"]],
              "character_end_times_seconds": [round(shift(x), 3) for x in al["character_end_times_seconds"]]}
        nd = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                                  capture_output=True, text=True).stdout)
        (ROOT / "audio" / f"vo_{n}.json").write_text(json.dumps({**d, "duration": nd, "alignment": al, "tightened_from": dur}, ensure_ascii=False))
        total_in += dur; total_out += nd
        print(f"beat {n}: {dur:5.2f} -> {nd:5.2f} s ({len(cuts)} cuts)")
    print(f"total {total_in:.1f} -> {total_out:.1f} s")


if __name__ == "__main__":
    main()
