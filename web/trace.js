// Sous trust board (/ui/trace.html?order=PO-...): one order's journey from the buyer's agent to Valio Aimo, hop by hop.
// Plain JS, no build step, no libraries. Every value comes from the gateway's REST API on the same origin
// (?api=http://host:port overrides it for development, like app.js); a missing field shows "–" or "not recorded".
// Signatures (quote, mandate, receipt) are verified here with WebCrypto Ed25519 against /.well-known/jwks.json.
"use strict";

const QS = new URLSearchParams(location.search);
const API = QS.get("api") || "";
const SANDBOX = (QS.get("sandbox") || "").trim().toUpperCase();
const DEMO_MANDATES = ["M-HOTEL-AURORA", "M-HOSPITAL-KITCHEN", "M-SCHOOL-ESPOO", "M-KAARNA-CATERING"];
const TOKEN_KEY = "sous-trace-token";
const NOTE_KEY = "sous-trace-note";
let PENDING_NOTE = "";  // shown once after "Try it live" lands on its order
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)");
const $ = (s, el = document) => el.querySelector(s);

// --- small helpers ------------------------------------------------------------------------------------------------
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const dash = (v) => (v === null || v === undefined || v === "" ? "–" : esc(v));
const eur = (v) => (v == null ? "–" : new Intl.NumberFormat("en-IE", { style: "currency", currency: "EUR" }).format(v));
const num = (v) => (v == null ? "–" : (Math.round(v * 1000) / 1000).toLocaleString("en-IE"));
const TZ = "Europe/Helsinki";
const day = (d) => { if (!d) return "–"; const x = new Date(d.length === 10 ? d + "T12:00:00" : d); return isNaN(x) ? esc(d) : x.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: TZ }); };
const when = (d) => { if (!d) return "–"; const x = new Date(d); return isNaN(x) ? esc(d) : x.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: TZ }); };
const short = (h, n = 10) => (h ? esc(String(h).slice(0, n)) + "…" : "–");
const unitName = (u) => ({ SALES_UNIT: "packs", ST: "pcs", KG: "kg", L: "L" }[u] || u || "");
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
const isQa = (o) => /^qa[-_:]/i.test(o.agent_id || "") || /^M-QA-/i.test(o.mandate_id || "") || /^QA\b/.test(o.buyer || "");
const store = {
  get: (s, k) => { try { return s.getItem(k) || ""; } catch { return ""; } },
  set: (s, k, v) => { try { v ? s.setItem(k, v) : s.removeItem(k); } catch { /* private mode */ } },
};

// Reads: the seller UI's saved approver code (same origin) and a token pasted here (sandbox agent or approver
// token, kept for this tab only) unlock non-demo data. Demo mandates are public without either.
function headers() {
  const h = {};
  const code = store.get(localStorage, "vk-approver-code" + (SANDBOX ? ":" + SANDBOX : "")) || store.get(localStorage, "vk-approver-code");
  if (code) h["x-approver-code"] = code;
  const tok = store.get(sessionStorage, TOKEN_KEY);
  if (tok) h.authorization = "Bearer " + tok;
  return h;
}
async function get(path, opts = {}) {
  const res = await fetch(API + path, { ...opts, headers: { ...headers(), ...(opts.headers || {}) } });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { message: text }; }
  if (!res.ok) { const e = new Error((data && data.message) || res.statusText); e.status = res.status; e.code = data && data.error; throw e; }
  return data;
}
const soft = (p) => p.then((v) => ({ ok: true, v }), (e) => ({ ok: false, e }));

// --- signatures (Ed25519 compact JWS), after web/mcp-card.html ---------------------------------------------------
const unb64 = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(s.length / 4) * 4, "=")), (c) => c.charCodeAt(0));
async function verifyJws(jws, jwks) {
  if (!jws) return { state: "none" };
  const parts = jws.split(".");
  if (parts.length !== 3) return { state: "bad", why: "not a compact JWS" };
  let header, payload;
  try { header = JSON.parse(new TextDecoder().decode(unb64(parts[0]))); payload = JSON.parse(new TextDecoder().decode(unb64(parts[1]))); }
  catch { return { state: "bad", why: "unreadable" }; }
  const key = (jwks?.keys || []).find((k) => !header.kid || k.kid === header.kid);
  if (!key) return { state: "unchecked", header, payload, why: "no public key with this kid at /.well-known/jwks.json" };
  try {
    const k = await crypto.subtle.importKey("jwk", { kty: key.kty, crv: key.crv, x: key.x }, { name: "Ed25519" }, false, ["verify"]);
    const ok = await crypto.subtle.verify({ name: "Ed25519" }, k, unb64(parts[2]), new TextEncoder().encode(parts[0] + "." + parts[1]));
    return ok ? { state: "ok", header, payload, kid: key.kid } : { state: "bad", header, payload, why: "signature does not match" };
  } catch { return { state: "unchecked", header, payload, why: "this browser can't check Ed25519 signatures" }; }
}
function sigLine(v, what, mismatch) {
  if (!v || v.state === "none") return `<div class="sig">${esc(what)}: no signature recorded.</div>`;
  if (v.state === "ok" && !mismatch) return `<div class="sig ok"><span class="ms">verified</span> ${esc(what)}: signature checked here (Ed25519, key ${esc(v.kid)}).</div>`;
  if (v.state === "ok") return `<div class="sig bad"><span class="ms">error</span> ${esc(what)}: the signature is valid but signs something else (${esc(mismatch)}).</div>`;
  if (v.state === "bad") return `<div class="sig bad"><span class="ms">error</span> ${esc(what)}: ${esc(v.why)}. Don't rely on it.</div>`;
  return `<div class="sig">${esc(what)} is signed (${esc(v.header?.typ || "JWS")}), not checked here: ${esc(v.why)}.</div>`;
}
const sigOk = (v, mismatch) => v && v.state === "ok" && !mismatch;

// --- tiers ----------------------------------------------------------------------------------------------------------
const TIER = { silent: ["tier-silent", "Auto"], notify: ["tier-notify", "Auto + FYI"], ask: ["tier-ask", "Needs approval"], block: ["tier-block", "Refused"] };
const tierBadge = (t) => (TIER[t] ? `<span class="tier ${TIER[t][0]}">${TIER[t][1]}</span>` : `<span class="muted small">${dash(t)}</span>`);
const STATUS = { ordered: "Ordered", pending_approval: "Waiting for the buyer's human", refused: "Refused by the mandate",
  rejected: "Rejected by the buyer's human", expired: "Expired", expired_cutoff: "Expired: nobody approved before the cut-off" };
const PEPPOL = { AP: "AP accepted", AB: "AB received, held for approval", RE: "RE not accepted" };
const peppolCode = (s) => (s === "ordered" ? "AP" : s === "pending_approval" ? "AB" : "RE");
const ruleUrl = (f) => f.rule_url || `${API}/rules/${encodeURIComponent(f.rule)}`;
const finding = (f) => `<div class="finding"><i class="${esc(f.outcome)}" title="${esc(f.outcome)}"></i><div>${esc(f.message)}
  <div class="rule-id"><a href="${esc(ruleUrl(f))}" target="_blank" rel="noopener">${esc(f.rule)}</a> · ${esc((TIER[f.outcome] || [, f.outcome])[1])}</div></div></div>`;
