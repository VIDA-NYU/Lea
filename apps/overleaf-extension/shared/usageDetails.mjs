/** Add normalized subsets without adding them to total input/output tokens. */
export function mergeUsageDetails(left, right) {
  if (!left && !right) return null;
  const result = { version: 1, basis_counts: {} };
  for (const key of ["cached_input_tokens", "cache_write_tokens", "reasoning_tokens"]) {
    const values = [left, right].filter((d) => d?.[key] != null).map((d) => Number(d[key]));
    result[key] = values.length ? values.reduce((a, b) => a + b, 0) : null;
  }
  for (const d of [left, right].filter(Boolean)) {
    for (const [basis, n] of Object.entries(d.basis_counts || { [d.cost_basis || "legacy_estimate"]: 1 })) {
      result.basis_counts[basis] = (result.basis_counts[basis] || 0) + n;
    }
  }
  const bases = Object.keys(result.basis_counts);
  result.cost_basis = bases.length === 1 ? bases[0] : "mixed";
  result.unpriced_calls = result.basis_counts.unavailable || 0;
  return result;
}
