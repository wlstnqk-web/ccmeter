"""Tests for the fork's local-measurement changes.

Each test pairs the change with a control: a check that only ever passes cannot
tell "the behaviour is right" from "the check measures nothing".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ccmeter import report, scan, update

# The cache serializers and the warn-once set are module-private, but they are
# exactly the surface these behaviours live on. Alias once here rather than
# scattering suppressions through the tests.
_token_to_dict = scan._token_to_dict  # pyright: ignore[reportPrivateUsage]
_dict_to_token = scan._dict_to_token  # pyright: ignore[reportPrivateUsage]
_unpriced_warned = report._UNPRICED_WARNED  # pyright: ignore[reportPrivateUsage]

# --- JSONL root override ---------------------------------------------------
# Upstream hardcodes ~/.claude/projects. When Claude Code keeps sessions elsewhere
# the glob silently finds nothing and an empty report reads as "no usage".


def test_projects_dir_prefers_explicit_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", "D:/elsewhere/projects")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "D:/ignored")
    assert scan.claude_projects_dir() == Path("D:/elsewhere/projects")


def test_projects_dir_falls_back_to_config_dir(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CLAUDE_PROJECTS_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "D:/cfg")
    assert scan.claude_projects_dir() == Path("D:/cfg") / "projects"


def test_projects_dir_ignores_blank_override(monkeypatch: pytest.MonkeyPatch):
    """A blank env var must not resolve to Path('') -- that would scan the cwd."""
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", "   ")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert scan.claude_projects_dir() == Path.home() / ".claude" / "projects"


def test_projects_dir_defaults_to_home(monkeypatch: pytest.MonkeyPatch):
    """Control: with no env set the upstream default must survive."""
    monkeypatch.delenv("CLAUDE_PROJECTS_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert scan.claude_projects_dir() == Path.home() / ".claude" / "projects"


def test_missing_root_is_announced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """An empty result must be distinguishable from a misconfigured root."""
    monkeypatch.setattr(scan, "CLAUDE_DIR", tmp_path / "nope")
    result = scan.scan(days=1)
    assert result.events == []
    assert "no such directory" in capsys.readouterr().err


def test_root_without_sessions_is_announced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """The real failure we hit: the directory exists but holds no session files."""
    (tmp_path / "some-project").mkdir()
    monkeypatch.setattr(scan, "CLAUDE_DIR", tmp_path)
    result = scan.scan(days=1)
    assert result.events == []
    assert "no session files" in capsys.readouterr().err


# --- per-request effort ----------------------------------------------------
# Effort varies inside a single session, so it can only be tagged per request.


def _event(**kw: object) -> scan.TokenEvent:
    base: dict[str, object] = {
        "ts": "2026-09-02T00:00:00Z",
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_read": 3,
        "cache_create": 4,
        "model": "claude-opus-5",
        "session_id": "s1",
        "cc_version": "1.0",
    }
    base.update(kw)
    return scan.TokenEvent(**base)  # type: ignore[arg-type]


def test_effort_survives_the_cache_round_trip():
    restored = _dict_to_token(_token_to_dict(_event(effort="high")))
    assert restored.effort == "high"


def test_effort_round_trip_keeps_every_other_field():
    """Control: if the round trip were broken generally, the effort test proves nothing."""
    original = _event(effort="low")
    assert _dict_to_token(_token_to_dict(original)) == original


def test_old_cache_rows_without_effort_still_load():
    d = _token_to_dict(_event(effort="high"))
    del d["e"]
    assert _dict_to_token(d).effort == ""


def test_cache_version_was_bumped_for_effort():
    """Parse output changed, so stale rows must be invalidated rather than reused."""
    assert "e" in _token_to_dict(_event())
    assert scan.CACHE_VERSION >= 5


# --- unpriced models -------------------------------------------------------
# PRICING has no Claude 5 entry, so those models fall back to Opus 4.6 rates.
# Pricing is the comparison axis; a silent fallback makes two models look equal.


def test_unknown_model_warns_once(capsys: pytest.CaptureFixture[str]):
    _unpriced_warned.discard("claude-fictional-9")
    report.pricing_for("claude-fictional-9")
    first = capsys.readouterr().err
    report.pricing_for("claude-fictional-9")
    second = capsys.readouterr().err

    assert "no published rates" in first
    assert second == ""


def test_known_model_does_not_warn(capsys: pytest.CaptureFixture[str]):
    """Control: warning on everything would be the same as warning on nothing."""
    assert report.pricing_for("claude-opus-4-6") is report.PRICING["claude-opus-4-6"]
    assert capsys.readouterr().err == ""


# --- version check kill switch ---------------------------------------------


def test_version_check_can_be_disabled(monkeypatch: pytest.MonkeyPatch):
    def _boom() -> str:
        raise AssertionError("network reached despite CCMETER_NO_VERSION_CHECK")

    monkeypatch.setattr(update, "_fetch_latest", _boom)
    monkeypatch.setattr(update, "_read_cache", _boom)
    monkeypatch.setenv("CCMETER_NO_VERSION_CHECK", "1")
    assert update.check_version() is None


def test_version_check_runs_when_not_disabled(monkeypatch: pytest.MonkeyPatch):
    """Control: the switch must gate the call, not remove it."""
    calls: list[str] = []

    def _cached() -> tuple[str | None, float]:
        calls.append("read")
        return None, 0.0

    monkeypatch.setattr(update, "_read_cache", _cached)
    monkeypatch.setattr(update, "_fetch_latest", lambda: None)
    monkeypatch.delenv("CCMETER_NO_VERSION_CHECK", raising=False)
    update.check_version()
    assert calls == ["read"]


@pytest.mark.parametrize("falsy", ["", "0", "false", "False"])
def test_falsy_switch_values_do_not_disable(monkeypatch: pytest.MonkeyPatch, falsy: str):
    """An env var set to '0' means off, not on -- the usual trap."""
    calls: list[str] = []
    monkeypatch.setattr(update, "_read_cache", lambda: (calls.append("read"), (None, 0.0))[1])
    monkeypatch.setattr(update, "_fetch_latest", lambda: None)
    monkeypatch.setenv("CCMETER_NO_VERSION_CHECK", falsy)
    update.check_version()
    assert calls == ["read"]
