"""
Streamlit shell — eval phase only.
All UI, password gate, feedback collection, and JSONL logging live here.
The core/ package has no Streamlit dependency and is portable to the
production Stanford server unchanged.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import gspread
import streamlit as st
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials as _GCreds

load_dotenv()

from core.finder import (  # noqa: E402 — must follow load_dotenv()
    DEFAULT_MODEL,
    MODELS,
    get_answer,
)

# ---------------------------------------------------------------------------
# Logging — local JSONL + optional Google Sheets
# ---------------------------------------------------------------------------
_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOG_DIR / "qa_log.jsonl"

_GS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Worksheet (tab) within the spreadsheet to log into. A dedicated tab keeps the
# new one-row-per-turn schema separate from any older data on other tabs.
_SHEET_TAB = "latest version"

# One row per answered turn. Feedback is no longer a separate row: the `feedback`
# and `comment` cells on the answer's own row are filled in later, in place,
# when the user rates the answer.
_SHEET_HEADERS = [
    "timestamp", "turn", "model", "question", "answer",
    "use_cache", "input_tokens", "output_tokens",
    "cache_creation_tokens", "cache_read_tokens", "cached_input_tokens",
    "feedback", "comment",
]
_FEEDBACK_COL = _SHEET_HEADERS.index("feedback") + 1  # 1-based for gspread
_COMMENT_COL = _SHEET_HEADERS.index("comment") + 1
_RATING_DISPLAY = {"up": "👍", "down": "👎"}


def _resolve_service_account() -> dict | None:
    """Service-account info from (in order): GOOGLE_SERVICE_ACCOUNT_JSON env var,
    then st.secrets["gcp_service_account"]. Returns None if neither is present."""
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if creds_json:
        try:
            return json.loads(creds_json)
        except json.JSONDecodeError:
            pass
    try:
        return dict(st.secrets["gcp_service_account"])
    except (KeyError, FileNotFoundError):
        return None


def _open_log_worksheet(sa_info: dict, sheet_id: str):
    """Open (creating if needed) the log tab and guarantee row 1 holds the
    current headers. Pure gspread — no Streamlit — so test scripts can reuse it.

    Header handling is idempotent: written when the tab is empty, and overwritten
    in place if an older/short header is found (so a stale schema self-heals
    instead of silently misaligning new rows)."""
    creds = _GCreds.from_service_account_info(sa_info, scopes=_GS_SCOPES)
    spreadsheet = gspread.authorize(creds).open_by_key(sheet_id)
    try:
        ws = spreadsheet.worksheet(_SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(
            title=_SHEET_TAB, rows=1000, cols=len(_SHEET_HEADERS)
        )

    header = ws.row_values(1)
    if not header:
        ws.append_row(_SHEET_HEADERS, value_input_option="USER_ENTERED")
    elif header != _SHEET_HEADERS:
        ws.update([_SHEET_HEADERS], "A1", value_input_option="USER_ENTERED")
    return ws


@st.cache_resource
def _get_sheet():
    """Streamlit-cached worksheet handle for the configured Google Sheet tab,
    or None if logging isn't configured / setup fails.
    Requires GOOGLE_SHEET_ID plus service-account creds (see
    _resolve_service_account)."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
    if not sheet_id:
        return None
    sa_info = _resolve_service_account()
    if sa_info is None:
        return None
    try:
        return _open_log_worksheet(sa_info, sheet_id)
    except Exception as exc:
        import sys
        print(f"[sheets] setup failed: {exc}", file=sys.stderr)
        return None


def _sheet_row(record: dict) -> list:
    """Build the answer row. `feedback`/`comment` start blank and are filled in
    place when the user rates (see _append_log)."""
    u = record.get("usage", {})
    return [
        record.get("timestamp", ""),
        record.get("turn", ""),
        record.get("model", ""),
        record.get("question", ""),
        record.get("answer", ""),
        str(record.get("use_cache", "")),
        u.get("input_tokens", ""),
        u.get("output_tokens", ""),
        u.get("cache_creation_input_tokens", ""),
        u.get("cache_read_input_tokens", ""),
        u.get("cached_input_tokens", ""),
        "",  # feedback
        "",  # comment
    ]


