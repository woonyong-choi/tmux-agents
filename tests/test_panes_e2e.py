"""Layout, exec and window control, against a real tmux server on a private socket."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time

import pytest

from tmux_agents.tmux import AgentSpec, TmuxError


def _launch(tmux, workdir, n=2, session="t-demo", layout="auto"):
    agents = [AgentSpec(f"worker {i}", f"echo ready-{i}; sh") for i in range(1, n + 1)]
    return tmux.launch(session, agents, cwd=workdir, layout=layout)


def _settle(tmux, title, timeout=10):
    pane = tmux.resolve(title)
    tmux.wait(pane, timeout=timeout, idle=1)
    return tmux.resolve(title)


# --- pane_add / pane_resize / layout_set / pane_zoom ---------------------------


def test_add_pane_joins_a_running_session_and_runs_its_command(tmux, workdir):
    _launch(tmux, workdir, 2)
    added = tmux.add_pane("t-demo", "latecomer", "echo joined-late; sh", cwd=workdir)
    assert added.title == "latecomer"
    assert len(tmux.panes("t-demo")) == 3
    tmux.wait(added, timeout=10, idle=1)
    assert "joined-late" in tmux.capture(added, 50)
    # the window was rearranged, not just halved: nobody is starved
    heights = [p.height for p in tmux.panes("t-demo")]
    assert max(heights) - min(heights) <= 2


def test_add_pane_honours_an_explicit_layout(tmux, workdir):
    _launch(tmux, workdir, 2)
    tmux.add_pane("t-demo", "third", "sh", cwd=workdir, layout="even-horizontal")
    widths = [p.width for p in tmux.panes("t-demo")]
    assert len(widths) == 3 and max(widths) - min(widths) <= 2
    assert len({p.height for p in tmux.panes("t-demo")}) == 1  # side by side


def test_add_pane_rejects_an_unknown_layout_and_session(tmux, workdir):
    _launch(tmux, workdir, 1)
    with pytest.raises(TmuxError):
        tmux.add_pane("t-demo", "x", "sh", layout="spiral")
    with pytest.raises(TmuxError):
        tmux.add_pane("t-nope", "x", "sh")


def test_resize_pane_changes_the_size_tmux_reports(tmux, workdir):
    panes = _launch(tmux, workdir, 2, layout="even-horizontal")
    first = panes[0]
    before = tmux.resolve(first.id).width
    after = tmux.resize_pane(first, width=before - 40)
    assert after.width == before - 40
    assert tmux.resolve(first.id).width == before - 40
    with pytest.raises(TmuxError):
        tmux.resize_pane(first)


def test_layout_set_rearranges_every_pane(tmux, workdir):
    _launch(tmux, workdir, 3)
    tmux.set_layout("t-demo", "even-vertical")
    stacked = tmux.panes("t-demo")
    assert len({p.width for p in stacked}) == 1  # one column
    assert len({p.height for p in stacked}) <= 2

    tmux.set_layout("t-demo", "main-vertical")
    main = sorted(tmux.panes("t-demo"), key=lambda p: p.index)
    # one pane down the full height, the rest stacked in a column beside it
    assert main[0].height > main[1].height
    assert main[1].width == main[2].width and main[1].height + main[2].height < main[0].height + 3
    with pytest.raises(TmuxError):
        tmux.set_layout("t-demo", "diagonal")


def test_zoom_fills_the_window_and_restores_it(tmux, workdir):
    panes = _launch(tmux, workdir, 3)
    small = sorted(panes, key=lambda p: p.index)[1]
    before = tmux.resolve(small.id)
    assert tmux.zoom_pane(before, True) is True
    zoomed = tmux.resolve(small.id)
    assert zoomed.height > before.height and zoomed.width >= before.width
    assert tmux.zoom_pane(zoomed, False) is False
    assert tmux.resolve(small.id).height == before.height
    # the other panes kept running while hidden
    assert len(tmux.panes("t-demo")) == 3


# --- pane_exec ------------------------------------------------------------------


def test_exec_returns_only_the_command_output_and_its_exit_code(tmux, workdir):
    _launch(tmux, workdir, 1)
    pane = _settle(tmux, "worker 1")
    result = tmux.exec(pane, "echo alpha; echo beta", timeout=15)
    assert result["state"] == "done" and result["exit_code"] == 0
    assert result["output"].splitlines() == ["alpha", "beta"]
    # neither the echo of what was typed nor what was on screen before leaks in
    assert "printf" not in result["output"] and "ready-1" not in result["output"]


def test_exec_reports_a_failing_exit_code(tmux, workdir):
    _launch(tmux, workdir, 1)
    pane = _settle(tmux, "worker 1")
    result = tmux.exec(pane, "sh -c 'echo nope >&2; exit 7'", timeout=15)
    assert result["exit_code"] == 7 and result["state"] == "done"
    assert "nope" in result["output"]


def test_exec_of_an_empty_result_is_empty_not_noise(tmux, workdir):
    _launch(tmux, workdir, 1)
    pane = _settle(tmux, "worker 1")
    assert tmux.exec(pane, "true", timeout=15)["output"] == ""


def test_exec_times_out_without_an_exit_code(tmux, workdir):
    _launch(tmux, workdir, 1)
    pane = _settle(tmux, "worker 1")
    result = tmux.exec(pane, "echo starting; sleep 5", timeout=2)
    assert result["state"] == "timeout" and result["exit_code"] is None
    assert "starting" in result["output"]
    tmux.send_key(tmux.resolve(pane.id), "C-c")


def test_exec_runs_a_heredoc_and_a_background_job_through_a_script(tmux, workdir):
    """Typed as one line this mixed the shell's echo into the output (see CHANGELOG 0.4.1)."""
    _launch(tmux, workdir, 1)
    pane = _settle(tmux, "worker 1")
    target = os.path.join(workdir, "written.txt")
    command = (
        f"cat <<'EOF' > {target}\nalpha\nbeta\nEOF\nwc -l < {target}\nsleep 3 &\necho launched"
    )
    result = tmux.exec(pane, command, timeout=20)
    assert result["state"] == "done" and result["exit_code"] == 0
    assert [ln.strip() for ln in result["output"].splitlines()] == ["2", "launched"]
    # none of the wrapper, the heredoc body or the shell prompt leaks into the output
    for leak in ("printf", "__ta_rc", "EOF", "$", "TAX"):
        assert leak not in result["output"]
    assert open(target).read() == "alpha\nbeta\n"
    script = result["script"]
    assert script and os.path.isfile(script) and "sleep 3 &" in open(script).read()
    # a command can carry a secret: the script must not be readable by anyone else
    assert stat.S_IMODE(os.stat(script).st_mode) == 0o600


