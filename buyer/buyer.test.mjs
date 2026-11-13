import assert from 'node:assert/strict';
import { generateKeyPairSync, sign } from 'node:crypto';
import test from 'node:test';

import { Gateway, gatewayUrl, verifyJws } from './gateway.mjs';
import { helsinkiDate, parseIntent } from './intent.mjs';

const { privateKey, publicKey } = generateKeyPairSync('ed25519');
const jwks = { keys: [{ ...publicKey.export({ format: 'jwk' }), kid: 'fixture', use: 'sig' }] };
const encode = (data) => Buffer.from(JSON.stringify(data)).toString('base64url');
function token(payload, type) {
  const input = `${encode({ alg: 'EdDSA', typ: `valtakirja-${type}+jws`, kid: 'fixture' })}.${encode(payload)}`;
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
  assert.deepEqual(requests[3].body.items[0], { product_ids: ['sku'], amount: 5, unit: 'KG', substitute_for: null });
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
