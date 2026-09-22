"""Thin, typed wrapper over the tmux CLI. No tmux state is kept in memory."""

from __future__ import annotations

import hashlib
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .redact import redact, strip_ansi
from .settings import Settings

PANE_FORMAT = "\t".join(
    [
        "#{pane_id}",
        "#{session_name}",
        "#{window_index}",
        "#{pane_index}",
        "#{pane_title}",
        "#{pane_current_path}",
        "#{pane_current_command}",
        "#{pane_active}",
        "#{pane_width}",
        "#{pane_height}",
        "#{pane_pid}",
        "#{pane_dead}",
    ]
)

# Where pane_exec parks a command it cannot type as one line, and how long such a
# script is kept afterwards so the `script` path in the reply stays readable.
SCRIPT_DIR = "~/.tmux-agents/exec"
SCRIPT_TTL = 24 * 3600.0

# A `&` that backgrounds a command: not `&&`, and not the `&` of `2>&1` or `&>log`.
# A quoted `&` matches too; that false positive only costs one unused script file.
BACKGROUND = re.compile(r"(?<![&>])&(?![&>])")

# Foreground commands that mean "the pane is sitting at a prompt", not working.
SHELL_COMMANDS = frozenset(
    {"sh", "bash", "zsh", "fish", "dash", "ksh", "mksh", "ash", "csh", "tcsh", "nu", "xonsh"}
)

# The tmux layouts worth exposing: every one of them rearranges every pane.
LAYOUTS = ("tiled", "even-horizontal", "even-vertical", "main-vertical", "main-horizontal")

SESSION_FORMAT = "\t".join(
    ["#{session_name}", "#{session_windows}", "#{session_attached}", "#{session_created}"]
)

WINDOW_FORMAT = "\t".join(
    ["#{session_name}", "#{window_index}", "#{window_name}", "#{window_panes}", "#{window_active}"]
)


def needs_script(command: str) -> bool:
    """Whether `pane_exec` has to put this command in a file rather than type it.

    Two shapes break the one-line `printf <BEG>; <command>; rc=$?` wrapper: a
    command spanning several lines (the shell echoes each continuation line back
    into the captured output, and a heredoc swallows the wrapper's tail), and one
    that backgrounds its last command with `&`, because `& ; rc=$?` will not parse.
    """
    return "\n" in command.strip() or bool(BACKGROUND.search(command))


class TmuxError(RuntimeError):
    """A tmux command failed or a target was refused."""


@dataclass(frozen=True)
class Pane:
    id: str
    session: str
    window: int
    index: int
    title: str
    cwd: str
    command: str
    active: bool
    width: int
    height: int
    pid: int
    dead: bool

    @property
    def target(self) -> str:
        return f"{self.session}:{self.window}.{self.index}"

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["target"] = self.target
        return data


@dataclass(frozen=True)
class Session:
    name: str
    windows: int
    attached: bool
    created: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentSpec:
    title: str
    command: str
    cwd: str | None = None


@dataclass(frozen=True)
class Window:
    session: str
    index: int
    name: str
    panes: int
    active: bool

    @property
    def target(self) -> str:
        return f"{self.session}:{self.index}"

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["target"] = self.target
        return data


