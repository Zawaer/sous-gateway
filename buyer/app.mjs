import { Gateway } from './gateway.mjs';
import { helsinkiDate, parseIntent } from './intent.mjs';
import { quoteCoverage } from './coverage.mjs';

const $ = (id) => document.getElementById(id);
const requestForm = $('request-form');
const shortlistForm = $('shortlist-form');
const state = { search: null, quote: null, order: null, basket: null, editing: null, busy: false, retry: false };
const statusPollMs = 4000;
let statusTimer;
let quoteExpiryTimer;
const units = { L: 'L', KG: 'kg', SALES_UNIT: 'packs' };
const salesUnits = new Map(Object.entries({
  ST: 'pcs', KPL: 'pcs', PRK: 'packs', KI: 'cartons', PSS: 'bags',
  BOT: 'bottles', PLO: 'bottles', RAS: 'tins', SK: 'cases',
}));
const tiers = {
  silent: 'Within mandate', notify: 'Within mandate · buyer notification required',
  ask: 'Human approval required', block: 'Blocked by mandate',
};
// Same tier labels and badge classes as the seller UI (web/tokens.css, docs/UI_PRINCIPLES.md).
const tierLabels = { silent: 'Auto', notify: 'Auto + FYI', ask: 'Needs approval', block: 'Refused' };
const statusTier = { pending_approval: 'ask', refused: 'block', rejected: 'block', expired: 'block' };
const statusLabels = { ordered: 'Ordered', pending_approval: 'Needs approval', refused: 'Refused', rejected: 'Rejected', expired: 'Expired' };

function outcomeLine(tier, label, text, className = 'outcome') {
  const line = element('p', undefined, className);
  if (tier in tierLabels) line.append(element('span', label, `tier tier-${tier}`));
  line.append(element('span', text));
  return line;
}
const statuses = {
  ordered: 'Ordered in the demo gateway', pending_approval: 'Waiting for human approval',
  refused: 'Refused by the gateway', rejected: 'Rejected by the approver', expired: 'Order expired',
};

const pageParams = new URLSearchParams(location.search);
$('gateway').value = pageParams.get('gateway') || location.origin;
const linkedMandate = pageParams.get('mandate')?.trim();
if (linkedMandate && linkedMandate.length <= $('mandate').maxLength) $('mandate').value = linkedMandate;
if (pageParams.get('sandbox')) {
  $('agent-token').closest('details').open = true;
  $('agent-token-hint').hidden = false;
}
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
  $('request-quote').hidden = Boolean(state.basket || state.editing !== null);
  $('add-to-basket').disabled = busy || retry || !checked.length;
  for (const button of document.querySelectorAll('#basket-panel button, #quote-content button, #order-content button')) {
    button.disabled = busy || retry;
  }
  $('submit-order').hidden = !quote || quote.mandate_preview.outcome === 'block';
  const expired = Boolean(quote && !retry && Date.parse(quote.expires_at) <= Date.now());
  $('submit-order').disabled = busy || !quote || expired;
  const quoteExpiryNotice = $('quote-expired');
  if (quoteExpiryNotice) quoteExpiryNotice.hidden = !expired;
  $('refresh').disabled = busy;
  if (state.basket) for (const id of ['gateway', 'mandate', 'delivery', 'agent-token']) $(id).disabled = true;
  $('open-linked-order').disabled = busy || retry;
}

async function run(work) {
  if (state.busy) return;
  state.busy = true;
  updateControls();
  notice($('feedback'));
  try {
    await work();
  } catch (error) {
    if (error.status === 404 && error.code === 'mandate_not_found') {
      $('agent-token').closest('details').open = true;
      notice($('feedback'), 'Check the mandate ID. If the mandate is private, enter its agent token and try again.', true);
    } else if (['agent_auth_required', 'agent_auth_invalid'].includes(error.code)) {
      $('agent-token').closest('details').open = true;
      notice($('feedback'), 'Enter a valid agent token for this mandate under Product filters and gateway, then try again.', true);
    } else {
      notice($('feedback'), error.message, true);
    }
  } finally {
    state.busy = false;
    updateControls();
  }
}

function clearQuote() {
  clearTimeout(quoteExpiryTimer);
  state.quote = null;
  $('quote-panel').hidden = true;
  updateControls();
}

