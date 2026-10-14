/** Cache and reasoning counts are already included in the input/output totals. */
export function usageBasis(details) {
  const basis = details?.cost_basis || 'legacy_estimate';
  return ({ provider_reported: 'Provider reported', cache_aware_estimate: 'Cache-aware estimate',
    legacy_estimate: 'Legacy estimate', unavailable: 'Pricing unavailable', mixed: 'Mixed calculation bases' })[basis]
    || 'Pricing unavailable';
}

export function usageDetailText(details) {
  const tokens = (value) => value == null ? 'unavailable' : Number(value).toLocaleString('en-US');
  const parts = [usageBasis(details), `Cached input: ${tokens(details?.cached_input_tokens)}`,
    `Cache writes: ${tokens(details?.cache_write_tokens)}`, `Reasoning output: ${tokens(details?.reasoning_tokens)}`];
  if (details?.unpriced_calls) parts.push(`${details.unpriced_calls} calls without pricing`);
  if (details?.details_missing_calls) parts.push('Some token details unavailable');
  return parts.join(' · ');
}

export function usageCostText(value, details, digits = 4) {
  const amount = Number(value || 0).toLocaleString('en-US', { style: 'currency', currency: 'USD',
    minimumFractionDigits: digits, maximumFractionDigits: digits });
  if (details?.cost_basis === 'unavailable' || details?.unpriced_calls) {
    return value > 0 ? `${amount} recorded · pricing incomplete` : 'Pricing unavailable';
  }
  return amount;
}
