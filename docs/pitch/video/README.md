# Draft pitch video for Sous (no voice)

A screen-recorded draft of [video-script.md](../video-script.md), beat by beat, with the script's VO lines burned
in as captions. It exists so the voice-over session only has to record audio and re-shoot the few shots marked
**[HUMAN]**. Made on Fri 13.11 around 23:30 Helsinki time.

- Draft: `/data/attachments/s_G5o6eFRtoL/video/valtakirja-draft.mp4` (119.5 s, 1920x1080, H.264, about 8 MB)
- Raw clips for re-cutting: `/data/attachments/s_G5o6eFRtoL/video/clips/` (each beat separately, 1920x1080, no captions)
- MCP transcript behind the terminal shot: `/data/attachments/s_G5o6eFRtoL/video/mcp_transcript.json`

Video files stay out of git. Everything on screen is a real gateway response; nothing is mocked.

## Shot list

| Time | Clip | Source | What's on screen | Needs a human |
|---|---|---|---|---|
| 0:00–0:10 | `00a_hook_puntari_placeholder` + `03b_hotel_order_ui` | still + staging | Split: left a placeholder ("13 min", "call wholesalers on Saturday"), right the hotel order PO-52A984B0 opening | **Replace the left half with Toivo's Puntari screenshot** |
| 0:10–0:20 | `01_problem` | still | "Agents will buy. Who defines how?", Sous, "every agent carries a valtakirja" | |
| 0:20–0:35 | `03a_mcp_terminal` | staging, MCP | Terminal render of a real MCP session: `search_products` (0 Valio calls) → `request_quote` (2 calls; the cut-off skip line for 464119) → `place_order` → PO-52A984B0, notify, signed receipt | Optional: re-record in real Claude Code (see below) |
| 0:35–0:42 | `03b_hotel_order_ui` | staging UI | Order panel, Auto + FYI, scroll to **Ed25519 signature valid** | |
| 0:42–0:48 | `04a_hospital_ask_ui` | staging UI | PO-FCE6F236 held: no allergen data (allergen.unknown) + substitute; "Decide within 41 h", Approve button | |
| 0:48–0:58 | `04b_phone_approve_local` | **local gateway**, phone viewport | Same scene (PO-9001EF39) on a 390 px phone: scroll, note, **Approve order** → "approved and ordered", Human decision shown | **Re-shoot on a real phone against staging** with Rene's approver code (or keep this) |
| 0:58–1:12 | `05_standing_drift_local` | **local gateway** | Standing order SO-590DEA84: Needs approval, quantity_creep +18 %, quantity_trend, cost_drift +€134.70 (+16 %), trend bars, renewals marked "simulated history" | Optional: re-run on staging (`demo_standing.py` needs the approver code there) |
| 1:12–1:24 | `06_audit_asof` | staging UI | Audit log "Chain intact, 85 events verified, mariadb system versioning", then PO-5B857D45 "Mandate at decision time" + order versions (Needs approval → Rejected) | "Since then" reads *unchanged*: for a then-vs-now difference, edit a mandate after its order is decided (needs the approver code) and re-shoot |
| 1:24–1:40 | `07_trust` | still | Rules in code / Signed / Open | |
| 1:40–1:52 | `08_numbers` | still | 45/45 · 36/36 · ≤2 | |
| 1:52–1:59.5 | `09_close` | still | Name, one-liner, prod URL, GitHub | |

Every shot has a small top-right label (DRAFT, source, order id, or the **[HUMAN]** action). Cut them in the
final edit; they come from `scripts/record_demo_shots.json` (`tag`), so dropping the field and re-running
`assemble` removes them.

## Things that differ from the script

- **Demo 1 product and total.** The live quote chose *Valio coffee milk 1 l lactose free UHT*, 1,000 × €1.30 =
  **€1,300** ex VAT (Valio Eila was listed under "more expensive per unit"). The caption says
  "Valio lactose-free coffee milk, thirteen hundred euros"; **change the VO line** in video-script.md to match,
  or re-record when the mock prices Eila cheapest.
