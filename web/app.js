// Sous gateway UI, two sides of one app (same file, chosen by <body data-side>):
//   buyer  (/ui/buyer.html): the buyer's own people. Approvals live here: a held order is the buyer's decision.
//   seller (/ui/):           Valio Aimo. Orders, audit, mandates, standing orders, stats, insights; held orders are
//                            read-only ("waiting for the buyer") with a link to the buyer side.
// Plain JS, no build step. Talks to the gateway's REST API on the same origin
// (override with ?api=http://host:port for development).

const API = new URLSearchParams(location.search).get("api") || "";
const $ = (sel, el = document) => el.querySelector(sel);
const view = $("#view");
const REFRESH_MS = 4000;

// --- helpers -----------------------------------------------------------------

// Human-only actions (approve, reject, revoke) need the approver code when the gateway sets one
// (X-Approver-Code; a stand-in for the buyer's real login). Kept in this browser only.
const store = {
  get: (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

// Judge sandbox: /ui/?sandbox=SBX-XXXX shows only that sandbox's mandates, orders and audit events.
// Its approver token (sbxa_…) is kept per sandbox, and also unlocks substitutes and suggestions there.
const SANDBOX = (new URLSearchParams(location.search).get("sandbox") || "").trim().toUpperCase();
const CODE_KEY = "vk-approver-code" + (SANDBOX ? ":" + SANDBOX : "");
const SIDE = document.body.dataset.side === "buyer" ? "buyer" : "seller";
const SBX_QS = SANDBOX ? `?sandbox=${encodeURIComponent(SANDBOX)}` : "";
const buyerLink = (id) => `/ui/buyer.html${SBX_QS}#/orders/${encodeURIComponent(id)}`;
const passkeyLink = (id) => `/ui/approve.html?order=${encodeURIComponent(id)}${SANDBOX ? `&sandbox=${encodeURIComponent(SANDBOX)}` : ""}`;
const SANDBOX_LISTS = /^\/(orders|mandates|audit|notifications|standing-orders)(\?|$)/;

// One code prompt at a time: the page refreshes every few seconds, so parallel 401s share one dialog,
// and after "Cancel" background reads stop asking until the human starts an action themselves.
let codePrompt = null, codeDeclined = false;

async function api(path, opts = {}, retried = false) {
  const headers = { "content-type": "application/json" };
  const code = store.get(CODE_KEY);
  const write = (opts.method || "GET") !== "GET";
  // The approver code also unlocks reads when the gateway scopes them (read access control).
  if (code) headers["x-approver-code"] = code;
  const url = SANDBOX && SANDBOX_LISTS.test(path) ? path + (path.includes("?") ? "&" : "?") + "sandbox=" + encodeURIComponent(SANDBOX) : path;
  if (SANDBOX && code) headers.authorization = "Bearer " + code;
  const res = await fetch(API + url, {
    ...opts,
    headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { message: text }; }
  if (res.status === 401 && !(retried && !write) && (write || !codeDeclined)) {
    // Ask in-page (masked, no auto-capitalise/autocorrect: phone keyboards change codes) until it
    // is accepted or the human cancels.
    codePrompt ||= askCode(code || retried
      ? "That approver code was not accepted. Check it and try again (it is case-sensitive)."
      : SANDBOX ? `This needs the approver token of sandbox ${SANDBOX} (sbxa_…, from POST /sandbox).`
      : write ? "This action needs the human approver code."
      : "Reading this data needs the approver code (demo buyers stay public).").finally(() => { codePrompt = null; });
    const c = await codePrompt;
    if (c) { codeDeclined = false; store.set(CODE_KEY, c); return api(path, opts, true); }
    if (!write) codeDeclined = true;
    else store.set(CODE_KEY, "");
    throw new Error("Approver code needed for this.");
  }
  if (!res.ok) {
    const msg = data?.message || data?.detail?.message || data?.detail || res.statusText;
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    err.status = res.status;  // network failures throw a TypeError without one
    throw err;
  }
  return data;
}

// A small in-page dialog for the approver code. Resolves to the code, or null when cancelled.
function askCode(message) {
  return new Promise((resolve) => {
    const d = document.createElement("div");
    d.className = "codebox";
    d.innerHTML = `<form class="codebox-panel" role="dialog" aria-modal="true" aria-labelledby="codebox-t">
      <h3 id="codebox-t" style="margin-top:0">Approver code</h3>
      <p class="small muted">${esc(message)} It stays in this browser only.</p>
      <input type="password" name="code" autocomplete="off" autocapitalize="none" autocorrect="off"
        spellcheck="false" enterkeyhint="done" aria-label="Approver code" required>
      <label class="small muted show"><input type="checkbox"> Show</label>
      <div class="actions">
        <button type="button" class="btn no" data-cancel>Cancel</button>
        <button type="submit" class="btn yes">Continue</button>
      </div></form>`;
    document.body.appendChild(d);
    const input = d.querySelector("input[name=code]");
    d.querySelector(".show input").addEventListener("change", (e) => { input.type = e.target.checked ? "text" : "password"; });
    const done = (v) => { d.remove(); resolve(v); };
    d.querySelector("form").addEventListener("submit", (e) => { e.preventDefault(); done(input.value.trim() || null); });
    d.querySelector("[data-cancel]").addEventListener("click", () => done(null));
    d.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); done(null); } });
    setTimeout(() => input.focus(), 30);
  });
}

// QA test data (agents qa-*, mandates M-QA-*, buyers "QA …") is hidden from the demo views unless the
// footer toggle shows it. Hiding is display-only: the audit chain check still covers every event.
const QA_AGENT = /^qa[-_:]/i, QA_MANDATE = /^M-QA-/i, QA_BUYER = /^QA\b/;
let showQA = (() => { try { return localStorage.getItem("vk-show-qa") === "1"; } catch { return false; } })();
const isQAOrder = (o) => QA_AGENT.test(o.agent_id || "") || QA_MANDATE.test(o.mandate_id || "") || QA_BUYER.test(o.buyer || "");
const isQAMandate = (m) => QA_MANDATE.test(m.id || m.mandate_id || "") || QA_AGENT.test(m.agent_id || "") || QA_BUYER.test(m.buyer || "");
const isQAEvent = (e) => QA_AGENT.test(e.actor || "") || QA_MANDATE.test(e.subject || "") || QA_BUYER.test(e.buyer || "") ||
  QA_AGENT.test(e.data?.agent_id || "") || QA_MANDATE.test(e.data?.mandate_id || "");
const noQA = (list, isQA) => (showQA ? list : list.filter((x) => !isQA(x)));

// Agent auth can be required per mandate (require_agent_auth) or gateway-wide (SOUS_REQUIRE_AGENT_AUTH, /health).
let authGlobal = false;
const loadAuthGlobal = () => fetch(API + "/health").then((r) => r.json())
  .then((h) => { authGlobal = !!h.agent_auth_required_globally; }).catch(() => {});
const authRequired = (m) => !!m.require_agent_auth || authGlobal;

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// UI review S4/S6/S7: display-only clean-ups of gateway text.
// Buyer names without the "(sandbox SBX-…)" suffix (the banner already says it).
const bn = (b) => String(b ?? "").replace(/\s*\(sandbox SBX-[A-Z0-9]+\)$/i, "");
// Engine messages say "€1,560.00"; show money the same way as the rest of the UI ("1 560,00 €").
// ... and ALL-CAPS catalogue names inside them ("BRAVO CHEF 10KG CRUSHED TOMATOES") in sentence case:
// a run of 3+ upper-case words, at least two of them real words, so codes like "UHT" or "AM, AN" stay.
const CAPS_RUN = /\b[A-ZÅÄÖ0-9][A-ZÅÄÖ0-9.,%/&'-]*(?:\s+[A-ZÅÄÖ0-9][A-ZÅÄÖ0-9.,%/&'-]*){2,}\b/g;
const decap = (t) => t.replace(CAPS_RUN, (run) =>
  (run.match(/\b[A-ZÅÄÖ]{3,}\b/g) || []).length >= 2 ? run.charAt(0) + run.slice(1).toLowerCase() : run);
const msg = (t) => decap(esc(t)).replace(/€(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)/g,
  (_, n) => eur(parseFloat(n.replace(/,/g, ""))));
// SAP sales units -> words; ALL-CAPS catalogue names -> sentence case.
const UNIT = { ST: "pcs", KPL: "pcs", PRK: "packs", KI: "cartons", PSS: "bags", BOT: "bottles", PLO: "bottles", RAS: "tins", SK: "cases" };
const unit = (u) => UNIT[String(u || "").toUpperCase()] || u || "";
const pname = (n) => { n = String(n ?? ""); return /[a-zåäö]/.test(n) ? n : n.charAt(0) + n.slice(1).toLowerCase(); };
// Event names: known labels, otherwise "sandbox.created" -> "Sandbox created".
const evLabel = (e) => EVENT_LABEL[e] || (e.charAt(0).toUpperCase() + e.slice(1)).replace(/[._]/g, " ");

const eur = (n) => n == null ? "–" :
  new Intl.NumberFormat("fi-FI", { style: "currency", currency: "EUR" }).format(n);

const when = (iso) => {
  if (!iso) return "–";
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  const dm = d.toLocaleDateString("fi-FI", { timeZone: "Europe/Helsinki", day: "numeric", month: "numeric" });
  const hm = d.toLocaleTimeString("fi-FI", { timeZone: "Europe/Helsinki", hour: "2-digit", minute: "2-digit" }).replace(".", ":");
  return `${dm} ${hm}`;
};
const dateOnly = (iso) => {
  if (!iso) return "–";
  const d = new Date(iso.length === 10 ? iso + "T12:00:00" : iso);
  return isNaN(d) ? esc(iso) : `${d.toLocaleDateString("en-GB", { weekday: "short" })} ${d.toLocaleDateString("fi-FI", { day: "numeric", month: "numeric" })}`;
};

function untilText(iso) {
  const ms = new Date(iso) - Date.now();
  if (isNaN(ms)) return null;
  if (ms <= 0) return { text: "cut-off passed", late: true };
  const m = Math.round(ms / 60000);
  const h = Math.floor(m / 60);
  const d = Math.floor(h / 24);
  const text = d >= 2 ? `${d} days` : h >= 1 ? `${h} h ${m % 60} min` : `${m} min`;
  return { text, late: ms < 30 * 60000 };
}

const STATUS_LABEL = {
  ordered: "Ordered", pending_approval: "Needs approval", refused: "Refused",
  rejected: "Rejected", expired: "Expired",
};
// One tier vocabulary everywhere (UI review S3): Auto · Auto + FYI · Needs approval · Refused.
// Badges differ in icon and fill, not only colour (S2, styles after web/tokens.css).
const TIER_LABEL = { silent: "Auto", notify: "Auto + FYI", ask: "Needs approval", block: "Refused",
  best: "Best", better: "Better", good: "Good" };
const TIER_STYLE = { silent: "silent", notify: "notify", ask: "ask", block: "block",
  ordered: "silent", pending_approval: "ask", refused: "block", rejected: "block", expired: "block" };
const RANKS = new Set(["best", "better", "good"]);
const tier = (t) => RANKS.has(t)
  ? `<span class="rank r-${esc(t)}">${esc(TIER_LABEL[t])}</span>`
  : `<span class="tier tier-${TIER_STYLE[t] || "silent"}">${esc(TIER_LABEL[t] || STATUS_LABEL[t] || t)}</span>`;

// Material Symbols (Rounded), loaded in index.html. Decorative: the text next to it carries the meaning.
const ic = (name, cls = "") => `<span class="ms ${cls}" aria-hidden="true">${name}</span>`;
const STATUS_ICON = { ordered: "check_circle", pending_approval: "pending_actions", refused: "block",
  rejected: "cancel", expired: "timer_off" };

function toast(msg, ms = 3500) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

// Who is approving. Kept in this browser only.
function approver() {
  try { return localStorage.getItem("vk-approver") || ""; } catch { return ""; }
}
function setApprover(name) {
  try { localStorage.setItem("vk-approver", name); } catch { /* private mode */ }
  renderWho();
}
function renderWho() {
  $("#who").textContent = approver() || "Set your name";
}
$("#who").addEventListener("click", () => {
  const n = prompt("Your name (recorded in the audit log when you approve or reject):", approver());
  if (n !== null) setApprover(n.trim());
});

// --- signatures: verify the gateway's Ed25519 JWS in the browser -----------------

let jwksCache = null;
const b64u = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), (c) => c.charCodeAt(0));

async function verifyJws(token) {
  try {
    const [h, p, s] = token.split(".");
    const header = JSON.parse(new TextDecoder().decode(b64u(h)));
    jwksCache ||= await api("/.well-known/jwks.json");
    const jwk = jwksCache.keys.find((k) => k.kid === header.kid);
    if (!jwk) return { ok: false, why: "unknown key id" };
    const key = await crypto.subtle.importKey("jwk", { kty: "OKP", crv: "Ed25519", x: jwk.x }, { name: "Ed25519" }, false, ["verify"]);
    const ok = await crypto.subtle.verify({ name: "Ed25519" }, key, b64u(s), new TextEncoder().encode(`${h}.${p}`));
    return { ok, why: ok ? `Ed25519 signature valid (key ${jwk.kid})` : "signature does not match", typ: header.typ };
  } catch (e) {
    return { ok: false, why: "this browser can't check Ed25519: " + e.message };
  }
}

function sigButton(token, label = "Verify signature") {
  if (!token) return "";
  const id = "sig-" + Math.random().toString(36).slice(2);
  queueMicrotask(() => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("click", async () => {
      el.disabled = true;
      const r = await verifyJws(token);
      el.outerHTML = `<span class="sig ${r.ok ? "ok" : "bad"}">${r.ok ? "✔" : "✖"} ${esc(r.why)}</span>`;
    });
  });
  return `<button class="btn plain" id="${id}">🔏 ${esc(label)}</button>`;
}

// --- shared order pieces ---------------------------------------------------------

function findingsList(findings, { only, plain } = {}) {
  const fs = (findings || []).filter((f) => !only || only.includes(f.outcome));
  if (!fs.length) return "";
  return `<ul class="findings${plain ? " plain" : ""}">${fs.map((f) => `
    <li>${plain ? "" : tier(f.outcome)}<div>${msg(f.message)}<div class="rule">${esc(f.rule)}${f.product_id ? " · " + esc(f.product_id) : ""}</div></div></li>`).join("")}
  </ul>`;
}

// What the line is for (sous/purpose.py): decides how strictly it may be substituted.
const PURPOSE_SAYS = { exact: "only this product", ask: "a substitute would need the buyer's yes", allowed: "any brand is fine" };
function purposeChip(l) {
  if (!l.purpose && !l.substitution) return "";
  return `<div class="small purpose">${ic(l.substitution === "exact" ? "lock" : l.substitution === "ask" ? "help" : "swap_horiz")}
    ${l.purpose ? `for ${esc(l.purpose)}` : ""}${l.substitution ? ` · ${esc(PURPOSE_SAYS[l.substitution] || l.substitution)}` : ""}
    ${l.substitute_for ? ` · replaces <span class="mono">${esc(l.substitute_for)}</span>` : ""}</div>`;
}

