"""Per-model weekly limits live in `limits[]`, a list — they must still be recorded.

Measured 2026-11-20: the usage response carries a model's own weekly cap (Fable) only as
a `weekly_scoped` entry in `limits[]`. `record_samples` walked dict values and skipped
the list, so the number arrived every cycle and was thrown away. The only way left to
read it was a second caller on the same endpoint, and that pushed the 429 count up
until the second caller had to stop.

These tests pin both halves: scoped entries become `scoped:<name>` buckets with the same
change-only recording as every other bucket, and nothing else in the list does.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ccmeter import poll
from ccmeter.report import model_filter_for


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE usage_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            bucket TEXT NOT NULL, utilization REAL NOT NULL, resets_at TEXT, tier TEXT, raw JSON, account_id TEXT)"""
    )
    return conn


def _payload(fable: float | None = 41.0) -> dict[str, Any]:
    limits: list[Any] = [
        {"kind": "weekly_all", "percent": 13},
        {"kind": "five_hour", "percent": 2},
        {"kind": "weekly_scoped", "percent": 5, "scope": {"model": {}}},  # no name — skipped
        {"kind": "weekly_scoped", "percent": "x", "scope": {"model": {"display_name": "Opus 5"}}},  # not a number
        "not-a-dict",
    ]
    if fable is not None:
        limits.append(
            {
                "kind": "weekly_scoped",
                "percent": fable,
                "resets_at": "2026-11-27T05:00:00+00:00",
                "scope": {"model": {"display_name": "Fable 5"}},
            }
        )
    return {"seven_day": {"utilization": 13.0, "resets_at": "2026-11-27T05:00:00+00:00"}, "limits": limits}


def _rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return conn.execute("SELECT bucket, utilization, resets_at, raw FROM usage_samples ORDER BY id").fetchall()


def test_scoped_entry_becomes_a_bucket() -> None:
    conn = _conn()
    seen = poll.record_samples(_payload(), {}, conn)
    rows = {b: (u, r, json.loads(raw)) for b, u, r, raw in _rows(conn)}
    assert set(rows) == {"seven_day", "scoped:Fable 5"}
    util, resets, raw = rows["scoped:Fable 5"]
    assert util == 41.0 and resets == "2026-11-27T05:00:00+00:00"
    assert raw["scope"]["model"]["display_name"] == "Fable 5"  # readers can match the model by name in raw too
    assert seen["scoped:Fable 5"] == 41.0


def test_scoped_bucket_records_only_on_change() -> None:
    conn = _conn()
    seen = poll.record_samples(_payload(41.0), {}, conn)
    seen = poll.record_samples(_payload(41.0), seen, conn)
    assert [b for b, *_ in _rows(conn)].count("scoped:Fable 5") == 1
    poll.record_samples(_payload(42.0), seen, conn)
    assert [u for b, u, *_ in _rows(conn) if b == "scoped:Fable 5"] == [41.0, 42.0]


def test_response_without_limits_is_unchanged() -> None:
    """The old shape (no `limits`, or `limits` not a list) records exactly what it did before."""
    for data in (
        {"seven_day": {"utilization": 9.0}},
        {"seven_day": {"utilization": 9.0}, "limits": {"kind": "weekly_scoped"}},
    ):
        conn = _conn()
        poll.record_samples(data, {}, conn)
        assert [b for b, *_ in _rows(conn)] == ["seven_day"]
        assert poll.scoped_buckets(data) == {}


def test_only_named_numeric_weekly_scoped_entries() -> None:
    assert list(poll.scoped_buckets(_payload())) == ["scoped:Fable 5"]
    assert poll.scoped_buckets(_payload(fable=None)) == {}


def test_report_filters_scoped_bucket_by_model() -> None:
    assert model_filter_for("scoped:Fable 5") == "claude-fable"
    assert model_filter_for("seven_day_opus") == "claude-opus"
    assert model_filter_for("seven_day") is None
