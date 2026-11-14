const weekdays = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'];
const units = {
  l: ['L', 1], liter: ['L', 1], litre: ['L', 1], ml: ['L', 1000],
  kg: ['KG', 1], kilogram: ['KG', 1], g: ['KG', 1000], gram: ['KG', 1000],
  pack: ['SALES_UNIT', 1], case: ['SALES_UNIT', 1],
};

export function helsinkiDate(now = new Date()) {
  return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Europe/Helsinki' }).format(now);
}

function deliveryDate(value, today) {
  const date = new Date(`${today}T00:00:00Z`);
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const parsed = new Date(`${value}T00:00:00Z`);
    if (Number.isFinite(+parsed) && parsed.toISOString().slice(0, 10) === value && value >= today) return value;
  } else {
    const weekday = weekdays.indexOf(value);
    const offset = value === 'today' ? 0 : value === 'tomorrow' ? 1 : weekday < 0 ? null : (weekday - date.getUTCDay() + 7) % 7;
    if (offset !== null) {
      date.setUTCDate(date.getUTCDate() + offset);
      return date.toISOString().slice(0, 10);
    }
  }
  throw new Error('Use a current or future delivery date in YYYY-MM-DD format, or a weekday.');
}

export function parseIntent(text, today = helsinkiDate()) {
  const input = text.trim().replace(/[.!]$/, '');
  const match = input.match(/^(?:(?:i need|we need|find|quote)\s+)?(\d[\d,.]*)\s*(litres?|liters?|l|ml|kilograms?|kg|grams?|g|packs?|cases?)\s+(?:of\s+)?(.+?)\s+(?:by|on|for)\s+(\S+)$/i);
  if (!match) throw new Error('Try “1,000 litres of lactose-free milk by Monday”, or enter the fields below.');
  const [, quantity, name, product, when] = match;
  if (/(?:\band\b|\bplus\b|[,;])\s*\d[\d,.]*\s*(?:litres?|liters?|l|ml|kilograms?|kg|grams?|g|packs?|cases?)\b/i.test(product)) {
    throw new Error('Fill one product at a time. Use Add to basket to build a multi-item order.');
  }
  if (!/^(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$/.test(quantity)) {
    throw new Error('Use a decimal point or comma-separated thousands, or enter the quantity below.');
  }
  const key = name.toLowerCase();
  const [unit, divisor] = units[key] ?? units[key.replace(/s$/, '')];
  const amount = Number(quantity.replaceAll(',', '')) / divisor;
  if (!Number.isFinite(amount) || amount <= 0 || (unit === 'SALES_UNIT' && !Number.isInteger(amount))) {
    throw new Error('Enter a positive quantity and use whole numbers for packs.');
  }
  const lactoseFree = /\blactose[- ]free\b/i.test(product);
  const query = product.replace(/\blactose[- ]free\b/gi, '').trim().replace(/\s+/g, ' ');
  if (!query) throw new Error('Enter the product name.');
  return { query, amount, unit, deliveryDate: deliveryDate(when.toLowerCase(), today), claims: lactoseFree ? ['FREE_FROM_LACTOSE'] : [] };
}