function linesTable(lines) {
  return `<table class="lines"><thead><tr>
      <th>Product</th><th class="num">Qty</th><th class="num hide-s">Unit €</th><th class="num">Total €</th><th class="hide-s">Delivery</th>
    </tr></thead><tbody>${(lines || []).map((l) => `
    <tr>
      <td>${esc(pname(l.name))}<div class="muted small mono">${esc(l.product_id)}${l.logistic_model ? " · " + esc(l.logistic_model) : ""}</div>
        ${purposeChip(l)}</td>
      <td class="num">${esc(l.quantity)} ${esc(unit(l.sales_unit))}</td>
      <td class="num hide-s">${eur(l.unit_price_ex_vat)}</td>
      <td class="num">${eur(l.line_total_ex_vat)}</td>
      <td class="hide-s">${dateOnly(l.delivery_date)}</td>
    </tr>`).join("")}</tbody></table>`;
}

// "Decide by Sun 15.11 15:00 (38 h left)": plain text; only a near or passed cut-off turns red.
function decideBy(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const day = d.toLocaleDateString("en-GB", { weekday: "short", timeZone: "Europe/Helsinki" });
  const dm = d.toLocaleDateString("fi-FI", { day: "numeric", month: "numeric", timeZone: "Europe/Helsinki" });
  const hm = d.toLocaleTimeString("fi-FI", { hour: "2-digit", minute: "2-digit", timeZone: "Europe/Helsinki" }).replace(".", ":");
  return `Decide by ${day} ${dm} ${hm} (<span class="deadline" data-until="${esc(iso)}"></span>)`;
}

// The gateway's approve_by (cut-off minus 15 min, or the mandate/sandbox expiry) is the real deadline: a later
// approval gets 409 approval_deadline_passed. Fall back to the earliest line cut-off for older orders.
function earliestCutoff(order) {
  if (order.approve_by) return order.approve_by;
  const cs = (order.lines || []).map((l) => l.order_cutoff).filter(Boolean).sort();
  return cs[0] || null;
}

// --- views ---------------------------------------------------------------------------

const views = {};
let current = null;
let timer = null;

// Approvals inbox: orders the mandate engine held for a human.
views.inbox = {
  key: "",
  async load() {
    const orders = noQA((await api("/orders?status=pending_approval")).orders, isQAOrder);
    setBadge(orders.length);
    const key = orders.map((o) => o.id + o.updated_at).join();
    if (key === this.key && view.dataset.view === "inbox") { tickDeadlines(); return; }
    // Keep notes the human is typing when a new order arrives and the list re-renders.
    const notes = {};
    view.querySelectorAll("[data-order]").forEach((c) => { notes[c.dataset.order] = c.querySelector("[data-note]")?.value; });
    const firstRender = view.dataset.view !== "inbox";
    const seen = this.seen || new Set();
    this.key = key;
    view.dataset.view = "inbox";
    const main = orders.length
      ? `${orders.map((o) => inboxCard(o, !firstRender && !seen.has(o.id))).join("")}`
      : `<div class="empty"><div class="big">Nothing waiting</div>
          <p>${SIDE === "buyer" ? "Orders that fall outside your agent's mandate show up here for you to approve or reject."
            : "Orders held by a buyer's mandate show up here while they wait for the buyer's own approval."}</p>
          <p><a href="#/orders">See the last decided orders</a> · try it yourself: <a href="/docs#/sandbox">POST /sandbox</a> gives you
            private copies of the demo buyers and a token to approve with.</p></div>`;
    view.innerHTML = `<div class="head"><div><h2>${orders.length ? (SIDE === "buyer" ? "Waiting for you" : "Waiting for buyers") : (SIDE === "buyer" ? "Approvals" : "Held")}</h2>
      <div class="muted">${orders.length ? `${orders.length} order${orders.length > 1 ? "s" : ""} held by the mandate engine` : "Agents are ordering within their mandates."}</div></div></div>
      <div class="inbox-grid"><div>${main}</div>
        <aside class="feed"><div class="feed-head"><span class="live-dot"></span>Live activity</div><div id="feed"></div></aside></div>`;
    this.seen = new Set(orders.map((o) => o.id));
    renderFeed();
    tickDeadlines();
    for (const card of view.querySelectorAll("[data-order]")) {
      if (notes[card.dataset.order]) card.querySelector("[data-note]").value = notes[card.dataset.order];
      wireDecision(card);
    }
  },
};

// --- live: poll the audit log, announce what agents just did, keep a feed -----------------

const live = { seq: null, events: [] };
const ANNOUNCE = {
  "order.ordered": (d, e) => `Ordered automatically · ${e.buyer} · ${eur(d.total_ex_vat)}`,
  "order.pending_approval": (d, e) => `Held for a human · ${e.buyer} · ${eur(d.total_ex_vat)}`,
  "order.refused": (d, e) => `Refused by the mandate · ${e.buyer} · ${eur(d.total_ex_vat)}`,
  "order.approved": (d, e) => `${e.actor} approved ${e.subject}`,
  "order.rejected_by_human": (d, e) => `${e.actor} rejected ${e.subject}`,
};

async function pollLive() {
  const events = noQA((await api("/audit?limit=40")).events, isQAEvent).slice(0, 14);
  const fresh = live.seq == null ? [] : events.filter((e) => e.seq > live.seq).reverse();
  live.seq = events[0]?.seq ?? live.seq ?? 0;
  live.events = events;
  live.fresh = new Set(fresh.map((e) => e.seq));
  const own = approver();
  for (const e of fresh) {
    const f = ANNOUNCE[e.event];
    if (f && e.actor !== own) toast(f(e.data || {}, e), 4500);
  }
  renderFeed();
}

function renderFeed() {
  const el = document.getElementById("feed");
  if (!el) return;
  el.innerHTML = live.events.length ? live.events.map((e) => `
    <a class="feed-ev ${live.fresh?.has(e.seq) ? "fresh" : ""}" href="#/orders${/^PO-/.test(e.subject || "") ? "/" + esc(e.subject) : ""}">
      <div class="row"><span>${esc(evLabel(e.event))}</span><span class="mono muted">${when(e.ts).split(" ").pop()}</span></div>
      <div class="small muted"><span class="actor ${esc(e.actor_type)}">${esc(e.actor_type)}</span> ${esc(e.buyer || e.actor)}</div>
    </a>`).join("") : `<div class="small muted">No activity yet.</div>`;
}

function inboxCard(o, isNew = false) {
  const cut = earliestCutoff(o);
  const asks = (o.decision?.findings || []).filter((f) => f.outcome === "ask");
  return `<article class="card ask ${isNew ? "fresh" : ""}" data-order="${esc(o.id)}">
    <div class="row">
      <div><div class="muted small">${esc(bn(o.buyer))}</div><div class="big">${eur(o.total_ex_vat)} <span class="muted small">ex VAT</span></div>
        ${cut ? `<div class="small" style="margin-top:4px">${decideBy(cut)}</div>` : ""}</div>
      <div>${tier("ask")}</div>
    </div>
    ${claimBlock(o)}
    <h3>Why it needs you</h3>
    ${findingsList(asks, { plain: true })}
    ${linesTable(o.lines)}
    <dl class="kv small">
      <dt>Agent</dt><dd class="mono">${esc(o.agent_id)}</dd>
      <dt>Mandate</dt><dd class="mono">${esc(o.mandate_id)}</dd>
      <dt>Order</dt><dd><a href="#" data-open="${esc(o.id)}" class="mono">${esc(o.id)}</a> · ${eur(o.total_inc_vat)} incl. VAT · delivery ${dateOnly(o.delivery_date)}</dd>
    </dl>
    ${SIDE === "buyer" ? `<textarea data-note rows="2" placeholder="Note for the agent and the log (optional)"></textarea>
    ${rememberBox(o)}
    ${editBox(o)}
    <div class="actions">
      <button class="btn no" data-act="reject">Reject</button>
      ${approveButton(o)}
    </div>
    ${passkeyNote(o)}` : waitingForBuyer(o)}
  </article>`;
}

// Seller side: a held order is the buyer's decision, so Valio Aimo sees it read-only, with the way to the buyer side.
function waitingForBuyer(o) {
  return `<div class="wait-buyer"><p class="note-line">${ic("hourglass_top")} Waiting for ${esc(bn(o.buyer))}'s approval.
      Held orders are decided by the buyer's own people on the buyer side, not by Valio Aimo.</p>
    <a class="btn plain sm" href="${buyerLink(o.id)}">Open on the buyer side ${ic("arrow_forward")}</a></div>`;
}

// A mandate that accepts only passkeys: the gateway gives such an order an approval_url on the passkey page
// (service.approval_url; every other held order gets the buyer dashboard). The approver code can still reject it,
// but approving (and approve with changes) is refused with 403 passkey_required, so offer the passkey page instead.
const passkeyOnly = (o) => /\/approve\.html\?/.test(o.approval_url || "");
function approveButton(o) {
  return passkeyOnly(o)
    ? `<a class="btn yes" href="${passkeyLink(o.id)}">Approve with a passkey</a>`
    : `<button class="btn yes" data-act="approve">Approve ${eur(o.total_ex_vat)}</button>`;
}

// Buyer side: the passkey page (web/approve.html) approves this exact order with the buyer's own device.
function passkeyNote(o) {
  if (passkeyOnly(o)) return `<p class="small muted">This mandate accepts only a passkey for approval; the approver code can still reject.</p>`;
  return `<p class="small muted">Or <a href="${passkeyLink(o.id)}">approve with a passkey</a> on your phone
    (required when the mandate accepts only passkeys).</p>`;
}

// Remember a human's allergen clearance (sous/clearance.py): offered only when the order waits on allergen.unknown.
function rememberBox(o) {
  const fs = (o.decision?.findings || []).filter((f) => f.rule === "allergen.unknown");
  if (!fs.length) return "";
  const names = [...new Set(fs.map((f) => pname((o.lines || []).find((l) => l.product_id === f.product_id)?.name || f.product_id)))];
  return `<label class="remember"><input type="checkbox" data-remember>
    <span>I checked ${esc(names.join(", "))} myself: don't ask again for ${esc(bn(o.buyer))}</span></label>`;
}
// Approve with changes (sous/approve_changes.py): edit quantities, re-priced live and re-checked by the gateway.
function editBox(o) {
  if (!(o.lines || []).length || passkeyOnly(o)) return "";  // passkey mandates: approve with changes is refused
  return `<details class="qty-edit"><summary>${ic("edit")} Change quantities</summary>
    <div class="qty-rows">${o.lines.map((l, i) => `<label><span>${esc(pname(l.name))}</span>
      <input type="number" min="0" step="any" inputmode="decimal" data-line="${i}" data-was="${esc(l.quantity)}" value="${esc(l.quantity)}">
      <span class="muted small">${esc(unit(l.sales_unit))}</span></label>`).join("")}</div>
    <p class="small muted">Re-priced live with Valio Aimo and checked against the mandate again; 0 removes a line.</p>
    <button class="btn yes" data-act="changes">${ic("published_with_changes")} Re-price and approve</button></details>`;
}

// After the order (sous/amendments.py): Valio reports a delay, short pick or cancelled line; the buyer's mandate
// decides whether it just applies (FYI) or waits for the buyer.
const AMEND_KIND = { delay: ["schedule", "Delivery delayed"], short_pick: ["inventory_2", "Short delivery"],
  cancel_line: ["remove_shopping_cart", "Line cancelled"], substitution_at_pick: ["swap_horiz", "Substituted at picking"] };
const AMEND_STATUS = { applied: "Applied", pending_buyer: "Waiting for the buyer", accepted: "Accepted", rejected: "Rejected" };
function amendSection(o) {
  const list = (o.amendments || []).map((am) => {
    const [icon, label] = AMEND_KIND[am.kind] || ["edit_note", am.kind];
    const l = am.line != null ? o.lines[am.line] : null;
    const what = am.kind === "delay" ? `to ${dateOnly(am.new_delivery_date)}${l ? ` (${esc(pname(l.name))})` : ""}`
      : am.kind === "short_pick" ? `${esc(am.delivered_quantity)} of ${esc(l?.quantity ?? "")} ${esc(unit(l?.sales_unit))} ${esc(pname(l?.name || ""))}`
      : l ? esc(pname(l.name)) : "";
    return `<li class="amend">${ic(icon, am.status === "pending_buyer" ? "st-ask" : "st-notify")}
      <div><b>${label}</b> ${what} <span class="muted small">· ${esc(AMEND_STATUS[am.status] || am.status)} · ${when(am.reported_at)}</span>
        <div class="small muted">${esc(am.reason)}</div>
        ${am.status === "pending_buyer" && SIDE === "seller" ? `<div class="small muted">Waiting for the buyer to accept or reject it.</div>` : ""}
        ${am.status === "pending_buyer" && SIDE === "buyer" ? `<div class="amend-acts"><button class="btn plain sm" data-amend="${esc(am.id)}" data-res="reject">Reject</button>
          <button class="btn yes sm" data-amend="${esc(am.id)}" data-res="accept">Accept</button></div>` : ""}</div></li>`;
  }).join("");
  const can = o.status === "ordered" && SIDE === "seller";  // Valio reports exceptions; the buyer accepts or rejects
  return `<h3>After the order</h3>
    ${list ? `<ul class="amends">${list}</ul>` : `<p class="small muted">No delays or short deliveries reported.</p>`}
    ${can ? `<details class="amend-form"><summary>${ic("report")} Report a delivery exception (Valio)</summary>
      <div class="amend-grid">
        <label>What<select data-k="kind"><option value="delay">Delivery delayed</option><option value="short_pick">Short delivery</option><option value="cancel_line">Line can't be delivered</option></select></label>
        <label>Line<select data-k="line"><option value="">Whole order</option>${o.lines.map((l, i) => `<option value="${i}">${esc(pname(l.name))}</option>`).join("")}</select></label>
        <label data-for="delay">New date<input type="date" data-k="new_delivery_date"></label>
        <label data-for="short_pick" hidden>Delivered<input type="number" min="0" step="any" data-k="delivered_quantity"></label>
        <label class="wide">Reason<input type="text" data-k="reason" maxlength="300" placeholder="e.g. Truck breakdown, delivery moves a day"></label>
      </div>
      <button class="btn yes sm" data-amend-report>Report it</button></details>` : ""}`;
}
function wireAmendments(o) {
  const root = $("#sheet-body");
  const form = root.querySelector(".amend-form");
  if (form) {
    const kind = form.querySelector('[data-k="kind"]');
    const sync = () => form.querySelectorAll("[data-for]").forEach((x) => { x.hidden = x.dataset.for !== kind.value; });
    kind.addEventListener("change", sync); sync();
    form.querySelector("[data-amend-report]").addEventListener("click", async (e) => {
      const v = (k) => form.querySelector(`[data-k="${k}"]`).value.trim();
      const body = { kind: v("kind"), reason: v("reason") || "Reported by Valio Aimo", by: approver() || "Valio Aimo" };
      if (v("line") !== "") body.line = +v("line");
      if (body.kind === "delay") body.new_delivery_date = v("new_delivery_date");
      if (body.kind === "short_pick") body.delivered_quantity = +v("delivered_quantity");
      e.target.disabled = true;
      try {
        await api(`/orders/${encodeURIComponent(o.id)}/amendments`, { method: "POST", body });
        toast("Reported. The buyer has been told.", 4000); openOrder(o.id);
      } catch (err) { toast("Failed: " + err.message, 6000); e.target.disabled = false; }
    });
  }
  root.querySelectorAll("[data-amend]").forEach((b) => b.addEventListener("click", async () => {
    const by = approver() || (prompt("Your name, for the audit log:") || "").trim();
    if (!by) return;
    b.disabled = true;
    try {
      await api(`/orders/${encodeURIComponent(o.id)}/amendments/${encodeURIComponent(b.dataset.amend)}/${b.dataset.res}`, { method: "POST", body: { by } });
      toast(b.dataset.res === "accept" ? "Accepted." : "Rejected: that part is cancelled with a credit.", 4000); openOrder(o.id);
    } catch (err) { toast("Failed: " + err.message, 6000); b.disabled = false; }
  }));
}

