import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from core import catalog, finder  # noqa: E402

_OK = {"ok": True, "violations": []}


def _anthropic_response(text: str):
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            cache_creation_input_tokens=80,
            cache_read_input_tokens=0,
        ),
    )


class GuidesDataTest(unittest.TestCase):
    def test_every_guide_has_name_link_and_keywords(self):
        guides = catalog.GUIDES["research_guides"]
        self.assertTrue(guides)
        for guide in guides:
            self.assertTrue(guide.get("name"), f"guide missing name: {guide}")
            self.assertTrue(
                guide.get("link", "").startswith("https://guides.law.stanford.edu"),
                f"unexpected link for {guide.get('name')}: {guide.get('link')}",
            )
            self.assertTrue(
                guide.get("keywords"), f"guide has no keywords: {guide.get('name')}"
            )

    def test_guide_count_matches_meta(self):
        self.assertEqual(
            len(catalog.GUIDES["research_guides"]),
            catalog.GUIDES["_meta"]["guide_count"],
        )

    def test_guide_links_are_unique(self):
        links = [g["link"] for g in catalog.GUIDES["research_guides"]]
        self.assertEqual(len(links), len(set(links)))


class GuidesPromptInjectionTest(unittest.TestCase):
    def test_system_prompt_contains_guides_and_no_placeholders(self):
        self.assertIn("guides.law.stanford.edu/dockets", catalog.SYSTEM_PROMPT)
        self.assertNotIn("{{GUIDES_JSON}}", catalog.SYSTEM_PROMPT)
        self.assertNotIn("{{CATALOG_JSON}}", catalog.SYSTEM_PROMPT)


class GuidesGuardrailTest(unittest.TestCase):
    def test_all_guide_links_are_allowed(self):
        for guide in catalog.GUIDES["research_guides"]:
            self.assertIn(guide["link"], finder._ALLOWED_LINKS, guide["name"])
        self.assertIn(
            catalog.GUIDES["_meta"]["guides_index_url"], finder._ALLOWED_LINKS
        )

    def test_local_guardrail_passes_query_string_guide_link(self):
        # Directed Research Projects is the one guide URL with a query string.
        verdict = finder._local_guardrail(
            "See the [Directed Research Projects]"
            "(https://guides.law.stanford.edu/c.php?g=1255722) guide."
        )
        self.assertTrue(verdict["ok"])

    def test_local_guardrail_blocks_unlisted_guide_link(self):
        verdict = finder._local_guardrail(
            "See [Some Guide](https://guides.law.stanford.edu/not-a-real-guide)."
        )
        self.assertFalse(verdict["ok"])

    def test_draft_with_guide_link_passes_through_get_answer(self):
        anthropic_client = Mock()
        anthropic_client.messages.create.return_value = _anthropic_response(
            "Use [Bloomberg Law](http://www.bloomberglaw.com/). For strategies, "
            "see the [Docket Research](https://guides.law.stanford.edu/dockets) guide."
        )

        with patch.object(finder, "_ANTHROPIC_CLIENT", anthropic_client), patch.object(
            finder, "_verify", return_value=_OK
        ):
            answer, _messages, _usage, _initial = finder.get_answer(
                [{"role": "user", "content": "Where do I find court filings?"}],
                use_cache=True,
                model="claude-opus-4-8",
            )

        self.assertIn("https://guides.law.stanford.edu/dockets", answer)
        self.assertNotEqual(answer, finder._SAFE_FALLBACK)


if __name__ == "__main__":
    unittest.main()