function clearSearch() {
  state.search = null;
  $('candidates').hidden = true;
  notice($('feedback'));
  if (!state.basket) clearQuote();
  else updateControls();
}

function actionButton(label, onClick) {
  const button = element('button', label, 'secondary');
  button.type = 'button';
  button.addEventListener('click', onClick);
  return button;
}

function selectedProducts() {
  return [...shortlistForm.querySelectorAll('input:checked')].map((input) => input.value);
}

function startItem(entry = null, index = null) {
  state.editing = index;
  $('intent').value = '';
  $('query').value = entry?.need.query ?? '';
  $('amount').value = entry?.need.amount ?? '';
  $('unit').value = entry?.need.unit ?? 'L';
  $('claims').value = entry?.need.claims.join(', ') ?? '';
  $('allergens').value = entry?.need.avoidAllergens.join(', ') ?? '';
  $('category').value = entry?.need.category ?? '';
  $('temperature').value = entry?.need.temperature ?? '';
  $('substitute').value = entry?.need.substituteFor ?? '';
  if (state.basket) {
    $('mandate').value = state.basket.mandate.id;
    $('delivery').value = state.basket.deliveryDate;
    $('gateway').value = state.basket.gateway.base;
  }
  $('add-to-basket').textContent = index === null ? 'Add to basket' : 'Update basket item';
  clearSearch();
  $('query').focus();
}

function renderBasket() {
  const basket = state.basket;
  $('basket-panel').hidden = !basket;
  if (!basket) return;
  $('basket-context').textContent = `${basket.mandate.buyer} · Delivery ${basket.deliveryDate}`;
  $('basket-items').replaceChildren(...basket.entries.map((entry, index) => {
    const row = element('li', undefined, 'basket-item');
    const title = `${entry.need.query} · ${entry.need.amount} ${units[entry.need.unit]}`;
    const edit = actionButton('Edit', () => startItem(entry, index));
    edit.setAttribute('aria-label', `Edit ${entry.need.query}`);
    const remove = actionButton('Remove', () => {
      basket.entries.splice(index, 1);
      if (!basket.entries.length) state.basket = null;
      startItem();
      clearQuote();
      renderBasket();
    });
    remove.setAttribute('aria-label', `Remove ${entry.need.query}`);
    const actions = element('div', undefined, 'actions');
    actions.append(edit, remove);
    row.append(element('span', title), actions);
    return row;
  }));
  updateControls();
}

function fillRequest() {
  try {
    const need = parseIntent($('intent').value);
    $('query').value = need.query;
    $('amount').value = need.amount;
    $('unit').value = need.unit;
    if (!state.basket) $('delivery').value = need.deliveryDate;
    $('claims').value = [...new Set([...codes($('claims').value), ...need.claims])].join(', ');
    clearSearch();
    const message = state.basket && need.deliveryDate !== state.basket.deliveryDate
      ? `Review the filled fields. This item will use the basket delivery date, ${state.basket.deliveryDate}.`
      : 'Review the filled fields and add any product constraints before searching.';
    notice($('intent-message'), message);
  } catch (error) {
    notice($('intent-message'), error.message, true);
  }
}

function claimLabel(code) {
  return code.toLowerCase().replaceAll('_', ' ').replace(/^free from (.+)$/, '$1 free');
}

