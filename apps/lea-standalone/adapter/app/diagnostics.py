"""The error-code catalog: the one place a failure's human copy lives (v2.4, A4).

Before this, user-facing failure text was written inline at each `except` site — so
the copy the human read was whatever the exception happened to stringify to
(`AuthenticationError: ...`), and half the failures had no copy at all because they
were `logger.warning`s. Two consequences this module fixes:

  * **Wording lives once.** A `code` (`provider.auth_missing`) resolves to a title
    and a remedy here, not in a handler. Changing how a failure explains itself is a
    one-line edit in one file.
  * **"Does this failure explain itself?" is a unit test.** `test_diagnostics.py`
    asserts every code in `CATALOG` has a non-empty title, and that every code the
    codebase emits is in `CATALOG` — impossible to assert about ad-hoc strings.

Resolution happens SERVER-SIDE: `resolve()` stamps title/remedy on before the
diagnostic is stored and streamed, so the browser renders what it is handed and
there is no second catalog to keep in sync (the failure mode
`packages/lea-model-catalog` exists to avoid). The frontend keys only off
`severity` (which decides the shape of the surface) and `context` (which decides
where it anchors).
"""

from __future__ import annotations

import json
import re
from typing import Any

# code -> (title, remedy). `remedy` is None when there is no action the user can
# take — an honest None beats inventing advice, which is worse than silence
# because it sends someone off to fix the wrong thing.
CATALOG: dict[str, tuple[str, str | None]] = {
    # --- provider / config (Phase B) ---
    "provider.auth_missing": (
        "No API key is saved for this model's provider",
        "Add the key in Settings → API keys, or pick a model from a provider you have "
        "already configured.",
    ),
    "provider.auth_invalid": (
        "The provider rejected the API key",
        # An auth rejection is genuinely ambiguous between two different mistakes, and
        # the user is the only one who can tell them apart — so name both rather than
        # guessing one. Guessing is what sent someone to change a perfectly good model.
        "Two things cause this: the saved key is wrong or expired, OR the selected "
        "model belongs to a different provider than the key you saved. Check the key "
        "first, then the model.",
    ),
    "provider.rate_limited": (
        "The provider rate-limited this run",
        "Too many requests too quickly. Wait a moment and retry, or switch to a "
        "different model in Settings.",
    ),
    "provider.quota_exceeded": (
        "This account is out of credit or quota",
        # Deliberately NOT "wait and retry". Providers return this as a rate-limit
        # error, but no amount of waiting adds money to the account — sending someone
        # away to wait on a billing problem costs them the whole afternoon.
        "Retrying will not help. Add credit or raise the quota in the provider's "
        "billing console, or switch to a model on an account that has budget.",
    ),
    "provider.model_access_denied": (
        "This API key is not allowed to use this model",
        "The key is valid but has no access to this model — some providers gate models "
        "by tier, project, or scope. Pick a different model, or use a key with access.",
    ),
    "provider.context_too_long": (
        "The conversation is too long for this model",
        "Run /compact to shrink the history, start a new session for the next step, or "
        "switch to a model with a larger context window.",
    ),
    "provider.content_filtered": (
        "The provider's safety filter refused this request",
        "Rephrase the request, or switch to a different model in Settings.",
    ),
    "provider.region_blocked": (
        "The provider does not serve this region",
        "This provider is unavailable from your country or region. Switch to a "
        "different provider's model in Settings.",
    ),
    "provider.unavailable": (
        "Could not reach the model provider",
        "Check your network connection, then retry the run.",
    ),
    "provider.model_unknown": (
        "The provider does not recognise this model",
        "Pick a different model in Settings — the configured model id was rejected.",
    ),
    "run.crashed": ("The run stopped on an unexpected error", None),
    "run.project_unavailable": (
        "This run is not attached to its project",
        "The run continues without project context, instructions, or skills. Re-check "
        "the project in the Projects hub.",
    ),
    # --- tools / steps (Phase C) ---
    "tool.raised": ("A tool failed", None),
    "tool.unknown": (
        "The model called a tool that is not registered",
        "Check the run's toolset — a sub-agent profile or MCP server may name a tool "
        "that did not load.",
    ),
    "mcp.server_failed": (
        "An MCP server did not start",
        "Its tools were unavailable for this run; the proof continued without them. "
        "The server's own error is shown below it — most often the command isn't "
        "installed, or the Lean project path doesn't point at a folder with a "
        "lean-toolchain and a lakefile.",
    ),
    "mcp.no_tools": (
        "An MCP server offered no tools",
        "It started but listed nothing, so it added nothing to the proof. Check that it "
        "is pointed at the right project, or remove it under Library → MCP servers.",
    ),
    "lean.lsp_cold_fallback": (
        "Lean checks are running cold",
        "Checks are ~440x slower until the language-server daemon comes back. "
        "Restarting the adapter rebuilds it.",
    ),
    "code.content_lost": (
        "A written file could not be read back",
        "The step is recorded, but its contents are not stored — the file may have "
        "been deleted or replaced while the run was writing it.",
    ),
    # --- sub-agents (Phase D) ---
    "tool.auth_missing": (
        "A tool needs an API key that is not saved",
        "The tool could not run. Add the key it names under Settings → API keys.",
    ),
    "tool.name_clash": (
        "A custom tool was not loaded",
        "Its name is already taken by a built-in or another tool, so it was skipped. "
        "Rename it under Library → Tools.",
    ),
    "skill.files_incomplete": (
        "Some of a skill's reference material is missing",
        "The skill itself loaded, but Lea cannot open the listed files. Re-import the "
        "skill under Library → Skills.",
    ),
    "subagent.role_unavailable": (
        "A sub-agent role could not be prepared",
        "The role exists in your Library but was not offered to the agent for this run, "
        "so it could not be used. Re-save it under Library → Sub-agents.",
    ),
    "subagent.tool_dropped": (
        "A sub-agent asked for a tool that no longer exists",
        "It ran without that tool. The tool was probably deleted, or the MCP server "
        "providing it is turned off — edit the role under Library → Sub-agents, or "
        "restore the tool.",
    ),
    "subagent.spawn_failed": ("A sub-agent could not be started", None),
    "subagent.promotion_rejected": (
        "The winning sub-agent proof did not survive re-verification",
        "The candidate compiled in the sub-agent's scratch directory but not at the "
        "session's canonical path, so it was not promoted. Its session is still "
        "readable under Sub-agents.",
    ),
    "subagent.promotion_failed": ("The winning sub-agent proof could not be written", None),
    "subagent.stopped_early": ("A sub-agent stopped before finishing", None),
    # --- approvals (Phase E) ---
    "approval.cancelled": (
        "A tool approval was cancelled by Stop",
        "The tool did not run. The agent was told the call was declined, so it will "
        "ask how to proceed rather than retry.",
    ),
    # --- assets / settings (Phase F) ---
    "asset.read_failed": ("A project file could not be read", None),
    "asset.write_failed": ("A project file could not be written", None),
    "import.incomplete": (
        "The repository import did not complete",
        "Some files may be missing from the project. Re-run the import.",
    ),
    "settings.overrides_unreadable": (
        "Your sub-agent overrides did not apply",
        "The overrides file could not be read, so every sub-agent used its built-in "
        "defaults. Re-save them on the Sub-agents page.",
    ),
    "settings.catalog_unavailable": (
        "The model catalog did not load",
        "The model picker is showing a built-in fallback list, which may be out of date.",
    ),
}

