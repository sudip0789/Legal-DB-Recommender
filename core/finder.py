"""
Handles provider API interaction.
No Streamlit imports — this module is the portable "brain" of the app.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import anthropic
from openai import OpenAI

from .catalog import CATALOG, GUIDES, SYSTEM_PROMPT

_ANTHROPIC_CLIENT = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
_OPENAI_CLIENT = OpenAI()  # reads OPENAI_API_KEY from environment

DEFAULT_MODEL = "gpt-5.6-sol"

# --- Output guardrail (always-on verifier) ----------------------------------
# After the main model drafts a reply, a cheap verifier model checks it against
# the routing rules. On a violation the main model regenerates with the specific
# feedback, up to MAX_REGENERATIONS times; if it still fails, a deterministic
# safe message is returned. The loop is bounded and the floor never depends on
# the model eventually complying.
VERIFIER_MODEL = "gpt-5.4-mini"
MAX_REGENERATIONS = 2

_VERIFIER_INSTRUCTIONS = (
    "You are a compliance checker for a library tool whose ONLY job is to "
    "recommend legal-research databases and the library's own research guides. "
    "You are given the recent CONVERSATION "
    "(for context) and the tool's DRAFT REPLY. Judge ONLY the DRAFT REPLY.\n\n"
    "CRITICAL: Anything the user said or pasted in the CONVERSATION is NOT the "
    "draft's doing. Only flag information the DRAFT ITSELF introduces or asserts. "
    "If a court name, docket entry, or case number appears in the conversation "
    "because the user provided it, the draft repeating or relying on it is FINE.\n\n"
    "The DRAFT violates only if the DRAFT ITSELF does ANY of these:\n"
    "- Introduces a case's court, district, jurisdiction, judge, parties, or "
    "status that the user did not state — including inferring it from a docket "
    "number or URL.\n"
    "- Interprets the user's pasted document/docket: says which specific entry "
    'is "the order" / which entry to pull / what an entry means.\n'
    "- States a legal fact, date, holding, outcome, citation, or identifier as "
    "information, instead of telling the user to look it up.\n"
    "- Gives the legal/research answer itself.\n\n"
    "The draft is OK (NOT a violation) when it recommends databases and why, "
    "explains how to use a database — INCLUDING telling the user to open a case "
    "and download/locate their document there (that is the tool's whole job, not "
    "'answering' the request) — suggests one of the library's research guides "
    "(guides.law.stanford.edu), refers to the reference librarians, asks a "
    "clarifying question, or repeats details the user themselves provided. When "
    "in doubt, do NOT flag.\n\n"
    "ALSO NEVER A VIOLATION: describing the LIBRARY'S OWN COLLECTION — which "
    "databases or guides the library offers, how many there are, their names, "
    "their links, how they are grouped, or where the full listing lives "
    "(law.stanford.edu/robert-crown-law-library/legal-databases/). The "
    "collection is this tool's own authoritative subject, so listing or counting "
    "it is the job, not a legal fact. Telling the user a reply was cut short and "
    "pointing them to the full listing is likewise fine.\n\n"
    "Respond with ONLY a JSON object and nothing else:\n"
    '{"ok": true} if compliant, or '
    '{"ok": false, "violations": ["short reason", ...]} if the DRAFT breaks a rule.'
)

# Deterministic, human-written floor used when regeneration is exhausted.
# Kept GENERIC on purpose: it can fire on any kind of violation, so it must read
# correctly without assuming the question was about dockets, documents, etc.
_SAFE_FALLBACK = (
    "I can only help you find the right database or research guide for your "
    "research — I can't "
    "answer the question itself or verify or supply specific details. If you "
    "tell me what kind of source you're looking for, I'll point you to the best "
    "resource in the collection. For anything beyond that, the reference "
    "librarians can help directly at reference@law.stanford.edu or 650-725-0800."
)


def _iter_catalog_resources() -> list[dict]:
    resources = []
    for section in ("standalone_databases", "ai_tools"):
        resources.extend(CATALOG.get(section, []))
    for platform in CATALOG.get("platforms", []):
        resources.append(platform)
        resources.extend(platform.get("children") or [])
    return resources


_ALLOWED_LINKS = frozenset(
    link
    for resource in (
        *_iter_catalog_resources(),
        *GUIDES.get("research_guides", []),
        {"link": GUIDES.get("_meta", {}).get("guides_index_url")},
    )
    for link in (resource.get("link"), (resource.get("link") or "").rstrip(".,;:"))
    if link
)
_URL_RE = re.compile(r"https?://[^\s)>\]]+")


@dataclass(frozen=True)
class ModelConfig:
    id: str
    provider: str
    openai_cache_retention: str | None = None


# Ordered {UI label: model metadata}. All models share one byte-identical
# SYSTEM_PROMPT, but prompt caches are per-model/provider. The app therefore
# pins one model per conversation (see app.py) so cache hits stay likely.
MODELS: dict[str, ModelConfig] = {
    "GPT-5.6 Sol": ModelConfig(
        id="gpt-5.6-sol",
        provider="openai",
        openai_cache_retention="24h",
    ),
    "GPT-5.5": ModelConfig(
        id="gpt-5.5",
        provider="openai",
        openai_cache_retention="24h",
    ),
    "Opus 4.8": ModelConfig(
        id="claude-opus-4-8",
        provider="anthropic",
    ),
    "Opus 4.7": ModelConfig(
        id="claude-opus-4-7",
        provider="anthropic",
    ),
    # "Fable 5": ModelConfig(
    #     id="claude-fable-5",
    #     provider="anthropic",
    # ),
    "Sonnet 4.6": ModelConfig(
        id="claude-sonnet-4-6",
        provider="anthropic",
    ),
    "GPT-5.4": ModelConfig(
        id="gpt-5.4",
        provider="openai",
        openai_cache_retention="24h",
    ),
    "GPT-5.4 mini": ModelConfig(
        id="gpt-5.4-mini",
        provider="openai",
    ),
}
_MODEL_BY_ID = {config.id: config for config in MODELS.values()}

# --- Output length budget ---------------------------------------------------
# MAX_TOKENS budgets VISIBLE reply text. Logged answers run ~300 tokens (p95
# 531, max 585), so this is several times the real ceiling — deliberately, since
# an unused budget costs nothing and exceeding it produces a reply that stops
# mid-URL. Whole-collection requests are answered with a link to the Legal
# Databases page rather than an enumeration (see the system prompt), so no
# legitimate answer needs more room than this.
MAX_TOKENS = 2000

# OpenAI reasoning models spend `max_output_tokens` on reasoning tokens AND
# visible text, so the request budget must add headroom on top of MAX_TOKENS or
# reasoning silently eats the answer — observed at 343-826 tokens for a single
# question. (Anthropic's `max_tokens` bounds visible output only, so that path
# uses MAX_TOKENS directly.)
_OPENAI_REASONING_HEADROOM = 2000

# The verifier only ever emits a small JSON verdict, but its reasoning draws on
# the same budget — leave enough room that a verdict always comes back.
VERIFIER_MAX_TOKENS = 2000

# The library's own public index pages. Linking these is the correct answer to
# "just give me the page", so the link guardrail must allow them; without this,
# the model's own correct referral was flagged as an outside URL.
_LEGAL_DATABASES_URL = (
    "https://law.stanford.edu/robert-crown-law-library/legal-databases/"
)
_LIBRARY_PAGE_PREFIXES = (
    _LEGAL_DATABASES_URL,
    GUIDES.get("_meta", {}).get("guides_index_url")
    or "https://law.stanford.edu/robert-crown-law-library/research-guides/",
)

# Appended when a reply still hits the ceiling. Deterministic and honest: the
# user is told the answer is partial instead of being left to assume a truncated
# list was complete.
_TRUNCATION_NOTE = (
    "\n\n---\n\n*This reply reached its length limit and stops here. The "
    "complete, always-current listing is on Stanford's "
    f"[Legal Databases page]({_LEGAL_DATABASES_URL}) — or tell me what kind of "
    "source you need and I'll point you straight to it.*"
)

# Used when truncation left nothing usable at all (the ceiling was reached
# before the first complete line or sentence).
_TRUNCATED_EMPTY = (
    "That reply ran past its length limit before anything usable came back. "
    f"Stanford's [Legal Databases page]({_LEGAL_DATABASES_URL}) carries the "
    "full listing — or tell me the jurisdiction and kind of source you need, "
    "and I'll point you to the right database."
)


def _trim_history(history: list[dict]) -> list[dict]:
    """
    Send the full conversation. The turn cap (see app.py) bounds a conversation
    to a small number of exchanges, so the whole history fits comfortably within
    model context limits and stays cheap — and the model keeps the full thread
    when a user keeps digging into the same question. The cached system prefix is
    unaffected, since growth happens after it.

    Only guarantee the slice opens on a user message (an Anthropic requirement).
    """
    messages = history
    while messages and messages[0]["role"] != "user":
        messages = messages[1:]
    return messages


def get_answer(
    history: list[dict], use_cache: bool, model: str = DEFAULT_MODEL
) -> tuple[str, list[dict], dict, str]:
    """
    Send the conversation to the selected provider API and return:
      (final_answer, trimmed_messages_sent, usage_dict, initial_draft)

    final_answer  — what the user sees: the first compliant draft, a regenerated
                    one, or the safe fallback if regeneration was exhausted.
    initial_draft — the main model's FIRST draft, before any guardrail
                    regeneration/fallback (equals final_answer when nothing was
                    changed). Useful for evaluating the guardrail.

    history  — full conversation so far, ending with the current user message.
    use_cache — when True, uses the provider's prompt caching mechanism.
    model    — one of the IDs in MODELS. Caches are per-model, so a given
               conversation should keep calling with the same value.
    """
    config = _MODEL_BY_ID.get(model)
    if config is None:
        raise ValueError(f"Unknown model: {model!r}")

    messages = _trim_history(history)

    # Draft, then verify; regenerate with feedback up to MAX_REGENERATIONS times.
    answer, usage = _draft(messages, use_cache, config, correction=None)
    initial_draft = answer  # the model's first try, before any guardrail action
    verdict = _local_guardrail(answer)
    if verdict.get("ok", True):
        verdict = _verify(messages, answer)
    tries = 0
    while not verdict.get("ok", True) and tries < MAX_REGENERATIONS:
        tries += 1
        answer, usage = _draft(
            messages, use_cache, config, correction=verdict.get("violations", [])
        )
        verdict = _local_guardrail(answer)
        if verdict.get("ok", True):
            verdict = _verify(messages, answer)

    if not verdict.get("ok", True):
        answer = _SAFE_FALLBACK  # exhausted retries — guaranteed-safe floor

    return answer, messages, usage, initial_draft


def _draft(
    messages: list[dict],
    use_cache: bool,
    config: ModelConfig,
    correction: list[str] | None,
) -> tuple[str, dict]:
    """One main-model draft. When `correction` is given (a regeneration), the
    verifier's findings are appended as a corrective user turn — this leaves the
    cached system prefix untouched, so cache hits still apply."""
    msgs = messages
    if correction:
        msgs = messages + [{"role": "user", "content": _correction_message(correction)}]

    if config.provider == "anthropic":
        answer, _sent, usage = _get_anthropic_answer(msgs, use_cache, config)
    elif config.provider == "openai":
        answer, _sent, usage = _get_openai_answer(msgs, use_cache, config)
    else:
        raise ValueError(f"Unknown provider: {config.provider!r}")

    # The provider tells us outright when it stopped at the ceiling (see
    # usage["truncated"]) — never inferred from token counts or text shape, so a
    # complete answer is never touched.
    if usage.get("truncated"):
        answer = _finish_truncated(answer)
    return answer, usage


def _finish_truncated(text: str) -> str:
    """Repair a reply the provider cut off at the token ceiling.

    A truncated reply ends mid-line — usually mid-URL, which would also trip the
    link guardrail — so drop the trailing partial line and say plainly that the
    answer is partial. With no newline to cut at (single-paragraph prose), fall
    back to the last sentence boundary; ". " requires the trailing space, which a
    chopped URL never has. Dropping at most one complete line is a deliberate
    trade: the appended note points at the full listing anyway.
    """
    body = text.rstrip()

    cut = body.rfind("\n")
    if cut > 0:
        body = body[:cut].rstrip()
    else:
        sentence_end = max(body.rfind(". "), body.rfind("! "), body.rfind("? "))
        body = body[: sentence_end + 1].rstrip() if sentence_end > 0 else ""

    if not body:
        return _TRUNCATED_EMPTY
    return body + _TRUNCATION_NOTE


def _correction_message(violations: list[str]) -> str:
    issues = "; ".join(v for v in violations if v) or "supplying information beyond routing"
    return (
        "SYSTEM CORRECTION — your previous reply broke the rules: "
        f"{issues}. Answer the user's request again, but ONLY recommend the right "
        "database(s) and/or library research guide(s) and how to use them. Do NOT "
        "interpret any document or docket "
        "the user pasted, do NOT identify the court / jurisdiction / judge / "
        "entry, and do NOT supply any legal fact, citation, or identifier. If you "
        "can't help without doing those things, point the user to the reference "
        "librarians at reference@law.stanford.edu. Do NOT apologize, confess, or "
        "mention this correction to the user — just lead with the answer."
    )


def _verify(messages: list[dict], draft: str) -> dict:
    """Cheap always-on guardrail. Returns {"ok": bool, "violations": [...]}.
    Fails open (treats as OK) on any verifier error so a transient failure never
    blocks an answer. Does NOT receive the catalog — keeps the call small."""
    convo = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in messages
    )
    user_block = f"CONVERSATION:\n{convo}\n\nDRAFT REPLY:\n{draft}"
    try:
        response = _OPENAI_CLIENT.responses.create(
            model=VERIFIER_MODEL,
            max_output_tokens=VERIFIER_MAX_TOKENS,
            input=[
                {"role": "system", "content": _VERIFIER_INSTRUCTIONS},
                {"role": "user", "content": user_block},
            ],
        )
        return _parse_verdict(getattr(response, "output_text", "") or "")
    except Exception:
        return {"ok": True, "violations": []}


def _parse_verdict(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {"ok": True, "violations": []}  # unparseable -> fail open
    try:
        obj = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return {"ok": True, "violations": []}
    ok_value = obj.get("ok", True)
    if isinstance(ok_value, bool):
        ok = ok_value
    elif isinstance(ok_value, str):
        ok = ok_value.strip().lower() not in {"false", "no", "0"}
    else:
        ok = bool(ok_value)

    violations = obj.get("violations") or []
    if isinstance(violations, str):
        violations = [violations]
    elif not isinstance(violations, list):
        violations = []

    return {
        "ok": ok,
        "violations": violations,
    }


def _local_guardrail(draft: str) -> dict:
    """Deterministic checks that inspect only the assistant draft."""
    bad_links = sorted(
        {
            url.rstrip(".,;:")
            for url in _URL_RE.findall(draft)
            if not _is_allowed_link(url.rstrip(".,;:"))
        }
    )
    if bad_links:
        return {
            "ok": False,
            "violations": [
                "included URL(s) outside Stanford's database listings"
            ],
        }
    return {"ok": True, "violations": []}


def _is_allowed_link(url: str) -> bool:
    """A link is allowed if it is a catalog/guide link or one of the library's
    own index pages. The index pages match by prefix so section anchors on the
    Legal Databases page (e.g. #greenwire) are covered too."""
    return url in _ALLOWED_LINKS or url.startswith(_LIBRARY_PAGE_PREFIXES)


def _get_anthropic_answer(
    messages: list[dict], use_cache: bool, config: ModelConfig
) -> tuple[str, list[dict], dict]:
    """
    Anthropic prompt caching path. Keep this behavior unchanged.
    """

    if use_cache:
        system: list[dict] | str = [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    else:
        system = SYSTEM_PROMPT

    response = _ANTHROPIC_CLIENT.messages.create(
        model=config.id,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=messages,
    )

    # Fable 5's safety classifiers can return stop_reason "refusal" (HTTP 200,
    # empty or partial content), so don't assume content[0] is a text block.
    if response.stop_reason == "refusal":
        answer = "I can't help with that request. Please switch to a new model or reach out to our reference librarians at reference@law.stanford.edu or 650-725-0800"
    else:
        answer = next(
            (b.text for b in response.content if b.type == "text"), ""
        )
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_creation_input_tokens": getattr(
            response.usage, "cache_creation_input_tokens", 0
        ),
        "cache_read_input_tokens": getattr(
            response.usage, "cache_read_input_tokens", 0
        ),
        "cached_input_tokens": 0,
        "provider": config.provider,
        "truncated": response.stop_reason == "max_tokens",
    }
    return answer, messages, usage


