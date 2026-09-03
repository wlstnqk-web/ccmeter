"""Daemon health snapshot must survive being written more than once.

Regression net for the bug that silently capped collection at the first poll cycle:
`Path.rename` raises FileExistsError on Windows when the destination already exists,
so `_write_health` only ever succeeded on a fresh machine. Measured 2026-09-03 — a
freshly started daemon exited after ~6s with WinError 183 and the sample count had
been frozen at 3 since the first cycle.

The important case is therefore the *second* write, not the first. A test that only
writes once passes against the broken code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccmeter import poll


@pytest.fixture
def health_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "health.json"
    monkeypatch.setattr(poll, "HEALTH_FILE", target)
    return target


def _write(ok: bool = True, failures: int = 0) -> None:
    poll._write_health(ok=ok, interval=120, consecutive_failures=failures, recent_errors=[])


def test_writes_snapshot(health_file: Path) -> None:
    _write()
    assert json.loads(health_file.read_text())["ok"] is True


def test_overwrites_existing_snapshot(health_file: Path) -> None:
    """The regression. Broken code raises FileExistsError here on Windows."""
    _write(ok=True)
    _write(ok=False, failures=2)

    written = json.loads(health_file.read_text())
    assert written["ok"] is False
    assert written["consecutive_failures"] == 2


def test_survives_many_cycles(health_file: Path) -> None:
    """A daemon writes this every interval — one overwrite is not the real load."""
    for i in range(10):
        _write(ok=i % 2 == 0, failures=i)
    assert json.loads(health_file.read_text())["consecutive_failures"] == 9


def test_leaves_no_temp_file_behind(health_file: Path) -> None:
    """A stranded .tmp is how the failure announces itself on disk.

    The broken code wrote the temp file and then failed to move it, so the directory
    kept a `health.tmp` that never got consumed. Asserting its absence catches a
    'fix' that swallows the error instead of completing the move.
    """
    _write()
    _write()
    assert not health_file.with_suffix(".tmp").exists()


def test_snapshot_is_replaced_not_appended(health_file: Path) -> None:
    """Negative control — the file must hold one snapshot, not accumulate.

    Without this, a 'fix' that opens the target in append mode would pass every
    assertion above while corrupting the file into invalid JSON over time.
    """
    _write(ok=True)
    _write(ok=False)
    text = health_file.read_text()
    json.loads(text)  # raises if a second object was appended
    assert text.count('"ok"') == 1
