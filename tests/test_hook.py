from __future__ import annotations

import json
import threading
import time

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


# --- which message the hook reports -------------------------------------------
#
# `~/.tmux-agents/events.jsonl` recorded "Now the commit." for a turn whose pane had
# a long final report on it. The shape below is the one that produced it (taken from
# .claude/projects/.../8916357c-....jsonl): a one-liner, a tool call, its result, and
# then the report in an entry of its own.

REPORT = "완료했습니다.\n\n## 결과\n\n커밋 1개. push 안 함.\nWP-N DONE"


def _turn_rows(final: bool = True):
    rows = [
        {"type": "user", "message": {"content": "do WP-N"}},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Now the commit."}]},
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash"}]},
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]},
        },
    ]
    if final:
        rows.append(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": REPORT}]}}
        )
    return rows


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_the_report_after_the_last_tool_call_is_the_turns_message(tmp_path):
    t = _write(tmp_path / "t.jsonl", _turn_rows())
    assert hook.last_assistant_text(t) == REPORT
    assert hook.read_turn(t) == (REPORT, True)


def test_a_turn_still_being_written_is_not_settled(tmp_path):
    """The bug's mechanism: the hook starts before the report reaches the file."""
    t = _write(tmp_path / "t.jsonl", _turn_rows(final=False))
    text, settled = hook.read_turn(t)
    assert settled is False and text != REPORT


def test_the_last_line_is_waited_for_when_it_lands_late(tmp_path):
    t = _write(tmp_path / "t.jsonl", _turn_rows(final=False))
    rows = _turn_rows()

    def flush():
        time.sleep(0.3)
        _write(t, rows)

    worker = threading.Thread(target=flush)
    worker.start()
    try:
        assert hook.last_assistant_text(t, wait=2.0) == REPORT
    finally:
        worker.join()


def test_waiting_gives_up_and_reports_what_it_has(tmp_path):
    t = _write(tmp_path / "t.jsonl", _turn_rows(final=False))
    start = time.monotonic()
    assert hook.last_assistant_text(t, wait=0.4) == "Now the commit."
    assert time.monotonic() - start < 1.5


def test_every_text_block_of_the_turn_is_kept(tmp_path):
    rows = _turn_rows(final=False)
    rows.append(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "part one"},
                    {"type": "text", "text": "part two"},
                ]
            },
        }
    )
    t = _write(tmp_path / "t.jsonl", rows)
    assert hook.last_assistant_text(t) == "part one\n\npart two"


def test_a_subagents_message_is_not_the_turns_message(tmp_path):
    rows = _turn_rows()
    rows.append(
        {
            "type": "assistant",
            "isSidechain": True,
            "message": {"content": [{"type": "text", "text": "subagent finished"}]},
        }
    )
    t = _write(tmp_path / "t.jsonl", rows)
    assert hook.last_assistant_text(t) == REPORT


def test_the_stdin_field_beats_a_transcript_that_is_behind(tmp_path):
    """Claude Code's Stop hook carries the final message; prefer it over the file."""
    t = _write(tmp_path / "t.jsonl", _turn_rows(final=False))
    payload = {"transcript_path": str(t), "last_assistant_message": REPORT}
    assert hook.message_of("claude", payload) == REPORT


def test_the_transcript_is_still_the_fallback(tmp_path):
    t = _write(tmp_path / "t.jsonl", _turn_rows())
    assert hook.message_of("claude", {"transcript_path": str(t)}) == REPORT
    blank = {"transcript_path": str(t), "last_assistant_message": " "}
    assert hook.message_of("claude", blank) == REPORT


def test_the_event_carries_the_report_not_the_one_liner(tmp_path, monkeypatch):
    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("TMUX_AGENTS_EVENTS", str(events))
    t = _write(tmp_path / "t.jsonl", _turn_rows())
    rc = hook.run(
        ["--pane", "", "--dry-run"],
        stdin=json.dumps({"transcript_path": str(t), "last_assistant_message": REPORT}),
    )
    assert rc == 0
    written = json.loads(events.read_text(encoding="utf-8").splitlines()[-1])
    assert written["message"] == REPORT
    assert written["stage"] == "WP-N" and written["status"] == "DONE"
