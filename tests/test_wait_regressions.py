"""Regressions for the four ways this server broke behind a 60-second tool bridge.

Every test drives a real tmux server on a throwaway socket, because all four bugs
were in how we read tmux, not in our own bookkeeping.
"""

from __future__ import annotations

import importlib
import json

import pytest

from tmux_agents.tmux import AgentSpec

SHELL = "PS1='$ '; sh"


@pytest.fixture
def pane(tmux, workdir):
    """One pane sitting at a plain shell prompt, ready for a command."""
    tmux.launch("t-reg", [AgentSpec("worker", SHELL)], cwd=workdir)
    p = tmux.resolve("worker")
    tmux.wait(p, timeout=10, idle=1)
    return p


# --- 1. a silent long command is not idle -------------------------------------


def test_silent_command_is_not_idle(tmux, pane):
    """`npm run pdf` prints nothing for 50s; the screen not changing is not enough."""
    tmux.send(pane, "sleep 4")
    state = tmux.wait(pane, timeout=2, idle=1)
    assert state["state"] == "running"
    assert state["busy"] is True
    assert state["current_command"] == "sleep"
    tmux.send_key(pane, "C-c")


def test_idle_still_reported_at_a_prompt(tmux, pane):
    tmux.send(pane, "echo quick")
    state = tmux.wait(pane, timeout=10, idle=1)
    assert state["state"] == "idle"
    assert state["busy"] is False
    assert state["since_line"] >= 0


# --- 2. the pattern must not match the echo of the command --------------------


def test_pattern_does_not_match_the_typed_command(tmux, pane):
    """`sleep 2; echo MARKER` used to 'match' instantly, on its own echo."""
    tmux.send(pane, "sleep 2; echo REG-MARKER-42")
    state = tmux.wait(pane, timeout=10, idle=10, pattern="REG-MARKER-42")
    assert state["state"] == "matched"
    assert state["elapsed"] >= 1.5, "matched the echo instead of the output"


def test_include_existing_reproduces_the_old_echo_match(tmux, pane):
    tmux.send(pane, "sleep 2; echo REG-MARKER-43")
    state = tmux.wait(pane, timeout=10, idle=10, pattern="REG-MARKER-43", include_existing=True)
    assert state["state"] == "matched"
    assert state["elapsed"] < 1.5
    tmux.send_key(pane, "C-c")


def test_pattern_ignores_scrolled_off_history(tmux, pane):
    """A marker that has scrolled away is old news, however deep the scrollback is."""
    tmux.send(pane, "echo OLD-NEWS; i=0; while [ $i -lt 200 ]; do i=$((i+1)); echo f-$i; done")
    tmux.wait(pane, timeout=20, idle=1)

    state = tmux.wait(pane, timeout=2, idle=10, pattern="OLD-NEWS")
    assert state["state"] == "timeout"
    assert "OLD-NEWS" in tmux.capture(pane, 500), "the marker is still in the scrollback"


# --- 3. incremental reading -----------------------------------------------------


def test_capture_since_returns_only_new_lines(tmux, pane):
    tmux.send(pane, "echo FIRST-BATCH")
    tmux.wait(pane, timeout=10, idle=1)
    since = tmux.probe(pane)[1]
    tmux.send(pane, "echo SECOND-BATCH")
    tmux.wait(pane, timeout=10, idle=1)

    fresh = tmux.capture_since(pane, since)
    assert "SECOND-BATCH" in fresh
    assert "FIRST-BATCH" not in fresh
    assert "FIRST-BATCH" in tmux.capture(pane, 50)


def test_capture_since_survives_a_scrolled_pane(tmux, pane):
    since = tmux.probe(pane)[1]
    tmux.send(pane, "i=0; while [ $i -lt 120 ]; do i=$((i+1)); echo row-$i; done")
    tmux.wait(pane, timeout=15, idle=1)
    fresh = tmux.capture_since(pane, since)
    assert "row-1\n" in fresh and "row-120" in fresh


def test_clear_history_shrinks_what_a_read_returns(tmux, pane):
    tmux.send(pane, "i=0; while [ $i -lt 120 ]; do i=$((i+1)); echo row-$i; done")
    tmux.wait(pane, timeout=15, idle=1)
    assert tmux.probe(pane)[0] > 50
    tmux.clear_history(pane)
    assert tmux.probe(pane)[0] == 0
    assert "row-1\n" not in tmux.capture(pane, 500)


# --- 4. the wait is capped below the client's tool timeout ----------------------