function renderSearch(need, mandate, products, original) {
  const context = [`${mandate.buyer} · ${need.amount} ${units[need.unit]} of ${original?.name || need.query} · ${need.deliveryDate}`];
  if (need.claims.length) context.push(need.claims.map(claimLabel).join(', '));
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
    const claims = [...new Set(product.claims ?? [])];
    const requestedClaims = claims.filter((claim) => need.claims.includes(claim));
    const otherClaims = claims.filter((claim) => !need.claims.includes(claim));
    const shownClaims = [...requestedClaims, ...otherClaims.slice(0, Math.max(0, 3 - requestedClaims.length))];
    metadata.push(...shownClaims.map(claimLabel));
    if (claims.length > shownClaims.length) metadata.push(`+${claims.length - shownClaims.length} more claims`);
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

function renderShortages(content, quote) {
  const shortages = (quote.uncovered ?? []).map(({ item, uncovered, unit, reason }) => {
    const quantity = `${uncovered} ${units[unit || item?.unit] || 'units'}`;
    return `${quantity}${item?.note ? ` of ${item.note}` : ''} missing${reason ? ` · ${reason}` : ''}`;
  });
  appendList(content, 'Missing from the quote', shortages);
}

function renderCoverage(content, quote, draft = [], findAlternatives) {
  const coverage = quoteCoverage(quote, draft);
  if (!coverage.length) {
    renderShortages(content, quote);
    return;
  }
  const list = element('ul', undefined, 'order-lines coverage');
  for (const [index, row] of coverage.entries()) {
    const { item, quoted, missing, reasons, referenceSku } = row;
    const label = item.note || row.lines[0]?.name || referenceSku || 'Requested item';
    const unit = units[item.unit];
    const li = element('li');
    li.append(element('strong', label));
    const amounts = element('dl');
    for (const [term, value] of [['Requested', item.amount], ['Quoted', quoted]]) {
      const pair = element('div');
      pair.append(element('dt', term), element('dd', `${value} ${unit}`));
      amounts.append(pair);
    }
    li.append(amounts);
    if (missing) {
      li.append(element('p', `${missing} ${unit} of ${label} missing by the requested date`, 'shortage'));
      appendList(li, 'Why it could not be covered', reasons);
      if (referenceSku) {
        const button = actionButton('Find alternatives', () => findAlternatives(index, row, li));
        button.setAttribute('aria-label', `Find alternatives for ${label}`);
        li.append(button);
      }
    }
    list.append(li);
  }
  content.append(element('h3', 'Coverage by item'), list);
}

function editQuotedItem(index, row) {
  const { gateway, mandate, entries, deliveryDate } = state.quoteContext;
  if (!state.basket) {
    state.basket = { gateway, mandate, entries: [...entries], deliveryDate };
    renderBasket();
  }
  const entry = entries[index];
  const need = { ...entry.need, substituteFor: row.referenceSku };
  startItem({ need }, index);
  requestForm.requestSubmit($('find-substitutes'));
}

function showOrderAlternatives(gateway, quote, row, container) {
  run(async () => {
    const mandate = await gateway.mandate(quote.mandate_id);
    const need = {
      amount: row.missing, unit: row.item.unit, deliveryDate: quote.requested_delivery_date,
      substituteFor: row.referenceSku, claims: [], avoidAllergens: [],
    };
    const { options } = await gateway.substitutes(need, mandate);
    container.querySelector('.alternative-results')?.remove();
    const results = element('div', undefined, 'alternative-results');
    if (!options.length) results.append(element('p', 'No alternatives match the mandate for this request.'));
    appendList(results, 'Alternatives for the missing quantity', options.map(({ name, offer, compromises }) =>
      [name, offer.delivery_date && `Delivery ${offer.delivery_date}`,
        offer.line_total_ex_vat != null && `${money(offer.line_total_ex_vat, quote.currency)} ex VAT`,
        ...compromises].filter(Boolean).join(' · ')));
    container.append(results);
  });
}

function renderQuote(quote) {
  const preview = quote.mandate_preview;
  const content = $('quote-content');
  content.replaceChildren(
    element('p', `${quote.id} · Valid until ${time(quote.expires_at)} Helsinki time`, 'muted'),
    element('p', `${money(quote.total_ex_vat, quote.currency)} ex VAT · ${money(quote.total_inc_vat, quote.currency)} inc VAT`, 'total'),
    outcomeLine(preview.outcome, tierLabels[preview.outcome], tiers[preview.outcome]),
  );
  const expired = element('p', 'Quote expired. Request a new signed quote to continue.', 'notice');
  expired.id = 'quote-expired';
  expired.setAttribute('role', 'status');
  expired.hidden = true;
  content.append(expired);
  clearTimeout(quoteExpiryTimer);
  quoteExpiryTimer = setTimeout(updateControls, Math.max(0, Date.parse(quote.expires_at) - Date.now()));
  const list = element('ul', undefined, 'order-lines');
  for (const line of quote.lines) {
    const row = element('li');
    row.append(element('strong', line.name));
    const details = [
      line.quantity != null && `${line.quantity} ${salesUnits.get(line.sales_unit?.toUpperCase()) ?? (line.sales_unit || 'sales units')}`,
      line.covers?.unit && `${line.covers.amount} ${units[line.covers.unit]}`,
      line.delivery_date && `Delivery ${line.delivery_date}`,
      line.orderable ? money(line.line_total_ex_vat, quote.currency) : 'Unavailable for this order',
    ];
    row.append(element('small', details.filter(Boolean).join(' · ')));
    for (const issue of line.issues) row.append(element('small', issue.message));
    list.append(row);
  }
  content.append(list);
  const draft = state.quoteContext.entries.map(({ need, productIds }) => ({
    amount: need.amount, unit: need.unit, note: need.query, product_ids: productIds, substitute_for: need.substituteFor,
  }));
  renderCoverage(content, quote, draft, editQuotedItem);
  appendList(content, 'Mandate findings', preview.findings.map((finding) => finding.message));
  appendList(content, 'Other products considered', quote.alternatives_considered.map((option) => `${option.name} · ${option.why_not.join(' ')}`));
  $('submit-order').textContent = preview.outcome === 'ask' ? 'Send for approval' : 'Submit order';
  $('quote-panel').hidden = false;
}

async function renderOrder(current, order) {
  const { gateway, quote } = current;
  const content = document.createDocumentFragment();
  const status = statuses[order.status] || order.status;
  const tier = statusTier[order.status] || order.decision?.outcome;
  content.append(outcomeLine(tier, (order.status === 'ordered' && tierLabels[tier]) || statusLabels[order.status] || status, `${order.id} · ${status}`, 'outcome total'));
  renderCoverage(content, quote, [], (_, row, container) => showOrderAlternatives(gateway, quote, row, container));
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
  if (current !== state.order) return;
  $('order-content').replaceChildren(content);
  $('order-panel').hidden = false;
  const fallback = `${gateway.base}/ui/#/orders/${encodeURIComponent(order.id)}`;
  let approval = fallback;
  if (order.approval_url) {
    const url = URL.parse(order.approval_url, `${gateway.base}/`);
    if (url?.origin === new URL(gateway.base).origin && !url.username && !url.password) approval = url.href;
  }
  $('approvals').href = approval;
  $('approvals').hidden = order.status !== 'pending_approval';
  const audit = new URL(`${gateway.base}/ui/`);
  const orderUrl = order.order_url && URL.parse(order.order_url, `${gateway.base}/`);
  const sandbox = orderUrl?.origin === audit.origin && orderUrl.searchParams.get('sandbox');
  if (sandbox) audit.searchParams.set('sandbox', sandbox);
  audit.hash = `/audit?${new URLSearchParams({ subject: order.id })}`;
  $('order-audit-link').href = audit.href;
  updateControls();
}

async function showOrder(gateway, order, quote) {
  clearTimeout(statusTimer);
  state.order = { gateway, id: order.id, quote, snapshot: order };
  await renderOrder(state.order, order);
  scheduleStatus();
}

function scheduleStatus() {
  clearTimeout(statusTimer);
  if (state.order?.snapshot.status !== 'pending_approval') return;
  statusTimer = setTimeout(pollPendingOrder, statusPollMs);
}

async function pollPendingOrder() {
  if (state.order?.snapshot.status !== 'pending_approval') return;
  clearTimeout(statusTimer);
  if (!document.hidden && !state.busy) {
    try { await refreshOrder(); } catch { /* Retry transient failures on the next poll. */ }
  }
  scheduleStatus();
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  updateControls();
  pollPendingOrder();
});

