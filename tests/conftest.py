from __future__ import annotations

import os
import shutil
import subprocess
import uuid

import pytest

from tmux_agents.settings import Settings
from tmux_agents.tmux import Tmux

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


@pytest.fixture
def tmux():
    """A Tmux wrapper bound to a throwaway tmux server (private socket)."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")
    socket = f"tmux-agents-test-{uuid.uuid4().hex[:8]}"
    settings = Settings(socket=socket, session_prefix="t-", max_lines=500, allow_kill=True)
    wrapper = Tmux(settings)
    # users often set base-index 1; make the throwaway server do the same so we catch it
    subprocess.run(["tmux", "-L", socket, "start-server"], capture_output=True)
    subprocess.run(["tmux", "-L", socket, "set", "-g", "base-index", "1"], capture_output=True)
    subprocess.run(["tmux", "-L", socket, "set", "-g", "pane-base-index", "1"], capture_output=True)
    yield wrapper
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)


@pytest.fixture
def server_env(monkeypatch):
    socket = f"tmux-agents-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("TMUX_AGENTS_SOCKET", socket)
    monkeypatch.setenv("TMUX_AGENTS_SESSION_PREFIX", "t-")
    yield socket
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)


@pytest.fixture
def workdir(tmp_path):
    os.makedirs(tmp_path, exist_ok=True)
    return str(tmp_path)


@pytest.fixture(autouse=True)
def isolated_event_log(tmp_path_factory, monkeypatch):
    """Never let a test append to the real ~/.tmux-agents/events.jsonl."""
    path = tmp_path_factory.mktemp("events") / "events.jsonl"
    monkeypatch.setenv("TMUX_AGENTS_EVENTS", str(path))
    return path
