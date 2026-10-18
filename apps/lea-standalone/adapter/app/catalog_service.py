"""Shared model discovery. Network work never blocks a picker or mutates LiteLLM.

Each source is replaced only after a complete, validated response. Account lists
are scoped to a credential fingerprint and endpoint; secrets never enter the cache.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

from . import models_catalog
from .config import configured_provider_keys, provider_endpoint, write_private_text

LOG = logging.getLogger("lea-interface.catalog")
INTERVAL = 7 * 24 * 60 * 60
METADATA_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"


def bundled_catalog() -> dict:
    installation = Path(__file__).resolve().parents[2]
    for base in (installation, installation.parent.parent):
        path = base / "packages/lea-model-catalog/models.json"
        if path.is_file():
            return json.loads(path.read_text())
    raise RuntimeError("The shared model catalog is missing from this installation")


def timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def release_time(value) -> float:
    try:
        if isinstance(value, (float, int)):
            parsed = float(value)
        else:
            date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            parsed = date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
        return parsed if math.isfinite(parsed) and 0 <= parsed < 253402300799 else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def metadata_entries(data: dict, *, include_excluded=False) -> list[dict]:
    """Pricing metadata describes capabilities, never account access."""
    if not isinstance(data, dict):
        raise ValueError("Expected a model metadata object")
    rows = []
    for value, spec in data.items():
        if not isinstance(spec, dict) or value == "sample_spec":
            continue
        excluded = spec.get("mode") not in {"chat", "responses"} or spec.get("supports_function_calling") is False or spec.get("supports_streaming") is False
        if excluded and not include_excluded:
            continue
        provider = models_catalog._normalize_provider(str(spec.get("litellm_provider") or ""))
        if not provider or value == "sample_spec":
            continue
        compatibility = "supported" if spec.get("supports_function_calling") is True else "unverified"
        if excluded:
            compatibility = "unsupported"
        # The Responses bridge is covered for direct OpenAI. Other response
        # dialects remain discoverable without claiming execution support.
        if spec.get("mode") == "responses" and provider != "openai" and not excluded:
            compatibility = "unverified"
        row = dict(value=value, label=value, provider=provider, mode=spec.get("mode"),
                   compatibility=compatibility, availability="unknown", source="metadata",
                   released_at=release_time(spec.get("created_at")),
                   deprecated_at=spec.get("deprecation_date"),
                   retires_at=spec.get("shutdown_date"),
                   preview="preview" in value.lower(),
                   supports_function_calling=spec.get("supports_function_calling"))
        rows.append(row)
        for alias in spec.get("aliases", []):
            if isinstance(alias, str) and alias:
                rows.append({**row, "value": alias, "label": alias, "canonical_id": value})
    return rows


def configuration() -> dict:
    keys = {**os.environ, **configured_provider_keys()}
    result = {"metadata": {"url": METADATA_URL, "headers": {}}}
    for provider, envs, url in (
        ("openai", ("OPENAI_API_KEY",), (keys.get("OPENAI_API_BASE") or keys.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/") + "/models"),
        ("anthropic", ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"), "https://api.anthropic.com/v1/models"),
        ("gemini", ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "https://generativelanguage.googleapis.com/v1beta/models"),
        ("portkey", ("PORTKEY_API_KEY",), str(provider_endpoint("PORTKEY_BASE_URL")["value"]).rstrip("/") + "/models"),
    ):
        key = next((keys[e] for e in envs if keys.get(e)), "")
        if not key:
            continue
        headers = {"Authorization": f"Bearer {key}"}
        if provider == "anthropic":
            headers = {"anthropic-version": "2023-06-01"}
            headers["x-api-key" if keys.get("ANTHROPIC_API_KEY") else "Authorization"] = key if keys.get("ANTHROPIC_API_KEY") else f"Bearer {key}"
        elif provider == "gemini":
            headers = {"x-goog-api-key": key}
        elif provider == "portkey":
            headers["x-portkey-api-key"] = key
            for env, header in (("PORTKEY_VIRTUAL_KEY", "x-portkey-virtual-key"), ("PORTKEY_CONFIG", "x-portkey-config"), ("PORTKEY_PROVIDER", "x-portkey-provider")):
                if keys.get(env):
                    headers[header] = keys[env]
        result[provider] = {"url": url, "headers": headers}
    return result


def fetch_json(url: str, headers: dict) -> dict:
    # No redirects: credentials must stay on the configured provider/gateway.
    with httpx.Client(timeout=5, follow_redirects=False) as client:
        deadline = time.monotonic() + 20
        with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise TimeoutError("Model catalog download timed out")
                size += len(chunk)
                if size > 16 * 1024 * 1024:
                    raise ValueError("Model catalog response exceeds size limit")
                chunks.append(chunk)
    value = json.loads(b"".join(chunks))
    if not isinstance(value, dict):
        raise ValueError("Expected a model catalog object")
    return value


class CatalogService:
    def __init__(self, path: Path, *, sources=configuration, fetch=fetch_json, clock=time.time):
        self.path, self.sources, self.fetch, self.clock = path, sources, fetch, clock
        self.lock = threading.RLock()
        self.refresh_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.worker = None
        self.records = {}
        self.recent_models = []
        try:
            cached = json.loads(path.read_text())
            if cached.get("version") == 1 and isinstance(cached.get("sources"), dict):
                self.recent_models = [m for m in cached.get("recent_models", []) if isinstance(m, str) and m][:5]
                self.records = {k: v for k, v in cached["sources"].items()
                                if isinstance(v, dict) and isinstance(v.get("models"), list)
                                and all(isinstance(m, dict) and isinstance(m.get("value"), str)
                                        and isinstance(m.get("provider"), str) for m in v["models"])
                                and isinstance(v.get("updated_at"), (int, float))
                                and release_time(v["updated_at"]) == v["updated_at"]
                                and all(isinstance(v.get(field, 0), (int, float)) and math.isfinite(v.get(field, 0))
                                        for field in ("attempted_at", "next_attempt", "failures"))}
                for record in self.records.values():
                    record["models"] = [{**m, "first_seen": release_time(m.get("first_seen")),
                                         "released_at": release_time(m.get("released_at"))} for m in record["models"]]
                    if not record.get("error"):
                        # Apply the current cadence to caches saved by older versions.
                        record["next_attempt"] = record["updated_at"] + INTERVAL
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    @staticmethod
    def scope(config: dict) -> str:
        return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()

    def start(self):
        self.payload()  # Restore route/key hints before any new run can start.
        if os.environ.get("LEA_MODEL_CATALOG_OFFLINE", "").lower() == "true":
            return
        with self.lock:
            if self.worker and self.worker.is_alive():
                return
            self.stop_event.clear()
            self.worker = threading.Thread(target=self._loop, name="model-catalog", daemon=True)
            self.worker.start()

    def close(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.worker:
            self.worker.join(timeout=30)

    def request_refresh(self):
        if os.environ.get("LEA_MODEL_CATALOG_OFFLINE", "").lower() != "true":
            self.wake_event.set()

    def _save(self):
        with self.lock:
            write_private_text(self.path, json.dumps({"version": 1, "sources": self.records,
                                                     "recent_models": self.recent_models}, sort_keys=True))

    def remember_model(self, model: str):
        if model:
            with self.lock:
                self.recent_models = [model, *[m for m in self.recent_models if m != model]][:5]
                self._save()

    def _loop(self):
        while not self.stop_event.is_set():
            force = self.wake_event.is_set()
            self.wake_event.clear()
            try:
                self.refresh(force=force)
            except Exception:
                # Do not log URLs/exception text: providers can echo secrets.
                LOG.warning("Model catalog refresh failed; keeping cached data")
            self.wake_event.wait(60)

    def _read_source(self, name: str, config: dict) -> list[dict]:
        if name == "metadata":
            data = self.fetch(config["url"], config["headers"])
            rows = metadata_entries(data, include_excluded=True)
            if len(rows) < 100:
                raise ValueError("Incomplete model metadata")
            return rows
        rows, token, seen = [], "", set()
        deadline = time.monotonic() + 20
        for _ in range(100):
            if self.stop_event.is_set() or time.monotonic() > deadline:
                raise TimeoutError("Model listing interrupted")
            params = {}
            if name == "anthropic":
                params = {"limit": 100, **({"after_id": token} if token else {})}
            elif name == "gemini":
                params = {"pageSize": 100, **({"pageToken": token} if token else {})}
            elif token:
                params = {"after": token}
            url = config["url"] + (("&" if "?" in config["url"] else "?") + urlencode(params) if params else "")
            data = self.fetch(url, config["headers"])
            page = data.get("models" if name == "gemini" else "data")
            if not isinstance(page, list) or any(not isinstance(m, dict) for m in page):
                raise ValueError("Invalid model list")
            for model in page:
                identifier = model.get("name" if name == "gemini" else "id")
                if not isinstance(identifier, str) or not identifier.strip():
                    raise ValueError("Invalid model ID")
                if name == "gemini":
                    if "generateContent" not in model.get("supportedGenerationMethods", []):
                        continue
                    identifier = "gemini/" + identifier.removeprefix("models/")
                elif name == "portkey":
                    identifier = "portkey/" + identifier.removeprefix("portkey/")
                rows.append(dict(value=identifier, label=model.get("display_name") or model.get("displayName") or identifier,
                                 provider=name, availability="available", source=name,
                                 released_at=release_time(model.get("created_at") or model.get("created")),
                                 retires_at=model.get("retires_at") or model.get("shutdown_date"),
                                 lifecycle=model.get("lifecycle", "active"),
                                 preview="preview" in identifier.lower()))
            if name == "gemini":
                token = data.get("nextPageToken", "")
            elif data.get("has_more"):
                token = data.get("last_id") or (page[-1].get("id") if page else "")
                if not token:
                    raise ValueError("Missing pagination cursor")
            else:
                token = ""
            if not token:
                return rows
            if token in seen:
                raise ValueError("Repeated pagination cursor")
            seen.add(token)
        raise ValueError("Model list exceeded page limit")

    def refresh(self, force=False):
        if not self.refresh_lock.acquire(blocking=False):
            return
        try:
            configs = self.sources()
            now = self.clock()
            with self.lock:
                for name in list(self.records):
                    if name not in configs or self.records[name].get("scope") != self.scope(configs[name]):
                        del self.records[name]
                due = [(name, cfg) for name, cfg in configs.items() if now >= self.records.get(name, {}).get("next_attempt", 0)
                       or (force and now - self.records.get(name, {}).get("attempted_at", 0) >= 30)]
            def update(item):
                name, cfg = item
                try:
                    rows = self._read_source(name, cfg)
                    if name == "metadata" and len(rows) < len(self.records.get(name, {}).get("models", [])) / 2:
                        raise ValueError("Model metadata shrank unexpectedly")
                    return name, rows, None
                except Exception as exc:
                    # Only the status code/type is retained; never response bodies,
                    # credentials or a URL that could contain a key.
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    return name, None, f"HTTP {status}" if status else type(exc).__name__
            with ThreadPoolExecutor(max_workers=5) as pool:
                for name, rows, error in pool.map(update, due):
                    with self.lock:
                        old = self.records.get(name, {})
                        record = {**old, "scope": self.scope(configs[name]), "attempted_at": now, "error": error}
                        if error:
                            failures = old.get("failures", 0) + 1
                            record.update(failures=failures, next_attempt=now + min(INTERVAL, 60 * 2 ** min(failures, 9)))
                        else:
                            baseline = {m["value"] for m in bundled_catalog()["models"]}
                            if models_catalog.is_available():
                                baseline.update(models_catalog.litellm.model_cost)
                            first_seen = {value: 0 for value in baseline}
                            first_seen.update({m["value"]: m.get("first_seen", 0) for m in old.get("models", [])})
                            record.update(models=[{**m, "first_seen": first_seen.get(m["value"], now)} for m in rows],
                                          updated_at=now, next_attempt=now + INTERVAL, failures=0)
                        self.records[name] = record
            self._save()
        finally:
            self.refresh_lock.release()

    def payload(self, selected="") -> dict:
        now, configs = self.clock(), self.sources()
        offline = os.environ.get("LEA_MODEL_CATALOG_OFFLINE", "").lower() == "true"
        with self.lock:
            records = copy.deepcopy({k: v for k, v in self.records.items()
                                     if k in configs and v.get("scope") == self.scope(configs[k])})
            recent = list(self.recent_models)
        base = metadata_entries(models_catalog.litellm.model_cost, include_excluded=True) if models_catalog.is_available() else []
        rows = {m["value"]: m for m in base}
        bundled = bundled_catalog()["models"]
        bundled_ids = {m["value"] for m in bundled}
        for m in bundled:
            value = m["value"]
            rows[value] = {"compatibility": "unverified", "availability": "unknown", "source": "bundled",
                           **rows.get(value, {}), **m, "provider": "gemini" if m.get("family") == "google" else m.get("provider", m.get("family", "")),
                           "first_seen": release_time(m.get("first_seen")),
                           "released_at": release_time(m.get("released_at"))}
        metadata = records.get("metadata", {})
        for row in metadata.get("models", []):
            previous = rows.get(row["value"], {})
            rows[row["value"]] = {**previous, **row, "released_at": row.get("released_at") or previous.get("released_at", 0),
                                  "first_seen": row.get("first_seen") or previous.get("first_seen", 0)}
        status, warnings = {}, []
        for name in configs:
            record = records.get(name, {})
            updated = record.get("updated_at")
            stale = updated is None or now - updated >= INTERVAL or bool(record.get("error"))
            status[name] = {"updated_at": timestamp(updated) if updated is not None else None,
                            "stale": stale, "error": record.get("error"),
                            "source": "cached" if updated is not None else "bundled"}
            if stale:
                age = f"Last successful update: {timestamp(updated)}." if updated is not None else "Using bundled data."
                warnings.append({"code": "catalog.stale", "message": f"{name}: using saved model data; refresh pending." if not record.get("error")
                                 else f"{name}: refresh failed ({record['error']}); using saved model data."})
                warnings[-1]["message"] += " " + age
            if name == "metadata":
                continue
            available = {m["value"]: m for m in record.get("models", [])}
            identities = {v.removeprefix(name + "/") for v in available}
            for value, row in rows.items():
                if row["provider"] == name:
                    row["availability"] = "unknown" if stale else ("available" if value.removeprefix(name + "/") in identities else "unavailable")
            for value, row in available.items():
                previous = rows.get(value, {})
                rows[value] = {"compatibility": "unverified", **previous, **row,
                               "released_at": row.get("released_at") or previous.get("released_at", 0),
                               "availability": "unknown" if stale else "available"}
        for row in rows.values():
            retirement = release_time(row.get("retires_at"))
            if retirement and retirement <= now or row.get("lifecycle") in {"retired", "shutdown"}:
                row["availability"] = "retired"
        for choice in [selected, *recent]:
            if not choice or choice in rows:
                continue
            rows[choice] = dict(value=choice, label=choice, provider=models_catalog.provider_for(choice) or "",
                                  compatibility="unverified", availability="unknown", source="custom")
        models = sorted((row for row in rows.values() if row.get("compatibility") != "unsupported" or row["value"] == selected),
                        key=lambda m: (m["provider"], m["value"]))
        models_catalog.set_catalog_models(models)
        try:
            from lea.providers import set_catalog_models
            set_catalog_models(models)
        except ImportError:
            pass  # Settings stays usable when the optional provider runtime fails.
        # Rotate suggestions by discovery/release metadata, never by a guessed
        # model-number ordering or a "most capable" claim.
        suggested, counts, canonical = [], {}, set()
        ranked = sorted(models, key=lambda m: (m["value"] != selected,
                        recent.index(m["value"]) if m["value"] in recent else len(recent),
                        not bool(release_time(m.get("released_at"))),
                        m["value"] not in bundled_ids,
                        m.get("preview", False),
                        -(release_time(m.get("released_at")) or m.get("first_seen", 0)),
                        m.get("compatibility") != "supported", m["value"]))
        for row in ranked:
            provider = row["provider"]
            identity = (provider, row.get("canonical_id", row["value"]).removeprefix(provider + "/"))
            bucket = "released" if release_time(row.get("released_at")) else "bundled" if row["value"] in bundled_ids else "discovered"
            group = (provider, bucket)
            pinned = row["value"] == selected or row["value"] in recent
            if identity in canonical:
                continue
            if row["value"] != selected and (row.get("availability") in {"retired", "unavailable"} or row.get("deprecated_at")):
                continue
            if not pinned and (provider not in {"openai", "anthropic", "gemini", "portkey"}
                               or counts.get(group, 0) >= (3 if bucket == "released" else 2)
                               or (bucket == "discovered" and not row.get("first_seen"))):
                continue
            suggested.append({**row, "family": provider, "suggestion_reason": "new" if bucket == "discovered" else bucket})
            canonical.add(identity)
            if not pinned:
                counts[group] = counts.get(group, 0) + 1
        revision = hashlib.sha256(json.dumps([models, status, suggested], sort_keys=True).encode()).hexdigest()[:16]
        updated = max((v.get("updated_at", 0) for v in records.values()), default=0)
        if offline:
            warnings.append({"code": "catalog.offline", "message": "Automatic model updates are disabled by offline mode."})
        return dict(models=models, featured=suggested, revision=revision, sources=status, warnings=warnings,
                    updated_at=timestamp(updated) if updated else None,
                    updates_disabled=offline,
                    degraded=any(s["stale"] for s in status.values()), refreshing=self.refresh_lock.locked() or self.wake_event.is_set())


_service = None


def service() -> CatalogService:
    global _service
    if _service is None:
        from .db import DB_PATH
        _service = CatalogService(DB_PATH.parent / "model-catalog.json")
    return _service
