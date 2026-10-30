"""Blueprint tools are host-mediated: the loop yields a request, the host answers.

docs/PLAN-blueprint-tools.md §3 "P2 host", items 1-2.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lea import agent, registry, tools
from lea.blueprint_tools import TOOL_NAMES, BlueprintAck, BlueprintRequested
from lea.config import LeaConfig
from lea.events import Finished, ToolResulted
from lea.providers import Done, TextDelta, ToolCall, Usage, _ToolMeta
from lea.runctx import run_context

NODES = [{"key": "main", "kind": "theorem", "statement": "The main result.", "uses": ["helper"]},
         {"key": "helper", "kind": "lemma", "statement": "A supporting fact."}]
PROTECTED = [".lea/blueprint.md", ".lea/blueprint.draft.md"]


def _drive(cfg, call, answer, directory):
    """Run one model turn that issues `call`, answering blueprint requests with `answer`.

    Returns (events, requests, offered tool names, tool results the model saw next turn)."""
    events, requests, offered, seen = [], [], [], []
    turn = 0

    def stream(model, system, messages, tool_schemas, *args, **kwargs):
        nonlocal turn
        turn += 1
        if turn == 1:
            offered.extend(schema["name"] for schema in tool_schemas)
            yield ToolCall(*call)
            yield _ToolMeta("call_bp")
        else:
            seen.extend(part for message in messages if isinstance(message.get("content"), list)
                        for part in message["content"]
                        if isinstance(part, dict) and part.get("type") == "tool_result")
            yield TextDelta("Planned.")
        yield Done(Usage(3, 2), 0.0)

    with patch.object(agent, "stream", side_effect=stream), \
            patch.object(agent, "load_system_prompt", return_value="Test"):
        gen = agent.run_events(cfg, [{"role": "user", "content": "Plan the proof"}], working_dir=directory)
        reply = None
        while True:
            try:
                event = gen.send(reply)
            except StopIteration:
                break
            events.append(event)
            if isinstance(event, BlueprintRequested):
                requests.append(event)
                reply = answer
            else:
                reply = None
    return events, requests, offered, seen


def _config(**changes):
    return LeaConfig(model="test", max_turns=3, mcp_servers={}, narrate_tool_steps=False, **changes)


class BlueprintToolLoopTests(unittest.TestCase):
    def test_upsert_yields_request_and_the_ack_reaches_the_model_as_json(self):
        result = {"applied": [{"key": "main", "kind": "theorem", "lean": "Lea.Demo.main",
                               "file": "Main.lean", "uses": ["helper"], "status": "planned"}],
                  "rejected": [], "ready": ["helper"], "draft": False, "commit_sha": "abc"}
        cfg = _config(blueprint_tools=True, extra_tools=list(TOOL_NAMES))
        with tempfile.TemporaryDirectory() as directory:
            events, requests, offered, seen = _drive(
                cfg, ("blueprint_upsert", {"nodes": NODES}), BlueprintAck(result=result), directory)
        self.assertTrue(set(TOOL_NAMES) <= set(offered))
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.tool, "blueprint_upsert")
        self.assertEqual(request.arguments, {"nodes": NODES})
        # turn:call index:provider call id — stable across a replay of the same turn.
        self.assertEqual(request.invocation_key, "1:0:call_bp")
        expected = json.dumps(result, ensure_ascii=False)
        resulted = [e for e in events if isinstance(e, ToolResulted) and e.name == "blueprint_upsert"]
        self.assertEqual([e.content for e in resulted], [expected])
        self.assertEqual([p["content"] for p in seen if p.get("tool_name") == "blueprint_upsert"], [expected])
        self.assertTrue(any(isinstance(e, Finished) for e in events))

    def test_ack_error_becomes_error_text(self):
        cfg = _config(blueprint_tools=True, extra_tools=list(TOOL_NAMES))
        with tempfile.TemporaryDirectory() as directory:
            events, requests, _, seen = _drive(
                cfg, ("blueprint_remove", {"keys": ["helper"]}),
                BlueprintAck(error="helper is used by main"), directory)
        self.assertEqual([r.tool for r in requests], ["blueprint_remove"])
        self.assertEqual([p["content"] for p in seen if p.get("tool_name") == "blueprint_remove"],
                         ["Error: helper is used by main"])

    def test_missing_ack_is_an_error_not_a_crash(self):
        cfg = _config(blueprint_tools=True, extra_tools=list(TOOL_NAMES))
        with tempfile.TemporaryDirectory() as directory:
            events, requests, _, _ = _drive(cfg, ("blueprint_read", {}), None, directory)
        self.assertEqual(len(requests), 1)
        contents = [e.content for e in events if isinstance(e, ToolResulted) and e.name == "blueprint_read"]
        self.assertEqual(contents, ["Error: Blueprint host did not acknowledge the tool."])

    def test_flag_off_offers_no_blueprint_tools_and_yields_no_request(self):
        with tempfile.TemporaryDirectory() as directory:
            events, requests, offered, _ = _drive(
                _config(), ("blueprint_upsert", {"nodes": NODES}), BlueprintAck(result={}), directory)
        self.assertFalse(set(TOOL_NAMES) & set(offered))
        self.assertEqual(requests, [])
        contents = [e.content for e in events if isinstance(e, ToolResulted) and e.name == "blueprint_upsert"]
        self.assertEqual(len(contents), 1)
        self.assertTrue(contents[0].startswith("Error:"))

    def test_tools_named_without_the_flag_reach_no_host(self):
        cfg = _config(extra_tools=list(TOOL_NAMES))
        with tempfile.TemporaryDirectory() as directory:
            events, requests, offered, _ = _drive(cfg, ("blueprint_read", {}), BlueprintAck(result={}), directory)
        self.assertEqual(requests, [])
        contents = [e.content for e in events if isinstance(e, ToolResulted) and e.name == "blueprint_read"]
        self.assertEqual(contents, ["Error: this tool requires a blueprint host."])

    def test_schemas_are_opt_in_and_constrained(self):
        default_names = {schema["name"] for schema in registry.build_toolset(None)[0]}
        self.assertFalse(set(TOOL_NAMES) & default_names)
        schemas = {schema["name"]: schema for schema in registry.build_toolset(list(TOOL_NAMES))[0]}
        upsert = schemas["blueprint_upsert"]["input_schema"]["properties"]["nodes"]
        self.assertEqual((upsert["minItems"], upsert["maxItems"]), (1, 100))
        self.assertEqual(upsert["items"]["required"], ["key"])
        self.assertEqual(upsert["items"]["properties"]["kind"]["enum"], ["theorem", "lemma", "definition"])
        self.assertNotIn("status", upsert["items"]["properties"])
        remove = schemas["blueprint_remove"]["input_schema"]
        self.assertEqual(remove["required"], ["keys"])
        self.assertEqual(remove["properties"]["keys"]["minItems"], 1)
        self.assertIn("Call this instead of reading .lea/blueprint.md.", schemas["blueprint_read"]["description"])
        self.assertIn("Never write status.", schemas["blueprint_upsert"]["description"])


class ProtectedPathTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        (self.root / ".lea").mkdir()
        self.cfg = _config(blueprint_tools=True, protected_paths=list(PROTECTED))

    def tearDown(self):
        self._dir.cleanup()

    def test_write_file_on_a_protected_path_refuses_without_writing(self):
        with run_context(working_dir=str(self.root), config=self.cfg):
            relative = tools.write_file(".lea/blueprint.md", "# Blueprint\n")
            absolute = tools.write_file(str(self.root / ".lea" / "blueprint.draft.md"), "# Draft\n")
        self.assertEqual(relative, "Error: .lea/blueprint.md is maintained by the system. "
                                   "Use blueprint_upsert / blueprint_remove instead.")
        self.assertEqual(absolute, "Error: .lea/blueprint.draft.md is maintained by the system. "
                                   "Use blueprint_upsert / blueprint_remove instead.")
        self.assertFalse((self.root / ".lea" / "blueprint.md").exists())
        self.assertFalse((self.root / ".lea" / "blueprint.draft.md").exists())

    def test_edit_file_on_a_protected_path_refuses_without_writing(self):
        live = self.root / ".lea" / "blueprint.md"
        live.write_text("## main\n- kind: theorem\n")
        with run_context(working_dir=str(self.root), config=self.cfg):
            result = tools.edit_file(".lea/./blueprint.md", "theorem", "lemma")
        self.assertTrue(result.startswith("Error: .lea/blueprint.md is maintained by the system."))
        self.assertEqual(live.read_text(), "## main\n- kind: theorem\n")

    def test_unprotected_paths_write_as_before(self):
        with run_context(working_dir=str(self.root), config=self.cfg):
            notes = tools.write_file(".lea/notes.md", "notes\n")
            proof = tools.write_file("Main.lean", "theorem main : True := by trivial\n")
            edited = tools.edit_file("Main.lean", "trivial", "exact trivial")
        self.assertFalse(str(notes).startswith("Error"))
        self.assertFalse(str(proof).startswith("Error"))
        self.assertEqual(str(edited), "OK")
        self.assertEqual((self.root / ".lea" / "notes.md").read_text(), "notes\n")
        self.assertEqual((self.root / "Main.lean").read_text(), "theorem main : True := by exact trivial\n")

    def test_no_protection_without_configured_paths_or_run_context(self):
        with run_context(working_dir=str(self.root), config=_config()):
            self.assertFalse(str(tools.write_file(".lea/blueprint.md", "a\n")).startswith("Error"))
        self.assertEqual((self.root / ".lea" / "blueprint.md").read_text(), "a\n")
        outside = self.root / ".lea" / "blueprint.draft.md"
        self.assertFalse(str(tools.write_file(str(outside), "b\n")).startswith("Error"))
        self.assertEqual(outside.read_text(), "b\n")


if __name__ == "__main__":
    unittest.main()
