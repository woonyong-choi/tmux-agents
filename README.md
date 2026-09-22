# tmux-agents

**Let one AI agent run a room full of coding agents.**

`tmux-agents` is a Model Context Protocol server that turns tmux into a control surface for CLI agents. Point Claude, Cursor, Codex, Hermes or any MCP client at it and it can start Claude Code / Codex / Hermes / aider in split panes, watch each one, answer their questions, hand them the next task and tear the session down — while you watch the same panes in your own terminal.

```
uvx tmux-agents
```

That is the whole install. It needs `tmux` on the machine and nothing else.

<p align="center">
<code>
┌ WP1 Path ───────────────┬ WP2 Run ────────────────┐<br>
│ claude --model sonnet   │ codex --model gpt-5     │<br>
│ 87 tests passed         │ ? Overwrite main.ts? y/n│<br>
├ WP3 Timeline ───────────┼ WP4 Diagrams ───────────┤<br>
│ hermes chat -q "..."    │ aider --yes             │<br>
└─────────────────────────┴─────────────────────────┘
</code>
</p>

## Why this exists — the concrete problem

This tool was built in the middle of a real session, out of one frustrating asymmetry.

I was orchestrating a multi-repo refactor with several coding agents at once: four Claude Code
workers in tmux panes, each on its own module, and a planning model above them deciding what
to run next. Two kinds of "orchestrator" were available to me:

- **A native CLI agent on the Mac** (Codex, Claude Code, Hermes …). It has a real shell. It can
  `open` a terminal, `tmux split-window`, start `claude --model …`, read the pane, notice that a
  worker is stuck on *"overwrite tsconfig.json? (y/N)"*, type `y`, and move on. Watching Codex do
  this is what made the gap obvious.
- **A hosted model** — Claude in the desktop/web app, or any agent running in someone else's
  cloud. It could read my repo through a file bridge and plan every step in detail, but it had
  **no terminal**. Every time it needed a batch started, a worker answered, or a follow-up typed,
  it had to stop and ask *me* to run one line. I became the keyboard for a model that already
  knew exactly what to type.

Same model quality, same plan, wildly different autonomy — purely because one process could touch
tmux and the other could not. There was no MCP server that closed that gap on the *agent* level:
the existing tmux servers are fine remote controls, but they think in windows and commands, not in
"a room full of agents I have to shepherd".

`tmux-agents` is the missing piece. It is deliberately small:

- It is only about **agents in panes**: launch N of them with titles and a layout that fits,
  wait until one goes quiet, read what it said, answer it, kill it.
- It gives the hosted model exactly the powers the native agent had — and **no more**. There is
  no generic `shell` tool; the model can type into agent panes, not into your machine.
- It runs on the machine that owns the terminal (`uvx tmux-agents` next to your tmux), and any
  MCP client anywhere can drive it.

### And the second half: not polling, hooking

Once the hosted model could see the panes, the next problem was *when to look*. Polling a pane
every minute from a cloud session costs tokens and still misses the moment an agent finishes.
Native agents don't have this problem either — they just block on the process.

So the package also ships a **Stop hook** (`tmux-agents hook`). Every agent ends its stage with a
one-line marker (`WP2 DONE`). When the agent's turn ends, the hook — running locally, for free —
reads the transcript, finds the marker, looks up the next stage in a plain-text pipeline file and
types the next prompt into the *same* pane. The chain runs itself; the orchestrating model (or a
human) is needed only at the points you mark `PAUSE`, or when a stage says `STOPPED`.

