import assert from 'node:assert/strict';
import { createHash, generateKeyPairSync, sign } from 'node:crypto';
import test from 'node:test';

import { Gateway, gatewayUrl, verifyJws, verifyProofBundle } from './gateway.mjs';
import { helsinkiDate, parseIntent } from './intent.mjs';
import { quoteCoverage } from './coverage.mjs';

const { privateKey, publicKey } = generateKeyPairSync('ed25519');
const jwks = { keys: [{ ...publicKey.export({ format: 'jwk' }), kid: 'fixture', use: 'sig' }] };
const encode = (data) => Buffer.from(JSON.stringify(data)).toString('base64url');
function token(payload, type, prefix = 'valtakirja') {
  const input = `${encode({ alg: 'EdDSA', typ: `${prefix}-${type}+jws`, kid: 'fixture' })}.${encode(payload)}`;
  return `${input}.${sign(null, Buffer.from(input), privateKey).toString('base64url')}`;
}

test('turns the hotel request into editable fields using the Helsinki date', () => {
  const today = helsinkiDate(new Date('2026-11-13T22:30:00Z'));
  assert.equal(today, '2026-11-14');
  assert.deepEqual(parseIntent('I need 1,000 litres of lactose-free milk by Monday.', today), {
    query: 'milk', amount: 1000, unit: 'L', deliveryDate: '2026-11-16', claims: ['FREE_FROM_LACTOSE'],
  });
  assert.equal(parseIntent('350 g of oats by tomorrow', today).amount, 0.35);
  assert.throws(() => parseIntent('1,25 litres of milk by Monday', today));
  assert.throws(() => parseIntent('2 packs of oats by 2026-11-31', today));
});

test('only accepts HTTP(S) gateway base URLs', () => {
  assert.equal(gatewayUrl('https://example.test/vendor/'), 'https://example.test/vendor');
  assert.throws(() => gatewayUrl('https://user:pass@example.test'));
  assert.throws(() => gatewayUrl('ftp://example.test'));
});

test('verifies Ed25519 JWS, rejecting changed payloads, a wrong type and ambiguous keys', async () => {
  const payload = { id: 'Q-1', total_ex_vat: 1 };
  const signed = token(payload, 'quote');
  assert.deepEqual(await verifyJws(signed, jwks, 'valtakirja-quote+jws'), payload);
  const [header, , sig] = signed.split('.');
  await assert.rejects(verifyJws(`${header}.${encode({ id: 'Q-2' })}.${sig}`, jwks, 'valtakirja-quote+jws'));
  await assert.rejects(verifyJws(signed, jwks, 'valtakirja-mandate+jws'));
  await assert.rejects(verifyJws(signed, { keys: [...jwks.keys, ...jwks.keys] }, 'valtakirja-quote+jws'));
});

const need = { query: 'oats', amount: 5, unit: 'KG', deliveryDate: '2026-11-16', claims: ['VEGAN'], avoidAllergens: ['AM'] };
const mandate = { id: 'M-1', agent_id: 'agent', buyer: 'Kitchen', required_claims: [], avoid_allergens: ['AN'] };
const quote = {
  id: 'Q-1', agent_id: mandate.agent_id, mandate_id: mandate.id, total_ex_vat: 19,
  requested_delivery_date: need.deliveryDate, expires_at: new Date(Date.now() + 60_000).toISOString(),
  mandate_preview: { outcome: 'ask' }, lines: [{ product_id: 'sku', claims: ['VEGAN'] }],
};

test('accepts both gateway namespaces without accepting a different signed document kind', async () => {
  for (const prefix of ['sous', 'valtakirja']) {
    const payload = {
      id: 'PO-1', quote_id: quote.id, mandate_id: mandate.id, total_ex_vat: quote.total_ex_vat,
      lines: [], decision: { outcome: 'ask' }, status: 'ordered',
      approval: { by: 'Demo approver', approved: true }, updated_at: '2026-11-14T00:00:00+02:00',
    };
    const order = { ...payload, agent_id: mandate.agent_id, receipt_jws: token(payload, 'order', prefix) };
    const responses = {
      '/mandates/M-1': { mandate, jws: token(mandate, 'mandate', prefix) },
      '/quotes/Q-1': { quote, jws: token(quote, 'quote', prefix) },
      '/.well-known/jwks.json': jwks,
    };
    const client = new Gateway('https://example.test', async (url) => Response.json(responses[new URL(url).pathname]));
    assert.deepEqual(await client.mandate(mandate.id), mandate);
    assert.deepEqual(await client.getQuote(quote.id), quote);
    assert.deepEqual(await client.receipt(order, quote), payload);
    await assert.rejects(client.receipt({ ...order, receipt_jws: token(payload, 'quote', prefix) }, quote));
    await assert.rejects(client.receipt({ ...order, receipt_jws: token(payload, 'order', 'other') }, quote));
  }
});

