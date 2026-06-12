# Stanford Law Library Database Finder

AI assistant that recommends the right legal-research database from the
[Robert Crown Law Library](https://law.stanford.edu/robert-crown-law-library/)
collection, given a user's research question.

---

## Architecture

```
project/
  core/               ← portable; no Streamlit dependency
    catalog.py        ← loads catalog.json, builds system prompt once at startup
    finder.py         ← Anthropic/OpenAI API calls, provider routing, history trimming
  data/
    catalog.json      ← database catalog (source of truth)
  prompts/
    system_prompt.md  ← prompt template ({{CATALOG_JSON}} replaced at startup)
  app.py              ← Streamlit shell (eval only — password gate, UI, logging)
  logs/               ← created at runtime; JSONL eval log
```

---

## Local Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

Edit `.env` (already in the repo root with placeholder values):

```
ANTHROPIC_API_KEY=sk-ant-...          # your Anthropic API key
OPENAI_API_KEY=sk-...                 # your OpenAI API key
APP_PASSWORD=your_shared_password     # password shown to eval users
USE_CACHE=true                       # set false to disable prompt caching
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | — | Anthropic API key for Claude models |
| `OPENAI_API_KEY` | Yes | — | OpenAI API key for GPT models |
| `APP_PASSWORD` | Yes | — | Shared password for the eval access gate |
| `USE_CACHE` | Yes | `true` | Enable provider prompt caching |
| `GOOGLE_SHEET_ID` | No | — | Spreadsheet ID for consolidated logging (see below) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | No | — | Service account credentials JSON string (see below) |

### 3. ⚠️ Set an API spending limit (manual step — required before sharing)

The eval app has no rate limiting by design (the password gate is sufficient).
Before sharing the URL with evaluators, set **monthly spending limits** on your
provider API keys. For Anthropic, use the
[Anthropic Console](https://console.anthropic.com/) → API Keys → your key →
Spending Limits. For OpenAI, use the platform billing/limits settings. This
caps cost exposure if the password leaks.

### 4. Run

```bash
streamlit run app.py
```

Open [http://localhost:8501](http://localhost:8501).

---

## Streamlit Community Cloud Deployment

1. Push this repo to GitHub. **Do not commit `.env` or `logs/`** — both are in
   `.gitignore`.

2. Go to [share.streamlit.io](https://share.streamlit.io), connect the repo,
   and set the main file to `app.py`.

3. In the app settings → **Secrets**, add your environment variables in TOML
   (see the Google Sheets section below for the full secrets block including
   `GOOGLE_SHEET_ID` and `GOOGLE_SERVICE_ACCOUNT_JSON`).

4. Deploy

### ⚠️ Ephemeral log caveat

Streamlit Community Cloud's filesystem is **ephemeral** — `logs/qa_log.jsonl`
is lost on app restart or sleep. Set up Google Sheets logging to retain eval data persistently across all testers and restarts.

---

## Eval Log Format

`logs/qa_log.jsonl` — one JSON object per line, two record types:

**`"type": "answer"`** — written immediately when the API responds:
```json
{
  "type": "answer",
  "timestamp": "2026-01-15T18:30:00+00:00",
  "turn": 0,
  "question": "...",
  "trimmed_history_sent": [...],
  "answer": "...",
  "use_cache": true,
  "usage": {
    "input_tokens": 1234,
    "output_tokens": 87,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "cached_input_tokens": 0,
    "provider": "anthropic"
  },
  "model": "claude-opus-4-8",
  "provider": "anthropic",
  "initial_response": "..."
}
```

`initial_response` is the model's first draft before the output guardrail acts.
It is **blank when the draft passed unchanged**, and holds the original draft
only when the verifier triggered a regeneration or the deterministic safe
fallback (see Output Guardrail below).

**`"type": "feedback"`** — written when a 👍 or 👎 is submitted:
```json
{"type": "feedback", "turn": 0, "rating": "up", "note": "", "timestamp": "..."}
```

Correlate by `"turn"` (0-indexed per session).

---

### Sheet columns


| Column | Holds |
|---|---|
| timestamp | When the answer was logged (UTC, ISO 8601) |
| turn | 0-indexed turn within the search session |
| model | Model ID used for the answer (e.g. `claude-opus-4-8`) |
| question | The user's question |
| answer | The assistant's answer |
| use_cache | Whether prompt caching was requested |
| input_tokens | Uncached input tokens |
| output_tokens | Output tokens |
| cache_creation_tokens | Anthropic cache-write tokens |
| cache_read_tokens | Anthropic cache-read tokens |
| cached_input_tokens | OpenAI cached input tokens |
| feedback | 👍 / 👎, filled in when the user rates (blank if not rated) |
| comment | The user's 👎 note, filled in with the feedback (blank otherwise) |
| initial_response | The model's first draft, before the guardrail acted. **Blank when the draft passed unchanged**; holds the original draft when the verifier triggered a regeneration or the safe fallback |

---

## Prompt Caching

The system prompt + full catalog is built **once at import time**
(`core/catalog.py`) and remains byte-identical across requests. Each search is pinned to one model because provider prompt caches are model-specific.

Each Q&A log record includes Anthropic cache fields
(`cache_creation_input_tokens`, `cache_read_input_tokens`) and OpenAI cache hits (`cached_input_tokens`) so you can measure the savings.

---

## Production Requirements 

When migrating to the Stanford production server, **only `app.py` is replaced**.
The `core/` package, `data/catalog.json`, and `prompts/system_prompt.md` move over untouched.

The production shell implements:

1. **Stanford Auth (SUNet)** — replace the `APP_PASSWORD` form with Stanford
   SSO / Shibboleth. The eval password gate is removed entirely.

2. **Rate limiting:**
   - 10 requests per minute per user
   - 100 requests per hour per user

   The eval shell intentionally omits rate limiting; the password gate is sufficient to prevent bot/anonymous traffic during eval.

3. **Page embed** — integrate the app into the existing Legal Databases page

No changes to `core/catalog.py`, `core/finder.py`, `catalog.json`, or
`system_prompt.md` are expected or required for the production migration.
