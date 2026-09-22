"""Server configuration, read once from environment variables."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Everything the server reads from the environment.

    TMUX_AGENTS_TMUX          tmux binary (default: `tmux` on PATH)
    TMUX_AGENTS_SOCKET        tmux socket name (`tmux -L`), empty = default server
    TMUX_AGENTS_SESSION_PREFIX
                              only sessions whose name starts with this prefix are
                              visible/controllable. Empty = every session.
    TMUX_AGENTS_MAX_LINES     hard cap for pane_read (default 2000)
    TMUX_AGENTS_MAX_WAIT      hard cap in seconds for pane_wait / pane_send(wait_for)
                              (default 50). Keep it below the tool timeout of the
                              client that calls this server (remote bridges: 60s).
    TMUX_AGENTS_MAX_CHARS     hard cap for the text pane_read returns (default 12000);
                              longer output is truncated from the front.
    TMUX_AGENTS_ALLOW_KILL    allow session_kill / pane_kill (default true)
    TMUX_AGENTS_OPEN_COMMAND  shell command run after agents_launch to show the
                              session in a terminal app. `{session}` is replaced.
                              Example (macOS, Ghostty):
                              open -na Ghostty.app --args -e tmux attach -t {session}
    TMUX_AGENTS_REDACT        redact token-looking strings in pane output (default true)
    """

    tmux: str = "tmux"
    socket: str = ""
    session_prefix: str = ""
    max_lines: int = 2000
    max_wait: int = 50
    max_chars: int = 12000
    allow_kill: bool = True
    open_command: str = ""
    redact: bool = True
    extra_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            tmux=os.environ.get("TMUX_AGENTS_TMUX", "tmux"),
            socket=os.environ.get("TMUX_AGENTS_SOCKET", ""),
            session_prefix=os.environ.get("TMUX_AGENTS_SESSION_PREFIX", ""),
            max_lines=max(50, _env_int("TMUX_AGENTS_MAX_LINES", 2000)),
            max_wait=max(5, _env_int("TMUX_AGENTS_MAX_WAIT", 50)),
            max_chars=max(500, _env_int("TMUX_AGENTS_MAX_CHARS", 12000)),
            allow_kill=_env_bool("TMUX_AGENTS_ALLOW_KILL", True),
            open_command=os.environ.get("TMUX_AGENTS_OPEN_COMMAND", ""),
            redact=_env_bool("TMUX_AGENTS_REDACT", True),
        )

    def base_args(self) -> list[str]:
        args = [self.tmux]
        if self.socket:
            args += ["-L", self.socket]
        return args

    def describe(self) -> dict[str, object]:
        return {
            "tmux": self.tmux,
            "socket": self.socket or "(default)",
            "session_prefix": self.session_prefix or "(any)",
            "max_lines": self.max_lines,
            "max_wait": self.max_wait,
            "max_chars": self.max_chars,
            "allow_kill": self.allow_kill,
            "open_command": self.open_command or "(none)",
            "redact": self.redact,
        }

    def open_command_argv(self, session: str) -> list[str]:
        if not self.open_command:
            return []
        return [part.replace("{session}", session) for part in shlex.split(self.open_command)]


__all__ = ["Settings", "_env_list"]
