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

"""Shared normalization for historical and current accounting surfaces."""
import json



def decode(row: dict) -> dict:
    raw = row.pop("usage_details_json", None)
    row["usage_details"] = json.loads(raw) if raw else row.get("usage_details") or legacy_details()
    return row


def encode(details) -> str | None:
    return json.dumps(details, sort_keys=True) if details is not None else None


def aggregate(rows) -> dict | None:
    result = None
    for row in rows:
        row = dict(row)
        if row.get("input_tokens") or row.get("output_tokens") or row.get("cost_usd") or row.get("usage_details_json"):
            result = merge_details(result, decode(row)["usage_details"])
    return result
