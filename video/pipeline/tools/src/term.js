// Terminal-window replay of the real MCP session (window.SESSION). Deterministic: render(t) only.
// ?scene=milk (b2,b3,b4 + anchors qa,fa,ka,oa)  |  ?scene=purpose (c1,c2,c3,held,dur)
const S = window.SESSION;
const ev = (scene, tool, n = 0) => S.events.filter(e => e.scene === scene && e.tool === tool)[n];
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const num = (name, d) => { const v = parseFloat(Q.get(name)); return isNaN(v) ? d : v; };
const host = S.base.replace(/^https?:\/\//, '');

// JSON with colours: keys, strings, numbers, literals
function jc(s) {
  return esc(s).replace(/(&quot;(?:[^&]|&(?!quot;))*?&quot;)(\s*:)?|\b(-?\d+(?:\.\d+)?)\b|\b(true|false|null)\b/g, (m, str, colon, n, lit) => {
    if (str) return colon ? `<span class="k">${str}</span>${colon}` : `<span class="s">${str}</span>`;
    if (n) return `<span class="n">${n}</span>`;
    return `<span class="l">${lit}</span>`;
  });
}
const J = o => JSON.stringify(o);

function build(lines, hls) {
  document.body.innerHTML = `<div class="tw"><div class="tt"><i style="background:#ff5f56"></i><i style="background:#ffbd2e"></i><i style="background:#27c93f"></i>
    <span>${Q.get("title") || `hotel-aurora-agent — MCP session — ${esc(host)}/mcp`}</span></div><div class="view" id="view"><div class="lines" id="lines"></div></div></div>`;
  $('lines').innerHTML = lines.map((l, i) => `<div class="ln ${l.c || ''}" id="L${i}"><span class="tx" id="T${i}">${l.h}</span></div>`).join('');
  const LH = 22.5, VIEW = 720 - 30 - 100;  // keep the bottom 100 px for captions
  return t => {
    let count = 0;
    lines.forEach((l, i) => {
      const el = $('L' + i), a = ease((t - l.t) / 0.12);
      el.style.opacity = a > 0 ? 1 : 0;
      if (l.type) {  // typed line
        const n = Math.round(clamp((t - l.t) / l.type) * l.raw.length);
        $('T' + i).innerHTML = `<span class="pr">${l.prefix || 'agent&gt;'}</span> ${esc(l.raw.slice(0, n))}${t >= l.t && n < l.raw.length ? '<span class="cur"></span>' : ''}`;
      }
      count += a * (el.offsetHeight / LH);
    });
    const off = Math.max(0, count * LH - VIEW);
    $('lines').style.transform = `translateY(${-off}px)`;
    if (!Q.get('nohl')) hls.forEach(([i, t0]) => { const el = $('T' + i); if (el) el.style.backgroundSize = `${(clamp((t - t0) / 0.7) * 100).toFixed(1)}% 100%`; });
  };
}

function milk() {
  const b2 = num('b2', 6), b3 = num('b3', 19), b4 = num('b4', 10.7), T1 = b2, T2 = b2 + b3;
  const qa = num('qa', 7), fa = num('fa', 12), ka = num('ka', 15.8), oa = num('oa', 4.7);
  window.DURATION = b2 + b3 + b4;
  const sr = ev('milk', 'search_products'), qe = ev('milk', 'request_quote'), q = qe.result.quote, oe = ev('milk', 'place_order').result;
  const line = q.lines[0], cut = (q.alternatives_considered || []).find(a => (a.why_not || []).some(w => /cut-off/.test(w)));
  const L = [], H = [];
  const add = (t, h, c, extra = {}) => { L.push({ t, h, c, ...extra }); return L.length - 1; };
  add(0.2, `<span class="dim">connected · server <b>${esc(S.server)}</b> · ${S.tools ? S.tools.length + ' tools' : 'MCP'} · Streamable HTTP · mandate ${esc(S.mandate)}</span>`, '');
  add(0.6, '', 'gap');
  const user = S.events.find(e => e.scene === 'milk' && e.user).user;
  add(0.9, '', 'prompt', { type: 2.4, raw: user });
  add(4.0, `<span class="ar">→</span> <span class="fn">search_products</span> ${jc(J(sr.args))}`, 'call');
  const i0 = add(4.6, `<span class="ar2">←</span> ${sr.result.products.length} products · local catalog index · <span class="hlt">0 Valio API calls</span>`, 'res');
  sr.result.products.slice(0, 5).forEach((p, k) => add(4.7 + k * 0.05, `<span class="dim">   ${p.id}  </span>${esc(p.name)}`, 'res'));
  add(4.95, `<span class="dim">   … ${sr.result.products.length - 5} more</span>`, 'res');
  H.push([i0, T1 + 2.0]);
  const qa_args = { ...qe.args, items: qe.args.items.map(it => ({ ...it, product_ids: `[${it.product_ids.length} ids]` })) };
  add(T1 + qa, `<span class="ar">→</span> <span class="fn">request_quote</span> ${jc(J(qa_args)).replace(/&quot;\[(\d+) ids\]&quot;/, '[$1 ids]')}`, 'call');
  const t1 = T1 + qa + 0.6;
  const J2 = (k, v, ind = 4) => `${' '.repeat(ind)}${jc(J(k))}: ${jc(J(v))},`;
  add(t1, `<span class="ar2">←</span> {`, 'res');
  add(t1 + 0.03, J2('id', q.id, 2), 'res');
  const iExp = add(t1 + 0.06, J2('expires_at', q.expires_at, 2), 'res');
  const iCalls = add(t1 + 0.09, J2('valio_api_calls', q.valio_api_calls, 2), 'res');
  add(t1 + 0.12, `  ${jc(J('lines'))}: [{`, 'res');
  add(t1 + 0.15, J2('name', line.name), 'res');
  add(t1 + 0.18, `    ${jc(J('quantity'))}: ${jc(J(line.quantity))}, ${jc(J('unit_price_ex_vat'))}: ${jc(J(line.unit_price_ex_vat))}, ${jc(J('line_total_ex_vat'))}: ${jc(J(line.line_total_ex_vat))},`, 'res');
  add(t1 + 0.21, `    ${jc(J('stock_level'))}: ${jc(J(line.stock_level))}, ${jc(J('order_cutoff'))}: ${jc(J(line.order_cutoff))},`, 'res');
  const f = [J2('delivery_reliability', line.delivery_reliability), J2('origin_name', line.origin_name), J2('labels', line.labels), J2('co2_estimate_kg', line.co2_estimate_kg)]
    .map((h, k) => add(t1 + 0.24 + k * 0.03, h, 'res'));
  add(t1 + 0.36, `  }],`, 'res');
  let iCut = -1;
  if (cut) {
    add(t1 + 0.39, `  ${jc(J('alternatives_considered'))}: [{ ${jc(J('name'))}: ${jc(J(cut.name))},`, 'res');
    iCut = add(t1 + 0.42, `    ${jc(J('why_not'))}: ${jc(J(cut.why_not.find(w => /cut-off/.test(w))))} }, …],`, 'res');
  }
  add(t1 + 0.45, `  ${jc(J('mandate_preview'))}: { ${jc(J('outcome'))}: ${jc(J((q.mandate_preview || {}).outcome))}, … }`, 'res');
  add(t1 + 0.48, `}`, 'res');
  const iSig = add(t1 + 0.6, `<span class="dim">jws</span> ${esc(JW(qe.result.jws).slice(0, 44))}…  <span class="ok" id="sigok">✓ Ed25519 signature valid · kid ${esc(S.jwks.keys[0].kid)}</span>`, 'res');
  H.push([iCalls, T1 + qa + 2.4]);
  f.forEach((i, k) => H.push([i, T1 + fa + k * 0.7]));
  if (iCut >= 0) H.push([iCut, T1 + ka]);
  H.push([iSig, T2 + 0.6]); H.push([iExp, T2 + 1.8]);
  add(T2 + oa, `<span class="ar">→</span> <span class="fn">place_order</span> ${jc(J({ quote_id: q.id, idempotency_key: '…' }))}`, 'call');
  const fnd = oe.decision.findings.find(x => x.rule === 'spend.notify') || oe.decision.findings[0];
  const iO = add(T2 + oa + 0.6, `<span class="ar2">←</span> ${jc(J({ id: oe.id, status: oe.status, decision: { outcome: oe.decision.outcome, findings: [{ rule: fnd.rule, message: fnd.message }] } }))}`, 'res');
  H.push([iO, T2 + oa + 1.0]);
  add(T2 + oa + 0.65, `<span class="dim">receipt_jws</span> ${esc(JW(oe.receipt_jws).slice(0, 44))}…  <span class="ok">✓ signed receipt</span>`, 'res');
  const r = build(L, H);
  window.READY = verify(qe.result.jws).then(ok => { if (!ok) $('sigok').textContent = '✗ signature NOT valid'; });
  window.render = r;
}

// The panna cotta story (beats 2-4): the usual cream can't make Monday -> substitutes for a recipe -> signed quote -> held.
// ?b2=&b3=&b4= (beat lengths) and anchors inside them: qa (quote on the usual, in b3), ka (cut-off highlight, in b3),
// sa (substitutes, in b4), pa (quote on the pick), fa (facts), oa (place_order)
function cream() {
  const b2 = num('b2', 5.8), b3 = num('b3', 16.4), b4 = num('b4', 12.4), T1 = b2, T2 = b2 + b3;
  const qa = num('qa', 7.4), ka = num('ka', 12.4), sa = num('sa', 0.1), pa = num('pa', 2.6), fa = num('fa', 7.6), oa = num('oa', 10.0);
  window.DURATION = b2 + b3 + b4;
  const sc = 'cream';
  const sr = ev(sc, 'search_products'), q1e = ev(sc, 'request_quote', 0), sb = ev(sc, 'suggest_substitutes'), q2e = ev(sc, 'request_quote', 1);
  const oe = ev(sc, 'place_order').result, q1 = q1e.result.quote, q2 = q2e.result.quote;
  const L = [], H = [];
  const add = (t, h, c, extra = {}) => { L.push({ t, h, c, ...extra }); return L.length - 1; };
  const J2 = (k, v, ind = 4) => `${' '.repeat(ind)}${jc(J(k))}: ${jc(J(v))},`;
  add(0.2, `<span class="dim">connected · server <b>${esc(S.server)}</b> · ${S.tools ? S.tools.length + ' tools' : 'MCP'} · Streamable HTTP · mandate ${esc(S.mandate)}</span>`, '');
  add(0.6, '', 'gap');
  add(0.9, '', 'prompt', { type: 2.6, raw: S.events.find(e => e.scene === sc && e.user).user });
  add(4.0, `<span class="ar">→</span> <span class="fn">search_products</span> ${jc(J({ query: sr.args.query, limit: sr.args.limit }))}`, 'call');
  const i0 = add(4.6, `<span class="ar2">←</span> ${sr.result.products.length} products · local catalog index · <span class="hlt">0 Valio API calls</span>`, 'res');
  sr.result.products.slice(0, 5).forEach((p, k) => add(4.7 + k * 0.05, `<span class="dim">   ${p.id}  </span>${esc(p.name)}`, 'res'));
  H.push([i0, T1 + 2.2]);
  // quote on the usual cream
  const it1 = q1e.args.items[0];
  add(T1 + qa, `<span class="ar">→</span> <span class="fn">request_quote</span> ${jc(J({ delivery_date: q1e.args.delivery_date, items: [{ product_ids: it1.product_ids, amount: it1.amount, unit: it1.unit, purpose: it1.purpose }] }))}`, 'call');
  const l1 = q1.lines[0], t1 = T1 + qa + 0.6;
  const issues = (l1.issues || []).map(i => typeof i === 'string' ? i : i.code);
  const msg = (l1.issues || []).map(i => typeof i === 'string' ? '' : (i.message || '')).find(m => /cut-off/i.test(m)) || '';
  add(t1, `<span class="ar2">←</span> {`, 'res');
  add(t1 + 0.03, J2('id', q1.id, 2), 'res');
  add(t1 + 0.06, `  ${jc(J('lines'))}: [{ ${jc(J('name'))}: ${jc(J(l1.name))},`, 'res');
  const iOrd = add(t1 + 0.09, `    ${jc(J('orderable'))}: ${jc(J(l1.orderable))}, ${jc(J('issues'))}: ${jc(J(issues))},`, 'res');
  let iMsg = -1;
  if (msg) iMsg = add(t1 + 0.12, J2('message', msg), 'res');
  add(t1 + 0.15, `  }],`, 'res');
  add(t1 + 0.18, `  ${jc(J('options'))}: [${(q1.options || []).length} × ${jc(J('substitute'))}, …],  ${jc(J('mandate_preview'))}: { ${jc(J('outcome'))}: ${jc(J((q1.mandate_preview || {}).outcome))} }`, 'res');
  add(t1 + 0.21, `}`, 'res');
  H.push([iOrd, T1 + ka]); if (iMsg >= 0) H.push([iMsg, T1 + ka + 0.8]);
  // Valio's substitutes for a recipe
  add(T2 + sa, `<span class="ar">→</span> <span class="fn">suggest_substitutes</span> ${jc(J({ product_id: sb.args.product_id, purpose: sb.args.purpose }))}`, 'call');
  add(T2 + sa + 0.5, `<span class="ar2">←</span> ${jc(J('options'))}: ${jc(J(sb.result.options.map(o => o.name)))}`, 'res');
  const iBd = add(T2 + sa + 0.55, `  ${jc(J('buyer_decides'))}: ${jc(J(sb.result.buyer_decides))}`, 'res');
  H.push([iBd, T2 + sa + 1.0]);
  // signed quote on the pick
  const it2 = q2e.args.items[0], l2 = q2.lines[0], t2 = T2 + pa + 0.6;
  add(T2 + pa, `<span class="ar">→</span> <span class="fn">request_quote</span> ${jc(J({ items: [{ product_ids: it2.product_ids, amount: it2.amount, unit: it2.unit, substitute_for: it2.substitute_for, purpose: it2.purpose }] }))}`, 'call');
  add(t2, `<span class="ar2">←</span> {`, 'res');
  add(t2 + 0.03, J2('id', q2.id, 2), 'res');
  const iExp = add(t2 + 0.06, J2('expires_at', q2.expires_at, 2), 'res');
  add(t2 + 0.09, `  ${jc(J('lines'))}: [{ ${jc(J('name'))}: ${jc(J(l2.name))},`, 'res');
  const iPrice = add(t2 + 0.12, `    ${jc(J('quantity'))}: ${jc(J(l2.quantity))}, ${jc(J('unit_price_ex_vat'))}: ${jc(J(l2.unit_price_ex_vat))}, ${jc(J('line_total_ex_vat'))}: ${jc(J(l2.line_total_ex_vat))},`, 'res');
  const f = [J2('delivery_reliability', l2.delivery_reliability), J2('origin_name', l2.origin_name), J2('co2_estimate_kg', l2.co2_estimate_kg)]
    .map((h, k) => add(t2 + 0.15 + k * 0.03, h, 'res'));
  add(t2 + 0.24, `    ${jc(J('substitute_for'))}: ${jc(J(l2.substitute_for))}, ${jc(J('purpose'))}: ${jc(J(l2.purpose))}, ${jc(J('substitution'))}: ${jc(J(l2.substitution))} }],`, 'res');
  add(t2 + 0.27, `  ${jc(J('mandate_preview'))}: { ${jc(J('outcome'))}: ${jc(J((q2.mandate_preview || {}).outcome))}, … }`, 'res');
  add(t2 + 0.3, `}`, 'res');
  const iSig = add(t2 + 0.4, `<span class="dim">jws</span> ${esc(JW(q2e.result.jws).slice(0, 44))}…  <span class="ok" id="sigok">✓ Ed25519 signature valid · kid ${esc(S.jwks.keys[0].kid)}</span>`, 'res');
  H.push([iPrice, T2 + pa + 2.2]); H.push([iExp, T2 + pa + 2.9]); H.push([iSig, T2 + pa + 3.6]);
  f.forEach((i, k) => H.push([i, T2 + fa + k * 0.7]));
  // order: held for the chef
  add(T2 + oa, `<span class="ar">→</span> <span class="fn">place_order</span> ${jc(J({ quote_id: q2.id, idempotency_key: '…' }))}`, 'call');
  const use = oe.decision.findings.find(x => x.rule === 'substitution.use_sensitive') || oe.decision.findings.find(x => x.rule === 'purpose.ask');
  const iO = add(T2 + oa + 0.6, `<span class="ar2">←</span> ${jc(J({ id: oe.id, status: oe.status, decision: { outcome: oe.decision.outcome, rules: oe.decision.findings.map(x => x.rule) } }))}`, 'res');
  const iU = add(T2 + oa + 0.65, `  ${jc(J(use.rule))}: ${jc(J(use.message))}`, 'res');
  H.push([iO, T2 + oa + 0.9]); H.push([iU, T2 + oa + 1.4]);
  window.render = build(L, H);
  window.READY = verify(q2e.result.jws).then(ok => { if (!ok) $('sigok').textContent = '✗ signature NOT valid'; });
}

function purpose() {
  const c1 = num('c1', 7.5), c2 = num('c2', 9.6), c3 = num('c3', 12.8), held = num('held', 15.4);
  window.DURATION = num('dur', 17);
  const subs = S.events.filter(e => e.scene === 'purpose' && e.tool === 'suggest_substitutes');
  const qe = ev('purpose', 'request_quote'), oo = (ev('purpose', 'place_order') || {}).result;
  const user = S.events.find(e => e.scene === 'purpose' && e.user).user;
  const L = [], H = [];
  const add = (t, h, c, extra = {}) => { L.push({ t, h, c, ...extra }); return L.length - 1; };
  add(0.2, `<span class="dim">connected · server <b>${esc(S.server)}</b> · mandate ${esc(S.mandate)}</span>`, '');
  add(0.4, '', 'gap');
  add(0.7, '', 'prompt', { type: 2.6, raw: user });
  const show = (t0, e) => {
    const r = e.result, a = e.args;
    add(t0 - 1.2, `<span class="ar">→</span> <span class="fn">suggest_substitutes</span> ${jc(J({ product_id: a.product_id, purpose: a.purpose }))}`, 'call');
    add(t0 - 0.6, `<span class="ar2">←</span> <span class="dim">${esc(r.original.name)}</span>`, 'res');
    const i = add(t0 - 0.55, `  ${jc(J('substitution'))}: ${jc(J(r.constraints.substitution))}, ${jc(J('purpose'))}: ${jc(J(r.constraints.purpose))}`, 'res');
    H.push([i, t0]);
    if ((r.options || []).length) {
      add(t0 - 0.5, `  ${jc(J('options'))}: ${jc(J(r.options.map(o => o.name)))}`, 'res');
      if (r.buyer_decides) { const j = add(t0 - 0.45, `  ${jc(J('buyer_decides'))}: ${jc(J(r.buyer_decides))}`, 'res'); H.push([j, t0 + 0.9]); }
    } else {
      add(t0 - 0.5, `  ${jc(J('options'))}: [],  ${jc(J('valio_api_calls'))}: 0`, 'res');
      const j = add(t0 - 0.45, `  ${jc(J('no_safe_substitute'))}: ${jc(J(r.no_safe_substitute))}`, 'res'); H.push([j, t0 + 0.9]);
    }
  };
  show(c1, subs[0]); show(c2, subs[1]); show(c3, subs[2]);
  if (!qe || !oo) { window.render = build(L, H); window.READY = Promise.resolve(); return; }  // order shown in the cream scene
  const it = qe.args.items[0];
  add(held - 1.0, `<span class="ar">→</span> <span class="fn">request_quote</span> ${jc(J({ items: [{ product_ids: it.product_ids, amount: it.amount, substitute_for: it.substitute_for, purpose: it.purpose }] }))}`, 'call');
  add(held - 0.5, `<span class="ar">→</span> <span class="fn">place_order</span> ${jc(J({ quote_id: qe.result.quote.id, idempotency_key: '…' }))}`, 'call');
  const k = add(held, `<span class="ar2">←</span> ${jc(J({ id: oo.id, status: oo.status, decision: { outcome: oo.decision.outcome, rules: oo.decision.findings.map(x => x.rule) }, approve_by: oo.approve_by }))}`, 'res');
  const u = add(held + 0.05, `  ${jc(J('approval_url'))}: ${jc(J(oo.approval_url))}`, 'res');
  H.push([k, held + 0.5]); H.push([u, held + 1.2]);
  window.render = build(L, H);
  window.READY = Promise.resolve();
}

function open() {
  window.DURATION = num('dur', 5.6);
  const o = S.open, c2 = num('c2', 2.4);
  const L = [], H = [];
  const add = (t, h, c, extra = {}) => { L.push({ t, h, c, ...extra }); return L.length - 1; };
  add(0.0, '', 'prompt', { type: 0.6, raw: `curl -s ${S.base}/.well-known/mcp/server-card.json | jq '{name, title, version, remotes: [.remotes[] | {type, url}]}'`, prefix: '~ $' });
  const card = JSON.stringify({ name: o.card.name, title: o.card.title, version: o.card.version, remotes: o.card.remotes.map(r => ({ type: r.type, url: r.url })) }, null, 2).split('\n');
  const i0 = card.length;
  card.forEach((ln, k) => { const j = add(0.75 + k * 0.02, jc(ln), 'res'); if (/"(title|type)"/.test(ln)) H.push([j, 1.0 + k * 0.03]); });
  add(c2 - 0.1, '', 'gap');
  add(c2, '', 'prompt', { type: 0.6, raw: `curl -s -H "Authorization: Bearer $AGENT_TOKEN" ${o.peppol_url} | head -8`, prefix: '~ $' });
  o.peppol_head.slice(0, 8).forEach((ln, k) => { const j = add(c2 + 0.75 + k * 0.02, `<span class="s">${esc(ln)}</span>`, 'res'); if (/CustomizationID|ProfileID/.test(ln)) H.push([j, c2 + 1.0 + k * 0.05]); });
  window.render = build(L, H);
  window.READY = Promise.resolve();
}

const JW = x => Array.isArray(x) ? x.join('.') : (x || '');  // published data stores each JWS as [header, payload, signature]
async function verify(jws) {
  jws = JW(jws);
  try {
    const [h, p, sig] = jws.split('.');
    const k = S.jwks.keys[0];
    const key = await crypto.subtle.importKey('jwk', { kty: k.kty, crv: k.crv, x: k.x }, { name: 'Ed25519' }, false, ['verify']);
    const b = s => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(s.length / 4) * 4, '=')), c => c.charCodeAt(0));
    return await crypto.subtle.verify({ name: 'Ed25519' }, key, b(sig), new TextEncoder().encode(h + '.' + p));
  } catch (e) { return false; }
}
