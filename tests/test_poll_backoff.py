"""A 429 must change the retry cadence, not repeat it.

Measured 2026-09-04 on a live `poll.err`: 93 consecutive `retry in 120s [429]` lines.
Every one the same delay, because `max(interval, 60)` at the default 120s interval
returns the interval itself. The daemon kept asking on exactly the schedule that
produced the rate limit, so the episode could only end when the server relented.

These tests pin the two halves of the contract: Retry-After wins when the server sends
one, and when it does not, the delay grows instead of standing still.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

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


def test_floor_holds_even_when_the_interval_is_tiny() -> None:
    """`interval` alone is not a floor.

    The code this replaces read `max(interval, 60)`; the 60 was worth keeping. At a 10s
    interval the doubled backoff is 20s, which is arithmetically a retreat and
    practically still hammering.
    """
    result = poll.PollResult(status=429)
    assert poll._next_delay(result, 10, backoff=10) == poll.RATE_LIMIT_MIN_DELAY
    assert poll._next_delay(result, 5, backoff=1) == poll.RATE_LIMIT_MIN_DELAY


def test_retry_after_accepts_an_http_date(capsys: pytest.CaptureFixture[str]) -> None:
    """RFC 7231 allows a date, not only seconds.

    Reading only the integer form let a date-valued header fall through to our own
    guess *silently* — the server named a time and nothing recorded that we ignored it.
    """
    now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
    assert poll.parse_retry_after("Sat, 05 Sep 2026 12:05:00 GMT", now=now) == 300
    # A date already past is 0, not negative — the caller's floor then applies.
    assert poll.parse_retry_after("Sat, 05 Sep 2026 11:00:00 GMT", now=now) == 0
    assert capsys.readouterr().out == "", "a readable header must not log a failure"


def test_a_past_date_does_not_become_an_immediate_retry() -> None:
    """A Retry-After already in the past must not turn into "ask again now".

    `parse_retry_after` returns 0 for it, and `_next_delay` reaches the header only
    through a truthiness check — so 0 falls through to our own backoff. That is the
    right behaviour and it rests entirely on 0 being falsy, which is exactly the kind
    of thing a later reader "corrects" into `is not None`. Then a stale date becomes an
    instant retry against a rate limit.
    """
    assert delay(429, backoff=480, retry_after=0) == 900, "a past date became a hammer"
    # For contrast: a real instruction of 0 seconds is not something we invent, but a
    # positive one is honoured exactly.
    assert delay(429, backoff=480, retry_after=5) == 5


def test_unreadable_retry_after_is_announced(capsys: pytest.CaptureFixture[str]) -> None:
    """"Sent one we could not read" and "sent none" are different facts.

    Falling back is the right behaviour; doing it quietly is not — that is the failure
    this whole change set exists to remove, one layer in.
    """
    assert poll.parse_retry_after("whenever you feel like it") is None
    out = capsys.readouterr().out
    assert "Retry-After" in out, "an unreadable header vanished without a trace"

    # Absent or blank is not a failure and must stay silent.
    assert poll.parse_retry_after(None) is None
    assert poll.parse_retry_after("   ") is None
    assert capsys.readouterr().out == "", "a missing header is not an error"


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
