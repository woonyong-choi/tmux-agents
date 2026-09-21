from __future__ import annotations

import time

import pytest

from tmux_agents.tmux import AgentSpec, TmuxError


def _launch(tmux, workdir, n=2, session="t-demo", layout="auto"):
    agents = [AgentSpec(f"worker {i}", f"echo ready-{i}; sh") for i in range(1, n + 1)]
    return tmux.launch(session, agents, cwd=workdir, layout=layout)


def test_launch_creates_titled_panes(tmux, workdir):
    panes = _launch(tmux, workdir, 3)
    assert [p.title for p in sorted(panes, key=lambda p: p.index)] == [
        "worker 1",
        "worker 2",
        "worker 3",
    ]
    assert all(p.session == "t-demo" for p in panes)
    assert {s.name for s in tmux.sessions()} == {"t-demo"}


def test_launch_runs_commands_and_read_sees_output(tmux, workdir):
    _launch(tmux, workdir, 2)
    p = tmux.resolve("worker 2")
    state = tmux.wait(p, timeout=10, idle=1)
    assert state["state"] == "idle"
    assert "ready-2" in tmux.capture(p, 50)


def test_resolve_accepts_id_target_and_partial_title(tmux, workdir):
    panes = _launch(tmux, workdir, 2)
    p = panes[0]
    assert tmux.resolve(p.id).id == p.id
    assert tmux.resolve(p.target).id == p.id
    assert tmux.resolve("WORKER 1").id == p.id
    with pytest.raises(TmuxError):
        tmux.resolve("nope")


def test_send_and_wait_pattern(tmux, workdir):
    _launch(tmux, workdir, 1)
    p = tmux.resolve("worker 1")
    tmux.wait(p, timeout=10, idle=1)
    tmux.send(p, "echo hello-$((20+22))")
    state = tmux.wait(p, timeout=10, idle=1, pattern=r"hello-42")
    assert state["state"] == "matched"
    assert "hello-42" in tmux.capture(p, 20)


def test_send_literal_multiline_is_safe(tmux, workdir):
    _launch(tmux, workdir, 1)
    p = tmux.resolve("worker 1")
    tmux.wait(p, timeout=10, idle=1)
    tmux.send(p, "printf '%s\\n' 'a;b' \"C-c\" ''", enter=True)
    tmux.wait(p, timeout=10, idle=1)
    out = tmux.capture(p, 20)
    assert "a;b" in out and "C-c" in out  # key names were not expanded


def test_send_key_interrupts(tmux, workdir):
    _launch(tmux, workdir, 1)
    p = tmux.resolve("worker 1")
    tmux.wait(p, timeout=10, idle=1)
    tmux.send(p, "sleep 30")
    time.sleep(0.5)
    tmux.send_key(p, "C-c")
    state = tmux.wait(p, timeout=10, idle=1)
    assert state["state"] == "idle"
    assert tmux.resolve("worker 1").command != "sleep"


def test_wait_times_out_on_busy_pane(tmux, workdir):
    _launch(tmux, workdir, 1)
    p = tmux.resolve("worker 1")
    tmux.wait(p, timeout=10, idle=1)
    tmux.send(p, "i=0; while true; do i=$((i+1)); echo tick-$i; sleep 0.2; done")
    state = tmux.wait(p, timeout=3, idle=2)
    assert state["state"] == "timeout"
    tmux.send_key(p, "C-c")


def test_replace_and_kill(tmux, workdir):
    _launch(tmux, workdir, 1)
    with pytest.raises(TmuxError):
        _launch(tmux, workdir, 1)
    panes = tmux.launch("t-demo", [AgentSpec("again", "sh")], cwd=workdir, replace=True)
    assert panes[0].title == "again"
    tmux.kill_session("t-demo")
    assert tmux.sessions() == []


def test_session_prefix_is_enforced(tmux, workdir):
    with pytest.raises(TmuxError):
        tmux.launch("other", [AgentSpec("x", "sh")], cwd=workdir)
    # a session created outside the policy stays invisible
    tmux.run("new-session", "-d", "-s", "hidden", "-c", workdir)
    assert [s.name for s in tmux.sessions()] == []
    assert tmux.panes() == []
    with pytest.raises(TmuxError):
        tmux.kill_session("hidden")


def test_layout_choice():
    from tmux_agents.tmux import Pane, Tmux

    wide = Pane("%0", "s", 0, 0, "", "/", "sh", True, 120, 40, 1, False)
    narrow = Pane("%0", "s", 0, 0, "", "/", "sh", True, 60, 40, 1, False)
    assert Tmux._pick_layout("auto", 1, wide) == "even-horizontal"
    assert Tmux._pick_layout("auto", 2, wide) == "even-horizontal"
    assert Tmux._pick_layout("auto", 2, narrow) == "even-vertical"
    assert Tmux._pick_layout("auto", 4, wide) == "tiled"
    assert Tmux._pick_layout("main-vertical", 4, wide) == "main-vertical"
