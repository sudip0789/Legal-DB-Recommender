"""DynamoDB-backed rate limiting.

Reuses the pure sliding-window logic in ``core.rate_limit`` (which takes an
injected clock and a caller-owned timestamp list) against a persistent store,
across two windows — per session and per source-IP /24 — plus a global daily
cap. All three run before any LLM call, so a blocked request costs nothing.

Fails OPEN on any infrastructure error: reserved Lambda concurrency and the
provider spend cap are the hard stops, so a DynamoDB blip must not take the tool
down. Each fail-open emits a ``ratelimit_unavailable`` metric to alarm on —
"rate limiting silently off" is exactly the state an attacker wants.

Storage note: boto3 returns DynamoDB numbers as decimal.Decimal, and
``check_and_record`` does float arithmetic (now - 3600.0). Timestamps are
therefore converted to float on read and back to Decimal(int(...)) on write.
"""

from __future__ import annotations

import time
from decimal import Decimal

from api import obs, settings
from core.rate_limit import check_and_record

_WINDOW_TTL = 7200        # 2h — comfortably past the 1h sliding window
_DAILY_TTL = 172800       # 48h

_table = None


def _get_table():
    global _table
    if _table is None:
        import boto3  # lazy: local dev without AWS never imports it

        _table = boto3.resource("dynamodb").Table(settings.RATE_TABLE)
    return _table


def ip_bucket(ip: str) -> str:
    """Aggregate to /24 for IPv4 so trivial address rotation within a subnet
    doesn't multiply an attacker's budget; pass other forms through, truncated."""
    ip = (ip or "").strip()
    if not ip:
        return "unknown"
    parts = ip.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return ".".join(parts[:3]) + ".0/24"
    return ip[:45]


def _window(pk: str, now: float, per_min: int, per_hour: int):
    """Sliding-window check with optimistic-concurrency write. Returns
    (allowed, limit)."""
    table = _get_table()
    conflict = table.meta.client.exceptions.ConditionalCheckFailedException
    for _ in range(2):
        item = table.get_item(Key={"pk": pk}, ConsistentRead=True).get("Item") or {}
        timestamps = [float(x) for x in item.get("ts", [])]
        ver = int(item.get("ver", 0))

        allowed, limit = check_and_record(timestamps, now, per_min, per_hour)
        if not allowed:
            return False, limit
        try:
            table.put_item(
                Item={
                    "pk": pk,
                    "ts": [Decimal(int(t)) for t in timestamps],
                    "ver": ver + 1,
                    "expires_at": int(now) + _WINDOW_TTL,
                },
                ConditionExpression="attribute_not_exists(pk) OR ver = :v",
                ExpressionAttributeValues={":v": ver},
            )
            return True, None
        except conflict:
            continue  # a concurrent request updated the same key; re-read once
    return True, None  # lost the race twice — allow; the daily cap backstops


def _daily(now: float) -> bool:
    if settings.DAILY_CAP <= 0:
        return True
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    table = _get_table()
    try:
        table.update_item(
            Key={"pk": f"day#{day}"},
            UpdateExpression=(
                "SET expires_at = if_not_exists(expires_at, :exp) ADD n :one"
            ),
            ConditionExpression="attribute_not_exists(n) OR n < :cap",
            ExpressionAttributeValues={
                ":one": Decimal(1),
                ":cap": Decimal(settings.DAILY_CAP),
                ":exp": Decimal(int(now) + _DAILY_TTL),
            },
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def check(session_id: str, client_ip: str, now: float | None = None):
    """Returns (allowed, limit) where limit is "minute" | "hour" | None.

    Fails open on infrastructure error. The global daily cap is surfaced as
    "hour" so the frontend's existing hourly-limit copy applies.
    """
    if not settings.RATE_TABLE:
        return True, None  # rate limiting disabled (local development)
    now = time.time() if now is None else now
    try:
        allowed, limit = _window(
            f"sess#{session_id}", now, settings.PER_MIN, settings.PER_HOUR
        )
        if not allowed:
            return False, limit
        allowed, limit = _window(
            f"ip#{ip_bucket(client_ip)}", now, settings.IP_PER_MIN, settings.IP_PER_HOUR
        )
        if not allowed:
            return False, limit
        if not _daily(now):
            return False, "hour"
        return True, None
    except Exception:
        obs.metric("ratelimit_unavailable")
        return True, None