SEVERITIES = ("fatal", "step_error", "degraded", "notice")

# Offers the UI can render as buttons on a card. A remedy that says "check the key in
# Settings" is still work for the user to go and find; an action takes them there.
ACTION_API_KEYS = {"label": "Check API keys", "action": "open-settings", "focus": "api-keys"}
ACTION_MODEL = {"label": "Change model", "action": "open-settings", "focus": "model"}
# v2.5 G4: the Library destinations. A remedy that says "check it under Library → MCP
# servers" is still work for the user to go and find; an action takes them there.
ACTION_MCP = {"label": "Open MCP servers", "action": "open-library", "focus": "mcp"}
ACTION_SUBAGENTS = {"label": "Open Sub-agents", "action": "open-library", "focus": "subagents"}
ACTION_SKILLS = {"label": "Open Skills", "action": "open-library", "focus": "skills"}

# code -> the buttons worth offering. Only where there IS somewhere useful to go: an
# action that lands on a page the user can do nothing with is worse than no button.
CODE_ACTIONS: dict[str, list[dict]] = {
    "mcp.server_failed": [ACTION_MCP],
    "mcp.no_tools": [ACTION_MCP],
    "subagent.tool_dropped": [ACTION_SUBAGENTS, ACTION_MCP],
    "subagent.role_unavailable": [ACTION_SUBAGENTS],
    "skill.files_incomplete": [ACTION_SKILLS],
    "tool.auth_missing": [ACTION_API_KEYS],
    "settings.overrides_unreadable": [ACTION_SUBAGENTS],
    "provider.auth_missing": [ACTION_API_KEYS, ACTION_MODEL],
}

