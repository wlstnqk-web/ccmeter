"""Where Claude Code keeps its own state.

Upstream hardcodes `~/.claude/...` in two places -- session JSONL and, on Windows,
the credentials file. Both are wrong whenever Claude Code is configured to live
elsewhere (CLAUDE_CONFIG_DIR), e.g. on another drive.

They are kept together here on purpose. The projects path was fixed first and the
credentials path was missed, which cost a full round of debugging: everything
scanned fine and then `account` said "no credentials found". Two sites of the same
assumption belong in one file so the next one cannot be fixed alone.

Note the boundary: this module resolves *Claude Code's* directories. ccmeter's own
state (`~/.ccmeter/`: db, config, health, logs) is deliberately NOT moved by
CLAUDE_CONFIG_DIR -- it is not Claude Code's data.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env(name: str) -> str:
    """Env value, treating blank as unset -- Path('') would silently mean the cwd."""
    return os.environ.get(name, "").strip()


def claude_config_dir() -> Path:
    """CLAUDE_CONFIG_DIR, else ~/.claude."""
    cfg = _env("CLAUDE_CONFIG_DIR")
    return Path(cfg) if cfg else Path.home() / ".claude"


def claude_projects_dir() -> Path:
    """CLAUDE_PROJECTS_DIR > CLAUDE_CONFIG_DIR/projects > ~/.claude/projects."""
    override = _env("CLAUDE_PROJECTS_DIR")
    return Path(override) if override else claude_config_dir() / "projects"


def claude_credentials_path() -> Path:
    """CLAUDE_CREDENTIALS_PATH > CLAUDE_CONFIG_DIR/.credentials.json > ~/.claude/...

    Windows only in practice: macOS and Linux read the OS keychain instead, which
    has no notion of a config directory.
    """
    override = _env("CLAUDE_CREDENTIALS_PATH")
    return Path(override) if override else claude_config_dir() / ".credentials.json"
