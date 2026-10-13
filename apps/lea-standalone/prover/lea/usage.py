"""Additive usage metadata. Token details are subsets, never extra tokens."""

DETAIL_TOKENS = ("cached_input_tokens", "cache_write_tokens", "reasoning_tokens")


def legacy_details() -> dict:
    return {"version": 1, "cost_basis": "legacy_estimate", "basis_counts": {"legacy_estimate": 1}, "details_missing_calls": 1}


def merge_details(left: dict | None, right: dict | None) -> dict | None:
    """Combine calls without turning unknown cache counts or pricing into zero."""
    if left is None and right is None:
        return None
    left, right = left or {}, right or {}
    result = {"version": 1}
    for key in DETAIL_TOKENS:
        values = [d[key] for d in (left, right) if d.get(key) is not None]
        result[key] = sum(values) if values else None
    counts = {}
    for detail in (left, right):
        for basis, count in (detail.get("basis_counts") or
                             ({detail["cost_basis"]: 1} if detail.get("cost_basis") else {})).items():
            counts[basis] = counts.get(basis, 0) + count
    result["basis_counts"] = counts
    result["cost_basis"] = next(iter(counts)) if len(counts) == 1 else "mixed"
    result["unpriced_calls"] = counts.get("unavailable", 0)
    result["details_missing_calls"] = sum(d.get("details_missing_calls", 0) for d in (left, right))
    return result