function wireDecision(card, onDone) {
  const id = card.dataset.order;
  card.querySelector("[data-open]")?.addEventListener("click", (e) => { e.preventDefault(); openOrder(id); });
  for (const b of card.querySelectorAll("[data-act]")) {
    b.addEventListener("click", async () => {
      let by = approver();
      if (!by) {
        by = (prompt("Your name, for the audit log:") || "").trim();
        if (!by) return;
        setApprover(by);
      }
      const act = b.dataset.act;
      const note = card.querySelector("[data-note]")?.value.trim() || null;
      let changes = [];
      if (act === "changes") {
        changes = [...card.querySelectorAll("[data-line]")].map((x) => ({ line: +x.dataset.line, quantity: +x.value, was: +x.dataset.was }))
          .filter((c) => Number.isFinite(c.quantity) && c.quantity !== c.was).map(({ line, quantity }) => ({ line, quantity }));
        if (!changes.length) { toast("Change a quantity first."); return; }
      }
      card.querySelectorAll("button").forEach((x) => (x.disabled = true));
      try {
        if (act === "changes") {
          const r = await api(`/orders/${encodeURIComponent(id)}/approve-with-changes`, { method: "POST", body: { by, note, changes } });
          toast(`Approved with changes as ${r.order.id}: ${eur(r.order.total_ex_vat)} (was ${eur(r.replaced.total_ex_vat)}).`, 6000);
          views.inbox.key = ""; if (current === "orders") views.orders.key = "";
          refresh(); onDone?.(r.order); openOrder(r.order.id);
          return;
        }
        const remember = act === "approve" && !!card.querySelector("[data-remember]")?.checked;
        const o = await api(`/orders/${encodeURIComponent(id)}/${act}`, {
          method: "POST", body: { by, note, ...(remember ? { remember_allergen_clearance: true } : {}) },
        });
        const msg = o.status === "ordered" ? `${id} approved and ordered.${o.allergen_clearances?.length ? " Clearance remembered." : ""}`
          : o.status === "rejected" ? `${id} rejected. The agent has been told.`
          : `${id}: ${STATUS_LABEL[o.status] || o.status}. ${o.approval?.result_reason || ""}`;
        toast(msg, 5000);
        views.inbox.key = "";
        if (current === "orders") views.orders.key = "";
        refresh();
        onDone?.(o);
      } catch (e) {
        toast("Failed: " + e.message, 6000);
        card.querySelectorAll("button").forEach((x) => (x.disabled = false));
      }
    });
  }
}

function tickDeadlines() {
  for (const el of document.querySelectorAll("[data-until]")) {
    const u = untilText(el.dataset.until);
    if (!u) continue;
    el.textContent = u.late && u.text === "cut-off passed" ? u.text : u.text + " left";
    el.classList.toggle("late", u.late);
    el.title = "Order cut-off " + when(el.dataset.until);
  }
}

function setBadge(n) {
  const b = $("#badge");
  b.hidden = !n;
  b.textContent = n;
  document.title = (n ? `(${n}) ` : "") + (SIDE === "buyer" ? "Sous · your orders" : "Sous · Valio Aimo");
}

// All orders, filterable by status.
views.orders = {
  filter: "",
  key: "",
  async load() {
    const orders = noQA((await api("/orders" + (this.filter ? "?status=" + this.filter : ""))).orders, isQAOrder);
    const key = this.filter + orders.map((o) => o.id + o.status).join();
    if (key === this.key && view.dataset.view === "orders") return;
    this.key = key;
    view.dataset.view = "orders";
    const chips = [["", "All"], ...Object.entries(STATUS_LABEL)];
    view.innerHTML = `<div class="head"><div><h2>Orders</h2><div class="muted">Every order an agent placed, and what the mandate decided</div></div></div>
      <div class="chips" style="margin-bottom:12px">${chips.map(([k, v]) =>
        `<button class="chip ${k === this.filter ? "on" : ""}" data-f="${k}">${v}</button>`).join("")}</div>
      ${orders.length ? `<div class="list">${orders.map((o) => `
        <button class="item" data-open="${esc(o.id)}">
          <div><b>${esc(bn(o.buyer))}</b> <span class="muted mono small">${esc(o.id)}</span>
            <div class="small">${orderStory(o)}</div>
            <div class="muted small">${esc(o.agent_id)} · ${when(o.created_at)}</div></div>
          <div style="text-align:right">${orderBadge(o)}<div class="small">${eur(o.total_ex_vat)}</div></div>
        </button>`).join("")}</div>`
        : `<div class="empty">No orders${this.filter ? " with this status" : " yet"}.</div>`}`;
    view.querySelectorAll("[data-f]").forEach((c) => c.addEventListener("click", () => {
      this.filter = c.dataset.f; this.key = ""; this.load();
    }));
    view.querySelectorAll("[data-open]").forEach((c) => c.addEventListener("click", () => openOrder(c.dataset.open)));
  },
};

// The agent's stated reason, labelled for the approver: its own words, unverified, never used by a rule.
// `suspicious` = approval- or instruction-like wording (a prompt-injection attempt at the human).
function claimBlock(o) {
  const c = o.agent_claim || (o.agent_rationale ? { text: o.agent_rationale } : null);
  if (!c?.text) return "";
  return `<div class="claim${c.suspicious ? " sus" : ""}"><span class="rationale">“${esc(c.text)}”</span>
    <div class="small muted">Agent's own words, unverified · not used by any rule</div>
    ${c.suspicious ? `<div class="small claim-warn">⚠ Reads like an instruction to you (${esc((c.suspicious_terms || []).join(", "))}).
      Decide on the facts above; the rules already ignore it.</div>` : ""}</div>`;
}

// UI review S10: a row tells the story: first product and quantity, then how it was decided.
function orderStory(o) {
  const ls = o.lines || [];
  if (!ls.length) return `<span class="muted">no lines</span>`;
  const l = ls[0];
  return `${esc(pname(l.name))} × ${esc(l.quantity)} ${esc(unit(l.sales_unit))}${ls.length > 1 ? ` <span class="muted">+ ${ls.length - 1} more</span>` : ""}`;
}
function orderBadge(o) {
  if (o.status === "ordered") return o.approval?.approved ? `<span class="tier tier-silent">Approved</span>` : tier(o.decision?.outcome || "silent");
  return tier(o.status);
}

async function openOrder(id) {
  const o = await api("/orders/" + encodeURIComponent(id));
  const d = o.decision || {};
  const a = o.approval;
  openSheet(`
    <div class="muted small">${esc(bn(o.buyer))}</div>
    <h2>${esc(o.id)} ${tier(o.status)}</h2>
    <div class="big">${eur(o.total_ex_vat)} <span class="muted small">ex VAT · ${eur(o.total_inc_vat)} incl.</span></div>
    ${claimBlock(o)}
    ${o.status === "pending_approval" ? `<div class="ask-summary">
      <p>${msg(((d.findings || []).find((f) => f.outcome === "ask") || {}).message || "Held by the mandate for a human decision.")}</p>
      ${earliestCutoff(o) ? `<p class="small">${decideBy(earliestCutoff(o))}</p>` : ""}</div>` : ""}
    <dl class="kv small">
      <dt>Agent</dt><dd class="mono">${esc(o.agent_id)}</dd>
      <dt>Mandate</dt><dd class="mono">${esc(o.mandate_id)}</dd>
      <dt>Quote</dt><dd class="mono">${esc(o.quote_id)}</dd>
      <dt>Delivery</dt><dd>${dateOnly(o.delivery_date)}</dd>
      <dt>Placed</dt><dd>${when(o.created_at)}</dd>
    </dl>
    <h3>How it happened</h3>
    <ol class="flow" id="flow"><li class="muted small">Loading…</li></ol>
    <h3>Mandate decision ${tier(d.outcome)}</h3>
    <div class="muted small">By ${esc(d.by)} at ${when(d.at)}. Deterministic rules; no AI in this step.</div>
    ${findingsList(d.findings)}
    ${a ? `<h3>Human decision</h3>
      <p>${a.approved ? "Approved" : "Rejected"} by <b>${esc(a.by)}</b> at ${when(a.at)}${a.note ? `: “${esc(a.note)}”` : ""}</p>
      ${a.result_reason && a.result_reason !== a.note ? `<p class="muted">${esc(a.result_reason)}</p>` : ""}` : ""}
    ${a?.replaced_by ? `<p class="note-line">${ic("published_with_changes")} Approved with changes as <a href="#/orders/${esc(a.replaced_by)}">${esc(a.replaced_by)}</a>.</p>` : ""}
    ${o.approval_changes ? `<p class="note-line">${ic("published_with_changes")} ${esc(o.approval_changes.by)} changed
      <a href="#/orders/${esc(o.approval_changes.from_order)}">${esc(o.approval_changes.from_order)}</a> before approving:
      ${o.approval_changes.changes.map((c) => `${esc(pname(c.name))} ${esc(c.from)} → ${esc(c.to)} ${esc(unit(c.unit))}`).join("; ")}. Re-priced live.</p>` : ""}
    <div id="asof"></div>
    <h3>Lines</h3>
    ${linesTable(o.lines)}
    ${o.status === "ordered" || (o.amendments || []).length ? amendSection(o) : ""}
    <h3>Substitutes</h3>
    <p class="small muted">Alternatives from Valio Aimo's own assortment that keep the mandate's hard rules
      (allergens, claims, category, unit). Ranked best (no compromises), better (minor) and good (a real trade-off).</p>
    <div id="subs">${(o.lines || []).map((l) => {
      const flagged = (d.findings || []).some((f) => f.product_id === l.product_id && (f.outcome === "ask" || f.outcome === "block"));
      return `<div class="sub-line" data-pid="${esc(l.product_id)}">
        <div class="row"><div>${esc(pname(l.name))} <span class="muted mono">${esc(l.product_id)}</span>
          ${flagged ? ` ${tier(d.findings.find((f) => f.product_id === l.product_id && (f.outcome === "ask" || f.outcome === "block")).outcome)}` : ""}</div>
          <button class="btn plain" data-subs="${esc(l.product_id)}" data-qty="${esc(l.quantity)}"
            data-date="${esc(l.delivery_date || o.delivery_date)}">Find substitutes</button></div>
        <div class="sub-out"></div></div>`;
    }).join("")}</div>
    <h3>Signed receipt</h3>
    <p class="small muted">The gateway signs every order. Anyone can check it against the public key at <a href="${API}/.well-known/jwks.json" target="_blank">/.well-known/jwks.json</a>.</p>
    ${sigButton(o.receipt_jws, "Verify receipt signature")}
    <details><summary>Raw JWS</summary><pre>${esc(o.receipt_jws)}</pre></details>
    <h3>History</h3>
    <div id="order-audit" class="list"><div class="empty">Loading…</div></div>
    ${o.status === "pending_approval" && SIDE === "seller" ? waitingForBuyer(o) : ""}
    ${o.status === "pending_approval" && SIDE === "buyer" ? `<div class="actionbar decide" data-order="${esc(o.id)}">
      <input data-note type="text" placeholder="Note for the agent and the log (optional)" aria-label="Note">
      ${rememberBox(o)}
      ${editBox(o)}
      <div class="actionbar-buttons">
        <button class="btn no" data-act="reject">Reject</button>
        ${approveButton(o)}
      </div>${passkeyNote(o)}</div>` : ""}
  `);
  const box = $("#sheet-body [data-order]");
  if (box) { tickDeadlines(); wireDecision(box, () => openOrder(id)); }
  wireAmendments(o);
  $("#sheet-body").querySelectorAll("[data-subs]").forEach((b) => b.addEventListener("click", () => findSubstitutes(b, o)));
  const { events } = await api("/audit?subject=" + encodeURIComponent(o.id) + "&limit=50");
  const q = await api("/audit?subject=" + encodeURIComponent(o.quote_id) + "&limit=50").catch(() => ({ events: [] }));
  const all = [...new Map([...events, ...q.events].map((e) => [e.seq, e])).values()].sort((x, y) => x.seq - y.seq);
  const el = $("#order-audit");
  if (el) el.innerHTML = all.length ? all.map(eventRow).join("") : `<div class="empty">No events.</div>`;

  // The four steps from Valio Aimo's brief, rebuilt from the signed quote and the audit log.
  allergenNames ||= await api("/allergens").then((x) => x.codes).catch(() => ({}));
  const [quote, recent, hist] = await Promise.all([
    api("/quotes/" + encodeURIComponent(o.quote_id)).then((r) => r.quote).catch(() => null),
    api("/audit?limit=400").then((r) => r.events).catch(() => []),
    api(`/orders/${encodeURIComponent(o.id)}/history`).catch(() => null),
  ]);
  if (!$("#flow") || $("#sheet-body h2")?.textContent.indexOf(o.id) !== 0) return; // panel closed or replaced
  const quoteEv = all.find((e) => e.event === "quote.issued");
  const qTime = new Date(quote?.issued_at || quoteEv?.ts || o.created_at);
  // Link a catalog search only if it was this agent's last step before the quote (no other quote in
  // between) and it returned at least one of the quoted products. Newest first in `recent`.
  const quoted = new Set([...(quote?.lines || []), ...(quote?.alternatives_considered || [])].map((l) => l.product_id));
  // All of this agent's searches since its previous quote (newest first in `recent`).
  const searches = [];
  for (const e of recent) {
    if (e.actor !== o.agent_id || new Date(e.ts) > qTime || e.subject === o.quote_id) continue;
    if (e.event === "quote.issued") break;
    if (e.event === "catalog.search" && qTime - new Date(e.ts) < 15 * 60000 &&
        (e.data.results || []).some((id) => quoted.has(id))) searches.unshift(e);
  }
  $("#flow").innerHTML = flowSteps(o, quote, searches, all);
  $("#asof").innerHTML = asOfSection(o, hist);
}

