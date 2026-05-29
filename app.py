"""
Streamlit shell — eval phase only.
All UI, password gate, feedback collection, and JSONL logging live here.
The core/ package has no Streamlit dependency and is portable to the
production Stanford server unchanged.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import gspread
import streamlit as st
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials as _GCreds

load_dotenv()

from core.finder import get_answer  # noqa: E402 — must follow load_dotenv()

# ---------------------------------------------------------------------------
# Logging — local JSONL + optional Google Sheets
# ---------------------------------------------------------------------------
_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOG_DIR / "qa_log.jsonl"

_GS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_SHEET_HEADERS = [
    "timestamp", "type", "turn", "question", "answer",
    "use_cache", "input_tokens", "output_tokens",
    "cache_creation_tokens", "cache_read_tokens", "rating", "note",
]


@st.cache_resource
def _get_sheet():
    """
    Returns the first worksheet of the configured Google Sheet, or None.
    Cached for the lifetime of the Streamlit server process.
    Credentials are read from (in order):
      1. GOOGLE_SERVICE_ACCOUNT_JSON env var — full service-account JSON as a string.
      2. st.secrets["gcp_service_account"] — TOML table in Streamlit Cloud Secrets.
    Also requires GOOGLE_SHEET_ID env var (the spreadsheet ID from its URL).
    """
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
    if not sheet_id:
        return None

    sa_info = None

    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if creds_json:
        try:
            sa_info = json.loads(creds_json)
        except json.JSONDecodeError:
            pass

    if sa_info is None:
        try:
            sa_info = dict(st.secrets["gcp_service_account"])
        except (KeyError, FileNotFoundError):
            pass

    if sa_info is None:
        return None

    try:
        creds = _GCreds.from_service_account_info(sa_info, scopes=_GS_SCOPES)
        ws = gspread.authorize(creds).open_by_key(sheet_id).sheet1
        if not ws.row_values(1):  # write headers if the sheet is empty
            ws.append_row(_SHEET_HEADERS)
        return ws
    except Exception as exc:
        import sys
        print(f"[sheets] setup failed: {exc}", file=sys.stderr)
        return None


def _sheet_row(record: dict) -> list:
    if record.get("type") == "answer":
        u = record.get("usage", {})
        return [
            record.get("timestamp", ""),
            "answer",
            record.get("turn", ""),
            record.get("question", ""),
            record.get("answer", ""),
            str(record.get("use_cache", "")),
            u.get("input_tokens", ""),
            u.get("output_tokens", ""),
            u.get("cache_creation_input_tokens", ""),
            u.get("cache_read_input_tokens", ""),
            "",
            "",
        ]
    # feedback record
    return [
        record.get("timestamp", ""),
        "feedback",
        record.get("turn", ""),
        "", "", "", "", "", "", "",
        record.get("rating", ""),
        record.get("note", ""),
    ]


def _append_log(record: dict) -> None:
    # Always write to local JSONL.
    with open(_LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    # Best-effort Google Sheets append — failures are logged to stderr, not shown to users.
    try:
        ws = _get_sheet()
        if ws is not None:
            ws.append_row(_sheet_row(record), value_input_option="USER_ENTERED")
    except Exception as exc:
        import sys
        print(f"[sheets] append failed: {exc}", file=sys.stderr)


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
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    # Cache toggle: initialise from env var, then track via its widget key.
    if "use_cache" not in st.session_state:
        st.session_state.use_cache = (
            os.getenv("USE_CACHE", "false").lower() == "true"
        )


def _reset() -> None:
    st.session_state.history = []
    st.session_state.turns = 0
    st.session_state.feedback = {}
    st.session_state.awaiting_note = None


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
        st.header("Options")
        st.checkbox(
            "Prompt caching",
            key="use_cache",
            help=(
                "Caches the system prompt + catalog (ephemeral, 5-min TTL). "
                "Reduces latency and cost on subsequent questions. "
                "Toggle to compare usage stats in the log."
            ),
        )
        st.divider()
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
    if st.session_state.turns >= 10:
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
                st.session_state.history, st.session_state.use_cache
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
        "use_cache": st.session_state.use_cache,
        "usage": usage,
    })

    st.rerun()


if __name__ == "__main__":
    main()
