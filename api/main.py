"""FastAPI entry point for the RCLL AI-Assisted Search backend.

Request/response contract (matches the frontend's documented shape):

  POST  (any path)   {action: "answer", sessionId, model, history:[{role,content,sig?}]}
    -> 200  {answer, sig}
    -> 429  {error, limit: "minute"|"hour"}
    -> 4xx  {error}

  POST  (any path)   {action: "feedback", sessionId, turn, rating, note, question, answer}
    -> 200  {ok: true}

  GET   /healthz      -> {ok: true, model}     (readiness probe / warmer; no I/O)

Never returned to the client: the rejected `initial_response` draft, `usage`
token counts, or any internal guardrail detail.
"""

# bootstrap MUST run before any `core` import: core.finder builds its provider
# client at import time, so the keys have to be in os.environ first.
from api import bootstrap  # noqa: F401  (import for side effect; keep first)

import time  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

from api import obs, ratelimit, settings, sheets, tokens  # noqa: E402
from core.finder import get_answer, _SAFE_FALLBACK  # noqa: E402

app = FastAPI(
    title="RCLL AI-Assisted Search",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# CORS is configured HERE ONLY — never also on the Function URL, or the browser
# sees two Access-Control-Allow-Origin headers and rejects the response.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=False,  # no cookies anywhere in this design
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Client-Id"],
    max_age=86400,
)

_RATE_MSG = {
    "minute": "You're sending questions too quickly. Please pause a moment and try again.",
    "hour": "You've reached the limit for now. Please try again later.",
}
_UNAVAILABLE = (
    "The assistant is temporarily unavailable. Please try again, or contact the "
    "reference librarians at reference@law.stanford.edu or 650-725-0800."
)


def _err(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message, **extra})


@app.get("/healthz")
async def healthz():
    # Readiness probe and warm-up target. Must touch NO external service, or
    # cold starts serialize behind it and the warmer stops being free.
    return {"ok": True, "model": settings.MODEL}


@app.post("/{_path:path}")
async def handle(request: Request, _path: str = ""):
    # One endpoint, dispatched on `action`, matching the frontend contract. A
    # catch-all path keeps it working whatever URL the frontend is pointed at.
    try:
        body = await request.json()
    except Exception:
        return _err(400, "Invalid request.")
    if not isinstance(body, dict):
        return _err(400, "Invalid request.")

    action = body.get("action")
    if action == "answer":
        return await _answer(request, body)
    if action == "feedback":
        return await _feedback(request, body)
    return _err(400, "Unknown action.")


async def _answer(request: Request, body: dict) -> JSONResponse:
    history = body.get("history")

    ok, message, status = _validate_history(history)
    if not ok:
        return _err(status, message)

    # Reject forged assistant turns before spending anything.
    if not _signatures_ok(history):
        return _err(400, "This conversation could not be verified. Please start a new search.")

    session = _session_id(body, request)
    allowed, limit = await run_in_threadpool(
        ratelimit.check, session, _client_ip(request)
    )
    if not allowed:
        return _err(429, _RATE_MSG.get(limit, _RATE_MSG["hour"]), limit=limit)

    # Pass only role/content to the engine — strip `sig` and any other keys.
    clean = [{"role": m["role"], "content": m["content"]} for m in history]
    question = clean[-1]["content"]
    turn = sum(1 for m in history if m.get("role") == "assistant")

    started = time.monotonic()
    try:
        answer, _sent, usage, initial = await run_in_threadpool(
            get_answer, clean, True, settings.MODEL,
            deadline=started + settings.BUDGET_S,
        )
    except Exception:
        obs.exception("get_answer_failed", session=session)
        return _err(502, _UNAVAILABLE)

    # Best-effort internal log — must never block or fail the response.
    try:
        await run_in_threadpool(
            sheets.log_answer, session, turn, question, answer, initial, usage
        )
    except Exception:
        obs.exception("sheets_answer_failed", session=session)

    usage = usage or {}
    obs.event(
        "answer",
        session=session,
        turn=turn,
        model=settings.MODEL,
        q_len=len(question),
        q_hash=obs.qhash(question),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cached_input_tokens=usage.get("cached_input_tokens"),
        truncated=usage.get("truncated"),
        guardrail_changed=(initial != answer),
        fallback=(answer == _SAFE_FALLBACK),
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )

    # Only {answer, sig} — never initial_response or usage.
    return JSONResponse({"answer": answer, "sig": tokens.sign(answer)})


async def _feedback(request: Request, body: dict) -> JSONResponse:
    rating = body.get("rating")
    if rating not in ("up", "down"):
        return _err(400, "Invalid feedback.")

    session = _session_id(body, request)
    allowed, limit = await run_in_threadpool(
        ratelimit.check, session, _client_ip(request)
    )
    if not allowed:
        return _err(429, _RATE_MSG.get(limit, _RATE_MSG["hour"]), limit=limit)

    turn = body.get("turn")
    note = str(body.get("note") or "")[:2000]
    question = str(body.get("question") or "")[:8000]
    answer = str(body.get("answer") or "")[:20000]

    try:
        await run_in_threadpool(
            sheets.log_feedback, session, turn, rating, note, question, answer
        )
    except Exception:
        obs.exception("sheets_feedback_failed", session=session)

    obs.event(
        "feedback",
        session=session,
        turn=turn if isinstance(turn, int) else None,
        rating=rating,
        has_note=bool(note),
    )
    return JSONResponse({"ok": True})


# --- validation helpers ------------------------------------------------------


def _validate_history(history):
    """Cheap structural + size validation, before any LLM call. Returns
    (ok, message, status)."""
    if not isinstance(history, list) or not history:
        return False, "Please enter a question.", 400
    if len(history) > settings.MAX_MESSAGES:
        return False, "This conversation is too long. Please start a new search.", 400

    total = 0
    assistant_turns = 0
    for msg in history:
        if not isinstance(msg, dict):
            return False, "Malformed request.", 400
        role = msg.get("role")
        content = msg.get("content")
        if role not in ("user", "assistant"):
            return False, "Malformed request.", 400
        if not isinstance(content, str):
            return False, "Malformed request.", 400
        if len(content) > settings.MAX_MSG_CHARS:
            return False, "Your message is too long. Please shorten it.", 413
        total += len(content)
        if role == "assistant":
            assistant_turns += 1

    if total > settings.MAX_HISTORY_CHARS:
        return False, "This conversation is too long. Please start a new search.", 413
    if history[-1].get("role") != "user":
        return False, "Malformed request.", 400
    if assistant_turns >= settings.MAX_TURNS:
        return (
            False,
            "You've reached the limit for this search. Please start a new search.",
            409,
        )
    return True, None, 200


def _signatures_ok(history) -> bool:
    """Every assistant message must carry a valid HMAC signature we issued."""
    for msg in history:
        if msg.get("role") == "assistant":
            if not tokens.verify(msg.get("content", ""), msg.get("sig")):
                return False
    return True


def _session_id(body: dict, request: Request) -> str:
    sid = body.get("sessionId") or request.headers.get("x-client-id") or ""
    sid = str(sid).strip()[:64]
    return sid or "anon"


def _client_ip(request: Request) -> str:
    # Behind the Lambda Function URL the source IP arrives in X-Forwarded-For
    # (verify empirically on first deploy — log the header set once). Fall back
    # to the socket peer for local development.
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or ""