function flowSteps(o, q, searches, events) {
  const search = searches[0];
  const found = new Set(searches.flatMap((s) => s.data.results || []));
  const d = o.decision || {};
  const a = o.approval;
  const calls = q?.valio_api_calls ? (q.valio_api_calls.price_calls || 0) + (q.valio_api_calls.availability_calls || 0) : null;
  const subs = (q?.lines || []).filter((l) => l.substitute_for);
  const gaps = (q?.uncovered || []).filter((g) => g.uncovered > 0);
  const skipped = q?.alternatives_considered || [];
  const notified = events.filter((e) => e.event === "notification.sent");
  const step = (n, title, when_, who, body) => `<li class="flow-step">
      <span class="flow-n">${n}</span>
      <div><div class="row"><b>${title}</b><span class="mono muted">${when_ ? when(when_) : ""}</span></div>
        ${who ? `<div class="small"><span class="actor ${who[0]}">${who[0]}</span> ${esc(who[1])}</div>` : ""}
        <div class="small">${body}</div></div></li>`;

  const why = null;  // shown via claimBlock in the order panel (labelled, with the suspicious flag)
  const request = step(1, "Request", search?.ts || q?.issued_at, ["agent", o.agent_id],
    `${why ? `<span class="rationale">“${esc(why)}”</span> ` : ""}Needs delivery by ${dateOnly(q?.requested_delivery_date || o.delivery_date)}
     ${search ? ` · searched ${searches.map((s) => `“${esc(s.data.query)}”`).join(", ")}${search.data.claims?.length ? ` with ${esc(search.data.claims.join(", "))}` : ""}${search.data.avoid_allergens?.length ? `, avoiding ${esc(search.data.avoid_allergens.map((c) => allergenNames?.[c] || c).join(", "))}` : ""}
       → ${found.size} products from the static catalog, 0 Valio API calls`
       : !subs.length && q ? ` · quoted ${(q.lines || []).map((l) => `<span class="mono">${esc(l.product_id)}</span>`).join(", ")} (no catalog search by this agent on record)` : ""}
     ${subs.length ? ` · asked to replace ${subs.map((l) => `<span class="mono">${esc(l.substitute_for)}</span>`).join(", ")} with a substitute` : ""}`);

  const lookup = step(2, "Lookup", q?.issued_at, ["system", "Sous gateway"],
    q?.seeded ? `<span class="claim-warn" style="color:var(--ink-80)">Sandbox example: ${esc(q.seeded.note || "priced from reference prices stored in the gateway, not a live Valio Aimo quote.")}</span>
      Signed quote <span class="mono">${esc(q.id)}</span>, ${eur(q.total_ex_vat)} ex VAT.`
    : q ? `Live price and availability${calls != null ? ` (${calls ? `${calls} Valio API call${calls === 1 ? "" : "s"}` : "from the gateway's short-lived cache, 0 Valio API calls"})` : ""} →
      signed quote <span class="mono">${esc(q.id)}</span>, orderable total ${eur(q.total_ex_vat)} ex VAT${q.indicative_total_ex_vat != null && q.indicative_total_ex_vat !== q.total_ex_vat ? ` (indicative ${eur(q.indicative_total_ex_vat)} incl. lines that can't be ordered)` : ""}, valid until ${when(q.expires_at)}.
      ${(q.lines || []).map((l) => `Chose ${esc(l.quantity)} × ${esc(pname(l.name))}${l.logistic_model ? ` (${esc(l.logistic_model)})` : ""}.`).join(" ")}
      ${skipped.length ? `Passed over ${skipped.length}: ${skipped.slice(0, 2).map((s) => `${esc(pname(s.name))} (${esc(((s.why_not || [])[0] || "").replace(/\.$/, ""))})`).join("; ")}${skipped.length > 2 ? ` and ${skipped.length - 2} more` : ""}.` : ""}
      ${gaps.length ? `<span style="color:var(--ask)">Could not cover ${gaps.map((g) => `${esc(g.uncovered)} ${esc(g.unit || "")}${g.item?.note ? ` of “${esc(g.item.note)}”` : ""}`).join(", ")}: nothing among its candidates can be delivered in time.</span>` : ""}`
      : `<span class="muted">Quote ${esc(o.quote_id)} not found.</span>`);

  const fired = (d.findings || []).filter((f) => f.outcome !== "silent" && f.rule !== "mandate.ok");
  const order = step(3, "Order", o.created_at, ["agent", o.agent_id],
    `${d.outcome === "block" ? "Tried to order" : "Committed to"} the quote at its fixed price, ${eur(o.total_ex_vat)} ex VAT. The gateway re-checked the quote's
     signature and cut-offs, then the mandate engine ruled ${tier(d.outcome)}${fired.length
       ? `: ${fired.map((f) => msg(f.message)).join(" ")}` : " (all checks passed)."}`);

  let appr;
  if (d.outcome === "block") {
    appr = step(4, "Approval", d.at, ["system", "mandate engine"], `Refused outright: this is outside what any human can approve under the mandate. Nothing was ordered.`);
  } else if (a) {
    appr = step(4, "Approval", a.at, ["human", a.by],
      `${a.approved ? "Approved" : "Rejected"}${a.note ? `: “${esc(a.note)}”` : ""} → ${tier(o.status)}${a.result_reason && a.result_reason !== a.note ? ` ${esc(a.result_reason)}` : ""}`);
  } else if (o.status === "pending_approval") {
    appr = step(4, "Approval", null, ["human", o.buyer],
      `<span style="color:var(--ask)">Waiting for a human at ${esc(bn(o.buyer))}.</span>${notified.length ? " Approval request sent." : ""}`);
  } else {
    appr = step(4, "Approval", d.at, ["system", "mandate engine"],
      `Within the mandate, so the agent decided alone; no human sign-off needed.${d.outcome === "notify" ? " The buyer was sent an FYI." : ""}`);
  }
  return request + lookup + order + appr;
}

// MariaDB system-versioned tables: the mandate exactly as it was when the order was decided.
function asOfSection(o, h) {
  if (!h) return "";
  const versions = (h.versions || []).map((v) => `<li>${tier(v.status)} <span class="mono muted">${when(v.valid_from)} → ${v.valid_to ? when(v.valid_to) : "now"}</span></li>`).join("");
  if (h.backend !== "mariadb" || !h.mandate_at_decision) {
    return `<h3>Mandate at decision time</h3><p class="small muted">${esc(h.note || "Point-in-time history needs the MariaDB backend.")}</p>`;
  }
  const m = h.mandate_at_decision;
  const changed = h.mandate_changed_since_decision || [];
  const allergens = (m.avoid_allergens || []).map((c) => allergenNames?.[c] || c).join(", ") || "none";
  return `<h3>Mandate at decision time</h3>
    <p class="small muted">Read from MariaDB's system-versioned history, not the current mandate, so a later edit can't
      rewrite why this order was decided the way it was.</p>
    <div class="rules small">
      <div><span class="muted">Agent alone up to</span><span>${eur(m.auto_approve_limit_eur)}</span></div>
      <div><span class="muted">Refuse above</span><span>${m.hard_limit_eur != null ? eur(m.hard_limit_eur) : "–"}</span></div>
      <div><span class="muted">Avoid allergens</span><span>${esc(allergens)}</span></div>
      <div><span class="muted">Substitutes</span><span>${tier(m.substitutions)}</span></div>
      <div><span class="muted">Revoked</span><span>${m.revoked ? "yes" : "no"}</span></div>
      <div><span class="muted">Since then</span><span>${changed.length
        ? `<span style="color:var(--ask)">changed: ${esc(changed.join(", "))}</span>` : "unchanged"}</span></div>
    </div>
    ${versions ? `<p class="small" style="margin-bottom:4px">Order versions</p><ul class="versions">${versions}</ul>` : ""}
    <details><summary>Query</summary><pre>${esc(h.query || "")}</pre></details>`;
}

// Good / better / best substitutes for one order line (live price and availability for a shortlist).
async function findSubstitutes(btn, order) {
  const out = btn.closest(".sub-line").querySelector(".sub-out");
  btn.disabled = true;
  btn.textContent = "Searching…";
  const date = (btn.dataset.date || "").slice(0, 10) || new Date(Date.now() + 2 * 864e5).toISOString().slice(0, 10);
  const qs = new URLSearchParams({
    delivery_date: date, amount: btn.dataset.qty || "1", unit: "SALES_UNIT", mandate_id: order.mandate_id,
    agent_id: "valio-ui:" + (approver() || "anonymous"), limit: "3",
  });
  try {
    allergenNames ||= await api("/allergens").then((x) => x.codes).catch(() => ({}));
    const r = await api(`/products/${encodeURIComponent(btn.dataset.subs)}/substitutes?${qs}`);
    const avoid = (r.constraints?.avoid_allergens || []).map((c) => allergenNames?.[c] || c).join(", ");
    const calls = r.valio_api_calls ? (r.valio_api_calls.price_calls || 0) + (r.valio_api_calls.availability_calls || 0) : null;
    out.innerHTML = `
      <p class="small muted" style="margin:10px 0 0">
        ${(r.original?.why_substitute || []).filter((w) => !w.startsWith("(")).map((w) => esc(w) + " · ").join("")}
        ${r.considered ? `${r.considered.in_category} in category, ${r.considered.priced_live} priced live` : ""}
        ${avoid ? ` · must avoid ${esc(avoid)}` : ""}${calls != null ? ` · ${calls} Valio API call(s)` : ""}</p>
      ${r.options?.length ? `<div class="subs">${r.options.map(subCard).join("")}</div>
        <p class="small muted">${esc(r.next_step || "")}</p>`
        : `<p class="small" style="color:var(--block)">${esc(r.no_safe_substitute || "No safe substitute found.")}</p>`}`;
    btn.textContent = "Search again";
  } catch (e) {
    out.innerHTML = `<p class="small" style="color:var(--block)">${esc(e.message)}</p>`;
    btn.textContent = "Try again";
  }
  btn.disabled = false;
}

const MATCH_LABEL = { same: "same type", compatible: "compatible type", unknown: "type unknown", different: "different type" };

function subCard(s) {
  const o = s.offer || {};
  const cp = o.comparison_price || {};
  const pc = s.price_change;
  const allergen = s.allergen_check?.status;
  return `<div class="sub">
    <div class="row" style="align-items:center">${tier(s.tier)}${s.official_substitute ? `<span class="mono muted">official</span>` : ""}</div>
    <div class="nm">${esc(pname(s.name))}</div>
    ${s.type?.label ? `<div class="small">${esc(s.type.label)} <span class="match m-${esc(s.type.match)}">${esc(MATCH_LABEL[s.type.match] || s.type.match || "")}</span></div>` : ""}
    <div class="muted mono">${esc(s.product_id)}${s.brand ? " · " + esc(s.brand) : ""}${o.logistic_model ? " · " + esc(o.logistic_model) : ""}</div>
    <div class="price">${cp.ex_vat != null ? `${eur(cp.ex_vat)} / ${esc(cp.per)}` : eur(o.unit_price_ex_vat)}
      ${pc != null ? `<span class="muted">(${pc > 0 ? "+" : ""}${Math.round(pc * 100)} %)</span>` : ""}
      ${o.delivery_date ? `<span class="muted"> · ${dateOnly(o.delivery_date)}</span>` : ""}</div>
    ${s.compromises?.length ? `<ul>${s.compromises.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : `<div class="small muted">No compromises</div>`}
    ${allergen && allergen !== "safe" && allergen !== "unknown" ? `<div class="small" style="color:var(--ask)">Allergen check: ${esc(allergen)}</div>` : ""}
  </div>`;
}

// Audit log: the shared, hash-chained record.
views.audit = {
  q: "",
  key: "",
  async load() {
    const params = new URLSearchParams({ limit: "200" });
    if (this.q) params.set("subject", this.q);
    const [{ events: raw }, chain] = await Promise.all([api("/audit?" + params), api("/audit/verify")]);
    // A search for a specific id always shows it, QA or not.
    const events = this.q ? raw : noQA(raw, isQAEvent);
    const hiddenQA = raw.length - events.length;
    const key = this.q + (events[0]?.seq ?? "") + chain.ok;
    if (key === this.key && view.dataset.view === "audit") return;
    this.key = key;
    const focused = document.activeElement?.id === "audit-q";
    view.dataset.view = "audit";
    view.innerHTML = `<div class="head"><div><h2>Audit log</h2>
        <div class="muted">One record for the buyer and Valio Aimo. Each entry includes the hash of the one before it.${hiddenQA ? ` ${hiddenQA} QA test event(s) hidden from this list (still in the chain).` : ""}</div></div></div>
      ${chain.ok
        ? `<div class="chain ok">✔ <div><b>Chain intact</b><div class="small">${chain.events} events verified · head <span class="mono">${esc((chain.head || "").slice(0, 16))}…</span>${versioning(chain)}</div></div></div>`
        : `<div class="chain bad">✖ <div><b>Chain broken at event #${esc(chain.broken_at_seq)}</b><div class="small">An entry was edited or deleted after it was written.</div></div></div>`}
      <input id="audit-q" type="search" placeholder="Filter by order, quote or mandate id (e.g. PO-…, M-HOTEL-AURORA)" value="${esc(this.q)}">
      <div class="list" style="margin-top:12px">${events.length ? events.map(eventRow).join("") : `<div class="empty">No events.</div>`}</div>`;
    const input = $("#audit-q");
    if (focused) { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
    let t;
    input.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(() => { this.q = input.value.trim(); this.key = ""; this.load(); }, 300);
    });
    view.querySelectorAll("[data-subject]").forEach((a) => a.addEventListener("click", (e) => {
      e.preventDefault(); this.q = a.dataset.subject; this.key = ""; this.load();
    }));
  },
};

// MariaDB keeps every past version of the audit rows (system-versioned table); the gateway
// checks that none was ever changed or deleted. Shown only when the backend reports it.
function versioning(chain) {
  const sv = chain.system_versioning;
  if (!sv) return "";
  const n = sv.rows_changed_or_deleted || 0;
  return ` · ${esc(chain.backend || "database")} system versioning: ${n ? `<b style="color:var(--block)">${n} row(s) changed or deleted (#${esc((sv.seqs || []).join(", #"))})</b>` : "no row ever changed or deleted"}`;
}

const EVENT_LABEL = {
  "mandate.issued": "Mandate issued",
  "mandate.revoked": "Mandate revoked",
  "catalog.search": "Catalog searched",
  "quote.issued": "Signed quote issued",
  "order.ordered": "Order placed",
  "order.pending_approval": "Order held for a human",
  "order.refused": "Order refused",
  "order.rejected": "Order attempt rejected",
  "order.approved": "Approved by a human",
  "order.rejected_by_human": "Rejected by a human",
  "order.erp_simulated": "Handed to the order system (simulated)",
  "order.erp_sent": "Sent to Valio Aimo's order system",
  "order.erp_confirmed": "Confirmed by Valio Aimo's order system",
  "order.erp_failed": "Order system hand-off failed (will retry)",
  "order.erp_gave_up": "Order system hand-off gave up",
  "order.erp_reconciled": "Order system hand-off reconciled",
  "substitutes.suggested": "Substitutes suggested",
  "notification.sent": "Notification sent",
  "notification.failed": "Notification failed",
  "standing.created": "Standing order created",
  "standing.paused": "Standing order paused",
  "standing.resumed": "Standing order resumed",
  "standing.renewal_checked": "Standing order checked for drift",
  "standing.renewed": "Standing order renewed",
  "standing.history_simulated": "Standing order history simulated (demo)",
  "suggestions.offered": "Valio suggestions offered (sponsored)",
  "agent.credential_issued": "Agent credential issued",
  "agent.credential_rotated": "Agent credential rotated",
  "agent.credential_revoked": "Agent credential revoked",
  "agent.auth_failed": "Agent failed to authenticate",
};
const NOTIFY_WHAT = { "order.notify": "FYI to the buyer", "order.ask": "approval request to the buyer",
  "order.decided": "decision to the agent", test: "test" };

function eventSummary(e) {
  const d = e.data || {};
  switch (e.event) {
    case "catalog.search":
      return `“${esc(d.query)}” → ${(d.results || []).length} products${d.avoid_allergens?.length ? ", avoiding " + esc(d.avoid_allergens.join(", ")) : ""} · 0 Valio API calls`;
    case "quote.issued": {
      // Only price + availability calls reach Valio Aimo; cache hits are served by the gateway.
      const v = d.valio_api_calls;
      const calls = v ? (Number(v.price_calls) || 0) + (Number(v.availability_calls) || 0) : null;
      const hits = v ? Number(v.cache_hits) || 0 : 0;
      return `${(d.lines || []).length} line(s), ${eur(d.total_ex_vat)} ex VAT${d.preview ? " · mandate preview " + tier(d.preview) : ""}${calls != null ? ` · ${calls} Valio API call(s)${hits ? `, ${hits} from cache` : ""}` : ""}`;
    }
    case "order.ordered": case "order.pending_approval": case "order.refused":
      return `${eur(d.total_ex_vat)} ${tier(d.outcome)} ${(d.fired || []).map((f) => msg(f.message)).join(" · ")}`;
    case "order.approved": case "order.rejected_by_human":
      return `→ ${tier(d.resulting_status)}${d.note ? ` “${esc(d.note)}”` : ""}${d.reason && d.reason !== d.note ? " · " + esc(d.reason) : ""}`;
    case "order.rejected":
      return esc(d.reason || "");
    case "notification.sent": case "notification.failed":
      return `${esc(NOTIFY_WHAT[d.event] || d.event)} via ${esc(d.channel)}${d.status_code ? ` (HTTP ${esc(d.status_code)})` : ""}${d.error ? " · " + esc(d.error) : ""}`;
    case "standing.created": {
      const so = d.standing_order || {};
      return `${esc(so.name || so.id || "")}${so.items ? ` · ${so.items.length} item(s)` : ""}`;
    }
    case "standing.renewal_checked":
      return `${eur(d.total_ex_vat)} for ${dateOnly(d.delivery_date)} · ${(d.drift || []).length
        ? `<span style="color:var(--ask)">drift: ${(d.drift || []).map((x) => msg(x.message || x.rule)).join(" · ")}</span>` : "no drift"}`;
    case "standing.renewed":
      return `→ <span class="mono">${esc(d.order_id)}</span> ${tier(d.status)}${(d.drift_rules || []).length ? ` · ${esc(d.drift_rules.join(", "))}` : ""}`;
    case "standing.history_simulated":
      return `${(d.weeks || []).length} past week(s), marked simulated`;
    case "suggestions.offered":
      return `to ${esc(d.agent_id)}: ${(d.suggestions || []).map((s) => `${esc((s.kind || "").replace(/_/g, " "))} <span class="mono">${esc(s.product_id)}</span>`).join(", ") || "none"}`;
    case "substitutes.suggested":
      return (d.options || []).length
        ? (d.options || []).map((x) => `${tier(x.tier)} <span class="mono">${esc(x.product_id)}</span>`).join(" ")
        : esc(d.no_safe_substitute || "no safe substitute");
    case "mandate.issued": {
      const m = d.mandate || {};
      return `${esc(bn(m.buyer))} → agent ${esc(m.agent_id)} · alone up to ${eur(m.auto_approve_limit_eur)}`;
    }
    default:
      return "";
  }
}

function eventRow(e) {
  return `<div class="ev">
    <div class="seq">#${esc(e.seq)}</div>
    <div>
      <div class="row"><span class="what">${esc(evLabel(e.event))}</span><span class="muted small">${when(e.ts)}</span></div>
      <div class="small"><span class="actor ${esc(e.actor_type)}">${esc(e.actor_type)}</span> ${esc(e.actor)}
        ${e.subject ? ` · <a href="#" class="mono" data-subject="${esc(e.subject)}">${esc(e.subject)}</a>` : ""}
        ${e.buyer ? ` · <span class="muted">${esc(bn(e.buyer))}</span>` : ""}</div>
      <div class="small" style="margin-top:2px">${eventSummary(e)}</div>
      <details><summary>Data and hashes</summary><pre>${esc(JSON.stringify(e.data, null, 2))}\n\nprev_hash ${esc(e.prev_hash)}\nhash      ${esc(e.hash)}</pre></details>
    </div>
  </div>`;
}

// Mandates: what each agent may do alone.
let allergenNames = null;
views.mandates = {
  key: "",
  async load() {
    await loadAuthGlobal();
    const mandates = noQA((await api("/mandates")).mandates, isQAMandate);
    allergenNames ||= await api("/allergens").then((r) => r.codes).catch(() => ({}));
    const key = mandates.map((m) => m.id + m.revoked).join();
    if (key === this.key && view.dataset.view === "mandates") return;
    this.key = key;
    view.dataset.view = "mandates";
    view.innerHTML = `<div class="head"><div><h2>Mandates</h2>
      <div class="muted">A mandate (valtakirja) is what a human lets their purchasing agent do. The gateway signs it and enforces it on every order.</div></div>
      <a class="btn plain" href="sign-mandate.html${SANDBOX ? "?sandbox=" + encodeURIComponent(SANDBOX) : ""}">Sign a mandate with your own key →</a></div>
      ${mandates.length ? mandates.map(mandateCard).join("") : `<div class="empty">No mandates issued yet.</div>`}`;
    view.querySelectorAll("[data-revoke]").forEach((b) => b.addEventListener("click", async () => {
      const id = b.dataset.revoke;
      const by = approver() || (prompt("Your name, for the audit log:") || "").trim();
      if (!by) return;
      if (!confirm(`Revoke ${id}? The agent can't place orders with it any more.`)) return;
      try {
        await api(`/mandates/${encodeURIComponent(id)}/revoke`, { method: "POST", body: { by } });
        toast(`${id} revoked.`);
        this.key = ""; this.load();
      } catch (e) { toast("Failed: " + e.message, 6000); }
    }));
    view.querySelectorAll("[data-suggest]").forEach((b) => b.addEventListener("click", () => showSuggestions(b)));
    if (SIDE === "buyer") mandates.filter((m) => !m.revoked).forEach((m) => loadRamp(m.id, () => { this.key = ""; this.load(); }));
    view.querySelectorAll("[data-sig]").forEach((b) => b.addEventListener("click", async () => {
      const id = b.dataset.sig;
      const [{ jws }, rep] = await Promise.all([api("/mandates/" + encodeURIComponent(id)),
        api(`/mandates/${encodeURIComponent(id)}/verify`).catch(() => null)]);
      const r = await verifyJws(jws);  // the gateway's signature, checked here in the browser
      const bs = rep?.buyer_signature;
      const parts = [`<span class="sig ${r.ok ? "ok" : "bad"}">${r.ok ? "✔" : "✖"} gateway: ${esc(r.why)}</span>`];
      if (bs) parts.push(`<span class="sig ${bs.ok ? "ok" : "bad"}">${bs.ok ? "✔" : "✖"} buyer's own signature (W3C VC, ${esc(bs.alg || "")})${
        rep.issuer_authorised?.ok ? `, key ${esc(rep.issuer_authorised.how || "registered")}` : ", key not authorised"}${bs.error ? ": " + esc(bs.error) : ""}</span>`);
      if (rep) parts.push(`<span class="sig ${rep.verdict === "valid" ? "ok" : "bad"}">verdict: ${esc(rep.verdict.replace(/_/g, " "))}</span>`);
      b.outerHTML = parts.join(" ");
    }));
  },
};

