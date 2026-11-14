// Sous live: the gateway's real orders as a flow. Buyer companies on the left, Sous in the middle, Valio Aimo on
// the right. Each order travels from its buyer to the gateway, gets the mandate's tier (colour), then goes on to
// Valio (auto), waits at the gateway for a human (held) or bounces back (refused). Reads only public demo data
// (GET /mandates, /orders, /allergens, /audit/verify); when nothing new happens it replays recent orders.
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const eur = (x) => (Number(x) || 0).toLocaleString("fi-FI", { style: "currency", currency: "EUR" });
const pname = (n) => { n = String(n ?? ""); return /[a-zåäö]/.test(n) ? n : n.charAt(0) + n.slice(1).toLowerCase(); };
const UNIT = { ST: "pcs", PRK: "packs", KI: "cartons", PAK: "packs", KG: "kg", L: "l", BOT: "bottles", RS: "boxes" };
const COLOR = { auto: "#7cc68e", fyi: "#8ab8ec", ask: "#f2c14e", block: "#ef8a76" };
const LABEL = { auto: "Auto", fyi: "Auto + FYI", ask: "Needs a human", block: "Refused" };
const ICON = { auto: "check_circle", fyi: "notifications", ask: "pan_tool", block: "block" };
const BUYER_ICON = [["hotel", "hotel"], ["hospital", "local_hospital"], ["school", "school"], ["catering", "restaurant"],
  ["café", "local_cafe"], ["cafe", "local_cafe"], ["kindergarten", "child_care"], ["päiväkoti", "child_care"], ["", "storefront"]];
const QA = (s) => /^(qa[-_:]|M-QA-)|^QA\b/i.test(s || "");
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

let W = 0, H = 0, DPR = 1, mobile = false;
const cv = $("#bg"), cx = cv.getContext("2d");
const companies = new Map(); // buyer -> {buyer, mandates[], x, y, el}
const hub = { x: 0, y: 0, el: null }, valio = { x: 0, y: 0, el: null };
const orders = new Map();    // id -> order (latest seen)
const flights = [];          // animated orders
const ambient = [];          // faint packets along the paths
let allergens = {}, lastLive = 0, firstLoad = true, chainOk = null, ruleCount = null;

const tierOf = (o) => o.status === "pending_approval" ? "ask"
  : o.status === "ordered" ? (o.approval?.approved ? "ask" : o.decision?.outcome === "notify" ? "fyi" : "auto")
  : "block";
const finalOf = (o) => o.status === "ordered" ? "out" : o.status === "pending_approval" ? "hold" : "back";
const what = (o) => {
  const l = (o.lines || [])[0];
  return l ? `${(+l.quantity).toLocaleString("fi-FI")} ${UNIT[l.sales_unit] || l.sales_unit || ""} ${pname(l.name)}${o.lines.length > 1 ? ` +${o.lines.length - 1}` : ""}` : "nothing";
};
const reason = (o) => {
  const f = (o.decision?.findings || []).find((x) => x.outcome === "block") || (o.decision?.findings || []).find((x) => x.outcome === "ask");
  return f ? f.message : "";
};

async function get(path) {
  const r = await fetch(path, { headers: { accept: "application/json" } });
  if (!r.ok) throw new Error(path + " " + r.status);
  return r.json();
}

// --- layout ---------------------------------------------------------------------------------------------------
function iconFor(buyer) { const b = buyer.toLowerCase(); return BUYER_ICON.find(([k]) => b.includes(k))[1]; }