async function refreshOrder(force = false) {
  const current = state.order;
  if (!current || current.refreshing) return;
  current.refreshing = true;
  try {
    const { gateway, id } = current;
    const order = await gateway.getOrder(id);
    if (current !== state.order) return;
    if (force || order.status !== current.snapshot.status || order.receipt_jws !== current.snapshot.receipt_jws) {
      await renderOrder(current, order);
      current.snapshot = order;
    }
  } finally {
    current.refreshing = false;
  }
}

$('interpret').addEventListener('click', fillRequest);
$('intent').addEventListener('keydown', (event) => {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  fillRequest();
});
requestForm.addEventListener('input', (event) => {
  if (event.target === $('gateway') || event.target === $('mandate')) $('agent-token').value = '';
  if (event.target !== $('intent')) clearSearch();
});
shortlistForm.addEventListener('change', () => {
  if (state.basket) updateControls();
  else clearQuote();
});

requestForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const findSubstitutes = event.submitter?.id === 'find-substitutes';
  run(async () => {
    const need = readNeed(findSubstitutes);
    const gateway = new Gateway($('gateway').value, undefined, $('agent-token').value.trim());
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
    const saved = state.basket?.entries[state.editing]?.productIds ?? [];
    for (const input of shortlistForm.querySelectorAll('input')) {
      input.checked = saved.includes(input.value) && input.dataset.unverified !== 'true';
    }
    $('candidates').scrollIntoView({ block: 'start' });
  });
});

