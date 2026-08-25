"""API-layer configuration, read from environment variables.

Imported after ``api.bootstrap`` has populated os.environ. These are plain
(non-secret) config values, set as Lambda environment variables in the SAM
template; the secret values live in Secrets Manager (see bootstrap).
"""

from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


# CORS: comma-separated list of exact allowed origins. NEVER "*" — the endpoint
# is public and holds provider keys, so only the site(s) we ship may call it.
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in (
        os.getenv("ALLOWED_ORIGINS", "https://rcll-finder.vercel.app").split(",")
    )
    if origin.strip()
]

# Fixed model. The client's `model` field is ignored — not exposing the choice
# is stronger than validating it, and keeps prompt caching to a single prefix.
MODEL = os.getenv("MODEL_ID", "gpt-5.6-sol")

# DynamoDB table for rate-limit state. Empty string disables rate limiting,
# which is the intended behavior for local development (no AWS).
RATE_TABLE = (os.getenv("RATE_TABLE") or "").strip()

# Per-session sliding window (fairness for honest users; not a security control
# on its own, since a session id is client-supplied).
PER_MIN = _int("PER_MIN", 10)
PER_HOUR = _int("PER_HOUR", 100)

# Per-IP (/24) window — catches one abusive host without throttling a whole
# NAT'd campus. This is the tier that actually holds against session rotation.
IP_PER_MIN = _int("IP_PER_MIN", 40)
IP_PER_HOUR = _int("IP_PER_HOUR", 400)

# Global daily answer cap — the interim dollar-proxy until the provider spend
# cap is in place. <= 0 disables it.
DAILY_CAP = _int("DAILY_CAP", 1500)

# Input size caps, checked before any LLM call. Nothing else bounds prompt size,
# so these are the second-biggest cost control after rate limiting.
MAX_MESSAGES = _int("MAX_MESSAGES", 21)
MAX_MSG_CHARS = _int("MAX_MSG_CHARS", 4000)
MAX_HISTORY_CHARS = _int("MAX_HISTORY_CHARS", 24000)
MAX_TURNS = _int("MAX_TURNS", 10)

# In-app budget for the draft/verify/regenerate loop. Kept below the Lambda
# timeout so there is room to write the Sheet and flush the response.
BUDGET_S = _int("BUDGET_S", 210)
