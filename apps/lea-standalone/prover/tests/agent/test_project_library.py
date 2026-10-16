"""Project work pauses only after acknowledged, durable tool results."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from lea import agent, registry
from lea.config import LeaConfig
from lea.events import Finished, ToolResulted, DependencyReadinessRequested, DependencyReadinessAck
from lea.project_library_tools import ProjectLibraryRequested, ProjectLibraryAck
from lea.providers import ToolCall, Done, Usage, TextDelta
from lea.check_runtime import CheckText
from lea.check_evidence import digest, environment_hash


class ProjectLibraryTests(unittest.TestCase):
    def test_rejected_self_reservation_can_continue_in_ordinary_target_file(self):
        events = []
        turn = 0
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "descent_energy.lean"
            source = "def descent_energy (n : Nat) : Nat := n\n"

            def stream(*args, **kwargs):
                nonlocal turn
                turn += 1
                if turn == 1:
                    yield ToolCall("request_project_dependency", {"mode": "inline",
                        "specification": {"name": "descent_energy"}})
                    yield ToolCall("write_file", {"path": str(path), "content": source})
                elif turn == 2:
                    yield ToolCall("lean_check", {"path": str(path)})
                else:
                    yield TextDelta("The requested definition is complete.")
                yield Done(Usage(3, 2), 0.1)

            def checked(args):
                environment = {"configuration": {}, "files": {}}
                execution = {"evidence_version": 1, "source_identity": str(path.resolve()),
                    "content_sha256": digest(path.read_bytes()), "environment": environment,
                    "environment_sha256": environment_hash(environment)}
                return CheckText("OK — no errors, no warnings.", execution, path.read_text())

            cfg = LeaConfig(model="test", max_turns=4, mcp_servers={}, project_library=True,
                prompt_variant="overleaf_faithful",
                extra_tools=["request_project_dependency", "search_project_library"], narrate_tool_steps=False)
            with patch.object(agent, "stream", side_effect=stream), \
                    patch.object(agent, "load_system_prompt", return_value="Test"), \
                    patch.dict(registry.REGISTRY, {"lean_check": replace(registry.REGISTRY["lean_check"],
                        handler=checked)}), \
                    patch.object(agent, "_classify_final_result", return_value=("defined", None, Usage(), 0.0)):
                gen = agent.run_events(cfg, [{"role": "user", "content": "Define descent_energy"}],
                    working_dir=directory)
                answer = None
                while True:
                    try:
                        event = gen.send(answer)
                    except StopIteration:
                        break
                    events.append(event)
                    if isinstance(event, ProjectLibraryRequested):
                        answer = ProjectLibraryAck(error="The requested dependency is the current formalization target, not a supporting result.")
                    elif isinstance(event, DependencyReadinessRequested):
                        answer = DependencyReadinessAck([], {})
                    else:
                        answer = None
            self.assertEqual(path.read_text(), source)
            self.assertEqual(cfg.project_context.get("reserved_paths", []), [])
        finished = next(e for e in events if isinstance(e, Finished))
        self.assertEqual(finished.reason, "completed")
        self.assertEqual(finished.result_kind, "defined")
        self.assertTrue(any("current formalization target" in e.content for e in events if isinstance(e, ToolResulted)))

    def test_deferred_request_is_journaled_before_clean_checkpoint(self):
        records, events = [], []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Target.lean"
            def stream(*args, **kwargs):
                yield ToolCall("request_project_dependency", {"mode": "deferred", "specification": {}})
                yield ToolCall("write_file", {"path": str(path), "content": "must not run"})
                yield Done(Usage(3, 2), 0.1)
            cfg = LeaConfig(model="test", max_turns=4, mcp_servers={}, project_library=True,
                extra_tools=["request_project_dependency", "search_project_library"], narrate_tool_steps=False)
            with patch.object(agent, "stream", side_effect=stream), patch.object(agent, "load_system_prompt", return_value="Test"):
                gen = agent.run_events(cfg, [{"role": "user", "content": "Prove A"}],
                    working_dir=directory, checkpoint_callback=lambda kind, value: records.append((kind, value)))
                answer = None
                while True:
                    try:
                        event = gen.send(answer)
                    except StopIteration:
                        break
                    events.append(event)
                    answer = ProjectLibraryAck({"outcome": "deferred", "dependency_id": "L", "stop_reason": "dependency_wait"}) if isinstance(event, ProjectLibraryRequested) else None
            self.assertFalse(path.exists())
        finished = next(e for e in events if isinstance(e, Finished))
        self.assertEqual(finished.reason, "dependency_wait")
        checkpoints = [value for kind, value in records if kind == "checkpoint"]
        self.assertEqual(checkpoints[-1]["stage"], "turn_boundary")
        self.assertEqual(checkpoints[-1]["turn"], 1)
        self.assertTrue(any("dependency_wait" in str(message) for message in checkpoints[-1]["messages"]))
        self.assertTrue(any("canceled" in e.content for e in events if isinstance(e, ToolResulted)))


if __name__ == "__main__":
    unittest.main()
