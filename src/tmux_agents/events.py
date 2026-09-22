"""The local event log: one JSON object per line, appended by the Stop/notify hook.

Every turn that ends under the hook — a Claude Code `Stop`, a Codex
`agent-turn-complete` — becomes one line in `~/.tmux-agents/events.jsonl`
(override with `--events` or `TMUX_AGENTS_EVENTS`):

    {"ts": "2026-09-23T04:11:07Z", "agent": "claude", "session": "manta",
     "pane": "wp-j", "stage": "WP2", "status": "DONE", "message": "...",
     "handoff": "~/woon-work/.../handoff.md", "next": "prompts/wp3.md"}

`status` is `DONE` or `STOPPED` when the turn ended with a `<STAGE> DONE|STOPPED`
marker, and `TURN` when it just ended. `stage`, `handoff` and `next` are null when
there is nothing to put there.

An orchestrator on this machine follows the file with `tail -f`; one somewhere else
reads it through the `events_read` MCP tool, or has the hook push each line out with
`--notify` (see `notify.py`).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_PATH = "~/.tmux-agents/events.jsonl"
MESSAGE_LIMIT = 8192  # bytes of the last assistant message we keep


def default_path() -> Path:
    return Path(os.environ.get("TMUX_AGENTS_EVENTS") or DEFAULT_PATH).expanduser()


def clip(message: str, limit: int = MESSAGE_LIMIT) -> str:
    """Keep the tail of a message: the marker and the handoff path live at the end."""
    data = message.encode("utf-8")
    if len(data) <= limit:
        return message
    return data[-limit:].decode("utf-8", "ignore")


def to_line(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False, sort_keys=False)


def append(event: dict[str, Any], path: Path | None = None) -> Path:
    """Append one event. Raises OSError if the file cannot be written."""
    target = path or default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(to_line(event) + "\n")
    return target


def read(
    since_line: int = 0,
    path: Path | None = None,
    limit: int = 200,
) -> tuple[list[dict[str, Any]], int]:
    """Return (events after `since_line`, next_since).

    `next_since` is the number of lines in the file, so passing it back returns
    only what was appended since. Unparseable lines are skipped but still counted.
    """
    target = path or default_path()
    try:
        raw = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], max(0, since_line)
    start = max(0, min(since_line, len(raw)))
    out: list[dict[str, Any]] = []
    for line in raw[start:]:
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out[-limit:] if limit and len(out) > limit else out, len(raw)


__all__ = ["DEFAULT_PATH", "MESSAGE_LIMIT", "append", "clip", "default_path", "read", "to_line"]