const issueOf = (i) => (typeof i === "string" ? { code: i, message: i, severity: "blocking" } : i || {});
const facts = (rows) => `<dl class="facts">${rows.filter(Boolean).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;

// --- loading one order ----------------------------------------------------------------------------------------------
let CTX = null;

async function load(id) {
  setStatus(`Loading ${id}…`);
  closeDrawer();
  const r = await soft(get("/orders/" + encodeURIComponent(id)));
  if (!r.ok) return showError(id, r.e);
  const o = r.v;
  const [qr, mr, ae, aq, jw, recent] = await Promise.all([
    soft(get("/quotes/" + encodeURIComponent(o.quote_id))),
    soft(get("/mandates/" + encodeURIComponent(o.mandate_id))),
    soft(get(`/audit?subject=${encodeURIComponent(o.id)}&limit=100`)),
    soft(get(`/audit?subject=${encodeURIComponent(o.quote_id)}&limit=100`)),
    soft(get("/.well-known/jwks.json")),
    soft(get("/audit?limit=400")),
  ]);
  const q = qr.ok ? qr.v.quote : null;
  const m = mr.ok ? mr.v.mandate : null;
  const jwks = jw.ok ? jw.v : null;
  const events = [...new Map([...(ae.ok ? ae.v.events : []), ...(aq.ok ? aq.v.events : [])].map((e) => [e.seq, e])).values()].sort((a, b) => a.seq - b.seq);
  // A catalog search belongs to this order only if it was this agent's, before the quote, after its previous quote,
  // within 15 minutes, and returned at least one product the quote compared (the same test as the seller UI).
  const searches = [];
  if (recent.ok && q) {
    const qTime = new Date(q.issued_at);
    const compared = new Set([...(q.lines || []), ...(q.alternatives_considered || [])].map((l) => l.product_id));
    for (const e of recent.v.events) {
      if (e.actor !== o.agent_id || new Date(e.ts) > qTime || e.subject === o.quote_id) continue;
      if (e.event === "quote.issued") break;
      if (e.event === "catalog.search" && qTime - new Date(e.ts) < 15 * 60000 && (e.data?.results || []).some((p) => compared.has(p))) searches.unshift(e);
    }
  }
  const authFails = recent.ok ? recent.v.events.filter((e) => e.event === "agent.auth_failed" && (e.actor === o.agent_id || e.data?.agent_id === o.agent_id)) : [];
  const [vq, vm, vr] = await Promise.all([verifyJws(qr.ok ? qr.v.jws : null, jwks), verifyJws(mr.ok ? mr.v.jws : null, jwks), verifyJws(o.receipt_jws, jwks)]);
  CTX = {
    o, q, m, events, searches, authFails, jwks, chain: null,
    claim: o.agent_claim || (qr.ok ? qr.v.agent_claim : null),
    qjws: qr.ok ? qr.v.jws : null, qErr: qr.ok ? null : qr.e, mErr: mr.ok ? null : mr.e,
    auditErr: ae.ok ? null : ae.e,
    v: { quote: vq, mandate: vm, receipt: vr },
    mis: {
      quote: q && vq.payload && (vq.payload.id !== q.id || vq.payload.total_ex_vat !== q.total_ex_vat) ? `quote ${vq.payload.id}, ${eur(vq.payload.total_ex_vat)}` : "",
      mandate: m && vm.payload && vm.payload.id !== m.id ? `mandate ${vm.payload.id}` : "",
      receipt: vr.payload && (vr.payload.id !== o.id || vr.payload.status !== o.status) ? `order ${vr.payload.id}, ${vr.payload.status}` : "",
    },
  };
  render();
  setStatus(PENDING_NOTE);
  PENDING_NOTE = "";
}

function showError(id, e) {
  CTX = null;
  const why = e.status === 404 ? `No order ${esc(id)} that you may read. Demo orders are public; a sandbox order needs that sandbox's agent or approver token.`
    : e.status === 401 || e.status === 403 ? "This order needs a credential to read."
    : `The gateway answered ${esc(e.status || "nothing")}: ${esc(e.message)}`;
  $("#out").innerHTML = `<div class="msg"><b>Can't show this order</b>${why}
    <details ${e.status === 404 || e.status === 401 || e.status === 403 ? "open" : ""}><summary>Private order? Paste a token</summary>
      <p class="small">A sandbox agent token (vka_…) or approver token (sbxa_…). Kept in this tab only, sent only to this gateway.</p>
      <form class="tokenrow" id="tokform"><input id="tok" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Token">
      <button class="btn" type="submit">Use token</button></form></details></div>`;
  $("#tokform").addEventListener("submit", (ev) => { ev.preventDefault(); store.set(sessionStorage, TOKEN_KEY, $("#tok").value.trim()); load(id); });
  setStatus("");
}

