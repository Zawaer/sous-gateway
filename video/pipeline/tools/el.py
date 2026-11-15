"""ElevenLabs helper: key from the environment, or from the repo's .env (ELEVENLABS= or ELEVENLABS_API_KEY=;
SOUS_ENV_FILE overrides the path). Never printed."""
import os, pathlib, httpx
ROOT = pathlib.Path(__file__).resolve().parent.parent
def key(names=("ELEVENLABS", "ELEVENLABS_API_KEY")):
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    env = pathlib.Path(os.environ.get("SOUS_ENV_FILE") or pathlib.Path(__file__).resolve().parents[3] / ".env")
    for line in (env.read_text().splitlines() if env.exists() else []):
        k, _, v = line.partition("=")
        if k.strip() in names and v.strip():
            return v.strip().strip('"').strip("'")
    raise SystemExit(f"no {names[0]} in the environment or .env")
def client(name="ELEVENLABS"):
    names = (name,) if name != "ELEVENLABS" else ("ELEVENLABS", "ELEVENLABS_API_KEY")
    return httpx.Client(base_url="https://api.elevenlabs.io", headers={"xi-api-key": key(names)}, timeout=180)
if __name__ == "__main__":
    import sys, json
    c = client()
    r = c.get("/v2/voices", params={"search": sys.argv[1] if len(sys.argv) > 1 else "Rene", "page_size": 30})
    print(r.status_code)
    for v in r.json().get("voices", []):
        print(v["voice_id"], "|", v["name"], "|", v.get("category"), "|", v.get("labels"), "|", (v.get("description") or "")[:80], "|", v.get("created_at_unix"))
    u = c.get("/v1/user/subscription")
    if u.status_code == 200:
        j = u.json(); print("tier", j.get("tier"), "chars", j.get("character_count"), "/", j.get("character_limit"))
