from tmux_agents.redact import redact, strip_ansi


def test_redacts_common_tokens():
    text = (
        "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz012345 "
        "sk-proj-ABCDEFGHIJKLMNOPQRSTUV "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdefghijklmnop "
        "api_key: 'super-secret-value-123' AKIAABCDEFGHIJKLMNOP"
    )
    out = redact(text)
    for leak in ("ghp_abcdefghijklmnop", "sk-proj-ABC", "eyJhbGci", "super-secret", "AKIAABCD"):
        assert leak not in out
    assert out.count("[redacted]") >= 4


def test_keeps_ordinary_text():
    text = "npm run verify passed: 365 tests, main.js 6.58 MB, token count 12"
    assert redact(text) == text


def test_strip_ansi():
    assert strip_ansi("\x1b[32mgreen\x1b[0m \x1b]0;title\x07x") == "green x"