// --- the nine hops --------------------------------------------------------------------------------------------------
// Each: [side, icon, title, who, state(ctx), summary(ctx), detail(ctx)]. state: ok | fyi | ask | block | skip | na.
const HOPS = [
  { side: "Buyer", icon: "smart_toy", title: "Buyer's agent", who: (c) => `${esc(c.o.agent_id)} for ${esc(c.o.buyer)}`,
    state: () => "ok",
    sum: (c) => {
      const items = c.q?.items || [];
      return facts([
        ["mandate", `<span class="mono">${esc(c.o.mandate_id)}</span>`],
        ["asked for", items.length ? items.map((i) => `${num(i.amount)} ${esc(unitName(i.unit))}${i.note ? ` · ${esc(i.note)}` : ""}`).join("<br>") : "–"],
        ["delivery", day(c.q?.requested_delivery_date || c.o.requested_delivery_date)],
      ]) + (c.claim?.text ? `<div class="claim-tag">${c.claim.source === "system" ? "Sous's note on a seeded order" : `${c.claim.suspicious ? "Flagged · " : ""}Unverified claim`}</div><p class="claim">“${esc(c.claim.text)}”</p>` : "");
    },
    det: (c) => {
      const items = c.q?.items || [];
      return facts([["agent", `<span class="mono">${esc(c.o.agent_id)}</span>`], ["buyer", dash(c.o.buyer)],
        ["mandate", `<span class="mono">${esc(c.o.mandate_id)}</span>`], ["requested delivery", day(c.q?.requested_delivery_date || c.o.requested_delivery_date)],
        ["idempotency key", `<span class="mono">${dash(c.o.idempotency_key)}</span>`]])
        + `<h4>What it asked for (quote request)</h4>`
        + (items.length ? `<table><thead><tr><th>#</th><th class="n">Amount</th><th>Candidates</th><th>Note</th></tr></thead><tbody>${items.map((i, n) => `<tr><td>${n + 1}</td>
            <td class="n">${num(i.amount)} ${esc(unitName(i.unit))}</td><td>${plural((i.product_ids || []).length, "product")}${i.substitute_for ? ` · replaces <span class="mono">${esc(i.substitute_for)}</span>` : ""}${i.product_type ? ` · ${esc(i.product_type)}` : ""}</td><td>${dash(i.note || i.use || i.purpose)}</td></tr>`).join("")}</tbody></table>`
          : `<p class="note">${c.qErr ? `Quote not readable (${esc(c.qErr.status)}).` : "Not recorded."}</p>`)
        + `<h4>In its own words</h4>`
        + (c.claim?.text ? `<p class="claim">“${esc(c.claim.text)}”</p><p class="note">${esc(c.claim.note || "The agent's own words; not used by any rule.")}${c.claim.suspicious ? " Flagged as suspicious (it reads like an instruction to the gateway)." : ""}</p>` : `<p class="note">The agent gave no rationale.</p>`)
        + (c.o.declared_risks?.length ? `<h4>Doubts the agent declared</h4><p>${c.o.declared_risks.map(esc).join(", ")}</p>` : "");
    } },

  { side: "Sous", icon: "shield_lock", title: "Gateway", who: () => "Sous, between the agent and Valio Aimo",
    state: () => "ok",  // failed auth attempts by the agent are listed in the details, not tied to this order
    sum: (c) => {
      const kinds = [...new Set(c.events.map((e) => e.event))];
      return facts([
        ["channel", `<span class="muted">not recorded</span> <span class="small muted">(MCP and REST run the same code)</span>`],
        ["agent auth", c.m ? (c.m.require_agent_auth ? "required: bearer credential checked" : "not required by this mandate") : "–"],
        ["calls", c.events.length ? `${plural(c.events.length, "audit event")}: ${kinds.slice(0, 3).map(esc).join(", ")}${kinds.length > 3 ? "…" : ""}` : "–"],
      ]);
    },
    det: (c) => facts([
        ["channel", "Not recorded per order. MCP tools and REST endpoints call the same gateway functions."],
        ["agent auth", c.m ? (c.m.require_agent_auth ? "Required by the mandate: the quote and the order were accepted only with the agent's bearer credential." : "Not required by this mandate (a public demo mandate). Mandates can require it (require_agent_auth).") : "Mandate not readable."],
        ["failed auth", c.authFails.length ? `${plural(c.authFails.length, "failed attempt")} by this agent on record` : "none on record"],
        ["quote → order", c.q ? `${Math.max(0, Math.round((new Date(c.o.created_at) - new Date(c.q.issued_at)) / 1000))} s` : "–"],
      ])
      + `<h4>Calls on record for this quote and order</h4>`
      + (c.events.length ? c.events.map((e) => `<div class="ev"><span class="seq">#${esc(e.seq)}</span><div><b>${esc(e.event)}</b> · ${esc(e.actor)} <span class="muted">(${esc(e.actor_type)})</span><div class="muted">${when(e.ts)}</div></div></div>`).join("")
        : `<p class="note">${c.auditErr ? `Audit not readable (${esc(c.auditErr.status)}).` : "No events."}</p>`)
      + (c.authFails.length ? `<h4>Failed authentication</h4>${c.authFails.map((e) => `<div class="ev"><span class="seq">#${esc(e.seq)}</span><div>${esc(e.data?.reason || e.data?.error || e.event)}<div class="muted">${when(e.ts)}</div></div></div>`).join("")}` : "") },

  { side: "Sous", icon: "manage_search", title: "Catalogue", who: () => "Sous's local catalogue index: 0 Valio calls",
    state: (c) => (c.q ? "ok" : "na"),
    sum: (c) => {
      if (!c.q) return `<p class="note">Quote not readable.</p>`;
      const cands = new Set((c.q.items || []).flatMap((i) => i.product_ids || []));
      const alts = c.q.alternatives_considered || [];
      const excluded = alts.filter((a) => a.orderable === false).length;
      const top = alts.filter((a) => a.orderable !== false).slice(0, 2);
      return facts([
        ["searched", c.searches.length ? c.searches.map((s) => `“${esc(s.data.query)}”`).join(", ") : `<span class="muted">no search linked</span>`],
        ["candidates", `${cands.size} put forward by the agent`],
        ["chosen", (c.q.lines || []).map((l) => esc(l.name || l.product_id)).join(", ") || "–"],
        ["runners-up", top.length ? top.map((a) => `${esc(a.name)} <span class="muted">${a.comparison_price ? `${eur(a.comparison_price.ex_vat)}/${esc(a.comparison_price.per)}` : ""}</span>`).join("<br>") : "–"],
        ["excluded", `${excluded} not orderable`],
      ]);
    },
    det: (c) => {
      if (!c.q) return `<p class="note">Quote not readable${c.qErr ? ` (${esc(c.qErr.status)})` : ""}.</p>`;
      const alts = c.q.alternatives_considered || [];
      return `<h4>Catalogue searches by this agent before the quote</h4>`
        + (c.searches.length ? c.searches.map((s) => `<div class="ev"><span class="seq">#${esc(s.seq)}</span><div>“${esc(s.data.query)}”${s.data.claims?.length ? ` with ${esc(s.data.claims.join(", "))}` : ""}${s.data.avoid_allergens?.length ? `, avoiding ${esc(s.data.avoid_allergens.join(", "))}` : ""} → ${plural((s.data.results || []).length, "result")}, ${esc(s.data.valio_api_calls ?? 0)} Valio calls<div class="muted">${when(s.ts)}</div></div></div>`).join("")
          : `<p class="note">No catalogue search on record that leads to this quote (the agent may have known the products, or the search is not readable).</p>`)
        + `<h4>Candidates the agent quoted</h4>`
        + (c.q.items || []).map((i, n) => `<p class="small">Item ${n + 1}: ${(i.product_ids || []).map((p) => `<span class="mono">${esc(p)}</span>`).join(", ") || "–"}</p>`).join("")
        + `<h4>Compared and passed over (${alts.length})</h4>`
        + (alts.length ? `<table><thead><tr><th>Product</th><th class="n">Per unit</th><th>Why not</th></tr></thead><tbody>${alts.map((a) => `<tr><td>${esc(a.name)} <div class="mono muted">${esc(a.product_id)}</div></td>
            <td class="n">${a.comparison_price ? `${eur(a.comparison_price.ex_vat)}/${esc(a.comparison_price.per)}` : "–"}</td><td class="${a.orderable === false ? "bad-ink" : ""}">${(a.why_not || []).map(esc).join(" ") || "–"}</td></tr>`).join("")}</tbody></table>`
          : `<p class="note">None recorded.</p>`);
    } },

  { side: "Valio Aimo", icon: "inventory_2", title: "Price and stock", who: (c) => (c.q?.seeded ? "Reference prices stored in Sous (sandbox example)" : "Valio Aimo pricing and availability API"),
    state: (c) => {
      if (!c.q) return "na";
      const iss = (c.q.lines || []).flatMap((l) => (l.issues || []).map(issueOf));
      return iss.some((i) => i.severity === "blocking") ? "block" : iss.length || (c.q.uncovered || []).some((g) => g.uncovered > 0) ? "ask" : "ok";
    },
    sum: (c) => {
      if (!c.q) return `<p class="note">Quote not readable.</p>`;
      const k = c.q.valio_api_calls;
      const l = (c.q.lines || [])[0];
      const iss = [...new Map((c.q.lines || []).flatMap((x) => (x.issues || []).map(issueOf)).map((i) => [i.message || i.code, i])).values()];
      return facts([
        ["API calls", c.q.seeded ? "0 (reference prices)" : k ? `${esc(k.price_calls ?? 0)} price · ${esc(k.availability_calls ?? 0)} availability · ${esc(k.cache_hits ?? 0)} cached` : "–"],
        l && ["unit price", `${eur(l.unit_price_ex_vat)} ex VAT${l.vat_rate != null ? ` · VAT ${num(l.vat_rate)} %` : ""}`],
        l && ["compare", l.comparison_price ? `${eur(l.comparison_price.ex_vat)} per ${esc(l.comparison_price.per)}` : "–"],
        l && ["logistics", `${dash(l.logistic_model)}${l.stock ? ` · stock ${esc(l.stock)}` : ""}`],
        l && ["cut-off", when(l.order_cutoff)],
        l && ["delivery", day(l.delivery_date)],
        (c.q.lines || []).length > 1 && ["lines", `${c.q.lines.length} lines, see details`],
        ["issues", iss.length ? iss.map((i) => `<span class="${i.severity === "blocking" ? "bad-ink" : "warn-ink"}">${esc(i.message || i.code)}</span>`).join("<br>") : "none"],
      ]);
    },
    det: (c) => {
      if (!c.q) return `<p class="note">Quote not readable.</p>`;
      const k = c.q.valio_api_calls || {};
      return (c.q.seeded ? `<p class="note">${esc(c.q.seeded.note || "Sandbox example: priced from reference prices stored in the gateway, not a live Valio Aimo quote.")}</p>` : "")
        + facts([["price calls", dash(k.price_calls)], ["availability calls", dash(k.availability_calls)], ["cache hits", dash(k.cache_hits)]])
        + (c.q.lines || []).map((l) => `<h4>${esc(l.name || l.product_id)}</h4>` + facts([
          ["product", `<span class="mono">${esc(l.product_id)}</span>${l.brand ? ` · ${esc(l.brand)}` : ""}${l.substitute_for ? ` · replaces <span class="mono">${esc(l.substitute_for)}</span>` : ""}`],
          ["quantity", `${num(l.quantity)} ${esc(l.sales_unit || "")}${l.covers ? ` = ${num(l.covers.amount)} ${esc(unitName(l.covers.unit))}` : ""}`],
          ["unit price", `${eur(l.unit_price_ex_vat)} ex VAT · ${eur(l.unit_price_inc_vat)} incl.${l.list_price_ex_vat != null ? ` · list ${eur(l.list_price_ex_vat)}` : ""}`],
          ["VAT", l.vat_rate != null ? `${num(l.vat_rate)} %` : "–"],
          ["comparison", l.comparison_price ? `${eur(l.comparison_price.ex_vat)} per ${esc(l.comparison_price.per)}` : "–"],
          ["line total", `${eur(l.line_total_ex_vat)} ex VAT · ${eur(l.line_total_inc_vat)} incl.`],
          ["logistic model", `${dash(l.logistic_model)}${l.logistic_model === "Terminal" ? " (ordered from the supplier)" : l.logistic_model === "Stock" ? " (from Valio Aimo's stock)" : ""}`],
          ["stock", l.stock ? esc(l.stock) : l.available_amount != null ? num(l.available_amount) : "–"],
          ["cut-off", when(l.order_cutoff)], ["available", day(l.available_date)], ["delivery", day(l.delivery_date)],
          ["reliability", l.delivery_reliability != null ? `${Math.round(l.delivery_reliability * 100)} %` : "–"],
          ["issues", (l.issues || []).length ? l.issues.map(issueOf).map((i) => `<span class="${i.severity === "blocking" ? "bad-ink" : "warn-ink"}">${esc(i.message || i.code)}</span>`).join("<br>") : "none"],
        ])).join("")
        + ((c.q.uncovered || []).filter((g) => g.uncovered > 0).length ? `<h4>Not covered</h4>${c.q.uncovered.filter((g) => g.uncovered > 0).map((g) => `<p class="warn-ink small">${num(g.uncovered)} ${esc(unitName(g.unit))}${g.reason ? `: ${esc(g.reason)}` : ""}</p>`).join("")}` : "");
    } },

  { side: "Sous", icon: "verified", title: "Signed quote", who: () => "Sous signs the price it got from Valio Aimo",
    state: (c) => (!c.q ? "na" : sigOk(c.v.quote, c.mis.quote) ? "ok" : c.v.quote.state === "bad" || c.mis.quote ? "block" : "na"),
    sum: (c) => c.q ? facts([
        ["quote", `<span class="mono">${esc(c.q.id)}</span>`],
        ["total", `${eur(c.q.total_ex_vat)} ex VAT · ${eur(c.q.total_inc_vat)} incl.`],
        ["expires", when(c.q.expires_at)],
        ["header", c.v.quote.header ? `<span class="mono">${esc(c.v.quote.header.typ)} · ${esc(c.v.quote.header.kid)}</span>` : "–"],
        ["signature", sigOk(c.v.quote, c.mis.quote) ? `<span class="ok-ink">✓ checked here</span>` : `<span class="bad-ink">${esc(c.mis.quote || c.v.quote.why || "not checked")}</span>`],
      ]) : `<p class="note">Quote ${esc(c.o.quote_id)} not readable${c.qErr ? ` (${esc(c.qErr.status)})` : ""}.</p>`,
    det: (c) => c.q ? facts([
        ["quote", `<span class="mono">${esc(c.q.id)}</span>`], ["issued", when(c.q.issued_at)], ["expires", when(c.q.expires_at)],
        ["total", `${eur(c.q.total_ex_vat)} ex VAT · ${eur(c.q.total_inc_vat)} incl. VAT`], ["currency", dash(c.q.currency)],
        ["alg / typ / kid", c.v.quote.header ? `<span class="mono">${esc(c.v.quote.header.alg)} · ${esc(c.v.quote.header.typ)} · ${esc(c.v.quote.header.kid)}</span>` : "–"],
      ])
      + sigLine(c.v.quote, "Quote", c.mis.quote)
      + `<p class="note">The agent can only order this exact signed payload, once, before it expires. The order step re-checks the signature on the gateway too.</p>`
      + (c.q.mandate_preview ? `<h4>Preview the agent saw: ${tierBadge(c.q.mandate_preview.outcome)}</h4>${(c.q.mandate_preview.findings || []).map(finding).join("")}` : "")
      + `<details><summary class="small">Raw JWS</summary><pre>${esc(c.qjws)}</pre></details>`
      + `<div class="links"><a class="btn" href="${API}/.well-known/jwks.json" target="_blank" rel="noopener">Public key</a></div>`
      : `<p class="note">Quote not readable.</p>` },

  { side: "Buyer", icon: "gavel", title: "Mandate policy", who: (c) => `Rules the buyer signed${c.m?.issued_by ? `: ${esc(c.m.issued_by)}` : ""}`,
    state: (c) => ({ silent: "ok", notify: "fyi", ask: "ask", block: "block" }[c.o.decision?.outcome] || "na"),
    decisive: true,
    sum: (c) => {
      const d = c.o.decision || {};
      const shown = (d.findings || []).filter((f) => f.outcome !== "silent" || f.rule === "mandate.ok").slice(0, 3);
      const ai = (d.findings || []).some((f) => f.rule === "allergen.likely");
      const how = ai ? "plain code; one input from the Gemini Decisions allergen check, which can only add a refusal"
                     : "plain code, no AI in this step";
      return `<div style="margin-bottom:6px">${tierBadge(d.outcome)} <span class="small muted">by ${dash(d.by)}, ${how}</span></div>`
        + shown.map(finding).join("") + ((d.findings || []).length > shown.length ? `<div class="small muted">+ ${(d.findings || []).length - shown.length} more</div>` : "");
    },
    det: (c) => {
      const d = c.o.decision || {}, m = c.m;
      return `<div>${tierBadge(d.outcome)} <span class="small muted">decided by ${dash(d.by)} at ${when(d.at)}: deterministic rules, no language model</span></div>`
        + `<h4>Every finding (${(d.findings || []).length})</h4>${(d.findings || []).map(finding).join("") || `<p class="note">None recorded.</p>`}`
        + `<h4>The mandate's key limits</h4>`
        + (m ? facts([
            ["mandate", `<span class="mono">${esc(m.id)}</span>${m.revoked ? ` <span class="bad-ink">revoked</span>` : ""}`],
            ["signed by", dash(m.issued_by)], ["valid until", m.valid_until ? when(m.valid_until) : "no end date"],
            ["FYI above", eur(m.notify_above_eur)], ["auto up to", eur(m.auto_approve_limit_eur)], ["refused above", eur(m.hard_limit_eur)],
            ["per day / week", `${eur(m.daily_limit_eur)} / ${eur(m.weekly_limit_eur)}`],
            ["avoid allergens", (m.avoid_allergens || []).length ? m.avoid_allergens.map(esc).join(", ") : "none"],
            ["substitutes", dash(m.substitutions)], ["first-time products", dash(m.first_time_products)],
            ["approval method", dash(m.approval_method)], ["agent auth", m.require_agent_auth ? "required" : "not required"],
          ]) + sigLine(c.v.mandate, "Mandate", c.mis.mandate)
          : `<p class="note">Mandate not readable${c.mErr ? ` (${esc(c.mErr.status)})` : ""}.</p>`)
        + `<div class="links"><a class="btn" href="${API}/rules" target="_blank" rel="noopener">All rules</a></div>`;
    } },

  { side: "Valio Aimo", icon: "local_shipping", title: "Order", who: () => "Valio Aimo's answer, as Peppol BIS",
    state: (c) => ({ ordered: c.o.decision?.outcome === "notify" ? "fyi" : "ok", pending_approval: "ask" }[c.o.status] || "block"),
    sum: (c) => {
      const code = peppolCode(c.o.status);
      return facts([
        ["order", `<span class="mono">${esc(c.o.id)}</span>`],
        ["status", esc(STATUS[c.o.status] || c.o.status)],
        ["Peppol", `<span class="mono">${esc(code)}</span> ${esc(PEPPOL[code].slice(3))}`],
        ["total", `${eur(c.o.total_ex_vat)} ex VAT`],
        ["receipt", c.o.receipt_jws ? (sigOk(c.v.receipt, c.mis.receipt) ? `<span class="ok-ink">✓ checked here</span>` : `<span class="bad-ink">${esc(c.mis.receipt || c.v.receipt.why || "not checked")}</span>`) : "–"],
        ["to ERP", c.o.erp ? (c.o.erp.simulated ? "simulated hand-off" : esc(c.o.erp.status)) : "not recorded"],
      ]);
    },
    det: (c) => {
      const o = c.o, code = peppolCode(o.status), id = encodeURIComponent(o.id);
      return facts([
          ["order", `<span class="mono">${esc(o.id)}</span>`], ["status", esc(STATUS[o.status] || o.status)],
          ["Peppol response", `<span class="mono">${esc(code)}</span> · ${esc(PEPPOL[code])}`],
          ["total", `${eur(o.total_ex_vat)} ex VAT · ${eur(o.total_inc_vat)} incl.`], ["delivery", day(o.delivery_date)],
          ["placed", when(o.created_at)], ["updated", when(o.updated_at)],
        ])
        + `<h4>Lines</h4><table><thead><tr><th>Product</th><th class="n">Qty</th><th class="n">Line</th></tr></thead><tbody>${(o.lines || []).map((l) => `<tr><td>${esc(l.name || l.product_id)}<div class="mono muted">${esc(l.product_id)}</div></td><td class="n">${num(l.quantity)} ${esc(l.sales_unit || "")}</td><td class="n">${eur(l.line_total_ex_vat)}</td></tr>`).join("") || `<tr><td colspan="3">–</td></tr>`}</tbody></table>`
        + `<h4>Signed receipt</h4>` + sigLine(c.v.receipt, "Receipt", c.mis.receipt)
        + (o.receipt_jws ? `<details><summary class="small">Raw JWS</summary><pre>${esc(o.receipt_jws)}</pre></details>` : "")
        + `<h4>Hand-off to Valio Aimo's ERP</h4>`
        + (o.erp ? facts([["adapter", dash(o.erp.adapter)], ["status", dash(o.erp.status)], ["confirmation", `<span class="mono">${dash(o.erp.confirmation_id)}</span>`]])
            + (o.erp.simulated ? `<p class="note">Simulated: Valio Aimo has no order API in this challenge, so nothing reached a real Valio system (SIM- id).</p>` : "")
          : `<p class="note">Not recorded for this order.</p>`)
        + `<div class="links"><a class="btn" href="${API}/orders/${id}/peppol/order.xml" target="_blank" rel="noopener">Peppol Order XML</a>
           <a class="btn" href="${API}/orders/${id}/peppol/order-response.xml" target="_blank" rel="noopener">Order Response XML</a></div>`;
    } },

  { side: "Buyer", icon: "person_check", title: "Human approver", who: (c) => (c.o.approval || c.o.approval_changes ? `${esc((c.o.approval || c.o.approval_changes).by)}, the buyer's person` : "The buyer's person, only if the mandate asks"),
    state: (c) => {
      const a = c.o.approval;
      if (a) return a.approved && c.o.status === "ordered" ? "ok" : "block";
      if (c.o.approval_changes) return "ok";
      if (c.o.status === "pending_approval") return "ask";
      if (c.o.status === "expired_cutoff" || c.o.status === "expired") return "block";
      return "skip";
    },
    sum: (c) => {
      const a = c.o.approval, o = c.o;
      if (a) return facts([["decision", a.approved ? `<span class="ok-ink">Approved</span>` : `<span class="bad-ink">Rejected</span>`],
        ["by", dash(a.by)], ["at", when(a.at)], ["credential", a.passkey ? "passkey" : dash(a.authority)],
        a.note && ["note", `“${esc(a.note)}”`]]);
      if (o.approval_changes) return changesFacts(o.approval_changes);
      if (o.status === "pending_approval") return facts([["status", `<span class="warn-ink">waiting</span>`], ["decide by", when(o.approve_by)],
        ["method", c.m ? esc(c.m.approval_method) : "–"]]) + `<p class="note">The agent has no approve tool. Only the buyer's person can decide.</p>`;
      if (o.status === "expired_cutoff" || o.status === "expired") return `<p class="note">Nobody approved by ${when(o.approve_by || o.expiry?.approve_by)}; nothing was ordered.</p>`;
      return `<p class="note">${o.decision?.outcome === "block" ? "Not needed: the rules refused it, and a human can't override a refusal here." : "Not needed: the mandate lets the agent order this alone."}</p>`;
    },
    det: (c) => {
      const a = c.o.approval, o = c.o, id = encodeURIComponent(o.id), sbx = SANDBOX ? `?sandbox=${encodeURIComponent(SANDBOX)}` : "";
      return (a ? facts([
            ["decision", a.approved ? "Approved" : "Rejected"], ["by", dash(a.by)], ["at", when(a.at)], ["credential", dash(a.authority)],
            ["note", a.note ? `“${esc(a.note)}”` : "–"], ["result", dash(a.result_reason)],
            a.passkey && ["passkey", `credential <span class="mono">${short(a.passkey.credential_id, 12)}</span> · rp ${dash(a.passkey.rp_id)} · user verified: ${a.passkey.user_verified ? "yes" : "no"}`],
            a.replaced_by && ["replaced by", `<a class="mono" href="?order=${encodeURIComponent(a.replaced_by)}">${esc(a.replaced_by)}</a>`],
          ])
          : o.approval_changes ? changesFacts(o.approval_changes, true)
          : o.status === "pending_approval" ? facts([["status", "Held for a human decision"], ["decide by", when(o.approve_by)],
              ["deadline basis", dash(o.approve_by_detail?.basis)], ["method", c.m ? dash(c.m.approval_method) : "–"]])
          : `<p class="note">No human step for this order.</p>`)
        + `<p class="note">Agents can never approve: there is no approval tool over MCP, and the approve endpoints need the buyer's approver code or passkey (four-eyes can also stop the requester approving).</p>`
        + `<div class="links"><a class="btn" href="/ui/${sbx}#/orders/${id}">Seller view</a>`
        + (o.status === "pending_approval" ? `<a class="btn solid" href="/ui/buyer.html${sbx}#/orders/${id}">Decide on the buyer side</a><a class="btn" href="/ui/approve.html?order=${id}${SANDBOX ? `&sandbox=${encodeURIComponent(SANDBOX)}` : ""}">Approve with a passkey</a>` : "")
        + `</div>`;
    } },

  { side: "Shared", icon: "link", title: "Audit ledger", who: () => "Hash-chained log that buyer and Valio Aimo both read",
    state: (c) => (c.chain ? (c.chain.ok ? "ok" : "block") : c.events.length ? (linkCheck(c.events).broken ? "block" : "ok") : "na"),
    sum: (c) => {
      const lc = linkCheck(c.events);
      return c.events.slice(-4).map((e) => `<div class="ev"><span class="seq">#${esc(e.seq)}</span><div>${esc(e.event)} <span class="muted">· ${esc(e.actor)}</span><div class="mono muted">${short(e.hash, 12)}</div></div></div>`).join("")
        + (c.events.length > 4 ? `<div class="small muted">+ ${c.events.length - 4} earlier</div>` : "")
        + `<div class="small" style="margin-top:6px">${lc.pairs ? (lc.broken ? `<span class="bad-ink">${lc.broken} link(s) don't match</span>` : `<span class="ok-ink">✓ ${plural(lc.pairs, "adjacent link")} match</span>`) : ""}
          ${c.chain ? (c.chain.ok ? ` · <span class="ok-ink">✓ whole chain verified (${esc(c.chain.events)} events)</span>` : ` · <span class="bad-ink">chain check failed</span>`) : ""}</div>`;
    },
    det: (c) => `<p class="note">Each event stores the hash of the one before it, so editing or deleting any event breaks every later hash. GET /audit/verify recomputes the whole chain on the gateway.</p>
      <div class="links"><button class="btn solid" type="button" data-verify>Verify chain</button></div><div id="chain-out">${chainHtml(c.chain)}</div>
      <h4>Events for this order and its quote (${c.events.length})</h4>`
      + (c.events.length ? `<table><thead><tr><th>Seq</th><th>Event</th><th>Hash / prev</th></tr></thead><tbody>${c.events.map((e) => `<tr><td class="mono">${esc(e.seq)}</td>
          <td><b>${esc(e.event)}</b><div class="small">${esc(e.actor)} <span class="muted">(${esc(e.actor_type)})</span></div><div class="small muted">${when(e.ts)}</div></td>
          <td class="mono small">${short(e.hash, 12)}<br><span class="muted">${short(e.prev_hash, 12)}</span></td></tr>`).join("")}</tbody></table>`
        : `<p class="note">${c.auditErr ? `Audit not readable (${esc(c.auditErr.status)}).` : "No events."}</p>`)
      + `<div class="links"><a class="btn" href="/ui/${SANDBOX ? `?sandbox=${encodeURIComponent(SANDBOX)}` : ""}#/audit">Full audit log</a></div>` },
];

// An order a human approved with changes: it replaces the held one, re-priced live (approval_changes on the order).
const changesFacts = (ch, full) => facts([
  ["decision", `<span class="ok-ink">Approved with changes</span>`], ["by", dash(ch.by)], ["credential", dash(ch.authority)],
  ["replaces", `<a class="mono" href="?order=${encodeURIComponent(ch.from_order)}${SANDBOX ? `&sandbox=${encodeURIComponent(SANDBOX)}` : ""}">${esc(ch.from_order)}</a>`],
  ["changed", (ch.changes || []).map((x) => `${esc(x.name || x.product_id)} ${num(x.from)} → ${num(x.to)} ${esc(x.unit || "")}`).join("<br>") || "–"],
  full && ch.note && ["note", `“${esc(ch.note)}”`],
]) + (full ? `<p class="note">The held order was replaced by this one: same products, the human's quantities, priced live again before it was ordered.</p>` : "");

// Adjacent events (seq n, n+1) must chain: the later one's prev_hash is the earlier one's hash.
function linkCheck(evs) {
  let pairs = 0, broken = 0;
  for (let i = 1; i < evs.length; i++) if (evs[i].seq === evs[i - 1].seq + 1) { pairs++; if (evs[i].prev_hash !== evs[i - 1].hash) broken++; }
  return { pairs, broken };
}
const chainHtml = (ch) => !ch ? "" : ch.error ? `<div class="sig bad">Couldn't verify: ${esc(ch.error)}</div>`
  : `<div class="sig ${ch.ok ? "ok" : "bad"}">${ch.ok ? "✓ Chain intact" : "✗ Chain broken"}: ${esc(ch.events)} events, head <span class="mono">${short(ch.head, 16)}</span>${ch.backend ? ` · ${esc(ch.backend)}` : ""}</div>`;

async function verifyChain() {
  if (!CTX) return;
  const btns = document.querySelectorAll("[data-verify]");
  btns.forEach((b) => (b.disabled = true));
  try { CTX.chain = await get("/audit/verify"); } catch (e) { CTX.chain = { ok: false, error: e.message }; }
  btns.forEach((b) => (b.disabled = false));
  const out = $("#chain-out");
  if (out) out.innerHTML = chainHtml(CTX.chain);
  refreshHop(9);
  renderProofs();
}

// --- rendering ------------------------------------------------------------------------------------------------------
const STATE_ICON = { ok: "check", fyi: "info", ask: "pause", block: "close", skip: "remove", na: "help" };
const STATE_LABEL = { ok: "fine", fyi: "fine, with an FYI", ask: "needs a human", block: "stopped", skip: "not needed", na: "not recorded" };
const stopHop = (c) => (c.o.status === "pending_approval" ? 8 : 9);

function hopHtml(h, n, c) {
  const st = h.state(c);
  return `<article class="hop st-${st}${h.decisive ? " decisive" : ""}" data-hop="${n}" style="grid-area:h${n}" tabindex="0" role="button"
      aria-label="Step ${n}: ${esc(h.title)}, ${esc(STATE_LABEL[st])}. Open details">
    <div class="hop-top"><span class="hop-n">${String(n).padStart(2, "0")}</span><span class="side ${h.side === "Valio Aimo" ? "valio" : ""}">${esc(h.side)}</span>
      <span class="state" title="${esc(STATE_LABEL[st])}"><span class="ms">${STATE_ICON[st]}</span></span></div>
    <h3><span class="ms" aria-hidden="true">${h.icon}</span>${esc(h.title)}</h3>
    <div class="who">${h.who(c)}</div>
    ${h.sum(c)}
    <div class="more">Details <span class="ms" aria-hidden="true">arrow_forward</span></div>
  </article>`;
}

function render() {
  const c = CTX, o = c.o;
  const sbx = SANDBOX ? `?sandbox=${encodeURIComponent(SANDBOX)}` : "";
  $("#out").innerHTML = `
    <section class="strip" aria-label="Order">
      <div><h2><span class="mono" style="font-size:.8em">${esc(o.id)}</span> ${tierBadge(o.decision?.outcome)}</h2>
        <div class="meta">${esc(o.buyer)} · ${esc(STATUS[o.status] || o.status)} · ${eur(o.total_ex_vat)} ex VAT · placed ${when(o.created_at)}</div></div>
      <div class="acts"><button class="btn solid" type="button" id="replay"><span class="ms">replay</span>Replay</button>
        <button class="btn" type="button" data-verify><span class="ms">link</span>Verify chain</button>
        <a class="btn" href="/ui/${sbx}#/orders/${encodeURIComponent(o.id)}">Open order</a></div>
    </section>
    <ul class="proofs" id="proofs" aria-label="What was checked here"></ul>
    <div class="board" id="board"><svg class="wires" id="wires" aria-hidden="true"></svg>${HOPS.map((h, i) => hopHtml(h, i + 1, c)).join("")}</div>`;
  renderProofs();
  $("#replay").addEventListener("click", replay);
  wireBoard();
  light(9, stopHop(c));
}

function renderProofs() {
  const c = CTX;
  if (!c || !$("#proofs")) return;
  const item = (ok, label) => `<li class="${ok === true ? "ok" : ok === false ? "bad" : ""}"><span class="ms">${ok === true ? "check_circle" : ok === false ? "error" : "radio_button_unchecked"}</span>${label}</li>`;
  const sig = (v, mis) => (!v || v.state === "none" ? null : v.state === "ok" && !mis ? true : v.state === "bad" || mis ? false : null);
  $("#proofs").innerHTML = [
    c.q ? item(sig(c.v.quote, c.mis.quote), "Quote signature") : item(null, "Quote not readable"),
    c.m ? item(sig(c.v.mandate, c.mis.mandate), "Mandate signature") : item(null, "Mandate not readable"),
    item(sig(c.v.receipt, c.mis.receipt), o_receiptLabel(c.o)),
    item(c.chain ? !!c.chain.ok : null, c.chain ? (c.chain.ok ? `Audit chain (${c.chain.events} events)` : "Audit chain") : "Audit chain: press Verify"),
    item(null, !c.claim?.text ? "No agent rationale" : c.claim.source === "system" ? "Seeded order: the text is Sous's, not an agent's" : "Agent's rationale: unverified, used by no rule"),
  ].join("");
}
const o_receiptLabel = (o) => (o.receipt_jws ? "Order receipt signature" : "No receipt (not final yet)");

function refreshHop(n) {
  const el = document.querySelector(`.hop[data-hop="${n}"]`);
  if (!el || !CTX) return;
  const keep = ["dim", "current", "sel"].filter((k) => el.classList.contains(k));
  el.outerHTML = hopHtml(HOPS[n - 1], n, CTX);
  const fresh = document.querySelector(`.hop[data-hop="${n}"]`);
  keep.forEach((k) => fresh.classList.add(k));
  wireCard(fresh);
}

// --- wires between the cards ---------------------------------------------------------------------------------------
let litUpTo = 9, current = 9;
function drawWires() {
  const board = $("#board"), svg = $("#wires");
  if (!board || !svg) return;
  const b = board.getBoundingClientRect();
  const r = (n) => { const x = document.querySelector(`.hop[data-hop="${n}"]`).getBoundingClientRect(); return { l: x.left - b.left, t: x.top - b.top, r: x.right - b.left, btm: x.bottom - b.top, cx: (x.left + x.right) / 2 - b.left, cy: (x.top + x.bottom) / 2 - b.top }; };
  svg.setAttribute("viewBox", `0 0 ${b.width} ${b.height}`);
  let out = "";
  for (let n = 1; n < 9; n++) {
    const a = r(n), z = r(n + 1);
    let d, head;
    if (Math.abs(a.t - z.t) < 8) { // same row: side to side, at the cards' upper third so it doesn't cross text much
      const y = Math.min(a.t, z.t) + 34;
      const [x1, x2] = z.l > a.r ? [a.r + 4, z.l - 6] : [a.l - 4, z.r + 6];
      d = `M${x1},${y} L${x2},${y}`;
      const s = x2 > x1 ? 1 : -1;
      head = `M${x2},${y} l${-7 * s},-4 l0,8 z`;
    } else { // stacked: bottom to top
      const x = a.cx, y1 = a.btm + 4, y2 = z.t - 6;
      d = `M${x},${y1} L${z.cx},${y2}`;
      head = `M${z.cx},${y2} l-4,-7 l8,0 z`;
    }
    const cls = n + 1 > litUpTo ? "" : n + 1 === current ? "lit flow" : "lit";
    out += `<path class="wire ${cls}" d="${d}"/><path class="wire-head ${cls}" d="${head}"/>`;
  }
  svg.innerHTML = out;
}

function light(upTo, cur) {
  litUpTo = upTo; current = cur;
  document.querySelectorAll(".hop").forEach((el) => {
    const n = +el.dataset.hop;
    el.classList.toggle("dim", n > upTo);
    el.classList.toggle("current", n === cur);
  });
  drawWires();
}

let replayTimer = null;
function replay() {
  clearTimeout(replayTimer);
  const narrow = matchMedia("(max-width: 900px)").matches;
  const stop = stopHop(CTX);
  let n = 1;
  const step = () => {
    light(n, n);
    if (narrow) document.querySelector(`.hop[data-hop="${n}"]`)?.scrollIntoView({ block: "center", behavior: REDUCED.matches ? "auto" : "smooth" });
    setStatus(`Step ${n} of 9: ${HOPS[n - 1].title}`);
    if (n < 9) { n++; replayTimer = setTimeout(step, REDUCED.matches ? 900 : 750); }
    else replayTimer = setTimeout(() => { light(9, stop); setStatus(""); }, 900);
  };
  step();
}

function wireCard(el) {
  el.addEventListener("click", () => openDrawer(+el.dataset.hop));
  el.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openDrawer(+el.dataset.hop); } });
}
let ro = null;
function wireBoard() {
  document.querySelectorAll(".hop").forEach(wireCard);
  document.querySelectorAll("[data-verify]").forEach((b) => b.addEventListener("click", verifyChain));
  ro?.disconnect();
  ro = new ResizeObserver(() => drawWires());
  ro.observe($("#board"));
}

// --- drawer ---------------------------------------------------------------------------------------------------------
let lastFocus = null;
function openDrawer(n) {
  if (!CTX) return;
  const h = HOPS[n - 1];
  lastFocus = document.activeElement;
  document.querySelectorAll(".hop").forEach((el) => el.classList.toggle("sel", +el.dataset.hop === n));
  const st = h.state(CTX);
  $("#drawer-body").innerHTML = `<div class="side ${h.side === "Valio Aimo" ? "valio" : ""}">Step ${n} · ${esc(h.side)} · ${esc(STATE_LABEL[st])}</div>
    <h2>${esc(h.title)}</h2><div class="muted small">${h.who(CTX)}</div>${h.det(CTX)}`;
  $("#drawer-body").querySelectorAll("[data-verify]").forEach((b) => b.addEventListener("click", verifyChain));
  document.body.classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#drawer").scrollTop = 0;
  $("#drawer").focus();
}
function closeDrawer() {
  if (!document.body.classList.contains("open")) return;
  document.body.classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  document.querySelectorAll(".hop.sel").forEach((el) => el.classList.remove("sel"));
  lastFocus?.focus?.();
}
$("#drawer-x").addEventListener("click", closeDrawer);
$("#drawer-bg").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeDrawer(); });