def test_exec_of_a_one_line_command_needs_no_script(tmux, workdir):
    _launch(tmux, workdir, 1)
    pane = _settle(tmux, "worker 1")
    assert tmux.exec(pane, "echo plain", timeout=15)["script"] is None


def test_exec_refuses_a_busy_pane_unless_forced(tmux, workdir):
    _launch(tmux, workdir, 1)
    pane = _settle(tmux, "worker 1")
    tmux.send(pane, "sleep 5", enter=True)
    time.sleep(1.0)
    with pytest.raises(TmuxError):
        tmux.exec(tmux.resolve(pane.id), "echo hi", timeout=3)
    tmux.send_key(tmux.resolve(pane.id), "C-c")


# --- multi-pane waiting and sending ---------------------------------------------


def test_wait_any_returns_as_soon_as_one_pane_settles(tmux, workdir):
    _launch(tmux, workdir, 2)
    fast, slow = _settle(tmux, "worker 1"), _settle(tmux, "worker 2")
    tmux.send(slow, "sleep 20", enter=True)
    tmux.send(fast, "echo quick", enter=True)
    result = tmux.wait_many([fast, slow], mode="any", timeout=20, idle=1)
    assert result["state"] == "settled"
    assert result["settled_titles"] == ["worker 1"]
    assert [r["state"] for r in result["panes"]] == ["idle", "waiting"]
    tmux.send_key(tmux.resolve(slow.id), "C-c")


