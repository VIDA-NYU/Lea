"""v2.4 error transparency: the Diagnostic contract, end to end.

The thing under test is a property the old error model could not have: **a failure
reaches the human, in the right place, and is still there after a reload.** So these
tests assert on the persisted row and the streamed payload together, not on log output.

Covered:
  * the code catalog — every code explains itself, and exception classification
    distinguishes a MISSING key from a REJECTED one (different remedies);
  * `store.add_diagnostic` round-trips and `session_detail` splits diagnostics out of
    `messages` (a diagnostic replayed to the model as conversation would be a bug);
  * `bridge.diagnose` stores and streams the SAME payload;
  * a prover `Diagnostic` event is dispatched, anchored, and persisted;
  * C2: an unreadable after-state is recorded as `content_lost`, NOT as an empty file;
  * E1: Stop during a pending approval reports `cancelled`, never a user "deny".
"""

from queue import Queue
from threading import Event

import pytest

from lea.interface import Diagnostic, FileChanged, Finished, TurnStarted
from lea.providers import Usage

from app import bridge, db, diagnostics, store
from app.config import LeaConfig


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()


def _drain(q: Queue) -> list[dict]:
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


# --- the catalog ---------------------------------------------------------------

def test_every_code_has_human_copy():
    # The point of a catalog is that copy can't be forgotten. An empty title would
    # mean a failure surfaced as a bare dotted code.
    for code, (title, remedy) in diagnostics.CATALOG.items():
        assert title and title.strip(), f"{code} has no title"
        assert remedy is None or remedy.strip(), f"{code} has a blank remedy"
        assert title != code, f"{code} title is just the code"


def test_unknown_code_still_surfaces():
    # A brand-new failure mode must not be invisible just because nobody wrote its
    # copy yet — that is the exact bug this phase exists to remove.
    out = diagnostics.resolve("step_error", "totally.new", "something broke")
    assert out["message"] == "something broke"
    assert out["title"]
    assert out["remedy"] is None


def test_missing_key_and_rejected_key_are_different_codes():
    class AuthenticationError(Exception):
        pass

    missing, _ = diagnostics.classify_exception(
        AuthenticationError("AuthenticationError: no api key provided for anthropic")
    )
    rejected, _ = diagnostics.classify_exception(
        AuthenticationError("Incorrect API key provided: sk-abc***")
    )
    # Telling someone to check a key they never set sends them to fix the wrong thing.
    assert missing == "provider.auth_missing"
    assert rejected == "provider.auth_invalid"
    assert diagnostics.CATALOG[missing][1] != diagnostics.CATALOG[rejected][1]


def test_deepseek_bad_key_is_auth_not_unknown_model():
    """Regression, observed live. DeepSeek rejecting a bad key arrives as LiteLLM
    `BadRequestError`, whose CLASS NAME maps to "unknown model" — so the card told the
    user to change a model that was fine and never mentioned the key that was wrong.
    The provider's own words are in the message; the text has to win over the type."""
    class BadRequestError(Exception):
        pass

    exc = BadRequestError(
        'litellm.BadRequestError: DeepseekException - {"error":{"message":'
        '"Authentication Fails, Your api key: abc is invalid","type":'
        '"authentication_error","param":null,"code":"invalid_request_error"}}'
    )
    out = diagnostics.analyze_exception(exc, model="deepseek/deepseek-chat", key_configured=True)

    assert out["code"] == "provider.auth_invalid"
    assert out["code"] != "provider.model_unknown"
    # The provider's sentence leads; the raw exception is demoted, not discarded.
    assert out["message"] == "Authentication Fails, Your api key: abc is invalid"
    assert "BadRequestError" not in out["message"]
    assert "BadRequestError" in out["detail"]
    assert "Deepseek" in out["title"]
    # Auth failure is ambiguous between a wrong key and a model from another
    # provider — offer both, since only the user can tell which.
    assert [a["focus"] for a in out["actions"]] == ["api-keys", "model"]
    assert "key" in out["remedy"].lower() and "model" in out["remedy"].lower()


def test_no_key_configured_is_distinguished_from_a_rejected_key():
    class AuthenticationError(Exception):
        pass

    exc = AuthenticationError('AnthropicException - {"error":{"message":"invalid x-api-key"}}')
    never_saved = diagnostics.analyze_exception(
        exc, model="anthropic/claude-x", key_configured=False)
    saved_but_bad = diagnostics.analyze_exception(
        exc, model="anthropic/claude-x", key_configured=True)
    unknown = diagnostics.analyze_exception(exc, model="anthropic/claude-x")

    assert never_saved["code"] == "provider.auth_missing"
    assert saved_but_bad["code"] == "provider.auth_invalid"
    # We cannot see keys exported into the environment, so with no information we must
    # not claim the key is absent — that would be a confident wrong statement.
    assert unknown["code"] == "provider.auth_invalid"
    assert "anthropic/claude-x" in never_saved["remedy"]


