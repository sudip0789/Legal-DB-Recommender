import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from core import finder  # noqa: E402


class FinderProviderRoutingTest(unittest.TestCase):
    def test_anthropic_models_keep_ephemeral_cache_control(self):
        anthropic_client = Mock()
        anthropic_client.messages.create.return_value = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="anthropic answer")],
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                cache_creation_input_tokens=80,
                cache_read_input_tokens=0,
            ),
        )

        with patch.object(finder, "_ANTHROPIC_CLIENT", anthropic_client):
            answer, messages, usage = finder.get_answer(
                [{"role": "user", "content": "Where should I search?"}],
                use_cache=True,
                model="claude-opus-4-8",
            )

        self.assertEqual(answer, "anthropic answer")
        self.assertEqual(messages, [{"role": "user", "content": "Where should I search?"}])
        call_kwargs = anthropic_client.messages.create.call_args.kwargs
        self.assertEqual(call_kwargs["model"], "claude-opus-4-8")
        self.assertEqual(
            call_kwargs["system"][0]["cache_control"],
            {"type": "ephemeral"},
        )
        self.assertEqual(usage["cache_creation_input_tokens"], 80)
        self.assertEqual(usage["cache_read_input_tokens"], 0)

    def test_gpt_54_routes_to_openai_with_24h_prompt_cache_retention(self):
        openai_client = Mock()
        openai_client.responses.create.return_value = SimpleNamespace(
            output_text="openai answer",
            usage=SimpleNamespace(
                input_tokens=300,
                output_tokens=40,
                input_tokens_details=SimpleNamespace(cached_tokens=256),
            ),
        )

        with patch.object(finder, "_OPENAI_CLIENT", openai_client):
            answer, messages, usage = finder.get_answer(
                [{"role": "user", "content": "Where should I search?"}],
                use_cache=True,
                model="gpt-5.4",
            )

        self.assertEqual(answer, "openai answer")
        self.assertEqual(messages, [{"role": "user", "content": "Where should I search?"}])
        call_kwargs = openai_client.responses.create.call_args.kwargs
        self.assertEqual(call_kwargs["model"], "gpt-5.4")
        self.assertEqual(call_kwargs["prompt_cache_retention"], "24h")
        self.assertEqual(call_kwargs["input"][0]["role"], "system")
        self.assertIn("content", call_kwargs["input"][0])
        self.assertEqual(usage["cached_input_tokens"], 256)
        self.assertEqual(usage["cache_creation_input_tokens"], 0)
        self.assertEqual(usage["cache_read_input_tokens"], 0)

    def test_gpt_55_routes_to_openai_with_24h_prompt_cache_retention(self):
        openai_client = Mock()
        openai_client.responses.create.return_value = SimpleNamespace(
            output_text="openai answer",
            usage=SimpleNamespace(
                input_tokens=300,
                output_tokens=40,
                input_tokens_details={"cached_tokens": 128},
            ),
        )

        with patch.object(finder, "_OPENAI_CLIENT", openai_client):
            finder.get_answer(
                [{"role": "user", "content": "Where should I search?"}],
                use_cache=True,
                model="gpt-5.5",
            )

        call_kwargs = openai_client.responses.create.call_args.kwargs
        self.assertEqual(call_kwargs["model"], "gpt-5.5")
        self.assertEqual(call_kwargs["prompt_cache_retention"], "24h")

    def test_gpt_54_mini_uses_automatic_openai_caching_without_retention_override(self):
        openai_client = Mock()
        openai_client.responses.create.return_value = SimpleNamespace(
            output_text="mini answer",
            usage=SimpleNamespace(
                input_tokens=200,
                output_tokens=30,
                input_tokens_details=SimpleNamespace(cached_tokens=100),
            ),
        )

        with patch.object(finder, "_OPENAI_CLIENT", openai_client):
            finder.get_answer(
                [{"role": "user", "content": "Where should I search?"}],
                use_cache=True,
                model="gpt-5.4-mini",
            )

        call_kwargs = openai_client.responses.create.call_args.kwargs
        self.assertEqual(call_kwargs["model"], "gpt-5.4-mini")
        self.assertNotIn("prompt_cache_retention", call_kwargs)

    def test_unknown_model_raises_clear_error(self):
        with self.assertRaisesRegex(ValueError, "Unknown model"):
            finder.get_answer(
                [{"role": "user", "content": "Where should I search?"}],
                use_cache=True,
                model="not-a-real-model",
            )


if __name__ == "__main__":
    unittest.main()
