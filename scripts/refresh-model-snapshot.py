"""Refresh the shared offline snapshot from LiteLLM's public capability metadata.

This is release maintenance, independent of account-scoped runtime discovery.
Only data is downloaded; existing defaults and provider configuration are retained.
"""
import argparse
import json
import os
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
TARGET = Path(__file__).resolve().parents[1] / "packages/lea-model-catalog/models.json"


def generate(current, metadata):
    if not isinstance(metadata, dict) or len(metadata) < 100:
        raise ValueError("Incomplete upstream metadata; keeping the existing snapshot")
    models = {}
    for value, spec in metadata.items():
        if not isinstance(spec, dict) or spec.get("mode") not in {"chat", "responses"}:
            continue
        provider = spec.get("litellm_provider")
        if provider not in {"openai", "anthropic", "gemini"} or spec.get("supports_function_calling") is not True:
            continue
        for name in [value, *spec.get("aliases", [])]:
            if not isinstance(name, str) or not name:
                continue
            # Ignore routed duplicates. Keep exact native IDs and Gemini's route.
            if "/" in name and not name.startswith(provider + "/"):
                continue
            if provider != "gemini":
                name = name.removeprefix(provider + "/")
            entry = dict(value=name, label=name, family="google" if provider == "gemini" else provider,
                         mode=spec["mode"], compatibility="supported", preview="preview" in name)
            if spec.get("deprecation_date"):
                entry["deprecated_at"] = spec["deprecation_date"]
            if spec.get("created_at"):
                entry["released_at"] = spec["created_at"]
            models[name] = entry
    if any(not any(m["family"] == family for m in models.values()) for family in ("openai", "anthropic", "google")):
        raise ValueError("A primary provider is missing; refusing an incomplete snapshot")
    previous = {m["value"]: m for m in current["models"]}
    observed_at = datetime.now(timezone.utc).isoformat()
    for name, entry in models.items():
        if not entry.get("released_at") and previous.get(name, {}).get("released_at"):
            entry["released_at"] = previous[name]["released_at"]
        entry["first_seen"] = previous.get(name, {}).get("first_seen", observed_at)
    # Never discard the installation's default merely because metadata lags.
    if current["default_model"] not in models:
        models[current["default_model"]] = previous[current["default_model"]]
    result = {**current, "schema_version": 1,
              "models": sorted(models.values(), key=lambda m: (m["family"], m["value"]))}
    if result["models"] != current["models"]:
        result["generated_at"] = datetime.now(timezone.utc).isoformat()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Use a local metadata fixture instead of downloading")
    args = parser.parse_args()
    if args.input:
        metadata = json.loads(args.input.read_text())
    else:
        with urllib.request.urlopen(URL, timeout=20) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError("Upstream metadata exceeds size limit")
            metadata = json.loads(raw)
    result = generate(json.loads(TARGET.read_text()), metadata)
    fd, temporary = tempfile.mkstemp(dir=TARGET.parent, prefix=".models-")
    try:
        with os.fdopen(fd, "w") as output:
            output.write(json.dumps(result, indent=2) + "\n")
        os.replace(temporary, TARGET)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
