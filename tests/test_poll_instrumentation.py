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
