"""`tmux-agents hook`: one turn-end hook for Claude Code and Codex.

Both agents can tell an outside program that a turn ended; they just say it
differently. This command takes either shape, turns it into one event
(`events.py`), appends that event to `~/.tmux-agents/events.jsonl`, pushes it to
every `--notify` sink, and — if a pipeline file says what comes next — types the
next stage's prompt into the same tmux pane.

An agent ends a stage with a marker line such as `WP1 DONE` or `WP1 STOPPED`.
`STOPPED`, an unknown stage, the end of the pipeline or a `PAUSE` entry all stop
the chain and leave the decision in the log and in the event stream, for a human
(or an orchestrating model) to pick up. A turn that carries no marker still
produces an event, with `status: "TURN"`.

Pipeline file (one stage per line, `#` comments allowed):

    # <marker>|<prompt file, relative to the pipeline file>
    WP1|prompts/wp2.md
    WP2|prompts/wp3.md
    WP3|PAUSE

Claude Code — `~/.claude/settings.json` (or a repo's `.claude/settings.json`):

    {"hooks": {"Stop": [{"hooks": [{"type": "command",
      "command": "tmux-agents hook --pipeline /abs/path/pipeline.txt", "timeout": 15}]}]}}

Codex CLI — `~/.codex/config.toml`:

    notify = ["tmux-agents", "hook", "--agent", "codex"]

Claude Code puts `{"transcript_path": ...}` on stdin; Codex passes an
`agent-turn-complete` JSON object as the last argument (stdin also works). Either
way only the last assistant message matters.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import events, notify

# Strict form: the marker alone on a line. Lenient form: the marker anywhere in the
# last few lines (agents often write "the last line is WP2 DONE" instead of the line).
MARKER = re.compile(r"^\s*([A-Z][A-Z0-9_-]*) (DONE|STOPPED)\s*$", re.M)
MARKER_LENIENT = re.compile(r"\b([A-Z][A-Z0-9_-]{1,40}) (DONE|STOPPED)\b")

# A handoff path as agents write it: `~/woon-work/WP-J/handoff.md`, `docs/handoff.md`.
HANDOFF = re.compile(r"(?:~|\.{0,2}/)?[\w.@+/-]*handoff\.md", re.I)


def last_assistant_text(transcript: Path) -> str:
    last = ""
    try:
        lines = transcript.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") != "assistant":
            continue
        content = (obj.get("message") or {}).get("content") or []
        if isinstance(content, str):
            last = content
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                last = part.get("text", "")
    return last


def find_marker(text: str) -> tuple[str, str] | None:
    found = MARKER.findall(text)
    if found:
        return found[-1]
    tail = "\n".join(text.strip().splitlines()[-6:])
    found = MARKER_LENIENT.findall(tail)
    return found[-1] if found else None


def find_handoff(text: str) -> str | None:
    """The last handoff.md path mentioned in the message, if any."""
    found = [m.group(0) for m in HANDOFF.finditer(text or "")]
    return found[-1] if found else None


def next_stage(pipeline: Path, stage: str) -> str | None:
    """Return what follows `stage` (a prompt path or PAUSE), or None if it is not listed."""
    for raw in pipeline.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "|" not in line:
            continue
        name, target = (s.strip() for s in line.split("|", 1))
        if name == stage:
            return target
    return None


def pane_info(pane: str) -> tuple[str, str]:
    """(session name, pane title) for a pane id, or ('-', '-') outside tmux."""
    if not pane:
        return "-", "-"
    try:
        done = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}\t#{pane_title}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return "-", "-"
    if done.returncode != 0:
        return "-", "-"
    parts = (done.stdout or "").strip().split("\t")
    session = parts[0].strip() if parts else ""
    title = parts[1].strip() if len(parts) > 1 else ""
    return session or "-", title or "-"


def read_payload(raw: str | None) -> dict[str, Any]:
    """Parse one JSON object handed to the hook, or {} if it is not one."""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def message_of(agent: str, payload: dict[str, Any]) -> str:
    """The last assistant message: from the transcript (Claude) or the payload (Codex)."""
    if agent == "codex":
        kind = payload.get("type")
        if kind and kind != "agent-turn-complete":
            return ""
        for key in ("last-assistant-message", "last_assistant_message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""
    transcript = Path(str(payload.get("transcript_path") or "")).expanduser()
    return last_assistant_text(transcript) if transcript.is_file() else ""


def build_event(agent: str, message: str, session: str, pane: str) -> dict[str, Any]:
    """One event shape, whatever the agent was. `next` is filled in later."""
    marker = find_marker(message)
    return {
        "ts": notify.now_iso(),
        "agent": agent,
        "session": session,
        "pane": pane,
        "stage": marker[0] if marker else None,
        "status": marker[1] if marker else "TURN",
        "message": events.clip(message),
        "handoff": find_handoff(message),
        "next": None,
    }


def send_prompt(pane: str, text: str, delay: float = 3.0) -> None:
    """Type the prompt into the pane after the current turn has fully ended."""
    script = (
        f"sleep {delay}; tmux send-keys -t {pane!r} -l -- {_sh(text)}; "
        f"sleep 0.3; tmux send-keys -t {pane!r} Enter"
    )
    subprocess.Popen(
        ["sh", "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _sh(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tmux-agents hook")
    ap.add_argument("--pipeline", help="pipeline file (stage|next per line)")
    ap.add_argument("--agent", default="claude", choices=["claude", "codex"], help="who calls us")
    ap.add_argument("--log", help="status log (default: <pipeline>.log)")
    ap.add_argument("--pane", default=os.environ.get("TMUX_PANE", ""), help="tmux pane id")
    ap.add_argument("--delay", type=float, default=3.0, help="seconds before typing next prompt")
    ap.add_argument(
        "--notify",
        action="append",
        default=[],
        metavar="SPEC",
        help="file:/path | gist:<id>[:<file>] | repo:<owner/name>[:<path>] | url:https://... "
        "(repeatable)",
    )
    ap.add_argument("--events", help=f"event log (default: {events.DEFAULT_PATH})")
    ap.add_argument("--no-events", action="store_true", help="do not write the local event log")
    ap.add_argument("--dry-run", action="store_true", help="decide, log, but do not type")
    ap.add_argument("payload", nargs="?", help="event JSON (Codex passes it as an argument)")
    return ap


def run(argv: list[str] | None = None, stdin: str | None = None) -> int:
    args = _parser().parse_args(argv)

    pipeline = Path(args.pipeline).expanduser() if args.pipeline else None
    if pipeline is not None and not pipeline.is_file():
        pipeline = None

    # Codex passes the event as an argument, Claude Code writes it to stdin.
    payload = read_payload(args.payload)
    if not payload:
        payload = read_payload(stdin if stdin is not None else _read_stdin())
    message = message_of(args.agent, payload)
    if not message.strip():
        return 0

    session, pane_title = pane_info(args.pane)
    event = build_event(args.agent, message, session, pane_title)
    stage, state = event["stage"], event["status"]

    target = next_stage(pipeline, stage) if (pipeline and stage) else None
    prompt: Path | None = None
    decision = ""
    if state == "DONE" and target:
        if target.upper() == "PAUSE":
            event["next"] = "PAUSE"
            decision = "PAUSE (decision needed)"
        else:
            candidate = (pipeline.parent / target).expanduser()
            if not candidate.is_file():
                decision = f"prompt not found: {candidate}"
            elif not args.pane:
                decision = f"no tmux pane; not typing {target}"
            else:
                event["next"] = target
                prompt = candidate
                decision = f"next: {target}"
    elif state == "STOPPED":
        decision = "stopped; needs a human or orchestrator"
    elif state == "DONE":
        decision = "end of pipeline" if pipeline else "no pipeline; event only"

    # The pipeline log stays a decision log: a plain turn with no marker adds nothing.
    if stage and (pipeline is not None or args.log):
        _write_log(args, pipeline, stage, state, decision)

    # Dispatch first: a sink that is slow or down must never hold up the next stage.
    if prompt is not None and not args.dry_run:
        send_prompt(args.pane, prompt.read_text(encoding="utf-8").strip(), args.delay)

    _sink(args, event)
    return 0


def _read_stdin() -> str:
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    try:
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""


def _write_log(args, pipeline: Path | None, stage: str, state: str, decision: str) -> None:
    log = Path(args.log).expanduser() if args.log else pipeline.with_suffix(".log")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} pane={args.pane or '-'} {stage} {state}\n")
            if decision:
                fh.write(f"{stamp}   -> {decision}\n")
    except OSError as exc:
        print(f"tmux-agents hook: cannot write {log}: {exc}", file=sys.stderr)


def _sink(args, event: dict[str, Any]) -> None:
    """Always the local event log, then every --notify spec. Failures only warn."""
    if not args.no_events:
        try:
            events.append(event, Path(args.events).expanduser() if args.events else None)
        except OSError as exc:
            print(f"tmux-agents hook: cannot write the event log: {exc}", file=sys.stderr)
    if args.notify:
        notify.notify_all(args.notify, notify.format_line(event), event)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