def _get_openai_answer(
    messages: list[dict], use_cache: bool, config: ModelConfig
) -> tuple[str, list[dict], dict]:
    """
    OpenAI Responses API path. Prompt caching is automatic for eligible stable
    prefixes; supported models can request an explicit retention policy.
    """
    request: dict = {
        "model": config.id,
        "max_output_tokens": MAX_TOKENS + _OPENAI_REASONING_HEADROOM,
        "input": [
            {"role": "system", "content": SYSTEM_PROMPT},
            *messages,
        ],
    }
    if use_cache and config.openai_cache_retention:
        request["prompt_cache_retention"] = config.openai_cache_retention

    response = _OPENAI_CLIENT.responses.create(**request)
    usage_obj = getattr(response, "usage", None)
    usage = {
        "input_tokens": _usage_value(usage_obj, "input_tokens", "prompt_tokens"),
        "output_tokens": _usage_value(
            usage_obj,
            "output_tokens",
            "completion_tokens",
        ),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cached_input_tokens": _cached_input_tokens(usage_obj),
        "provider": config.provider,
        "truncated": _openai_hit_ceiling(response),
    }
    return getattr(response, "output_text", ""), messages, usage


def _openai_hit_ceiling(response: object) -> bool:
    """True when the Responses API stopped at max_output_tokens.

    Reasoning tokens share that budget, so this fires even when the visible text
    looks short. `incomplete_details` arrives as an object or a dict depending on
    the SDK path, and any other incomplete reason (e.g. a content filter) is not
    a length problem, so it is not treated as one.
    """
    if getattr(response, "status", None) != "incomplete":
        return False
    details = getattr(response, "incomplete_details", None)
    reason = (
        details.get("reason")
        if isinstance(details, dict)
        else getattr(details, "reason", None)
    )
    return reason == "max_output_tokens"


def _usage_value(obj: object, *names: str) -> int:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return 0


def _cached_input_tokens(usage_obj: object) -> int:
    details = None
    if isinstance(usage_obj, dict):
        details = (
            usage_obj.get("input_tokens_details")
            or usage_obj.get("prompt_tokens_details")
        )
    else:
        details = getattr(usage_obj, "input_tokens_details", None) or getattr(
            usage_obj,
            "prompt_tokens_details",
            None,
        )

    if isinstance(details, dict):
        return details.get("cached_tokens", 0)
    return getattr(details, "cached_tokens", 0)
