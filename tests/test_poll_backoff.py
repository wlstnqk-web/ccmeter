"""A 429 must change the retry cadence, not repeat it.

Measured 2026-09-04 on a live `poll.err`: 93 consecutive `retry in 120s [429]` lines.
Every one the same delay, because `max(interval, 60)` at the default 120s interval
returns the interval itself. The daemon kept asking on exactly the schedule that
produced the rate limit, so the episode could only end when the server relented.

These tests pin the two halves of the contract: Retry-After wins when the server sends
one, and when it does not, the delay grows instead of standing still.
"""

from __future__ import annotations

from ccmeter import poll

INTERVAL = 120


def delay(status: int, backoff: int = INTERVAL, retry_after: int | None = None) -> int:
    result = poll.PollResult(status=status, retry_after=retry_after)
    return poll._next_delay(result, INTERVAL, backoff)


def test_retry_after_wins_when_the_server_sends_one() -> None:
    """The server knows its own limit better than any guess we make."""
    assert delay(429, retry_after=42) == 42
    # Including when it asks for longer than our own ceiling — this is not our call.
    assert delay(429, retry_after=poll.RATE_LIMIT_MAX_DELAY * 3) == poll.RATE_LIMIT_MAX_DELAY * 3


def test_without_retry_after_the_delay_grows() -> None:
    """The regression. Old code returned 120 here, every time, forever."""
    first = delay(429, backoff=INTERVAL)
    assert first > INTERVAL, f"a 429 returned the same cadence that caused it: {first}"
    assert first == 240

    # And it keeps growing as the caller feeds the previous delay back in.
    second = delay(429, backoff=first)
    assert second == 480
    assert delay(429, backoff=second) == 900


def test_backoff_is_capped() -> None:
    """Unbounded growth would turn a rate limit into a silent collection outage."""
    assert delay(429, backoff=100_000) == poll.RATE_LIMIT_MAX_DELAY


def test_backoff_never_drops_below_the_normal_cadence() -> None:
    """Backing off *below* the interval would be a speed-up wearing a retreat's name.

    Reachable whenever the caller's running backoff is smaller than the interval — a
    short network-error delay immediately followed by a 429, for instance.
    """
    assert delay(429, backoff=10) == INTERVAL
    assert delay(429, backoff=1) == INTERVAL


def test_other_failures_keep_their_own_paths() -> None:
    """Negative control — this change must not reach past 429.

    Without this, widening the rate-limit branch could quietly swallow the auth and
    network cases, and every assertion above would still pass.
    """
    assert delay(401) == 30
    assert delay(403) == 30
    # network/5xx: unchanged exponential path with its own 5m ceiling
    assert delay(0, backoff=60) == 120
    assert delay(500, backoff=10_000) == 300


def test_success_returns_to_the_normal_interval() -> None:
    """A recovered poll must not inherit the backed-off delay."""
    result = poll.PollResult(data={"five_hour": {"utilization": 1.0}}, status=200)
    assert poll._next_delay(result, INTERVAL, backoff=poll.RATE_LIMIT_MAX_DELAY) == INTERVAL
