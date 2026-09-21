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

## Why

Every coding agent ships as a terminal program, and every orchestrator wants to run several of them at once. The pieces that are missing are small but annoying:

- An agent hosted **somewhere else** (a cloud session, a desktop app, a phone client) has no terminal. It can plan the work but has to ask a human to type `run-batch.sh`.
- Four agents in four windows are four things to babysit. One stops to ask *"overwrite? (y/n)"* and everything waits until someone notices.
- Reading a pane, deciding it is done, and typing the follow-up is exactly the kind of loop a model is good at — if it can see the pane.

`tmux-agents` gives the orchestrating model seven tools that map to what you would do by hand, and nothing more. There is no generic `shell` tool: the model can type into agent panes, not into your machine.

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

There are other tmux MCP servers ([nickgnd/tmux-mcp](https://github.com/nickgnd/tmux-mcp), [laszlopere/mcp-tmux](https://github.com/laszlopere/mcp-tmux)) and they are good general tmux remotes: create windows, run commands, read output, some over SSH. `tmux-agents` is narrower on purpose: it knows about *agents* — titled panes, "launch N of these in a layout that fits", "wait until it goes quiet", prefix-scoped visibility, redaction — and it deliberately leaves out a raw command tool. If you want a tmux remote control, use one of those. If you want an orchestrator, this is the one.

## Development

```
git clone https://github.com/woonyong-choi/tmux-agents
cd tmux-agents
uv sync --extra dev
uv run pytest        # real tmux, private socket, ~20s
uv run ruff check src tests
```

Tests spin up a throwaway tmux server (`tmux -L tmux-agents-test-…`), so they never touch your sessions.

## License

MIT — see [LICENSE](LICENSE).