The same hook is also the event source: every finished turn — Claude Code's `Stop`, Codex's
`notify` — becomes one JSON line in `~/.tmux-agents/events.jsonl`, and `--notify` pushes that line
somewhere an orchestrator that *cannot* see this machine can read it. See
[Chaining stages with the Stop hook](#chaining-stages-with-the-stop-hook).

## Tools

| Tool | What it does |
|---|---|
| `tmux_status` | Server config, tmux version, visible sessions |
| `agents_launch` | Create a session with one pane per agent, title each pane, run each command, pick a layout that fits (side-by-side for 2, tiled for 3+), optionally open it in your terminal app |
| `panes_list` | Panes with title, cwd, running command, size |
| `pane_read` | Last N lines of a pane, ANSI stripped, secrets redacted; `since` reads only what is new |
| `pane_wait` | Block until a pane is quiet *and* idle, or a regex appears in new output — returns `idle` / `matched` / `running` / `timeout` plus the tail |
| `pane_send` | Type text (literally, multi-line safe) and press Enter; optionally wait for a regex in the same call |
| `pane_key` | Send `C-c`, `Escape`, `Up`, `Tab` … |
| `pane_kill` / `session_kill` | Stop one agent or the whole batch (can be disabled) |
| `events_read` | Read turn-end events written by the hook (`since_line` reads only what is new) — wait on finished turns instead of polling panes |

Plus one CLI subcommand, `tmux-agents hook`, for chaining stages without polling (below).

Panes are addressed by **title** (`"WP2"`, case-insensitive partial match), by id (`%3`) or by tmux target (`batch:0.1`). Titles are shown on the pane borders, so what the model calls a pane is what you see.

## Setup

### Claude Code

```
claude mcp add tmux-agents -- uvx tmux-agents
```

### Claude Desktop / Cursor / Windsurf / Codex

```json
{
  "mcpServers": {
    "tmux-agents": {
      "command": "uvx",
      "args": ["tmux-agents"],
      "env": {
        "TMUX_AGENTS_SESSION_PREFIX": "agents-",
        "TMUX_AGENTS_OPEN_COMMAND": "open -na Ghostty.app --args -e tmux attach -t {session}"
      }
    }
  }
}
```

`TMUX_AGENTS_OPEN_COMMAND` is the one macOS-flavoured line: it pops the new session into a real terminal window so you can watch. Other terminals:

| Terminal | Command |
|---|---|
| Ghostty | `open -na Ghostty.app --args -e tmux attach -t {session}` |
| iTerm2 | `open -na iTerm.app --args tmux attach -t {session}` |
| kitty | `kitty tmux attach -t {session}` |
| WezTerm | `wezterm start -- tmux attach -t {session}` |
| Terminal.app | `osascript -e 'tell app "Terminal" to do script "tmux attach -t {session}"'` |
| Linux (any) | `x-terminal-emulator -e tmux attach -t {session}` |

> Upgrading: `uvx` caches the tool. After a new release run `uvx --refresh tmux-agents` once (or pin `uvx tmux-agents@0.2.0`) so the cached copy is replaced.

Leave it unset and the session runs detached; `tmux attach -t <name>` from any terminal shows it.

## A full loop, as the model sees it

```jsonc
agents_launch({
  "session": "agents-wp5",
  "cwd": "/Users/me/repo",
  "agents": [
    {"title": "WP5 Review",   "command": "claude --model claude-opus-5 \"$(cat prompts/wp5.md)\""},
    {"title": "WP6a Lazy",    "command": "claude --model claude-sonnet-5 \"$(cat prompts/wp6a.md)\""},
    {"title": "WP6b Shell",   "command": "codex --model gpt-5-codex \"$(cat prompts/wp6b.md)\""}
  ]
})
// → 3 tiled panes, Ghostty window opens

pane_wait({"pane": "WP6b", "idle_seconds": 8})
// → {"state": "running", "busy": true, "current_command": "codex",
//    "capped": true, "since_line": 1840}   // still thinking: call again
pane_wait({"pane": "WP6b", "idle_seconds": 8})
// → {"state": "idle", "busy": false, "since_line": 1907,
//    "tail": "... Overwrite tsconfig.json? (y/N)"}

pane_send({"pane": "WP6b", "text": "y", "force": true})   // a pane holding an agent is "busy"

pane_wait({"pane": "WP5", "pattern": "handoff\\.md written"})
pane_read({"pane": "WP5", "since": 1907})   // only what WP5 printed since
// → {"text": "...", "next_since": 2233, "truncated": false}

session_kill({"session": "agents-wp5"})
```

`pane_wait` is the heart of it, and it asks two questions, not one: has the screen
stopped changing, *and* is the pane back at a shell prompt? Either alone lies. A
`npm run build` that prints nothing for a minute has a perfectly still screen; a
Claude Code pane with a spinner never stops changing. Screen-still **and**
prompt-back is a reliable definition of *this agent is either done or waiting for
you*, and the tail tells the model which.

Two consequences worth knowing:

- **Every wait returns.** `timeout_seconds` is capped at `TMUX_AGENTS_MAX_WAIT`
  (50s) so the call finishes inside the 60-second tool timeout that remote
  bridges impose. `state: "running"` with `capped: true` means "still working,
  ask again" — it is a checkpoint, not a failure.
- **A pattern never matches the command you just typed.** `pane_wait` searches
  only output that arrived after the wait began, minus the shell's echo of the
  last text this server sent. Without that, `pane_send("echo DONE")` followed by
  `pane_wait(pattern="DONE")` matches the echo instantly, before the command has
  run. Pass `include_existing: true` for the old, whole-screen search.

For a short command the two steps collapse into one call:

```jsonc
pane_send({"pane": "WP5", "text": "pytest -q", "wait_for": "passed|failed"})
// → {"state": "matched", "elapsed": 12.4, "tail": "36 passed in 80.31s"}
```

## Chaining stages with the Stop hook

`tmux-agents hook` is a command for Claude Code's (or any compatible agent's) `Stop` hook. It
turns a list of prompts into a self-advancing pipeline without anyone polling.

