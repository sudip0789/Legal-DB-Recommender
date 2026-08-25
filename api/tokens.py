"""HMAC signing of assistant answers.

Because the browser now holds the conversation and posts the full history back
each turn, a crafted request could otherwise fake an assistant turn to steer the
model. Each answer is returned with an HMAC signature; the client echoes it back
on that assistant turn; the backend rejects any assistant message whose
signature doesn't verify. The secret is read lazily so it is available after
``api.bootstrap`` has populated the environment.
"""

from __future__ import annotations

import hashlib
import hmac
import os


def _secret() -> bytes:
    key = os.environ.get("ANSWER_SIG_SECRET", "")
    if not key:
        # bootstrap guarantees this is set; treat absence as a hard error rather
        # than silently signing with an empty key.
        raise RuntimeError("ANSWER_SIG_SECRET is not configured")
    return key.encode("utf-8")


def sign(answer: str) -> str:
    return hmac.new(_secret(), (answer or "").encode("utf-8"), hashlib.sha256).hexdigest()


def verify(answer: str, sig) -> bool:
    """Constant-time check that ``sig`` is a valid signature for ``answer``."""
    if not isinstance(sig, str) or not sig:
        return False
    try:
        return hmac.compare_digest(sign(answer), sig)
    except Exception:
        return False
