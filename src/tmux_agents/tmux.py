"""Thin, typed wrapper over the tmux CLI. No tmux state is kept in memory."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any

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

# Foreground commands that mean "the pane is sitting at a prompt", not working.
SHELL_COMMANDS = frozenset(
    {"sh", "bash", "zsh", "fish", "dash", "ksh", "mksh", "ash", "csh", "tcsh", "nu", "xonsh"}
)

SESSION_FORMAT = "\t".join(
    ["#{session_name}", "#{session_windows}", "#{session_attached}", "#{session_created}"]
)


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
        import re

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

    def kill_session(self, session: str) -> None:
        self._require_visible(session)
        if not self.settings.allow_kill:
            raise TmuxError("killing is disabled (TMUX_AGENTS_ALLOW_KILL=false)")
        self.run("kill-session", "-t", f"={session}")

    def kill_pane(self, pane: Pane) -> None:
        if not self.settings.allow_kill:
            raise TmuxError("killing is disabled (TMUX_AGENTS_ALLOW_KILL=false)")
        self.run("kill-pane", "-t", pane.id)
