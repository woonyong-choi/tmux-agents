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
    # or hand the baton to a conductor pane instead of carrying on in this one:
    WP3|@pane:orchestrator: WP3 done. check ~/wp/WP-3/handoff.md
    WP4|PAUSE

Claude Code — `~/.claude/settings.json` (or a repo's `.claude/settings.json`):

    {"hooks": {"Stop": [{"hooks": [{"type": "command",
      "command": "tmux-agents hook --pipeline /abs/path/pipeline.txt", "timeout": 15}]}]}}

Codex CLI — `~/.codex/config.toml`:

    notify = ["tmux-agents", "hook", "--agent", "codex"]

Claude Code puts `{"last_assistant_message": ..., "transcript_path": ...}` on
stdin; Codex passes an `agent-turn-complete` JSON object as the last argument
(stdin also works). Either way only the last assistant message matters, and both
agents hand it over directly — the transcript is only read when they do not.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import events, notify

# Strict form: the marker alone on a line. Lenient form: the marker anywhere in the
# last few lines (agents often write "the last line is WP2 DONE" instead of the line).
MARKER = re.compile(r"^\s*([A-Z][A-Z0-9_-]*) (DONE|STOPPED)\s*$", re.M)
MARKER_LENIENT = re.compile(r"\b([A-Z][A-Z0-9_-]{1,40}) (DONE|STOPPED)\b")

# `@pane:<title|id>: <sentence>` — type the sentence into *another* pane (the
# conductor), instead of typing the next prompt into this one.
PANE_TARGET = re.compile(r"^@pane:(?P<pane>.+?):\s+(?P<text>.+)$", re.S)

# A handoff path as agents write it: `~/woon-work/WP-J/handoff.md`, `docs/handoff.md`.
HANDOFF = re.compile(r"(?:~|\.{0,2}/)?[\w.@+/-]*handoff\.md", re.I)

# The hook can start before the agent has written the turn's last line to the
# transcript. Re-read for this long, this often, while the turn still looks unfinished.
FLUSH_WAIT = 1.5
FLUSH_POLL = 0.15


def _texts(obj: dict[str, Any]) -> list[str]:
    """Every non-empty text block of one transcript entry, in order."""
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, str):
        return [content] if content.strip() else []
    if not isinstance(content, list):
        return []
    out = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            out.append(text)
    return out


def _has_tool_use(obj: dict[str, Any]) -> bool:
    content = (obj.get("message") or {}).get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(p, dict) and p.get("type") == "tool_use" for p in content)


def read_turn(transcript: Path) -> tuple[str, bool]:
    """(what the assistant said in the turn that just ended, whether that turn is over).

    "This turn" is everything said after the last thing the user or a tool said: a
    final report that follows a tool call is a *separate* entry from the one-liner
    before that call ("Now the commit."), so taking the file's last text block alone
    reports the wrong message. All of the turn's text blocks are joined instead.

    The turn is over only once an assistant entry with no tool call in it has been
    written. Anything else means the agent has not finished flushing, and the caller
    should look again in a moment. Sidechain entries belong to a subagent, not to
    this pane's turn, so they are skipped. The fallback, for a transcript that never
    settles, is the last text block anywhere in it.
    """
    try:
        lines = transcript.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return "", False
    turn: list[str] = []
    fallback = ""
    pending = True
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("isSidechain"):
            continue
        kind = obj.get("type")
        if kind == "user":  # a prompt or a tool result: whatever came before is done
            turn, pending = [], True
        elif kind == "assistant":
            found = _texts(obj)
            if found:
                turn.extend(found)
                fallback = found[-1]
            pending = _has_tool_use(obj)
    if turn and not pending:
        return "\n\n".join(turn), True
    return ("\n\n".join(turn) if turn else fallback), False


def last_assistant_text(transcript: Path, wait: float = FLUSH_WAIT) -> str:
    """The turn's final message, waiting briefly if it is still being written."""
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        text, settled = read_turn(transcript)
        if settled or time.monotonic() >= deadline:
            return text
        time.sleep(FLUSH_POLL)


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


def resolve_pane(spec: str) -> str | None:
    """A pane id for `%3`, `sess:0.1` or a pane title (exact, then case-insensitive).

    The hook runs inside the pane it was triggered from, so plain `tmux` — which
    follows `$TMUX` — is already pointed at the right server.
    """
    spec = spec.strip()
    if not spec:
        return None
    try:
        done = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_title}",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    rows = [line.split("\t") for line in (done.stdout or "").splitlines() if "\t" in line]
    for pane_id, target, _title in rows:
        if spec in (pane_id, target):
            return pane_id
    for pane_id, _target, title in rows:
        if title == spec:
            return pane_id
    for pane_id, _target, title in rows:
        if spec.lower() in title.lower():
            return pane_id
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


def _payload_message(payload: dict[str, Any]) -> str:
    """The final message the agent handed us, under either spelling of the key."""
    for key in ("last_assistant_message", "last-assistant-message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def message_of(agent: str, payload: dict[str, Any]) -> str:
    """The last assistant message: straight from the payload, else from the transcript.

    Claude Code's Stop hook carries `last_assistant_message` (2.1.280 checked) as
    well as the transcript path. The field wins: it is the message the agent just
    printed, whereas the transcript's last line may not be on disk yet.
    """
    if agent == "codex":
        kind = payload.get("type")
        if kind and kind != "agent-turn-complete":
            return ""
        return _payload_message(payload)
    direct = _payload_message(payload)
    if direct:
        return direct
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


def plan(
    pipeline: Path | None, target: str, own_pane: str
) -> tuple[str | None, str | None, str | None, str]:
    """Turn a pipeline target into (pane to type into, text, event `next`, log line).

    Three shapes: `PAUSE`, `@pane:<title|id>: <sentence>` (type the sentence into
    *that* pane — the conductor), or a prompt file to type into our own pane.
    """
    if target.upper() == "PAUSE":
        return None, None, "PAUSE", "PAUSE (decision needed)"
    hit = PANE_TARGET.match(target.strip())
    if hit:
        spec, text = hit.group("pane").strip(), hit.group("text").strip()
        dest = resolve_pane(spec)
        if dest is None:
            return None, None, None, f"no pane matches {spec!r}; not typing"
        return dest, text, f"@pane:{spec}", f"next: @pane:{spec} ({dest})"
    if pipeline is None:
        return None, None, None, f"no pipeline directory for {target}"
    prompt = (pipeline.parent / target).expanduser()
    if not prompt.is_file():
        return None, None, None, f"prompt not found: {prompt}"
    if not own_pane:
        return None, None, None, f"no tmux pane; not typing {target}"
    return own_pane, prompt.read_text(encoding="utf-8").strip(), target, f"next: {target}"


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
    dest, text, decision = None, None, ""
    if state == "DONE" and target:
        dest, text, event["next"], decision = plan(pipeline, target, args.pane)
    elif state == "STOPPED":
        decision = "stopped; needs a human or orchestrator"
    elif state == "DONE":
        decision = "end of pipeline" if pipeline else "no pipeline; event only"

    # The pipeline log stays a decision log: a plain turn with no marker adds nothing.
    if stage and (pipeline is not None or args.log):
        _write_log(args, pipeline, stage, state, decision)

    # Dispatch first: a sink that is slow or down must never hold up the next stage.
    if dest and text and not args.dry_run:
        send_prompt(dest, text, args.delay)

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
