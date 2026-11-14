const tiers = new Set(['silent', 'notify', 'ask', 'block']);

export function gatewayUrl(value) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error('Enter an HTTP or HTTPS gateway URL without credentials, query parameters, or a fragment.');
  }
  return url.href.replace(/\/+$/, '');
}

export class Gateway {
  constructor(base, fetcher = globalThis.fetch.bind(globalThis), agentToken = '') {
    this.base = gatewayUrl(base);
    this.fetch = fetcher;
    this.agentToken = agentToken;
    if (agentToken && !this.base.startsWith('https://') && !['localhost', '127.0.0.1', '[::1]'].includes(new URL(this.base).hostname)) {
      throw new Error('Use an HTTPS gateway with an agent token.');
    }
  }

  async request(path, body) {
    const response = await this.fetch(`${this.base}${path}`, {
      headers: {
        Accept: 'application/json',
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        ...(this.agentToken && path !== '/.well-known/jwks.json' ? { Authorization: `Bearer ${this.agentToken}` } : {}),
      },
      method: body === undefined ? 'GET' : 'POST',
      cache: body === undefined ? 'no-store' : 'default',
      body: body === undefined ? undefined : JSON.stringify(body),
      redirect: 'error',
      signal: AbortSignal.timeout(25_000),
    });
    const data = await response.json().catch(() => null);
    if (!response.ok || data === null) {
      const detail = typeof data?.message === 'string' ? data.message : data?.detail;
      const error = new Error(typeof detail === 'string' ? detail : `Gateway ${path.split('?')[0]} returned HTTP ${response.status}.`);
      error.status = response.ok ? 502 : response.status;
      error.code = data?.error;
      throw error;
    }
    return data;
  }

  async signed(envelope, name) {
    const keys = await this.request('/.well-known/jwks.json');
    const payload = await verifyJws(envelope.jws, keys, documentTypes(name));
    if (!equalJson(payload, envelope[name])) throw new Error(`The ${name} differs from its signed payload.`);
    return payload;
  }

  async mandate(id) {
    const mandate = await this.signed(await this.request(`/mandates/${encodeURIComponent(id)}`), 'mandate');
    if (mandate.id !== id || !mandate.agent_id) throw new Error('The signed mandate does not match this request.');
    if (mandate.revoked) throw new Error('This mandate has been revoked.');
    if (mandate.valid_until && Date.parse(mandate.valid_until) <= Date.now()) throw new Error('This mandate has expired.');
    return mandate;
  }

  async search(need, mandate) {
    const params = new URLSearchParams({ q: need.query, agent_id: mandate.agent_id, limit: '15' });
    for (const claim of new Set([...need.claims, ...mandate.required_claims])) params.append('claims', claim);
    for (const allergen of need.avoidAllergens) params.append('avoid_allergens', allergen);
    if (!mandate.allow_may_contain) {
      for (const allergen of mandate.avoid_allergens) {
        if (!need.avoidAllergens.includes(allergen)) params.append('avoid_allergens', allergen);
      }
    }
    if (need.category) params.set('category', need.category);
    if (need.temperature) params.set('temperature', need.temperature);
    const { products } = await this.request(`/products?${params}`);
    return products.filter((product) => !mandate.allowed_categories || mandate.allowed_categories.includes(product.category));
  }

  async substitutes(need, mandate) {
    const params = new URLSearchParams({
      agent_id: mandate.agent_id, mandate_id: mandate.id, amount: String(need.amount),
      unit: need.unit, delivery_date: need.deliveryDate,
    });
    for (const allergen of need.avoidAllergens) params.append('avoid_allergens', allergen);
    const path = `/products/${encodeURIComponent(need.substituteFor)}/substitutes`;
    const { original, constraints, options } = await this.request(`${path}?${params}`);
    const eligible = options.filter(({ offer, allergen_check: allergen }) => (
      (!need.temperature || offer.temperature === need.temperature) &&
      (!need.category || constraints.same_category === need.category) &&
      (!mandate.allowed_categories || mandate.allowed_categories.includes(constraints.same_category)) &&
      need.claims.every((claim) => offer.claims.includes(claim)) &&
      (!need.avoidAllergens.length || !['contains', 'may_contain'].includes(allergen.status))
    ));
    return { original, options: eligible };
  }

  async quote(need, mandate, productIds) {
    return this.quoteBasket(need.deliveryDate, mandate, [{ need, productIds }]);
  }

