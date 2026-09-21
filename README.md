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
│ ✓ 87 tests passed       │ ? Overwrite main.ts? y/n│<br>
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
human) is needed only at the points you mark `PAUSE`, or when a stage says `STOPPED`. See
[Chaining stages with the Stop hook](#chaining-stages-with-the-stop-hook).

## Tools

| Tool | What it does |
|---|---|
| `tmux_status` | Server config, tmux version, visible sessions |
| `agents_launch` | Create a session with one pane per agent, title each pane, run each command, pick a layout that fits (side-by-side for 2, tiled for 3+), optionally open it in your terminal app |
| `panes_list` | Panes with title, cwd, running command, size |
| `pane_read` | Last N lines of a pane, ANSI stripped, secrets redacted |
| `pane_wait` | Block until a pane is quiet for N seconds or a regex appears — returns `idle` / `matched` / `timeout` plus the tail |
| `pane_send` | Type text (literally, multi-line safe) and press Enter |
| `pane_key` | Send `C-c`, `Escape`, `Up`, `Tab` … |
| `pane_kill` / `session_kill` | Stop one agent or the whole batch (can be disabled) |

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

pane_wait({"pane": "WP6b", "timeout_seconds": 300, "idle_seconds": 8})
// → {"state": "idle", "tail": "... Overwrite tsconfig.json? (y/N)"}

pane_send({"pane": "WP6b", "text": "y"})

pane_wait({"pane": "WP5", "pattern": "handoff\\.md written"})
pane_read({"pane": "WP5", "lines": 80})

session_kill({"session": "agents-wp5"})
```

`pane_wait` is the heart of it. "Quiet for 8 seconds" is a surprisingly reliable definition of *an agent is either done or waiting for you*, and the tail tells the model which.

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

**3. Register the hook** in the repo the agents work in, `.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {"hooks": [{"type": "command",
                  "command": "tmux-agents hook --pipeline pipeline/manta.txt",
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
file, a missing prompt file, or running outside tmux (no `$TMUX_PANE`). Options: `--log`,
`--pane`, `--delay`, and `--dry-run` (log the decision, type nothing).

Codex and other agents that expose a "turn finished" hook with the transcript path in stdin work
the same way; the hook only needs `{"transcript_path": ...}` on stdin.

## Configuration

All via environment variables (set them in the `env` block of your MCP config):

| Variable | Default | Meaning |
|---|---|---|
| `TMUX_AGENTS_SESSION_PREFIX` | *(empty = all)* | Only sessions whose name starts with this are visible or controllable. **Set it.** With `agents-` the model can never read or type into your personal tmux sessions. |
| `TMUX_AGENTS_OPEN_COMMAND` | *(none)* | Command run after `agents_launch`; `{session}` is substituted |
| `TMUX_AGENTS_ALLOW_KILL` | `true` | Set `false` to disable `pane_kill` / `session_kill` |
| `TMUX_AGENTS_REDACT` | `true` | Replace token-looking strings in pane text with `[redacted]` |
| `TMUX_AGENTS_MAX_LINES` | `2000` | Hard cap for `pane_read` |
| `TMUX_AGENTS_SOCKET` | *(default server)* | `tmux -L <socket>` — isolate the agents on their own tmux server |
| `TMUX_AGENTS_TMUX` | `tmux` | Path to the tmux binary |

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
