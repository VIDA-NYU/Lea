"""Project work pauses only after acknowledged, durable tool results."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from lea import agent
from lea.config import LeaConfig
from lea.events import Finished, ToolResulted
from lea.project_library_tools import ProjectLibraryRequested, ProjectLibraryAck
from lea.providers import ToolCall, Done, Usage


class ProjectLibraryTests(unittest.TestCase):
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
