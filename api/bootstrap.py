"""Populate os.environ from secrets, then validate.

MUST be imported before ``core.finder`` — that module constructs its provider
client at import time, so the API keys have to be in the environment first.
``api.main`` imports this on its very first line for exactly that reason.

Two sources, chosen automatically:
  * AWS  — if RCLL_SECRET_ARN is set, read the JSON secret from Secrets Manager.
  * local — otherwise load a .env file (developer machine / the Streamlit app).

os.environ.setdefault is used so an explicitly-set variable always wins — which
is what lets the unit tests and local overrides work without touching a secret.
"""

from __future__ import annotations

import json
import os

# Required in every environment. GOOGLE_* are intentionally NOT required —
# Google Sheets logging is best-effort and the app runs fine without it.
_REQUIRED = ("OPENAI_API_KEY", "ANSWER_SIG_SECRET")


def load() -> None:
    if os.getenv("RCLL_SECRETS_LOADED"):
        return

    arn = os.getenv("RCLL_SECRET_ARN", "").strip()
    if arn:
        import boto3  # imported lazily so local dev needs no AWS SDK

        raw = boto3.client("secretsmanager").get_secret_value(SecretId=arn)[
            "SecretString"
        ]
        for key, value in json.loads(raw).items():
            if value is not None:
                os.environ.setdefault(key, str(value))
    else:
        # Local development: load .env if python-dotenv is available.
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except Exception:
            pass

    # A missing HMAC secret is fatal in AWS (it must be provisioned in the
    # secret). Locally we mint an ephemeral one so `uvicorn --reload` just works;
    # signatures simply won't survive a restart, which is fine for dev.
    if not os.getenv("ANSWER_SIG_SECRET") and not arn:
        import secrets as _secrets

        os.environ["ANSWER_SIG_SECRET"] = _secrets.token_hex(32)

    missing = [name for name in _REQUIRED if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Missing required configuration: " + ", ".join(missing)
        )

    os.environ["RCLL_SECRETS_LOADED"] = "1"


load()
