"""`tmux-agents hook`: a Claude Code / Codex *Stop* hook that chains pipeline stages.

An agent ends every stage with a marker line such as `WP1 DONE` or `WP1 STOPPED`.
When the agent's turn ends, the hook reads its transcript, finds the last marker,
looks the stage up in a pipeline file and types the next stage's prompt into the
same tmux pane. `STOPPED`, an unknown stage, the end of the pipeline or a `PAUSE`
entry all stop the chain and leave a line in the status log for a human (or an
orchestrating model) to pick up.

Pipeline file (one stage per line, `#` comments allowed):

    # <marker>|<prompt file, relative to the pipeline file>
    WP1|prompts/wp2.md
    WP2|prompts/wp3.md
    WP3|PAUSE

Install once, globally, in `~/.claude/settings.json` (or per repo in `.claude/settings.json`):

    {"hooks": {"Stop": [{"hooks": [{"type": "command",
      "command": "tmux-agents hook --pipeline /abs/path/pipeline.txt", "timeout": 15}]}]}}
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

# Strict form: the marker alone on a line. Lenient form: the marker anywhere in the
# last few lines (agents often write "the last line is WP2 DONE" instead of the line).
MARKER = re.compile(r"^\s*([A-Z][A-Z0-9_-]*) (DONE|STOPPED)\s*$", re.M)
MARKER_LENIENT = re.compile(r"\b([A-Z][A-Z0-9_-]{1,40}) (DONE|STOPPED)\b")


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


def run(argv: list[str] | None = None, stdin: str | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tmux-agents hook")
    ap.add_argument("--pipeline", required=True, help="pipeline file (stage|next per line)")
    ap.add_argument("--log", help="status log (default: <pipeline>.log)")
    ap.add_argument("--pane", default=os.environ.get("TMUX_PANE", ""), help="tmux pane id")
    ap.add_argument("--delay", type=float, default=3.0, help="seconds before typing next prompt")
    ap.add_argument("--dry-run", action="store_true", help="decide, log, but do not type")
    args = ap.parse_args(argv)

    pipeline = Path(args.pipeline).expanduser()
    if not pipeline.is_file():
        return 0
    log = Path(args.log) if args.log else pipeline.with_suffix(".log")

    payload = stdin if stdin is not None else sys.stdin.read()
    try:
        transcript = Path(json.loads(payload).get("transcript_path", ""))
    except ValueError:
        return 0
    marker = find_marker(last_assistant_text(transcript)) if transcript.is_file() else None
    if marker is None:
        return 0
    stage, state = marker
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp} pane={args.pane or '-'} {stage} {state}\n")
        if state != "DONE":
            fh.write(f"{stamp}   -> stopped; needs a human or orchestrator\n")
            return 0
        target = next_stage(pipeline, stage)
        if target is None:
            fh.write(f"{stamp}   -> end of pipeline\n")
            return 0
        if target.upper() == "PAUSE":
            fh.write(f"{stamp}   -> PAUSE (decision needed)\n")
            return 0
        prompt = (pipeline.parent / target).expanduser()
        if not prompt.is_file():
            fh.write(f"{stamp}   -> prompt not found: {prompt}\n")
            return 0
        if not args.pane:
            fh.write(f"{stamp}   -> no tmux pane; not typing {target}\n")
            return 0
        fh.write(f"{stamp}   -> next: {target}\n")
    if not args.dry_run:
        send_prompt(args.pane, prompt.read_text(encoding="utf-8").strip(), args.delay)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
