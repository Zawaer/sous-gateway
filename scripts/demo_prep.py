"""Get a gateway ready for Toivo's live demo (stage pitch and peer review), over HTTP only.

    uv run python scripts/demo_prep.py --base https://<server> --check-only     # morning check: health + links
    uv run python scripts/demo_prep.py --base https://<server>                  # + one private demo sandbox
    uv run python scripts/demo_prep.py --base https://<server> --reset          # a fresh sandbox even if one is usable

Without --check-only it also:
- creates ONE private demo sandbox (POST /sandbox), or reuses the one in --out if it is still active, has hours
  left and its seeded held order is still waiting (--reset always makes a new one);
- checks the seeded held order: pending_approval with approve_by in the future (gateway clock);
- warms the Valio Aimo cache with at most 2 live lookups: the quickstart's hotel milk quote and the hospital's
  substitute case. It places no order and runs no attack: those are clicked live.
- writes the tokens, links and stage steps to --out (JSON) and a printable .txt next to it, mode 600, outside
  any git checkout (default ~/sous-demo-<date>.json). Tokens are printed to stdout only as their last 4 chars.

Exit 1 with the list of problems if anything a judge would hit is broken. Nothing from `sous` is imported.
Sandboxes live 24 h (SOUS_SANDBOX_TTL_HOURS), and the seeded held order expires at the Monday delivery's cut-off
(Sun ~17:00 minus the approval margin); see docs/demo-runbook.md for when to run it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

TZ = ZoneInfo("Europe/Helsinki")
PAGES = ["/rules", "/holds", "/ui/", "/ui/break.html", "/buyer/"]
HOTEL_MILK = ["464119", "476240", "464201"]  # the quickstart's quote (sous/sandbox.py quickstart)
HOSPITAL_TOMATOES = "468376"  # the hospital prompt's usual product (sous/sandbox.py prompts)
MIN_HOURS_LEFT = 3  # reuse a sandbox only if it outlives the next demo slot comfortably
STAGE_STEPS = [
    ("Hotel milk over MCP", "In the Claude Code terminal, send the hotel prompt (1,000 L lactose-free milk).",
     "It searches (free), gets a signed live quote and orders: ordered, tier Auto + FYI (spend.notify) when the "
     "total is EUR 1,000-2,500; above EUR 2,500 or the EUR 2.50/L cap it is held instead (still a valid story)."),
    ("Hospital substitute", "Send the hospital prompt (substitutes for chopped tomatoes 468376, order 4 packs).",
     "Held for a human (substitution, maybe allergen.unknown) with 'decide by' a clock time from Valio's cut-off. "
     "Approve it on the phone (inbox link, approver token)."),
    ("Break page", "On the break page click 'agent approves its own order'.",
     "403, blocked: agents never decide their own orders; the event is in the audit log."),
    ("Seeded renewal (if time)", "In the seller inbox open the held standing-order renewal.",
     "Held by the drift rules (standing.quantity_creep / quantity_trend / cost_drift); approve or reject."),
]


def last4(token: str | None) -> str:
    return f"...{token[-4:]}" if token else "(none)"


def next_delivery_day(today: date) -> date:
    """Next Monday (tomorrow on a Sunday), as the gateway's sandbox prompts use."""
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def default_out() -> Path:
    return Path.home() / f"sous-demo-{datetime.now(TZ):%Y-%m-%d}.json"


def inside_git_checkout(path: Path) -> Path | None:
    """The git checkout containing `path`, if any (a .git dir or worktree file in it or a parent)."""
    for d in [path.parent, *path.parent.parents]:
        if (d / ".git").exists():
            return d
    return None


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)  # also when the file already existed with looser bits


