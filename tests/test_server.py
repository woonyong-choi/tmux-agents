"""End-to-end through the MCP tool functions (real tmux, private socket)."""

from __future__ import annotations

import importlib
import json


def _load(server_env):
    import tmux_agents.server as server

    return importlib.reload(server)


def test_status_and_launch_roundtrip(server_env, workdir):
    s = _load(server_env)
    status = json.loads(s.tmux_status())
    assert status["ok"] and status["socket"] == server_env
    assert status["sessions"] == []

    launched = json.loads(
        s.agents_launch(
            "t-batch",
            workdir,
            [
                {"title": "WP1 Path", "command": "echo path-done; sh"},
                {"title": "WP2 Run", "command": "echo run-done; sh"},
            ],
        )
    )
    assert launched["ok"] and launched["attach"] == "tmux attach -t t-batch"
    assert [p["title"] for p in launched["panes"]] == ["WP1 Path", "WP2 Run"]

    waited = json.loads(s.pane_wait("WP2", timeout_seconds=10, idle_seconds=1))
    assert waited["state"] == "idle" and "run-done" in waited["tail"]

    read = json.loads(s.pane_read("wp1 path", lines=20))
    assert read["ok"] and "path-done" in read["text"]

    sent = json.loads(s.pane_send("WP1 Path", "echo answer-received"))
    assert sent["ok"] and sent["sent_chars"] == len("echo answer-received")
    waited = json.loads(s.pane_wait("WP1", pattern="answer-received", timeout_seconds=10))
    assert waited["state"] == "matched"

    listed = json.loads(s.panes_list("t-batch"))
    assert len(listed["panes"]) == 2

    killed = json.loads(s.pane_kill("WP2 Run"))
    assert killed["ok"]
    assert len(json.loads(s.panes_list("t-batch"))["panes"]) == 1

    assert json.loads(s.session_kill("t-batch"))["ok"]
    assert json.loads(s.tmux_status())["sessions"] == []


def test_errors_are_json_not_exceptions(server_env, workdir):
    s = _load(server_env)
    assert json.loads(s.pane_read("missing"))["ok"] is False
    assert json.loads(s.session_kill("outside-prefix"))["ok"] is False
    bad = json.loads(s.agents_launch("t-x", workdir, [{"title": "no command"}]))
    assert bad["ok"] is False and "command" in bad["error"]


def test_output_is_redacted(server_env, workdir):
    s = _load(server_env)
    s.agents_launch("t-red", workdir, [{"title": "leak", "command": "sh"}])
    s.pane_wait("leak", timeout_seconds=10, idle_seconds=1)
    s.pane_send("leak", "echo ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    s.pane_wait("leak", timeout_seconds=10, idle_seconds=1)
    text = json.loads(s.pane_read("leak"))["text"]
    assert "[redacted]" in text and "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in text