test('sends an agent token to the selected gateway and omits it from key discovery', async () => {
  const requests = [];
  const client = new Gateway('https://example.test', async (url, init) => {
    requests.push({ url, init });
    return Response.json({});
  }, 'test-token');
  await client.request('/orders/PO-1');
  await client.request('/quotes', {});
  await client.request('/.well-known/jwks.json');
  assert.deepEqual(requests.map(({ init }) => init.headers.Authorization), ['Bearer test-token', 'Bearer test-token', undefined]);
  assert.ok(requests.every(({ url, init }) => new URL(url).origin === 'https://example.test' && init.redirect === 'error'));
  assert.throws(() => new Gateway('http://example.test', undefined, 'test-token'), /HTTPS gateway/);
});

test('uses signed request identities, selected SKUs, catalog filters and one idempotency key', async () => {
  const requests = [];
  const client = new Gateway('https://example.test', async (url, init) => {
    const parsed = new URL(url);
    const body = init.body && JSON.parse(init.body);
    requests.push({ url: parsed, body });
    const responses = {
      '/mandates/M-1': { mandate, jws: token(mandate, 'mandate') },
      '/.well-known/jwks.json': jwks,
      '/products': { products: [{ id: 'sku' }] },
      '/quotes': { quote, jws: token(quote, 'quote') },
      '/orders': { id: 'PO-1', status: 'pending_approval' },
    };
    return Response.json(responses[parsed.pathname]);
  });
  assert.deepEqual(await client.mandate(mandate.id), mandate);
  assert.deepEqual(await client.search(need, mandate), [{ id: 'sku' }]);
  assert.deepEqual(requests[2].url.searchParams.getAll('avoid_allergens'), ['AM', 'AN']);
  assert.deepEqual(requests[2].url.searchParams.getAll('claims'), ['VEGAN']);
  assert.deepEqual(await client.quote(need, mandate, ['sku']), quote);
  assert.deepEqual(requests[3].body.items[0], { product_ids: ['sku'], amount: 5, unit: 'KG', substitute_for: null, note: 'oats', product_type: 'oats' });
  assert.equal((await client.order(quote)).status, 'pending_approval');
  const expired = { ...quote, expires_at: '2000-01-01T00:00:00Z' };
  await assert.rejects(client.order(expired), /expired/);
  await client.order(expired, true);
  assert.deepEqual(requests.at(-1).body, requests.at(-2).body);
  await assert.rejects(client.order({ ...quote, mandate_preview: { outcome: 'block' } }), /blocks/);
  assert.equal(requests.filter(({ url }) => url.pathname === '/orders').length, 2);
});

test('rejects a quote whose JSON, signed identity, or product shortlist differs', async () => {
  for (const [display, signed, productIds] of [
    [{ ...quote, total_ex_vat: 1 }, quote, ['sku']],
    [{ ...quote, mandate_id: 'other' }, { ...quote, mandate_id: 'other' }, ['sku']],
    [quote, quote, ['different-sku']],
  ]) {
    const client = new Gateway('https://example.test', async (url) => Response.json(
      url.endsWith('jwks.json') ? jwks : { quote: display, jws: token(signed, 'quote') },
    ));
    await assert.rejects(client.quote(need, mandate, productIds));
  }
});

test('rejects a signed quote that drops or changes the requested product type', async () => {
  for (const type of [{}, { product_type: null }, { product_type: 'milk' }]) {
    const items = [{ product_ids: ['sku'], amount: 5, unit: 'KG', substitute_for: null, note: 'oats', ...type }];
    const signed = { ...quote, items, lines: quote.lines.map((line) => ({ ...line, item_index: 0 })) };
    const client = new Gateway('https://example.test', async (url) => Response.json(
      url.endsWith('jwks.json') ? jwks : { quote: signed, jws: token(signed, 'quote') },
    ));
    await assert.rejects(client.quote(need, mandate, ['sku']), /signed quote does not match/);
  }
});

test('accepts a signed null product type for replacements and rejects an added constraint', async () => {
  for (const product_type of [null, 'milk']) {
    const items = [{ product_ids: ['sku'], amount: 5, unit: 'KG', substitute_for: 'old-sku', note: 'oats', product_type }];
    const signed = { ...quote, items, lines: quote.lines.map((line) => ({ ...line, item_index: 0, substitute_for: 'old-sku' })) };
    const client = new Gateway('https://example.test', async (url) => Response.json(
      url.endsWith('jwks.json') ? jwks : { quote: signed, jws: token(signed, 'quote') },
    ));
    const result = client.quote({ ...need, substituteFor: 'old-sku' }, mandate, ['sku']);
    if (product_type === null) assert.deepEqual(await result, signed);
    else await assert.rejects(result, /signed quote does not match/);
  }
});