// "Valio suggests": Valio Aimo's own commercial offers to this buyer's agent (sponsored, labelled as such),
// each pre-checked against the mandate. A preview of what the agent receives; nothing is ordered.
const KIND_LABEL = {
  cheaper_alternative: "Cheaper alternative", certified_alternative: "Certified alternative",
  more_reliable_alternative: "More reliable", lower_co2_alternative: "Lower CO₂", seasonal: "Seasonal", new: "New",
};

function nextMondayISO() {
  const d = new Date();
  d.setDate(d.getDate() + (((8 - d.getDay()) % 7) || 7));
  return d.toISOString().slice(0, 10);
}

async function showSuggestions(btn) {
  const out = btn.closest(".card").querySelector(".sugs-out");
  btn.disabled = true;
  btn.textContent = "Asking Valio…";
  try {
    const r = await api(`/mandates/${encodeURIComponent(btn.dataset.suggest)}/suggestions?` + new URLSearchParams({
      delivery_date: nextMondayISO(), agent_id: btn.dataset.agent, limit: "4" }));
    const calls = r.valio_api_calls ? (r.valio_api_calls.price_calls || 0) + (r.valio_api_calls.availability_calls || 0) : null;
    const excluded = Object.entries(r.considered?.excluded || {}).filter(([, n]) => n);
    out.innerHTML = `<h3>Valio suggests <span class="match m-compatible">sponsored by ${esc(r.sponsored_by || "Valio Aimo")}</span></h3>
      <p class="small muted">${esc(r.disclosure || "")} Preview of what ${esc(btn.dataset.agent)} would see for delivery
        ${dateOnly(nextMondayISO())}.${calls != null ? ` ${calls} Valio API call(s).` : ""}</p>
      ${(r.suggestions || []).length ? `<div class="subs">${r.suggestions.map(sugCard).join("")}</div>`
        : `<p class="small muted">Nothing to suggest that this mandate would allow.</p>`}
      ${excluded.length ? `<p class="small muted">Left out before showing: ${excluded.map(([k, n]) => `${esc(n)} ${esc(k.replace(/_/g, " "))}`).join(", ")}.</p>` : ""}`;
    btn.textContent = "Refresh suggestions";
  } catch (e) {
    out.innerHTML = `<p class="small" style="color:var(--block)">${esc(e.message)}</p>`;
    btn.textContent = "Valio suggests…";
  }
  btn.disabled = false;
}

function sugCard(s) {
  const p = s.product || {};
  const o = s.offer || {};
  const cp = o.comparison_price || {};
  return `<div class="sub">
    <div class="row" style="align-items:center"><span class="mono small">${esc(KIND_LABEL[s.kind] || s.kind)}</span>
      ${s.mandate_check?.outcome ? tier(s.mandate_check.outcome) : ""}</div>
    <div class="nm">${esc(pname(p.name || p.id))}</div>
    <div class="muted mono">${esc(p.id)}${o.logistic_model ? " · " + esc(o.logistic_model) : ""}</div>
    ${s.instead_of ? `<div class="small">instead of ${esc(s.instead_of.name)}</div>` : ""}
    ${cp.ex_vat != null ? `<div class="price">${eur(cp.ex_vat)} / ${esc(cp.per)}${o.delivery_date ? `<span class="muted"> · ${dateOnly(o.delivery_date)}</span>` : ""}</div>` : ""}
    <div class="small muted">${msg(s.reason || "")}</div>
  </div>`;
}

function mandateCard(m) {
  const a = m.auto_approve_limit_eur, n = m.notify_above_eur, h = m.hard_limit_eur;
  const ladder = [
    `<div class="l-silent">Auto ≤ ${eur(n != null && n < a ? n : a)}</div>`,
    n != null && n < a ? `<div class="l-notify">Auto + FYI ≤ ${eur(a)}</div>` : "",
    `<div class="l-ask">Needs approval ${h != null ? "≤ " + eur(h) : "above"}</div>`,
    h != null ? `<div class="l-block">Refused &gt; ${eur(h)}</div>` : "",
  ].join("");
  const allergens = (m.avoid_allergens || []).map((c) => (allergenNames?.[c] || c)).join(", ");
  const rule = (k, v) => `<div><span class="muted">${k}</span><span>${v}</span></div>`;
  return `<article class="card ${m.revoked ? "revoked" : ""}">
    <div class="row"><div><div class="muted small mono">${esc(m.id)}</div><h2>${esc(bn(m.buyer))}</h2></div>
      <div style="text-align:right">${m.revoked ? `<span class="state off">Revoked</span>` : `<span class="state">Active</span>`}
        <div class="small" style="margin-top:4px">${m.issuer?.kind === "buyer"
          ? `<span class="match m-same" title="${esc(m.issuer.did || "")}">buyer-signed</span>`
          : `<span class="match">signed by the gateway</span>`}</div></div></div>
    <div class="small">Agent <span class="mono">${esc(m.agent_id)}</span> · issued by ${esc(m.issued_by)}${m.valid_until ? " · valid until " + when(m.valid_until) : ""}</div>
    <div class="ladder">${ladder}</div>
    <div class="muted small">Per order, ex VAT${m.daily_limit_eur != null ? ` · daily limit ${eur(m.daily_limit_eur)} before a human must approve` : ""}</div>
    <div class="rules small">
      ${rule("Avoid allergens", esc(allergens || "none") + (allergens ? (m.allow_may_contain ? " (may-contain OK)" : " (incl. may-contain)") : ""))}
      ${rule("No allergen data", tier(m.unknown_allergens))}
      ${rule("Substitutes", tier(m.substitutions))}
      ${rule("Supplier-shipped (stock unknown)", tier(m.terminal_products))}
      ${rule("Later delivery date", tier(m.later_delivery))}
      ${rule("Min. delivery reliability", m.min_delivery_reliability != null ? Math.round(m.min_delivery_reliability * 100) + " %" : "–")}
      ${(m.required_claims || []).length ? rule("Required claims", esc(m.required_claims.join(", "))) : ""}
      ${Object.entries(m.max_comparison_price_eur || {}).map(([u, v]) => rule("Price cap", `${eur(v)} / ${esc(u)}`)).join("")}
      ${Object.entries(m.comparison_price_caps || {}).map(([k, v]) => rule(`Price cap: ${esc(k)}`, `${eur(v)} / L or kg`)).join("")}
      ${m.allowed_categories ? rule("Categories", esc(m.allowed_categories.join(", "))) : ""}
      ${rule("Agent must authenticate", authRequired(m) ? `<span>Bearer token${!m.require_agent_auth ? " (gateway-wide)" : ""}</span>` : `<span class="muted">no (agent id only)</span>`)}
    </div>
    <div class="actions">
      <button class="btn plain" data-sig="${esc(m.id)}">🔏 Verify signature</button>
      <a class="btn plain" href="#/audit" onclick="views.audit.q='${esc(m.id)}';views.audit.key=''">History</a>
      ${m.revoked ? "" : `<button class="btn plain" data-suggest="${esc(m.id)}" data-agent="${esc(m.agent_id)}">Valio suggests…</button>`}
      ${m.revoked ? "" : `<button class="btn plain" data-revoke="${esc(m.id)}" style="color:var(--block)">Revoke</button>`}
    </div>
    <div class="sugs-out"></div>
    ${SIDE === "buyer" && !m.revoked ? `<div class="ramp-out" data-ramp="${esc(m.id)}"></div>` : ""}
  </article>`;
}

