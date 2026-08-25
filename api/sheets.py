"""Best-effort Google Sheets logging of Q&A and feedback.

Rows use the SAME 14-column schema as the Streamlit ``app.py`` so both frontends
can append to the one live log tab without fighting over the header row. (Both
modules self-heal row 1 by overwriting A1 when it doesn't match; with two
different schemas that silently mislabeled every row from the other writer.)

Two deliberate differences from the Streamlit version:

  * The worksheet handle is a per-container singleton (was ``@st.cache_resource``).
  * Feedback is appended as its OWN row with the `feedback`/`comment` cells
    filled, rather than patched into the answer's row in place. Streamlit can
    patch because it holds turn->row_index in session state; the stateless HTTP
    contract returns only {answer, sig}, so there is no row reference to come
    back with. The columns still line up — analysis joins the feedback row to
    its answer row on (turn, question, answer) offline.

The log holds real user traffic and nothing else: ``_open_worksheet`` returns
None inside a test runner, so an automated run appends nothing, anywhere.

Every failure is logged and swallowed — logging must never break an answer.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone

from api import obs, settings

_GS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Shared with the Streamlit app — both append real user traffic here.
_SHEET_TAB = "latest version"

# Identical to app.py's _SHEET_HEADERS — keep the two in sync or the A1
# self-heal below will flip-flop between schemas again.
_HEADERS = [
    "timestamp", "turn", "model", "question", "answer",
    "use_cache", "input_tokens", "output_tokens",
    "cache_creation_tokens", "cache_read_tokens", "cached_input_tokens",
    "feedback", "comment", "initial_response",
]

# Matches app.py's _RATING_DISPLAY so the feedback column reads the same
# whichever frontend produced the row.
_RATING_DISPLAY = {"up": "👍", "down": "👎"}

_lock = threading.Lock()
_worksheet = None
_init_attempted = False


def _is_test_run() -> bool:
    """True when this process is a test runner rather than a served request.

    Checked against the import graph: importing ``api.main`` normally pulls in
    neither ``pytest`` nor ``unittest``, so neither marker can fire in
    production. Detection lives here rather than in the test file because the
    test-side guard silently stopped working once — which is how automated rows
    reached the live log to begin with.
    """
    return bool(
        os.getenv("PYTEST_CURRENT_TEST")     # set by pytest for each test
        or "pytest" in sys.modules
        or "unittest" in sys.modules
    )


def _service_account_info():
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _open_worksheet():
    """Open (creating if needed) the log tab and guarantee row 1 is the current
    header. Returns None when Sheets is not configured, or when this is a test
    run — the log is for real user traffic only."""
    if _is_test_run():
        return None

    sheet_id = (os.getenv("GOOGLE_SHEET_ID") or "").strip()
    sa_info = _service_account_info()
    if not sheet_id or sa_info is None:
        return None

    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(sa_info, scopes=_GS_SCOPES)
    client = gspread.authorize(creds)
    try:
        client.set_timeout(20)  # gspread has no default request timeout
    except Exception:
        pass

    spreadsheet = client.open_by_key(sheet_id)
    try:
        ws = spreadsheet.worksheet(_SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(
            title=_SHEET_TAB, rows=20000, cols=len(_HEADERS)
        )

    header = ws.row_values(1)
    if not header:
        ws.append_row(_HEADERS, value_input_option="RAW")
    elif header != _HEADERS:
        ws.update([_HEADERS], "A1", value_input_option="RAW")
    return ws


def _get_worksheet():
    """Per-container singleton. Initialization is attempted once; a failure
    disables Sheets for the life of the container rather than retrying on every
    request."""
    global _worksheet, _init_attempted
    with _lock:
        if _worksheet is None and not _init_attempted:
            _init_attempted = True
            try:
                _worksheet = _open_worksheet()
            except Exception:
                obs.exception("sheets_init_failed")
                _worksheet = None
        return _worksheet


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cell(value):
    return "" if value is None else value


def log_answer(session, turn, question, answer, initial_response, usage) -> None:
    """Append one answer row. `session` is used only for error context — the
    shared schema has no session column."""
    ws = _get_worksheet()
    if ws is None:
        return
    usage = usage or {}
    # Store the rejected first draft ONLY when the guardrail actually changed the
    # answer — mirrors the Streamlit app and minimizes retained draft text.
    initial = "" if initial_response == answer else (initial_response or "")
    row = [
        _now(), _cell(turn), settings.MODEL, question or "", answer or "",
        "True",  # use_cache: main.py always calls get_answer with use_cache=True
        _cell(usage.get("input_tokens")),
        _cell(usage.get("output_tokens")),
        _cell(usage.get("cache_creation_input_tokens")),
        _cell(usage.get("cache_read_input_tokens")),
        _cell(usage.get("cached_input_tokens")),
        "", "",  # feedback, comment — carried by the feedback row instead
        initial,
    ]
    try:
        ws.append_row(row, value_input_option="RAW")
    except Exception:
        obs.exception("sheets_append_failed", kind="answer", session=session)


def log_feedback(session, turn, rating, note, question, answer) -> None:
    """Append one feedback row in the same 14 columns. The answer-only cells
    (use_cache, token counts, initial_response) stay blank; question/answer are
    echoed so the row identifies which turn it rates."""
    ws = _get_worksheet()
    if ws is None:
        return
    row = [
        _now(), _cell(turn), settings.MODEL, question or "", answer or "",
        "", "", "", "", "", "",  # use_cache + the five token columns
        _RATING_DISPLAY.get(rating or "", ""), note or "",
        "",  # initial_response
    ]
    try:
        ws.append_row(row, value_input_option="RAW")
    except Exception:
        obs.exception("sheets_append_failed", kind="feedback", session=session)
