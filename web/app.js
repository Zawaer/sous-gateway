// Valtakirja gateway UI: approval inbox, orders, audit log and mandates.
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

async function api(path, opts = {}, retried = false) {
  const headers = { "content-type": "application/json" };
  const code = store.get("vk-approver-code");
  if (opts.method === "POST" && code) headers["x-approver-code"] = code;
  const res = await fetch(API + path, {
    ...opts,
    headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { message: text }; }
  if (res.status === 401 && opts.method === "POST") {
    // Ask in-page (masked, no auto-capitalise/autocorrect: phone keyboards change codes) until it
    // is accepted or the human cancels.
    const c = await askCode(code || retried
      ? "That approver code was not accepted. Check it and try again (it is case-sensitive)."
      : "This action needs the human approver code.");
    if (c) { store.set("vk-approver-code", c); return api(path, opts, true); }
    store.set("vk-approver-code", "");
    throw new Error("Approver code needed for this action.");
  }
  if (!res.ok) {
    const msg = data?.message || data?.detail?.message || data?.detail || res.statusText;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
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

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const eur = (n) => n == null ? "–" :
  new Intl.NumberFormat("fi-FI", { style: "currency", currency: "EUR" }).format(n);

const when = (iso) => {
  if (!iso) return "–";
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  return d.toLocaleString("fi-FI", { timeZone: "Europe/Helsinki", day: "numeric", month: "numeric",
    hour: "2-digit", minute: "2-digit" });
};
const dateOnly = (iso) => {
  if (!iso) return "–";
  const d = new Date(iso.length === 10 ? iso + "T12:00:00" : iso);
  return isNaN(d) ? esc(iso) : d.toLocaleDateString("fi-FI", { weekday: "short", day: "numeric", month: "numeric" });
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
const TIER_LABEL = { silent: "Auto", notify: "Auto + FYI", ask: "Ask human", block: "Blocked",
  best: "Best", better: "Better", good: "Good" };
const tier = (t) => `<span class="tier t-${esc(t)}">${esc(TIER_LABEL[t] || STATUS_LABEL[t] || t)}</span>`;

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

function findingsList(findings, { only } = {}) {
  const fs = (findings || []).filter((f) => !only || only.includes(f.outcome));
  if (!fs.length) return "";
  return `<ul class="findings">${fs.map((f) => `
    <li>${tier(f.outcome)}<div>${esc(f.message)}<div class="rule">${esc(f.rule)}${f.product_id ? " · " + esc(f.product_id) : ""}</div></div></li>`).join("")}
  </ul>`;
}

function linesTable(lines) {
  return `<table class="lines"><thead><tr>
      <th>Product</th><th class="num">Qty</th><th class="num hide-s">Unit €</th><th class="num">Total €</th><th class="hide-s">Delivery</th>
    </tr></thead><tbody>${(lines || []).map((l) => `
    <tr>
      <td>${esc(l.name)}<div class="muted small mono">${esc(l.product_id)}${l.logistic_model ? " · " + esc(l.logistic_model) : ""}</div></td>
      <td class="num">${esc(l.quantity)} ${esc(l.sales_unit || "")}</td>
      <td class="num hide-s">${eur(l.unit_price_ex_vat)}</td>
      <td class="num">${eur(l.line_total_ex_vat)}</td>
      <td class="hide-s">${dateOnly(l.delivery_date)}</td>
    </tr>`).join("")}</tbody></table>`;
}

function earliestCutoff(order) {
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
    const { orders } = await api("/orders?status=pending_approval");
    setBadge(orders.length);
    const key = orders.map((o) => o.id + o.updated_at).join();
    if (key === this.key && view.dataset.view === "inbox") { tickDeadlines(); return; }
    // Keep notes the human is typing when a new order arrives and the list re-renders.
    const notes = {};
    view.querySelectorAll("[data-order]").forEach((c) => { notes[c.dataset.order] = c.querySelector("textarea")?.value; });
    const firstRender = view.dataset.view !== "inbox";
    const seen = this.seen || new Set();
    this.key = key;
    view.dataset.view = "inbox";
    const main = orders.length
      ? `${orders.map((o) => inboxCard(o, !firstRender && !seen.has(o.id))).join("")}`
      : `<div class="empty"><div class="big">Nothing waiting</div>
          <p>Orders that fall outside an agent's mandate show up here for a human to approve or reject.</p>
          <p><a href="#/orders">See all orders</a></p></div>`;
    view.innerHTML = `<div class="head"><div><h2>${orders.length ? "Waiting for a human" : "Approvals"}</h2>
      <div class="muted">${orders.length ? `${orders.length} order${orders.length > 1 ? "s" : ""} held by the mandate engine` : "Agents are ordering within their mandates."}</div></div></div>
      <div class="inbox-grid"><div>${main}</div>
        <aside class="feed"><div class="feed-head"><span class="live-dot"></span>Live activity</div><div id="feed"></div></aside></div>`;
    this.seen = new Set(orders.map((o) => o.id));
    renderFeed();
    tickDeadlines();
    for (const card of view.querySelectorAll("[data-order]")) {
      if (notes[card.dataset.order]) card.querySelector("textarea").value = notes[card.dataset.order];
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
  const { events } = await api("/audit?limit=14");
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
      <div class="row"><span>${esc(EVENT_LABEL[e.event] || e.event)}</span><span class="mono muted">${when(e.ts).split(" ").pop()}</span></div>
      <div class="small muted"><span class="actor ${esc(e.actor_type)}">${esc(e.actor_type)}</span> ${esc(e.buyer || e.actor)}</div>
    </a>`).join("") : `<div class="small muted">No activity yet.</div>`;
}

function inboxCard(o, isNew = false) {
  const cut = earliestCutoff(o);
  const asks = (o.decision?.findings || []).filter((f) => f.outcome === "ask");
  return `<article class="card ask ${isNew ? "fresh" : ""}" data-order="${esc(o.id)}">
    <div class="row">
      <div><div class="muted small">${esc(o.buyer)}</div><div class="big">${eur(o.total_ex_vat)} <span class="muted small">ex VAT</span></div></div>
      <div style="text-align:right">${tier("ask")}
        ${cut ? `<div class="small">Decide within <span class="deadline" data-until="${esc(cut)}"></span></div>` : ""}</div>
    </div>
    <h3>Why it needs you</h3>
    ${findingsList(asks)}
    ${linesTable(o.lines)}
    <dl class="kv small">
      <dt>Agent</dt><dd class="mono">${esc(o.agent_id)}</dd>
      <dt>Mandate</dt><dd class="mono">${esc(o.mandate_id)}</dd>
      <dt>Order</dt><dd><a href="#" data-open="${esc(o.id)}" class="mono">${esc(o.id)}</a> · ${eur(o.total_inc_vat)} incl. VAT · delivery ${dateOnly(o.delivery_date)}</dd>
    </dl>
    <textarea rows="2" placeholder="Note for the agent and the log (optional)"></textarea>
    <div class="actions">
      <button class="btn no" data-act="reject">Reject</button>
      <button class="btn yes" data-act="approve">Approve order</button>
    </div>
  </article>`;
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
      card.querySelectorAll("button").forEach((x) => (x.disabled = true));
      try {
        const o = await api(`/orders/${encodeURIComponent(id)}/${act}`, {
          method: "POST", body: { by, note: card.querySelector("textarea").value.trim() || null },
        });
        const msg = o.status === "ordered" ? `${id} approved and ordered.`
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
    el.textContent = u.text;
    el.classList.toggle("late", u.late);
    el.title = "Order cut-off " + when(el.dataset.until);
  }
}

function setBadge(n) {
  const b = $("#badge");
  b.hidden = !n;
  b.textContent = n;
  document.title = (n ? `(${n}) ` : "") + "Valtakirja gateway";
}

// All orders, filterable by status.
views.orders = {
  filter: "",
  key: "",
  async load() {
    const { orders } = await api("/orders" + (this.filter ? "?status=" + this.filter : ""));
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
          <div><b>${esc(o.buyer)}</b> <span class="muted mono small">${esc(o.id)}</span>
            <div class="muted small">${esc(o.agent_id)} · ${(o.lines || []).length} line${(o.lines || []).length === 1 ? "" : "s"} · ${when(o.created_at)}</div></div>
          <div style="text-align:right">${tier(o.status)}<div class="small">${eur(o.total_ex_vat)}</div></div>
        </button>`).join("")}</div>`
        : `<div class="empty">No orders${this.filter ? " with this status" : " yet"}.</div>`}`;
    view.querySelectorAll("[data-f]").forEach((c) => c.addEventListener("click", () => {
      this.filter = c.dataset.f; this.key = ""; this.load();
    }));
    view.querySelectorAll("[data-open]").forEach((c) => c.addEventListener("click", () => openOrder(c.dataset.open)));
  },
};

async function openOrder(id) {
  const o = await api("/orders/" + encodeURIComponent(id));
  const d = o.decision || {};
  const a = o.approval;
  openSheet(`
    <div class="muted small">${esc(o.buyer)}</div>
    <h2>${esc(o.id)} ${tier(o.status)}</h2>
    <div class="big">${eur(o.total_ex_vat)} <span class="muted small">ex VAT · ${eur(o.total_inc_vat)} incl.</span></div>
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
    ${o.status === "pending_approval" ? `<div data-order="${esc(o.id)}">
      ${earliestCutoff(o) ? `<p class="small">Decide within <span class="deadline" data-until="${esc(earliestCutoff(o))}"></span>, before the order cut-off.</p>` : ""}
      <textarea rows="2" placeholder="Note for the agent and the log (optional)"></textarea>
      <div class="actions">
        <button class="btn no" data-act="reject">Reject</button>
        <button class="btn yes" data-act="approve">Approve order</button>
      </div></div>` : ""}
    ${a ? `<h3>Human decision</h3>
      <p>${a.approved ? "Approved" : "Rejected"} by <b>${esc(a.by)}</b> at ${when(a.at)}${a.note ? `: “${esc(a.note)}”` : ""}</p>
      ${a.result_reason && a.result_reason !== a.note ? `<p class="muted">${esc(a.result_reason)}</p>` : ""}` : ""}
    <div id="asof"></div>
    <h3>Lines</h3>
    ${linesTable(o.lines)}
    <h3>Substitutes</h3>
    <p class="small muted">Alternatives from Valio Aimo's own assortment that keep the mandate's hard rules
      (allergens, claims, category, unit). Ranked best (no compromises), better (minor) and good (a real trade-off).</p>
    <div id="subs">${(o.lines || []).map((l) => {
      const flagged = (d.findings || []).some((f) => f.product_id === l.product_id && (f.outcome === "ask" || f.outcome === "block"));
      return `<div class="sub-line" data-pid="${esc(l.product_id)}">
        <div class="row"><div>${esc(l.name)} <span class="muted mono">${esc(l.product_id)}</span>
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
  `);
  const box = $("#sheet-body [data-order]");
  if (box) { tickDeadlines(); wireDecision(box, () => openOrder(id)); }
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

  const request = step(1, "Request", search?.ts || q?.issued_at, ["agent", o.agent_id],
    `Needs delivery by ${dateOnly(q?.requested_delivery_date || o.delivery_date)}
     ${search ? ` · searched ${searches.map((s) => `“${esc(s.data.query)}”`).join(", ")}${search.data.claims?.length ? ` with ${esc(search.data.claims.join(", "))}` : ""}${search.data.avoid_allergens?.length ? `, avoiding ${esc(search.data.avoid_allergens.map((c) => allergenNames?.[c] || c).join(", "))}` : ""}
       → ${found.size} products from the static catalog, 0 Valio API calls`
       : !subs.length && q ? ` · named ${(q.lines || []).map((l) => `<span class="mono">${esc(l.product_id)}</span>`).join(", ")} directly, no catalog search` : ""}
     ${subs.length ? ` · asked to replace ${subs.map((l) => `<span class="mono">${esc(l.substitute_for)}</span>`).join(", ")} with a substitute` : ""}`);

  const lookup = step(2, "Lookup", q?.issued_at, ["system", "Valtakirja gateway"],
    q ? `Live price and availability${calls != null ? ` (${calls ? `${calls} Valio API call${calls === 1 ? "" : "s"}` : "from the gateway's short-lived cache, 0 Valio API calls"})` : ""} →
      signed quote <span class="mono">${esc(q.id)}</span>, ${eur(q.total_ex_vat)} ex VAT, valid until ${when(q.expires_at)}.
      ${(q.lines || []).map((l) => `Chose ${esc(l.quantity)} × ${esc(l.name)}${l.logistic_model ? ` (${esc(l.logistic_model)})` : ""}.`).join(" ")}
      ${skipped.length ? `Passed over ${skipped.length}: ${skipped.slice(0, 2).map((s) => `${esc(s.name)} (${esc(((s.why_not || [])[0] || "").replace(/\.$/, ""))})`).join("; ")}${skipped.length > 2 ? ` and ${skipped.length - 2} more` : ""}.` : ""}
      ${gaps.length ? `<span style="color:var(--ask)">Could not cover ${gaps.map((g) => `${esc(g.uncovered)} ${esc(g.unit || "")}${g.item?.note ? ` of “${esc(g.item.note)}”` : ""}`).join(", ")}: nothing among its candidates can be delivered in time.</span>` : ""}`
      : `<span class="muted">Quote ${esc(o.quote_id)} not found.</span>`);

  const fired = (d.findings || []).filter((f) => f.outcome !== "silent" && f.rule !== "mandate.ok");
  const order = step(3, "Order", o.created_at, ["agent", o.agent_id],
    `${d.outcome === "block" ? "Tried to order" : "Committed to"} the quote at its fixed price, ${eur(o.total_ex_vat)} ex VAT. The gateway re-checked the quote's
     signature and cut-offs, then the mandate engine ruled ${tier(d.outcome)}${fired.length
       ? `: ${fired.map((f) => esc(f.message)).join(" ")}` : " (all checks passed)."}`);

  let appr;
  if (d.outcome === "block") {
    appr = step(4, "Approval", d.at, ["system", "mandate engine"], `Refused outright: this is outside what any human can approve under the mandate. Nothing was ordered.`);
  } else if (a) {
    appr = step(4, "Approval", a.at, ["human", a.by],
      `${a.approved ? "Approved" : "Rejected"}${a.note ? `: “${esc(a.note)}”` : ""} → ${tier(o.status)}${a.result_reason && a.result_reason !== a.note ? ` ${esc(a.result_reason)}` : ""}`);
  } else if (o.status === "pending_approval") {
    appr = step(4, "Approval", null, ["human", o.buyer],
      `<span style="color:var(--ask)">Waiting for a human at ${esc(o.buyer)}.</span>${notified.length ? " Approval request sent." : ""}`);
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
    <div class="nm">${esc(s.name)}</div>
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
    const [{ events }, chain] = await Promise.all([api("/audit?" + params), api("/audit/verify")]);
    const key = this.q + (events[0]?.seq ?? "") + chain.ok;
    if (key === this.key && view.dataset.view === "audit") return;
    this.key = key;
    const focused = document.activeElement?.id === "audit-q";
    view.dataset.view = "audit";
    view.innerHTML = `<div class="head"><div><h2>Audit log</h2>
        <div class="muted">One record for the buyer and Valio Aimo. Each entry includes the hash of the one before it.</div></div></div>
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
};
const NOTIFY_WHAT = { "order.notify": "FYI to the buyer", "order.ask": "approval request to the buyer",
  "order.decided": "decision to the agent", test: "test" };

function eventSummary(e) {
  const d = e.data || {};
  switch (e.event) {
    case "catalog.search":
      return `“${esc(d.query)}” → ${(d.results || []).length} products${d.avoid_allergens?.length ? ", avoiding " + esc(d.avoid_allergens.join(", ")) : ""} · 0 Valio API calls`;
    case "quote.issued": {
      const calls = d.valio_api_calls ? Object.values(d.valio_api_calls).reduce((a, b) => a + (Number(b) || 0), 0) : null;
      return `${(d.lines || []).length} line(s), ${eur(d.total_ex_vat)} ex VAT${d.preview ? " · mandate preview " + tier(d.preview) : ""}${calls != null ? ` · ${calls} Valio API call(s)` : ""}`;
    }
    case "order.ordered": case "order.pending_approval": case "order.refused":
      return `${eur(d.total_ex_vat)} ${tier(d.outcome)} ${(d.fired || []).map((f) => esc(f.message)).join(" · ")}`;
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
        ? `<span style="color:var(--ask)">drift: ${(d.drift || []).map((x) => esc(x.message || x.rule)).join(" · ")}</span>` : "no drift"}`;
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
      return `${esc(m.buyer)} → agent ${esc(m.agent_id)} · alone up to ${eur(m.auto_approve_limit_eur)}`;
    }
    default:
      return "";
  }
}

function eventRow(e) {
  return `<div class="ev">
    <div class="seq">#${esc(e.seq)}</div>
    <div>
      <div class="row"><span class="what">${esc(EVENT_LABEL[e.event] || e.event)}</span><span class="muted small">${when(e.ts)}</span></div>
      <div class="small"><span class="actor ${esc(e.actor_type)}">${esc(e.actor_type)}</span> ${esc(e.actor)}
        ${e.subject ? ` · <a href="#" class="mono" data-subject="${esc(e.subject)}">${esc(e.subject)}</a>` : ""}
        ${e.buyer ? ` · <span class="muted">${esc(e.buyer)}</span>` : ""}</div>
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
    const { mandates } = await api("/mandates");
    allergenNames ||= await api("/allergens").then((r) => r.codes).catch(() => ({}));
    const key = mandates.map((m) => m.id + m.revoked).join();
    if (key === this.key && view.dataset.view === "mandates") return;
    this.key = key;
    view.dataset.view = "mandates";
    view.innerHTML = `<div class="head"><div><h2>Mandates</h2>
      <div class="muted">A mandate (valtakirja) is what a human lets their purchasing agent do. The gateway signs it and enforces it on every order.</div></div></div>
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
    view.querySelectorAll("[data-sig]").forEach((b) => b.addEventListener("click", async () => {
      const { jws } = await api("/mandates/" + encodeURIComponent(b.dataset.sig));
      const r = await verifyJws(jws);
      b.outerHTML = `<span class="sig ${r.ok ? "ok" : "bad"}">${r.ok ? "✔" : "✖"} ${esc(r.why)}</span>`;
    }));
  },
};

function mandateCard(m) {
  const a = m.auto_approve_limit_eur, n = m.notify_above_eur, h = m.hard_limit_eur;
  const ladder = [
    `<div class="l-silent">Alone ≤ ${eur(n != null && n < a ? n : a)}</div>`,
    n != null && n < a ? `<div class="l-notify">FYI ≤ ${eur(a)}</div>` : "",
    `<div class="l-ask">Ask ${h != null ? "≤ " + eur(h) : "above"}</div>`,
    h != null ? `<div class="l-block">Refuse &gt; ${eur(h)}</div>` : "",
  ].join("");
  const allergens = (m.avoid_allergens || []).map((c) => (allergenNames?.[c] || c)).join(", ");
  const rule = (k, v) => `<div><span class="muted">${k}</span><span>${v}</span></div>`;
  return `<article class="card ${m.revoked ? "revoked" : ""}">
    <div class="row"><div><div class="muted small mono">${esc(m.id)}</div><h2>${esc(m.buyer)}</h2></div>
      ${m.revoked ? `<span class="tier t-block">Revoked</span>` : `<span class="tier t-silent">Active</span>`}</div>
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
      ${m.allowed_categories ? rule("Categories", esc(m.allowed_categories.join(", "))) : ""}
    </div>
    <div class="actions">
      <button class="btn plain" data-sig="${esc(m.id)}">🔏 Verify signature</button>
      <a class="btn plain" href="#/audit" onclick="views.audit.q='${esc(m.id)}';views.audit.key=''">History</a>
      ${m.revoked ? "" : `<button class="btn plain" data-revoke="${esc(m.id)}" style="color:var(--block)">Revoke</button>`}
    </div>
  </article>`;
}

// Stats: what the gateway did, for Valio Aimo's operators (and the demo's numbers slide).
// Everything is computed from /orders and the audit log, so it's the same record anyone can check.
const CATALOG_SIZE = 17546;
const NAIVE_CALLS = Math.ceil(CATALOG_SIZE / 1000) * 2; // price + availability, 1000 ids per call
const TIER_ICON = { silent: "✓", notify: "✉", ask: "?", block: "✕" };
const RULE_GROUP = {
  allergen: "Allergen safety", spend: "Spend limits", availability: "Stock and cut-offs", supply: "Supply risk",
  delivery: "Delivery date", price: "Price", product: "Product requirements", substitution: "Substitutes",
  need: "Partial cover", mandate: "Mandate validity", order: "Empty order",
};

views.stats = {
  key: "",
  async load() {
    const [{ orders }, { events }, chain] = await Promise.all([
      api("/orders"), api("/audit?limit=10000"), api("/audit/verify")]);
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
        <div class="bar-track"><div class="bar ${cls}" style="width:${max ? Math.max(2, (value / max) * 100) : 0}%"></div></div>
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
          `<span class="tier t-${t}">${TIER_ICON[t]} ${TIER_LABEL[t]}</span>`, byTier[t], tierMax,
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
      const { orders } = await api("/orders?status=pending_approval");
      setBadge(orders.length);
    }
  } catch (e) {
    view.dataset.view = "";
    view.innerHTML = `<div class="empty"><div class="big">Can't reach the gateway</div><p class="mono small">${esc(e.message)}</p></div>`;
    for (const v of Object.values(views)) v.key = "";
  }
}

function route() {
  const [, name, openId] = location.hash.match(/^#\/(\w+)(?:\/([\w-]+))?/) || [];
  const same = current === name && openId;
  current = views[name] ? name : "inbox";
  closeSheet();
  // Deep link: #/orders/PO-XXXX opens that order (a buyer agent can send this to its human).
  if (openId) openOrder(openId).catch((e) => toast("Can't open " + openId + ": " + e.message, 5000));
  if (same) return;
  document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("on", a.dataset.tab === current));
  view.dataset.view = "";
  views[current].key = "";
  view.innerHTML = `<div class="empty">Loading…</div>`;
  refresh();
  clearInterval(timer);
  timer = setInterval(() => { if (!document.hidden) refresh(); tickDeadlines(); }, REFRESH_MS);
}

window.addEventListener("hashchange", route);
renderWho();
route();