// Earned autonomy (sous/ramp.py), buyer side only: Sous proposes delegating a scope (ask -> notify) after a clean
// approval record, or taking it back after a rejection/edit. Nothing changes until the buyer's human accepts.
// Shown in English and Finnish: the gateway sends both.
async function loadRamp(id, reload) {
  const box = view.querySelector(`.ramp-out[data-ramp="${CSS.escape(id)}"]`);
  if (!box) return;
  let r;
  try { r = await api(`/mandates/${encodeURIComponent(id)}/autonomy`); } catch { return; }
  const props = r.proposals || [], del = r.delegated || [];
  if (!props.length && !del.length) return;
  const pol = r.policy || {};
  box.innerHTML = `<h3>Delegate more? <span class="muted small">· Delegoidaanko lisää?</span></h3>
    <p class="small muted">Sous proposes only after ${esc(pol.min_approvals_unchanged)} held orders of a kind were approved
      unchanged over ${esc(pol.min_distinct_weeks)}+ weeks with none edited or rejected; never allergens, block rules or
      hard limits. · Sous ehdottaa vasta, kun ${esc(pol.min_approvals_unchanged)} samanlaista pidätettyä tilausta on
      hyväksytty muuttamatta vähintään ${esc(pol.min_distinct_weeks)} eri viikolla; ei koskaan allergeeneja, estoja tai ehdottomia rajoja.</p>
    ${props.map((p) => `<div class="sub" data-prop="${esc(p.id)}">
      <div class="row" style="align-items:center"><span class="mono small">${p.kind === "revert" ? "Take back · Peru" : "Delegate · Delegoi"}</span>
        <span>${tier(p.from_tier)} → ${tier(p.to_tier)}</span></div>
      <div class="small">${esc(p.evidence)}</div>
      <div class="small muted" lang="fi">${esc(p.evidence_fi)}</div>
      <div class="actions"><button class="btn" data-ramp-do="accept">Accept · Hyväksy</button>
        <button class="btn plain" data-ramp-do="decline">Decline · Hylkää</button></div></div>`).join("")}
    ${del.length ? `<p class="small">Delegated · Delegoitu: ${del.map((d) => `${esc(d.label || d.scope)} (${esc(d.rule)}, ${dateOnly(d.since)})`).join(", ")}</p>` : ""}`;
  box.querySelectorAll("[data-ramp-do]").forEach((b) => b.addEventListener("click", async () => {
    const pid = b.closest("[data-prop]").dataset.prop, act = b.dataset.rampDo;
    const by = approver() || (prompt("Your name, for the audit log:") || "").trim();
    if (!by) return;
    try {
      await api(`/mandates/${encodeURIComponent(id)}/autonomy/${encodeURIComponent(pid)}/${act}`, { method: "POST", body: { by } });
      toast(act === "accept" ? "Mandate updated and re-signed." : "Declined; nothing changed.");
      reload();
    } catch (e) { toast("Failed: " + e.message, 6000); }
  }));
}

// Standing orders: recurring weekly orders renewed by the buyer's agent, with drift detection.
const DAY = { mon: "Monday", tue: "Tuesday", wed: "Wednesday", thu: "Thursday", fri: "Friday", sat: "Saturday", sun: "Sunday" };

views.standing = {
  key: "",
  async load() {
    const list = noQA((await api("/standing-orders")).standing_orders, isQAMandate);
    const details = await Promise.all(list.map((s) => api("/standing-orders/" + encodeURIComponent(s.id)).catch(() => null)));
    const key = details.map((d) => d && d.standing_order.id + d.standing_order.active + (d.renewals[0]?.status || "") + d.renewals.length).join();
    if (key === this.key && view.dataset.view === "standing") return;
    this.key = key;
    view.dataset.view = "standing";
    view.innerHTML = `<div class="head"><div><h2>Standing orders</h2>
      <div class="muted">Recurring weekly orders. Each week the buyer's agent renews them live; the gateway compares the
        new quote with the last four renewals and holds the order for a human when quantities, cost or products drift.</div></div></div>
      ${details.filter(Boolean).length ? `<div class="list">${details.filter(Boolean).map(standingRow).join("")}</div>`
        : `<div class="empty">No standing orders yet.<p class="small">Try <span class="mono">scripts/demo_standing.py</span>.</p></div>`}`;
    view.querySelectorAll("[data-so]").forEach((b) => b.addEventListener("click", () => { location.hash = "#/standing/" + b.dataset.so; }));
  },
};

function lastLive(d) { return d.renewals.find((r) => !r.simulated) || null; }

function standingRow(d) {
  const so = d.standing_order;
  const r = lastLive(d);
  const totals = [...d.renewals].reverse().map((x) => x.total_ex_vat);
  return `<button class="item" data-so="${esc(so.id)}">
    <div><b>${esc(so.name)}</b> <span class="muted mono small">${esc(so.id)}</span>
      <div class="muted small">${esc(bn(so.buyer))} · every ${DAY[so.cadence?.weekday] || esc(so.cadence?.weekday)} ·
        ${so.items.map((i) => `${esc(i.amount)} ${esc(i.unit)} ${esc(i.label || "")}`).join(", ")}</div>
      ${r ? `<div class="small" style="margin-top:4px">${tier(r.status)} ${msg(r.summary || "")}</div>` : `<div class="small muted">Not renewed yet · next ${dateOnly(d.next_delivery_date)}</div>`}</div>
    <div style="text-align:right">${so.active ? `<span class="state">Active</span>` : `<span class="state off">Paused</span>`}
      ${spark(totals, r && r.drift?.length ? totals.length - 1 : -1, "Weekly total")}</div>
  </button>`;
}

// Tiny bar trend: one bar per renewal, oldest first; the flagged bar uses the "ask" colour.
function spark(values, flagged = -1, label = "") {
  const vs = values.filter((v) => v != null);
  if (!vs.length) return "";
  const max = Math.max(...vs) || 1;
  return `<span class="spark" role="img" aria-label="${esc(label)}: ${vs.map((v) => esc(v)).join(", ")}">${vs.map((v, i) =>
    `<i style="height:${Math.max(8, (v / max) * 100)}%" class="${i === flagged ? "hot" : ""}" title="${esc(v)}"></i>`).join("")}</span>`;
}

async function openStanding(id) {
  const d = await api("/standing-orders/" + encodeURIComponent(id));
  const so = d.standing_order;
  const live = lastLive(d);
  const chrono = [...d.renewals].reverse();
  const flaggedLabels = new Set((live?.drift || []).map((f) => (f.message || "").split(":")[0]));
  openSheet(`
    <div class="muted small">${esc(bn(so.buyer))} · standing order</div>
    <h2>${esc(so.name)} ${so.active ? `<span class="state">Active</span>` : `<span class="state off">Paused</span>`}</h2>
    <dl class="kv small">
      <dt>Delivery</dt><dd>Every ${DAY[so.cadence?.weekday] || esc(so.cadence?.weekday)} · next ${dateOnly(d.next_delivery_date)}</dd>
      <dt>Agent</dt><dd class="mono">${esc(so.agent_id)}</dd>
      <dt>Mandate</dt><dd class="mono">${esc(so.mandate_id)}</dd>
      <dt>Drift rules</dt><dd>amount +${Math.round(d.rules.quantity_creep * 100)} % or up ${d.rules.trend_run} weeks in a row,
        cost or price per unit +${Math.round(d.rules.cost_drift * 100)} %, a switched product or new supply risk, each vs the
        median of the last ${d.rules.history_weeks} ordered renewals</dd>
    </dl>
    ${live ? `<h3>This week ${tier(live.status)}</h3>
      <p>${msg(live.summary || "")}</p>
      ${live.drift?.length ? findingsList(live.drift) : `<p class="small muted">No drift: renewed like the last weeks.</p>`}
      ${live.order_id ? `<p class="small"><a href="#/orders/${esc(live.order_id)}">Open order ${esc(live.order_id)}</a>${live.status === "pending_approval" ? " to approve or reject" : ""}</p>` : ""}`
      : `<p class="muted">Not renewed yet.</p>`}
    <h3>Week by week</h3>
    <table class="lines"><thead><tr><th>Item</th><th>Trend</th><th class="num">Median</th><th class="num">${live ? "This week" : "Usual"}</th></tr></thead><tbody>
      ${so.items.map((it, k) => {
        const amounts = chrono.map((r) => r.items?.[k]?.amount);
        const base = d.baseline.items?.[k] || {};
        const now = live?.items?.[k]?.amount ?? it.amount;
        const hot = flaggedLabels.has(it.label);
        const pct = base.median_amount ? Math.round((now / base.median_amount - 1) * 100) : null;
        return `<tr><td>${esc(it.label || it.product_ids[0])}</td>
          <td>${spark(amounts, hot && live ? amounts.length - 1 : -1, it.label + " per week")}</td>
          <td class="num">${base.median_amount != null ? `${esc(base.median_amount)} ${esc(it.unit)}` : "–"}</td>
          <td class="num" ${hot ? `style="color:var(--ask)"` : ""}>${esc(now)} ${esc(it.unit)}${pct ? ` <span class="small">(${pct > 0 ? "+" : ""}${pct} %)</span>` : ""}</td></tr>`;
      }).join("")}
      <tr><td>Total</td><td>${spark(chrono.map((r) => r.total_ex_vat), live?.drift?.some((f) => f.rule === "standing.cost_drift") ? chrono.length - 1 : -1, "weekly total")}</td>
        <td class="num">${eur(d.baseline.median_total_ex_vat)}</td><td class="num">${eur(live?.total_ex_vat)}</td></tr>
    </tbody></table>
    <h3>Renewals</h3>
    <div class="list">${d.renewals.map((r) => `
      <div class="ev"><div class="seq">${dateOnly(r.delivery_date)}</div><div>
        <div class="row"><span>${tier(r.status)} ${eur(r.total_ex_vat)}</span>
          ${r.simulated ? `<span class="mono muted small">simulated history</span>` : r.order_id ? `<a class="mono small" href="#/orders/${esc(r.order_id)}">${esc(r.order_id)}</a>` : ""}</div>
        <div class="small muted">${(r.items || []).map((i) => `${esc(i.amount)} ${esc(i.unit)} ${esc(i.label)}`).join(", ")}</div>
        ${r.drift?.length ? `<div class="small" style="color:var(--ask)">${r.drift.map((f) => esc(f.rule.replace("standing.", ""))).join(", ")}</div>` : ""}
      </div></div>`).join("") || `<div class="empty">No renewals yet.</div>`}</div>
    <p class="small muted">Simulated weeks are demo history: amounts made up, priced from one real signed quote. Live
      renewals always quote Valio Aimo's API.</p>
    <div class="actions">
      <button class="btn plain" data-toggle="${so.active ? "pause" : "resume"}">${so.active ? "Pause standing order" : "Resume standing order"}</button>
      <a class="btn plain" href="#/audit?subject=${esc(so.id)}">History in audit log</a>
    </div>
  `);
  $("#sheet-body [data-toggle]")?.addEventListener("click", async (e) => {
    const act = e.target.dataset.toggle;
    let by = approver();
    if (!by) { by = (prompt("Your name, for the audit log:") || "").trim(); if (!by) return; setApprover(by); }
    try {
      await api(`/standing-orders/${encodeURIComponent(so.id)}/${act}`, { method: "POST", body: { by } });
      toast(`${so.name} ${act === "pause" ? "paused" : "resumed"}.`);
      views.standing.key = "";
      openStanding(so.id);
      refresh();
    } catch (err) { toast("Failed: " + err.message, 6000); }
  });
}

// Today: one screen for a Valio Aimo operator (or a judge): what needs a human, what agents did today,
// what was stopped and why, standing-order drift, and whether the record can be trusted.
const helsinkiDay = (iso) => new Date(iso).toLocaleDateString("sv-SE", { timeZone: "Europe/Helsinki" });