// --- order picker ---------------------------------------------------------------------------------------------------
const setStatus = (t) => { $("#status").textContent = t; };

async function loadPicker(selected) {
  const res = await Promise.all(DEMO_MANDATES.map((m) => soft(get(`/orders?mandate_id=${encodeURIComponent(m)}`))));
  const orders = res.flatMap((r) => (r.ok ? r.v.orders || [] : [])).filter((o) => !isQa(o))
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  const groups = new Map();
  for (const o of orders) { if (!groups.has(o.buyer)) groups.set(o.buyer, []); if (groups.get(o.buyer).length < 12) groups.get(o.buyer).push(o); }
  const label = (o) => `${o.id} · ${STATUS[o.status] || o.status} · ${eur(o.total_ex_vat)} · ${(o.lines || [])[0]?.name || ""}`.slice(0, 96);
  const sel = $("#pick");
  const known = orders.some((o) => o.id === selected);
  sel.innerHTML = (selected && !known ? `<option value="${esc(selected)}">${esc(selected)}</option>` : "")
    + (orders.length ? [...groups].map(([buyer, os]) => `<optgroup label="${esc(buyer)}">${os.map((o) => `<option value="${esc(o.id)}">${esc(label(o))}</option>`).join("")}</optgroup>`).join("")
      : `<option value="">No demo orders found</option>`);
  return orders;
}

