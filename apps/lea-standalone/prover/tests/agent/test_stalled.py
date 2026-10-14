"""Mocked final gates: bounded requests, evidence resets, and durable stops.

Run with python -m tests.agent.test_stalled.
"""
import copy
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lea import agent
from lea.config import LeaConfig
from lea.events import DependencyReadinessRequested, DependencyReadinessAck, Finished
from lea.providers import TextDelta, ToolCall, _ToolMeta, Done, Usage
from lea.stall import StallDetector


class StalledTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "Target.lean"
        self.cfg = LeaConfig(model="mock", tools=[], mcp_servers={}, max_turns=20,
                            prompt_variant="default", narrate_tool_steps=False)
        self.calls = 0
        self.states = []

    def run_mock(self, actions, *, resume=None, blocked=False):
        def stream(*args, **kwargs):
            self.calls += 1
            action = actions[min(self.calls - 1, len(actions) - 1)]
            for name, arguments in action:
                yield ToolCall(name, arguments)
                yield _ToolMeta(f"{self.calls}-{name}")
            if not action:
                yield TextDelta("Acknowledged.")
            yield Done(Usage(100, 10), .01)

        def write(args):
            self.path.write_text(args["content"])
            return "Written"

        handlers = {"write_file": write, "lean_check": lambda _: "error: unchanged blocker",
                    "search_mathlib": lambda _: "New lemma: useful_lemma", "read_file": lambda _: "unchanged"}
        token = agent._checkpoint_hook.set(lambda kind, data: self.states.append(copy.deepcopy(data)) if kind == "checkpoint" else None)
        self.addCleanup(agent._checkpoint_hook.reset, token)
        with patch.object(agent, "stream", stream), patch.object(agent, "load_system_prompt", return_value="SYS"), \
             patch.object(agent, "build_toolset", return_value=([], handlers)):
            gen = agent._run_events_inner(self.cfg, [{"role": "user", "content": "Prove it"}],
                                          working_dir=self.temp.name, resume_state=resume)
            events, ack = [], None
            while True:
                try:
                    ev = gen.send(ack)
                except StopIteration:
                    break
                events.append(ev)
                ack = DependencyReadinessAck(["canonical-dep"] if blocked else [], {"dep": "missing"}) \
                    if isinstance(ev, DependencyReadinessRequested) else None
        return next(ev for ev in reversed(events) if isinstance(ev, Finished))

    def test_no_artifact_stops_before_fourth_request(self):
        final = self.run_mock([[]])
        self.assertEqual((final.reason, self.calls), ("stalled", 3))
        self.assertEqual(self.states[-1]["stage"], "turn_boundary")
        self.assertEqual(self.states[-1]["stall_detector"]["failures"], 3)
        self.assertEqual(final.cost, .03)

    def test_failed_proof_preserved_and_no_paid_summary(self):
        content = "theorem target : True := by sorry\n"
        final = self.run_mock([[('write_file', {"path": str(self.path), "content": content})], []])
        self.assertEqual((final.reason, self.calls), ("stalled", 4))
        self.assertEqual(self.path.read_text(), content)
        self.assertIn("unchanged blocker", final.text)
        self.assertNotIn("claimed", final.text)

    def test_crash_keeps_failure_streak(self):
        self.run_mock([[]])
        saved = next(s for s in self.states if s["stall_detector"]["failures"] == 2)
        self.calls = 0
        self.assertEqual(self.run_mock([[]], resume=saved).reason, "stalled")
        self.assertEqual(self.calls, 1)

    def test_dependency_wait_during_failure_preserves_artifact(self):
        self.cfg = replace(self.cfg, project_library=True)
        content = "import Shared.Missing\ntheorem target : True := by sorry\n"
        final = self.run_mock([[('write_file', {"path": str(self.path), "content": content})], []], blocked=True)
        self.assertEqual((final.reason, self.calls), ("dependency_wait", 2))
        self.assertEqual(self.path.read_text(), content)
        saved = self.states[-1]
        saved["reporting_stop"] = None  # an explicit continuation
        self.calls = 0
        self.assertEqual(self.run_mock([[]], resume=saved, blocked=True).reason, "dependency_wait")
        self.assertEqual(self.calls, 0)

    def test_new_research_buys_another_bounded_attempt(self):
        final = self.run_mock([[], [('search_mathlib', {"query": "new"})], [], [], []])
        self.assertEqual((final.reason, self.calls), ("stalled", 5))

    def test_repeated_reads_and_status_do_not_reset(self):
        proof = agent.ProofVerificationState()
        guard = StallDetector()
        guard.note_tool("read_file", {"path": "x"}, "same")
        self.assertFalse(guard.failure(proof, {}, {}))
        guard.note_tool("read_file", {"path": "x"}, "same")
        guard.note_tool("update_lea_status", {}, "Acknowledged")
        guard.note_tool("lean_check", {}, "same")
        self.assertFalse(guard.failure(proof, {}, {}))
        self.assertTrue(guard.failure(proof, {}, {}))

    def test_changed_bytes_or_verification_reset(self):
        proof = agent.ProofVerificationState()
        proof.latest_proof_path = str(self.path)
        self.path.write_text("partial")
        guard = StallDetector()
        guard.failure(proof, {}, {})
        guard.failure(proof, {}, {})
        self.path.write_text("repaired")
        self.assertFalse(guard.failure(proof, {}, {}))
        self.assertEqual(guard.failures, 1)
        proof.latest_check_output = "new diagnostic"
        self.assertFalse(guard.failure(proof, {}, {}))
        self.assertEqual(guard.failures, 1)

    def test_interactive_chat_returns_control(self):
        self.cfg = replace(self.cfg, prompt_variant="interactive")
        final = self.run_mock([[]])
        self.assertEqual((final.reason, self.calls), ("assistant", 1))


if __name__ == "__main__":
    unittest.main()