views.today = {
  key: "",
  async load() {
    await loadAuthGlobal();
    const [ordersR, mandatesR, standingR, chain] = await Promise.all([
      api("/orders"), api("/mandates"), api("/standing-orders").catch(() => ({ standing_orders: [] })), api("/audit/verify")]);
    const orders = noQA(ordersR.orders, isQAOrder);
    const mandates = noQA(mandatesR.mandates, isQAMandate);
    const day = helsinkiDay(new Date().toISOString());
    const today = orders.filter((o) => helsinkiDay(o.created_at) === day);
    const pending = orders.filter((o) => o.status === "pending_approval");
    const ordered = today.filter((o) => o.status === "ordered");
    const stopped = today.filter((o) => o.status === "refused" || o.status === "rejected" || o.status === "expired");
    const standing = noQA(standingR.standing_orders || [], isQAMandate);
    const details = await Promise.all(standing.map((s) => api("/standing-orders/" + encodeURIComponent(s.id)).catch(() => null)));
    const drift = details.filter(Boolean).map((d) => ({ d, r: lastLive(d) })).filter(({ r }) => r && r.drift?.length);
    // Needs changed mid-contract (sous/needs.py): the latest re-check per mandate from the last 14 days
    const since = Date.now() - 14 * 864e5;
    const needs = (await Promise.all(mandates.filter((m) => !m.revoked).map((m) =>
      api(`/mandates/${encodeURIComponent(m.id)}/needs-reviews?limit=1`).then((r) => r.needs_reviews[0]).catch(() => null))))
      .filter((r) => r && r.change?.tightened?.length && Date.parse(r.created_at) > since);
    const key = [day, orders.length, pending.length, orders[0]?.updated_at, drift.length, chain.events, needs.map((r) => r.id).join()].join("|");
    if (key === this.key && view.dataset.view === "today") { tickDeadlines(); return; }
    this.key = key;
    view.dataset.view = "today";

    const firstReason = (o) => {
      const f = (o.decision?.findings || []).find((x) => x.outcome === "block") || (o.decision?.findings || []).find((x) => x.outcome === "ask");
      return o.status === "rejected" ? `Rejected by ${o.approval?.by || "a human"}${o.approval?.note ? `: “${o.approval.note}”` : ""}`
        : o.status === "expired" ? (o.approval?.result_reason || "Approved after the order cut-off") : (f?.message || "");
    };
    const reasons = (o) => (o.decision?.findings || []).filter((f) => f.outcome === "block" || f.outcome === "ask");
    // Minimal by default: one line per order; the reasons open in place, the full story in the order panel.
    const row = (o, sub = "") => `<details class="mrow">
        <summary>${ic(STATUS_ICON[o.status] || "receipt_long", "st-" + (TIER_STYLE[o.status] || "silent"))}
          <span class="mrow-main"><b>${esc(bn(o.buyer))}</b><span class="muted">${orderStory(o)}</span>${sub}</span>
          <span class="mrow-amt">${eur(o.total_ex_vat)}</span>${ic("expand_more", "chev")}</summary>
        <div class="mrow-more">
          ${o.status === "rejected" || o.status === "expired" ? `<p>${msg(firstReason(o))}</p>` : ""}
          ${reasons(o).length ? `<ul class="why">${reasons(o).slice(0, 4).map((f) => `<li>${msg(f.message)}</li>`).join("")}
            ${reasons(o).length > 4 ? `<li class="muted">+ ${reasons(o).length - 4} more</li>` : ""}</ul>` : ""}
          <a class="btn plain sm" href="#/orders/${esc(o.id)}">${o.status === "pending_approval" && SIDE === "buyer" ? "Review and decide" : "Open order"} ${ic("arrow_forward")}</a>
          <span class="muted mono small">${esc(o.id)}</span></div></details>`;
    const buyerSigned = mandates.filter((m) => m.issuer?.kind === "buyer").length;
    const authed = mandates.filter(authRequired).length;
    const heldDrift = drift.filter(({ r }) => r.status === "pending_approval").length;
    const spent = ordered.reduce((s, o) => s + (o.total_ex_vat || 0), 0);
    const stat = (icon, cls, value, label, href) => `<a class="mstat ${cls}" href="${href}">${ic(icon)}<b>${value}</b><span>${label}</span></a>`;

    view.innerHTML = `<div class="head"><h2>Today</h2>
        <span class="muted">${new Date().toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", timeZone: "Europe/Helsinki" })}</span></div>
      <div class="mstats">
        ${stat("pending_actions", pending.length ? "st-ask" : "", pending.length, SIDE === "buyer" ? "waiting for you" : "waiting for buyers", "#/inbox")}
        ${stat("check_circle", "", ordered.length, `ordered, ${eur(spent)}`, "#/orders")}
        ${stat("block", stopped.length ? "st-block" : "", stopped.length, "stopped", "#/orders")}
        ${stat("trending_up", heldDrift ? "st-ask" : "", heldDrift, "drift held", "#/standing")}
        ${stat(chain.ok ? "verified" : "gpp_bad", chain.ok ? "st-silent" : "st-block", chain.ok ? "Intact" : "Broken", "audit chain", "#/audit")}
      </div>
      ${needs.length ? `<section class="msec"><h3>Your needs changed</h3><div class="mlist">${needs.map((r) => `
        <details class="mrow"><summary>${ic("rule", r.held.length || r.flagged.length ? "st-ask" : "st-notify")}
          <span class="mrow-main"><b>${esc(bn(r.buyer))}</b><span class="muted">${esc(r.held.length)} held · ${esc(r.flagged.length)} to review · ${esc(r.revoked.length)} revoked · ${esc(r.unaffected.length)} still running</span></span>
          ${ic("expand_more", "chev")}</summary>
          <div class="mrow-more"><p>${msg(r.summary)}</p><p class="muted small" lang="fi">${esc(r.summary_fi)}</p>
            ${[...r.held, ...r.flagged, ...r.revoked].length ? `<ul class="why">${[...r.held, ...r.flagged, ...r.revoked].slice(0, 6).map((x) => `<li>${msg(x.findings[0].message)}</li>`).join("")}</ul>` : ""}
            ${r.substitutes.filter((s) => s.proposal).map((s) => `<p class="small">Proposed instead of ${esc(s.for_product_id)}: <b>${esc(s.proposal.name)}</b> (a proposal; your agent quotes it and your rules decide)</p>`).join("")}
            <a class="btn plain sm" href="#/standing">Standing orders ${ic("arrow_forward")}</a>
            <a class="btn plain sm" href="#/inbox">Approvals ${ic("arrow_forward")}</a>
            <span class="muted mono small">${esc(r.id)}</span></div></details>`).join("")}</div></section>` : ""}
      <section class="msec"><h3>${SIDE === "buyer" ? "Waiting for you" : "Waiting for buyers"}</h3>
        ${pending.length ? `<div class="mlist">${pending.slice(0, 6).map((o) => {
          const cut = earliestCutoff(o);
          return row(o, cut ? `<span class="mrow-due small">${ic("schedule")}<span class="deadline" data-until="${esc(cut)}"></span></span>` : "");
        }).join("")}</div>${pending.length > 6 ? `<p class="small"><a href="#/inbox">All ${pending.length} in Approvals</a></p>` : ""}`
          : `<p class="empty">${ic("done_all")} Nothing waiting</p>`}</section>
      <section class="msec"><h3>Stopped today</h3>
        ${stopped.length ? `<div class="mlist">${stopped.slice(0, 6).map((o) => row(o)).join("")}</div>`
          : `<p class="empty">${ic("done_all")} Nothing stopped</p>`}</section>
      ${drift.length ? `<section class="msec"><h3>Standing orders</h3><div class="mlist">${drift.map(({ d, r }) => `
        <details class="mrow"><summary>${ic("event_repeat", "st-" + (TIER_STYLE[r.status] || "silent"))}
          <span class="mrow-main"><b>${esc(d.standing_order.name)}</b><span class="muted">${esc(r.drift.length)} drift finding${r.drift.length > 1 ? "s" : ""}</span></span>
          ${tier(r.status)}${ic("expand_more", "chev")}</summary>
          <div class="mrow-more"><p>${msg(r.summary || "")}</p>
            <a class="btn plain sm" href="#/standing/${esc(d.standing_order.id)}">Open standing order ${ic("arrow_forward")}</a></div></details>`).join("")}</div></section>` : ""}
      <details class="mtrust"><summary>${ic("shield")} Why this record can be trusted ${ic("expand_more", "chev")}</summary>
        <div class="rules small" style="grid-template-columns:1fr">
          <div><span class="muted">Audit chain</span><span>${chain.ok ? "intact" : `broken at #${esc(chain.broken_at_seq)}`} · ${esc(chain.events)} events${versioning(chain)}</span></div>
          <div><span class="muted">Mandates in force</span><span>${mandates.filter((m) => !m.revoked).length}</span></div>
          <div><span class="muted">Signed by the buyer's own key</span><span>${buyerSigned}</span></div>
          <div><span class="muted">Agents must authenticate</span><span>${authed}</span></div>
          <div><span class="muted">Quotes, orders, receipts</span><span>Ed25519 signed · <a href="/.well-known/jwks.json">key</a></span></div>
        </div>
        <p class="small muted">Decisions are deterministic rules on signed quotes; no AI decides.</p></details>`;
    tickDeadlines();
  },
};

// Stats: what the gateway did, for Valio Aimo's operators (and the demo's numbers slide).
// Everything is computed from /orders and the audit log, so it's the same record anyone can check.
const CATALOG_SIZE = 17546;
const NAIVE_CALLS = Math.ceil(CATALOG_SIZE / 1000) * 2; // price + availability, 1000 ids per call
const RULE_GROUP = {
  allergen: "Allergen safety", spend: "Spend limits", availability: "Stock and cut-offs", supply: "Supply risk",
  delivery: "Delivery date", price: "Price", product: "Product requirements", substitution: "Substitutes",
  need: "Partial cover", mandate: "Mandate validity", order: "Empty order", standing: "Standing-order drift",
};

// Insights: a summary for Valio Aimo. Which of the stops are Valio's to fix (missing product data,
// stock, cut-offs, units) and which are buyers' own rules working as intended. Computed from /orders.
const VALIO_ISSUE = {
  "allergen.unknown": { icon: "help", label: "Missing allergen data", fix: "Add the allergen declaration to the product data" },
  "unit.interpreted": { icon: "straighten", label: "Unclear sales unit", fix: "Define the pack and piece units for the product" },
  "availability.out_of_stock": { icon: "inventory_2", label: "Out of stock", fix: "Restock or offer a substitute" },
  "availability.undeliverable": { icon: "local_shipping", label: "Not deliverable on any date", fix: "Fix the delivery schedule or delist" },
  "availability.cutoff_passed": { icon: "schedule", label: "Ordered after the cut-off", fix: "Stop reporting delivery dates whose cut-off has passed" },
  "supply.terminal": { icon: "warehouse", label: "Supplier stock unknown", fix: "Share the supplier warehouse's stock" },
  "price.changed": { icon: "sell", label: "Price changed after the quote", fix: "Keep prices stable for the quote's lifetime" },
};
views.insights = {
  key: "",
  async load() {
    const orders = noQA((await api("/orders")).orders, isQAOrder);
    const key = orders.length + "|" + orders[0]?.updated_at;
    if (key === this.key && view.dataset.view === "insights") return;
    this.key = key;
    view.dataset.view = "insights";

    const stops = (o) => (o.decision?.findings || []).filter((f) => f.outcome === "ask" || f.outcome === "block");
    const lineOf = (o, pid) => (o.lines || []).find((l) => l.product_id === pid);
    const held = orders.filter((o) => stops(o).length);
    const valioOrders = held.filter((o) => stops(o).some((f) => VALIO_ISSUE[f.rule]));
    const lost = orders.filter((o) => ["refused", "rejected", "expired"].includes(o.status));
    const lostValio = lost.filter((o) => stops(o).some((f) => VALIO_ISSUE[f.rule]));
    const sum = (os) => os.reduce((s, o) => s + (o.total_ex_vat || (o.lines || []).reduce((t, l) => t + (l.line_total_ex_vat || 0), 0)), 0);

    // per issue: orders and products; per product: issues, orders, value at stake
    const issues = {}, products = {};
    for (const o of held) for (const f of stops(o)) {
      const v = VALIO_ISSUE[f.rule];
      const key = v ? f.rule : "buyer";
      const it = issues[key] ||= { orders: new Set(), products: new Set(), example: f.message };
      it.orders.add(o.id);
      if (f.product_id) it.products.add(f.product_id);
      if (v && f.product_id) {
        const l = lineOf(o, f.product_id);
        const p = products[f.product_id] ||= { name: l?.name || f.product_id, rules: new Set(), orders: new Set(), buyers: new Set(), value: 0 };
        if (!p.orders.has(o.id)) p.value += l?.line_total_ex_vat || 0;
        p.rules.add(f.rule); p.orders.add(o.id); p.buyers.add(bn(o.buyer));
      }
    }
    const buyerRules = {};
    for (const o of held) for (const r of new Set(stops(o).filter((f) => !VALIO_ISSUE[f.rule]).map((f) => f.rule))) buyerRules[r] = (buyerRules[r] || 0) + 1;
    const ranked = Object.entries(issues).filter(([k]) => k !== "buyer").sort((a, b) => b[1].orders.size - a[1].orders.size);
    const top = Math.max(1, ...ranked.map(([, v]) => v.orders.size));
    const prods = Object.entries(products).sort((a, b) => b[1].orders.size - a[1].orders.size || b[1].value - a[1].value);
    const stat = (icon, cls, value, label) => `<div class="mstat ${cls}">${ic(icon)}<b>${value}</b><span>${label}</span></div>`;

    view.innerHTML = `<div class="head"><h2>Insights</h2><span class="muted">for Valio Aimo · from ${orders.length} agent orders</span></div>
      <div class="mstats four">
        ${stat("pan_tool", held.length ? "st-ask" : "", held.length, "orders held or refused")}
        ${stat("build", valioOrders.length ? "st-ask" : "", valioOrders.length, "of them Valio can fix")}
        ${stat("remove_shopping_cart", lostValio.length ? "st-block" : "", eur(sum(lostValio)), `lost to Valio-side issues (${lostValio.length} orders)`)}
        ${stat("inventory", "", prods.length, "products to fix")}
      </div>
      <section class="msec"><h3>Most common issues</h3>
        ${ranked.length ? `<div class="mlist">${ranked.map(([rule, v]) => { const d = VALIO_ISSUE[rule]; return `
          <details class="mrow"><summary>${ic(d.icon, "st-ask")}
            <span class="mrow-main"><b>${esc(d.label)}</b><span class="bar"><i style="width:${Math.round(100 * v.orders.size / top)}%"></i></span></span>
            <span class="mrow-amt">${v.orders.size} order${v.orders.size > 1 ? "s" : ""}</span>${ic("expand_more", "chev")}</summary>
            <div class="mrow-more"><p><b>Fix:</b> ${esc(d.fix)}.</p><p class="small muted">e.g. ${msg(v.example)}</p>
              <span class="small muted mono">${esc(rule)} · ${v.products.size} product${v.products.size === 1 ? "" : "s"}</span></div></details>`; }).join("")}</div>`
          : `<p class="empty">${ic("done_all")} No Valio-side issues</p>`}</section>
      <section class="msec"><h3>Products to fix</h3>
        ${prods.length ? `<div class="mlist">${prods.slice(0, 12).map(([pid, p]) => `
          <details class="mrow"><summary>${ic([...p.rules].map((r) => VALIO_ISSUE[r].icon)[0], "st-ask")}
            <span class="mrow-main"><b>${esc(pname(p.name))}</b><span class="muted">${[...p.rules].map((r) => esc(VALIO_ISSUE[r].label)).join(" · ")}</span></span>
            <span class="mrow-amt">${p.orders.size}×</span>${ic("expand_more", "chev")}</summary>
            <div class="mrow-more"><ul class="why">${[...p.rules].map((r) => `<li>${esc(VALIO_ISSUE[r].fix)}</li>`).join("")}</ul>
              <span class="small muted">${p.orders.size} order${p.orders.size > 1 ? "s" : ""} · ${eur(p.value)} at stake · ${esc([...p.buyers].join(", "))}</span>
              <a class="small mono" href="/products/${esc(pid)}">${esc(pid)}</a></div></details>`).join("")}</div>
          ${prods.length > 12 ? `<p class="small muted">+ ${prods.length - 12} more</p>` : ""}`
          : `<p class="empty">${ic("done_all")} Nothing to fix</p>`}</section>
      <details class="mtrust"><summary>${ic("gavel")} Stopped by buyers' own rules (working as intended) ${ic("expand_more", "chev")}</summary>
        <div class="rules small" style="grid-template-columns:1fr">${Object.entries(buyerRules).sort((a, b) => b[1] - a[1]).map(([r, n]) =>
          `<div><span class="muted mono">${esc(r)}</span><span>${n}</span></div>`).join("") || `<div><span class="muted">none</span></div>`}</div></details>`;
  },
};

// Valio's lost demand (GET /stats/lost-demand): what agents wanted and didn't get, and why.
function lostDemandCard(L, bar) {
  const C = L.causes || {};
  const rows = [["supply", "Valio couldn't supply", "b-block"], ["too_late", "Approved too late", "b-ask"],
                ["declined", "Buyer's human said no", "b-notify"], ["buyer", "Buyer's own rules refused", "b-ask"]];
  const max = Math.max(...rows.map(([k]) => C[k]?.value_ex_vat || 0), 1);
  const unit = (u) => (u === "SALES_UNIT" ? "packs" : u);
  const unc = (L.uncovered || []).slice(0, 6);
  const po = (L.passed_over || []).slice(0, 6);
  const poMax = Math.max(...po.map((p) => p.lines), 1);
  const top = (c) => (C[c]?.top_rules || []).map((r) => r.rule).join(", ");
  return `<div class="card">
    <h3 style="margin-top:0">Lost demand</h3>
    <div class="muted small">What buyers' agents wanted from Valio Aimo and didn't get: ${eur(L.lost_value_ex_vat || 0)} ex VAT
      in ${Object.values(C).reduce((s, c) => s + (c.orders || 0), 0)} orders${L.lost_share_pct != null ? ` (${L.lost_share_pct} % of all demand placed)` : ""}.
      Sandboxes and QA data left out.</div>
    ${rows.map(([k, label, cls]) => bar(label, C[k]?.value_ex_vat || 0, max,
        `${eur(C[k]?.value_ex_vat || 0)} <span class="muted small">(${C[k]?.orders || 0})</span>`,
        top(k) ? `Rules: ${top(k)}` : "", C[k]?.value_ex_vat ? cls : "")).join("")}
    <h4 style="margin:14px 0 4px">Asked for, but no product could cover it</h4>
    ${unc.length ? unc.map((u) => `<div class="small"><b>${esc(String(u.uncovered))} ${esc(unit(u.unit))}</b> of ${esc(u.item)}
        <span class="muted">· ${u.quotes} quote${u.quotes === 1 ? "" : "s"}</span></div>`).join("")
      : `<div class="muted small">Every quoted need was covered.</div>`}
    <h4 style="margin:14px 0 4px">Why products were passed over</h4>
    ${po.length ? po.map((p) => bar(esc(p.what), p.lines, poMax, String(p.lines),
        p.top_products.map((t) => t.name || t.product_id).join(", "), "b-block")).join("")
      : `<div class="muted small">No candidate was passed over.</div>`}
  </div>`;
}

views.stats = {
  key: "",
  async load() {
    const [{ orders: allOrders }, { events: allEvents }, chain, lost] = await Promise.all([
      api("/orders"), api("/audit?limit=5000")  /* 5000 = cap on older gateways (newer allow 20000) */, api("/audit/verify"),
      api("/stats/lost-demand?all_time=true").catch(() => null) /* older gateways: no lost-demand view */]);
    const orders = noQA(allOrders, isQAOrder);
    const events = noQA(allEvents, isQAEvent);
    const key = (events[0]?.seq ?? "") + "|" + orders.length;
    if (key === this.key && view.dataset.view === "stats") return;
    this.key = key;
    view.dataset.view = "stats";

    const n = orders.length;
    const byTier = { silent: 0, notify: 0, ask: 0, block: 0 };
    for (const o of orders) byTier[o.decision?.outcome] = (byTier[o.decision?.outcome] || 0) + 1;
    const auto = byTier.silent + byTier.notify;

    // Rules that held or stopped an order (ask/block findings), counted once per order.
    const rules = {};
    for (const o of orders) {
      const seen = new Set();
      for (const f of o.decision?.findings || []) {
        if (f.outcome !== "ask" && f.outcome !== "block") continue;
        if (seen.has(f.rule)) continue;
        seen.add(f.rule);
        const r = (rules[f.rule] ||= { rule: f.rule, ask: 0, block: 0, example: f.message });
        r[f.outcome]++;
      }
    }
    const ruleRows = Object.values(rules).sort((a, b) => (b.ask + b.block) - (a.ask + a.block)).slice(0, 10);
    const allergenStops = orders.filter((o) => (o.decision?.findings || [])
      .some((f) => f.rule.startsWith("allergen.") && (f.outcome === "ask" || f.outcome === "block"))).length;

    // Valio Aimo API usage, from the audit log.
    const quotes = events.filter((e) => e.event === "quote.issued");
    const searches = events.filter((e) => e.event === "catalog.search").length;
    let calls = 0, hits = 0;
    for (const q of quotes) {
      const c = q.data.valio_api_calls || {};
      calls += (c.price_calls || 0) + (c.availability_calls || 0);
      hits += c.cache_hits || 0;
    }
    const perQuote = quotes.length ? calls / quotes.length : 0;

    // Human decisions and how long they took.
    const decided = orders.filter((o) => o.approval);
    const mins = decided.map((o) => (new Date(o.approval.at) - new Date(o.created_at)) / 60000).sort((a, b) => a - b);
    const median = mins.length ? mins[Math.floor(mins.length / 2)] : null;
    const approved = decided.filter((o) => o.approval.approved).length;
    const pending = orders.filter((o) => o.status === "pending_approval").length;

    // Spend that actually went through, by buyer.
    const spend = {};
    for (const o of orders) if (o.status === "ordered") spend[o.buyer] = (spend[o.buyer] || 0) + (o.total_ex_vat || 0);
    const spendRows = Object.entries(spend).sort((a, b) => b[1] - a[1]);
    const spendTotal = spendRows.reduce((s, [, v]) => s + v, 0);

    const pct = (a, b) => b ? Math.round((a / b) * 100) + " %" : "–";
    const tile = (label, value, sub) => `<div class="tile"><div class="muted small">${label}</div>
      <div class="hero">${value}</div><div class="small muted">${sub}</div></div>`;
    const bar = (label, value, max, text, title, cls = "") => `
      <div class="bar-row" title="${esc(title || "")}">
        <div class="bar-label">${label}</div>
        <div class="bar-track"><div class="bar ${cls}" style="width:${max && value ? Math.max(2, (value / max) * 100) : 0}%"></div></div>
        <div class="bar-val">${text}</div>
      </div>`;
    const tierMax = Math.max(...Object.values(byTier), 1);
    const ruleMax = Math.max(...ruleRows.map((r) => r.ask + r.block), 1);
    const spendMax = Math.max(...spendRows.map(([, v]) => v), 1);

    view.innerHTML = `<div class="head"><div><h2>Gateway stats</h2>
        <div class="muted">Computed live from the orders and the audit log (${events.length} events).</div></div></div>
      <div class="tiles">
        ${tile("Handled without a human", pct(auto, n), `${auto} of ${n} agent orders went straight through`)}
        ${tile("Held or refused by the mandate", byTier.ask + byTier.block, `${byTier.block} refused · ${byTier.ask} sent to a human`)}
        ${tile("Allergen checks that stopped an order", allergenStops, "Unknown allergen data counts as unsafe")}
        ${tile("Valio API calls per quote", perQuote ? perQuote.toFixed(1) : "–",
          `${calls} calls for ${quotes.length} quotes · ${searches} searches used none · vs ~${NAIVE_CALLS} to poll the whole catalog`)}
        ${tile("Human decisions", decided.length, `${approved} approved · ${decided.length - approved} rejected${median != null ? ` · median ${median < 1 ? "<1" : Math.round(median)} min` : ""}${pending ? ` · ${pending} waiting` : ""}`)}
        ${tile("Ordered through the gateway", eur(spendTotal), "ex VAT, approved and automatic orders")}
      </div>

      <div class="card">
        <h3 style="margin-top:0">Mandate outcomes</h3>
        <div class="muted small">What the rules engine decided for each order. Deterministic code, no AI.</div>
        ${["silent", "notify", "ask", "block"].map((t) => bar(
          tier(t), byTier[t], tierMax,
          `${byTier[t]} <span class="muted">${pct(byTier[t], n)}</span>`, `${byTier[t]} orders: ${TIER_LABEL[t]}`, "b-" + t)).join("")}
      </div>

      <div class="card">
        <h3 style="margin-top:0">Rules that held or stopped orders</h3>
        <div class="muted small">Each rule counted once per order. Hover a row for an example message.</div>
        ${ruleRows.length ? ruleRows.map((r) => bar(
          `<div>${esc(RULE_GROUP[r.rule.split(".")[0]] || r.rule)}</div><div class="rule mono">${esc(r.rule)}</div>`,
          r.ask + r.block, ruleMax,
          `${r.ask + r.block}${r.block ? ` <span class="muted small">(${r.block} refused)</span>` : ""}`, r.example)).join("")
          : `<div class="empty">No orders held yet.</div>`}
      </div>

      <div class="card">
        <h3 style="margin-top:0">Ordered spend by buyer</h3>
        <div class="muted small">ex VAT</div>
        ${spendRows.length ? spendRows.map(([b, v]) => bar(esc(b), v, spendMax, eur(v), `${b}: ${eur(v)}`)).join("")
          : `<div class="empty">Nothing ordered yet.</div>`}
      </div>

      ${lost ? lostDemandCard(lost, bar) : ""}

      <div class="chain ${chain.ok ? "ok" : "bad"}">${chain.ok ? "✔" : "✖"} <div><b>${chain.ok ? "Every number above comes from an intact audit chain" : `Audit chain broken at #${esc(chain.broken_at_seq)}`}</b>
        <div class="small">${chain.events} events verified. <a href="#/audit">Open the audit log</a></div></div></div>`;
  },
};

// --- sheet, routing, refresh ------------------------------------------------------

function openSheet(html) {
  $("#sheet-body").innerHTML = html;
  $("#sheet").hidden = false;
  document.body.style.overflow = "hidden";
}
function closeSheet() {
  $("#sheet").hidden = true;
  document.body.style.overflow = "";
}
document.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) closeSheet(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(); });
view.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-subject]");
  if (a && view.dataset.view !== "audit") { e.preventDefault(); views.audit.q = a.dataset.subject; location.hash = "#/audit"; }
});
$("#sheet-body").addEventListener("click", (e) => {
  const a = e.target.closest("a[data-subject]");
  if (a) { e.preventDefault(); closeSheet(); views.audit.q = a.dataset.subject; views.audit.key = ""; location.hash = "#/audit"; route(); }
});

async function refresh() {
  try {
    await pollLive().catch(() => {});
    await views[current].load();
    if (current !== "inbox") {
      setBadge(noQA((await api("/orders?status=pending_approval")).orders, isQAOrder).length);
    }
  } catch (e) {
    view.dataset.view = "";
    view.innerHTML = `<div class="empty"><div class="big">Can't reach the gateway</div><p class="mono small">${esc(e.message)}</p></div>`;
    for (const v of Object.values(views)) v.key = "";
  }
}

