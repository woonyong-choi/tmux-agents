# Changelog

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
