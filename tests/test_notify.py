from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tmux_agents import events, hook, notify


def _transcript(tmp_path, text):
    t = tmp_path / "t.jsonl"
    rows = [
        {"type": "user", "message": {"content": "go"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
    ]
    t.write_text("\n".join(json.dumps(r) for r in rows))
    return t


def _pipeline(tmp_path):
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "wp2.md").write_text("do WP2\nend with WP2 DONE")
    p = tmp_path / "pipe.txt"
    p.write_text("WP1|prompts/wp2.md\nWP2|PAUSE\n")
    return p


def _run(tmp_path, text, extra=None, pane="%9"):
    args = ["--pane", pane, "--dry-run", "--events", str(tmp_path / "events.jsonl")]
    return hook.run(
        args + (extra or []),
        stdin=json.dumps({"transcript_path": str(_transcript(tmp_path, text))}),
    )


# --- the record line ----------------------------------------------------------


def test_line_format_is_fixed():
    event = {
        "ts": "2026-09-23T04:11:07Z",
        "session": "manta",
        "pane": "wp-j",
        "stage": "WP2",
        "status": "DONE",
        "next": "prompts/wp3.md",
    }
    assert notify.format_line(event) == (
        "2026-09-23T04:11:07Z manta/wp-j WP2 DONE next=prompts/wp3.md"
    )
    event = {**event, "next": None, "status": "STOPPED", "session": "a b", "pane": ""}
    assert notify.format_line(event) == "2026-09-23T04:11:07Z a_b/- WP2 STOPPED next=none"


# --- file: ---------------------------------------------------------------------


def test_file_spec_writes_one_line_end_to_end(tmp_path):
    out = tmp_path / "sub" / "orchestrator.log"
    rc = _run(
        tmp_path,
        "all good\nWP1 DONE",
        ["--pipeline", str(_pipeline(tmp_path)), "--notify", f"file:{out}"],
    )
    assert rc == 0
    lines = out.read_text().splitlines()
    assert len(lines) == 1
    stamp, where, stage, status, nxt = lines[0].split(" ")
    assert (stage, status, nxt) == ("WP1", "DONE", "next=prompts/wp2.md")
    assert stamp.endswith("Z") and "/" in where


def test_notify_works_without_a_pipeline(tmp_path):
    out = tmp_path / "o.log"
    assert _run(tmp_path, "WP7 DONE", ["--notify", f"file:{out}"]) == 0
    assert out.read_text().split(" ")[2:4] == ["WP7", "DONE"]


def test_stage_missing_from_the_pipeline_is_still_reported(tmp_path):
    out = tmp_path / "o.log"
    _run(
        tmp_path, "WP9 STOPPED", ["--pipeline", str(_pipeline(tmp_path)), "--notify", f"file:{out}"]
    )
    assert out.read_text().strip().endswith("WP9 STOPPED next=none")


def test_a_failing_sink_does_not_break_the_hook(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(notify, "RETRY_PAUSE", 0.0)
    good = tmp_path / "good.log"
    blocked = tmp_path / "file-not-a-dir"
    blocked.write_text("in the way")
    rc = _run(
        tmp_path,
        "WP1 DONE",
        ["--notify", f"file:{blocked}/x.log", "--notify", f"file:{good}", "--notify", "bogus"],
    )
    assert rc == 0
    assert len(good.read_text().splitlines()) == 1
    assert capsys.readouterr().err.count("failed:") == 2


def test_two_notifies_both_fire(tmp_path):
    a, b = tmp_path / "a.log", tmp_path / "b.log"
    _run(tmp_path, "WP1 DONE", ["--notify", f"file:{a}", "--notify", f"file:{b}"])
    assert a.read_text() == b.read_text() and a.read_text().count("\n") == 1


# --- url: ----------------------------------------------------------------------


class _Sink(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Sink.received.append(json.loads(body.decode()))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args):  # keep pytest output clean
        pass


@pytest.fixture
def http_sink():
    _Sink.received = []
    server = HTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/hook", _Sink
    server.shutdown()


def test_url_spec_posts_the_event(tmp_path, http_sink):
    url, sink = http_sink
    assert _run(tmp_path, "note: see ~/wp/handoff.md\nWP1 DONE", ["--notify", f"url:{url}"]) == 0
    assert len(sink.received) == 1
    got = sink.received[0]
    assert got["stage"] == "WP1" and got["status"] == "DONE" and got["agent"] == "claude"
    assert got["handoff"] == "~/wp/handoff.md"
    assert set(got) >= {"ts", "agent", "session", "pane", "stage", "status", "message", "next"}


def test_url_failure_is_reported_and_retried(tmp_path, capsys, monkeypatch):
    tries = []
    real = notify.urllib.request.urlopen

    def boom(*a, **kw):
        tries.append(1)
        raise OSError("refused")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    monkeypatch.setattr(notify, "RETRY_PAUSE", 0.0)
    assert _run(tmp_path, "WP1 DONE", ["--notify", "url:http://127.0.0.1:9/x"]) == 0
    assert len(tries) == notify.ATTEMPTS == 2
    assert "notify url:" in capsys.readouterr().err
    assert real is not None


# --- gist: / repo: (a real `gh` on PATH, backed by a directory instead of GitHub) --


@pytest.fixture
def gh_store(tmp_path, monkeypatch):
    """Put tests/fakes/gh on PATH so the transports really shell out to `gh`."""
    store = tmp_path / "gh-store"
    monkeypatch.setenv("GH_FAKE_DIR", str(store))
    fakes = Path(__file__).parent / "fakes"
    monkeypatch.setenv("PATH", f"{fakes}{os.pathsep}{os.environ['PATH']}")
    return store


def test_gist_notifications_accumulate(tmp_path, gh_store):
    for text in ("WP1 DONE", "WP2 STOPPED"):
        _run(tmp_path, text, ["--notify", "gist:abc123:run.log"])
    kept = (gh_store / "gists" / "abc123" / "run.log").read_text().splitlines()
    assert [line.split(" ")[2:4] for line in kept] == [["WP1", "DONE"], ["WP2", "STOPPED"]]
    assert "gist view abc123 --filename run.log --raw" in (gh_store / "calls.log").read_text()


def test_gist_filename_defaults_and_the_first_write_creates_the_file(tmp_path, gh_store):
    _run(tmp_path, "WP1 DONE", ["--notify", "gist:abc123"])
    written = gh_store / "gists" / "abc123" / notify.DEFAULT_FILENAME
    assert written.read_text().strip().endswith("WP1 DONE next=none")


def test_repo_notifications_accumulate_one_commit_each(tmp_path, gh_store):
    for text in ("WP1 DONE", "WP2 DONE"):
        _run(tmp_path, text, ["--notify", "repo:me/notes:logs/run.log"])
    kept = (gh_store / "repo" / "logs" / "run.log").read_text().splitlines()
    assert len(kept) == 2 and kept[0].split(" ")[2:4] == ["WP1", "DONE"]
    commits = (gh_store / "commits.log").read_text().splitlines()
    assert commits == ["tmux-agents: WP1 DONE", "tmux-agents: WP2 DONE"]


def test_repo_path_defaults(tmp_path, gh_store):
    _run(tmp_path, "WP1 DONE", ["--notify", "repo:me/notes"])
    assert (gh_store / "repo" / notify.DEFAULT_FILENAME).read_text().strip().endswith("next=none")


def test_a_missing_gh_is_reported_and_the_hook_still_exits_zero(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(notify, "RETRY_PAUSE", 0.0)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))  # no gh anywhere
    out = tmp_path / "local.log"
    rc = _run(tmp_path, "WP1 DONE", ["--notify", "gist:abc123", "--notify", f"file:{out}"])
    assert rc == 0
    assert "gh not found" in capsys.readouterr().err
    assert out.read_text().strip().endswith("WP1 DONE next=none")  # the other sink still ran


def test_bad_spec_shapes_are_rejected():
    for spec in ["repo:notaslug", "gist:", "url:ftp://x", "nope:/tmp/x", "file"]:
        with pytest.raises(notify.NotifyError):
            notify.deliver(spec, "line", {"stage": "WP1", "status": "DONE"})


# --- the local event log --------------------------------------------------------


def test_every_turn_lands_in_the_event_log(tmp_path):
    log = tmp_path / "events.jsonl"
    _run(tmp_path, "WP1 DONE", ["--pipeline", str(_pipeline(tmp_path))])
    _run(tmp_path, "just talking, no marker")
    rows, next_since = events.read(path=log)
    assert next_since == 2
    assert [r["status"] for r in rows] == ["DONE", "TURN"]
    assert rows[0]["next"] == "prompts/wp2.md" and rows[1]["stage"] is None
    assert rows[0]["agent"] == "claude"
    fresh, _ = events.read(since_line=1, path=log)
    assert len(fresh) == 1 and fresh[0]["status"] == "TURN"


def test_no_events_flag_writes_nothing(tmp_path):
    log = tmp_path / "events.jsonl"
    hook.run(
        ["--no-events", "--events", str(log), "--dry-run"],
        stdin=json.dumps({"transcript_path": str(_transcript(tmp_path, "WP1 DONE"))}),
    )
    assert not log.exists()


def test_long_messages_are_clipped_to_the_tail():
    clipped = events.clip("x" * 9000 + "WP1 DONE", limit=events.MESSAGE_LIMIT)
    assert len(clipped.encode()) <= events.MESSAGE_LIMIT
    assert clipped.endswith("WP1 DONE")


# --- Codex ----------------------------------------------------------------------


def test_codex_payload_from_argv_normalizes_to_the_same_event(tmp_path):
    log = tmp_path / "events.jsonl"
    out = tmp_path / "o.log"
    payload = json.dumps(
        {
            "type": "agent-turn-complete",
            "turn-id": "t-1",
            "input-messages": ["do WP3"],
            "last-assistant-message": "wrote ~/woon-work/WP-J/handoff.md\n\nWP3 STOPPED",
        }
    )
    rc = hook.run(
        ["--agent", "codex", "--events", str(log), "--notify", f"file:{out}", payload],
        stdin="",
    )
    assert rc == 0
    (row,), _ = events.read(path=log)
    assert row["agent"] == "codex"
    assert (row["stage"], row["status"], row["next"]) == ("WP3", "STOPPED", None)
    assert row["handoff"] == "~/woon-work/WP-J/handoff.md"
    assert row["message"].endswith("WP3 STOPPED")
    assert out.read_text().strip().endswith("WP3 STOPPED next=none")


def test_codex_payload_on_stdin_also_works(tmp_path):
    log = tmp_path / "events.jsonl"
    hook.run(
        ["--agent", "codex", "--events", str(log), "--dry-run"],
        stdin=json.dumps(
            {"type": "agent-turn-complete", "last-assistant-message": "done here\nWP4 DONE"}
        ),
    )
    (row,), _ = events.read(path=log)
    assert (row["agent"], row["stage"], row["status"]) == ("codex", "WP4", "DONE")


def test_codex_advances_the_pipeline_like_claude_does(tmp_path):
    log = tmp_path / "events.jsonl"
    hook.run(
        [
            "--agent",
            "codex",
            "--pipeline",
            str(_pipeline(tmp_path)),
            "--pane",
            "%9",
            "--events",
            str(log),
            "--dry-run",
            json.dumps({"type": "agent-turn-complete", "last-assistant-message": "WP1 DONE"}),
        ],
        stdin="",
    )
    (row,), _ = events.read(path=log)
    assert row["next"] == "prompts/wp2.md"
    assert "next: prompts/wp2.md" in (tmp_path / "pipe.log").read_text()


def test_other_codex_notifications_are_ignored(tmp_path):
    log = tmp_path / "events.jsonl"
    rc = hook.run(
        ["--agent", "codex", "--events", str(log)],
        stdin=json.dumps({"type": "something-else", "last-assistant-message": "WP1 DONE"}),
    )
    assert rc == 0 and not log.exists()
