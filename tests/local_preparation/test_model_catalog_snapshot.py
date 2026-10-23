import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("snapshot", Path(__file__).resolve().parents[2] / "scripts/refresh-model-snapshot.py")
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


class ModelSnapshotTests(unittest.TestCase):
    def test_snapshot_validates_completeness_preserves_default_and_is_repeatable(self):
        current = {"default_model": "custom-default", "families": [], "models": [{"value": "custom-default", "family": "custom"}]}
        metadata = {f"ignored-{i}": {} for i in range(100)}
        for provider in ("openai", "anthropic", "gemini"):
            metadata[f"{provider}/{provider}-future"] = {"mode": "responses" if provider == "openai" else "chat", "litellm_provider": provider, "supports_function_calling": True}
        output = snapshot.generate(current, metadata)
        self.assertIn("custom-default", [m["value"] for m in output["models"]])
        self.assertEqual(snapshot.generate(output, metadata), output)
        with self.assertRaises(ValueError):
            snapshot.generate(output, {})
        del metadata["anthropic/anthropic-future"]
        with self.assertRaises(ValueError):
            snapshot.generate(output, metadata)

    def test_snapshot_never_includes_non_tool_or_non_text_models(self):
        current = {"default_model": "gpt-test", "families": [], "models": [{"value": "gpt-test", "family": "openai"}]}
        metadata = {f"ignored-{i}": {} for i in range(100)}
        for provider in ("openai", "anthropic", "gemini"):
            metadata[provider + "/" + provider + "-test"] = {"mode": "chat", "litellm_provider": provider, "supports_function_calling": True}
        metadata["image"] = {"mode": "image_generation", "litellm_provider": "openai", "supports_function_calling": True}
        metadata["no-tools"] = {"mode": "chat", "litellm_provider": "openai", "supports_function_calling": False}
        values = [m["value"] for m in snapshot.generate(current, metadata)["models"]]
        self.assertNotIn("image", values)
        self.assertNotIn("no-tools", values)
