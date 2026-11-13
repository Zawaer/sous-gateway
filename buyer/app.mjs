import { Gateway } from './gateway.mjs';
import { helsinkiDate, parseIntent } from './intent.mjs';

const $ = (id) => document.getElementById(id);
const requestForm = $('request-form');
const shortlistForm = $('shortlist-form');
const state = { search: null, quote: null, order: null, busy: false, retry: false };
const units = { L: 'L', KG: 'kg', SALES_UNIT: 'packs' };
const tiers = {
  silent: 'Within mandate', notify: 'Within mandate · buyer notification required',
  ask: 'Human approval required', block: 'Blocked by mandate',
};
const statuses = {
  ordered: 'Ordered in the demo gateway', pending_approval: 'Waiting for human approval',
  refused: 'Refused by the gateway', rejected: 'Rejected by the approver', expired: 'Order expired',
};

$('gateway').value = new URLSearchParams(location.search).get('gateway') || location.origin;
$('delivery').min = helsinkiDate();

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function notice(node, message = '', error = false) {
  node.textContent = message;
  node.dataset.error = String(error);
  node.hidden = !message;
}

function codes(value) {
  return [...new Set(value.toUpperCase().split(/[\s,]+/).filter(Boolean))];
}

function readNeed(findSubstitutes) {
  const need = {
    query: $('query').value.trim(), amount: $('amount').valueAsNumber, unit: $('unit').value,
    deliveryDate: $('delivery').value, claims: codes($('claims').value), avoidAllergens: codes($('allergens').value),
    category: $('category').value.trim(), temperature: $('temperature').value, substituteFor: $('substitute').value.trim(),
  };
  if (findSubstitutes && !need.substituteFor) throw new Error('Enter the original SKU under Product filters and gateway.');
  if (!findSubstitutes && need.query.length < 3) throw new Error('Enter at least three characters for a product search.');
  if (!Number.isFinite(need.amount) || need.amount < 0.001) throw new Error('Enter a quantity of at least 0.001.');
  if (!need.deliveryDate || need.deliveryDate < helsinkiDate()) throw new Error('Enter a current or future delivery date.');
  if (need.unit === 'SALES_UNIT' && !Number.isInteger(need.amount)) throw new Error('Enter a whole number of sales units.');
  return need;
}

function updateControls() {
  const { busy, quote, retry } = state;
  for (const control of requestForm.elements) control.disabled = busy || retry;
  const checked = shortlistForm.querySelectorAll('input:checked');
  for (const checkbox of shortlistForm.querySelectorAll('input')) {
    checkbox.disabled = busy || retry || checkbox.dataset.unverified === 'true' || (checked.length >= 6 && !checkbox.checked);
  }
  $('request-quote').disabled = busy || retry || !checked.length;
  $('submit-order').hidden = !quote || quote.mandate_preview.outcome === 'block';
  $('submit-order').disabled = busy || !quote || (!retry && Date.parse(quote.expires_at) <= Date.now());
  $('quote-substitutes').disabled = busy || retry;
  $('refresh').disabled = busy;
}

async function run(work) {
  if (state.busy) return;
  state.busy = true;
  updateControls();
  notice($('feedback'));
  try {
    await work();
  } catch (error) {
    notice($('feedback'), error.message, true);
  } finally {
    state.busy = false;
    updateControls();
  }
}

function clearQuote() {
  state.quote = null;
  $('quote-panel').hidden = true;
  updateControls();
}

function clearSearch() {
  state.search = null;
  $('candidates').hidden = true;
  notice($('feedback'));
  clearQuote();
}

function fillRequest() {
  try {
    const need = parseIntent($('intent').value);
    $('query').value = need.query;
    $('amount').value = need.amount;
    $('unit').value = need.unit;
    $('delivery').value = need.deliveryDate;
    $('claims').value = [...new Set([...codes($('claims').value), ...need.claims])].join(', ');
    clearSearch();
    notice($('intent-message'), 'Review the filled fields and add any product constraints before searching.');
  } catch (error) {
    notice($('intent-message'), error.message, true);
  }
}