def test_wait_all_waits_for_the_slow_one(tmux, workdir):
    _launch(tmux, workdir, 2)
    a, b = _settle(tmux, "worker 1"), _settle(tmux, "worker 2")
    tmux.send(a, "echo one", enter=True)
    tmux.send(b, "sleep 3; echo two", enter=True)
    result = tmux.wait_many([a, b], mode="all", timeout=25, idle=1)
    assert result["state"] == "settled" and len(result["settled"]) == 2
    assert result["elapsed"] >= 3
    assert "two" in tmux.capture(tmux.resolve(b.id), 20)


def test_wait_any_matches_a_pattern_in_one_of_the_panes(tmux, workdir):
    _launch(tmux, workdir, 2)
    a, b = _settle(tmux, "worker 1"), _settle(tmux, "worker 2")
    tmux.send(b, "sleep 2; echo BATCH-DONE; sleep 20", enter=True)
    result = tmux.wait_many([a, b], mode="any", timeout=20, idle=30, pattern="BATCH-DONE")
    assert result["settled_titles"] == ["worker 2"]
    assert next(r for r in result["panes"] if r["pane"]["id"] == b.id)["state"] == "matched"
    tmux.send_key(tmux.resolve(b.id), "C-c")


def test_wait_all_times_out_and_says_which_pane_is_still_running(tmux, workdir):
    _launch(tmux, workdir, 2)
    a, b = _settle(tmux, "worker 1"), _settle(tmux, "worker 2")
    tmux.send(b, "sleep 20", enter=True)
    result = tmux.wait_many([a, b], mode="all", timeout=4, idle=1)
    assert result["state"] == "timeout"
    states = {r["pane"]["title"]: r["state"] for r in result["panes"]}
    assert states["worker 1"] == "idle" and states["worker 2"] == "running"
    tmux.send_key(tmux.resolve(b.id), "C-c")


def test_send_many_reaches_every_pane(tmux, workdir):
    panes = _launch(tmux, workdir, 3)
    settled = [_settle(tmux, f"worker {i}") for i in (1, 2, 3)]
    tmux.send_many(settled, "echo same-question")
    time.sleep(1.0)
    for pane in settled:
        assert "same-question" in tmux.capture(tmux.resolve(pane.id), 20)
    assert len(panes) == 3
    with pytest.raises(TmuxError):
        tmux.send_many([], "x")


# --- pane_clear / pane_swap / pane_scroll ---------------------------------------


def test_clear_pane_drops_the_screen_and_the_scrollback(tmux, workdir):
    _launch(tmux, workdir, 1)
    pane = _settle(tmux, "worker 1")
    tmux.exec(pane, "for i in $(seq 1 200); do echo line-$i; done", timeout=20)
    assert "line-200" in tmux.capture(tmux.resolve(pane.id), 300)
    assert tmux.probe(tmux.resolve(pane.id))[0] > 0  # there is scrollback

    result = tmux.clear_pane(tmux.resolve(pane.id))
    assert result["cleared_screen"] is True and result["history_size"] == 0
    after = tmux.capture(tmux.resolve(pane.id), 300)
    assert "line-200" not in after and "line-1" not in after


def test_clear_a_busy_pane_only_drops_the_history(tmux, workdir):
    _launch(tmux, workdir, 1)
    pane = _settle(tmux, "worker 1")
    tmux.exec(pane, "for i in $(seq 1 200); do echo old-$i; done", timeout=20)
    tmux.send(tmux.resolve(pane.id), "sleep 10", enter=True)
    time.sleep(1.0)
    result = tmux.clear_pane(tmux.resolve(pane.id))
    assert result["cleared_screen"] is False and result["history_size"] == 0
    tmux.send_key(tmux.resolve(pane.id), "C-c")


def test_swap_pane_exchanges_positions_not_identities(tmux, workdir):
    panes = _launch(tmux, workdir, 2)
    first, second = sorted(panes, key=lambda p: p.index)
    swapped = tmux.swap_panes(first, second)
    assert [p.title for p in swapped] == ["worker 2", "worker 1"]
    # same panes, same processes, just in the other position
    assert {p.id for p in swapped} == {first.id, second.id}
    assert tmux.resolve("worker 1").id == first.id
    with pytest.raises(TmuxError):
        tmux.swap_panes(first, first)