class Tmux:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._last_sent: dict[str, str] = {}

    # ---------- low level ----------

    def available(self) -> bool:
        return shutil.which(self.settings.tmux) is not None

    def run(self, *args: str, check: bool = True, timeout: int = 20) -> str:
        argv = self.settings.base_args() + list(args)
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, check=False
            )
        except FileNotFoundError as exc:
            raise TmuxError(f"tmux binary not found: {self.settings.tmux}") from exc
        except subprocess.TimeoutExpired as exc:
            raise TmuxError(f"tmux timed out after {timeout}s: {' '.join(argv)}") from exc
        if check and proc.returncode != 0:
            raise TmuxError(proc.stderr.strip() or f"tmux exited {proc.returncode}")
        return proc.stdout

    def version(self) -> str:
        return self.run("-V").strip()

    # ---------- visibility policy ----------

    def _visible(self, session: str) -> bool:
        return session.startswith(self.settings.session_prefix)

    def _require_visible(self, session: str) -> None:
        if not self._visible(session):
            raise TmuxError(
                f"session {session!r} is outside TMUX_AGENTS_SESSION_PREFIX="
                f"{self.settings.session_prefix!r}"
            )

    # ---------- listing ----------

    def sessions(self) -> list[Session]:
        out = self.run("list-sessions", "-F", SESSION_FORMAT, check=False)
        result = []
        for line in out.splitlines():
            name, windows, attached, created = line.split("\t")
            if self._visible(name):
                result.append(Session(name, int(windows), attached != "0", int(created)))
        return result

    def panes(self, session: str | None = None) -> list[Pane]:
        if session is not None:
            self._require_visible(session)
            out = self.run("list-panes", "-s", "-t", session, "-F", PANE_FORMAT)
        else:
            out = self.run("list-panes", "-a", "-F", PANE_FORMAT, check=False)
        panes = []
        for line in out.splitlines():
            f = line.split("\t")
            pane = Pane(
                id=f[0],
                session=f[1],
                window=int(f[2]),
                index=int(f[3]),
                title=f[4],
                cwd=f[5],
                command=f[6],
                active=f[7] == "1",
                width=int(f[8]),
                height=int(f[9]),
                pid=int(f[10]),
                dead=f[11] == "1",
            )
            if self._visible(pane.session):
                panes.append(pane)
        return panes

    def resolve(self, pane: str, session: str | None = None) -> Pane:
        """Accept a pane id (%3), a target (sess:0.1) or a pane title."""
        candidates = self.panes(session)
        for p in candidates:
            if pane in {p.id, p.target}:
                return p
        for p in candidates:
            if p.title == pane:
                return p
        for p in candidates:
            if pane and pane.lower() in p.title.lower():
                return p
        raise TmuxError(f"no visible pane matches {pane!r}")

    # ---------- reading ----------

    def probe(self, pane: Pane) -> tuple[int, int, str]:
        """(history_size, line_cursor, foreground command) in one tmux round trip.

        `history_size` is how many lines have scrolled off the top; it is also the
        absolute index of the first visible line. `line_cursor` is the absolute
        index of the line the cursor sits on, so it only ever grows (until
        `clear_history`) and can be handed back as `since`.
        """
        out = self.run(
            "display-message",
            "-p",
            "-t",
            pane.id,
            "#{history_size}\t#{cursor_y}\t#{pane_current_command}",
        ).strip("\n")
        fields = out.split("\t")
        if len(fields) < 3:
            raise TmuxError(f"unexpected display-message output: {out!r}")
        history, cursor_y, command = int(fields[0]), int(fields[1]), fields[2]
        return history, history + cursor_y, command

    @staticmethod
    def is_busy(command: str) -> bool:
        """True when the pane's foreground process is not just a shell prompt."""
        return command.lstrip("-").lower() not in SHELL_COMMANDS

    def capture(self, pane: Pane, lines: int | None = None, *, clean: bool = True) -> str:
        n = min(lines or self.settings.max_lines, self.settings.max_lines)
        raw = self.run("capture-pane", "-p", "-J", "-t", pane.id, "-S", f"-{n}")
        return self._clean(raw, clean=clean)

    def capture_since(self, pane: Pane, since: int, *, clean: bool = True) -> str:
        """Capture every line from absolute offset `since` to the bottom of the pane."""
        history = self.probe(pane)[0]
        start = max(since - history, -min(history, self.settings.max_lines))
        raw = self.run("capture-pane", "-p", "-J", "-t", pane.id, "-S", str(start), "-E", "-")
        return self._clean(raw, clean=clean)

    def _clean(self, raw: str, *, clean: bool) -> str:
        text = strip_ansi(raw) if clean else raw
        text = "\n".join(line.rstrip() for line in text.splitlines()).rstrip("\n")
        if self.settings.redact:
            text = redact(text)
        return text

    def clear_history(self, pane: Pane) -> None:
        """Drop the scrollback. `since` offsets taken before this call become stale."""
        self.run("clear-history", "-t", pane.id)

    def fingerprint(self, pane: Pane) -> str:
        """Hash of the visible screen: what 'the pane is not changing' means."""
        raw = self.run("capture-pane", "-p", "-t", pane.id)
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()

    @staticmethod
    def strip_echo(text: str, echo: str | None) -> str:
        """Drop everything up to and including the shell's echo of `echo`.

        A pattern must not match the command that was just typed. Position alone
        cannot tell the echo apart from the output (both land on the same screen,
        and a long command wraps), so we key on the text we ourselves sent: the
        last line of it, found once, with the rest of that display line removed.
        `capture-pane -J` has already rejoined wrapped lines.
        """
        if not echo:
            return text
        marker = next((ln for ln in reversed(echo.strip().splitlines()) if ln.strip()), "")
        if not marker.strip():
            return text
        at = text.find(marker.strip())
        if at < 0:
            return text
        nl = text.find("\n", at + len(marker.strip()))
        return "" if nl < 0 else text[nl + 1 :]

    def wait(
        self,
        pane: Pane,
        *,
        timeout: float = 50.0,
        idle: float = 4.0,
        pattern: str | None = None,
        poll: float = 0.5,
        since: int | None = None,
        include_existing: bool = False,
        echo: str | None = None,
    ) -> dict[str, Any]:
        """Block until the pane goes idle, `pattern` appears, or `timeout` runs out.

        Idle means two things at once: the screen stopped changing *and* no
        foreground command is running. A silent build is therefore not idle, and a
        spinner that never settles reports `running` instead of `timeout`.

        `pattern` is searched only in what arrived after `since` (default: the
        history size when the wait began), minus the echo of the last text this
        server sent to the pane. `include_existing=True` restores the old
        behaviour of searching the whole visible scrollback.
        """
        regex = re.compile(pattern, re.MULTILINE) if pattern else None
        start = time.monotonic()
        history, cursor, command = self.probe(pane)
        if since is None:
            since = history
        if echo is None and not include_existing:
            echo = self._last_sent.get(pane.id)
        last_fp = self.fingerprint(pane)
        last_change = start

        def result(state: str) -> dict[str, Any]:
            history, cursor, command = self.probe(pane)
            return {
                "state": state,
                "elapsed": round(time.monotonic() - start, 1),
                "busy": self.is_busy(command),
                "current_command": command,
                "since_line": cursor,
            }

        while True:
            time.sleep(poll)
            now = time.monotonic()
            _, cursor, command = self.probe(pane)
            busy = self.is_busy(command)
            if regex is not None:
                if include_existing:
                    text = self.capture(pane, 200)
                else:
                    text = self.strip_echo(self.capture_since(pane, since), echo)
                if regex.search(text):
                    return result("matched")
            fp = self.fingerprint(pane)
            if fp != last_fp:
                last_fp, last_change = fp, now
            elif not busy and now - last_change >= idle:
                return result("idle")
            if now - start >= timeout:
                return result("running" if busy else "timeout")

    def wait_many(
        self,
        panes: list[Pane],
        *,
        mode: str = "any",
        timeout: float = 50.0,
        idle: float = 4.0,
        pattern: str | None = None,
        poll: float = 0.5,
    ) -> dict[str, Any]:
        """Watch several panes at once. `mode="any"` returns on the first to settle.

        A pane settles the same way it does in `wait`: `pattern` shows up in new
        output, or the screen stops changing while nothing is running. `mode="all"`
        keeps going until every pane has settled (or the timeout). Panes that never
        settle come back as `running` / `timeout`, exactly as a single wait would.
        """
        if not panes:
            raise TmuxError("pane_wait_any needs at least one pane")
        if mode not in {"any", "all"}:
            raise TmuxError(f"unknown wait mode {mode!r}")
        regex = re.compile(pattern, re.MULTILINE) if pattern else None
        start = time.monotonic()
        state: dict[str, dict[str, Any]] = {}
        for pane in panes:
            history, _, _ = self.probe(pane)
            state[pane.id] = {
                "pane": pane,
                "since": history,
                "echo": self._last_sent.get(pane.id),
                "fp": self.fingerprint(pane),
                "changed": start,
                "settled": None,
            }

        def snapshot(final: bool = False) -> dict[str, Any]:
            rows = []
            for pane in panes:
                item = state[pane.id]
                _, cursor, command = self.probe(item["pane"])
                busy = self.is_busy(command)
                result = item["settled"]
                if result is None and final:
                    result = "running" if busy else "timeout"
                rows.append(
                    {
                        "pane": item["pane"].as_dict(),
                        "state": result or "waiting",
                        "busy": busy,
                        "current_command": command,
                        "since_line": cursor,
                    }
                )
            done = [r for r in rows if r["state"] in {"idle", "matched"}]
            return {
                "elapsed": round(time.monotonic() - start, 1),
                "panes": rows,
                "settled": [r["pane"]["id"] for r in done],
                "settled_titles": [r["pane"]["title"] for r in done],
            }

        while True:
            time.sleep(poll)
            now = time.monotonic()
            for pane in panes:
                item = state[pane.id]
                if item["settled"]:
                    continue
                _, _, command = self.probe(pane)
                busy = self.is_busy(command)
                if regex is not None:
                    text = self.strip_echo(self.capture_since(pane, item["since"]), item["echo"])
                    if regex.search(text):
                        item["settled"] = "matched"
                        continue
                fp = self.fingerprint(pane)
                if fp != item["fp"]:
                    item["fp"], item["changed"] = fp, now
                elif not busy and now - item["changed"] >= idle:
                    item["settled"] = "idle"
            done = [p for p in panes if state[p.id]["settled"]]
            if (mode == "any" and done) or (mode == "all" and len(done) == len(panes)):
                return {"state": "settled", **snapshot()}
            if now - start >= timeout:
                return {"state": "timeout", **snapshot(final=True)}

    def exec(
        self,
        pane: Pane,
        command: str,
        *,
        timeout: float = 50.0,
        poll: float = 0.4,
        force: bool = False,
    ) -> dict[str, Any]:
        """Run a shell command in a pane and return only *its* output and exit code.

        The command is wrapped in two one-off markers, so the reply holds neither
        the shell's echo of what was typed nor anything that was already on screen:

            printf '\\n<BEG>\\n'; <command>; rc=$?; printf '\\n<END> %s\\n' "$rc"

        The markers only ever appear alone on a line when the shell actually printed
        them — the echoed command line has the quoting around them — so matching a
        whole line is enough to find the boundaries.

        That wrapper is one typed line, which two kinds of command cannot survive: a
        multi-line one (the shell echoes every continuation line, and with a heredoc
        the wrapper's own tail lands inside the body) and one ending in `&` (`... &;
        rc=$?` is a syntax error). Those go to a file run as `bash <file>` instead,
        and the reply says where the file is under `script`.
        """
        _, _, current = self.probe(pane)
        if not force and self.is_busy(current):
            raise TmuxError(
                f"pane is running {current!r}; pane_exec expects a shell prompt "
                f"(pass force=true to send anyway)"
            )
        token = uuid4().hex[:10].upper()
        beg, end = f"TAX{token}B", f"TAX{token}E"
        done = re.compile(rf"^{end} (\d+)$", re.M)
        script = self.write_exec_script(token, command) if needs_script(command) else None
        payload = f"bash {shlex.quote(str(script))}" if script else command
        since = self.probe(pane)[1]
        wrapped = (
            f"printf '\\n{beg}\\n'; {payload}; __ta_rc=$?; printf '\\n{end} %s\\n' \"$__ta_rc\""
        )
        self.send(pane, wrapped, enter=True)
        start = time.monotonic()
        while True:
            time.sleep(poll)
            text = self.capture_since(pane, since)
            hit = done.search(text)
            if hit:
                return {
                    "state": "done",
                    "exit_code": int(hit.group(1)),
                    "output": self._between(text, beg, end),
                    "elapsed": round(time.monotonic() - start, 1),
                    "since_line": self.probe(pane)[1],
                    "script": str(script) if script else None,
                }
            if time.monotonic() - start >= timeout:
                return {
                    "state": "timeout",
                    "exit_code": None,
                    "output": self._between(text, beg, end),
                    "elapsed": round(time.monotonic() - start, 1),
                    "since_line": self.probe(pane)[1],
                    "script": str(script) if script else None,
                    "note": "still running; read the pane or wait again",
                }

    @staticmethod
    def write_exec_script(token: str, command: str) -> Path:
        """Park a command in `SCRIPT_DIR` for the pane's shell to read back.

        Kept after the run — the reply points at it, so a command that failed can be
        looked at and run again by hand — and swept a day later. A command can carry a
        secret, so neither the directory nor the file is readable by anyone else.
        """
        directory = Path(SCRIPT_DIR).expanduser()
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            Tmux._prune_scripts(directory)
            path = directory / f"exec-{token}.sh"
            path.touch(mode=0o600)
            path.write_text(command.strip() + "\n", encoding="utf-8")
        except OSError as exc:
            raise TmuxError(f"cannot write the pane_exec script: {exc}") from exc
        return path

    @staticmethod
    def _prune_scripts(directory: Path, ttl: float = SCRIPT_TTL) -> None:
        cutoff = time.time() - ttl
        for old in directory.glob("exec-*.sh"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:  # someone else's file, or already gone
                pass

    @staticmethod
    def _between(text: str, beg: str, end: str) -> str:
        """Everything the command printed: the lines between the two marker lines."""
        lines = text.splitlines()
        try:
            first = len(lines) - 1 - lines[::-1].index(beg)
        except ValueError:
            return ""
        out = []
        for line in lines[first + 1 :]:
            if line.startswith(f"{end} "):
                break
            out.append(line)
        return "\n".join(out).strip("\n")

    # ---------- writing ----------

    def send(self, pane: Pane, text: str, *, enter: bool = True, literal: bool = True) -> None:
        if text:
            self._last_sent[pane.id] = text
            args = ["send-keys", "-t", pane.id]
            if literal:
                args.append("-l")
            self.run(*args, text)
        if enter:
            # Separate call so multi-line paste does not race the Enter key.
            time.sleep(0.05)
            self.run("send-keys", "-t", pane.id, "Enter")

    def send_many(self, panes: list[Pane], text: str, *, enter: bool = True) -> list[Pane]:
        """Type the same thing into several panes. One failure does not skip the rest."""
        if not panes:
            raise TmuxError("pane_send_many needs at least one pane")
        for pane in panes:
            self.send(pane, text, enter=enter)
        return panes

    def send_key(self, pane: Pane, key: str) -> None:
        """Send a named key: C-c, C-d, Escape, Up, Enter ..."""
        self.run("send-keys", "-t", pane.id, key)

    # ---------- sessions & layout ----------

    def has_session(self, session: str) -> bool:
        return session in {s.name for s in self.sessions()}

    def launch(
        self,
        session: str,
        agents: list[AgentSpec],
        *,
        cwd: str,
        layout: str = "auto",
        replace: bool = False,
    ) -> list[Pane]:
        self._require_visible(session)
        if not agents:
            raise TmuxError("agents_launch needs at least one agent")
        if self.has_session(session):
            if not replace:
                raise TmuxError(
                    f"session {session!r} already exists; pass replace=true to restart it"
                )
            self.kill_session(session)

        first = agents[0]
        self.run(
            "new-session", "-d", "-s", session, "-c", first.cwd or cwd, "-x", "250", "-y", "70"
        )
        self.run("set-option", "-t", session, "pane-border-status", "top")
        self.run("set-option", "-t", session, "pane-border-format", " #{pane_title} ")
        self.run("set-option", "-t", session, "allow-rename", "off")
        window = self.run(
            "display-message", "-p", "-t", f"={session}", "#{session_name}:#{window_index}"
        ).strip()
        for spec in agents[1:]:
            self.run("split-window", "-t", window, "-c", spec.cwd or cwd)
            self.run("select-layout", "-t", window, "tiled")

        panes = self.panes(session)
        panes.sort(key=lambda p: p.index)
        chosen = self._pick_layout(layout, len(panes), panes[0])
        self.run("select-layout", "-t", window, chosen)
        for pane, spec in zip(panes, agents, strict=False):
            self.run("select-pane", "-t", pane.id, "-T", spec.title)
            self.send(pane, spec.command, enter=True)
        self.run("select-pane", "-t", panes[0].id)
        argv = self.settings.open_command_argv(session)
        if argv:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self.panes(session)

    @staticmethod
    def _pick_layout(layout: str, count: int, sample: Pane) -> str:
        if layout != "auto":
            return layout
        if count == 1:
            return "even-horizontal"
        if count == 2:
            # side by side when the window is wide, stacked when narrow
            return "even-horizontal" if sample.width * 2 >= 160 else "even-vertical"
        return "tiled"

    def add_pane(
        self,
        session: str,
        title: str,
        command: str,
        *,
        cwd: str | None = None,
        layout: str | None = None,
    ) -> Pane:
        """Split the session's active window, title the new pane and start `command`."""
        self._require_visible(session)
        if not self.has_session(session):
            raise TmuxError(f"no session {session!r}")
        window = self.run(
            "display-message", "-p", "-t", f"={session}", "#{session_name}:#{window_index}"
        ).strip()
        args = ["split-window", "-t", window, "-P", "-F", "#{pane_id}"]
        if cwd:
            args += ["-c", cwd]
        pane_id = self.run(*args).strip()
        # A fresh split halves one pane; rearranging gives every agent the same room.
        self.run("select-layout", "-t", window, self._layout_name(layout or "tiled"))
        self.run("select-pane", "-t", pane_id, "-T", title)
        pane = self.resolve(pane_id, session)
        if command:
            self.send(pane, command, enter=True)
        return self.resolve(pane_id, session)

    def resize_pane(self, pane: Pane, width: int | None = None, height: int | None = None) -> Pane:
        """Set a pane's size in cells. Either dimension may be left alone."""
        if width is None and height is None:
            raise TmuxError("pane_resize needs width or height")
        args = ["resize-pane", "-t", pane.id]
        if width is not None:
            if width < 1:
                raise TmuxError("width must be at least 1")
            args += ["-x", str(width)]
        if height is not None:
            if height < 1:
                raise TmuxError("height must be at least 1")
            args += ["-y", str(height)]
        self.run(*args)
        return self.resolve(pane.id)

    def set_layout(self, target: str, layout: str) -> str:
        """Rearrange a session (its active window) or one window: `sess` or `sess:1`."""
        session = target.split(":", 1)[0]
        self._require_visible(session)
        name = self._layout_name(layout)
        self.run("select-layout", "-t", target, name)
        return name

    def zoom_pane(self, pane: Pane, on: bool = True) -> bool:
        """Make one pane fill its window, or give the window back. Returns the state."""
        if on:
            # zoom follows the active pane, so point the window at this one first
            self.run("select-pane", "-t", pane.id)
        if self._zoomed(pane) != on:
            self.run("resize-pane", "-Z", "-t", pane.id)
        return self._zoomed(pane)

    def _zoomed(self, pane: Pane) -> bool:
        return (
            self.run("display-message", "-p", "-t", pane.id, "#{window_zoomed_flag}").strip() == "1"
        )

    @staticmethod
    def _layout_name(layout: str) -> str:
        if layout not in LAYOUTS:
            raise TmuxError(f"unknown layout {layout!r}; use one of {', '.join(LAYOUTS)}")
        return layout

    def server_pid(self, session: str) -> int:
        return int(self.run("display-message", "-p", "-t", f"={session}", "#{pid}").strip())

    def keep_awake(self, session: str) -> dict[str, Any]:
        """Hold off idle sleep until the tmux server exits (macOS `caffeinate`)."""
        if sys.platform != "darwin":
            return {"requested": True, "active": False, "reason": f"not macOS ({sys.platform})"}
        if shutil.which("caffeinate") is None:
            return {"requested": True, "active": False, "reason": "caffeinate not found on PATH"}
        try:
            pid = self.server_pid(session)
        except (TmuxError, ValueError) as exc:
            return {"requested": True, "active": False, "reason": str(exc)}
        subprocess.Popen(
            ["caffeinate", "-i", "-w", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {
            "requested": True,
            "active": True,
            "waits_on_pid": pid,
            "command": f"caffeinate -i -w {pid}",
        }

    def clear_pane(self, pane: Pane) -> dict[str, Any]:
        """Blank the screen *and* drop the scrollback — `clear` alone keeps the history.

        A busy pane only gets its history dropped: typing `clear` into a running
        program would go to the program, not to a shell.
        """
        _, _, command = self.probe(pane)
        busy = self.is_busy(command)
        if not busy:
            self.send(pane, "clear", enter=True)
            time.sleep(0.2)
        self.run("clear-history", "-t", pane.id)
        history, cursor, _ = self.probe(pane)
        return {"cleared_screen": not busy, "history_size": history, "since_line": cursor}

    def swap_panes(self, a: Pane, b: Pane) -> list[Pane]:
        """Swap two panes' positions. The panes keep their ids, titles and processes."""
        if a.id == b.id:
            raise TmuxError("pane_swap needs two different panes")
        self.run("swap-pane", "-s", a.id, "-t", b.id)
        return sorted(self.panes(a.session), key=lambda p: (p.window, p.index))

    def scroll(self, pane: Pane, lines: int = 200, offset: int = 0, *, clean: bool = True) -> str:
        """Read a window of scrollback: `lines` lines ending `offset` lines above the bottom.

        `offset=0` is the tail (what `pane_read` gives); `offset=200` the 200 lines
        before those. That is how you page back to the start of an error that has
        already scrolled away. Both numbers count lines from the bottom of the pane,
        not from the top of the screen the way `capture-pane -S` does.
        """
        if lines < 1:
            raise TmuxError("lines must be at least 1")
        if offset < 0:
            raise TmuxError("offset cannot be negative")
        lines = min(lines, self.settings.max_lines)
        # capture-pane counts 0 at the *top* of the visible screen; translate.
        bottom = self.resolve(pane.id).height - 1
        end = bottom - offset
        start = end - lines + 1
        raw = self.run("capture-pane", "-p", "-J", "-t", pane.id, "-S", str(start), "-E", str(end))
        return self._clean(raw, clean=clean)

    # ---------- windows ----------

    def windows(self, session: str | None = None) -> list[Window]:
        if session is not None:
            self._require_visible(session)
            out = self.run("list-windows", "-t", f"={session}", "-F", WINDOW_FORMAT)
        else:
            out = self.run("list-windows", "-a", "-F", WINDOW_FORMAT, check=False)
        found = []
        for line in out.splitlines():
            name, index, title, panes, active = line.split("\t")
            if self._visible(name):
                found.append(Window(name, int(index), title, int(panes), active == "1"))
        return found

    def new_window(
        self,
        session: str,
        name: str | None = None,
        command: str | None = None,
        cwd: str | None = None,
    ) -> Window:
        self._require_visible(session)
        if not self.has_session(session):
            raise TmuxError(f"no session {session!r}")
        args = ["new-window", "-t", f"={session}", "-P", "-F", "#{session_name}:#{window_index}"]
        if name:
            args += ["-n", name]
        if cwd:
            args += ["-c", cwd]
        target = self.run(*args).strip()
        if command:
            pane = self.panes(session)
            chosen = [p for p in pane if f"{p.session}:{p.window}" == target]
            if chosen:
                self.send(chosen[0], command, enter=True)
        index = int(target.rsplit(":", 1)[1])
        for window in self.windows(session):
            if window.index == index:
                return window
        raise TmuxError(f"window {target} vanished right after it was created")

    def kill_window(self, target: str) -> None:
        session = target.split(":", 1)[0]
        self._require_visible(session)
        if not self.settings.allow_kill:
            raise TmuxError("killing is disabled (TMUX_AGENTS_ALLOW_KILL=false)")
        self.run("kill-window", "-t", target)

    def rename_window(self, target: str, name: str) -> Window:
        session = target.split(":", 1)[0]
        self._require_visible(session)
        if not name.strip():
            raise TmuxError("a window name cannot be empty")
        self.run("rename-window", "-t", target, name)
        index = int(target.rsplit(":", 1)[1]) if ":" in target else None
        for window in self.windows(session):
            if index is None or window.index == index:
                return window
        raise TmuxError(f"no window {target}")

    # ---------- attaching ----------

    def attach_command(self, session: str) -> str:
        """The exact shell command a human types to watch this session."""
        self._require_visible(session)
        if not self.has_session(session):
            raise TmuxError(f"no session {session!r}")
        prefix = " ".join(self.settings.base_args())
        return f"{prefix} attach -t {session}"

    def kill_session(self, session: str) -> None:
        self._require_visible(session)
        if not self.settings.allow_kill:
            raise TmuxError("killing is disabled (TMUX_AGENTS_ALLOW_KILL=false)")
        self.run("kill-session", "-t", f"={session}")

    def kill_pane(self, pane: Pane) -> None:
        if not self.settings.allow_kill:
            raise TmuxError("killing is disabled (TMUX_AGENTS_ALLOW_KILL=false)")
        self.run("kill-pane", "-t", pane.id)