  async quoteBasket(deliveryDate, mandate, entries) {
    if (!entries.length || entries.length > 50) throw new Error('A basket needs between one and 50 items.');
    const items = entries.map(({ need, productIds }) => {
      if (!productIds.length || productIds.length > 6) throw new Error('Select between one and six products per item.');
      return {
        product_ids: productIds, amount: need.amount, unit: need.unit,
        substitute_for: need.substituteFor || null, note: need.query,
        ...(need.query && !need.substituteFor ? { product_type: need.query } : {}),
        ...(need.purpose ? { purpose: need.purpose } : {}),
        ...(need.substitution ? { substitution: need.substitution } : {}),
        ...(need.use ? { use: need.use } : {}),
      };
    });
    const allIds = items.flatMap((item) => item.product_ids);
    if (new Set(allIds).size !== allIds.length) throw new Error('A product can only appear on one basket item. Combine quantities or choose different products.');
    const quote = await this.signed(await this.request('/quotes', {
      agent_id: mandate.agent_id, mandate_id: mandate.id, delivery_date: deliveryDate, items,
    }), 'quote');
    if (!quote.id || !Number.isFinite(Date.parse(quote.expires_at)) || quote.agent_id !== mandate.agent_id || quote.mandate_id !== mandate.id ||
        quote.requested_delivery_date !== deliveryDate || !tiers.has(quote.mandate_preview?.outcome) ||
        (quote.items ? !equalJson(
          quote.items.map((item) => ({ product_type: null, purpose: null, substitution: null, use: null, ...item })),
          items.map((item) => ({ product_type: null, purpose: null, substitution: null, use: null, ...item })),
        ) : entries.length > 1)) {
      throw new Error('The signed quote does not match this buyer, delivery date, or product shortlist.');
    }
    for (const line of quote.lines) {
      const entry = entries[quote.items ? line.item_index : 0];
      if (!entry?.productIds.includes(line.product_id) || (line.substitute_for || '') !== (entry.need.substituteFor || '')) {
        throw new Error('A quoted product does not match its basket item.');
      }
      const requiredClaims = [...entry.need.claims, ...mandate.required_claims];
      if (requiredClaims.some((claim) => !line.claims?.includes(claim))) {
        throw new Error('A quoted product is missing a required claim.');
      }
    }
    return quote;
  }

  async order(quote, retry = false) {
    const expiry = Date.parse(quote.expires_at);
    if (!retry && (!Number.isFinite(expiry) || expiry <= Date.now())) {
      const error = new Error('This quote has expired. Request a fresh quote.');
      error.status = 409;
      throw error;
    }
    if (!['silent', 'notify', 'ask'].includes(quote.mandate_preview?.outcome)) {
      throw new Error('The mandate preview blocks this quote.');
    }
    return this.request('/orders', {
      agent_id: quote.agent_id, mandate_id: quote.mandate_id, quote_id: quote.id,
      idempotency_key: `reference-buyer:${quote.id}`,
    });
  }

  async getOrder(id) {
    const order = await this.request(`/orders/${encodeURIComponent(id)}`);
    if (order.id !== id || !order.quote_id) throw new Error('The gateway returned a different order.');
    return order;
  }

  async getQuote(id) {
    const envelope = await this.request(`/quotes/${encodeURIComponent(id)}`);
    const quote = await this.signed(envelope, 'quote');
    if (quote.id !== id) throw new Error('The gateway returned a different signed quote.');
    return quote;
  }

  async proofBundle(order, quote) {
    const envelope = await this.request(`/quotes/${encodeURIComponent(quote.id)}`);
    const keys = await this.request('/.well-known/jwks.json');
    const original = await verifyJws(envelope.jws, keys, documentTypes('quote'));
    if (!equalJson(original, quote) || !equalJson(original, envelope.quote)) {
      throw new Error('The exported signed quote does not match this order’s original quote.');
    }
    await this.receipt(order, quote);
    return {
      format: 'sous-order-proof/v1', gateway: this.base, jwks: keys,
      quote_jws: envelope.jws, receipt_jws: order.receipt_jws,
      amendment_jws: (order.amendments ?? []).map(amendment => amendment.jws),
    };
  }

