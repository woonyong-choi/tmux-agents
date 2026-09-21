# Contributing

- `uv sync --extra dev`, then `uv run pytest` and `uv run ruff check src tests` must pass.
- Tests run against a real tmux on a private socket; keep new tests that way (no mocks of tmux).
- No new tool that runs arbitrary commands. The scope is: launch agents, read panes, type into panes, wait, kill.
- Conventional commit messages (`feat:`, `fix:`, `docs:`).
