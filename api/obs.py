"""Structured JSON logging to stdout (captured by CloudWatch).

Privacy rule: log METADATA ONLY. Never the question text, the answer text, or
the system prompt. Full question/answer text lives in the Google Sheet by
design, for librarian review; CloudWatch must not become a second, longer-lived
copy of patron research questions. A short salted-free hash of the question is
logged only so a metadata line can be correlated to a Sheet row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys

_log = logging.getLogger("rcll")
if not _log.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _log.addHandler(_handler)
    _log.setLevel(logging.INFO)
    _log.propagate = False


def _line(event: str, fields: dict) -> str:
    return json.dumps({"event": event, **fields}, ensure_ascii=False, default=str)


def event(name: str, **fields) -> None:
    try:
        _log.info(_line(name, fields))
    except Exception:
        pass


def metric(name: str, **fields) -> None:
    event(name, **fields)


def exception(name: str, **fields) -> None:
    """Log an event with the current exception traceback attached."""
    try:
        _log.error(_line(name, fields), exc_info=True)
    except Exception:
        pass


def qhash(text: str) -> str:
    """Short digest of a question, for correlating a log line to a Sheet row
    without storing the question text itself."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]