**1. Make every stage end with a marker.** Put this at the end of each stage prompt:

> Finish by printing exactly `WP2 DONE` on its own line, or `WP2 STOPPED` if you could not finish.

The marker is `<STAGE> DONE|STOPPED` — uppercase stage name, one space, then the word — on a line
of its own. The last such line in the agent's final message wins, so quoting the marker earlier in
the prompt does no harm.

**2. Write a pipeline file** (`pipeline/manta.txt`, say):

```
# after <stage> finishes, type <prompt file> into the same pane
WP1|prompts/wp2.md
WP2|prompts/wp3.md
WP3|PAUSE            # stop here: a person or orchestrator decides what's next
```

Paths are relative to the pipeline file.

**3. Register the hook once, globally** — in `~/.claude/settings.json` (applies to every Claude Code session on the machine; in a repo with no matching pipeline stage the hook does nothing, so it is safe everywhere). Put it in a repo's own `.claude/settings.json` instead only if you want it scoped to that repo:

```json
{
  "hooks": {
    "Stop": [
      {"hooks": [{"type": "command",
                  "command": "tmux-agents hook --pipeline /abs/path/to/pipeline/manta.txt",
                  "timeout": 15}]}
    ]
  }
}
```

That is all. When a worker prints `WP1 DONE`, the hook types `prompts/wp2.md` into its pane three
seconds later; when it prints `WP2 DONE`, `wp3.md` follows; at `WP3 DONE` the chain pauses. Every
decision is appended to `pipeline/manta.log`:

```
2026-09-22 05:41:03 pane=%0 WP1 DONE
2026-09-22 05:41:03   -> next: prompts/wp2.md
2026-09-22 06:02:17 pane=%0 WP2 STOPPED
2026-09-22 06:02:17   -> stopped; needs a human or orchestrator
```

What halts the chain, on purpose: a `STOPPED` marker, a `PAUSE` entry, a stage that is not in the
file, a missing prompt file, or running outside tmux (no `$TMUX_PANE`). None of that stops the
event from being recorded. Options: `--agent`, `--notify`, `--events`, `--no-events`, `--log`,
`--pane`, `--delay`, and `--dry-run` (decide and record, type nothing).