def test_a_real_unknown_model_still_says_so():
    # The guard must not swallow the case it was originally written for.
    class NotFoundError(Exception):
        pass

    out = diagnostics.analyze_exception(
        NotFoundError("litellm.NotFoundError: model 'gpt-99' does not exist"),
        model="openai/gpt-99",
    )
    assert out["code"] == "provider.model_unknown"
    assert [a["focus"] for a in out["actions"]] == ["model"]


# Real provider wordings, with the exception class LiteLLM actually wraps them in.
# The point of the matrix is the MISMATCHES: quota arrives as a rate-limit error,
# context overflow and content refusals arrive as bad requests. Classifying on the
# class name gets each of these wrong, with a remedy that wastes the user's time.
_PROVIDER_ERRORS = [
    ("RateLimitError",
     "You exceeded your current quota, please check your plan and billing details. "
     "code: insufficient_quota",
     "provider.quota_exceeded"),
    ("RateLimitError",
     "Your credit balance is too low to access the Anthropic API",
     "provider.quota_exceeded"),
    ("RateLimitError",
     "Rate limit reached for gpt-4 in organization org-x on requests per minute",
     "provider.rate_limited"),
    ("BadRequestError",
     "This model's maximum context length is 128000 tokens, however you requested "
     "190000 tokens. Please reduce the length of the messages.",
     "provider.context_too_long"),
    ("BadRequestError",
     "context_length_exceeded",
     "provider.context_too_long"),
    ("PermissionDeniedError",
     "Your organization must be verified to use this model; you do not have access to it",
     "provider.model_access_denied"),
    ("PermissionDeniedError",
     "Country, region, or territory not supported (unsupported_country_region_territory)",
     "provider.region_blocked"),
    ("BadRequestError",
     "The response was filtered due to the prompt triggering content_filter",
     "provider.content_filtered"),
    ("NotFoundError",
     "The model 'gpt-99' does not exist",
     "provider.model_unknown"),
    ("InternalServerError",
     "Overloaded (overloaded_error)",
     "provider.unavailable"),
    ("APITimeoutError",
     "Request timed out",
     "provider.unavailable"),
    ("AuthenticationError",
     "Incorrect API key provided: sk-abc",
     "provider.auth_invalid"),
]


@pytest.mark.parametrize("exc_name,message,expected", _PROVIDER_ERRORS)
def test_provider_error_matrix(exc_name, message, expected):
    exc = type(exc_name, (Exception,), {})(message)
    assert diagnostics.analyze_exception(exc, model="openai/gpt-4")["code"] == expected


def test_quota_is_not_reported_as_a_rate_limit():
    """The one most likely to waste someone's afternoon: providers return "out of
    credit" as a rate-limit error, and the rate-limit remedy is "wait and retry" —
    advice that can never work, because waiting does not add money to an account."""
    exc = type("RateLimitError", (Exception,), {})(
        "You exceeded your current quota. code: insufficient_quota")
    out = diagnostics.analyze_exception(exc, model="openai/gpt-4")
    assert out["code"] == "provider.quota_exceeded"
    assert "will not help" in out["remedy"]
    assert "wait" not in out["remedy"].lower()


def test_an_unrecognised_bad_request_makes_no_claim():
    """`BadRequestError` is LiteLLM's catch-all. Guessing "unknown model" for one it
    can't identify is exactly the bug that started this — better to say nothing than
    to send someone to change a model that was never the problem."""
    exc = type("BadRequestError", (Exception,), {})("something entirely novel broke")
    out = diagnostics.analyze_exception(exc, model="openai/gpt-4")
    assert out["code"] == "run.crashed"
    assert out["remedy"] is None
    assert out["actions"] == []


def test_provider_message_extraction():
    # Plain text with no embedded JSON has nothing to extract — the caller falls back
    # to the raw string rather than being handed an invented one.
    assert diagnostics.provider_message("something went wrong") is None
    assert diagnostics.provider_message('X - {"error":{"message":"be nice"}}') == "be nice"
    # Escapes survive the round trip.
    assert diagnostics.provider_message('{"error":{"message":"a \\"quoted\\" bit"}}') == 'a "quoted" bit'
    assert diagnostics.provider_name("DeepseekException - ...") == "Deepseek"
    assert diagnostics.provider_name("boom", "deepseek/chat") == "deepseek"


