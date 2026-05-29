# Stanford Law Library Database Finder

AI assistant that recommends the right legal-research database from the
[Robert Crown Law Library](https://law.stanford.edu/robert-crown-law-library/)
collection, given a user's research question. Built on the Anthropic API.

---

## Architecture

Two layers, cleanly separated so Stanford's production team inherits the
"brain" and rebuilds only the "shell":

```
project/
  core/               ← portable; no Streamlit dependency
    catalog.py        ← loads catalog.json, builds system prompt once at startup
    finder.py         ← Anthropic API call, history trimming, cache toggle
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
APP_PASSWORD=your_shared_password     # password shown to eval users
USE_CACHE=false                       # set true to enable prompt caching
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | — | Anthropic API key |
| `APP_PASSWORD` | Yes | — | Shared password for the eval access gate |
| `USE_CACHE` | No | `false` | Enable Anthropic ephemeral prompt caching |
| `GOOGLE_SHEET_ID` | No | — | Spreadsheet ID for consolidated logging (see below) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | No | — | Service account credentials JSON string (see below) |

### 3. ⚠️ Set an API spending limit (manual step — required before sharing)

The eval app has no rate limiting by design (the password gate is sufficient).
Before sharing the URL with evaluators, set a **monthly spending limit** on your
API key in the [Anthropic Console](https://console.anthropic.com/) → API Keys →
your key → Spending Limits. This caps cost exposure if the password leaks.

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

3. In the app settings → **Secrets**, add your environment variables in TOML:

```toml
ANTHROPIC_API_KEY = "sk-ant-..."
APP_PASSWORD = "your_password"
USE_CACHE = "false"
```

4. Deploy. Share the URL with eval participants.

### ⚠️ Ephemeral log caveat

Streamlit Community Cloud's filesystem is **ephemeral** — `logs/qa_log.jsonl`
is lost on app restart or sleep. Set up Google Sheets logging (below) to
retain eval data persistently across all testers and restarts.

---

## Google Sheets Logging (consolidated, persistent)

Every Q&A and feedback event is appended to a shared Google Sheet in addition
to the local JSONL. This means all testers — regardless of which machine they
use or which instance of the app they hit — write to the same spreadsheet. It
also survives Streamlit Cloud restarts.

### One-time setup

1. **Create a Google Cloud project** (or reuse one) at
   [console.cloud.google.com](https://console.cloud.google.com).

2. **Enable the Google Sheets API** — search "Sheets API" in the library and
   click Enable.

3. **Create a service account** — IAM & Admin → Service Accounts → Create. No
   special roles needed. Generate a JSON key and download it.

4. **Create a Google Sheet** and copy its ID from the URL:
   `https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit`

5. **Share the sheet** with the service account's email address
   (found in the JSON key file as `client_email`), granting **Editor** access.

6. **Add the credentials to `.env`:**

   ```
   GOOGLE_SHEET_ID=1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms
   GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account","project_id":"..."}
   ```

   Paste the **entire contents** of the downloaded JSON key file as the value
   of `GOOGLE_SERVICE_ACCOUNT_JSON` (all on one line, no line breaks).

### For Streamlit Community Cloud

In the app settings → Secrets, you can either:

**Option A — JSON string** (same as local):
```toml
GOOGLE_SHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
GOOGLE_SERVICE_ACCOUNT_JSON = '{"type":"service_account","project_id":"..."}'
```

**Option B — TOML table** (easier to read, no escaping required):
```toml
GOOGLE_SHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"

[gcp_service_account]
type = "service_account"
project_id = "my-project"
private_key_id = "key-id"
private_key = "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----\n"
client_email = "name@my-project.iam.gserviceaccount.com"
client_id = "123456789"
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "https://www.googleapis.com/robot/v1/metadata/x509/name%40my-project.iam.gserviceaccount.com"
```

If neither `GOOGLE_SHEET_ID` nor credentials are set, Sheets logging is
silently skipped — the app still works and logs locally.

### Sheet columns

| Column | Answer records | Feedback records |
|---|---|---|
| timestamp | ✓ | ✓ |
| type | `answer` | `feedback` |
| turn | ✓ | ✓ |
| question | ✓ | |
| answer | ✓ | |
| use_cache | ✓ | |
| input_tokens | ✓ | |
| output_tokens | ✓ | |
| cache_creation_tokens | ✓ | |
| cache_read_tokens | ✓ | |
| rating | | `up` / `down` |
| note | | ✓ (if provided) |

Correlate answer and feedback rows by matching `turn` values within a session.

---

## Prompt Caching

When `USE_CACHE=true`, the system prompt + full catalog is sent with
`cache_control: {type: "ephemeral"}`. The prefix is built **once at import
time** (`core/catalog.py`) and is byte-identical across all requests — a
requirement for Anthropic cache hits.

- Ephemeral TTL: 5 minutes, sliding (resets on each cache hit).
- The sidebar checkbox lets evaluators toggle caching live to compare costs.
- Each Q&A log record includes `cache_creation_input_tokens` and
  `cache_read_input_tokens` so you can measure the savings.

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
  "use_cache": false,
  "usage": {
    "input_tokens": 1234,
    "output_tokens": 87,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0
  }
}
```

**`"type": "feedback"`** — written when a 👍 or 👎 is submitted:
```json
{"type": "feedback", "turn": 0, "rating": "up", "note": "", "timestamp": "..."}
```

Correlate by `"turn"` (0-indexed per session).

---

## Production Requirements (Stanford server rebuild)

When migrating to the Stanford production server, **only `app.py` is replaced**.
The `core/` package, `data/catalog.json`, and `prompts/system_prompt.md` move
over untouched.

The production shell must implement:

1. **Stanford Auth (SUNet)** — replace the `APP_PASSWORD` form with Stanford
   SSO / Shibboleth. The eval password gate is removed entirely.

2. **Rate limiting:**
   - 10 requests per minute per user
   - 100 requests per hour per user

   The eval shell intentionally omits rate limiting; the password gate is
   sufficient to prevent bot/anonymous traffic during eval.

3. **Page embed** — integrate the app into the existing Legal Databases page
   at the appropriate URL under `law.stanford.edu`.

No changes to `core/catalog.py`, `core/finder.py`, `catalog.json`, or
`system_prompt.md` are expected or required for the production migration.