function layout() {
  DPR = Math.min(2, window.devicePixelRatio || 1);
  W = innerWidth; H = innerHeight; mobile = W < 720;
  cv.width = W * DPR; cv.height = H * DPR; cx.setTransform(DPR, 0, 0, DPR, 0, 0);
  const list = [...companies.values()];
  if (mobile) {
    hub.x = W / 2; hub.y = H * 0.67; valio.x = W / 2; valio.y = H * 0.85;
    const top = H * 0.47, n = list.length;
    list.forEach((c, i) => { c.x = W * (0.5 + (i - (n - 1) / 2) * Math.min(0.2, 0.86 / Math.max(1, n))); c.y = top + (i % 2) * 34; });
  } else {
    hub.x = W * 0.52; hub.y = H * 0.56; valio.x = W * 0.84; valio.y = hub.y;
    const R = Math.min(W * 0.34, H * 0.36), n = list.length;
    const span = Math.min(Math.PI * 0.85, 0.42 * Math.max(1, n - 1) + 0.0001);
    list.forEach((c, i) => {
      const a = Math.PI + (n === 1 ? 0 : -span / 2 + (span * i) / (n - 1));
      c.x = hub.x + Math.cos(a) * R * 1.05; c.y = hub.y + Math.sin(a) * R;
    });
  }
  for (const n of [hub, valio, ...list]) if (n.el) { n.el.style.left = n.x + "px"; n.el.style.top = n.y + "px"; }
}

function node(cls, icon, title, sub, onClick) {
  const b = document.createElement("button");
  b.className = "node " + cls;
  b.innerHTML = `<span class="dot"><span class="ms">${icon}</span></span><span class="lbl"><b>${esc(title)}</b><span>${esc(sub)}</span></span>`;
  b.addEventListener("click", onClick);
  $("#nodes").appendChild(b);
  return b;
}

// --- curves -----------------------------------------------------------------------------------------------------
function ctrl(a, b, bend) {
  const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2, dx = b.x - a.x, dy = b.y - a.y;
  return { x: mx - dy * bend, y: my + dx * bend };
}
function at(a, c, b, t) {
  const u = 1 - t;
  return { x: u * u * a.x + 2 * u * t * c.x + t * t * b.x, y: u * u * a.y + 2 * u * t * c.y + t * t * b.y };
}
const ease = (t) => t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;

// --- flights ----------------------------------------------------------------------------------------------------
function launch(o, live) {
  const c = companies.get(o.buyer);
  if (!c) return;
  flights.push({ o, c, tier: tierOf(o), end: finalOf(o), phase: "in", t: 0, live, trail: [], hold: 0, born: performance.now() });
  ticker(o, live);
}

function ticker(o, live) {
  const t = tierOf(o);
  $("#ticker").innerHTML = `<span>${live ? "● " : ""}<b>${esc(o.buyer.split(",")[0])}</b> · ${esc(what(o))} · <span class="t-${t}">${LABEL[t]}</span> · ${eur(o.total_ex_vat)}</span>`;
}

function step(f, dt) {
  const speed = { in: 1 / 1.7, decide: 1 / 0.7, out: 1 / 1.3, back: 1 / 1.3, hold: 0 }[f.phase];
  f.t += dt * speed;
  if (f.phase === "hold") {
    f.hold += dt;
    const cur = orders.get(f.o.id);
    if (cur && cur.status !== "pending_approval") { f.end = finalOf(cur); f.phase = f.end === "out" ? "out" : "back"; f.t = 0; f.decided = true; }
    else if (!f.live && f.hold > 5) { f.done = true; }
    else if (f.hold > 40) { f.done = true; }
    return;
  }
  if (f.t < 1) return;
  f.t = 0;
  if (f.phase === "in") { f.phase = "decide"; if (f.tier === "ask") hub.el.classList.add("pulse"), setTimeout(() => hub.el.classList.remove("pulse"), 1200); }
  else if (f.phase === "decide") f.phase = f.end;
  else { f.done = true; if (f.phase === "out") burst(valio, f.tier); }
}

function posOf(f) {
  if (f.phase === "in") return at(f.c, ctrl(f.c, hub, 0.18), hub, ease(f.t));
  if (f.phase === "decide") return hub;
  if (f.phase === "hold") {
    const a = f.hold * 2.2 + f.born / 300, r = 50;
    return { x: hub.x + Math.cos(a) * r, y: hub.y + Math.sin(a) * r };
  }
  if (f.phase === "out") return at(hub, ctrl(hub, valio, -0.12), valio, ease(f.t));
  return at(hub, ctrl(hub, f.c, 0.18), f.c, ease(f.t));
}