- **Terminal shot** is a real MCP Streamable HTTP session (`mcp` Python client, same tools Claude Code would call),
  rendered frame by frame from the transcript, not Claude Code's own UI. To use Claude Code instead:
  `claude mcp add --transport http sous https://staging.135-181-63-134.sslip.io/mcp` and the prompt from the
  script; screen-record the terminal.
- **Phone approval and standing order** run on a local gateway (same code, same Valio mock, SQLite, an approver
  code set just for this) because the staging approver code is with Rene. The order panel on the local gateway says
  "Point-in-time history needs MariaDB", which the phone shot briefly shows; the AS OF shot itself is from staging.
- **Product name.** Captions and stills say **Sous** (the rename on Fri 13.11 night); "valtakirja" appears only as the
  word for the signed mandate. The UI clips were recorded before staging got the renamed UI, so their header still says
  "Valtakirja": **[HUMAN/agent] re-run the `ui order`, `ui ask` and `ui audit` steps once staging is redeployed** (GET
  only, no Valio calls) and re-run `assemble`. The terminal line "connected to valtakirja" is the real MCP server
  name at the time.
- **No ntfy push** is shown (not configured on the gateways used).
- The hook's right half shows the order panel while "How it happened" is still loading for the first seconds.

## Created on staging (Fri 13.11, ~23:33–23:36 Helsinki)

- PO-52A984B0 (M-HOTEL-AURORA, 1,000 L, notify, ordered), quote Q-892818EB, via MCP
- PO-FCE6F236 (M-HOSPITAL-KITCHEN, substitute 466203, ask, **left pending**), via `scripts/demo.py 2`
- Valio Aimo API calls: 4 on staging, 8 on the local gateway (hospital quote 2, standing quote + renewal 6): 12 in total.

## How it was made

`scripts/record_demo.py` (see its docstring) with Playwright + Chromium and ffmpeg:

```sh
uv run python scripts/record_demo.py mcp --base https://staging.135-181-63-134.sslip.io --out $OUT
uv run --with pillow python scripts/record_demo.py term $OUT/mcp_transcript.json $OUT/clips/03a_mcp_terminal.mp4 --seconds 15
uv run --with playwright --with pillow python scripts/record_demo.py ui order    --base $STAGING --order PO-... --out $OUT/clips/03b_hotel_order_ui.mp4
uv run --with playwright --with pillow python scripts/record_demo.py ui ask      --base $STAGING --order PO-... --out $OUT/clips/04a_hospital_ask_ui.mp4
SOUS_APPROVER_CODE=... uv run --with playwright --with pillow python scripts/record_demo.py ui approve --base $LOCAL --order PO-... --out $OUT/clips/04b_phone_approve_local.mp4
uv run --with playwright --with pillow python scripts/record_demo.py ui standing --base $LOCAL --so SO-...   --out $OUT/clips/05_standing_drift_local.mp4
uv run --with playwright --with pillow python scripts/record_demo.py ui audit    --base $STAGING --order PO-... --out $OUT/clips/06_audit_asof.mp4
uv run --with pillow python scripts/record_demo.py cards $OUT/clips
uv run python scripts/record_demo.py assemble $OUT        # cuts per scripts/record_demo_shots.json, burns captions
```

(`python -m playwright install --with-deps chromium` once.) The hospital order came from `scripts/demo.py 2`, the
standing order from `scripts/demo_standing.py`. Browser clips are screenshot sequences (about 12 per second, at
1280x720 CSS px × 1.5 = 1920x1080) so text stays sharp; the approver code goes straight into the browser's
localStorage and never appears on screen. `record_demo.py` refuses the production host for `mcp` and `ui`.
Captions: each beat's VO line is split into sentences and timed by length over the beat (`vo`, `vo_dur` in the
shot list); edit the JSON and re-run `assemble` (about a minute) to re-cut.
