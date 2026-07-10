"""
Per-session rate limiting.

No Streamlit imports — like core/finder.py, this module is portable and
unit-testable in isolation. The caller owns the timestamp list (stored in
st.session_state) and supplies the current time, so tests can inject a clock.

Algorithm: a sliding-window log. We keep the times of accepted requests, prune
anything older than the hour window, then count how many fall inside the minute
and hour windows.
"""

from __future__ import annotations

_MINUTE = 60.0
_HOUR = 3600.0


def check_and_record(timestamps, now, per_minute: int = 10, per_hour: int = 100):
    """Decide whether a request at ``now`` is allowed and, if so, record it.

    ``timestamps`` is a mutable list of prior accepted request times (seconds,
    from a monotonic clock), mutated in place: stale entries are pruned and, when
    the request is allowed, ``now`` is appended.

    Returns ``(allowed, limit)`` where ``limit`` is ``None`` when allowed, else
    ``"minute"`` or ``"hour"`` — used to pick the message shown to the user.
    """
    # Drop entries that have aged out of the (larger) hour window.
    while timestamps and timestamps[0] <= now - _HOUR:
        timestamps.pop(0)

    in_minute = [t for t in timestamps if t > now - _MINUTE]
    if len(in_minute) >= per_minute:
        return False, "minute"
    if len(timestamps) >= per_hour:  # remaining list == requests within the hour
        return False, "hour"

    timestamps.append(now)
    return True, None