function selectedEntry() {
  const { gateway, mandate, need } = state.search;
  const productIds = selectedProducts();
  if (!productIds.length) throw new Error('Select at least one product.');
  return { gateway, mandate, entry: { need: { ...need, query: need.query || need.substituteFor }, productIds } };
}

$('add-to-basket').addEventListener('click', () => {
  run(() => {
    const { gateway, mandate, entry } = selectedEntry();
    state.basket ??= { gateway, mandate, deliveryDate: entry.need.deliveryDate, entries: [] };
    const basket = state.basket;
    const index = state.editing ?? basket.entries.length;
    if (index >= 50) throw new Error('A basket can contain up to 50 items.');
    const used = new Set(basket.entries.filter((_, position) => position !== index).flatMap((item) => item.productIds));
    if (entry.productIds.some((id) => used.has(id))) {
      throw new Error('A product can only appear on one basket item. Combine quantities or choose different products.');
    }
    basket.entries[index] = entry;
    startItem();
    clearQuote();
    renderBasket();
    $('basket-panel').scrollIntoView({ block: 'start' });
  });
});

$('add-another').addEventListener('click', () => startItem());

async function requestQuote(gateway, mandate, deliveryDate, entries) {
  const quote = await gateway.quoteBasket(deliveryDate, mandate, entries);
  state.quoteContext = { gateway, mandate, deliveryDate, entries: [...entries] };
  state.quote = quote;
  renderQuote(quote);
  $('quote-panel').scrollIntoView({ block: 'start' });
}

$('quote-basket').addEventListener('click', () => {
  run(async () => {
    const { gateway, entries, deliveryDate, mandate: saved } = state.basket;
    const mandate = await gateway.mandate(saved.id);
    if (mandate.agent_id !== saved.agent_id) throw new Error('The mandate holder changed. Build a new basket.');
    await requestQuote(gateway, mandate, deliveryDate, entries);
  });
});

shortlistForm.addEventListener('submit', (event) => {
  event.preventDefault();
  if (state.basket) {
    $('add-to-basket').click();
    return;
  }
  run(async () => {
    const { gateway, mandate, entry } = selectedEntry();
    await requestQuote(gateway, mandate, entry.need.deliveryDate, [entry]);
  });
});

$('submit-order').addEventListener('click', () => {
  run(async () => {
    const { quote, quoteContext: { gateway } } = state;
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
    const url = new URL(location.href);
    url.searchParams.set('order', order.id);
    if (gateway.base === location.origin) url.searchParams.delete('gateway');
    else url.searchParams.set('gateway', gateway.base);
    history.replaceState(null, '', url);
    state.basket = null;
    startItem();
    renderBasket();
    clearQuote();
    await showOrder(gateway, order, quote);
    $('order-panel').scrollIntoView({ block: 'start' });
  });
});

$('refresh').addEventListener('click', () => {
  run(() => refreshOrder(true));
});

const linkedOrder = pageParams.get('order');
if (linkedOrder) {
  const openLinkedOrder = () => run(async () => {
    const gateway = new Gateway($('gateway').value, undefined, $('agent-token').value.trim());
    let order, quote;
    try {
      order = await gateway.getOrder(linkedOrder);
      quote = await gateway.getQuote(order.quote_id);
    } catch (error) {
      if (error.status === 404) {
        throw new Error('This order is unavailable. Check the link and gateway. If you have an agent token, enter it under Product filters and gateway, then choose Open linked order.');
      }
      if (error.status >= 500 || error instanceof TypeError || error.name === 'TimeoutError') {
        throw new Error('Could not load this order from the gateway. Check your connection and reload this page to retry.');
      }
      throw error;
    }
    await showOrder(gateway, order, quote);
    $('api-link').href = `${gateway.base}/docs`;
    $('order-panel').scrollIntoView({ block: 'start' });
  });
  $('open-linked-order').hidden = false;
  $('open-linked-order').addEventListener('click', openLinkedOrder);
  openLinkedOrder();
}