test('keeps explicit request constraints when showing ranked substitutes', async () => {
  const option = { product_id: 'sku-2', offer: { temperature: 'chilled', claims: ['VEGAN'] }, allergen_check: { status: 'safe' } };
  const original = { product_id: 'sku-1' };
  const client = new Gateway('https://example.test', async (url) => {
    const parsed = new URL(url);
    assert.equal(parsed.pathname, '/products/sku-1/substitutes');
    assert.equal(parsed.searchParams.get('mandate_id'), mandate.id);
    assert.equal(parsed.searchParams.get('avoid_allergens'), 'AM');
    return Response.json({ original, constraints: { same_category: 'category-1' }, options: [
      option,
      { ...option, product_id: 'sku-3', offer: { ...option.offer, claims: [] } },
      { ...option, product_id: 'sku-4', allergen_check: { status: 'may_contain' } },
    ] });
  });
  assert.deepEqual(await client.substitutes({ ...need, substituteFor: 'sku-1', temperature: 'chilled' }, mandate), {
    original, options: [option],
  });
});

test('reports an original signed approval hold separately from the refreshed status', async () => {
  const payload = {
    id: 'PO-1', quote_id: quote.id, mandate_id: mandate.id, total_ex_vat: quote.total_ex_vat,
    lines: [], decision: { outcome: 'ask' }, status: 'pending_approval',
  };
  const client = new Gateway('https://example.test', async () => Response.json(jwks));
  const order = { ...payload, agent_id: mandate.agent_id, receipt_jws: token(payload, 'order') };
  assert.equal((await client.receipt(order, quote)).status, 'pending_approval');
  assert.equal((await client.receipt({ ...order, status: 'ordered', approval: { approved: true } }, quote)).status, 'pending_approval');
  await assert.rejects(client.receipt({ ...order, status: 'ordered' }, quote));
  await assert.rejects(client.receipt({ ...order, total_ex_vat: 5 }, quote));
});

test('requires the current status, approval details and update time to match a new receipt', async () => {
  const payload = {
    id: 'PO-2', quote_id: quote.id, mandate_id: mandate.id, total_ex_vat: quote.total_ex_vat,
    lines: [], decision: { outcome: 'ask' }, status: 'ordered',
    approval: { by: 'Demo approver', approved: true }, updated_at: '2026-11-13T21:45:00+02:00',
  };
  const client = new Gateway('https://example.test', async () => Response.json(jwks));
  const order = { ...payload, agent_id: mandate.agent_id, receipt_jws: token(payload, 'order') };
  assert.deepEqual(await client.receipt(order, quote), payload);
  await assert.rejects(client.receipt({ ...order, approval: { ...payload.approval, by: 'Someone else' } }, quote));
  await assert.rejects(client.receipt({ ...order, updated_at: null }, quote));
  const pendingToken = token({ ...payload, status: 'pending_approval', approval: null }, 'order');
  await assert.rejects(client.receipt({ ...order, receipt_jws: pendingToken }, quote));
});

test('reopens an order using its original signed quote and rejects mismatched IDs', async () => {
  const order = { id: 'PO-1', quote_id: quote.id };
  const responses = {
    '/orders/PO-1': order, '/orders/PO-2': order,
    '/quotes/Q-1': { quote, jws: token(quote, 'quote') },
    '/quotes/Q-2': { quote, jws: token(quote, 'quote') },
    '/.well-known/jwks.json': jwks,
  };
  const client = new Gateway('https://example.test', async (url, init) => {
    assert.equal(init.method, 'GET');
    return Response.json(responses[new URL(url).pathname]);
  });
  const reopened = await client.getOrder(order.id);
  assert.deepEqual(await client.getQuote(reopened.quote_id), quote);
  await assert.rejects(client.getOrder('PO-2'), /different order/);
  await assert.rejects(client.getQuote('Q-2'), /different signed quote/);
});

