"""data/session-<env>-all.json -> tools/src/session-data.js (window.SESSION), tokens already redacted."""
import json, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
env = sys.argv[1] if len(sys.argv) > 1 else "staging"
d = json.loads((ROOT / "data" / f"session-{env}-all.json").read_text())
(ROOT / "tools" / "src" / "session-data.js").write_text("window.SESSION = " + json.dumps(d, ensure_ascii=False) + ";\n")
print("wrote session-data.js", len(d["events"]), "events from", d["base"], d["sandbox"])