# Exception-type names that identify a provider failure, mapped to a code. Matched
# by CLASS NAME rather than by importing LiteLLM's exception types: the adapter must
# not take a hard dependency on the prover's model SDK, and LiteLLM re-exports these
# names across versions.
_EXC_CODES = {
    "AuthenticationError": "provider.auth_invalid",
    "PermissionDeniedError": "provider.auth_invalid",
    "RateLimitError": "provider.rate_limited",
    "APIConnectionError": "provider.unavailable",
    "APITimeoutError": "provider.unavailable",
    "ServiceUnavailableError": "provider.unavailable",
    "InternalServerError": "provider.unavailable",
    "NotFoundError": "provider.model_unknown",
    # NOTE: `BadRequestError` is deliberately NOT here. LiteLLM uses it as a catch-all
    # — a rejected key, a context overflow and a content refusal all arrive as one —
    # so mapping the class to any single meaning guarantees wrong advice for the rest.
    # It is classified by message signal, or not at all.
}

# Substrings that identify a MISSING key rather than a rejected one. LiteLLM reports
# both as AuthenticationError, but they are different problems with different
# remedies — "add a key" vs "your key is wrong" — and telling someone to check a key
# they never set is the kind of misdirection this module exists to prevent.
_MISSING_KEY_HINTS = (
    "api key not provided", "no api key", "missing api key", "set the api key",
    "api_key must be set", "could not resolve authentication", "no auth credentials",
)

# Substrings that mean AUTHENTICATION regardless of what the exception class is called.
#
# This exists because the exception type is NOT reliable. Observed live: DeepSeek
# rejecting a bad key arrives as LiteLLM `BadRequestError`, whose class name maps to
# "unknown model" — so the card told the user to change a model that was perfectly
# fine, and said nothing about the key that was actually wrong. LiteLLM normalises
# provider errors inconsistently, but the provider's own words are right there in the
# message. The text wins over the class name.
_AUTH_SIGNALS = (
    "authentication_error", "authenticationerror", "authentication fails",
    "invalid api key", "invalid_api_key", "api key is invalid", "incorrect api key",
    "invalid authentication", "unauthorized", "your api key", "no api key",
    "api key not provided",
)

# Message signals, checked IN THIS ORDER before falling back to the exception class.
#
# Order is the whole design. Providers overload their error types — OpenAI reports
# "you exceeded your current quota" (a billing problem) as a *RateLimitError*, so
# quota must be tested before rate limiting or the user is told to wait for something
# that waiting cannot fix. Likewise `context_length_exceeded` and content refusals
# arrive as *BadRequestError*, LiteLLM's catch-all, which is why class-name-first
# classification told a user to change a model that was never the problem.
_SIGNAL_CODES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("provider.quota_exceeded", (
        "insufficient_quota", "exceeded your current quota", "quota exceeded",
        "insufficient credit", "insufficient balance", "billing", "payment required",
        "credit balance is too low", "out of credits", "spending limit",
    )),
    ("provider.region_blocked", (
        "unsupported_country_region_territory", "country, region, or territory",
        "not available in your region", "not supported in your country",
    )),
    ("provider.model_access_denied", (
        "do not have access to", "does not have access to", "not allowed to access",
        "model_not_allowed", "no access to model", "not authorized to use",
        "your account is not authorized",
    )),
    ("provider.context_too_long", (
        "context_length_exceeded", "maximum context length", "context window",
        "too many tokens", "reduce the length of the messages", "prompt is too long",
        "input length and `max_tokens` exceed",
    )),
    ("provider.content_filtered", (
        "content_filter", "content policy", "safety filter", "responsible ai",
        "flagged by", "content_policy_violation",
    )),
    ("provider.rate_limited", (
        "rate limit", "rate_limit", "too many requests", "429",
        "requests per minute", "tokens per minute",
    )),
    ("provider.unavailable", (
        "overloaded_error", "overloaded", "service unavailable", "bad gateway",
        "temporarily unavailable", "timeout", "timed out", "connection error",
    )),
    ("provider.model_unknown", (
        "does not exist", "model not found", "unknown model", "invalid model",
        "no such model", "model_not_found",
    )),
)