def test_scroll_pages_back_through_output_that_left_the_screen(tmux, workdir):
    _launch(tmux, workdir, 1)
    pane = _settle(tmux, "worker 1")
    tmux.exec(pane, "for i in $(seq 1 300); do echo row-$i; done", timeout=30)
    pane = tmux.resolve(pane.id)

    tail = tmux.scroll(pane, lines=20, offset=0)
    assert "row-300" in tail and "row-100" not in tail

    older = tmux.scroll(pane, lines=100, offset=150)
    assert "row-300" not in older and "row-150" in older
    assert len(older.splitlines()) <= 101
    with pytest.raises(TmuxError):
        tmux.scroll(pane, lines=0)


# --- windows ---------------------------------------------------------------------


def test_window_lifecycle(tmux, workdir):
    _launch(tmux, workdir, 1)
    assert [w.index for w in tmux.windows("t-demo")] == [1]  # pane-base-index 1

    made = tmux.new_window("t-demo", name="scratch", command="echo in-new-window", cwd=workdir)
    assert made.name == "scratch" and made.panes == 1
    assert [w.name for w in tmux.windows("t-demo")][-1] == "scratch"
    pane = [p for p in tmux.panes("t-demo") if p.window == made.index][0]
    tmux.wait(pane, timeout=10, idle=1)
    assert "in-new-window" in tmux.capture(pane, 20)

    renamed = tmux.rename_window(made.target, "verify")
    assert renamed.name == "verify" and renamed.index == made.index
    assert "verify" in {w.name for w in tmux.windows("t-demo")}
    assert "scratch" not in {w.name for w in tmux.windows("t-demo")}

    tmux.kill_window(made.target)
    assert made.index not in [w.index for w in tmux.windows("t-demo")]
    assert tmux.has_session("t-demo")  # the batch survived


def test_window_tools_respect_the_session_prefix_and_kill_switch(tmux, workdir):
    _launch(tmux, workdir, 1)
    with pytest.raises(TmuxError):
        tmux.new_window("other-session")
    with pytest.raises(TmuxError):
        tmux.rename_window("t-demo:1", "  ")


# --- attaching and keeping the machine awake ------------------------------------


def test_attach_command_actually_attaches(tmux, workdir):
    import pty
    import shlex

    _launch(tmux, workdir, 1)
    command = tmux.attach_command("t-demo")
    assert command.startswith("tmux -L ") and command.endswith("attach -t t-demo")
    assert not tmux.run("list-clients", "-t", "t-demo", check=False).strip()

    master, slave = pty.openpty()
    proc = subprocess.Popen(shlex.split(command), stdin=slave, stdout=slave, stderr=slave)
    try:
        for _ in range(40):
            time.sleep(0.25)
            if tmux.run("list-clients", "-t", "t-demo", check=False).strip():
                break
        assert tmux.run("list-clients", "-t", "t-demo", check=False).strip()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        for fd in (master, slave):
            try:
                import os

                os.close(fd)
            except OSError:
                pass
    with pytest.raises(TmuxError):
        tmux.attach_command("t-missing")


def test_keep_awake_starts_caffeinate_for_the_tmux_server(tmux, workdir):
    _launch(tmux, workdir, 1)
    result = tmux.keep_awake("t-demo")
    if sys.platform != "darwin":
        assert result["active"] is False and "not macOS" in result["reason"]
        return
    assert result["active"] is True
    pid = result["waits_on_pid"]
    assert pid == int(tmux.run("display-message", "-p", "-t", "=t-demo", "#{pid}").strip())
    found = subprocess.run(["pgrep", "-f", f"caffeinate -i -w {pid}"], capture_output=True)
    assert found.returncode == 0 and found.stdout.strip()
    # caffeinate is bound to the tmux server: killing the session ends it
    tmux.kill_session("t-demo")
    for _ in range(40):
        time.sleep(0.25)
        if subprocess.run(
            ["pgrep", "-f", f"caffeinate -i -w {pid}"], capture_output=True
        ).returncode:
            break
    assert subprocess.run(
        ["pgrep", "-f", f"caffeinate -i -w {pid}"], capture_output=True
    ).returncode


