"""Outbound notifications for the Stop/notify hook.

The hook runs on the machine that owns the tmux pane. An orchestrator elsewhere
(a cloud session, another agent) cannot see that machine, so `--notify <spec>`
pushes every event out to somewhere the orchestrator *can* read.

The pushed line is fixed:

    2026-09-23T04:11:07Z manta/wp-j WP2 DONE next=prompts/wp3.md

`<ISO8601Z> <session>/<pane_title> <STAGE> <DONE|STOPPED|TURN> next=<prompt>|PAUSE|none`.
Whitespace inside a session name or pane title is collapsed to `_` so the line stays
parseable with `awk`/`cut`; an unknown field is `-`.

Spec forms (`--notify` may be given more than once):

    file:/abs/path.log          append locally (tests, a local orchestrator)
    gist:<id>[:<filename>]      append to a gist file via `gh gist edit`
    repo:<owner/name>[:<path>]  append to a file in a repo via `gh api` (one commit)
    url:https://...             POST the whole event as JSON (see events.py)

Every delivery gets one retry and a 3-second timeout. A failure is reported on
stderr only: the hook still exits 0 and the next stage still goes into the pane.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TIMEOUT = 3.0
ATTEMPTS = 2  # the first try plus one retry
RETRY_PAUSE = 0.5
DEFAULT_FILENAME = "tmux-agents.log"


class NotifyError(RuntimeError):
    """A notification could not be delivered."""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _field(value: str | None) -> str:
    cleaned = re.sub(r"\s+", "_", (value or "").strip())
    return cleaned or "-"


def format_line(event: dict[str, Any], ts: str | None = None) -> str:
    """The one fixed line every notification carries, built from a normalized event."""
    where = f"{_field(event.get('session'))}/{_field(event.get('pane'))}"
    stamp = ts or event.get("ts") or now_iso()
    stage = _field(event.get("stage"))
    status = _field(event.get("status"))
    nxt = event.get("next") or "none"
    return f"{stamp} {where} {stage} {status} next={_field(nxt)}"


# --- transports ---------------------------------------------------------------


def _gh(args: list[str]) -> tuple[int, str, str]:
    """Run `gh` once, returning (returncode, stdout, stderr).

    Kept as one function so tests can monkeypatch it instead of calling GitHub.
    """
    try:
        done = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise NotifyError("gh not found on PATH") from exc
    except subprocess.SubprocessError as exc:
        raise NotifyError(f"gh {args[0]} timed out") from exc
    return done.returncode, done.stdout or "", done.stderr or ""


def _appended(old: str, line: str) -> str:
    head = old.rstrip("\n")
    return f"{head}\n{line}\n" if head else f"{line}\n"


def _to_file(path: str, line: str, _event: dict[str, Any]) -> None:
    target = Path(path).expanduser()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        raise NotifyError(f"{target}: {exc}") from exc


def _to_gist(spec: str, line: str, _event: dict[str, Any]) -> None:
    gist_id, _, filename = spec.partition(":")
    gist_id = gist_id.strip()
    filename = filename.strip() or DEFAULT_FILENAME
    if not gist_id:
        raise NotifyError("gist: needs a gist id")
    code, out, _ = _gh(["gist", "view", gist_id, "--filename", filename, "--raw"])
    old = out if code == 0 else ""
    # `gh gist edit <id> --filename <name> <source-file>` replaces the file wholesale,
    # so the append happens here: read, add our line, write the whole thing back.
    source = Path(tempfile.mkdtemp(prefix="tmux-agents-")) / filename.replace("/", "_")
    try:
        source.write_text(_appended(old, line), encoding="utf-8")
        code, _, err = _gh(["gist", "edit", gist_id, "--filename", filename, str(source)])
    except OSError as exc:
        raise NotifyError(f"gist {gist_id}: {exc}") from exc
    finally:
        shutil.rmtree(source.parent, ignore_errors=True)
    if code != 0:
        raise NotifyError(f"gh gist edit {gist_id}: {err.strip() or code}")


def _to_repo(spec: str, line: str, event: dict[str, Any]) -> None:
    repo, _, path = spec.partition(":")
    repo = repo.strip()
    path = path.strip() or DEFAULT_FILENAME
    if repo.count("/") != 1 or not all(repo.split("/")):
        raise NotifyError("repo: needs <owner>/<name>")
    endpoint = f"/repos/{repo}/contents/{path}"
    code, out, _ = _gh(["api", endpoint])
    old, sha = "", ""
    if code == 0:
        try:
            body = json.loads(out)
            sha = str(body.get("sha") or "")
            old = base64.b64decode(body.get("content") or "").decode("utf-8", "replace")
        except (ValueError, TypeError) as exc:
            raise NotifyError(f"{endpoint}: unreadable response") from exc
    content = base64.b64encode(_appended(old, line).encode("utf-8")).decode("ascii")
    args = [
        "api",
        "-X",
        "PUT",
        endpoint,
        "-f",
        f"message=tmux-agents: {_field(event.get('stage'))} {_field(event.get('status'))}",
        "-f",
        f"content={content}",
    ]
    if sha:
        args += ["-f", f"sha={sha}"]
    code, _, err = _gh(args)
    if code != 0:
        raise NotifyError(f"gh api PUT {endpoint}: {err.strip() or code}")


def _to_url(url: str, _line: str, event: dict[str, Any]) -> None:
    if not url.lower().startswith(("http://", "https://")):
        raise NotifyError("url: needs http:// or https://")
    request = urllib.request.Request(
        url,
        data=json.dumps(event, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "tmux-agents-hook"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            if response.status >= 400:
                raise NotifyError(f"{url}: HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        raise NotifyError(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise NotifyError(f"{url}: {exc}") from exc


TRANSPORTS = {"file": _to_file, "gist": _to_gist, "repo": _to_repo, "url": _to_url}


# --- entry point --------------------------------------------------------------


def deliver(spec: str, line: str, event: dict[str, Any]) -> None:
    """Send one notification, retrying once. Raises NotifyError if both tries fail."""
    kind, sep, rest = spec.partition(":")
    kind = kind.strip().lower()
    if not sep or kind not in TRANSPORTS:
        raise NotifyError(f"unknown notify spec {spec!r} (want file:/gist:/repo:/url:)")
    if not rest.strip():
        raise NotifyError(f"empty target in notify spec {spec!r}")
    last: NotifyError | None = None
    for attempt in range(ATTEMPTS):
        try:
            TRANSPORTS[kind](rest.strip(), line, event)
            return
        except NotifyError as exc:
            last = exc
            if attempt + 1 < ATTEMPTS:
                time.sleep(RETRY_PAUSE)
    raise last if last else NotifyError(spec)


def notify_all(specs: list[str], line: str, event: dict[str, Any], stderr=None) -> int:
    """Deliver to every spec. Returns how many succeeded; never raises."""
    out = stderr if stderr is not None else sys.stderr
    delivered = 0
    for spec in specs:
        try:
            deliver(spec, line, event)
            delivered += 1
        except Exception as exc:  # noqa: BLE001 - a notification must never break the hook
            print(f"tmux-agents hook: notify {spec} failed: {exc}", file=out)
    return delivered


__all__ = ["NotifyError", "deliver", "format_line", "notify_all", "now_iso"]
