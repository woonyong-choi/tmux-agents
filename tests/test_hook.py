from __future__ import annotations

import json

from tmux_agents import hook


def _transcript(tmp_path, text):
    t = tmp_path / "t.jsonl"
    rows = [
        {"type": "user", "message": {"content": "go"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "working..."}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
    ]
    t.write_text("\n".join(json.dumps(r) for r in rows))
    return t


def _pipeline(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "wp2.md").write_text("do WP2\nend with WP2 DONE")
    p = tmp_path / "pipe.txt"
    p.write_text("# demo\nWP1|prompts/wp2.md\nWP2|PAUSE\n")
    return p


def _run(tmp_path, text, pane="%9"):
    t = _transcript(tmp_path, text)
    p = _pipeline(tmp_path)
    rc = hook.run(
        ["--pipeline", str(p), "--pane", pane, "--dry-run"],
        stdin=json.dumps({"transcript_path": str(t)}),
    )
    log = (tmp_path / "pipe.log").read_text() if (tmp_path / "pipe.log").exists() else ""
    return rc, log


def test_marker_parsing():
    assert hook.find_marker("summary\n\nWP1 DONE\n") == ("WP1", "DONE")
    assert hook.find_marker('The prompt says end with "WP1 DONE" later\nWP1 STOPPED') == (
        "WP1",
        "STOPPED",
    )
    assert hook.find_marker("nothing here") is None
    # lenient: marker quoted inside a sentence near the end
    assert hook.find_marker("done.\n\nThe handoff's last line is WP2 DONE as required.") == (
        "WP2",
        "DONE",
    )
    # but not when it is far from the end
    assert hook.find_marker("end with WP2 DONE later\n" + "filler\n" * 10) is None


def test_done_advances_to_next_prompt(tmp_path):
    rc, log = _run(tmp_path, "all good\nWP1 DONE")
    assert rc == 0 and "WP1 DONE" in log and "next: prompts/wp2.md" in log


def test_stopped_halts(tmp_path):
    _, log = _run(tmp_path, "ran out of context\nWP1 STOPPED")
    assert "stopped" in log and "next:" not in log


def test_pause_halts(tmp_path):
    _, log = _run(tmp_path, "WP2 DONE")
    assert "PAUSE" in log


def test_unknown_stage_or_no_marker(tmp_path):
    _, log = _run(tmp_path, "WP9 DONE")
    assert "end of pipeline" in log
    t = _transcript(tmp_path, "no marker at all")
    args = ["--pipeline", str(tmp_path / "pipe.txt"), "--dry-run"]
    assert hook.run(args, stdin=json.dumps({"transcript_path": str(t)})) == 0


def test_no_pane_does_not_type(tmp_path):
    _, log = _run(tmp_path, "WP1 DONE", pane="")
    assert "no tmux pane" in log