# --- the Stop hook typing into a conductor pane (real tmux, real send-keys) ------


def _tmux_shim(tmp_path, tmux, monkeypatch):
    """A `tmux` on PATH that always carries `-L <test socket>`.

    The hook calls plain `tmux` (it normally runs inside the pane, where $TMUX
    already points at the right server). The shim execs the *absolute* path of the
    real binary, so it cannot find itself again through PATH.
    """
    real = shutil.which("tmux")
    shim = tmp_path / "bin"
    shim.mkdir(exist_ok=True)
    args = " ".join(tmux.settings.base_args()[1:])
    (shim / "tmux").write_text(f'#!/bin/sh\nexec {real} {args} "$@"\n')
    (shim / "tmux").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")
    return shim


def _hook_transcript(path, text):
    import json

    path.write_text(
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    )
    return path


def test_hook_types_the_sentence_into_the_conductor_pane(tmux, workdir, tmp_path, monkeypatch):
    """`WP-G|@pane:orchestrator: ...` must land in the conductor, not in the worker."""
    import json

    from tmux_agents import hook

    tmux.launch(
        "t-demo",
        [
            AgentSpec("worker", "echo worker-ready; sh"),
            AgentSpec("orchestrator", "echo conductor-ready; sh"),
        ],
        cwd=workdir,
    )
    worker = _settle(tmux, "worker")
    conductor = _settle(tmux, "orchestrator")

    # the hook shells out to plain `tmux`; point that at this test's private server
    _tmux_shim(tmp_path, tmux, monkeypatch)

    pipeline = tmp_path / "pipe.txt"
    pipeline.write_text("WP-G|@pane:orchestrator: WP-G done. verify ~/wp/WP-G/handoff.md please\n")
    events = tmp_path / "events.jsonl"
    rc = hook.run(
        [
            "--pipeline",
            str(pipeline),
            "--pane",
            worker.id,
            "--delay",
            "0.2",
            "--events",
            str(events),
        ],
        stdin=json.dumps(
            {"transcript_path": str(_hook_transcript(tmp_path / "t.jsonl", "all set\nWP-G DONE"))}
        ),
    )
    assert rc == 0

    typed = tmux.wait(tmux.resolve(conductor.id), timeout=15, idle=1, pattern="verify .*handoff.md")
    assert typed["state"] == "matched"
    conductor_screen = tmux.capture(tmux.resolve(conductor.id), 50)
    assert "WP-G done. verify ~/wp/WP-G/handoff.md please" in conductor_screen
    # the worker's own pane was left alone
    assert "WP-G done" not in tmux.capture(tmux.resolve(worker.id), 50)

    row = json.loads(events.read_text().splitlines()[0])
    assert row["next"] == "@pane:orchestrator" and row["stage"] == "WP-G"
    assert row["session"] == "t-demo" and row["pane"] == "worker"
    assert "next: @pane:orchestrator" in (tmp_path / "pipe.log").read_text()


def test_hook_reports_a_conductor_pane_that_is_not_there(tmux, workdir, tmp_path, monkeypatch):
    import json

    from tmux_agents import hook

    tmux.launch("t-demo", [AgentSpec("worker", "sh")], cwd=workdir)
    worker = _settle(tmux, "worker")
    _tmux_shim(tmp_path, tmux, monkeypatch)

    pipeline = tmp_path / "pipe.txt"
    pipeline.write_text("WP-G|@pane:nobody-here: hello\n")
    hook.run(
        ["--pipeline", str(pipeline), "--pane", worker.id, "--events", str(tmp_path / "e.jsonl")],
        stdin=json.dumps(
            {"transcript_path": str(_hook_transcript(tmp_path / "t.jsonl", "WP-G DONE"))}
        ),
    )
    log = (tmp_path / "pipe.log").read_text()
    assert "no pane matches 'nobody-here'" in log
    row = json.loads((tmp_path / "e.jsonl").read_text().splitlines()[0])
    assert row["next"] is None and row["status"] == "DONE"
