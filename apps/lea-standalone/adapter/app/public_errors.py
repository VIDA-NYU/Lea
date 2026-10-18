"""Bounded public result details, safe for durable history and live/replayed SSE."""
import os
import re
from collections.abc import Iterable



_CREDENTIAL_LIKE = re.compile(
    r"(?:sk-(?:ant-)?[A-Za-z0-9_./+\-=]{8,}|AIza[A-Za-z0-9_-]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})"
)


def redact(text: str, *, secrets: Iterable[str] = ()) -> str:
    values = set(secrets)
    try:
        if os.environ.get('LEA_PROCESS_ROLE') != 'api':
            from .config import configured_provider_keys
            values.update(configured_provider_keys().values())
    except Exception:
        pass  # A damaged config must not replace the original error.
    values.update(value for name, value in os.environ.items() if name.endswith("_API_KEY"))
    for secret in sorted(values, key=len, reverse=True):
        if len(secret) >= 8:
            text = text.replace(secret, "[redacted]")
    return _CREDENTIAL_LIKE.sub("[redacted]", text)[:2000]


def run_detail(row: dict | None) -> dict | None:
    if row and row.get("result_detail"):
        row = {**row, "result_detail": redact(row["result_detail"])}
    return row