test('binds each signed basket line to its request and reports a named shortage with its own reasons', async () => {
  const rice = { ...need, query: 'rice', amount: 15, claims: [] };
  const entries = [{ need, productIds: ['sku'] }, { need: rice, productIds: ['rice-sku'] }];
  const items = [
    { note: 'oats', amount: 5, unit: 'KG', product_ids: ['sku'], substitute_for: null, product_type: 'oats' },
    { note: 'rice', amount: 15, unit: 'KG', product_ids: ['rice-sku'], substitute_for: null, product_type: 'rice' },
  ];
  const basketQuote = {
    ...quote, items,
    lines: [{ ...quote.lines[0], item_index: 0, orderable: true, quantity: 3,
      covers: { unit: 'KG', amount: 6 }, delivery_date: need.deliveryDate }],
    uncovered: [{ item: items[1], item_index: 1, uncovered: 15, unit: 'KG' }],
    alternatives_considered: [{ item_index: 1, orderable: false, why_not: ['Cut-off passed.'] }],
  };
  const client = new Gateway('https://example.test', async (url, init) => {
    if (url.endsWith('jwks.json')) return Response.json(jwks);
    assert.deepEqual(JSON.parse(init.body).items, items);
    return Response.json({ quote: basketQuote, jws: token(basketQuote, 'quote') });
  });
  const signed = await client.quoteBasket(need.deliveryDate, mandate, entries);
  const [oats, riceRow] = quoteCoverage(signed);
  assert.equal(oats.quoted, 6);
  assert.equal(oats.missing, 0);
  assert.equal(riceRow.quoted, 0);
  assert.equal(riceRow.missing, 15);
  assert.deepEqual(riceRow.reasons, ['Cut-off passed.']);
  assert.equal(riceRow.referenceSku, 'rice-sku');
  basketQuote.lines[0].item_index = 1;
  await assert.rejects(client.quoteBasket(need.deliveryDate, mandate, entries), /basket item/);
  await assert.rejects(client.quoteBasket(need.deliveryDate, mandate, [entries[0], entries[0]]), /only appear on one/);
});

test('shows a shortage when a single offer quotes more stock than it can deliver', () => {
  const item = { amount: 12, unit: 'SALES_UNIT', product_ids: ['sku'] };
  const [row] = quoteCoverage({
    ...quote, items: [item],
    lines: [{ item_index: 0, orderable: true, quantity: 12, available_amount: 3,
      delivery_date: need.deliveryDate, issues: [{ code: 'PARTIAL_STOCK', message: 'Only three packs available.' }] }],
  });
  assert.equal(row.quoted, 12);
  assert.equal(row.missing, 9);
  assert.deepEqual(row.reasons, ['Only three packs available.']);
});

test('reports all 15 kg of rice missing when the gateway returns blocked lines and no explicit gap', () => {
  const item = { note: 'rice', amount: 15, unit: 'KG', product_ids: ['rice-sku'] };
  const [row] = quoteCoverage({
    ...quote, items: [item], uncovered: [],
    lines: [{ item_index: 0, orderable: false, quantity: 3,
      covers: { unit: 'KG', amount: 15 }, delivery_date: need.deliveryDate,
      issues: [{ severity: 'blocking', message: 'Cut-off passed.' }] }],
  });
  assert.equal(row.quoted, 0);
  assert.equal(row.missing, 15);
  assert.deepEqual(row.reasons, ['Cut-off passed.']);
  assert.equal(row.referenceSku, 'rice-sku');
});

