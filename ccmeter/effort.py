"""Cost per utilization percent, broken down by (model, effort).

`scan` records reasoning effort per request, but the calibration chain groups by
model alone, so "what does one percent cost at each effort level" cannot be read
off the normal report. This module answers that question **without changing the
existing report shape** -- the model-only aggregation, its JSON output and
`share.py`'s validation contract all stay exactly as they are.

Effort is a per-request property: a single session mixes several levels, so a
session-level label would average them. That is why the key is the request's own
effort, never the session's.
"""

from __future__ import annotations

import bisect
import sqlite3
from collections import defaultdict
from typing import Any

from ccmeter.report import account_clause, cost_usd, model_filter_for

_ZERO = ("input", "output", "cache_read", "cache_create")


def utilization_ticks(bucket: str, conn: sqlite3.Connection, account_id: str | None = None) -> list[Any]:
    """Consecutive usage samples where utilization went up, for one bucket."""
    af = account_clause(account_id)
    return conn.execute(
        f"""
        SELECT s1.ts as t0, s2.ts as t1,
               s2.utilization - s1.utilization as delta_pct
        FROM usage_samples s1
        JOIN usage_samples s2
            ON s2.bucket = s1.bucket
            AND {af("s2")}
            AND s2.id = (SELECT MIN(id) FROM usage_samples
                         WHERE bucket = s1.bucket AND {af()} AND id > s1.id)
        WHERE s1.bucket = ?
            AND {af("s1")}
            AND s2.utilization > s1.utilization
        ORDER BY s1.ts
        """,
        (bucket,),
    ).fetchall()


def tokens_by_model_effort(
    events: list[Any], t0: str, t1: str, model_prefix: str | None = None
) -> dict[tuple[str, str], dict[str, int]]:
    """Sum token counts per (model, effort) for events between two timestamps."""
    lo = bisect.bisect_left(events, t0, key=lambda e: e.ts)
    hi = bisect.bisect_right(events, t1, key=lambda e: e.ts)
    out: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: dict.fromkeys(_ZERO, 0) | {"count": 0})
    for i in range(lo, hi):
        e = events[i]
        model = e.model or "unknown"
        if model_prefix and not model.startswith(model_prefix):
            continue
        # Blank effort is its own bucket, not folded into another level: an
        # unlabelled request is not evidence of any particular effort.
        cell = out[(model, getattr(e, "effort", "") or "unknown")]
        cell["input"] += e.input_tokens
        cell["output"] += e.output_tokens
        cell["cache_read"] += e.cache_read
        cell["cache_create"] += e.cache_create
        cell["count"] += 1
    return dict(out)


def calibrate_by_effort(
    bucket: str,
    events: list[Any],
    conn: sqlite3.Connection,
    account_id: str | None = None,
) -> dict[tuple[str, str], dict[str, float]]:
    """Aggregate cost and tokens per percent, keyed by (model, effort).

    Percent is attributed the way the model-only path does it: within one tick the
    delta is split across cells by their share of that tick's cost. A cell that
    never appears in a tick is absent rather than zero -- "not measured" and
    "measured as nothing" are different answers.
    """
    prefix = model_filter_for(bucket)
    totals: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {"pct": 0.0, "usd": 0.0, "tokens": 0.0, "ticks": 0.0, "requests": 0.0}
    )

    for row in utilization_ticks(bucket, conn, account_id):
        cells = tokens_by_model_effort(events, row["t0"], row["t1"], prefix)
        if not cells:
            continue
        costs = {k: cost_usd(v, k[0]) for k, v in cells.items()}
        tick_cost = sum(costs.values())
        if tick_cost <= 0:
            continue
        for key, tokens in cells.items():
            share = costs[key] / tick_cost
            agg = totals[key]
            agg["pct"] += row["delta_pct"] * share
            agg["usd"] += costs[key]
            agg["tokens"] += sum(tokens[k] for k in _ZERO)
            agg["ticks"] += 1
            agg["requests"] += tokens["count"]

    out: dict[tuple[str, str], dict[str, float]] = {}
    for key, agg in totals.items():
        # Never divide by an unmeasured denominator: a cell with no attributed
        # percent has no cost-per-percent, and 0 would read as "free".
        if agg["pct"] <= 0:
            continue
        out[key] = dict(
            agg,
            usd_per_pct=agg["usd"] / agg["pct"],
            tokens_per_pct=agg["tokens"] / agg["pct"],
        )
    return out