# `{"error": {"message": "...."}}`, possibly nested inside prose. The provider's own
# sentence is almost always clearer than LiteLLM's wrapper, and infinitely clearer
# than the exception class name.
_JSON_MESSAGE_RE = re.compile(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"')
# `DeepseekException`, `AnthropicException`, ... — the provider that actually answered.
_PROVIDER_EXC_RE = re.compile(r"\b([A-Z][A-Za-z]+?)Exception\b")


def provider_message(text: str) -> str | None:
    """The human sentence the PROVIDER returned, dug out of LiteLLM's wrapper.

    `litellm.BadRequestError: DeepseekException - {"error":{"message":"Authentication
    Fails, Your api key: abc is invalid","type":"authentication_error",...}}`
    becomes `Authentication Fails, Your api key: abc is invalid`. Returns None when
    there is no embedded message to find, so the caller can fall back to the raw text
    rather than inventing one."""
    start = text.find("{")
    if start != -1:
        try:
            blob = json.loads(text[start:])
            err = blob.get("error") if isinstance(blob, dict) else None
            msg = err.get("message") if isinstance(err, dict) else None
            if isinstance(msg, str) and msg.strip():
                return msg.strip()
        except (ValueError, AttributeError):
            pass  # not clean JSON — fall through to the regex
    match = _JSON_MESSAGE_RE.search(text)
    if match:
        try:
            return json.loads(f'"{match.group(1)}"').strip() or None
        except ValueError:
            return match.group(1).strip() or None
    return None


def provider_name(text: str, model: str | None = None) -> str | None:
    """Which provider actually refused — from `XxxException` in the message, else the
    model id's prefix (`deepseek/deepseek-chat` -> `deepseek`)."""
    match = _PROVIDER_EXC_RE.search(text)
    if match and match.group(1).lower() != "litellm":
        return match.group(1)
    if model and "/" in model:
        return model.split("/", 1)[0]
    return None


def analyze_exception(
    exc: BaseException,
    *,
    model: str | None = None,
    key_configured: bool | None = None,
) -> dict:
    """Turn a raised exception into everything a diagnostic needs: which failure it is,
    what to SAY, and what the user can DO about it.

    `key_configured` distinguishes the two auth cases that look identical from the
    exception alone — "you never saved a key for this provider" from "the key you
    saved was rejected". It is passed in (rather than looked up here) because only the
    caller knows the run's model, and because a key can also arrive via the
    environment, where the adapter cannot see it: `None` means "don't know", and we
    then avoid claiming either way.

    Returns `{code, title, message, detail, remedy, actions}`. `detail` is the raw
    exception, kept but demoted — useful when the friendly message isn't enough,
    noise when it is.
    """
    name = type(exc).__name__
    raw = f"{name}: {exc}"
    text = str(exc).lower()
    friendly = provider_message(str(exc))
    who = provider_name(str(exc), model)

    # Precedence, most specific first. The MESSAGE always beats the class name,
    # because the class is what the provider's error was flattened into and the
    # message is what it actually said.
    auth_by_message = any(signal in text for signal in _AUTH_SIGNALS)
    # A bare KeyError('ANTHROPIC_API_KEY') from an env lookup is a missing key.
    if name == "KeyError" and "api_key" in text.replace("'", ""):
        auth_by_message, key_configured = True, False
    signal_code = next(
        (c for c, signals in _SIGNAL_CODES if any(s in text for s in signals)),
        None,
    )
    # `PermissionDeniedError` covers several distinct refusals — a bad key, a model
    # your tier can't touch, a blocked region. Treat the class as "auth" only when no
    # sharper signal identified which one, or a region block reads as a key problem
    # and the user goes hunting through Settings for a key that was always fine.
    is_auth = auth_by_message or (
        name in ("AuthenticationError", "PermissionDeniedError") and signal_code is None
    )

    if is_auth:
        missing = any(h in text for h in _MISSING_KEY_HINTS) or key_configured is False
        code = "provider.auth_missing" if missing else "provider.auth_invalid"
        title, remedy = CATALOG[code]
        if who:
            title = (f"No API key saved for {who}" if missing
                     else f"{who} rejected the API key")
        if missing and model:
            remedy = (
                f"No key is saved for {who or 'this provider'}, which is what "
                f"'{model}' needs. Add it in Settings → API keys, or switch to a model "
                "from a provider you have configured."
            )
        return {
            "code": code,
            "title": title,
            "message": friendly or raw,
            "detail": raw if friendly else None,
            "remedy": remedy,
            # BOTH offers: an auth rejection cannot distinguish "wrong key" from
            # "model belongs to another provider", and the user can.
            "actions": [ACTION_API_KEYS, ACTION_MODEL],
        }

    # The signal match (a quota problem dressed as a rate limit, a context overflow
    # dressed as a bad request), else the class name — and only where the class is
    # UNAMBIGUOUS. `BadRequestError` is deliberately absent from `_EXC_CODES`: it is
    # LiteLLM's catch-all, and mapping it to "unknown model" is what produced a
    # confident wrong remedy. An unmatched failure gets the generic `run.crashed`,
    # which promises nothing.
    code = signal_code or _EXC_CODES.get(name, "run.crashed")

    title, remedy = CATALOG.get(code, (None, None))
    if code == "provider.model_unknown" and model:
        title = f"'{model}' was rejected by the provider"
    if code in ("provider.model_access_denied", "provider.quota_exceeded") and who:
        title = f"{title} ({who})"
    # Only offer what can actually help. A model switch fixes an access, context, or
    # unknown-model problem; it does nothing for a transient outage, so no button.
    actions = (
        [ACTION_MODEL]
        if code in ("provider.model_unknown", "provider.model_access_denied",
                    "provider.context_too_long", "provider.content_filtered",
                    "provider.region_blocked", "provider.quota_exceeded")
        else []
    )
    if code in ("provider.model_access_denied", "provider.quota_exceeded"):
        actions = [ACTION_API_KEYS, ACTION_MODEL]
    return {
        "code": code,
        "title": title,
        "message": friendly or raw,
        "detail": raw if friendly else None,
        "remedy": remedy,
        "actions": actions,
    }


def classify_exception(exc: BaseException) -> tuple[str, str | None]:
    """`(code, remedy)` only — the narrow form, kept for callers that just need to
    label a failure. `analyze_exception` is the one that produces user-facing copy."""
    result = analyze_exception(exc)
    return result["code"], result["remedy"]


def resolve(
    severity: str,
    code: str,
    message: str,
    *,
    source: str = "adapter",
    remedy: str | None = None,
    context: dict[str, Any] | None = None,
    title: str | None = None,
    detail: str | None = None,
    actions: list[dict] | None = None,
) -> dict:
    """Build the wire/DB payload for one diagnostic, filling title + remedy from the
    catalog. An explicit `remedy` wins over the catalog's (a caller that knows more
    about this specific failure than the generic code does).

    An UNKNOWN code is not an error here: it is stored and shown with the raw message
    and no remedy. Dropping it — or raising — would mean a brand-new failure mode is
    invisible precisely because nobody had written its copy yet, which is the bug
    this whole phase is about.
    """
    # G4: fill in the code's buttons when the caller didn't supply any.
    if actions is None:
        actions = CODE_ACTIONS.get(code)
    catalog_title, catalog_remedy = CATALOG.get(
        code, (code.replace(".", " ").replace("_", " ").capitalize(), None)
    )
    return {
        "severity": severity if severity in SEVERITIES else "notice",
        "code": code,
        "title": title or catalog_title,
        "message": message,
        # The raw exception, when a friendlier message is leading. Kept so nothing is
        # hidden, shown collapsed so it isn't the first thing a user reads.
        "detail": detail,
        "remedy": remedy or catalog_remedy,
        "actions": actions or [],
        "source": source,
        "context": {k: v for k, v in (context or {}).items() if v is not None},
    }
