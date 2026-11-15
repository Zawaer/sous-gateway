// Replay of the real MCP session (window.SESSION from session-data.js). Deterministic: render(t) only.
// Scenes: milk (beats 2-4, ?b2=&b3=&b4=) and purpose (beat 6, ?c0=&c1=&c2=&c3=&held=&dur=).
const S = window.SESSION;
const ev = (scene, tool, n = 0) => S.events.filter(e => e.scene === scene && e.tool === tool)[n];
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const eur = v => '€' + Number(v).toLocaleString('en-IE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const num = (name, d) => { const v = parseFloat(Q.get(name)); return isNaN(v) ? d : v; };
const hm = iso => { const d = new Date(iso); return d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Helsinki' }); };
const dayhm = iso => new Date(iso).toLocaleString('en-GB', { weekday: 'short', day: 'numeric', month: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Helsinki' }).replace(',', '');

function frame(stateFn) {
  document.body.insertAdjacentHTML('beforeend', `<div class="barwrap" id="bar"></div>
  <div class="win"><div class="term"><div class="tbar"><i style="background:#ff5f56"></i><i style="background:#ffbd2e"></i><i style="background:#27c93f"></i>
  <span>hotel-aurora-agent  ▸  Sous gateway · MCP</span></div><div class="body" id="tb"></div></div>
  <div class="ans"><div class="ahead"><span>VALIO AIMO · SOUS GATEWAY ANSWERED</span><span class="counter" id="ctr"></span></div><div id="ab"></div></div></div>`);
  let last = -1;
  return t => { const s = stateFn(t); if (s !== last) { $('bar').innerHTML = chapterBar(s); last = s; } };
}
// terminal lines: [time, cls, text]; a 'prompt' line types itself over `typeDur`
function termLines(lines) {
  const tb = $('tb');
  tb.innerHTML = lines.map((l, i) => `<div class="tl ${l[1]}" id="tl${i}"></div>`).join('');
  return t => lines.forEach((l, i) => {
    const el = $('tl' + i), [t0, cls, text, typeDur] = l;
    if (cls === 'prompt') {
      const n = Math.round(clamp((t - t0) / (typeDur || 2)) * text.length);
      el.style.opacity = t >= t0 - 0.4 ? 1 : 0;
      el.innerHTML = '&gt; ' + esc(text.slice(0, n)) + (n < text.length && t >= t0 - 0.4 ? '<span class="caret"></span>' : '');
    } else {
      el.innerHTML = esc(text);
      el.style.opacity = ease((t - t0) / 0.25);
    }
  });
}
function shortWhy(w) {
  const c = w.find(x => /cut-off/.test(x));
  if (c) { const m = c.match(/was (\w{3} \d+\.\d+ \d+:\d+)/); return `order cut-off ${m ? m[1] : ''} already passed, though the API still says Monday`; }
  return w[0];
}
function show(el, t, t0, d = 0.35, dy = 10) { fadeUp(el, ease((t - t0) / d), dy); }
function hide(el, t, t1, d = 0.3) { if (t >= t1) el.style.opacity = String(Math.min(+el.style.opacity || 1, 1 - ease((t - t1) / d))); }

// --------------------------------------------------------------------------------------------- milk
function milk() {
  const b2 = num('b2', 7), b3 = num('b3', 17), b4 = num('b4', 11), T1 = b2, T2 = b2 + b3;
  const qa = num('qa', 5.0), fa = num('fa', 11.1), ka = num('ka', 14.4), oa = num('oa', 3.6);
  window.DURATION = b2 + b3 + b4;
  const sr = ev('milk', 'search_products').result, qe = ev('milk', 'request_quote'), q = qe.result.quote, oe = ev('milk', 'place_order').result;
  const user = S.events.find(e => e.scene === 'milk' && e.user).user;
  const line = q.lines[0], calls = q.valio_api_calls, live = calls.price_calls + calls.availability_calls;
  const pv = q.mandate_preview || {};
  const skipped = (q.alternatives_considered || []).filter(a => (a.why_not || []).some(w => /cut-off/.test(w))).slice(0, 1);
  const typed = (q.alternatives_considered || []).filter(a => (a.why_not || []).some(w => /≠/.test(w)) && !skipped.includes(a)).slice(0, 1);
  const bar = frame(t => t < T1 ? 1 : t < T2 ? 2 : 3);
  const term = termLines([
    [0.5, 'dim', `connected to ${S.server} (Streamable HTTP)`],
    [0.9, 'prompt', user, 2.6],
    [4.0, 'call', `● search_products(query="laktoositon maito", claims=["FREE_FROM_LACTOSE"])`],
    [4.9, 'res', `  └ ${sr.products.length} products from the local index · 0 Valio API calls`],
    [T1 + qa, 'call', `● request_quote(mandate_id="${S.mandate}", delivery_date="${S.delivery_date}", items=[1,000 L milk])`],
    [T1 + qa + 0.9, 'res', `  └ signed quote ${q.id} · ${eur(q.total_ex_vat)} ex VAT`],
    [T1 + qa + 1.2, 'res', `    if ordered: ${pv.outcome === 'notify' ? 'Auto + FYI' : pv.outcome}`],
    [T2 + oa, 'call', `● place_order(quote_id="${q.id}", idempotency_key=…)`],
    [T2 + oa + 0.7, 'ok', `  └ ${oe.id} · ${oe.status} · Auto + FYI`],
  ]);
  const ab = $('ab');
  ab.innerHTML = `<div class="blk" id="b0" style="top:44px"><div class="sub" style="font-size:13px">Every call gets a structured, signed answer from Valio Aimo's gateway. The agent never sees Valio's systems.</div></div>
  <div class="blk" id="bS" style="top:44px"><h3>search_products · ${sr.products.length} matches</h3>
    <div class="sub">Structured catalog search: 17,546 products in Finnish, Swedish and English. <span class="hl" id="hS">No call to Valio's systems.</span></div>
    <table class="res">${sr.products.slice(0, 8).map((p, i) => `<tr id="r${i}"><td class="id">${p.id}</td><td>${esc(p.name)}${(p.labels || []).includes('GOODS_FROM_FINLAND_BLUE_SWAN') ? '<span class="chip">Blue Swan</span>' : ''}</td><td style="white-space:nowrap;color:var(--ink-60)">${p.temperature || ''}</td></tr>`).join('')}</table></div>
  <div class="blk" id="bQ" style="top:42px"><h3>request_quote · <span class="mono" style="font-size:15px">${q.id}</span></h3>
    <div class="sub">Live price and stock for the shortlist only. Valio API: ${live ? `${live} calls` : `0 new calls, ${calls.cache_hits} answers from the short-lived cache`} · <span class="hl" id="hX">fixed price until ${hm(q.expires_at)}</span></div>
    <div class="qline"><span><b>${esc(line.name)}</b> <span class="chip">${esc(line.logistic_model)}</span></span><span>${line.quantity.toLocaleString('en-IE')} × ${eur(line.unit_price_ex_vat)} = <b>${eur(line.line_total_ex_vat)}</b></span></div>
    <div class="facts">
      <div class="fact" id="f0"><div class="k">Stock</div><div class="v">${esc(line.stock_level)} · order by ${dayhm(line.order_cutoff)}</div></div>
      <div class="fact" id="f1"><div class="k">Delivery reliability</div><div class="v">${Math.round(line.delivery_reliability * 100)} % delivered in full</div></div>
      <div class="fact" id="f2"><div class="k">Origin</div><div class="v">${esc(line.origin_name)} <span style="color:var(--ink-60);font-weight:400">(supplier-declared)</span></div></div>
      <div class="fact" id="f3"><div class="k">Label</div><div class="v">Blue Swan · Goods from Finland</div></div>
      <div class="fact" id="f4"><div class="k">CO₂ estimate</div><div class="v">~${Math.round(line.co2_estimate_kg).toLocaleString('en-IE')} kg · range over ${line.co2_range_kg.options} options</div></div>
      <div class="fact" id="f5"><div class="k">Lactose free</div><div class="v">claim checked · ${esc(line.content_per_sales_unit.amount)} L packs</div></div>
    </div>
    <div class="skip" id="sk">${skipped.concat(typed).map((a, i) => `<div class="s" id="s${i}"><span class="n">${esc(a.name)}</span> · <span ${i === 0 ? 'class="hl" id="hC"' : ''}>${esc(shortWhy(a.why_not))}</span></div>`).join('')}</div>
    <div class="sig" id="sig"><span>✓</span><span><b id="sigTxt">Signed quote</b> · <span class="hl" id="hG">checked here against the gateway's public key</span> · <span class="mono">${esc(qe.result.jws.slice(0, 26))}…</span></span></div>
  </div>
  <div class="blk" id="bO" style="top:385px"><div class="ord"><div class="top"><span class="t">place_order · ${oe.id}</span>${tierBadge('notify', 'Auto + FYI')}</div>
    <div class="f">${esc((oe.decision.findings.find(f => f.rule === 'spend.notify') || oe.decision.findings[0]).message)}</div>
    <div class="r">spend.notify · one order per quote · receipt signed (Ed25519) · on account, invoiced as today</div></div></div>`;
  window.READY = verify(qe.result.jws).then(ok => { $('sigTxt').textContent = ok ? 'Signature valid' : 'Signature NOT valid'; window.SIG_OK = ok; });
  window.render = t => {
    bar(t); term(t);
    $('ctr').textContent = t < T1 + qa + 0.6 ? 'Valio API calls: 0' : `Valio API calls: ${live}${live ? '' : ` · cache hits: ${calls.cache_hits}`}`;
    const b0 = $('b0'); show(b0, t, 1.2, 0.5); hide(b0, t, 4.4);
    const bS = $('bS'); show(bS, t, 4.7); hide(bS, t, T1 + qa + 0.2);
    sr.products.slice(0, 8).forEach((p, i) => { const r = $('r' + i); if (r) r.style.opacity = ease((t - 4.9 - i * 0.07) / 0.3); });
    hl($('hS'), (t - (T1 + 1.6)) / 0.8);
    const bQ = $('bQ'); show(bQ, t, T1 + qa + 0.5);
    for (let i = 0; i < 6; i++) show($('f' + i), t, T1 + fa + i * 0.45, 0.3, 6);
    document.querySelectorAll('.s').forEach((s, i) => { s.style.opacity = ease((t - (T1 + ka + i * 0.3)) / 0.3) * (1 - ease((t - T2) / 0.4)); });
    if ($('hC')) hl($('hC'), (t - (T1 + ka + 0.6)) / 1.0);
    $('sk').style.height = t < T2 ? '' : `${Math.max(0, 1 - ease((t - T2) / 0.4)) * 80}px`;
    show($('sig'), t, T2 + 0.2); hl($('hG'), (t - (T2 + 0.6)) / 0.8); hl($('hX'), (t - (T2 + 1.6)) / 0.7);
    show($('bO'), t, T2 + oa + 0.8);
  };
}

// --------------------------------------------------------------------------------------------- purpose
function purpose() {
  const c1 = num('c1', 7.6), c2 = num('c2', 9.6), c3 = num('c3', 12.4), held = num('held', 14.6);
  window.DURATION = num('dur', 17);
  const subs = S.events.filter(e => e.scene === 'purpose' && e.tool === 'suggest_substitutes');
  const oq = ev('purpose', 'request_quote').result.quote, oo = ev('purpose', 'place_order').result;
  const user = S.events.find(e => e.scene === 'purpose' && e.user).user;
  const bar = frame(() => 4);
  const term = termLines([
    [0.4, 'dim', `connected to ${S.server} (Streamable HTTP)`],
    [0.8, 'prompt', user, 2.8],
    [c1 - 0.6, 'call', `● suggest_substitutes(463442, purpose="coffee")`],
    [c2 - 0.6, 'call', `● suggest_substitutes(464194, purpose="recipe: panna cotta")`],
    [c3 - 0.6, 'call', `● suggest_substitutes(464194, purpose="allergy menu")`],
    [held - 0.9, 'call', `● request_quote(464699, substitute_for="464194", purpose="recipe: panna cotta")`],
    [held - 0.2, 'call', `● place_order(quote_id="${oq.id}", …)`],
    [held + 0.5, 'warn', `  └ ${oo.id} · held for the buyer · purpose.ask`],
  ]);
  const col = (r, title, use) => {
    const lvl = r.constraints.substitution;
    const body = (r.options || []).length
      ? r.options.map(o => `<div class="opt"><span class="tg">${o.tier}</span>${esc(o.name)}</div>`).join('') + (r.buyer_decides ? `<div class="bd"><b>Buyer decides.</b> Any substitute here needs the buyer's yes <span class="mono" style="font-size:10.5px">(purpose.ask)</span></div>` : `<div class="bd" style="background:#eef5ef;border-color:#2e6b3c">Any brand is fine: the mandate's normal substitute rule applies.</div>`)
      : `<div class="none">No substitutes.</div><div class="opt" style="border:0">${esc(r.no_safe_substitute.replace(/^the buyer/, 'The buyer'))}.</div><div class="opt">0 Valio API calls</div>`;
    return `<div class="p">${esc(r.original.name || r.original.product_id)}</div><div class="use">for ${esc(use)}</div><span class="lvl ${lvl}">substitution: ${lvl}</span>${body}`;
  };
  $('ab').innerHTML = `<div class="fb" id="fb">From Valio Aimo's feedback: <b>the customer stays in control</b>, and <b>what a product is for decides whether a swap is fine</b>.</div>
    <div class="cols"><div class="col" id="k0">${col(subs[0].result, 'Coffee cream', 'coffee')}</div><div class="col" id="k1">${col(subs[1].result, 'Whipping cream', 'recipe: panna cotta')}</div><div class="col" id="k2">${col(subs[2].result, 'Whipping cream', 'the allergy menu')}</div></div>
    <div class="toast" id="tst">${tierBadge('ask', 'Needs approval')}<span><b>${oo.id}</b> held for the buyer's chef · decide by ${dayhm(oo.approve_by)} (Valio's cut-off) · the approval link goes to the buyer's side</span></div>`;
  window.READY = Promise.resolve();
  window.render = t => {
    bar(t); term(t);
    $('ctr').textContent = 'suggest_substitutes · purpose';
    show($('fb'), t, 1.0, 0.5);
    [c1, c2, c3].forEach((c, i) => show($('k' + i), t, c, 0.4, 14));
    show($('tst'), t, held + 0.6, 0.4, 14);
  };
}

async function verify(jws) {
  try {
    const [h, p, sig] = jws.split('.');
    const k = S.jwks.keys[0];
    const key = await crypto.subtle.importKey('jwk', { kty: k.kty, crv: k.crv, x: k.x }, { name: 'Ed25519' }, false, ['verify']);
    const b = s => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(s.length / 4) * 4, '=')), c => c.charCodeAt(0));
    return await crypto.subtle.verify({ name: 'Ed25519' }, key, b(sig), new TextEncoder().encode(h + '.' + p));
  } catch (e) { console.error(e); return false; }
}