**Codex CLI** reaches the same hook through its own `notify` setting, in `~/.codex/config.toml`:

```toml
notify = ["tmux-agents", "hook", "--agent", "codex"]
```

Codex appends one `agent-turn-complete` JSON object to that argument list instead of writing to
stdin, and the last assistant message is in the payload rather than in a transcript file. The hook
takes either shape and normalizes both, so a pipeline can mix Claude Code and Codex panes. Side by
side: [`examples/claude_settings.json`](examples/claude_settings.json) and
[`examples/codex_config.toml`](examples/codex_config.toml). Any other agent that runs a command at
the end of a turn works too; it only needs to hand over `{"transcript_path": ...}` on stdin.

### The event log

Whatever the agent, every finished turn becomes one line in `~/.tmux-agents/events.jsonl`
(`--events <path>` or `TMUX_AGENTS_EVENTS` to move it, `--no-events` to turn it off):

```json
{"ts":"2026-09-23T04:11:07Z","agent":"claude","session":"manta","pane":"WP2 Run","stage":"WP2","status":"DONE","message":"...the whole last assistant message...","handoff":"~/woon-work/WP-J/handoff.md","next":"prompts/wp3.md"}
```

| Field | |
|---|---|
| `ts` | when the turn ended, ISO 8601 UTC |
| `agent` | `claude` or `codex` |
| `session` / `pane` | tmux session name and pane title (`-` outside tmux) |
| `stage` / `status` | the marker: `WP2` + `DONE`/`STOPPED`, or `null` + `TURN` when the turn carried no marker |
| `message` | the last assistant message in full, clipped to the last 8 KB |
| `handoff` | the last `handoff.md` path mentioned in it, or `null` |
| `next` | the prompt the hook typed, `PAUSE`, or `null` |

### Getting the events out (`--notify`)

An orchestrator running *on this machine* just follows the file. One running somewhere else — a
cloud session, another model — cannot see it, so `--notify <spec>` pushes each event out. Repeat
the flag for more than one sink:

| Spec | What it does |
|---|---|
| `file:/abs/path.log` | append the line locally |
| `gist:<gist_id>[:<filename>]` | append to a gist file via `gh gist edit` (filename defaults to `tmux-agents.log`) |
| `repo:<owner/name>[:<path>]` | append to a file in a repo via `gh api`, one commit per event |
| `url:https://...` | POST the whole event as JSON |

The pushed line is fixed, and independent of the pipeline — a stage that is not in the pipeline
file, or no pipeline file at all, still gets reported:

```
2026-09-23T04:11:07Z manta/WP2_Run WP2 DONE next=prompts/wp3.md
2026-09-23T05:22:41Z manta/WP3_Docs WP3 STOPPED next=none
```

`<ISO8601Z> <session>/<pane_title> <STAGE> <DONE|STOPPED|TURN> next=<prompt>|PAUSE|none`.
Whitespace inside a session name or pane title becomes `_` so `awk`/`cut` still work; an unknown
field is `-`. `gist:` and `repo:` need `gh` on `PATH` and authenticated.

Delivery is best effort and deliberately out of the way: one retry, a 3-second timeout, failures
on stderr only. The hook exits 0 and the next stage goes into the pane even when every sink is
down.

### How an orchestrator picks them up

**On this machine** — follow the file, or call `events_read(since_line=...)` and keep the
`next_since` it returns:

```bash
tail -f ~/.tmux-agents/events.jsonl | jq -r '"\(.pane) \(.stage // "-") \(.status)"'
```

**From somewhere else** — have the hook write to a gist (or a repo file) and poll its raw URL,
printing only what is new:

