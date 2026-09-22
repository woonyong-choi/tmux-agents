# Changelog

## Unreleased

- `relay/cloudflare/`: reference WebSocket relay for remote conductors (`--notify url:`), with deploy script and README section.

## 0.4.0 — 2026-09-23

Two halves. The hook grew an exit, so what an agent finishes locally can be seen —
and acted on — from outside the machine. And the tool set caught up with what an
agent could already do by typing `tmux` into a shell.

### The hook

- **One hook for Claude Code and Codex.** `tmux-agents hook --agent codex` accepts
  Codex's `notify` shape (an `agent-turn-complete` JSON object passed as an
  argument, last assistant message in the payload) alongside Claude Code's `Stop`
  shape (`{"transcript_path": ...}` on stdin), and normalizes both into the same
  event. A pipeline can mix Claude Code and Codex panes.
- **An event log.** Every finished turn appends one JSON line to
  `~/.tmux-agents/events.jsonl` (`--events`, `TMUX_AGENTS_EVENTS`, `--no-events`):
  `{ts, agent, session, pane, stage, status, message, handoff, next}`. `status` is
  `DONE`/`STOPPED` from the marker, or `TURN` when the turn carried none; `message`
  is the last assistant message clipped to its last 8 KB; `handoff` is the
  `handoff.md` path mentioned in it, if any.
- **`--notify <spec>`**, repeatable, pushes each event out to `file:/abs/path.log`,
  `gist:<id>[:<file>]`, `repo:<owner/name>[:<path>]` (both through `gh`) or
  `url:https://...` (POST). The pushed line is fixed —
  `<ISO8601Z> <session>/<pane_title> <STAGE> <DONE|STOPPED|TURN> next=<prompt>|PAUSE|none`
  — and independent of the pipeline: a stage that is not in the pipeline file, or no
  pipeline file at all, is still reported. One retry, a 3-second timeout, failures on
  stderr only; the next stage goes into the pane even when every sink is down.
- **A conductor pane.** A pipeline entry written `@pane:<title|id>: <sentence>` types
  that sentence into *another* pane instead of continuing in this one, so a finished
  worker wakes the orchestrator directly — Claude Code, Codex or a human, they are all
  just a pane that takes keystrokes.
- **`events_read(since_line)`**, a new MCP tool, hands those events to an
  orchestrator that has no shell on the machine, `next_since` at a time.
- `--pipeline` is now optional, and `examples/` has a Claude Code `settings.json`
  and a Codex `config.toml` side by side.

### The tools

- **`pane_exec(pane, command, timeout_seconds)`** runs a shell command in a pane and
  returns only *that command's* output and exit code — no echo of what was typed, no
  leftovers from the screen. It refuses a pane that is running an agent unless
  `force=true`.
- **`pane_wait_any` / `pane_wait_all`** watch a list of panes instead of one: return
  on the first to settle, or when every one of them has. `pane_send_many` types the
  same text into several panes at once.
- **`pane_add`** joins one more agent to a running session (split, title, start,
  rearrange). **`layout_set`**, **`pane_resize`**, **`pane_zoom`** and **`pane_swap`**
  cover the rest of the geometry.
- **`window_list` / `window_new` / `window_kill` / `window_rename`** manage windows,
  and **`session_attach_cmd`** gives a human the exact command to watch a session.
- **`pane_clear`** blanks the screen *and* drops the scrollback; **`pane_scroll`**
  pages back through output that has already left it.
- **`agents_launch(keep_awake=true)`** holds off idle sleep for as long as the tmux
  server lives (macOS `caffeinate -i -w <pid>`; elsewhere it is ignored and the reply
  says so).

## 0.3.0 — 2026-09-23

Everything here comes from running the server behind a remote bridge, where a tool
call is cut off after 60 seconds. Four things broke; all four are fixed.

- **`pane_wait` always returns.** `timeout_seconds` is capped at
  `TMUX_AGENTS_MAX_WAIT` (new, default 50) and the default dropped from 120 to 50,
  so the call cannot outlive a client's tool timeout. A capped call answers with
  `capped: true` and says to call again.
- **Idle means idle.** A pane counts as idle only when the screen stopped changing
  *and* no foreground command is running (`pane_current_command` is a shell). A
  silent 50-second build is no longer reported as finished. A pane that never
  settles — an agent's spinner — now ends a wait with `state: "running"` instead of
  `timeout`, so the caller knows to keep waiting rather than to give up. Replies
  carry `busy`, `current_command` and `since_line`.
- **A pattern no longer matches the command you typed.** `pane_wait` searches only
  output that arrived after the wait began, minus the shell's echo of the last text
  this server sent to that pane — so `pane_send("echo DONE")` +
  `pane_wait(pattern="DONE")` waits for the command instead of matching its echo,
  and callers no longer need `^` anchors that break on wrapped commands.
  `include_existing=true` restores the old whole-screen search.
- **Reads stay small.** `pane_read(since=...)` returns only what a pane printed
  after a given offset and hands back `next_since` for the following call. Output
  longer than `TMUX_AGENTS_MAX_CHARS` (new, default 12000) is cut at the front and
  flagged `truncated: true`. `pane_send(clear_history=true)` runs
  `tmux clear-history`, which `clear` alone never did.
- **`pane_send` refuses a busy pane** unless `force=true`, so a command meant for a
  shell cannot land inside a running build. `pane_send(wait_for=...)` sends and
  waits for a regex in one call.

Existing calls keep working: every new argument is optional and no tool was
removed or renamed. Only defaults changed (`pane_wait(timeout_seconds=120 → 50)`),
and a busy pane that used to answer `timeout` now answers `running`.

## 0.2.1 — 2026-09-22

- hook: also accept a marker quoted inside a sentence within the last 6 lines (agents often write "the last line is WP2 DONE" rather than the bare line).

## 0.2.0 — 2026-09-22

- Fix: `agents_launch` failed with `can't find window: 0` when tmux uses `base-index 1`; the window index is now looked up.
- New: `tmux-agents hook` — a Stop hook that chains pipeline stages: when an agent ends its turn with `<STAGE> DONE`, the next stage's prompt is typed into the same pane. `STOPPED`, `PAUSE` and end-of-pipeline halt the chain and log for a human.

## 0.1.1 — 2026-09-22

- Fix: start under mcp 2.x (FastMCP was renamed to MCPServer). Supports mcp 1.2–2.x.

## 0.1.0 — 2026-09-22

Initial release.

- `agents_launch`: one session, one titled pane per agent, auto layout, optional terminal-app open.
- `pane_read`, `pane_wait` (idle / regex), `pane_send` (literal, multi-line safe), `pane_key`.
- `panes_list`, `tmux_status`, `pane_kill`, `session_kill`.
- Session-prefix scoping, secret redaction, no raw shell tool.