function choose(id, push = true) {
  if (!id) return;
  $("#pick").value = id;
  if (push) {
    const u = new URL(location.href);
    u.searchParams.set("order", id);
    history.pushState({ order: id }, "", u);
  }
  load(id);
}
$("#pick").addEventListener("change", (ev) => choose(ev.target.value));
addEventListener("popstate", () => { const id = new URLSearchParams(location.search).get("order"); if (id) choose(id, false); });

// --- try it live: a private sandbox, the hotel quickstart quote + order, then this board -----------------------------
async function tryLive() {
  if (!confirm("Create a private sandbox (24 h, copies of the demo mandates) and run its quickstart: the hotel's agent asks for a signed quote for 100 L of lactose-free milk and orders it. Uses one quote's live Valio lookups. Continue?")) return;
  const btn = $("#try");
  btn.disabled = true;
  let sb = null;
  const go = (orderId, note) => {
    // The agent token reads this sandbox's orders; kept for this tab only.
    store.set(sessionStorage, TOKEN_KEY, sb.agent_token);
    store.set(sessionStorage, NOTE_KEY, note || "");
    const u = new URL(location.href);
    u.searchParams.set("order", orderId);
    if (sb.sandbox?.id) u.searchParams.set("sandbox", sb.sandbox.id);
    location.assign(u.toString());
  };
  try {
    setStatus("Creating a sandbox…");
    sb = await get("/sandbox", { method: "POST" });
    const m = /-d '(\{.*\})'\s*$/.exec(sb.quickstart?.curl_quote || "");
    if (!m || !sb.agent_token) throw new Error("the sandbox answer has no quickstart quote");
    const body = JSON.parse(m[1]);
    const auth = { authorization: "Bearer " + sb.agent_token, "content-type": "application/json" };
    setStatus(`Sandbox ${sb.sandbox?.id || ""}: the agent asks for a quote (live Valio price and stock)…`);
    const qr = await get("/quotes", { method: "POST", headers: auth, body: JSON.stringify(body) });
    const qid = qr.quote?.id;
    if (!qid) throw new Error(qr.message || "no quote");
    setStatus(`Quote ${qid} signed. The agent orders it…`);
    const sid = sb.sandbox?.id || "";
    const o = await get("/orders", { method: "POST", headers: auth, body: JSON.stringify({ agent_id: body.agent_id, mandate_id: body.mandate_id, quote_id: qid, idempotency_key: `${sid.toLowerCase()}-${qid}` }) });
    if (!o.id) throw new Error(o.message || "no order");
    go(o.id, `Your sandbox ${sid}: the hotel agent's quote and order, just now.`);
  } catch (e) {
    // The sandbox exists but the live quote failed (Valio down, past the cut-off): show its seeded held order instead.
    if (sb?.agent_token && sb.seeded?.held_order_id) return go(sb.seeded.held_order_id, `The live quote failed (${e.message}). Showing your sandbox's seeded standing-order renewal, held for approval, instead.`);
    setStatus(`Try it live stopped: ${e.message}${e.status === 429 ? " (rate limited, try again later)" : ""}`);
  } finally { btn.disabled = false; }
}
$("#try").addEventListener("click", tryLive);

// --- start ----------------------------------------------------------------------------------------------------------
(async () => {
  PENDING_NOTE = store.get(sessionStorage, NOTE_KEY);
  store.set(sessionStorage, NOTE_KEY, "");
  const want = QS.get("order");
  const orders = await loadPicker(want);
  const id = want || orders[0]?.id;
  if (id) choose(id, false);
  else { $("#out").innerHTML = `<div class="msg"><b>No order to show</b>Pick an order, or press Try it live.</div>`; }
})();