def _dt(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


class Prep:
    def __init__(self, base: str, client: httpx.Client | None = None, out: Path | None = None):
        self.base = base.rstrip("/")
        self.c = client or httpx.Client(base_url=self.base, timeout=30, follow_redirects=True,
                                        headers={"User-Agent": "sous-demo-prep"})
        self.out = out
        self.problems: list[str] = []
        self.warnings: list[str] = []
        self.log: list[str] = []
        self.health: dict = {}
        self.milk, self.tomatoes = HOTEL_MILK, HOSPITAL_TOMATOES  # tests point these at their fake catalog
        self.check_mcp = True  # tests without the app lifespan (MCP's session manager) turn it off

    # --- output -------------------------------------------------------------------------------------------
    def say(self, ok: bool | None, msg: str) -> None:
        mark = {True: "ok  ", False: "FAIL", None: "warn"}[ok]
        line = f"[{mark}] {msg}"
        self.log.append(line)
        print(line, flush=True)
        if ok is False:
            self.problems.append(msg)
        elif ok is None:
            self.warnings.append(msg)

    def _get(self, path: str, **kw) -> httpx.Response | None:
        try:
            return self.c.get(path, **kw)
        except httpx.HTTPError as e:
            self.say(False, f"GET {path}: {type(e).__name__}: {e}")
            return None

    # --- 1. health and links ------------------------------------------------------------------------------
    def check_health(self) -> None:
        r = self._get("/health")
        if r is None:
            return
        if r.status_code != 200:
            self.say(False, f"/health answered {r.status_code}")
            return
        h = self.health = r.json()
        self.say(h.get("ok") is True, f"/health ok={h.get('ok')} version={h.get('version')} storage={h.get('storage')}")
        commit = h.get("commit") or "unknown"
        self.say(None if commit == "unknown" else True, f"commit {commit}"
                 + (" (SOUS_COMMIT not set: can't tell what is deployed)" if commit == "unknown" else ""))
        clock = h.get("clock") or {}
        if clock.get("mode") == "demo":
            self.say(None, f"clock mode DEMO: business time {clock.get('now')} (real {clock.get('real_now')}); "
                           "cut-offs follow it, the UI shows a banner")
        else:
            self.say(clock.get("mode") == "real", f"clock mode {clock.get('mode')} now {clock.get('now')}")
        va = h.get("valio_aimo") or {}
        sb = va.get("shared_budget") or {}
        self.say(sb.get("ok") is True, f"Valio shared budget ok={sb.get('ok')} backend={sb.get('backend')} "
                                        f"used {sb.get('used_this_minute')}/{sb.get('limit')} this minute"
                 + (f" error={sb.get('error')}" if sb.get("error") else ""))
        if va.get("circuit") == "open":
            self.say(False, f"Valio circuit OPEN (Valio Aimo pushing back); retry in {va.get('circuit_retry_after_s')} s")
        if not h.get("approver_code_required"):
            self.say(None, "approver code NOT required on this gateway (anyone can approve)")

    def check_links(self, extra: list[str] = ()) -> None:
        for p in [*PAGES, *extra]:
            r = self._get(p)
            if r is not None:
                self.say(r.status_code == 200, f"GET {p} -> {r.status_code}")
        if not self.check_mcp:
            return
        try:
            r = self.c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "sous-demo-prep", "version": "1"}}},
                headers={"Accept": "application/json, text/event-stream"})
            self.say(r.status_code == 200 and "serverInfo" in r.text, f"MCP initialize at /mcp -> {r.status_code}")
        except httpx.HTTPError as e:
            self.say(False, f"MCP initialize at /mcp/: {type(e).__name__}: {e}")

    # --- 2. the demo sandbox ------------------------------------------------------------------------------
    def server_now(self) -> datetime:
        return _dt((self.health.get("clock") or {}).get("now")) or datetime.now(TZ)

    def held_order(self, s: dict) -> dict | None:
        oid = (s.get("seeded") or {}).get("held_order_id")
        if not oid:
            self.say(False, "the sandbox has no seeded held order (seeding failed on the gateway; see its logs)")
            return None
        r = self._get(f"/orders/{oid}", headers={"X-Approver-Code": s["approver_token"]})
        if r is None:
            return None
        if r.status_code != 200:
            self.say(False, f"seeded held order {oid}: GET answered {r.status_code}")
            return None
        return r.json()

    def check_held(self, s: dict) -> bool:
        o = self.held_order(s)
        if o is None:
            return False
        by, now = _dt(o.get("approve_by")), self.server_now()
        ok = o.get("status") == "pending_approval" and by is not None and by > now
        left = f"{(by - now).total_seconds() / 3600:.1f} h left" if by else "no approve_by"
        self.say(ok, f"seeded held order {o['id']}: {o.get('status')}, approve_by {o.get('approve_by')} ({left})")
        return ok

    def reusable(self, s: dict) -> str | None:
        """Why the sandbox in the --out file can't be reused, or None if it can."""
        r = self._get(f"/sandbox/{s['sandbox']['id']}")
        if r is None or r.status_code != 200:
            return f"GET /sandbox/{s['sandbox']['id']} answered {r.status_code if r is not None else 'nothing'}"
        st = r.json()
        if st.get("status") != "active":
            return f"it is {st.get('status')}"
        exp = _dt(st.get("expires_at"))
        if not exp or exp - datetime.now(TZ) < timedelta(hours=MIN_HOURS_LEFT):
            return f"it expires at {st.get('expires_at')}"
        if st.get("quotes_left", 0) < 6:
            return f"only {st.get('quotes_left')} live lookups left"
        o = self.held_order(s)
        if not o or o.get("status") != "pending_approval":
            return f"its seeded order is {o.get('status') if o else 'gone'} (already decided at an earlier demo?)"
        by = _dt(o.get("approve_by"))
        if not by or by <= self.server_now() + timedelta(hours=1):
            return f"its seeded order's approve_by {o.get('approve_by')} is within the hour"
        return None

    def sandbox(self, reset: bool) -> dict | None:
        if not reset and self.out and self.out.exists():
            try:
                old = json.loads(self.out.read_text())
            except (OSError, ValueError):
                old = None
            if old and old.get("base") == self.base and old.get("sandbox_response"):
                why = self.reusable(old["sandbox_response"])
                if why is None:
                    self.say(True, f"reusing sandbox {old['sandbox_response']['sandbox']['id']} from {self.out}")
                    return old["sandbox_response"]
                self.say(None, f"not reusing sandbox {old['sandbox_response']['sandbox']['id']}: {why}; making a new one")
        try:
            r = self.c.post("/sandbox")
        except httpx.HTTPError as e:
            self.say(False, f"POST /sandbox: {type(e).__name__}: {e}")
            return None
        if r.status_code != 200:
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            self.say(False, f"POST /sandbox -> {r.status_code} {body.get('error', '')}: {body.get('message', r.text[:200])}")
            return None
        s = r.json()
        self.say(True, f"sandbox {s['sandbox']['id']} created, expires {s['sandbox']['expires_at']}, "
                       f"agent token {last4(s['agent_token'])}, approver token {last4(s['approver_token'])}")
        return s

    def warm(self, s: dict) -> None:
        """At most 2 live lookups; no order. The price cache lasts ~10 min and availability ~1 min, so this mainly
        proves the live path works end to end and starts the demo with warm prices."""
        by = {m["scenario"]: m for m in s["mandates"]}
        auth = {"Authorization": f"Bearer {s['agent_token']}"}
        day = next_delivery_day(datetime.now(TZ).date()).isoformat()
        hotel, hosp = by["hotel"], by["hospital"]
        body = {"agent_id": hotel["agent_id"], "mandate_id": hotel["mandate_id"], "delivery_date": day,
                "rationale": "demo prep: warming the cache for the hotel milk demo (no order)",
                "items": [{"product_ids": self.milk, "amount": 100, "unit": "L"}]}
        try:
            r = self.c.post("/quotes", json=body, headers=auth)
            if r.status_code == 200:
                q = r.json()["quote"]
                pv = (q.get("mandate_preview") or {}).get("outcome")
                calls = q.get("valio_api_calls") or {}
                self.say(True, f"hotel milk quote {q['id']}: EUR {q.get('total_ex_vat')} ex VAT, {len(q.get('lines', []))} "
                               f"line(s), preview {pv}, Valio calls {calls}")
                if not q.get("lines"):
                    self.say(False, "hotel milk quote has no lines: the live milk demo would find nothing to order")
            else:
                self.say(False, f"hotel milk quote -> {r.status_code}: {r.text[:200]}")
        except httpx.HTTPError as e:
            self.say(False, f"hotel milk quote: {type(e).__name__}: {e}")
        try:
            r = self.c.get(f"/products/{self.tomatoes}/substitutes", headers=auth, params={
                "mandate_id": hosp["mandate_id"], "agent_id": hosp["agent_id"], "amount": 40, "delivery_date": day})
            if r.status_code == 200:
                subs = r.json().get("options") or []
                self.say(True if subs else None, f"hospital substitutes for {self.tomatoes}: {len(subs)} found")
            else:
                self.say(False, f"hospital substitutes -> {r.status_code}: {r.text[:200]}")
        except httpx.HTTPError as e:
            self.say(False, f"hospital substitutes: {type(e).__name__}: {e}")
        r = self._get(f"/sandbox/{s['sandbox']['id']}")
        if r is not None and r.status_code == 200:
            st = r.json()
            self.say(True, f"sandbox lookups used {st.get('quotes_used')}/{st.get('quotes_limit')}")

    # --- 3. the handoff -----------------------------------------------------------------------------------
    def handoff(self, s: dict) -> dict:
        qs = s["quickstart"]
        sid = s["sandbox"]["id"]
        return {
            "base": self.base, "prepared_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "commit": self.health.get("commit"), "clock": self.health.get("clock"),
            "sandbox_id": sid, "expires_at": s["sandbox"]["expires_at"],
            "agent_token": s["agent_token"], "approver_token": s["approver_token"],
            "held_order_id": (s.get("seeded") or {}).get("held_order_id"),
            "open_in_order": [
                {"what": "Laptop tab 1: seller inbox (paste the approver token when asked)", "url": qs["seller_ui"]},
                {"what": "Laptop tab 2: buyer page (hotel)", "url": qs["buyer_ui"]},
                {"what": "Laptop tab 3: break page (paste the agent token)", "url": qs["break_ui"]},
                {"what": "Phone: seller inbox (approver token)", "url": qs["seller_ui"]},
            ],
            "mcp_connect": qs["claude_code"],
            "prompts": s.get("prompts", []),
            "stage_steps": [{"step": a, "do": b, "expect": c} for a, b, c in STAGE_STEPS],
            "warnings": self.warnings,
            "sandbox_response": s,
        }

    def text(self, h: dict) -> str:
        out = [f"Sous demo handoff  {h['prepared_at']}", f"Server  {h['base']}  commit {h['commit']}  "
               f"clock {(h.get('clock') or {}).get('mode')}",
               f"Sandbox {h['sandbox_id']}  expires {h['expires_at']}  held order {h['held_order_id']}", "",
               "TOKENS (never on the projector)", f"  agent token     {h['agent_token']}",
               f"  approver token  {h['approver_token']}", "", "OPEN IN THIS ORDER"]
        out += [f"  {i}. {x['what']}\n     {x['url']}" for i, x in enumerate(h["open_in_order"], 1)]
        out += ["", "MCP (terminal, before going on)", f"  {h['mcp_connect']}", "", "STAGE DEMO"]
        for i, st in enumerate(h["stage_steps"], 1):
            out += [f"  {i}. {st['step']}: {st['do']}", f"     expect: {st['expect']}"]
        out += ["", "PROMPTS (copy-paste)"]
        for p in h["prompts"]:
            out += [f"  - {p['title']}:", f"    {p['prompt']}"]
        if h["warnings"]:
            out += ["", "WARNINGS"] + [f"  - {w}" for w in h["warnings"]]
        return "\n".join(out) + "\n"

    def save(self, h: dict) -> tuple[Path, Path]:
        txt = self.out.with_suffix(".txt")
        write_private(self.out, json.dumps(h, indent=2, ensure_ascii=False) + "\n")
        write_private(txt, self.text(h))
        return self.out, txt

    # --- run ----------------------------------------------------------------------------------------------
    def run(self, check_only: bool = False, reset: bool = False) -> int:
        print(f"Sous demo prep against {self.base}", flush=True)
        self.check_health()
        self.check_links()
        if check_only:
            return self.finish()
        if self.out is None:
            raise ValueError("out is required unless check_only")
        repo = inside_git_checkout(self.out.resolve())
        if repo:
            self.say(False, f"--out {self.out} is inside the git checkout {repo}; tokens must not go there")
            return self.finish()
        s = self.sandbox(reset)
        if s is None:
            return self.finish()
        self.check_held(s)
        qs = s["quickstart"]
        self.check_links_for(qs)
        self.warm(s)
        j, t = self.save(self.handoff(s))
        print(f"\nHandoff written (mode 600): {j}\n                            {t}")
        print(f"Open first: {qs['seller_ui']}")
        return self.finish()

    def check_links_for(self, qs: dict) -> None:
        for key in ("seller_ui", "break_ui", "buyer_ui", "status"):
            url = qs.get(key, "")
            path = url[len(self.base):] if url.startswith(self.base) else url
            if not url.startswith(self.base):
                self.say(None, f"quickstart {key} points at {url.split('?')[0]}, not {self.base} (SOUS_PUBLIC_BASE_URL?)")
            r = self._get(path.split("#")[0])
            if r is not None:
                self.say(r.status_code == 200, f"{key} link -> {r.status_code}")

    def finish(self) -> int:
        if self.problems:
            print(f"\nNOT READY: {len(self.problems)} problem(s) a judge would hit:", file=sys.stderr)
            for p in self.problems:
                print(f"  - {p}", file=sys.stderr)
            return 1
        print(f"\nREADY ({len(self.warnings)} warning(s)).")
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="gateway URL, e.g. https://135-181-8-209.sslip.io")
    ap.add_argument("--out", type=Path, default=None, help="handoff JSON (default ~/sous-demo-<date>.json); "
                                                          "a .txt is written next to it")
    ap.add_argument("--check-only", action="store_true", help="health and links only; no sandbox, no Valio calls")
    ap.add_argument("--reset", action="store_true", help="always create a new sandbox (e.g. before the final, "
                                                         "after the morning one's held order was approved)")
    a = ap.parse_args(argv)
    out = (a.out.expanduser() if a.out else default_out())
    with httpx.Client(base_url=a.base.rstrip("/"), timeout=30, follow_redirects=True,
                      headers={"User-Agent": "sous-demo-prep"}) as c:
        return Prep(a.base, c, None if a.check_only else out).run(a.check_only, a.reset)


if __name__ == "__main__":
    sys.exit(main())
