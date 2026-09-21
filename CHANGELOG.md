# Changelog

## 0.1.1 — 2026-09-22

- Fix: start under mcp 2.x (FastMCP was renamed to MCPServer). Supports mcp 1.2–2.x.

## 0.1.0 — 2026-09-22

Initial release.

- `agents_launch`: one session, one titled pane per agent, auto layout, optional terminal-app open.
- `pane_read`, `pane_wait` (idle / regex), `pane_send` (literal, multi-line safe), `pane_key`.
- `panes_list`, `tmux_status`, `pane_kill`, `session_kill`.
- Session-prefix scoping, secret redaction, no raw shell tool.