def test_unrecognised_exception_does_not_invent_a_remedy():
    code, remedy = diagnostics.classify_exception(RuntimeError("boom"))
    assert code == "run.crashed"
    assert remedy is None
    assert diagnostics.CATALOG["run.crashed"][1] is None


def test_severity_is_clamped():
    assert diagnostics.resolve("nonsense", "tool.raised", "x")["severity"] == "notice"


# --- persistence ---------------------------------------------------------------

def test_diagnostic_round_trips_and_is_not_a_message(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Prove it")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    payload = diagnostics.resolve(
        "step_error", "tool.raised", "search_mathlib failed: TimeoutError",
        context={"tool": "search_mathlib", "turn": 2},
    )
    store.add_diagnostic(session["id"], run["id"], payload, turn=2)

    detail = store.session_detail(session["id"])
    assert len(detail["diagnostics"]) == 1
    got = detail["diagnostics"][0]
    assert got["code"] == "tool.raised"
    assert got["severity"] == "step_error"
    assert got["context"]["tool"] == "search_mathlib"
    assert got["turn"] == 2
    # It must not leak into the conversation: `messages` is replayed to the model on
    # the next activation, and a diagnostic is not something the user or agent said.
    assert all(m["content"] != payload["message"] for m in detail["messages"])


def test_message_count_ignores_diagnostics(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Prove it")
    store.add_message(session["id"], "user", "hello")
    store.add_diagnostic(session["id"], None, diagnostics.resolve(
        "notice", "tool.raised", "x"))
    summary = next(s for s in store.list_sessions() if s["id"] == session["id"])
    assert summary["message_count"] == 1


def test_diagnose_streams_and_stores_the_same_payload(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Prove it")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    queue: Queue = Queue()

    bridge.diagnose(queue, session["id"], run["id"], "fatal", "provider.auth_missing",
                    "AuthenticationError: no api key", turn=1)

    events = _drain(queue)
    assert [e["type"] for e in events] == ["diagnostic"]
    streamed = events[0]["payload"]
    stored = store.session_detail(session["id"])["diagnostics"][0]
    # Same bytes on both paths — the invariant the code rows already hold, and the one
    # a client-side error string broke by construction.
    for field in ("code", "severity", "title", "message", "remedy"):
        assert streamed[field] == stored[field]
    assert streamed["remedy"], "a missing API key must tell the user what to do"


def test_a_diagnostic_that_cannot_be_stored_is_still_streamed(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Prove it")
    queue: Queue = Queue()

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "add_diagnostic", boom)
    bridge.diagnose(queue, session["id"], None, "fatal", "run.crashed", "it broke")

    events = _drain(queue)
    # Persistence is a bonus; visibility is the point.
    assert len(events) == 1 and events[0]["payload"]["persisted"] is False


# --- dispatch through a run ----------------------------------------------------

def _ctx(tmp_path, session, run, queue):
    return bridge.RunnerContext(
        session_id=session["id"], run_id=run["id"], task="Prove it",
        config=LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path),
        events=queue,
    )


def test_prover_diagnostic_is_persisted_and_anchored(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Prove it")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    queue: Queue = Queue()
    ctx = _ctx(tmp_path, session, run, queue)

    def fake(config, messages, **kwargs):
        yield TurnStarted(1)
        yield Diagnostic(
            severity="step_error", code="tool.raised",
            message="search_mathlib failed: TimeoutError",
            source="prover", context={"tool": "search_mathlib", "turn": 1},
        )
        yield Finished("completed", "done", 1, session["id"], "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {})

    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(ctx)

    stored = store.session_detail(session["id"])["diagnostics"]
    assert len(stored) == 1
    assert stored[0]["code"] == "tool.raised"
    assert stored[0]["context"]["tool"] == "search_mathlib"
    assert stored[0]["turn"] == 1
    assert stored[0]["source"] == "prover"


def test_unreadable_write_is_content_lost_not_empty(tmp_path, monkeypatch):
    # C2: the file was written but could not be read back. Storing "" would render on
    # the canvas as "Lea wrote an empty file" — a confident false claim about proof
    # content. The row must say it doesn't have the bytes.
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Prove it")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    queue: Queue = Queue()
    ctx = _ctx(tmp_path, session, run, queue)
    monkeypatch.setattr(bridge, "_read_after", lambda path: None)

    def fake(config, messages, **kwargs):
        yield TurnStarted(1)
        yield FileChanged(str(tmp_path / "Sqrt2.lean"))
        yield Finished("completed", "done", 1, session["id"], "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {})

    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(ctx)

    detail = store.session_detail(session["id"])
    step = detail["code_steps"][0]
    assert step["content_lost"] is True
    codes = [d["code"] for d in detail["diagnostics"]]
    assert "code.content_lost" in codes


def test_readable_write_is_not_marked_lost(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Prove it")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    queue: Queue = Queue()
    ctx = _ctx(tmp_path, session, run, queue)
    proof = tmp_path / "Sqrt2.lean"
    proof.write_text("theorem t : True := by trivial\n")

    def fake(config, messages, **kwargs):
        yield TurnStarted(1)
        yield FileChanged(str(proof))
        yield Finished("completed", "done", 1, session["id"], "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {})

    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(ctx)

    detail = store.session_detail(session["id"])
    assert detail["code_steps"][0]["content_lost"] is False
    assert detail["code_steps"][0]["code"].startswith("theorem")
    assert [d["code"] for d in detail["diagnostics"]] == []


def test_empty_file_is_not_content_lost(tmp_path, monkeypatch):
    # The distinction only matters if "empty" still round-trips as empty.
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Prove it")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    step = store.add_code_step(session["id"], run["id"], "Empty.lean", content="")
    assert step["content_lost"] is False
    assert store.session_detail(session["id"])["code_steps"][0]["code"] == ""


# --- E1: Stop during an approval ------------------------------------------------

def test_stop_during_approval_is_cancelled_not_denied(tmp_path, monkeypatch):
    """The tool must not run — but the model must not be told the USER declined it,
    and the human must be told their Stop is what ended it. Before v2.4 both facts
    were lost: the decision fell through to 'deny'."""
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Prove it")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    queue: Queue = Queue()
    stop_event = Event()
    stop_event.set()  # Stop pressed before the gate is answered

    class _Ev:
        tool_name = "bash"
        args = {"command": "ls"}

    decision = bridge._await_decision(run["id"], session["id"], _Ev(), queue, stop_event)

    assert decision == "cancelled"
    assert decision != "deny"
    codes = [d["code"] for d in store.session_detail(session["id"])["diagnostics"]]
    assert "approval.cancelled" in codes


def test_unanswerable_approval_still_defaults_to_deny(tmp_path, monkeypatch):
    # Without a Stop, an unresolvable/garbage decision must still fail SAFE.
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Prove it")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    queue: Queue = Queue()
    stop_event = Event()

    class _Ev:
        tool_name = "bash"
        args = {"command": "ls"}

    # Resolve with an invalid decision once the gate has registered. Polls with a
    # bounded wait; if it never registers, arm the stop flag so the call under test
    # cannot hang the suite (the assertion below is what fails, not the runner).
    import threading
    import time

    def resolve_soon():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            pending = bridge._pending_approvals.get(run["id"])
            if pending is not None:
                pending["decision"] = "wat"
                pending["event"].set()
                return
            time.sleep(0.01)
        stop_event.set()

    threading.Thread(target=resolve_soon, daemon=True).start()
    assert bridge._await_decision(run["id"], session["id"], _Ev(), queue, stop_event) == "deny"


# --- F2: overrides that didn't load ---------------------------------------------

def test_corrupt_overrides_are_reported_not_swallowed(tmp_path, monkeypatch):
    """The trap this covers: `load_overrides` handles its own failure and returns {},
    so a caller wrapping it in `try` catches NOTHING. The reason has to come back as a
    value or the user's configured sub-agents silently revert to vendored defaults."""
    from app import subagent_overrides

    path = tmp_path / "subagent-overrides.json"
    monkeypatch.setattr(subagent_overrides, "_OVERRIDES_PATH", path)

    # Absent file = no overrides configured. Not a failure, must not be reported.
    assert subagent_overrides.load_overrides_checked() == ({}, None)

    path.write_text("{not json at all")
    overrides, why = subagent_overrides.load_overrides_checked()
    assert overrides == {}
    assert why and "JSON" in why
    # And the lossy wrapper still behaves exactly as before for value-only callers.
    assert subagent_overrides.load_overrides() == {}

    # Partially usable: good entries apply, bad ones are named.
    path.write_text('{"proof-candidate": {"max_turns": 5}, "premise-search": "oops"}')
    overrides, why = subagent_overrides.load_overrides_checked()
    assert overrides == {"proof-candidate": {"max_turns": 5}}
    assert why and "premise-search" in why


def test_run_reports_unreadable_overrides(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    from app import subagent_overrides

    bad = tmp_path / "subagent-overrides.json"
    bad.write_text("{broken")
    monkeypatch.setattr(subagent_overrides, "_OVERRIDES_PATH", bad)

    session = store.create_session("Prove it")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    queue: Queue = Queue()
    ctx = _ctx(tmp_path, session, run, queue)

    def fake(config, messages, **kwargs):
        yield TurnStarted(1)
        yield Finished("completed", "done", 1, session["id"], "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {})

    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(ctx)

    diags = store.session_detail(session["id"])["diagnostics"]
    assert "settings.overrides_unreadable" in [d["code"] for d in diags]
    assert next(d for d in diags if d["code"] == "settings.overrides_unreadable")["severity"] == "degraded"


# --- D3: a child that stopped short ---------------------------------------------

def _child(reason, *, candidate=None):
    from lea.interface import SubagentFinished

    return SubagentFinished(
        result_id="pc-1", subagent_type="proof-candidate", candidate_path=candidate,
        check_status="ok" if candidate else None, check_detail=None,
        stop_reason=reason, summary="", transcript=[],
    )


@pytest.mark.parametrize(
    "reason,expected",
    [("max_turns", True), ("interrupted", True), ("assistant", True),
     ("completed", False), ("error", False)],
)
def test_stop_notice_covers_ran_but_stopped_short(reason, expected):
    # 'error' is `_subagent_error`'s job (the child never ran); 'completed' is a clean
    # finish. Everything else ran and was cut short — which used to render identically
    # to a clean finish that happened to produce nothing.
    assert (bridge._subagent_stop_notice(_child(reason)) is not None) is expected


def test_a_child_that_returned_a_candidate_is_never_called_empty_handed():
    """Observed live: a child's row said "ended without producing a candidate"
    directly above its own message describing the candidate it had just compiled
    cleanly. The notice keyed on the terminal reason alone and ignored the result
    sitting in the very same event."""
    ev = _child("assistant", candidate=".lea/tmp/run/agent/candidate.lean")
    assert bridge._subagent_stop_notice(ev) is None

    # And when a budget cap coincides with a real candidate, the budget fact is
    # reported without claiming it came back empty.
    capped = _child("max_turns", candidate=".lea/tmp/run/agent/candidate.lean")
    text, _ = bridge._subagent_stop_notice(capped)
    assert "budget" in text
    assert "no candidate" not in text


def test_no_candidate_notice_describes_what_was_recorded():
    # We can say what we captured. We cannot say what the child did — it may well have
    # written a proof somewhere we could not collect it from, which is exactly the case
    # that produced the contradiction.
    text, remedy = bridge._subagent_stop_notice(_child("assistant"))
    assert "recorded" in text
    assert "produced" not in text
    assert "scratch" in (remedy or "")


def test_every_emitted_code_is_in_the_catalog():
    """v2.5 G1 — the audit that keeps `CATALOG` honest, as a test rather than a one-off.

    A code with no catalog entry still surfaces (that is deliberate — `resolve` never
    drops an unknown one), but it surfaces with the raw message and NO remedy. Since the
    whole point of the code layer is that a failure explains itself, an unregistered code
    is a half-built failure mode. This walks both the adapter and the prover.
    """
    import re
    from pathlib import Path

    from app.diagnostics import CATALOG

    prefixes = ("mcp", "tool", "skill", "subagent", "settings", "run", "code",
                "approval", "asset", "import", "provider", "lean")
    pattern = re.compile(rf'"(({"|".join(prefixes)})\.[a-z_]+)"')
    roots = [Path(__file__).parent.parent / "app",
             Path(__file__).parent.parent.parent / "prover" / "lea"]

    emitted: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            emitted |= {m[0] for m in pattern.findall(path.read_text())}

    # `skill.md` is a FILENAME the pattern also matches; it is not a diagnostic code.
    emitted.discard("skill.md")
    missing = sorted(code for code in emitted if code not in CATALOG)
    assert not missing, f"emitted but not in CATALOG: {missing}"


def test_every_action_points_at_a_real_code():
    """A button attached to a code nothing emits is dead weight that reads as coverage."""
    from app.diagnostics import CATALOG, CODE_ACTIONS

    assert not [c for c in CODE_ACTIONS if c not in CATALOG]


def test_every_catalog_entry_has_a_title():
    from app.diagnostics import CATALOG

    assert not [c for c, (title, _) in CATALOG.items() if not (title or "").strip()]
