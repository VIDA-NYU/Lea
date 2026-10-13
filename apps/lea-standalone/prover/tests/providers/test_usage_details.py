"""No network model calls: full-response pricing and provider usage conventions."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from lea import providers
from lea.providers import Done, Usage


class UsageTests(unittest.TestCase):
    def test_streaming_and_blocking_keep_same_terminal_usage(self):
        raw = {"prompt_tokens": 1000, "completion_tokens": 100,
               "prompt_tokens_details": {"cached_tokens": 600, "cache_creation_tokens": 100},
               "completion_tokens_details": {"reasoning_tokens": 40}}
        response = NS(model="response-model", service_tier="flex", usage=raw,
                      choices=[NS(message=NS(content="ok", tool_calls=[]))])
        chunks = [NS(model="response-model", service_tier="flex", usage=None, choices=[]),
                  NS(usage=raw, choices=[])]
        received = []

        def cost(**kwargs):
            received.append(providers._plain(kwargs["completion_response"]))
            return .012

        with patch.object(providers.litellm, "completion_cost", cost):
            with patch.object(providers.litellm, "completion", return_value=response):
                blocking = list(providers._stream_blocking("request-model", {}))[-1]
            with patch.object(providers.litellm, "completion", return_value=chunks):
                streamed = list(providers._stream_streaming("request-model", {}))[-1]
        self.assertEqual(blocking.usage_details, streamed.usage_details)
        self.assertEqual(blocking.raw_usage, raw)
        self.assertEqual(streamed.raw_usage, raw)
        self.assertEqual(blocking.cost, streamed.cost)
        self.assertEqual((streamed.usage.input_tokens, streamed.usage.output_tokens), (1000, 100))
        self.assertEqual(received[-1]["service_tier"], "flex")
        self.assertEqual(received[-1]["model"], "response-model")

    def test_native_anthropic_exclusive_input_vs_litellm_inclusive_input(self):
        native = {"input_tokens": 300, "output_tokens": 80,
                  "cache_read_input_tokens": 500, "cache_creation_input_tokens": 200}
        inclusive = {"prompt_tokens": 1000, "completion_tokens": 80,
                     "cache_read_input_tokens": 500, "cache_creation_input_tokens": 200}
        a, b = providers._normalized_usage(native), providers._normalized_usage(inclusive)
        self.assertEqual((a.input_tokens, a.output_tokens), (1000, 80))
        self.assertEqual(a.usage_details, b.usage_details)
        self.assertEqual(a.usage_details["cache_write_tokens"], 200)

    def test_litellm_cache_price_and_reasoning_not_double_charged(self):
        response = {"model": "gpt-4o", "usage": {"prompt_tokens": 1000, "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 500},
            "completion_tokens_details": {"reasoning_tokens": 20}}}
        done = providers._terminal_done("openai/gpt-4o", response, [])
        self.assertAlmostEqual(done.cost, .002875)
        self.assertEqual(done.usage.output_tokens, 100)
        self.assertEqual(done.usage_details["reasoning_tokens"], 20)
        self.assertEqual(done.usage_details["cost_basis"], "cache_aware_estimate")

    def test_cache_writes_use_full_litellm_usage(self):
        model = "anthropic/lea-usage-test"
        providers.litellm.register_model({model: {
            "input_cost_per_token": .000003, "output_cost_per_token": .000015,
            "cache_read_input_token_cost": .0000003, "cache_creation_input_token_cost": .00000375,
            "litellm_provider": "anthropic", "mode": "chat"}})
        raw = {"input_tokens": 300, "output_tokens": 80,
               "cache_read_input_tokens": 500, "cache_creation_input_tokens": 200}
        done = providers._terminal_done(model, {"model": model, "usage": raw}, [])
        self.assertEqual(done.usage.input_tokens, 1000)
        self.assertAlmostEqual(done.cost, 300*.000003 + 500*.0000003 + 200*.00000375 + 80*.000015)

    def test_provider_charge_precedes_estimation(self):
        raw = {"prompt_tokens": 100, "completion_tokens": 5}
        with patch.object(providers.litellm, "completion_cost", side_effect=AssertionError("must not estimate")):
            done = providers._terminal_done("unmapped", {"usage": raw, "_hidden_params": {
                "additional_headers": {"llm_provider-x-litellm-response-cost": "0.123"}}}, [])
        self.assertEqual(done.cost, .123)
        self.assertEqual(done.usage_details["cost_basis"], "provider_reported")

    def test_native_and_normalized_cache_ttls_keep_distinct_write_rates(self):
        model = "anthropic/lea-ttl-test"
        providers.litellm.register_model({model: {
            "input_cost_per_token": .000003, "output_cost_per_token": .000015,
            "cache_read_input_token_cost": .0000003, "cache_creation_input_token_cost": .00000375,
            "cache_creation_input_token_cost_above_1hr": .000006,
            "litellm_provider": "anthropic", "mode": "chat"}})
        ttl = {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 100}
        native = {"input_tokens": 300, "output_tokens": 80, "cache_read_input_tokens": 500,
                  "cache_creation_input_tokens": 200, "cache_creation": ttl,
                  "output_tokens_details": {"reasoning_tokens": 20}}
        normalized = {"prompt_tokens": 1000, "completion_tokens": 80,
            "prompt_tokens_details": {"cached_tokens": 500, "cache_creation_tokens": 200,
                                      "cache_creation_token_details": ttl},
            "completion_tokens_details": {"reasoning_tokens": 20}}
        for raw in (native, normalized):
            done = providers._terminal_done(model, {"model": model, "usage": raw}, [])
            self.assertEqual(done.usage.input_tokens, 1000)
            self.assertEqual(done.usage_details["cache_write_tokens"], 200)
            self.assertEqual(done.raw_usage, raw)
            self.assertAlmostEqual(done.cost, 300*.000003 + 500*.0000003 + 100*.00000375 + 100*.000006 + 80*.000015)

    def test_response_model_and_flex_tier_select_actual_rates(self):
        model = "openai/lea-flex-test"
        providers.litellm.register_model({model: {
            "input_cost_per_token": .000004, "output_cost_per_token": .000020,
            "cache_read_input_token_cost": .0000004,
            "input_cost_per_token_flex": .000002, "output_cost_per_token_flex": .000010,
            "cache_read_input_token_cost_flex": .0000002,
            "litellm_provider": "openai", "mode": "chat"}})
        response = {"model": model, "service_tier": "flex", "usage": {
            "prompt_tokens": 1000, "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 800}}}
        done = providers._terminal_done("openai/gpt-4o", response, [])
        self.assertAlmostEqual(done.cost, 200*.000002 + 800*.0000002 + 100*.000010)
        self.assertEqual(done.usage_details["response_model"], model)

    def test_unknown_pricing_and_missing_usage_are_explicit(self):
        with patch.object(providers.litellm, "completion_cost", side_effect=ValueError("unmapped")):
            done = providers._terminal_done("unmapped", {"usage": {"prompt_tokens": 100, "completion_tokens": 5}}, [])
        self.assertEqual(done.usage_details["cost_basis"], "unavailable")
        self.assertEqual(done.usage_details["unpriced_calls"], 1)
        self.assertIsNone(done.usage_details["cached_input_tokens"])
        self.assertEqual(providers._terminal_done("any", {}, []).usage_details["cost_basis"], "unavailable")

    def test_accumulation_preserves_mixed_legacy_counts(self):
        total = Usage()
        total.add(Usage(20, 5))
        rich = Usage(100, 20, {"cached_input_tokens": 50, "cache_write_tokens": 10,
            "reasoning_tokens": 4, "cost_basis": "cache_aware_estimate"})
        total.add(rich)
        self.assertEqual((total.input_tokens, total.output_tokens), (120, 25))
        self.assertEqual(total.usage_details["cached_input_tokens"], 50)
        self.assertEqual(total.usage_details["cost_basis"], "mixed")
        self.assertEqual(total.usage_details["basis_counts"], {"legacy_estimate": 1, "cache_aware_estimate": 1})
        self.assertEqual(Done(Usage(1, 2), .01, []).usage.input_tokens, 1)


if __name__ == "__main__":
    unittest.main()
