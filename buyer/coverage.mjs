export function quoteCoverage(quote, draft = []) {
  const items = quote.items ?? draft;
  return items.map((item, index) => {
    const belongs = (entry) => entry.item_index === index ||
      (entry.item_index === undefined && !quote.items && items.length === 1);
    const lines = quote.lines.filter(belongs);
    const gaps = (quote.uncovered ?? []).filter(belongs);
    const alternatives = (quote.alternatives_considered ?? []).filter(belongs);
    let quoted = 0;
    let deliverable = 0;
    const reasons = new Set(gaps.map((gap) => gap.reason).filter(Boolean));
    for (const line of lines) {
      const quantity = item.unit === 'SALES_UNIT' ? line.quantity :
        line.covers?.unit === item.unit ? line.covers.amount : 0;
      const usable = line.orderable && line.delivery_date && line.delivery_date <= quote.requested_delivery_date;
      if (line.orderable) quoted += quantity ?? 0;
      if (usable) {
        const ratio = line.quantity > 0 && line.available_amount != null ?
          Math.min(1, line.available_amount / line.quantity) : 1;
        deliverable += (quantity ?? 0) * ratio;
      }
      for (const issue of line.issues ?? []) {
        if (issue.severity === 'blocking' || ['PARTIAL_STOCK', 'LATER_DATE'].includes(issue.code)) reasons.add(issue.message);
      }
    }
    const missing = Math.max(0, Number((item.amount - deliverable).toFixed(3)),
      ...gaps.map((gap) => gap.uncovered));
    if (missing > 0) {
      for (const alternative of alternatives) {
        if (!alternative.orderable) for (const reason of alternative.why_not ?? []) reasons.add(reason);
      }
      if (!reasons.size) reasons.add('The selected products cannot cover the requested quantity by the delivery date.');
    }
    return {
      item, lines, quoted: Number(quoted.toFixed(3)), missing, reasons: [...reasons],
      referenceSku: item.substitute_for || item.product_ids[0],
    };
  });
}