  async receipt(order, quote) {
    const keys = await this.request('/.well-known/jwks.json');
    const signed = await verifyJws(order.receipt_jws, keys, documentTypes('order'));
    const amended = Array.isArray(signed.amendments) && signed.amendments.length > 0;
    const hasDecisionSnapshot = Object.hasOwn(signed, 'approval') || Object.hasOwn(signed, 'updated_at');
    const matchesStatus = hasDecisionSnapshot
      ? signed.status === order.status && equalJson(signed.approval, order.approval) && signed.updated_at === order.updated_at
      : signed.status === order.status || (signed.status === 'pending_approval' && Boolean(order.approval));
    if (signed.quote_id !== quote.id || signed.mandate_id !== quote.mandate_id ||
        order.agent_id !== quote.agent_id ||
        (amended ? signed.original_total_ex_vat ?? signed.total_ex_vat : signed.total_ex_vat) !== quote.total_ex_vat ||
        !['id', 'quote_id', 'mandate_id', 'total_ex_vat', 'lines', 'decision'].every((key) => equalJson(signed[key], order[key])) ||
        !matchesStatus) {
      throw new Error('The signed receipt does not match this order and quote.');
    }
    if (amended) {
      if (!equalJson(signed.original_total_ex_vat ?? null, order.original_total_ex_vat ?? null) ||
          !equalJson(signed.credit_notes, order.credit_notes ?? []) ||
          !Array.isArray(order.amendments) || order.amendments.length !== signed.amendments.length) {
        throw new Error('The signed amendment history does not match this order.');
      }
      for (const [index, summary] of signed.amendments.entries()) {
        const amendment = order.amendments[index];
        const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(amendment.jws));
        const hash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
        const payload = await verifyJws(amendment.jws, keys, 'sous-amendment+jws');
        const { jws, erp, ...reported } = amendment;
        if (hash !== summary.jws_sha256 || payload.order_id !== signed.id ||
            !equalJson(payload, reported) ||
            !['id', 'kind', 'status', 'tier', 'line'].every(key => equalJson(payload[key], summary[key]))) {
          throw new Error('The signed amendment does not match this order and receipt.');
        }
      }
    } else if (order.amendments?.length) {
      throw new Error('The receipt does not sign this order’s amendments.');
    }
    return signed;
  }
}

export async function verifyProofBundle(bundle, trustedKeys) {
  if (bundle.format !== 'sous-order-proof/v1' || !Array.isArray(bundle.amendment_jws)) {
    throw new Error('Unsupported buyer proof bundle.');
  }
  const quote = await verifyJws(bundle.quote_jws, trustedKeys, documentTypes('quote'));
  const receipt = await verifyJws(bundle.receipt_jws, trustedKeys, documentTypes('order'));
  const amendments = [];
  for (const jws of bundle.amendment_jws) {
    amendments.push({ ...await verifyJws(jws, trustedKeys, 'sous-amendment+jws'), jws });
  }
  const gateway = new Gateway(bundle.gateway, async path => {
    if (!path.endsWith('/.well-known/jwks.json')) throw new Error('Offline verification cannot fetch gateway data.');
    return Response.json(trustedKeys);
  });
  return gateway.receipt({
    ...receipt, agent_id: quote.agent_id, amendments, receipt_jws: bundle.receipt_jws,
  }, quote);
}

function decode(segment) {
  if (!/^[\w-]+$/.test(segment)) throw new Error('Invalid base64url in JWS.');
  return Uint8Array.from(atob(segment.replace(/-/g, '+').replace(/_/g, '/')), (char) => char.charCodeAt(0));
}

function equalJson(a, b) {
  if (a === b) return true;
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object' || Array.isArray(a) !== Array.isArray(b)) return false;
  return Object.keys(a).length === Object.keys(b).length &&
    Object.keys(a).every((key) => Object.hasOwn(b, key) && equalJson(a[key], b[key]));
}

function documentTypes(name) {
  return [`sous-${name}+jws`, `valtakirja-${name}+jws`];
}

export async function verifyJws(token, jwks, types) {
  if (!globalThis.crypto?.subtle) throw new Error('Signature verification requires HTTPS or a localhost browser session.');
  try {
    const parts = token.split('.');
    if (parts.length !== 3) throw new Error();
    const [header, payload, signature] = parts;
    const decoder = new TextDecoder('utf-8', { fatal: true });
    const { alg, kid, typ, crit, b64 } = JSON.parse(decoder.decode(decode(header)));
    const allowedTypes = Array.isArray(types) ? types : [types];
    if (alg !== 'EdDSA' || !kid || !allowedTypes.includes(typ) || crit !== undefined || b64 !== undefined) throw new Error();
    const keys = jwks.keys.filter((key) => key.kid === kid && key.kty === 'OKP' && key.crv === 'Ed25519' &&
      (key.alg === undefined || key.alg === 'EdDSA') && (key.use === undefined || key.use === 'sig'));
    if (keys.length !== 1) throw new Error();
    const key = await crypto.subtle.importKey('jwk', keys[0], 'Ed25519', false, ['verify']);
    const valid = await crypto.subtle.verify('Ed25519', key, decode(signature), new TextEncoder().encode(`${header}.${payload}`));
    if (!valid) throw new Error();
    return JSON.parse(decoder.decode(decode(payload)));
  } catch {
    throw new Error('The gateway signature could not be verified.');
  }
}