function renderSearch(need, mandate, products, original) {
  const context = [`${mandate.buyer} · ${need.amount} ${units[need.unit]} of ${original?.name || need.query} · ${need.deliveryDate}`];
  if (need.claims.length) context.push(need.claims.join(', '));
  if (need.avoidAllergens.length) context.push(`Avoid ${need.avoidAllergens.join(', ')}`);
  if (original) context.push(...original.why_substitute);
  $('candidates-title').textContent = original ? 'Substitute shortlist' : 'Product shortlist';
  $('search-context').textContent = context.join(' · ');
  $('products').replaceChildren(...products.map((product) => {
    const label = element('label');
    const checkbox = element('input');
    checkbox.type = 'checkbox';
    checkbox.name = 'product';
    checkbox.value = product.id;
    const allergenStatus = product.allergen_check?.status;
    const unknownAllergens = allergenStatus === 'unknown' || !product.allergens?.declared;
    checkbox.dataset.unverified = String(Boolean(need.avoidAllergens.length && unknownAllergens));
    const content = element('span', product.name);
    const metadata = [`SKU ${product.id}`, product.category && `Category ${product.category}`, product.temperature].filter(Boolean);
    if (unknownAllergens) {
      metadata.push(checkbox.dataset.unverified === 'true' ? 'Requested allergens could not be checked' : 'Allergen information unavailable');
    }
    if (product.claims?.length) metadata.push(product.claims.join(', '));
    if (product.tier) metadata.push(`Gateway ranking ${product.tier}`);
    if (product.content_per_sales_unit) {
      const { amount, unit } = product.content_per_sales_unit;
      metadata.push(`${amount} ${units[unit] || unit} per sales unit`);
    }
    if (product.compromises?.length) metadata.push(...product.compromises);
    content.append(element('small', metadata.join(' · ')));
    label.append(checkbox, content);
    return label;
  }));
  $('candidates').hidden = false;
  shortlistForm.hidden = products.length === 0;
  if (!products.length) notice($('feedback'), original ? 'No suggested substitute meets the current filters.' :
    'No matching products. Try a more specific product name or review the filters.');
}

function money(amount, currency = 'EUR') {
  if (amount == null) return 'Price unavailable';
  return new Intl.NumberFormat('en-IE', { style: 'currency', currency }).format(amount);
}