const bursts = [];
function burst(n, tier) { bursts.push({ x: n.x, y: n.y, r: 30, a: 0.5, col: COLOR[tier] }); }

// --- drawing ----------------------------------------------------------------------------------------------------
let last = performance.now(), gridShift = 0;
function frame(now) {
  const dt = Math.min(0.05, (now - last) / 1000); last = now;
  cx.clearRect(0, 0, W, H);
  // dot grid, drifting slowly
  gridShift = (gridShift + dt * 4) % 28;
  cx.fillStyle = "rgba(242,241,234,0.055)";
  for (let x = -28 + gridShift; x < W; x += 28) for (let y = -28 + gridShift * 0.5; y < H; y += 28) cx.fillRect(x, y, 1.2, 1.2);
  // soft glow behind the hub
  const g = cx.createRadialGradient(hub.x, hub.y, 0, hub.x, hub.y, Math.max(W, H) * 0.45);
  g.addColorStop(0, "rgba(242,241,234,0.06)"); g.addColorStop(1, "rgba(242,241,234,0)");
  cx.fillStyle = g; cx.fillRect(0, 0, W, H);
  // paths
  cx.lineWidth = 1;
  for (const c of companies.values()) {
    const k = ctrl(c, hub, 0.18);
    cx.strokeStyle = "rgba(242,241,234,0.08)"; cx.beginPath(); cx.moveTo(c.x, c.y); cx.quadraticCurveTo(k.x, k.y, hub.x, hub.y); cx.stroke();
  }
  const kv = ctrl(hub, valio, -0.12);
  cx.strokeStyle = "rgba(242,241,234,0.14)"; cx.beginPath(); cx.moveTo(hub.x, hub.y); cx.quadraticCurveTo(kv.x, kv.y, valio.x, valio.y); cx.stroke();
  // ambient packets keep the paths alive
  if (!reduced && ambient.length < companies.size * 2 && Math.random() < dt * 2) {
    const list = [...companies.values()];
    if (list.length) ambient.push({ c: list[(Math.random() * list.length) | 0], t: 0, v: 0.25 + Math.random() * 0.2 });
  }
  for (const p of ambient) {
    p.t += dt * p.v;
    const q = at(p.c, ctrl(p.c, hub, 0.18), hub, p.t);
    cx.fillStyle = `rgba(242,241,234,${0.22 * Math.sin(Math.PI * Math.min(1, p.t))})`;
    cx.beginPath(); cx.arc(q.x, q.y, 1.4, 0, 7); cx.fill();
  }
  for (let i = ambient.length - 1; i >= 0; i--) if (ambient[i].t >= 1) ambient.splice(i, 1);
  // flights
  for (const f of flights) {
    step(f, reduced ? dt * 3 : dt);
    const p = posOf(f);
    const col = f.phase === "in" ? "#f2f1ea" : COLOR[f.phase === "back" && f.decided ? "block" : f.tier];
    f.trail.push(p); if (f.trail.length > 18) f.trail.shift();
    for (let i = 1; i < f.trail.length; i++) {
      cx.strokeStyle = hexA(col, (i / f.trail.length) * 0.5); cx.lineWidth = 2.2 * (i / f.trail.length);
      cx.beginPath(); cx.moveTo(f.trail[i - 1].x, f.trail[i - 1].y); cx.lineTo(f.trail[i].x, f.trail[i].y); cx.stroke();
    }
    cx.fillStyle = col; cx.shadowColor = col; cx.shadowBlur = 14;
    cx.beginPath(); cx.arc(p.x, p.y, f.live ? 4.6 : 3.6, 0, 7); cx.fill(); cx.shadowBlur = 0;
    if (f.phase === "decide" || f.phase === "hold") {
      cx.strokeStyle = hexA(COLOR[f.tier], 0.5); cx.lineWidth = 1.2;
      cx.beginPath(); cx.arc(hub.x, hub.y, 44 + 6 * Math.sin(now / 200), 0, 7); cx.stroke();
    }
  }
  for (let i = flights.length - 1; i >= 0; i--) if (flights[i].done) flights.splice(i, 1);
  for (const b of bursts) {
    b.r += dt * 60; b.a -= dt * 0.7;
    cx.strokeStyle = hexA(b.col, Math.max(0, b.a)); cx.lineWidth = 1.5; cx.beginPath(); cx.arc(b.x, b.y, b.r, 0, 7); cx.stroke();
  }
  for (let i = bursts.length - 1; i >= 0; i--) if (bursts[i].a <= 0) bursts.splice(i, 1);
  requestAnimationFrame(frame);
}
function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a.toFixed(3)})`;
}

// --- data -------------------------------------------------------------------------------------------------------
async function loadStatic() {
  const [m, al, rules] = await Promise.all([get("/mandates"), get("/allergens").catch(() => ({})), get("/rules").catch(() => null)]);
  allergens = al.codes || {};
  ruleCount = rules?.rules?.length ?? null;
  for (const md of (m.mandates || []).filter((x) => !x.sandbox && !QA(x.id) && !QA(x.buyer) && !x.revoked)) {
    const c = companies.get(md.buyer) || { buyer: md.buyer, mandates: [] };
    c.mandates.push(md);
    companies.set(md.buyer, c);
  }
  hub.el = node("hub", "shield", "Sous", "checks every order", () => openHub());
  valio.el = node("valio", "local_shipping", "Valio Aimo", "live prices and stock", () => openValio());
  for (const c of companies.values()) {
    const [name, place] = c.buyer.split(",");
    c.el = node("", iconFor(c.buyer), name, (place || "").trim(), () => openCompany(c));
  }
  layout();
}

async function poll() {
  try {
    const { orders: list } = await get("/orders");
    const fresh = [];
    for (const o of list.filter((x) => companies.has(x.buyer))) {
      if (!orders.has(o.id) && !firstLoad) fresh.push(o);
      orders.set(o.id, o);
    }
    fresh.sort((a, b) => a.created_at.localeCompare(b.created_at)).forEach((o, i) => setTimeout(() => launch(o, true), i * 900));
    if (fresh.length) lastLive = Date.now();
    firstLoad = false;
    stats();
    $("#mode").innerHTML = Date.now() - lastLive < 60000 && lastLive ? `<span class="live">● live</span> · orders arrive as agents place them`
      : `replaying recent orders · live ones appear as they come`;
  } catch (e) {
    $("#mode").textContent = "can't reach the gateway right now";
  }
}

function replay() {
  if (flights.length > 2 || document.hidden) return;
  const recent = [...orders.values()].sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 40);
  if (recent.length) launch(recent[(Math.random() * recent.length) | 0], false);
}

function stats() {
  const all = [...orders.values()];
  const auto = all.filter((o) => o.status === "ordered" && !o.approval).length;
  const held = all.filter((o) => o.status === "pending_approval" || o.approval).length;
  const refused = all.filter((o) => o.status === "refused").length;
  $("#stats").innerHTML = `<div><b>${all.length}</b>agent orders</div>
    <div><b>${all.length ? Math.round((100 * auto) / all.length) : 0} %</b>ordered alone</div>
    <div><b>${held}</b>sent to a human</div><div><b>${refused}</b>refused</div>`;
}

// --- side panel ---------------------------------------------------------------------------------------------------
function openSide(html, sel) {
  document.querySelectorAll(".node.sel").forEach((n) => n.classList.remove("sel"));
  sel?.classList.add("sel");
  $("#side-body").innerHTML = html;
  $("#side").classList.add("open");
}
$("#close").addEventListener("click", () => { $("#side").classList.remove("open"); document.querySelectorAll(".node.sel").forEach((n) => n.classList.remove("sel")); });
addEventListener("keydown", (e) => { if (e.key === "Escape") $("#close").click(); });

function chips(m) {
  const out = [];
  if (m.auto_approve_limit_eur != null) out.push(`Alone up to ${eur(m.auto_approve_limit_eur)}`);
  if (m.notify_above_eur != null) out.push(`FYI above ${eur(m.notify_above_eur)}`);
  if (m.hard_limit_eur != null) out.push(`Never over ${eur(m.hard_limit_eur)}`);
  if (m.daily_limit_eur != null) out.push(`${eur(m.daily_limit_eur)} a day`);
  if ((m.avoid_allergens || []).length) out.push("Avoids " + m.avoid_allergens.map((c) => (allergens[c] || c)).join(", ").toLowerCase());
  if (m.substitutions === "ask") out.push("Substitutes need a human");
  if (m.unknown_allergens) out.push(`Missing allergen data: ${m.unknown_allergens === "block" ? "refused" : "ask a human"}`);
  if (m.require_agent_auth) out.push("Agent must authenticate");
  return out.map((t) => `<span class="chip">${esc(t)}</span>`).join("");
}

function orderList(list) {
  return `<ul class="orders">${list.slice(0, 8).map((o) => { const t = tierOf(o); return `<li>
    <span class="ms t-${o.status === "pending_approval" ? "ask" : o.status === "ordered" ? (t === "ask" ? "auto" : t) : "block"}">${o.status === "ordered" ? (o.approval ? "how_to_reg" : ICON[t]) : o.status === "pending_approval" ? "pan_tool" : "block"}</span>
    <span class="what" title="${esc(reason(o))}">${esc(what(o))}</span><span class="eur">${eur(o.total_ex_vat)}</span></li>`; }).join("")}</ul>`;
}

function openCompany(c) {
  const list = [...orders.values()].filter((o) => o.buyer === c.buyer).sort((a, b) => b.created_at.localeCompare(a.created_at));
  const m = c.mandates[0];
  openSide(`<h2>${esc(c.buyer.split(",")[0])}</h2>
    <div class="muted">${esc([c.buyer.split(",").slice(1).join(",").trim(), m.issued_by ? `mandate signed by ${m.issued_by}` : ""].filter(Boolean).join(" · "))}</div>
    <h3>What its agent may do</h3><div class="rules">${chips(m)}</div>
    <h3>Its orders</h3>${list.length ? orderList(list) : `<p class="muted">No orders yet.</p>`}
    <div class="links"><a class="btn solid" href="/buyer/"><span class="ms">person</span>Its side (buyer view)</a>
      <a class="btn" href="/ui/#/orders">All orders</a></div>`, c.el);
}
function openHub() {
  openSide(`<h2>Sous</h2><div class="muted">Valio Aimo's front door for purchasing agents.</div>
    <h3>How it decides</h3><p class="muted">Every quote and order is checked against the buyer's signed mandate by
    deterministic rules${ruleCount ? ` (${ruleCount} of them)` : ""}. No model decides. Within the mandate the agent orders alone;
    outside it a human approves on their phone, or the order is refused with the rule that stopped it.</p>
    <h3>Can it be trusted</h3><p class="muted">${chainOk == null ? "…" : chainOk ? "The audit log's hash chain verifies" : "The audit chain does not verify"}; quotes, orders and receipts are Ed25519-signed.</p>
    <div class="links"><a class="btn solid" href="/ui/">Seller view</a><a class="btn" href="/ui/#/audit">Audit log</a><a class="btn" href="/docs">API</a></div>`, hub.el);
}
function openValio() {
  const ordered = [...orders.values()].filter((o) => o.status === "ordered");
  openSide(`<h2>Valio Aimo</h2><div class="muted">The seller. Live prices and availability come from its API on every quote.</div>
    <h3>Received through Sous</h3><p class="muted">${ordered.length} orders, ${eur(ordered.reduce((s, o) => s + (o.total_ex_vat || 0), 0))} ex VAT.</p>
    <div class="links"><a class="btn solid" href="/ui/#/insights">What Valio can fix</a><a class="btn" href="/ui/#/stats">Stats</a></div>`, valio.el);
}

// --- start --------------------------------------------------------------------------------------------------------
addEventListener("resize", layout);
(async () => {
  try { await loadStatic(); } catch (e) { $("#mode").textContent = "can't reach the gateway right now"; }
  get("/audit/verify").then((v) => { chainOk = !!v.ok; }).catch(() => {});
  await poll();
  setInterval(poll, 4000);
  setInterval(replay, 2600);
  setTimeout(replay, 400);
  requestAnimationFrame(frame);
})();
