"""What the daemon writes must be enough to answer "when, and how often".

Measured 2026-09-04, on a live `poll.err` holding 93 `retry in 120s [429]` lines:

- No line carried a timestamp, so the count was known and the window was not. Whether
  those rate limits were still happening could not be answered from the file at all.
- Successes went to stdout and retries to stderr — two files that cannot be
  interleaved, so "what else was the daemon doing then" had no answer either.
- `recent_errors` keeps five entries, so a failure *rate* was never readable: five
  entries look identical whether they came from five failures or five hundred.

These tests pin the three properties that investigation needed and did not have.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ccmeter import poll

ISO_PREFIX = re.compile(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00\] ")


@pytest.fixture
def health_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "health.json"
    monkeypatch.setattr(poll, "HEALTH_FILE", target)
    return target


def test_event_carries_a_timestamp(capsys: pytest.CaptureFixture[str]) -> None:
    poll._event("retry in 120s [429]")
    out = capsys.readouterr().out
    assert ISO_PREFIX.match(out), f"no timestamp prefix: {out!r}"
    assert "retry in 120s [429]" in out


def test_event_goes_to_stdout_not_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """The whole point of the change. A failure on stderr cannot be interleaved with
    the samples on stdout, which is what made the 93 lines uncorrelatable."""
    poll._event("retry in 120s [429]")
    captured = capsys.readouterr()
    assert captured.out
    assert captured.err == "", "operational events must not split across streams"


def test_shutdown_is_on_the_timeline_too(capsys: pytest.CaptureFixture[str]) -> None:
    """A restart is where the failure counters reset, so it has to be timestamped.

    `failure_counts` is an in-memory total. Without a stamped boundary the numbers
    restart silently and a reader has no way to notice they are looking at a fresh
    window — which is the same "count without its window" failure this change exists
    to remove.
    """
    poll._handle_signal(15, None)
    out = capsys.readouterr().out
    assert ISO_PREFIX.match(out), f"shutdown left the timeline: {out!r}"
    poll._running = True  # the handler flips module state; put it back


def test_both_ends_of_the_counting_window_are_stamped(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window has two edges, and the net only covered one.

    The shutdown test above pins `shutting down`, but `stopped` and the start event
    were reachable without a timestamp — the processing was right and nothing was
    watching it. Since `failure_counts` resets per run, the *start* edge is the one a
    reader needs most: it is what `counts_since` means in the log.

    Driving one `once=True` cycle is what makes both edges observable; asserting on
    the source text would pass against a print that merely mentions the word.
    """
    monkeypatch.setattr(poll, "PIDFILE", tmp_path / "poll.pid")
    monkeypatch.setattr(poll, "HEALTH_FILE", tmp_path / "health.json")
    monkeypatch.setattr(poll, "_acquire_lock", lambda: (tmp_path / "lock").open("w"))
    monkeypatch.setattr(poll, "_rotate_logs", lambda: None)
    monkeypatch.setattr(poll, "get_credentials", lambda: _FakeCreds())
    monkeypatch.setattr(poll, "fetch_account_id", _fake_account_id)
    monkeypatch.setattr(poll, "pinned_account", _no_pin)
    monkeypatch.setattr(poll, "connect", _FakeConn)
    monkeypatch.setattr(poll, "seed_last_seen", _no_seed)
    # ★Non-empty on purpose: `if result.data:` treats {} as a failure, which would send
    #   this through the retry path and never reach the stop edge.
    ok = poll.PollResult(data={"five_hour": {"utilization": 1.0}}, status=200)

    def _one_good_poll(creds: object) -> poll.PollResult:
        return ok

    monkeypatch.setattr(poll, "fetch_usage", _one_good_poll)
    monkeypatch.setattr(poll, "record_samples", _no_record)

    poll.run_poll(interval=120, once=True)

    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    started = [ln for ln in lines if "poll loop started" in ln]
    stopped = [ln for ln in lines if "stopped" in ln]

    assert started, "no start event — the counting window has no beginning in the log"
    assert ISO_PREFIX.match(started[0]), f"start edge unstamped: {started[0]!r}"
    assert stopped, "no stop event"
    assert ISO_PREFIX.match(stopped[0]), f"stop edge unstamped: {stopped[0]!r}"


def _fake_account_id(token: str) -> str:
    return "acct-0001"


def _no_pin() -> str | None:
    return None


def _no_seed(conn: object, account_id: str | None = None) -> dict[str, float]:
    return {}


def _no_record(*_args: object, **_kwargs: object) -> dict[str, float]:
    return {}


class _FakeCreds:
    access_token = "token"
    subscription_type = "max"
    rate_limit_tier = "default_claude_max_20x"


class _FakeConn:
    """Only what run_poll touches on the success path."""

    def execute(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_status_label_separates_transport_failures_from_http_ones() -> None:
    # A transport failure has no HTTP status; counting it as "0" would read as a code.
    assert poll._status_label(429) == "429"
    assert poll._status_label(401) == "401"
    assert poll._status_label(0) == "network"


def test_health_counts_failures_by_kind(health_file: Path) -> None:
    poll._write_health(
        ok=False,
        interval=120,
        consecutive_failures=2,
        recent_errors=[],
        failure_counts={"429": 93, "network": 1},
        counts_since="2026-09-04T00:00:00+00:00",
    )
    written = json.loads(health_file.read_text())
    assert written["failure_counts"] == {"429": 93, "network": 1}
    assert written["counts_since"] == "2026-09-04T00:00:00+00:00"


def test_counts_carry_a_window(health_file: Path) -> None:
    """A count without its window is not a rate.

    93 failures means nothing until you know whether that was an hour or two days.
    `counts_since` must be present even when nothing has failed yet, or the first
    reader has to guess when counting started.
    """
    poll._write_health(
        ok=True,
        interval=120,
        consecutive_failures=0,
        recent_errors=[],
        failure_counts={},
        counts_since="2026-09-04T10:00:00+00:00",
    )
    written = json.loads(health_file.read_text())
    assert written["counts_since"] == "2026-09-04T10:00:00+00:00"
    assert written["failure_counts"] == {}


def test_health_without_counts_still_writes(health_file: Path) -> None:
    """Negative control — the older call signature must not raise.

    `_write_health` is called from more than one place; a required argument here would
    have turned a logging improvement into a daemon crash.
    """
    poll._write_health(ok=True, interval=120, consecutive_failures=0, recent_errors=[])
    written = json.loads(health_file.read_text())
    assert written["failure_counts"] == {}
    assert written["counts_since"] is None


def test_counts_are_a_snapshot_copy_not_a_live_reference(health_file: Path) -> None:
    """The daemon keeps mutating its counter dict after the snapshot is written.

    If the file held a reference rather than a copy, this would be fine on disk but
    the bug class is worth pinning: a later `json.dumps` of the same object must not
    be able to change what an earlier snapshot claimed.
    """
    counts = {"429": 1}
    poll._write_health(
        ok=False,
        interval=120,
        consecutive_failures=1,
        recent_errors=[],
        failure_counts=counts,
        counts_since="2026-09-04T10:00:00+00:00",
    )
    counts["429"] = 999
    written = json.loads(health_file.read_text())
    assert written["failure_counts"] == {"429": 1}
