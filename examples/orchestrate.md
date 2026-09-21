# Example: a batch of three agents, steered by one

A prompt you can give the orchestrating model once `tmux-agents` is connected:

> Launch session `agents-wp5` in `/path/to/repo` with three agents:
> "WP5 Review" → `claude --model claude-opus-5 "$(cat prompts/wp5.md)"`,
> "WP6a Lazy" → `claude --model claude-sonnet-5 "$(cat prompts/wp6a.md)"`,
> "WP6b Shell" → `codex --model gpt-5-codex "$(cat prompts/wp6b.md)"`.
> Then loop: `pane_wait` each pane (idle 8s, timeout 240s). If the tail ends in a
> question, answer it with `pane_send` following the rules in `docs/plan.md`. If it
> ends with "handoff.md written", read the last 80 lines and summarise. When all
> three are done, `session_kill`.

Everything the model reads from the panes is another agent's output; it should
verify claims (`npm test`, `git status`) rather than trust "done".
