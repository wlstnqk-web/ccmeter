"""Cost per percent split by (model, effort)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pytest

from ccmeter import effort as eff


@dataclass
class _E:
    ts: str
    model: str
    effort: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_create: int = 0


def _events() -> list[_E]:
    return [
        _E("2026-09-01T00:00:10Z", "claude-opus-5", "high", output_tokens=1_000_000),
        _E("2026-09-01T00:00:20Z", "claude-opus-5", "low", output_tokens=1_000_000),
        _E("2026-09-01T00:00:30Z", "claude-sonnet-5", "high", output_tokens=1_000_000),
        _E("2026-09-01T01:00:00Z", "claude-opus-5", "high", output_tokens=1_000_000),
    ]


def _conn(ticks: list[tuple[str, float]], bucket: str = "five_hour") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE usage_samples (id INTEGER PRIMARY KEY, bucket TEXT, ts TEXT, utilization REAL, account_id TEXT)"
    )
    for i, (ts, util) in enumerate(ticks, start=1):
        conn.execute(
            "INSERT INTO usage_samples (id, bucket, ts, utilization, account_id) VALUES (?,?,?,?,?)",
            (i, bucket, ts, util, None),
        )
    conn.commit()
    return conn


def test_splits_one_model_across_effort_levels():
    conn = _conn([("2026-09-01T00:00:00Z", 0.0), ("2026-09-01T00:01:00Z", 2.0)])
    out = eff.calibrate_by_effort("five_hour", _events(), conn)

    assert ("claude-opus-5", "high") in out
    assert ("claude-opus-5", "low") in out
    # Same model, same tokens, different effort -> the percent is split, not doubled.
    assert out[("claude-opus-5", "high")]["pct"] == pytest.approx(out[("claude-opus-5", "low")]["pct"])


def test_cheaper_model_costs_less_per_percent():
    """Control: if pricing were not applied per model these would be equal."""
    conn = _conn([("2026-09-01T00:00:00Z", 0.0), ("2026-09-01T00:01:00Z", 2.0)])
    out = eff.calibrate_by_effort("five_hour", _events(), conn)
    assert out[("claude-sonnet-5", "high")]["usd"] < out[("claude-opus-5", "high")]["usd"]


def test_percent_is_conserved_within_a_tick():
    conn = _conn([("2026-09-01T00:00:00Z", 0.0), ("2026-09-01T00:01:00Z", 2.0)])
    out = eff.calibrate_by_effort("five_hour", _events(), conn)
    assert sum(v["pct"] for v in out.values()) == pytest.approx(2.0)


def test_events_outside_the_tick_are_not_counted():
    """Control: the 01:00 event must not leak into a 00:00-00:01 window."""
    conn = _conn([("2026-09-01T00:00:00Z", 0.0), ("2026-09-01T00:01:00Z", 2.0)])
    out = eff.calibrate_by_effort("five_hour", _events(), conn)
    assert out[("claude-opus-5", "high")]["requests"] == 1


def test_blank_effort_gets_its_own_cell():
    """An unlabelled request is not evidence of any particular effort level."""
    evs = [_E("2026-09-01T00:00:10Z", "claude-opus-5", "", output_tokens=1_000_000)]
    conn = _conn([("2026-09-01T00:00:00Z", 0.0), ("2026-09-01T00:01:00Z", 1.0)])
    out = eff.calibrate_by_effort("five_hour", evs, conn)
    assert list(out) == [("claude-opus-5", "unknown")]


def test_no_ticks_means_no_rows_not_zero_rows():
    """'not measured' and 'measured as nothing' are different answers."""
    conn = _conn([("2026-09-01T00:00:00Z", 5.0)])
    assert eff.calibrate_by_effort("five_hour", _events(), conn) == {}


def test_zero_cost_tick_is_skipped_rather_than_dividing():
    evs = [_E("2026-09-01T00:00:10Z", "claude-opus-5", "high")]  # all token counts zero
    conn = _conn([("2026-09-01T00:00:00Z", 0.0), ("2026-09-01T00:01:00Z", 1.0)])
    assert eff.calibrate_by_effort("five_hour", evs, conn) == {}


def test_per_percent_values_are_derived_from_the_totals():
    conn = _conn([("2026-09-01T00:00:00Z", 0.0), ("2026-09-01T00:01:00Z", 2.0)])
    out = eff.calibrate_by_effort("five_hour", _events(), conn)
    cell = out[("claude-opus-5", "high")]
    assert cell["usd_per_pct"] == pytest.approx(cell["usd"] / cell["pct"])
    assert cell["tokens_per_pct"] == pytest.approx(cell["tokens"] / cell["pct"])


def test_model_scoped_bucket_filters_by_prefix():
    conn = _conn([("2026-09-01T00:00:00Z", 0.0), ("2026-09-01T00:01:00Z", 2.0)], bucket="seven_day_opus")
    out = eff.calibrate_by_effort("seven_day_opus", _events(), conn)
    # The fixture has to actually exercise the path: assert it produced rows at all
    # before reading anything into which rows those are.
    assert out, "no ticks matched -- the fixture, not the filter, would be under test"
    assert {k[0] for k in out} == {"claude-opus-5"}


def test_zero_cost_cell_beside_a_paying_one_is_dropped_not_divided():
    """A cell can reach the totals with pct == 0 and still be real.

    A request with no recorded tokens costs nothing, so it takes a zero share of
    the tick. Without the guard that cell divides by zero; with it the cell is
    absent, which is the honest answer -- it consumed no measurable percent.
    """
    evs = [
        _E("2026-09-01T00:00:10Z", "claude-opus-5", "high", output_tokens=1_000_000),
        _E("2026-09-01T00:00:20Z", "claude-opus-5", "low"),  # zero tokens
    ]
    conn = _conn([("2026-09-01T00:00:00Z", 0.0), ("2026-09-01T00:01:00Z", 1.0)])
    out = eff.calibrate_by_effort("five_hour", evs, conn)
    assert ("claude-opus-5", "high") in out
    assert ("claude-opus-5", "low") not in out