// Valio Aimo's own views; the buyer side doesn't show them.
const SELLER_ONLY = new Set(["stats", "insights"]);
// The side switch keeps the sandbox and, for an open order, the order.
function syncSideSwitch() {
  const a = $("#to-other-side");
  if (a) a.href = (SIDE === "buyer" ? "./" : "buyer.html") + SBX_QS + (location.hash.startsWith("#/orders/") ? location.hash : "");
}
window.addEventListener("hashchange", syncSideSwitch);
syncSideSwitch();

function route() {
  const [path, query] = location.hash.split("?", 2);
  const [, name, openId] = path.match(/^#\/(\w+)(?:\/([\w-]+))?\/?$/) || [];
  const same = current === name && openId;
  current = views[name] && !(SIDE === "buyer" && SELLER_ONLY.has(name)) ? name : "today";  // S5: Today is the front page
  if (current === "audit" && query !== undefined) {
    views.audit.q = new URLSearchParams(query).get("subject") || "";
  }
  closeSheet();
  // Deep link: #/orders/PO-XXXX opens that order (a buyer agent can send this to its human).
  // #/standing/SO-XXXX opens that standing order.
  if (openId) openLinked(openId);
  if (same) return;
  document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("on", a.dataset.tab === current));
  document.querySelector(".tabs a.on")?.scrollIntoView({ block: "nearest", inline: "center" });
  view.dataset.view = "";
  views[current].key = "";
  view.innerHTML = `<div class="empty">Loading…</div>`;
  refresh();
  clearInterval(timer);
  timer = setInterval(() => { if (!document.hidden) refresh(); tickDeadlines(); }, REFRESH_MS);
}

// Deep links: with read access control a private order answers 404 like a missing one, so offer the code.
async function openLinked(id) {
  const open = current === "standing" ? openStanding : openOrder;
  try { await open(id); } catch (e) {
    if (e.status !== 404) {  // gateway unreachable or failing: not a missing/private order (QA 01:05Z)
      openSheet(`<h2>${esc(id)}</h2>
        <p>The gateway didn't answer${e.status ? ` (HTTP ${esc(e.status)})` : ""}, so this can't be shown right now.</p>
        <p class="small muted mono">${esc(e.message)}</p>
        <div class="actions"><button class="btn yes" id="link-retry">Try again</button>
          <a class="btn plain" href="#/today">Back to Today</a></div>`);
      $("#link-retry")?.addEventListener("click", () => openLinked(id));
      return;
    }
    const hasCode = !!store.get(CODE_KEY);
    openSheet(`<h2>${esc(id)}</h2>
      <p>This ${current === "standing" ? "standing order" : "order"} can't be shown: it doesn't exist, or it is private
        to its buyer${hasCode ? " and the code saved in this browser doesn't cover it" : ""}.</p>
      <p class="small muted">Approvers see everything with the approver code; a judge sandbox's own orders open with its
        sbxa_ token (use the link with ?sandbox=…).</p>
      <div class="actions"><button class="btn yes" id="link-code">${hasCode ? "Enter a different code" : "Enter the approver code"}</button>
        <a class="btn plain" href="#/today">Back to Today</a></div>`);
    $("#link-code")?.addEventListener("click", async () => {
      const c = await askCode("Enter the approver code to open private orders. It stays in this browser only.");
      if (!c) return;
      store.set(CODE_KEY, c);
      codeDeclined = false;
      for (const v of Object.values(views)) v.key = "";
      openLinked(id);
      refresh();
    });
  }
}

function renderQAToggle() {
  const b = $("#qa-toggle");
  if (b) b.textContent = showQA ? "Hide QA test data" : "Show QA test data";
}
$("#qa-toggle")?.addEventListener("click", () => {
  showQA = !showQA;
  try { localStorage.setItem("vk-show-qa", showQA ? "1" : "0"); } catch { /* private mode */ }
  renderQAToggle();
  for (const v of Object.values(views)) v.key = "";
  route();
});
renderQAToggle();

// Sandbox banner: which sandbox this is, its status and how many live lookups are left.
async function renderSandboxBanner() {
  if (!SANDBOX) return;
  let el = $("#sbx");
  if (!el) {
    el = document.createElement("div");
    el.id = "sbx";
    el.className = "sbx";
    $(".top").after(el);
  }
  try {
    const s = await api("/sandbox/" + encodeURIComponent(SANDBOX));
    const live = s.status === "active";
    el.classList.toggle("off", !live);
    el.innerHTML = `<b>Sandbox ${esc(s.id)}</b> <span class="state${live ? "" : " off"}">${esc(s.status)}</span>
      <span>Only this sandbox's mandates, orders and audit events are shown · ${esc(s.quotes_left)} of ${esc(s.quotes_limit)}
      live lookups left · expires ${when(s.expires_at)} · ${esc(s.orders?.total ?? 0)} ${(s.orders?.total ?? 0) === 1 ? "order" : "orders"}</span>
      <a href="${location.pathname}${location.hash}">Leave sandbox</a>`;
  } catch (e) {
    el.classList.add("off");
    el.innerHTML = `<b>Sandbox ${esc(SANDBOX)}</b> <span>${esc(e.message)}</span> <a href="${location.pathname}">Leave sandbox</a>`;
  }
}
renderSandboxBanner();
if (SANDBOX) setInterval(() => { if (!document.hidden) renderSandboxBanner(); }, 30000);

window.addEventListener("hashchange", route);
renderWho();
route();
