"""Claude Code directory resolution -- both sites, together.

The projects path and the Windows credentials path share one assumption. Fixing
only the first is what produced "everything scans fine, then: no credentials
found", so both are asserted in the same file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ccmeter import auth, claude_home

ENV = ("CLAUDE_CONFIG_DIR", "CLAUDE_PROJECTS_DIR", "CLAUDE_CREDENTIALS_PATH")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):  # pyright: ignore[reportUnusedFunction]
    # Autouse: pytest calls this, so the checker cannot see the reference.
    # Without it a stray CLAUDE_* in the developer's own shell would decide the
    # result -- the tests would pass or fail on the machine, not on the code.
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def test_config_dir_defaults_to_home():
    assert claude_home.claude_config_dir() == Path.home() / ".claude"


def test_config_dir_honours_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "D:/.claude")
    assert claude_home.claude_config_dir() == Path("D:/.claude")


def test_config_dir_moves_both_paths(monkeypatch: pytest.MonkeyPatch):
    """The point of the shared resolver: one env moves the pair."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "D:/.claude")
    assert claude_home.claude_projects_dir() == Path("D:/.claude/projects")
    assert claude_home.claude_credentials_path() == Path("D:/.claude/.credentials.json")


def test_specific_overrides_beat_config_dir(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "D:/.claude")
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", "E:/sessions")
    monkeypatch.setenv("CLAUDE_CREDENTIALS_PATH", "E:/creds.json")
    assert claude_home.claude_projects_dir() == Path("E:/sessions")
    assert claude_home.claude_credentials_path() == Path("E:/creds.json")


@pytest.mark.parametrize("name", ENV)
def test_blank_env_is_ignored(monkeypatch: pytest.MonkeyPatch, name: str):
    """Path('') is the cwd -- a blank value must not silently redirect anything."""
    monkeypatch.setenv(name, "   ")
    assert claude_home.claude_config_dir() == Path.home() / ".claude"
    assert claude_home.claude_projects_dir() == Path.home() / ".claude" / "projects"
    assert claude_home.claude_credentials_path() == Path.home() / ".claude" / ".credentials.json"


def test_windows_credential_reads_the_configured_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """End to end: the reader has to follow the resolver, not a hardcoded home."""
    cred = tmp_path / ".credentials.json"
    cred.write_text(
        '{"claudeAiOauth": {"accessToken": "t", "refreshToken": "r",'
        ' "expiresAt": 1, "subscriptionType": "max", "rateLimitTier": "x"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    got = auth._windows_credential()  # pyright: ignore[reportPrivateUsage]
    assert got is not None
    assert got.access_token == "t"
    assert got.rate_limit_tier == "x"


def test_windows_credential_returns_none_when_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Control: a missing file must still be None, not an exception or a stub."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
    assert auth._windows_credential() is None  # pyright: ignore[reportPrivateUsage]


def test_windows_credential_rejects_a_file_without_oauth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Control: present-but-wrong must not read as authenticated."""
    (tmp_path / ".credentials.json").write_text('{"somethingElse": {}}', encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert auth._windows_credential() is None  # pyright: ignore[reportPrivateUsage]