test('reopens a short-pick order and verifies its original price, amended receipt and signed change history', async () => {
  const amendment = { id: 'AM-1', order_id: 'PO-amended', kind: 'short_pick', line: 0,
    status: 'applied', tier: 'notify', reason: 'Only half the packs were picked.' };
  const amendmentJws = token(amendment, 'amendment', 'sous');
  const summary = { id: amendment.id, kind: amendment.kind, line: 0, status: amendment.status,
    tier: amendment.tier, jws_sha256: createHash('sha256').update(amendmentJws).digest('hex') };
  const payload = { id: amendment.order_id, quote_id: quote.id, mandate_id: mandate.id,
    total_ex_vat: quote.total_ex_vat / 2, original_total_ex_vat: quote.total_ex_vat,
    lines: [{ name: 'Oats', quantity: 1 }], decision: { outcome: 'notify' }, status: 'ordered',
    approval: null, updated_at: '2026-11-14T12:00:00Z', credit_notes: [], amendments: [summary] };
  const order = { ...payload, agent_id: mandate.agent_id,
    amendments: [{ ...amendment, jws: amendmentJws, erp: { status: 'simulated' } }],
    receipt_jws: token(payload, 'order', 'sous') };
  const client = new Gateway('https://example.test', async url => {
    if (url.endsWith('jwks.json')) return Response.json(jwks);
    if (url.includes('/quotes/')) return Response.json({ quote, jws: token(quote, 'quote') });
    return Response.json(order);
  });
  const reopened = await client.getOrder(order.id);
  const originalQuote = await client.getQuote(reopened.quote_id);
  assert.deepEqual(await client.receipt(reopened, originalQuote), payload);
  const changedReason = structuredClone(order);
  changedReason.amendments[0].reason = 'A fabricated seller reason';
  await assert.rejects(client.receipt(changedReason, quote), /amendment/);
  await assert.rejects(client.receipt({ ...order, amendments: [] }, quote), /amendment/);
  const proof = await client.proofBundle(order, quote);
  assert.deepEqual(proof.amendment_jws, [amendmentJws]);
  assert.deepEqual(await verifyProofBundle(proof, jwks), payload);
  await assert.rejects(verifyProofBundle({ ...proof, amendment_jws: [] }, jwks), /amendment/);
  await assert.rejects(client.receipt({ ...order, original_total_ex_vat: 999 }, quote), /amendment/);
  const badDigest = { ...payload, amendments: [{ ...summary, jws_sha256: '0'.repeat(64) }] };
  await assert.rejects(client.receipt({ ...order, receipt_jws: token(badDigest, 'order', 'sous') }, quote), /amendment/);
  const wrongOrder = { ...amendment, order_id: 'PO-other' };
  const wrongJws = token(wrongOrder, 'amendment', 'sous');
  const wrongReceipt = { ...payload, amendments: [{ ...summary,
    jws_sha256: createHash('sha256').update(wrongJws).digest('hex') }] };
  await assert.rejects(client.receipt({ ...order, amendments: [{ ...wrongOrder, jws: wrongJws }],
    receipt_jws: token(wrongReceipt, 'order', 'sous') }, quote), /amendment/);
  const pending = { ...amendment, kind: 'delay', status: 'pending_buyer' };
  const pendingJws = token(pending, 'amendment', 'sous');
  const pendingReceipt = { ...payload, total_ex_vat: quote.total_ex_vat, original_total_ex_vat: null,
    amendments: [{ ...summary, kind: 'delay', status: 'pending_buyer',
      jws_sha256: createHash('sha256').update(pendingJws).digest('hex') }] };
  const pendingOrder = { ...order, ...pendingReceipt, amendments: [{ ...pending, jws: pendingJws }],
    receipt_jws: token(pendingReceipt, 'order', 'sous') };
  delete pendingOrder.original_total_ex_vat;
  delete pendingOrder.credit_notes;
  assert.deepEqual(await client.receipt(pendingOrder, quote), pendingReceipt);
});

test('exports signed verification material without buyer credentials and refuses a different quote or receipt', async () => {
  const payload = { id: 'PO-proof', quote_id: quote.id, mandate_id: mandate.id,
    total_ex_vat: quote.total_ex_vat, lines: [], decision: { outcome: 'notify' },
    status: 'ordered', approval: null, updated_at: '2026-11-14T13:00:00Z' };
  const receipt = token(payload, 'order', 'sous');
  const order = { ...payload, agent_id: mandate.agent_id, receipt_jws: receipt };
  let exportedQuote = quote;
  const client = new Gateway('https://example.test', async url => {
    if (url.endsWith('jwks.json')) return Response.json(jwks);
    return Response.json({ quote: exportedQuote, jws: token(exportedQuote, 'quote', 'sous') });
  }, 'private-agent-credential');
  const bundle = await client.proofBundle(order, quote);
  assert.equal(bundle.format, 'sous-order-proof/v1');
  assert.deepEqual(await verifyProofBundle(bundle, jwks), payload);
  await assert.rejects(verifyProofBundle(bundle, { keys: [] }));
  await assert.rejects(verifyProofBundle({ ...bundle, format: 'other' }, jwks), /Unsupported/);
  await assert.rejects(verifyProofBundle({ ...bundle, receipt_jws: receipt + 'changed' }, jwks));
  assert.deepEqual(bundle.jwks, jwks);
  assert.equal(bundle.receipt_jws, receipt);
  assert.deepEqual(bundle.amendment_jws, []);
  assert.deepEqual(await verifyJws(bundle.quote_jws, bundle.jwks, 'sous-quote+jws'), quote);
  assert.deepEqual(await verifyJws(bundle.receipt_jws, bundle.jwks, 'sous-order+jws'), payload);
  assert.equal(JSON.stringify(bundle).includes('private-agent-credential'), false);
  await assert.rejects(client.proofBundle({ ...order, receipt_jws: receipt + 'broken' }, quote));
  exportedQuote = { ...quote, total_ex_vat: quote.total_ex_vat + 1 };
  await assert.rejects(client.proofBundle(order, quote), /exported signed quote/);
});
