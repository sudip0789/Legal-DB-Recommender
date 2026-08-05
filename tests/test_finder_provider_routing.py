import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from core import finder  # noqa: E402

# Routing tests isolate the main-model call from the always-on verifier by
# stubbing _verify to "ok"; the verifier loop has its own tests below.
_OK = {"ok": True, "violations": []}


def _anthropic_response(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            cache_creation_input_tokens=80,
            cache_read_input_tokens=0,
        ),
    )


def _openai_response(text: str, status: str = "completed", incomplete=None):
    return SimpleNamespace(
        output_text=text,
        status=status,
        incomplete_details=incomplete,
        usage=SimpleNamespace(
            input_tokens=300,
            output_tokens=40,
            input_tokens_details=SimpleNamespace(cached_tokens=256),
        ),
    )


class FinderProviderRoutingTest(unittest.TestCase):
    def test_anthropic_models_keep_ephemeral_cache_control(self):
        anthropic_client = Mock()
        anthropic_client.messages.create.return_value = _anthropic_response(
            "anthropic answer"
        )

        with patch.object(finder, "_ANTHROPIC_CLIENT", anthropic_client), patch.object(
            finder, "_verify", return_value=_OK
        ):
            answer, messages, usage, _initial = finder.get_answer(
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

        with patch.object(finder, "_OPENAI_CLIENT", openai_client), patch.object(
            finder, "_verify", return_value=_OK
        ):
            answer, messages, usage, _initial = finder.get_answer(
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

        with patch.object(finder, "_OPENAI_CLIENT", openai_client), patch.object(
            finder, "_verify", return_value=_OK
        ):
            finder.get_answer(
                [{"role": "user", "content": "Where should I search?"}],
                use_cache=True,
                model="gpt-5.5",
            )

        call_kwargs = openai_client.responses.create.call_args.kwargs
        self.assertEqual(call_kwargs["model"], "gpt-5.5")
        self.assertEqual(call_kwargs["prompt_cache_retention"], "24h")

    def test_gpt_56_sol_is_the_default_and_uses_24h_prompt_cache_retention(self):
        openai_client = Mock()
        openai_client.responses.create.return_value = SimpleNamespace(
            output_text="openai answer",
            usage=SimpleNamespace(
                input_tokens=300,
                output_tokens=40,
                input_tokens_details={"cached_tokens": 128},
            ),
        )

        with patch.object(finder, "_OPENAI_CLIENT", openai_client), patch.object(
            finder, "_verify", return_value=_OK
        ):
            finder.get_answer(
                [{"role": "user", "content": "Where should I search?"}],
                use_cache=True,
            )

        self.assertEqual(finder.DEFAULT_MODEL, "gpt-5.6-sol")
        call_kwargs = openai_client.responses.create.call_args.kwargs
        self.assertEqual(call_kwargs["model"], "gpt-5.6-sol")
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

        with patch.object(finder, "_OPENAI_CLIENT", openai_client), patch.object(
            finder, "_verify", return_value=_OK
        ):
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


class VerifierLoopTest(unittest.TestCase):
    def test_regenerates_once_when_first_draft_violates(self):
        anthropic_client = Mock()
        anthropic_client.messages.create.side_effect = [
            _anthropic_response("bad draft"),
            _anthropic_response("good draft"),
        ]
        verdicts = [{"ok": False, "violations": ["interpreted the docket"]}, _OK]

        with patch.object(finder, "_ANTHROPIC_CLIENT", anthropic_client), patch.object(
            finder, "_verify", side_effect=verdicts
        ):
            answer, _messages, _usage, initial = finder.get_answer(
                [{"role": "user", "content": "q"}],
                use_cache=True,
                model="claude-opus-4-8",
            )

        self.assertEqual(answer, "good draft")
        self.assertEqual(initial, "bad draft")  # initial draft preserved for logging
        self.assertEqual(anthropic_client.messages.create.call_count, 2)
        # The regeneration appends a corrective user turn (system prefix untouched).
        regen_messages = anthropic_client.messages.create.call_args_list[1].kwargs["messages"]
        self.assertEqual(regen_messages[-1]["role"], "user")
        self.assertIn("SYSTEM CORRECTION", regen_messages[-1]["content"])

    def test_exhausts_regenerations_then_returns_safe_fallback(self):
        anthropic_client = Mock()
        anthropic_client.messages.create.return_value = _anthropic_response("still bad")

        with patch.object(finder, "_ANTHROPIC_CLIENT", anthropic_client), patch.object(
            finder, "_verify", return_value={"ok": False, "violations": ["nope"]}
        ):
            answer, _messages, _usage, initial = finder.get_answer(
                [{"role": "user", "content": "q"}],
                use_cache=True,
                model="claude-opus-4-8",
            )

        self.assertEqual(answer, finder._SAFE_FALLBACK)
        self.assertEqual(initial, "still bad")  # original draft kept for the log
        # 1 initial draft + MAX_REGENERATIONS regenerations, all verified.
        self.assertEqual(
            anthropic_client.messages.create.call_count,
            1 + finder.MAX_REGENERATIONS,
        )

    def test_parse_verdict_is_robust(self):
        self.assertTrue(finder._parse_verdict('{"ok": true}')["ok"])
        fenced = finder._parse_verdict('```json\n{"ok": false, "violations": ["a"]}\n```')
        self.assertFalse(fenced["ok"])
        self.assertEqual(fenced["violations"], ["a"])
        string_false = finder._parse_verdict('{"ok": "false", "violations": "a"}')
        self.assertFalse(string_false["ok"])
        self.assertEqual(string_false["violations"], ["a"])
        numeric_false = finder._parse_verdict('{"ok": 0, "violations": ["a"]}')
        self.assertFalse(numeric_false["ok"])
        malformed_violations = finder._parse_verdict(
            '{"ok": false, "violations": {"reason": "a"}}'
        )
        self.assertFalse(malformed_violations["ok"])
        self.assertEqual(malformed_violations["violations"], [])
        self.assertTrue(finder._parse_verdict("not json at all")["ok"])  # fail open

    def test_verify_fails_open_on_client_error(self):
        openai_client = Mock()
        openai_client.responses.create.side_effect = RuntimeError("boom")
        with patch.object(finder, "_OPENAI_CLIENT", openai_client):
            self.assertEqual(
                finder._verify([{"role": "user", "content": "q"}], "draft"),
                {"ok": True, "violations": []},
            )

    def test_blocks_external_url_in_assistant_draft(self):
        anthropic_client = Mock()
        anthropic_client.messages.create.return_value = _anthropic_response(
            "Try [Example Document](https://example.com/document.pdf)."
        )

        with patch.object(finder, "_ANTHROPIC_CLIENT", anthropic_client), patch.object(
            finder, "_verify", return_value=_OK
        ):
            answer, _messages, _usage, initial = finder.get_answer(
                [{"role": "user", "content": "Can you look at this URL?"}],
                use_cache=True,
                model="claude-opus-4-8",
            )

        self.assertEqual(answer, finder._SAFE_FALLBACK)
        self.assertIn("example.com/document.pdf", initial)

    def test_allows_user_pasted_external_url_when_draft_uses_catalog_link(self):
        anthropic_client = Mock()
        anthropic_client.messages.create.return_value = _anthropic_response(
            "Use [Bloomberg Law](http://www.bloomberglaw.com/)."
        )

        with patch.object(finder, "_ANTHROPIC_CLIENT", anthropic_client), patch.object(
            finder, "_verify", return_value=_OK
        ):
            answer, _messages, _usage, _initial = finder.get_answer(
                [
                    {
                        "role": "user",
                        "content": "I pasted https://example.com/case-materials",
                    }
                ],
                use_cache=True,
                model="claude-opus-4-8",
            )

        self.assertEqual(answer, "Use [Bloomberg Law](http://www.bloomberglaw.com/).")

    def test_allows_the_librarys_own_index_pages(self):
        anthropic_client = Mock()
        anthropic_client.messages.create.return_value = _anthropic_response(
            "The full set is on Stanford's [Legal Databases page]"
            "(https://law.stanford.edu/robert-crown-law-library/legal-databases/), "
            "and the [Research Guides]"
            "(https://law.stanford.edu/robert-crown-law-library/research-guides/) index "
            "covers the guides."
        )

        with patch.object(finder, "_ANTHROPIC_CLIENT", anthropic_client), patch.object(
            finder, "_verify", return_value=_OK
        ):
            answer, _messages, _usage, _initial = finder.get_answer(
                [{"role": "user", "content": "Just share the website link."}],
                use_cache=True,
                model="claude-opus-4-8",
            )

        # Regression: these were flagged as outside URLs, so the tool answered
        # "give me the page" with the safe fallback instead of the page.
        self.assertNotEqual(answer, finder._SAFE_FALLBACK)
        self.assertIn("legal-databases/", answer)

    def test_allows_section_anchor_on_the_legal_databases_page(self):
        self.assertTrue(
            finder._is_allowed_link(
                "https://law.stanford.edu/robert-crown-law-library/"
                "legal-databases/#greenwire"
            )
        )
        self.assertFalse(finder._is_allowed_link("https://law.stanford.edu/other-page/"))


class OutputBudgetTest(unittest.TestCase):
    def test_openai_request_adds_reasoning_headroom_to_the_visible_budget(self):
        openai_client = Mock()
        openai_client.responses.create.return_value = _openai_response("answer")

        with patch.object(finder, "_OPENAI_CLIENT", openai_client), patch.object(
            finder, "_verify", return_value=_OK
        ):
            finder.get_answer(
                [{"role": "user", "content": "q"}], use_cache=True, model="gpt-5.6-sol"
            )

        # Reasoning tokens share max_output_tokens on the Responses API, so the
        # request budget must exceed the visible-text budget.
        self.assertEqual(
            openai_client.responses.create.call_args.kwargs["max_output_tokens"],
            finder.MAX_TOKENS + finder._OPENAI_REASONING_HEADROOM,
        )

    def test_anthropic_request_uses_the_visible_budget_directly(self):
        anthropic_client = Mock()
        anthropic_client.messages.create.return_value = _anthropic_response("answer")

        with patch.object(finder, "_ANTHROPIC_CLIENT", anthropic_client), patch.object(
            finder, "_verify", return_value=_OK
        ):
            finder.get_answer(
                [{"role": "user", "content": "q"}],
                use_cache=True,
                model="claude-opus-4-8",
            )

        self.assertEqual(
            anthropic_client.messages.create.call_args.kwargs["max_tokens"],
            finder.MAX_TOKENS,
        )

    def test_verifier_gets_its_own_budget(self):
        openai_client = Mock()
        openai_client.responses.create.return_value = _openai_response('{"ok": true}')

        with patch.object(finder, "_OPENAI_CLIENT", openai_client):
            finder._verify([{"role": "user", "content": "q"}], "draft")

        self.assertEqual(
            openai_client.responses.create.call_args.kwargs["max_output_tokens"],
            finder.VERIFIER_MAX_TOKENS,
        )


class TruncationTest(unittest.TestCase):
    """Truncation is detected from the provider's own stop signal, never guessed
    from token counts, so a completed answer is never rewritten."""

    _PARTIAL_LIST = (
        "## Standalone databases\n\n"
        "- [Bloomberg Law](http://www.bloomberglaw.com/)\n"
        "- [Jus Mundi](https://jusmundi-com.ezproxy.law.stanford.edu/en)\n"
        "- [WorldTradeLaw.net](https://www-worldtradelaw-net.ezproxy.law"
    )

    def test_anthropic_max_tokens_stop_is_repaired_and_disclosed(self):
        anthropic_client = Mock()
        anthropic_client.messages.create.return_value = _anthropic_response(
            self._PARTIAL_LIST, stop_reason="max_tokens"
        )

        with patch.object(finder, "_ANTHROPIC_CLIENT", anthropic_client), patch.object(
            finder, "_verify", return_value=_OK
        ):
            answer, _messages, usage, _initial = finder.get_answer(
                [{"role": "user", "content": "list them all"}],
                use_cache=True,
                model="claude-opus-4-8",
            )

        self.assertTrue(usage["truncated"])
        self.assertIn("reached its length limit", answer)
        self.assertIn(finder._LEGAL_DATABASES_URL, answer)
        # The chopped URL is gone — it would otherwise trip the link guardrail.
        self.assertNotIn("www-worldtradelaw-net.ezproxy.law\n", answer)
        self.assertNotIn("WorldTradeLaw", answer)
        self.assertIn("Jus Mundi", answer)  # complete entries survive

    def test_openai_incomplete_on_max_output_tokens_is_repaired(self):
        openai_client = Mock()
        openai_client.responses.create.return_value = _openai_response(
            self._PARTIAL_LIST,
            status="incomplete",
            incomplete=SimpleNamespace(reason="max_output_tokens"),
        )

        with patch.object(finder, "_OPENAI_CLIENT", openai_client), patch.object(
            finder, "_verify", return_value=_OK
        ):
            answer, _messages, usage, _initial = finder.get_answer(
                [{"role": "user", "content": "list them all"}],
                use_cache=True,
                model="gpt-5.6-sol",
            )

        self.assertTrue(usage["truncated"])
        self.assertIn("reached its length limit", answer)
        self.assertNotIn("WorldTradeLaw", answer)

    def test_truncation_survives_the_link_guardrail(self):
        # The repaired reply carries the Legal Databases URL, so the guardrail
        # must accept it rather than burn regenerations and fall back.
        self.assertTrue(
            finder._local_guardrail(finder._finish_truncated(self._PARTIAL_LIST))["ok"]
        )
        self.assertTrue(finder._local_guardrail(finder._TRUNCATED_EMPTY)["ok"])

    def test_completed_answers_are_never_rewritten(self):
        for label, client_attr, client, response in [
            (
                "anthropic",
                "_ANTHROPIC_CLIENT",
                Mock(),
                _anthropic_response("Use [Bloomberg Law](http://www.bloomberglaw.com/)."),
            ),
            (
                "openai",
                "_OPENAI_CLIENT",
                Mock(),
                _openai_response("Use [Bloomberg Law](http://www.bloomberglaw.com/)."),
            ),
        ]:
            with self.subTest(label):
                if label == "anthropic":
                    client.messages.create.return_value = response
                    model = "claude-opus-4-8"
                else:
                    client.responses.create.return_value = response
                    model = "gpt-5.6-sol"

                with patch.object(finder, client_attr, client), patch.object(
                    finder, "_verify", return_value=_OK
                ):
                    answer, _m, usage, _i = finder.get_answer(
                        [{"role": "user", "content": "q"}], use_cache=True, model=model
                    )

                self.assertFalse(usage["truncated"])
                self.assertEqual(
                    answer, "Use [Bloomberg Law](http://www.bloomberglaw.com/)."
                )

    def test_non_length_incomplete_reason_is_not_treated_as_truncation(self):
        response = _openai_response(
            "partial", status="incomplete", incomplete={"reason": "content_filter"}
        )
        self.assertFalse(finder._openai_hit_ceiling(response))
        # dict-shaped details are handled for the length case too
        self.assertTrue(
            finder._openai_hit_ceiling(
                _openai_response(
                    "p", status="incomplete", incomplete={"reason": "max_output_tokens"}
                )
            )
        )

    def test_finish_truncated_falls_back_to_the_last_sentence(self):
        repaired = finder._finish_truncated(
            "Bloomberg Law is the better fit for dockets. For historical mate"
        )
        self.assertTrue(repaired.startswith("Bloomberg Law is the better fit"))
        self.assertNotIn("For historical mate", repaired)
        self.assertIn("reached its length limit", repaired)

    def test_finish_truncated_handles_nothing_usable(self):
        self.assertEqual(finder._finish_truncated("## Standalone data"), finder._TRUNCATED_EMPTY)
        self.assertEqual(finder._finish_truncated(""), finder._TRUNCATED_EMPTY)


if __name__ == "__main__":
    unittest.main()
