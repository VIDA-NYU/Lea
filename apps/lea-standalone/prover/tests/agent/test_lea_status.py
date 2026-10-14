"""Reporting is an acknowledged boundary between tools, with no extra model call."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import copy
from lea import agent
from lea.config import LeaConfig
from lea.providers import ToolCall, Done, Usage
from lea.events import FileChanged, Finished, ToolResulted
from lea.status_reporting import LeaStatusUpdateRequested, LeaStatusUpdateAck


def status(**changes):
    return dict(kind="progress", confidence="medium", summary="Checking the argument.",
        scope="partial_artifact", confidence_reason="The main step has been written.") | changes


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "Target.lean"

    def drive(self, calls, host, check=lambda path: "OK — no errors, no warnings."):
        def stream(*args, **kwargs):
            yield from calls
            yield Done(Usage(1, 1), 0)
        config = LeaConfig(model="test", max_turns=1, status_reporting=True,
            extra_tools=["update_lea_status"], mcp_servers={}, narrate_tool_steps=False)
        events = []
        with patch.object(agent, "stream", side_effect=stream) as model, patch.object(agent, "load_system_prompt", return_value="Test"), patch.object(agent._tools, "lean_check", side_effect=check):
            gen = agent.run_events(config, [{"role": "user", "content": "Formalize target"}])
            answer = None
            while True:
                try:
                    event = gen.send(answer)
                except StopIteration:
                    break
                events.append(event)
                answer = host(event) if isinstance(event, LeaStatusUpdateRequested) else None
            self.assertEqual(model.call_count, 1)
        return events

    def test_publication_precedes_next_tool_and_uses_intermediate_bytes(self):
        seen = []
        def host(event):
            seen.append(self.path.read_text())
            return LeaStatusUpdateAck(True, "update", len(seen), event.payload)
        def check(path):
            self.assertEqual(seen, ["first"])
            return "OK — no errors, no warnings."
        events = self.drive([
            ToolCall("write_file", dict(path=str(self.path), content="first")),
            ToolCall("update_lea_status", status()),
            ToolCall("lean_check", dict(path=str(self.path))),
            ToolCall("write_file", dict(path=str(self.path), content="second")),
            ToolCall("update_lea_status", status()),
        ], host, check)
        self.assertEqual(seen, ["first", "second"])
        self.assertEqual(sum(isinstance(e, FileChanged) for e in events), 2)

    def test_blocking_ack_cancels_later_mutations(self):
        events = self.drive([
            ToolCall("update_lea_status", status(scope="source_only")),
            ToolCall("write_file", dict(path=str(self.path), content="must not write")),
        ], lambda event: LeaStatusUpdateAck(True, "update", 1, event.payload, stop_reason="source_obstruction"))
        self.assertFalse(self.path.exists())
        self.assertEqual(next(e for e in events if isinstance(e, Finished )).reason, "source_obstruction")
        self.assertTrue(any("canceled" in e.content for e in events if isinstance(e, ToolResulted)))

    def test_absent_host_never_claims_success(self):
        events = self.drive([ToolCall("update_lea_status", status(scope="source_only"))], lambda event: None)
        self.assertTrue(any("did not acknowledge" in e.content for e in events if isinstance(e, ToolResulted)))

    def test_rejected_material_update_defers_the_preplanned_correction(self):
        events = self.drive([
            ToolCall("update_lea_status", status(scope="source_only", finding_updates=[{
                "key": "gap", "severity": "warning", "category": "source_gap", "title": "Missing justification",
                "detail": "A justification is missing.", "lean_resolution": "planned", "source_resolution": "open",
                "correction": "Add the justification." }])),
            ToolCall("write_file", dict(path=str(self.path), content="must not write")),
        ], lambda event: LeaStatusUpdateAck(False, error="Invalid finding transition"))
        self.assertFalse(self.path.exists())
        self.assertTrue(any("deferred" in e.content for e in events if isinstance(e, ToolResulted)))

    def test_cadence_and_last_acknowledged_findings_survive_compaction(self):
        self.path.write_text("source context")
        requests = []
        previous = {"findings": [{"key": "gap", "detail": "Retain this source issue"}]}
        def stream(model, system, messages, tools, *args, **kwargs):
            requests.append(copy.deepcopy(messages))
            if len(requests) == 1:
                for _ in range(5):
                    yield ToolCall("read_file", {"path": str(self.path)})
            elif len(requests) == 2:
                yield ToolCall("update_lea_status", status(scope="source_only"))
            yield Done(Usage(1, 1), 0)
        def compact(messages, *args, **kwargs):
            return SimpleNamespace(changed=True, messages=[{"role": "user", "content": "Original request"}],
                usage=Usage(), cost=0, before_tokens=100, after_tokens=10, pruned=1, summarized=False)
        config = LeaConfig(model="test", max_turns=3, status_reporting=True, extra_tools=["update_lea_status"],
            prompt_variant="overleaf_continuation", status_context={"previous_assessment": previous}, mcp_servers={})
        with patch.object(agent, "stream", side_effect=stream), patch.object(agent, "load_system_prompt", return_value="Test"), \
             patch.object(agent.condenser, "should_compact", return_value=True), patch.object(agent.condenser, "condense", side_effect=compact):
            gen = agent.run_events(config, [{"role": "user", "content": "Original request"}])
            answer = None
            while True:
                try:
                    event = gen.send(answer)
                except StopIteration:
                    break
                answer = LeaStatusUpdateAck(True, "accepted", 1, previous) if isinstance(event, LeaStatusUpdateRequested) else None
        self.assertEqual(len(requests), 3)
        contexts = [[m for m in messages if m.get("lea_status_context")] for messages in requests]
        self.assertTrue(all(len(items) == 1 for items in contexts))
        self.assertIn("Publish a useful update_lea_status", contexts[1][0]["content"])
        self.assertIn("Retain this source issue", contexts[2][0]["content"])
        self.assertNotIn("previous_assessment", contexts[2][0]["content"])


if __name__ == "__main__":
    unittest.main()