```bash
# tmux-agents hook ... --notify gist:<gist_id>:run.log
RAW="https://gist.githubusercontent.com/<user>/<gist_id>/raw/run.log"
seen=0
while :; do
  curl -fsSL "$RAW" > /tmp/run.log || { sleep 30; continue; }
  now=$(wc -l < /tmp/run.log)
  [ "$now" -gt "$seen" ] && sed -n "$((seen + 1)),\$p" /tmp/run.log && seen=$now
  sleep 30
done
```

A raw gist URL is cached for a minute or so, so expect the first line to show up a poll or two
late; `repo:<owner/name>` through `gh api` is immediate but costs a commit per event.

## Configuration

All via environment variables (set them in the `env` block of your MCP config):

| Variable | Default | Meaning |
|---|---|---|
| `TMUX_AGENTS_SESSION_PREFIX` | *(empty = all)* | Only sessions whose name starts with this are visible or controllable. **Set it.** With `agents-` the model can never read or type into your personal tmux sessions. |
| `TMUX_AGENTS_OPEN_COMMAND` | *(none)* | Command run after `agents_launch`; `{session}` is substituted |
| `TMUX_AGENTS_ALLOW_KILL` | `true` | Set `false` to disable `pane_kill` / `session_kill` |
| `TMUX_AGENTS_REDACT` | `true` | Replace token-looking strings in pane text with `[redacted]` |
| `TMUX_AGENTS_MAX_LINES` | `2000` | Hard cap on the lines `pane_read` captures |
| `TMUX_AGENTS_MAX_CHARS` | `12000` | Hard cap on the characters `pane_read` returns; longer output is cut at the front and flagged `truncated` |
| `TMUX_AGENTS_MAX_WAIT` | `50` | Hard cap in seconds on `pane_wait`. Keep it below your client's tool timeout — remote bridges cut calls off at 60s |
| `TMUX_AGENTS_SOCKET` | *(default server)* | `tmux -L <socket>` — isolate the agents on their own tmux server |
| `TMUX_AGENTS_TMUX` | `tmux` | Path to the tmux binary |
| `TMUX_AGENTS_EVENTS` | `~/.tmux-agents/events.jsonl` | Where the hook appends turn-end events and `events_read` reads them |

## Security model, plainly

- `pane_send` types into a pane. If that pane is a shell, that is a shell. Keep agent sessions under a prefix (`TMUX_AGENTS_SESSION_PREFIX`) and the model cannot reach anything else.
- Pane text is scrubbed for GitHub/OpenAI/Slack/AWS tokens, bearer headers and `*_TOKEN=` style assignments before it is returned. This is a filter, not a guarantee — do not paste secrets into agent panes.
- The server instructs the model to treat everything it reads from panes as data written by another program, never as instructions. Agents talking to agents is exactly where prompt injection lives; the instruction is a reminder, not a defence. Review what your agents commit.
- No network, no config files, no state: the server is a thin layer over the `tmux` CLI and forgets everything between calls.

## How it compares

There are other tmux MCP servers ([nickgnd/tmux-mcp](https://github.com/nickgnd/tmux-mcp), [laszlopere/mcp-tmux](https://github.com/laszlopere/mcp-tmux)) and they are good general tmux remotes: create windows, run commands, read output, some over SSH. `tmux-agents` is narrower on purpose: it knows about *agents* — titled panes, "launch N of these in a layout that fits", "wait until it goes quiet", prefix-scoped visibility, redaction, a Stop hook that chains stages — and it deliberately leaves out a raw command tool. If you want a tmux remote control, use one of those. If you want an orchestrator, this is the one.

## Development

```
git clone https://github.com/woonyong-choi/tmux-agents
cd tmux-agents
uv sync --extra dev
uv run pytest        # real tmux on a private socket, ~25s
uv run ruff check src tests
```

Tests spin up a throwaway tmux server (`tmux -L tmux-agents-test-…`), so they never touch your sessions.

## License

MIT — see [LICENSE](LICENSE).