def _server(server_env):
    import tmux_agents.server as server

    return importlib.reload(server)


def test_pane_wait_caps_a_too_long_timeout(server_env, workdir, monkeypatch):
    monkeypatch.setenv("TMUX_AGENTS_MAX_WAIT", "3")
    s = _server(server_env)
    s.agents_launch("t-cap", workdir, [{"title": "capped", "command": SHELL}])
    s.pane_wait("capped", timeout_seconds=10, idle_seconds=1)
    json.loads(s.pane_send("capped", "sleep 30"))

    waited = json.loads(s.pane_wait("capped", timeout_seconds=120, idle_seconds=1))
    assert waited["capped"] is True
    assert "call pane_wait again" in waited["hint"]
    assert waited["elapsed"] <= 6, "blocked past the cap"
    assert waited["state"] == "running" and waited["busy"] is True
    s.pane_key("capped", "C-c")


def test_pane_wait_does_not_flag_a_timeout_within_the_cap(server_env, workdir):
    s = _server(server_env)
    s.agents_launch("t-cap2", workdir, [{"title": "fine", "command": SHELL}])
    waited = json.loads(s.pane_wait("fine", timeout_seconds=10, idle_seconds=1))
    assert waited["state"] == "idle"
    assert "capped" not in waited


# --- the tools these fixes added ------------------------------------------------


def test_pane_read_since_and_truncation(server_env, workdir, monkeypatch):
    monkeypatch.setenv("TMUX_AGENTS_MAX_CHARS", "500")
    s = _server(server_env)
    s.agents_launch("t-read", workdir, [{"title": "reader", "command": SHELL}])
    s.pane_wait("reader", timeout_seconds=10, idle_seconds=1)

    first = json.loads(s.pane_read("reader", lines=50))
    assert first["truncated"] is False
    since = first["next_since"]

    s.pane_send("reader", "i=0; while [ $i -lt 200 ]; do i=$((i+1)); echo row-$i; done")
    s.pane_wait("reader", timeout_seconds=20, idle_seconds=1)

    full = json.loads(s.pane_read("reader", lines=500))
    assert full["truncated"] is True and len(full["text"]) == 500

    fresh = json.loads(s.pane_read("reader", since=since))
    assert "row-200" in fresh["text"]
    assert fresh["next_since"] > since


def test_pane_send_refuses_a_busy_pane_unless_forced(server_env, workdir):
    s = _server(server_env)
    s.agents_launch("t-busy", workdir, [{"title": "busy", "command": SHELL}])
    s.pane_wait("busy", timeout_seconds=10, idle_seconds=1)
    s.pane_send("busy", "sleep 30")

    refused = json.loads(s.pane_send("busy", "echo nope"))
    assert refused["ok"] is False and refused["busy"] is True
    assert refused["current_command"] == "sleep"

    forced = json.loads(s.pane_send("busy", "", enter=False, force=True))
    assert forced["ok"] is True
    s.pane_key("busy", "C-c")


def test_pane_send_wait_for_returns_the_result_in_one_call(server_env, workdir):
    s = _server(server_env)
    s.agents_launch("t-oneshot", workdir, [{"title": "one", "command": SHELL}])
    s.pane_wait("one", timeout_seconds=10, idle_seconds=1)

    sent = json.loads(s.pane_send("one", "sleep 2; echo ONESHOT-OK", wait_for="ONESHOT-OK"))
    assert sent["ok"] is True and sent["state"] == "matched"
    assert sent["elapsed"] >= 1.5
    assert "ONESHOT-OK" in sent["tail"]


def test_pane_send_clear_history_empties_the_scrollback(server_env, workdir):
    s = _server(server_env)
    s.agents_launch("t-clear", workdir, [{"title": "clr", "command": SHELL}])
    s.pane_wait("clr", timeout_seconds=10, idle_seconds=1)
    s.pane_send("clr", "i=0; while [ $i -lt 120 ]; do i=$((i+1)); echo old-$i; done")
    s.pane_wait("clr", timeout_seconds=20, idle_seconds=1)
    assert "old-1\n" in json.loads(s.pane_read("clr", lines=500))["text"]

    sent = json.loads(s.pane_send("clr", "echo after-clear", clear_history=True))
    assert sent["cleared_history"] is True
    s.pane_wait("clr", timeout_seconds=10, idle_seconds=1)
    assert "old-1\n" not in json.loads(s.pane_read("clr", lines=500))["text"]
