import test from 'node:test';
import assert from 'node:assert/strict';
import { usageBasis, usageCostText, usageDetailText } from './lib/usageDetails.mjs';

test('unknown pricing is never displayed as free', () => {
  assert.equal(usageCostText(0, { cost_basis: 'unavailable', unpriced_calls: 1 }), 'Pricing unavailable');
  assert.match(usageCostText(.12, { cost_basis: 'mixed', unpriced_calls: 1 }), /recorded · pricing incomplete/);
});

test('historical amounts stay intact and details distinguish calculation bases', () => {
  assert.equal(usageBasis(null), 'Legacy estimate');
  assert.equal(usageCostText(.1234, null), '$0.1234');
  assert.match(usageDetailText(null), /Cached input: unavailable/);
  assert.match(usageDetailText({ cost_basis: 'cache_aware_estimate', cached_input_tokens: 500,
    cache_write_tokens: 20, reasoning_tokens: 80 }), /Cache-aware estimate · Cached input: 500 · Cache writes: 20 · Reasoning output: 80/);
  assert.equal(usageBasis({ cost_basis: 'provider_reported' }), 'Provider reported');
});
