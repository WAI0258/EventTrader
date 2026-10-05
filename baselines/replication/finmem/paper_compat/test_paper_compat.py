from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve()
FINMEM_ROOT = SCRIPT_PATH.parents[3] / "FinMem-LLM-StockTrading"
for path in (SCRIPT_PATH.parent, FINMEM_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import m2_1_transport
from m2_1_transport import MiniMaxOpenAITransport, USAGE_TRACKER


class _Response:
    status_code = 200
    text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "choices": [{"message": {"content": '{"investment_decision":"hold"}'}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }


class PaperCompatTransportTest(unittest.TestCase):
    def test_transport_keeps_original_guardrail_envelope_and_usage(self) -> None:
        previous_key = os.environ.get("MINIMAX_API_KEY")
        previous_gold_key = os.environ.get("MINIMAX_API_KEY_1d")
        os.environ["MINIMAX_API_KEY"] = "test-key"
        os.environ["MINIMAX_API_KEY_1d"] = "gold-test-key"
        observed: dict = {}
        original_post = m2_1_transport.httpx.post

        def fake_post(*args, **kwargs):
            observed["args"] = args
            observed["kwargs"] = kwargs
            return _Response()

        m2_1_transport.httpx.post = fake_post
        before = USAGE_TRACKER.snapshot()
        try:
            response = MiniMaxOpenAITransport(
                end_point="https://example.invalid/v1/chat/completions",
                model="MiniMax-M2.1",
                other_parameters={
                    "max_tokens": 1200,
                    "temperature": 0.0,
                    "api_key_env": "MINIMAX_API_KEY_1d",
                },
            ).guardrail_endpoint()("original guardrails prompt")
        finally:
            m2_1_transport.httpx.post = original_post
            if previous_key is None:
                del os.environ["MINIMAX_API_KEY"]
            else:
                os.environ["MINIMAX_API_KEY"] = previous_key
            if previous_gold_key is None:
                del os.environ["MINIMAX_API_KEY_1d"]
            else:
                os.environ["MINIMAX_API_KEY_1d"] = previous_gold_key

        self.assertEqual(response, '{"investment_decision":"hold"}')
        payload = observed["kwargs"]["json"]
        self.assertEqual(payload["messages"][1]["content"], "original guardrails prompt")
        self.assertNotIn("api_key_env", payload)
        self.assertEqual(observed["kwargs"]["headers"]["Authorization"], "Bearer gold-test-key")
        after = USAGE_TRACKER.snapshot()
        self.assertEqual(after["llm_calls"] - before["llm_calls"], 1)
        self.assertEqual(after["tokens_in"] - before["tokens_in"], 11)
        self.assertEqual(after["tokens_out"] - before["tokens_out"], 7)


if __name__ == "__main__":
    unittest.main()