def _appended_row_number(result: dict) -> int | None:
    """Extract the 1-based row index from a gspread append_row() response
    (e.g. ``'latest version'!A5:N5`` -> 5)."""
    try:
        updated_range = result["updates"]["updatedRange"]
    except (KeyError, TypeError):
        return None
    match = re.search(r"![A-Z]+(\d+)", updated_range)
    return int(match.group(1)) if match else None


def _append_log(record: dict) -> None:
    # Always write the raw event to local JSONL (append-only, lossless — old
    # lines simply lack newer keys, which analysis treats as blank).
    with open(_LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # Best-effort Google Sheets sync — failures go to stderr, never to users.
    try:
        ws = _get_sheet()
        if ws is None:
            return

        if record.get("type") == "answer":
            result = ws.append_row(
                _sheet_row(record), value_input_option="USER_ENTERED"
            )
            row = _appended_row_number(result)
            if row is not None:
                st.session_state.sheet_row_by_turn[record.get("turn")] = row

        elif record.get("type") == "feedback":
            # Fill the feedback/comment cells on this turn's existing row.
            row = st.session_state.sheet_row_by_turn.get(record.get("turn"))
            if row is not None:
                ws.update_cell(
                    row, _FEEDBACK_COL,
                    _RATING_DISPLAY.get(record.get("rating", ""), ""),
                )
                ws.update_cell(row, _COMMENT_COL, record.get("note", ""))
    except Exception as exc:
        import sys
        print(f"[sheets] sync failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Session-state helpers
# ---------------------------------------------------------------------------
def _init_state() -> None:
    defaults: dict = {
        "authenticated": False,
        "history": [],       # list of {role, content} dicts
        "turns": 0,          # number of answered questions
        "feedback": {},      # turn_idx -> {rating, note, submitted}
        "awaiting_note": None,  # turn_idx currently awaiting a 👎 note, or None
        "model": DEFAULT_MODEL,  # pinned for the duration of one search
        "sheet_row_by_turn": {},  # turn_idx -> sheet row number, for feedback
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value



def _reset() -> None:
    st.session_state.history = []
    st.session_state.turns = 0
    st.session_state.feedback = {}
    st.session_state.awaiting_note = None
    # Clear row tracking so turn 0 of the next search doesn't update the
    # previous search's row.
    st.session_state.sheet_row_by_turn = {}


# ---------------------------------------------------------------------------
# Feedback widget (rendered inside each assistant chat bubble)
# ---------------------------------------------------------------------------
def _render_feedback(turn_idx: int) -> None:
    fb: dict = st.session_state.feedback.get(turn_idx, {})

    if fb.get("submitted"):
        if fb["rating"] == "up":
            st.caption("👍 Thanks for the feedback!")
        else:
            note_text = f": {fb['note']}" if fb.get("note") else ""
            st.caption(f"👎 Feedback recorded{note_text}")
        return

    # Show 👍 / 👎 buttons side-by-side.
    col1, col2, _ = st.columns([1, 1, 10])
    with col1:
        if st.button("👍", key=f"up_{turn_idx}"):
            st.session_state.feedback[turn_idx] = {
                "rating": "up", "note": "", "submitted": True
            }
            _append_log({
                "type": "feedback",
                "turn": turn_idx,
                "rating": "up",
                "note": "",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            st.rerun()
    with col2:
        if st.button("👎", key=f"down_{turn_idx}"):
            st.session_state.feedback[turn_idx] = {
                "rating": "down", "note": "", "submitted": False
            }
            st.session_state.awaiting_note = turn_idx
            st.rerun()

    # If this is the turn waiting for a 👎 improvement note, show input.
    if st.session_state.awaiting_note == turn_idx:
        note = st.text_area(
            "How could this answer be better? (optional)",
            key=f"note_{turn_idx}",
            height=80,
        )
        if st.button("Submit feedback", key=f"submit_note_{turn_idx}"):
            st.session_state.feedback[turn_idx] = {
                "rating": "down", "note": note, "submitted": True
            }
            st.session_state.awaiting_note = None
            _append_log({
                "type": "feedback",
                "turn": turn_idx,
                "rating": "down",
                "note": note,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            st.rerun()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(
        page_title="SLS Database Finder",
        page_icon="⚖️",
        layout="centered",
    )
    _init_state()

    # ------------------------------------------------------------------
    # Password gate
    # ------------------------------------------------------------------
    if not st.session_state.authenticated:
        st.title("Stanford Law Library Database Finder")
        with st.form("login_form"):
            password = st.text_input("Access password", type="password")
            submitted = st.form_submit_button("Enter")
        if submitted:
            correct = os.getenv("APP_PASSWORD", "")
            if password == correct:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Incorrect password.")
        return

    # ------------------------------------------------------------------
    # Sidebar
    # ------------------------------------------------------------------
    with st.sidebar:
        labels = list(MODELS.keys())
        ids = [config.id for config in MODELS.values()]
        current_label = labels[ids.index(st.session_state.model)]
        conv_active = len(st.session_state.history) > 0

        chosen = st.selectbox(
            "Model",
            labels,
            index=ids.index(st.session_state.model),
            disabled=conv_active,
            help=(
                "Each search is pinned to one model. Start a new search to switch models."
            ),
        )
        if not conv_active:
            # Only mutate the pinned model before the conversation begins.
            st.session_state.model = MODELS[chosen].id
        else:
            st.caption(f"Pinned to **{current_label}** for this search.")

        if st.button("New search", use_container_width=True):
            _reset()
            st.rerun()

    st.title("Stanford Law Library Database Finder")
    st.caption(
        "Find the right database from the Robert Crown Law Library collection."
    )

    # ------------------------------------------------------------------
    # Render existing conversation
    # ------------------------------------------------------------------
    for i, msg in enumerate(st.session_state.history):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg["role"] == "assistant":
                _render_feedback(i // 2)

    # ------------------------------------------------------------------
    # Turn cap
    # ------------------------------------------------------------------
    if st.session_state.turns >= 20:
        st.info("Starting a fresh search keeps results sharp — click to reset")
        if st.button("Reset search"):
            _reset()
            st.rerun()
        return

    # ------------------------------------------------------------------
    # Chat input
    # ------------------------------------------------------------------
    user_input = st.chat_input("Describe your research question…")
    if not user_input:
        return

    # Flush any pending 👎 note (user moved on without submitting).
    aw = st.session_state.awaiting_note
    if aw is not None and not st.session_state.feedback.get(aw, {}).get("submitted"):
        st.session_state.feedback[aw] = {
            "rating": "down", "note": "", "submitted": True
        }
        st.session_state.awaiting_note = None
        _append_log({
            "type": "feedback",
            "turn": aw,
            "rating": "down",
            "note": "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    st.session_state.history.append({"role": "user", "content": user_input})

    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        with st.spinner("Looking up databases…"):
            answer, trimmed_sent, usage = get_answer(
                st.session_state.history,
                use_cache=True,
                model=st.session_state.model,
            )
        st.markdown(answer)

    st.session_state.history.append({"role": "assistant", "content": answer})
    st.session_state.turns += 1

    _append_log({
        "type": "answer",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "turn": st.session_state.turns - 1,
        "question": user_input,
        "trimmed_history_sent": trimmed_sent,
        "answer": answer,
        "use_cache": True,
        "usage": usage,
        "model": st.session_state.model,
        "provider": usage.get("provider", ""),
    })

    st.rerun()


if __name__ == "__main__":
    main()
