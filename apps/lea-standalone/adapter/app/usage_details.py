"""Shared normalization for historical and current accounting surfaces."""
import json
from .queries.usage import legacy_details, merge_details


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