function time(value) {
  return new Intl.DateTimeFormat('en-GB', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Europe/Helsinki' }).format(new Date(value));
}

function appendList(node, title, items) {
  if (!items.length) return;
  const list = element('ul');
  for (const item of items) list.append(element('li', item));
  node.append(element('h3', title), list);
}

function renderQuote(quote) {
  const preview = quote.mandate_preview;
  const content = $('quote-content');
  content.replaceChildren(
    element('p', `${quote.id} · Valid until ${time(quote.expires_at)} Helsinki time`, 'muted'),
    element('p', `${money(quote.total_ex_vat, quote.currency)} ex VAT · ${money(quote.total_inc_vat, quote.currency)} inc VAT`, 'total'),
    element('p', tiers[preview.outcome]),
  );
  const list = element('ul', undefined, 'order-lines');
  for (const line of quote.lines) {
    const row = element('li');
    row.append(element('strong', line.name));
    const details = [
      line.quantity != null && `${line.quantity} ${line.sales_unit || 'sales units'}`,
      line.covers?.unit && `${line.covers.amount} ${units[line.covers.unit]}`,
      line.delivery_date && `Delivery ${line.delivery_date}`,
      money(line.line_total_ex_vat, quote.currency),
    ];
    row.append(element('small', details.filter(Boolean).join(' · ')));
    for (const issue of line.issues) row.append(element('small', issue.message));
    list.append(row);
  }
  content.append(list);
  appendList(content, 'Unfilled quantity', quote.uncovered.map((gap) => `${gap.uncovered} ${units[gap.unit || gap.item?.unit] || 'units'}${gap.reason ? ` · ${gap.reason}` : ''}`));
  appendList(content, 'Mandate findings', preview.findings.map((finding) => finding.message));
  appendList(content, 'Other products considered', quote.alternatives_considered.map((option) => `${option.name} · ${option.why_not.join(' ')}`));
  $('submit-order').textContent = preview.outcome === 'ask' ? 'Send for approval' : 'Submit order';
  $('quote-substitutes').hidden = Boolean(state.search.need.substituteFor) || quote.lines.length !== 1 ||
    !quote.lines[0].issues.some(({ code }) => ['OUT_OF_STOCK', 'CUTOFF_PASSED', 'SALES_BLOCK', 'UNDELIVERABLE', 'LATER_DATE'].includes(code));
  $('quote-panel').hidden = false;
}

async function renderOrder(gateway, order, quote) {
  const content = $('order-content');
  const status = statuses[order.status] || order.status;
  content.replaceChildren(element('p', `${order.id} · ${status}`, 'total'));
  appendList(content, 'Gateway decision', order.decision.findings.map((finding) => finding.message));
  try {
    const signed = await gateway.receipt(order, quote);
    if (signed.approval) {
      const action = signed.approval.approved ? 'Approved by' : 'Rejected by';
      content.append(element('p', `${action} ${signed.approval.by}`));
      if (signed.approval.result_reason) content.append(element('p', signed.approval.result_reason));
    }
    const message = signed.status === order.status ? 'Receipt signature verified' :
      `Original receipt verified (${statuses[signed.status] || signed.status}). Current status reported by the gateway.`;
    content.append(element('p', message, 'verified'));
  } catch (error) {
    content.append(element('p', error.message, 'notice'));
  }
  $('order-panel').hidden = false;
  const fallback = `${gateway.base}/ui/#/orders/${encodeURIComponent(order.id)}`;
  let approval = fallback;
  if (order.approval_url) {
    const url = URL.parse(order.approval_url, `${gateway.base}/`);
    if (url?.origin === new URL(gateway.base).origin && !url.username && !url.password) approval = url.href;
  }
  $('approvals').href = approval;
  $('approvals').hidden = order.status !== 'pending_approval';
}

$('interpret').addEventListener('click', fillRequest);
$('intent').addEventListener('keydown', (event) => {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  fillRequest();
});
requestForm.addEventListener('input', (event) => {
  if (event.target !== $('intent')) clearSearch();
});
shortlistForm.addEventListener('change', clearQuote);

requestForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const findSubstitutes = event.submitter?.id === 'find-substitutes';
  run(async () => {
    const need = readNeed(findSubstitutes);
    const gateway = new Gateway($('gateway').value);
    clearSearch();
    const mandate = await gateway.mandate($('mandate').value.trim());
    let original;
    let products;
    if (findSubstitutes) {
      const suggestions = await gateway.substitutes(need, mandate);
      original = suggestions.original;
      products = suggestions.options.map(({ offer, product_id, tier, compromises, allergen_check }) => ({
        ...offer, id: product_id, tier, compromises, allergen_check,
      }));
    } else {
      products = await gateway.search(need, mandate);
    }
    state.search = { gateway, need, mandate };
    $('api-link').href = `${gateway.base}/docs`;
    renderSearch(need, mandate, products, original);
    $('candidates').scrollIntoView({ block: 'start' });
  });
});

$('quote-substitutes').addEventListener('click', () => {
  $('substitute').value = state.quote.lines[0].product_id;
  requestForm.requestSubmit($('find-substitutes'));
});

shortlistForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const ids = [...new FormData(shortlistForm).getAll('product')];
  run(async () => {
    const { gateway, need, mandate } = state.search;
    state.quote = await gateway.quote(need, mandate, ids);
    renderQuote(state.quote);
    $('quote-panel').scrollIntoView({ block: 'start' });
  });
});

$('submit-order').addEventListener('click', () => {
  run(async () => {
    const { quote, search: { gateway } } = state;
    let order;
    try {
      order = await gateway.order(quote, state.retry);
    } catch (error) {
      if (!error.status || error.status >= 500 || (state.retry && error.status === 429)) {
        state.retry = true;
        $('submit-order').textContent = 'Retry order status';
        throw new Error(`Order status is uncertain. Retry this same submission to recover its status. ${error.message}`);
      }
      state.retry = false;
      renderQuote(quote);
      throw error;
    }
    state.retry = false;
    if (!order.id || order.quote_id !== quote.id || order.mandate_id !== quote.mandate_id || order.agent_id !== quote.agent_id) {
      state.retry = true;
      $('submit-order').textContent = 'Retry order status';
      throw new Error('The gateway returned an unexpected order. Retry this same submission to recover its status.');
    }
    state.order = { gateway, id: order.id, quote };
    clearQuote();
    await renderOrder(gateway, order, quote);
    $('order-panel').scrollIntoView({ block: 'start' });
  });
});

$('refresh').addEventListener('click', () => {
  run(async () => {
    const { gateway, id, quote } = state.order;
    const order = await gateway.request(`/orders/${encodeURIComponent(id)}`);
    await renderOrder(gateway, order, quote);
  });
});
